# Salesforce MCP Server

An [MCP](https://modelcontextprotocol.io) server for the [Salesforce](https://www.salesforce.com)
REST API. It lets an AI agent discover the objects and fields of a Salesforce org, run SOQL
queries (including aggregates) and SOSL searches and, on explicit request, insert, update,
upsert or delete records.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: credentials arrive on every request as HTTP headers and are
  never stored; the access token is obtained per request and discarded with it
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx (REST API v62.0)
- License: Apache-2.0, Copyright 2026 MSPbots.ai

## Authentication headers

`X-Salesforce-Instance-Url` is always required. Then send **either** the connected app's
consumer key and secret (client credentials flow) **or** a username and password (username /
password flow).

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Salesforce-Instance-Url` | Yes | string | none | https URL | The org's My Domain URL (any path is ignored) | `https://yourcompany.my.salesforce.com` |
| `X-Salesforce-Auth-Mode` | No | string | chosen from the fields sent | `client_credentials`, `username_password` | Force one flow | `client_credentials` |
| `X-Salesforce-Client-Id` | One of | string | none | - | Connected app consumer key | `your-consumer-key` |
| `X-Salesforce-Client-Secret` | One of | string | none | - | Connected app consumer secret | `your-consumer-secret` |
| `X-Salesforce-Username` | One of | string | none | - | Salesforce username, for the username / password flow | `user@example.com` |
| `X-Salesforce-Password` | One of | string | none | - | That user's password | `your-password` |
| `X-Salesforce-Token` | No | string | none | - | Security token appended to the password (needed when logging in from outside the org's trusted IP ranges) | `your-security-token` |

How the flow is chosen when `X-Salesforce-Auth-Mode` is blank: username + password present ->
username / password flow; otherwise -> client credentials flow.

Where to get them:

- **Client credentials (recommended):** in Setup create an External Client App or Connected App
  with OAuth enabled, the `api` scope and **Enable Client Credentials Flow**, and set a **Run
  As** user whose profile / permission sets cover the objects the agent may read and change.
  Copy the consumer key and secret. Client credentials requires the My Domain URL as the
  instance URL.
- **Username / password:** use a dedicated integration user. Its security token comes from
  **Settings -> Reset My Security Token**. If a consumer key / secret is also sent, the OAuth
  password grant is used; otherwise the server signs in with the SOAP API `login()` call.

How credentials are sent to Salesforce: the server obtains an access token from
`{instance}/services/oauth2/token` (or a session from `/services/Soap/u/62.0`), then calls
`{instance}/services/data/v62.0/...` with `Authorization: Bearer <token>`. An expired session
(401) triggers one fresh login. Values that look like unresolved templates (`${...}`) are
treated as missing.

A request to `/mcp` without the headers its flow needs gets HTTP 401 naming them; a non-https
instance URL or an unknown auth mode gets HTTP 400.

## Tools

All tools return a JSON string. Records come back without Salesforce's per-record
`attributes` block; the object type is kept as `_type`.

| Tool | Purpose | Parameters (required in **bold**) |
|---|---|---|
| `salesforce_search_objects` | Find standard and custom objects by part of their name or label | **pattern** |
| `salesforce_describe_object` | An object's fields (API name, type, picklist values, references) and child relationships | **objectName** |
| `salesforce_query_records` | Run a SOQL query; returns records, `totalSize`, `done` and `nextRecordsUrl` | **soql**, next_records_url |
| `salesforce_aggregate_query` | Run a SOQL aggregate query (GROUP BY, COUNT, SUM, ...) | **soql** |
| `salesforce_dml_records` | Insert, update, upsert or delete up to 200 records of one object (**write**) | **operation** (`insert`, `update`, `delete`, `upsert`), **objectName**, **records**, externalIdField, confirm |
| `salesforce_search_all` | Cross-object text search with SOSL | **sosl** |

`salesforce_dml_records` runs each record independently (`allOrNone: false`) and reports
`succeeded`, `failed` and per-record results. `delete` needs `confirm: true`.

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "invalid_argument", "message": "Salesforce returned HTTP 400 for ...: MALFORMED_QUERY: unexpected token: FORM", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`. Reads are retried with backoff on 429, 5xx and network
errors; writes are never retried. Every call counts towards the org's daily API request limit.

## Run locally

```bash
uv sync
uv run python -m salesforce_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t salesforce-mcp:dev .
docker run --rm -p 8080:8080 salesforce-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials are never read
from the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Salesforce-Instance-Url: https://yourcompany.my.salesforce.com" \
  -H "X-Salesforce-Client-Id: your-consumer-key" \
  -H "X-Salesforce-Client-Secret: your-consumer-secret" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# open opportunities by stage
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Salesforce-Instance-Url: https://yourcompany.my.salesforce.com" \
  -H "X-Salesforce-Client-Id: your-consumer-key" \
  -H "X-Salesforce-Client-Secret: your-consumer-secret" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"salesforce_aggregate_query","arguments":{"soql":"SELECT StageName, COUNT(Id) FROM Opportunity WHERE IsClosed = false GROUP BY StageName"}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Tool count (6).** Tool names and required parameters are unchanged.
- **Auth mode chosen from the fields.** Older versions used client credentials unless the
  mode header said `username_password`, so a connection with only a username and password
  failed. A blank mode now picks the flow from the credentials supplied.
- **Paging.** `salesforce_query_records` gained an optional `next_records_url` to read the
  batches after the first one.
- **DML.** Uses sObject collections (up to 200 records, each succeeding or failing on its
  own). `delete` now needs `confirm: true`; the description tells the agent to show the user
  the records first. Insert rejects records that carry an `Id`.
- **Input checks.** `soql` must be a SELECT, `sosl` a FIND, object and field names valid API
  names, and `next_records_url` a query cursor from this org.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
