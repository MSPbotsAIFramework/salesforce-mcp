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
"""Records: SOQL queries, SOSL search and DML through sObject collections."""

import re
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SalesforceClient
from ..config import MAX_DML_RECORDS
from ._common import DML, READ_ONLY, ClientFactory, api_name, run_tool, strip_attributes

_SELECT = re.compile(r"^\s*select\s", re.IGNORECASE)
_FIND = re.compile(r"^\s*find\s", re.IGNORECASE)
_NEXT_URL = re.compile(r"^/services/data/v\d+\.\d+/query(All)?/[A-Za-z0-9-]+$")
_RECORD_ID = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")


def _soql(value: str) -> str:
    text = (value or "").strip()
    if not _SELECT.match(text):
        raise ValueError("soql must be a SOQL SELECT statement.")
    return text


def _query_result(payload: Any) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    result = {
        "totalSize": payload.get("totalSize"),
        "done": payload.get("done"),
        "records": strip_attributes(payload.get("records") or []),
    }
    if payload.get("nextRecordsUrl"):
        result["nextRecordsUrl"] = payload["nextRecordsUrl"]
    return result


def _outcome(operation: str, object_name: str, results: Any, extra: dict | None = None) -> dict:
    rows = results if isinstance(results, list) else []
    failures = [r for r in rows if isinstance(r, dict) and not r.get("success")]
    return {
        "operation": operation,
        "objectName": object_name,
        **(extra or {}),
        "succeeded": len(rows) - len(failures),
        "failed": len(failures),
        "results": rows,
    }


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def salesforce_query_records(
        soql: Annotated[str, Field(description="SOQL SELECT, e.g. SELECT Id, Name FROM Account WHERE Industry = 'Technology' LIMIT 100.")],
        next_records_url: Annotated[
            str | None, Field(description="nextRecordsUrl from a previous result, to read the next batch (soql is then ignored).")
        ] = None,
    ) -> str:
        """Run a SOQL query, including parent / child relationship queries; returns records and nextRecordsUrl."""

        async def action(client: SalesforceClient) -> dict:
            if next_records_url and next_records_url.strip():
                path = next_records_url.strip()
                if not _NEXT_URL.match(path):
                    raise ValueError("next_records_url must be the nextRecordsUrl returned by a previous query.")
                return _query_result(await client.get(path))
            return _query_result(await client.get("query", {"q": _soql(soql)}))

        return await run_tool("salesforce_query_records", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def salesforce_aggregate_query(
        soql: Annotated[str, Field(description="SOQL aggregate, e.g. SELECT StageName, COUNT(Id) FROM Opportunity GROUP BY StageName.")],
    ) -> str:
        """Run a SOQL aggregate query (GROUP BY, COUNT, SUM, AVG, MIN, MAX, HAVING) for rollups."""

        async def action(client: SalesforceClient) -> dict:
            result = _query_result(await client.get("query", {"q": _soql(soql)}))
            result.pop("nextRecordsUrl", None)
            return result

        return await run_tool("salesforce_aggregate_query", client_factory, action)

    @mcp.tool(annotations=DML)
    async def salesforce_dml_records(
        operation: Annotated[Literal["insert", "update", "delete", "upsert"], Field(description="Data operation.")],
        objectName: Annotated[str, Field(description='Object API name, e.g. "Account".', min_length=1)],
        records: Annotated[
            list[dict[str, Any]],
            Field(description="Records (up to 200). update / delete need Id; upsert needs the externalIdField value.", min_length=1),
        ],
        externalIdField: Annotated[str | None, Field(description="upsert only: external id field API name to match on.")] = None,
        confirm: Annotated[bool | None, Field(description="delete only: must be true; deleted records go to the recycle bin.")] = None,
    ) -> str:
        """Insert, update, upsert or delete records of one object; each record succeeds or fails on its own.

        Changes live CRM data: only when the user explicitly asks, with the exact records given.
        delete needs confirm=true.
        """

        async def action(client: SalesforceClient) -> dict:
            obj = api_name(objectName, "objectName")
            if len(records) > MAX_DML_RECORDS:
                raise ValueError(f"At most {MAX_DML_RECORDS} records per call (got {len(records)}); split the batch.")

            if operation == "delete":
                ids = [str(r.get("Id") or r.get("id") or "").strip() for r in records]
                bad = [i for i, value in enumerate(ids) if not _RECORD_ID.match(value)]
                if bad:
                    raise ValueError(f"delete needs a valid record Id on every record (missing or invalid at index {bad[0]}).")
                if confirm is not True:
                    raise ValueError(
                        f"Deleting {len(ids)} {obj} record(s) needs confirm=true. Show the user the records first and "
                        "call again with confirm=true only after they approve."
                    )
                results = await client.request("DELETE", "composite/sobjects", params={"ids": ",".join(ids), "allOrNone": False})
                return _outcome(operation, obj, results)

            if operation == "update":
                missing = next((i for i, r in enumerate(records) if not (r.get("Id") or r.get("id"))), None)
                if missing is not None:
                    raise ValueError(f'update needs an "Id" on every record (missing at index {missing}).')
            body = {
                "allOrNone": False,
                "records": [{"attributes": {"type": obj}, **{k: v for k, v in r.items() if k != "attributes"}} for r in records],
            }
            if operation == "insert":
                if any(r.get("Id") or r.get("id") for r in records):
                    raise ValueError("insert records must not carry an Id; use update or upsert for existing records.")
                return _outcome(operation, obj, await client.request("POST", "composite/sobjects", body=body))
            if operation == "update":
                return _outcome(operation, obj, await client.request("PATCH", "composite/sobjects", body=body))
            if not externalIdField:
                raise ValueError("upsert needs externalIdField (the external id field to match records on).")
            field = api_name(externalIdField, "externalIdField")
            missing = next((i for i, r in enumerate(records) if r.get(field) in (None, "")), None)
            if missing is not None:
                raise ValueError(f'upsert needs "{field}" on every record (missing at index {missing}).')
            results = await client.request("PATCH", f"composite/sobjects/{obj}/{field}", body=body)
            return _outcome(operation, obj, results, {"externalIdField": field})

        return await run_tool("salesforce_dml_records", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def salesforce_search_all(
        sosl: Annotated[
            str, Field(description="SOSL FIND, e.g. FIND {example} IN ALL FIELDS RETURNING Account(Id,Name), Contact(Id,Name,Email).")
        ],
    ) -> str:
        """Search text across several objects at once with SOSL (names, emails, phone numbers, keywords)."""

        async def action(client: SalesforceClient) -> dict:
            text = (sosl or "").strip()
            if not _FIND.match(text):
                raise ValueError("sosl must be a SOSL FIND statement.")
            payload = await client.get("search", {"q": text})
            found = payload.get("searchRecords") if isinstance(payload, dict) else payload
            found = found if isinstance(found, list) else []
            return {"searchRecordCount": len(found), "searchRecords": strip_attributes(found)}

        return await run_tool("salesforce_search_all", client_factory, action)
