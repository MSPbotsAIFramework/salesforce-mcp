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
"""Async client for the Salesforce REST API, with OAuth / SOAP login."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import httpx

from ._json import error_envelope
from .config import API_VERSION, DEFAULT_LOGIN_URL

logger = logging.getLogger("salesforce_mcp.api")

_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=20.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

AuthFlow = Literal["client_credentials", "username_password"]

# One connection pool for the process. It never holds credentials: the
# Authorization header is built per request from the caller's own token, so
# sharing the pool across tenants is safe.
_shared_http_client: httpx.AsyncClient | None = None


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _shared_http_client


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class SalesforceError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Salesforce API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)


@dataclass(frozen=True)
class SalesforceCredentials:
    instance_url: str
    auth_flow: AuthFlow
    client_id: str | None = None
    client_secret: str | None = None
    username: str | None = None
    password: str | None = None
    security_token: str | None = None

    def __repr__(self) -> str:  # never leak secrets into logs or tracebacks
        return f"SalesforceCredentials(instance_url={self.instance_url!r}, auth_flow={self.auth_flow!r}, secrets=<redacted>)"


class SalesforceClient:
    """Salesforce REST wrapper for one caller's credentials.

    The access token is obtained lazily and kept only on this instance, which
    lives for a single MCP request; nothing is cached across requests.
    """

    def __init__(self, credentials: SalesforceCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.instance_url = credentials.instance_url.rstrip("/")
        self._token: str | None = None

    @property
    def auth_flow(self) -> AuthFlow:
        return self._credentials.auth_flow

    def _client(self) -> httpx.AsyncClient:
        return self._http or _get_shared_http_client()

    # ---- authentication -------------------------------------------------

    async def _oauth(self, url: str, form: dict[str, str]) -> None:
        try:
            resp = await self._client().post(url, data=form, headers={"Accept": "application/json"})
        except httpx.RequestError as e:
            raise SalesforceError(0, f"Network error requesting a Salesforce token: {type(e).__name__}") from e
        logger.info("salesforce_api method=POST path=/services/oauth2/token status=%s", resp.status_code)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code >= 400 or not payload.get("access_token"):
            detail = payload.get("error_description") or payload.get("error") or f"HTTP {resp.status_code}"
            status = 401 if resp.status_code in (400, 401, 403) else (resp.status_code if resp.status_code >= 400 else 502)
            raise SalesforceError(status, f"Salesforce rejected the {self.auth_flow} login: {detail}")
        self._token = payload["access_token"]
        if payload.get("instance_url"):
            self.instance_url = str(payload["instance_url"]).rstrip("/")

    async def _soap_login(self) -> None:
        """SOAP login() for the username / password flow without a connected app."""
        creds = self._credentials
        password = (creds.password or "") + (creds.security_token or "")
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:urn="urn:partner.soap.sforce.com"><env:Body><urn:login>'
            f"<urn:username>{escape(creds.username or '')}</urn:username>"
            f"<urn:password>{escape(password)}</urn:password>"
            "</urn:login></env:Body></env:Envelope>"
        )
        url = f"{self._login_url()}/services/Soap/u/{API_VERSION}"
        headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "login"}
        try:
            resp = await self._client().post(url, content=envelope.encode("utf-8"), headers=headers)
        except httpx.RequestError as e:
            raise SalesforceError(0, f"Network error logging in to Salesforce: {type(e).__name__}") from e
        logger.info("salesforce_api method=POST path=/services/Soap/u status=%s", resp.status_code)
        try:
            root = ElementTree.fromstring(resp.content)
        except ElementTree.ParseError as e:
            raise SalesforceError(502 if resp.status_code < 400 else resp.status_code, "Salesforce login returned an unreadable response") from e
        fault = _find(root, "faultstring")
        session, server = _find(root, "sessionId"), _find(root, "serverUrl")
        if fault or not session or not server:
            raise SalesforceError(401, f"Salesforce rejected the username / password login: {fault or 'no session returned'}")
        parts = urlsplit(server)
        self._token = session
        self.instance_url = f"{parts.scheme}://{parts.netloc}"

    def _login_url(self) -> str:
        return self.instance_url or DEFAULT_LOGIN_URL

    async def authenticate(self) -> None:
        creds = self._credentials
        if creds.auth_flow == "client_credentials":
            await self._oauth(
                f"{self.instance_url}/services/oauth2/token",
                {"grant_type": "client_credentials", "client_id": creds.client_id or "", "client_secret": creds.client_secret or ""},
            )
        elif creds.client_id and creds.client_secret:
            await self._oauth(
                f"{self._login_url()}/services/oauth2/token",
                {
                    "grant_type": "password",
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                    "username": creds.username or "",
                    "password": (creds.password or "") + (creds.security_token or ""),
                },
            )
        else:
            await self._soap_login()

    # ---- requests -------------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        """Call ``{instance}/services/data/vXX.X/{path}`` (or an absolute /services/... path)."""
        if not self._token:
            await self.authenticate()
        resp = await self._send(method, path, params, body)
        if resp.status_code == 401:
            await self.authenticate()
            resp = await self._send(method, path, params, body)
        if resp.status_code >= 400:
            raise SalesforceError(resp.status_code, _error_message(resp, path))
        return _parse_body(resp)

    def _url(self, path: str) -> str:
        if path.startswith("/services/"):
            return f"{self.instance_url}{path}"
        return f"{self.instance_url}/services/data/v{API_VERSION}/{path.lstrip('/')}"

    async def _send(self, method: str, path: str, params: dict[str, Any] | None, body: Any) -> httpx.Response:
        url = self._url(path)
        query = {k: _param_value(v) for k, v in (params or {}).items() if v is not None}
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        started = time.monotonic()
        attempt = 0
        log_path = urlsplit(url).path
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            error: SalesforceError | None = None
            try:
                resp = await self._client().request(method, url, params=query, json=body, headers=headers)
                status = resp.status_code
            except httpx.TimeoutException:
                status, error = 0, SalesforceError(0, f"Timed out calling Salesforce ({method} {log_path})")
            except httpx.RequestError as e:
                status = 0
                error = SalesforceError(0, f"Network error calling Salesforce ({method} {log_path}): {type(e).__name__}")

            logger.info(
                "salesforce_api method=%s path=%s status=%s ms=%d attempt=%d",
                method, log_path, status, (time.monotonic() - t0) * 1000, attempt,
            )

            # Only reads are retried, so a write never runs twice.
            retryable = method == "GET" and (status in (0, 429) or status >= 500)
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if error is not None:
                raise error
            assert resp is not None
            return resp


def _find(root: ElementTree.Element, local_name: str) -> str | None:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == local_name and element.text:
            return element.text.strip()
    return None


def _param_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)


def _parse_body(resp: httpx.Response) -> Any:
    if not resp.content or not resp.content.strip():
        return None
    try:
        return resp.json()
    except ValueError:
        return {"raw_response": resp.text[:2000]}


def _error_message(resp: httpx.Response, path: str) -> str:
    """Salesforce errors are a list of {message, errorCode, fields}."""
    fallback = f"Salesforce returned HTTP {resp.status_code} for {urlsplit(path).path or path}"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        return f"{fallback}: {text[:300]}" if text else fallback
    items = payload if isinstance(payload, list) else [payload]
    parts = []
    for item in items:
        if isinstance(item, dict) and (item.get("message") or item.get("errorCode")):
            code = f"{item.get('errorCode')}: " if item.get("errorCode") else ""
            parts.append(f"{code}{item.get('message', '')}".strip())
    return f"{fallback}: {' | '.join(parts)[:600]}" if parts else fallback
