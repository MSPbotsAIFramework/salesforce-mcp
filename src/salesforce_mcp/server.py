# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import AuthFlow, SalesforceClient, SalesforceCredentials
from .config import Settings

HEADER_INSTANCE_URL = "X-Salesforce-Instance-Url"
HEADER_AUTH_MODE = "X-Salesforce-Auth-Mode"
HEADER_CLIENT_ID = "X-Salesforce-Client-Id"
HEADER_CLIENT_SECRET = "X-Salesforce-Client-Secret"
HEADER_USERNAME = "X-Salesforce-Username"
HEADER_PASSWORD = "X-Salesforce-Password"
HEADER_TOKEN = "X-Salesforce-Token"

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}
# Accepted spellings of the auth mode header; blank means "decide from the fields".
_MODES: dict[str, AuthFlow] = {
    "client_credentials": "client_credentials",
    "client-credentials": "client_credentials",
    "clientcredentials": "client_credentials",
    "username_password": "username_password",
    "username-password": "username_password",
    "password": "username_password",
}

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[SalesforceCredentials | None] = contextvars.ContextVar(
    "salesforce_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("salesforce_request_id", default="-")

INSTRUCTIONS = (
    "Salesforce is a CRM. This server reads and, on request, writes one "
    "Salesforce org through the REST API, with the permissions of the "
    "connected user / integration user.\n"
    "Usage:\n"
    "- Unknown object or field names: salesforce_search_objects(pattern) -> "
    "salesforce_describe_object(objectName) for exact field API names, types, "
    "picklist values and relationships.\n"
    "- Records: salesforce_query_records(SOQL SELECT); pass next_records_url "
    "from a result to read the next batch. Rollups: salesforce_aggregate_query "
    "(GROUP BY, COUNT, SUM, ...).\n"
    "- Find a name, email or phone anywhere: salesforce_search_all with SOSL, "
    "e.g. FIND {acme} IN ALL FIELDS RETURNING Account(Id,Name), Contact(Id,Name,Email).\n"
    "- Always add LIMIT to SOQL that may match many rows.\n"
    "salesforce_dml_records inserts, updates, upserts or deletes live CRM "
    "records: call it only when the user explicitly asks, with the exact "
    "records given; delete also needs confirm=true and should follow a query "
    "that shows the user what will be deleted."
)


def get_client_from_context() -> SalesforceClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return SalesforceClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


def normalize_instance_url(value: str) -> str | None:
    """https origin of the org (path dropped), or None when unusable."""
    text = value.strip()
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if not parts.hostname:
        return None
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS):
        return None
    return f"{parts.scheme}://{parts.netloc}"


def resolve_flow(mode: str | None, has_client: bool, has_user: bool) -> AuthFlow | None:
    """The auth flow to use: the explicit mode, else whichever credentials are complete."""
    if mode:
        return _MODES.get(mode.lower())
    if has_client and not has_user:
        return "client_credentials"
    if has_user:
        return "username_password"
    return "client_credentials"


class GatewayCredentialsMiddleware:
    """Reads the Salesforce headers on /mcp requests into the request context.

    Needs X-Salesforce-Instance-Url plus either Client-Id + Client-Secret
    (client credentials) or Username + Password (username / password, with
    the optional security token); otherwise 401.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        def header(name: str) -> str | None:
            return _clean(request.headers.get(name))

        instance_raw = header(HEADER_INSTANCE_URL)
        client_id, client_secret = header(HEADER_CLIENT_ID), header(HEADER_CLIENT_SECRET)
        username, password = header(HEADER_USERNAME), header(HEADER_PASSWORD)
        mode = header(HEADER_AUTH_MODE)

        flow = resolve_flow(mode, bool(client_id and client_secret), bool(username and password))
        if mode and flow is None:
            await self._reject(scope, receive, send, 400, f"{HEADER_AUTH_MODE} must be client_credentials or username_password")
            return
        missing = [] if instance_raw else [HEADER_INSTANCE_URL]
        if flow == "client_credentials":
            missing += [n for n, v in ((HEADER_CLIENT_ID, client_id), (HEADER_CLIENT_SECRET, client_secret)) if not v]
        else:
            missing += [n for n, v in ((HEADER_USERNAME, username), (HEADER_PASSWORD, password)) if not v]
        if missing:
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s) for the {flow} flow: {', '.join(missing)}",
                    "required_headers": [HEADER_INSTANCE_URL],
                    "required_one_of": [[HEADER_CLIENT_ID, HEADER_CLIENT_SECRET], [HEADER_USERNAME, HEADER_PASSWORD]],
                    "optional_headers": [HEADER_AUTH_MODE, HEADER_TOKEN],
                },
                status_code=401,
            )(scope, receive, send)
            return

        instance_url = normalize_instance_url(instance_raw or "")
        if instance_url is None:
            await self._reject(scope, receive, send, 400, f"{HEADER_INSTANCE_URL} must be an https URL such as https://acme.my.salesforce.com")
            return

        credentials = SalesforceCredentials(
            instance_url=instance_url,
            auth_flow=flow or "client_credentials",
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
            security_token=header(HEADER_TOKEN),
        )
        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(credentials)
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, status: int, message: str) -> None:
        await JSONResponse({"error": "Invalid header", "message": message}, status_code=status)(scope, receive, send)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = FastMCP(
        name="salesforce-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], SalesforceClient | None] = get_client_from_context

    from .tools import data, schema

    for module in (schema, data):
        module.register(mcp, client_factory)

    return mcp
