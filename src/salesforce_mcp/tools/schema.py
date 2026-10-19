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
"""Object discovery: which sObjects exist and what fields they have."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SalesforceClient
from ._common import READ_ONLY, ClientFactory, api_name, run_tool


def _field(f: dict) -> dict:
    field: dict[str, Any] = {
        "name": f.get("name"),
        "label": f.get("label"),
        "type": f.get("type"),
    }
    if f.get("type") in ("string", "textarea", "email", "phone", "url") and f.get("length"):
        field["length"] = f["length"]
    for flag in ("nillable", "createable", "updateable", "externalId", "unique"):
        if f.get(flag):
            field[flag] = True
    if f.get("defaultValue") is not None:
        field["defaultValue"] = f["defaultValue"]
    picklist = [
        {"value": p.get("value"), "label": p.get("label")}
        for p in f.get("picklistValues") or []
        if isinstance(p, dict) and p.get("active", True)
    ]
    if picklist:
        field["picklistValues"] = picklist
    if f.get("referenceTo"):
        field["referenceTo"] = f["referenceTo"]
        if f.get("relationshipName"):
            field["relationshipName"] = f["relationshipName"]
    return field


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def salesforce_search_objects(
        pattern: Annotated[str, Field(description="Part of an object's API name or label, e.g. account or invoice.", min_length=1)],
    ) -> str:
        """Find standard and custom objects whose name or label contains the text, e.g. Account, Invoice__c."""

        async def action(client: SalesforceClient) -> dict:
            needle = pattern.strip().lower()
            if not needle:
                raise ValueError("pattern must not be blank.")
            payload = await client.get("sobjects") or {}
            matches = [
                {
                    "name": o.get("name"),
                    "label": o.get("label"),
                    "custom": o.get("custom"),
                    "queryable": o.get("queryable"),
                    "createable": o.get("createable"),
                    "updateable": o.get("updateable"),
                    "deletable": o.get("deletable"),
                }
                for o in payload.get("sobjects") or []
                if isinstance(o, dict)
                and (needle in str(o.get("name", "")).lower() or needle in str(o.get("label", "")).lower())
            ]
            return {"pattern": pattern, "matchCount": len(matches), "objects": matches}

        return await run_tool("salesforce_search_objects", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def salesforce_describe_object(
        objectName: Annotated[str, Field(description='Object API name, e.g. "Account", "Opportunity", "Custom_Object__c".', min_length=1)],
    ) -> str:
        """Get an object's fields (API name, type, picklist values, references) and child relationships."""

        async def action(client: SalesforceClient) -> dict:
            meta = await client.get(f"sobjects/{api_name(objectName, 'objectName')}/describe") or {}
            return {
                "name": meta.get("name"),
                "label": meta.get("label"),
                "custom": meta.get("custom"),
                "fields": [_field(f) for f in meta.get("fields") or [] if isinstance(f, dict)],
                "childRelationships": [
                    {"relationshipName": r.get("relationshipName"), "childSObject": r.get("childSObject"), "field": r.get("field")}
                    for r in meta.get("childRelationships") or []
                    if isinstance(r, dict) and r.get("relationshipName")
                ],
            }

        return await run_tool("salesforce_describe_object", client_factory, action)
