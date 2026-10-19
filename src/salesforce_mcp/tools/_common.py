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
"""Shared annotations, validation helpers and the error-handling wrapper."""

import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import ToolAnnotations

from .._json import dump_json_capped, error_envelope
from ..api_client import SalesforceClient, SalesforceError

logger = logging.getLogger("salesforce_mcp.tools")

ClientFactory = Callable[[], SalesforceClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
DML = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured",
    "Salesforce credentials are missing for this request (X-Salesforce-Instance-Url plus client or user credentials).",
    False,
)

_API_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


def api_name(value: str, what: str) -> str:
    """sObject / field API names go into URL paths; they are letters, digits and underscores."""
    text = (value or "").strip()
    if not _API_NAME.match(text):
        raise ValueError(f"{what} must be a Salesforce API name such as Account or Custom_Object__c (got {value!r}).")
    return text


def strip_attributes(value: Any) -> Any:
    """Drop the per-record "attributes" blocks (type + URL) to save tokens, keeping the type."""
    if isinstance(value, list):
        return [strip_attributes(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "attributes" and isinstance(item, dict):
                if item.get("type"):
                    out["_type"] = item["type"]
                continue
            out[key] = strip_attributes(item)
        return out
    return value


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[SalesforceClient], Awaitable[Any]],
) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    started = time.monotonic()
    try:
        result = await action(client)
    except SalesforceError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"result": None, "note": "Salesforce returned an empty response."}
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
