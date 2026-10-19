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
"""Credential middleware: flow selection, 401/400 on bad headers, isolation."""

import asyncio

import pytest
from starlette.testclient import TestClient

from salesforce_mcp.__main__ import build_http_app
from salesforce_mcp.server import (
    GatewayCredentialsMiddleware,
    _credentials_var,
    create_mcp_server,
    get_client_from_context,
    normalize_instance_url,
    resolve_flow,
)

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
INSTANCE = {"X-Salesforce-Instance-Url": "https://acme.my.salesforce.com"}
CLIENT = {"X-Salesforce-Client-Id": "consumer-key", "X-Salesforce-Client-Secret": "consumer-secret"}
USER = {"X-Salesforce-Username": "user@example.com", "X-Salesforce-Password": "pw"}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers,missing",
    [
        (CLIENT, "X-Salesforce-Instance-Url"),
        (INSTANCE, "X-Salesforce-Client-Id"),
        (INSTANCE | {"X-Salesforce-Client-Id": "k"}, "X-Salesforce-Client-Secret"),
        (INSTANCE | {"X-Salesforce-Auth-Mode": "username_password", "X-Salesforce-Username": "u"}, "X-Salesforce-Password"),
        (INSTANCE | CLIENT | {"X-Salesforce-Client-Secret": "${SF_SECRET}"}, "X-Salesforce-Client-Secret"),
    ],
)
def test_missing_credentials_return_401(headers, missing):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert missing in resp.json()["message"]


@pytest.mark.parametrize(
    "extra",
    [{"X-Salesforce-Instance-Url": "http://acme.my.salesforce.com"}, {"X-Salesforce-Auth-Mode": "jwt"}],
)
def test_bad_values_return_400(extra):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | INSTANCE | CLIENT | extra)
    assert resp.status_code == 400


@pytest.mark.parametrize("auth", [CLIENT, USER])
def test_tools_list_over_http(auth):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | INSTANCE | auth)
    assert resp.status_code == 200
    assert "salesforce_dml_records" in resp.text


@pytest.mark.parametrize(
    "mode,has_client,has_user,expected",
    [
        (None, True, False, "client_credentials"),
        (None, False, True, "username_password"),
        (None, True, True, "username_password"),
        (None, False, False, "client_credentials"),
        ("username_password", True, False, "username_password"),
        ("Client_Credentials", False, True, "client_credentials"),
        ("password", False, True, "username_password"),
        ("jwt", True, False, None),
    ],
)
def test_resolve_flow(mode, has_client, has_user, expected):
    assert resolve_flow(mode, has_client, has_user) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://acme.my.salesforce.com", "https://acme.my.salesforce.com"),
        ("acme.my.salesforce.com/", "https://acme.my.salesforce.com"),
        ("https://acme.lightning.force.com/lightning/page/home", "https://acme.lightning.force.com"),
        ("http://acme.my.salesforce.com", None),
        ("https://", None),
    ],
)
def test_normalize_instance_url(value, expected):
    assert normalize_instance_url(value) == expected


def _run_middleware(headers: dict[str, str]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": raw}, receive, send))
    return seen


def test_user_flow_reaches_context_and_is_reset():
    seen = _run_middleware(INSTANCE | USER | {"X-Salesforce-Token": "sectok"})
    creds = seen["credentials"]
    assert (creds.auth_flow, creds.username, creds.security_token) == ("username_password", "user@example.com", "sectok")
    assert seen["client"].instance_url == "https://acme.my.salesforce.com"
    assert _credentials_var.get() is None


def test_concurrent_requests_do_not_share_credentials():
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().client_id
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().client_id))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(key: str) -> dict:
        headers = INSTANCE | {"X-Salesforce-Client-Id": key, "X-Salesforce-Client-Secret": "s"}
        return {"type": "http", "path": "/mcp", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}

    async def both():
        await asyncio.gather(middleware(scope("tenant-a"), receive, send), middleware(scope("tenant-b"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")]


def test_credentials_repr_hides_secrets():
    seen = _run_middleware(INSTANCE | {"X-Salesforce-Client-Id": "id-value", "X-Salesforce-Client-Secret": "very-secret-value"})
    text = repr(seen["credentials"])
    assert "very-secret-value" not in text and "id-value" not in text
