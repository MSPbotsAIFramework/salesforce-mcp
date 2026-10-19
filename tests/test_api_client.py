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
"""Auth flows, retries and request shapes against a mocked Salesforce org."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from salesforce_mcp import api_client
from salesforce_mcp.api_client import SalesforceClient, SalesforceCredentials, SalesforceError
from salesforce_mcp.server import _credentials_var, create_mcp_server

INSTANCE = "https://yourcompany.my.salesforce.com"
CC = SalesforceCredentials(instance_url=INSTANCE, auth_flow="client_credentials", client_id="ck", client_secret="cs")
DATA = "/services/data/v62.0/"
ID1, ID2 = "001000000000001AAA", "001000000000002AAA"

SOAP_OK = (
    '<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns="urn:partner.soap.sforce.com"><soapenv:Body><loginResponse><result>'
    "<serverUrl>https://yourcompany.my.salesforce.com/services/Soap/u/62.0/00D000000000001</serverUrl>"
    "<sessionId>SESSION-1</sessionId></result></loginResponse></soapenv:Body></soapenv:Envelope>"
)
SOAP_FAULT = (
    '<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
    "<soapenv:Body><soapenv:Fault><faultcode>INVALID_LOGIN</faultcode>"
    "<faultstring>INVALID_LOGIN: Invalid username, password, security token; or user locked out.</faultstring>"
    "</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


def token_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "TOKEN-1", "instance_url": INSTANCE, "token_type": "Bearer"})


class Recorder:
    """Mock transport: replies by "METHOD path" or path (the REST part after /services/data/vXX.X/)."""

    def __init__(self, routes=None, default=None):
        self.requests: list[httpx.Request] = []
        self.routes = {"/services/oauth2/token": token_ok} | (routes or {})
        self.default = default if default is not None else {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        key = path.removeprefix(DATA) if path.startswith(DATA) else path
        reply = self.routes.get(f"{request.method} {key}", self.routes.get(key, self.default))
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, key: str, method: str | None = None) -> list[httpx.Request]:
        return [
            r for r in self.requests
            if (r.url.path.removeprefix(DATA) if r.url.path.startswith(DATA) else r.url.path) == key
            and (method is None or r.method == method)
        ]


def body(request: httpx.Request):
    return json.loads(request.content) if request.content else None


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict, credentials: SalesforceCredentials = CC):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(credentials)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _client(recorder: Recorder, credentials: SalesforceCredentials = CC) -> SalesforceClient:
    return SalesforceClient(credentials, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


# ---- authentication --------------------------------------------------------


async def test_client_credentials_flow():
    rec = Recorder({"sobjects": {"sobjects": []}})
    await _client(rec).get("sobjects")
    token, call = rec.requests
    assert str(token.url) == f"{INSTANCE}/services/oauth2/token"
    assert parse_qs(token.content.decode()) == {"grant_type": ["client_credentials"], "client_id": ["ck"], "client_secret": ["cs"]}
    assert call.headers["Authorization"] == "Bearer TOKEN-1"
    assert str(call.url) == f"{INSTANCE}/services/data/v62.0/sobjects"


async def test_password_grant_with_connected_app_appends_security_token():
    creds = SalesforceCredentials(
        instance_url=INSTANCE, auth_flow="username_password", client_id="ck", client_secret="cs",
        username="u@example.com", password="pw", security_token="TOK",
    )
    rec = Recorder()
    await _client(rec, creds).get("limits")
    form = parse_qs(rec.requests[0].content.decode())
    assert form["grant_type"] == ["password"] and form["password"] == ["pwTOK"] and form["username"] == ["u@example.com"]


async def test_soap_login_without_connected_app():
    creds = SalesforceCredentials(instance_url=INSTANCE, auth_flow="username_password", username="u&me@example.com", password="p<w", security_token="T")
    rec = Recorder({"/services/Soap/u/62.0": lambda r: httpx.Response(200, text=SOAP_OK)})
    await _client(rec, creds).get("limits")
    login, call = rec.requests
    assert login.headers["SOAPAction"] == "login"
    assert b"<urn:username>u&amp;me@example.com</urn:username>" in login.content
    assert b"<urn:password>p&lt;wT</urn:password>" in login.content
    assert call.headers["Authorization"] == "Bearer SESSION-1"
    assert str(call.url).startswith("https://yourcompany.my.salesforce.com/services/data/")


async def test_soap_login_fault_is_unauthorized(monkeypatch):
    creds = SalesforceCredentials(instance_url=INSTANCE, auth_flow="username_password", username="u", password="pw-value")
    rec = Recorder({"/services/Soap/u/62.0": lambda r: httpx.Response(500, text=SOAP_FAULT)})
    result = await _call(monkeypatch, rec, "salesforce_search_objects", {"pattern": "acc"}, creds)
    assert result["error"]["code"] == "unauthorized"
    assert "INVALID_LOGIN" in result["error"]["message"] and "pw-value" not in json.dumps(result)


async def test_oauth_error_is_unauthorized(monkeypatch):
    rec = Recorder({"/services/oauth2/token": lambda r: httpx.Response(400, json={"error": "invalid_client", "error_description": "invalid client credentials"})})
    result = await _call(monkeypatch, rec, "salesforce_search_objects", {"pattern": "acc"})
    assert result["error"]["code"] == "unauthorized"
    assert "invalid client credentials" in result["error"]["message"]


async def test_expired_session_reauthenticates_once():
    tokens = iter(["OLD", "NEW"])
    rec = Recorder(
        {
            "/services/oauth2/token": lambda r: httpx.Response(200, json={"access_token": next(tokens)}),
            "sobjects": lambda r: httpx.Response(
                200 if r.headers["Authorization"] == "Bearer NEW" else 401,
                json={"sobjects": []} if r.headers["Authorization"] == "Bearer NEW" else [{"errorCode": "INVALID_SESSION_ID", "message": "expired"}],
            ),
        }
    )
    assert await _client(rec).get("sobjects") == {"sobjects": []}
    assert len(rec.calls("/services/oauth2/token")) == 2


async def test_errors_are_flattened(monkeypatch):
    error = [{"message": "unexpected token: FORM", "errorCode": "MALFORMED_QUERY"}]
    rec = Recorder({"query": lambda r: httpx.Response(400, json=error)})
    result = await _call(monkeypatch, rec, "salesforce_query_records", {"soql": "SELECT Id FORM Account"})
    assert result["error"]["code"] == "invalid_argument"
    assert "MALFORMED_QUERY: unexpected token: FORM" in result["error"]["message"]


async def test_reads_retry_but_writes_do_not():
    replies = iter([httpx.Response(503), httpx.Response(200, json={"sobjects": []})])
    rec = Recorder({"GET sobjects": lambda r: next(replies), "POST composite/sobjects": lambda r: httpx.Response(503)})
    client = _client(rec)
    assert await client.get("sobjects") == {"sobjects": []}
    with pytest.raises(SalesforceError):
        await client.request("POST", "composite/sobjects", body={"records": []})
    assert len(rec.calls("sobjects")) == 2 and len(rec.calls("composite/sobjects")) == 1


# ---- schema ----------------------------------------------------------------


async def test_search_objects_matches_name_or_label(monkeypatch):
    sobjects = [
        {"name": "Account", "label": "Account", "custom": False, "queryable": True},
        {"name": "Invoice__c", "label": "Customer Invoice", "custom": True, "queryable": True},
        {"name": "Case", "label": "Case"},
    ]
    rec = Recorder({"sobjects": {"sobjects": sobjects}})
    result = await _call(monkeypatch, rec, "salesforce_search_objects", {"pattern": "CUSTOMER"})
    assert [o["name"] for o in result["objects"]] == ["Invoice__c"]


async def test_describe_object_trims_fields(monkeypatch):
    meta = {
        "name": "Opportunity", "label": "Opportunity", "custom": False,
        "fields": [
            {"name": "StageName", "label": "Stage", "type": "picklist", "nillable": False, "updateable": True,
             "picklistValues": [{"value": "Prospecting", "label": "Prospecting", "active": True}, {"value": "Old", "label": "Old", "active": False}]},
            {"name": "AccountId", "label": "Account ID", "type": "reference", "referenceTo": ["Account"], "relationshipName": "Account"},
        ],
        "childRelationships": [{"relationshipName": "OpportunityLineItems", "childSObject": "OpportunityLineItem", "field": "OpportunityId"},
                               {"relationshipName": None, "childSObject": "X", "field": "Y"}],
    }
    rec = Recorder({"sobjects/Opportunity/describe": meta})
    result = await _call(monkeypatch, rec, "salesforce_describe_object", {"objectName": "Opportunity"})
    stage, account = result["fields"]
    assert stage["picklistValues"] == [{"value": "Prospecting", "label": "Prospecting"}] and stage["updateable"] is True
    assert account["referenceTo"] == ["Account"] and account["relationshipName"] == "Account"
    assert len(result["childRelationships"]) == 1


async def test_object_names_are_validated(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "salesforce_describe_object", {"objectName": "../limits"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


# ---- queries ---------------------------------------------------------------


async def test_query_strips_attributes_and_returns_next_url(monkeypatch):
    payload = {
        "totalSize": 3000, "done": False, "nextRecordsUrl": "/services/data/v62.0/query/01gD0000002HU6KIAW-2000",
        "records": [{"attributes": {"type": "Account", "url": "/x"}, "Id": ID1, "Owner": {"attributes": {"type": "User"}, "Name": "Ann"}}],
    }
    rec = Recorder({"query": payload})
    result = await _call(monkeypatch, rec, "salesforce_query_records", {"soql": "SELECT Id, Owner.Name FROM Account"})
    assert rec.calls("query")[0].url.params["q"] == "SELECT Id, Owner.Name FROM Account"
    assert result["records"] == [{"_type": "Account", "Id": ID1, "Owner": {"_type": "User", "Name": "Ann"}}]
    assert result["nextRecordsUrl"].endswith("-2000")


async def test_query_next_batch(monkeypatch):
    rec = Recorder({"query/01gD0000002HU6KIAW-2000": {"totalSize": 3000, "done": True, "records": []}})
    result = await _call(
        monkeypatch, rec, "salesforce_query_records",
        {"soql": "ignored", "next_records_url": "/services/data/v62.0/query/01gD0000002HU6KIAW-2000"},
    )
    assert result["done"] is True


@pytest.mark.parametrize(
    "args",
    [
        {"soql": "DELETE FROM Account"},
        {"soql": "SELECT Id FROM Account", "next_records_url": "https://evil.example.com/x"},
        {"soql": "SELECT Id FROM Account", "next_records_url": "/services/data/v62.0/sobjects/Account"},
    ],
)
async def test_bad_queries_are_rejected(monkeypatch, args):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "salesforce_query_records", args)
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_aggregate_query(monkeypatch):
    rec = Recorder({"query": {"totalSize": 2, "done": True, "records": [{"attributes": {"type": "AggregateResult"}, "StageName": "Won", "expr0": 4}]}})
    result = await _call(monkeypatch, rec, "salesforce_aggregate_query", {"soql": "SELECT StageName, COUNT(Id) FROM Opportunity GROUP BY StageName"})
    assert result["records"] == [{"_type": "AggregateResult", "StageName": "Won", "expr0": 4}]


async def test_sosl_search(monkeypatch):
    rec = Recorder({"search": {"searchRecords": [{"attributes": {"type": "Contact"}, "Id": "003000000000001AAA", "Name": "Jane"}]}})
    result = await _call(monkeypatch, rec, "salesforce_search_all", {"sosl": "FIND {jane} IN ALL FIELDS RETURNING Contact(Id,Name)"})
    assert result == {"searchRecordCount": 1, "searchRecords": [{"_type": "Contact", "Id": "003000000000001AAA", "Name": "Jane"}]}
    bad = await _call(monkeypatch, Recorder(), "salesforce_search_all", {"sosl": "SELECT Id FROM Contact"})
    assert bad["error"]["code"] == "invalid_argument"


# ---- DML -------------------------------------------------------------------


async def test_insert_uses_collections(monkeypatch):
    rec = Recorder({"POST composite/sobjects": [{"id": ID1, "success": True, "errors": []}, {"success": False, "errors": [{"message": "Required fields are missing: [Name]"}]}]})
    result = await _call(monkeypatch, rec, "salesforce_dml_records", {"operation": "insert", "objectName": "Account", "records": [{"Name": "Example Co"}, {}]})
    assert body(rec.calls("composite/sobjects", "POST")[0]) == {
        "allOrNone": False,
        "records": [{"attributes": {"type": "Account"}, "Name": "Example Co"}, {"attributes": {"type": "Account"}}],
    }
    assert (result["succeeded"], result["failed"]) == (1, 1)


async def test_update_requires_ids(monkeypatch):
    rec = Recorder({"PATCH composite/sobjects": [{"id": ID1, "success": True}]})
    ok = await _call(monkeypatch, rec, "salesforce_dml_records", {"operation": "update", "objectName": "Account", "records": [{"Id": ID1, "Rating": "Hot"}]})
    assert ok["succeeded"] == 1
    bad = await _call(monkeypatch, Recorder(), "salesforce_dml_records", {"operation": "update", "objectName": "Account", "records": [{"Rating": "Hot"}]})
    assert bad["error"]["code"] == "invalid_argument"


async def test_upsert_path_and_key_check(monkeypatch):
    rec = Recorder({"PATCH composite/sobjects/Account/External_Id__c": [{"id": ID1, "success": True, "created": False}]})
    args = {"operation": "upsert", "objectName": "Account", "externalIdField": "External_Id__c", "records": [{"External_Id__c": "A-1", "Name": "Example Co"}]}
    result = await _call(monkeypatch, rec, "salesforce_dml_records", args)
    assert result["externalIdField"] == "External_Id__c" and result["succeeded"] == 1
    missing = await _call(monkeypatch, Recorder(), "salesforce_dml_records", {**args, "records": [{"Name": "x"}]})
    assert missing["error"]["code"] == "invalid_argument"
    no_field = await _call(monkeypatch, Recorder(), "salesforce_dml_records", {**args, "externalIdField": None})
    assert no_field["error"]["code"] == "invalid_argument"


async def test_delete_needs_confirm(monkeypatch):
    args = {"operation": "delete", "objectName": "Account", "records": [{"Id": ID1}, {"Id": ID2}]}
    rec = Recorder()
    refused = await _call(monkeypatch, rec, "salesforce_dml_records", args)
    assert refused["error"]["code"] == "invalid_argument" and "confirm=true" in refused["error"]["message"]
    assert rec.requests == []
    rec = Recorder({"DELETE composite/sobjects": [{"id": ID1, "success": True}, {"id": ID2, "success": True}]})
    result = await _call(monkeypatch, rec, "salesforce_dml_records", {**args, "confirm": True})
    params = rec.calls("composite/sobjects", "DELETE")[0].url.params
    assert (params["ids"], params["allOrNone"]) == (f"{ID1},{ID2}", "false")
    assert result["succeeded"] == 2


async def test_dml_batch_limit(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "salesforce_dml_records", {"operation": "insert", "objectName": "Account", "records": [{"Name": "x"}] * 201})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []
