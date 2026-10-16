# Notion

Local Notion public API emulator for `Notion-Version: 2022-06-28` and `2025-09-03`.
The latter separates database containers from data sources. Reference contracts:
[database](https://developers.notion.com/reference/database),
[data source](https://developers.notion.com/reference/data-source),
[blocks](https://developers.notion.com/reference/block),
[property items](https://developers.notion.com/reference/retrieve-a-page-property),
[uploads](https://developers.notion.com/reference/file-upload), and
[webhooks](https://developers.notion.com/reference/webhooks).

## Hosts and routing

`api.notion.com`, `www.notion.so`, `file.notion.so`, and `*.notion.so`.
Dedicated port: **44115**. Prefix mode: `http://localhost:44080/notion/v1/...`.
The emulator does not claim Amazon S3 hostnames. Page links and signed file URLs
use Notion hosts in host mode and the caller's base URL in prefix/per-port mode.
Binary downloads work without an API token or version header using their signed URL.

```python
import httpx
from emulators.core.hub import EmulatorHub

hub = EmulatorHub(providers=["notion"], anchor="2026-10-16", webhook_mode="sync")
async with httpx.AsyncClient(transport=hub.async_transport()) as client:
    info = (await client.get("https://api.notion.com/_emulator/info")).json()
    token = info["credentials"]["internal_token"]
    result = await client.post(
        "https://api.notion.com/v1/search",
        headers={
            "Authorization": "Bearer " + token,
            "Notion-Version": "2025-09-03",
        },
        json={"query": "Clients"},
    )
    result.raise_for_status()
```

## Authentication and permissions

Read the synthetic internal integration token and its legacy alias from
`GET https://api.notion.com/_emulator/info` at `credentials.internal_token` and
`credentials.legacy_internal_token`.

Client credentials: `notion-client` / `notion-secret`. The core defaults to lenient
client registration; `EmulatorHub(lenient_clients=False, ...)` enforces the registered
secret and rejects unknown clients. Internal tokens are non-expiring and restore on reset.
Arbitrary `ntn_`/`secret_` strings are not accepted.

API routes require `Authorization: Bearer ...` and a supported `Notion-Version`.
The browser authorization endpoint and signed downloads are exceptions. Missing versions
return `400 missing_version`; invalid, expired, or revoked credentials return
`401 unauthorized`. Errors have `object`, `status`, `code`, `message`, and UUID `request_id`.
API responses include `X-Request-Id`; error bodies use that same `request_id`.
Core faults use the same envelope, including `429 rate_limited` and `Retry-After`.
The provider redacts OAuth codes, tokens, and client secrets from journal request bodies.

Public OAuth:

1. `GET /v1/oauth/authorize?client_id=notion-client&response_type=code&owner=user&redirect_uri=https%3A%2F%2Fconsumer.example%2Fcallback&state=fixture&login_hint=maya.chen%40halcyonadvisory.example`
2. Exchange the single-use code at `POST /v1/oauth/token` using HTTP Basic client
   authentication and JSON `{"grant_type":"authorization_code","code":"...","redirect_uri":"https://consumer.example/callback"}`.
3. Refresh through the same endpoint with `{"grant_type":"refresh_token","refresh_token":"..."}`.
   Access tokens last one hour; refresh tokens last 90 days. Refresh rotates both
   tokens and invalidates the previous pair. Revoke invalidates the linked pair.

Without `login_hint`, authorization renders an account picker. Default consent shares
the employee's accessible workspace subset. The emulator extension `shared_page_ids`
(comma-separated UUIDs on authorize, array on token minting) narrows sharing to those
pages/databases and their accessible descendants. A user cannot share inaccessible content.

`POST /_emulator/tokens` or `/_emulator/actions/mint_token`
accepts `{"user":"p018"}` or an employee email/Notion UUID. Omit `user` for an internal
integration token. Optional fields: `client_id`, `app_only`, `expires_in`, `shared_page_ids`,
and `scopes`. Capability names are `read_content`, `insert_content`, `update_content`,
`read_comments`, `insert_comments`, `read_user`, and `read_user_with_email`.
The default grant has all content/comment capabilities and user emails. Explicit
`scopes: []` grants no content, comment, or directory capabilities. `read_user`
omits `person.email`; without either user capability, mentions, people properties,
and the OAuth bot owner contain partial users only. Insert/update operations without
`read_content` return partial objects. Missing capabilities produce `403 restricted_resource`.

The internal integration sees Knowledge Base, Client Engagements, and the four business
databases. It cannot read People & Culture, Technology, Marketing & BD, Finance,
Partner Group, personal spaces, or documents with broken permission inheritance.
IT Runbooks is explicitly shared even though its Technology parent is not.
Delegated tokens use world space/group membership, explicit document sharing, owners,
and restricted-document ACLs. People & Culture is limited to People department employees
and leadership (executive employees and partners). Viewers can read; editors/owners can
write; commenters can add comments. Inaccessible objects return `404 object_not_found`;
visible objects without write permission return `403 restricted_resource`.

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/v1/oauth/authorize` | Account picker or auto-approve with `login_hint`; redirects code/state |
| POST | `/v1/oauth/token` | Authorization-code exchange and refresh; Basic auth, JSON |
| POST | `/v1/oauth/introspect` | Basic auth; JSON token; active, scope, iat, request_id |
| POST | `/v1/oauth/revoke` | Basic auth; JSON token; returns `{}` |
| GET | `/v1/users` | Paginated active employees plus bot; user-email capability |
| GET | `/v1/users/me` | Bot, workspace name, workspace/user owner |
| GET | `/v1/users/{id}` | UUID with or without dashes |
| POST | `/v1/search` | Title substring, object filter, last-edited sort, scoped pagination |
| POST | `/v1/pages` | Page/database/data-source parent, schema properties, nested children, icon/cover |
| GET | `/v1/pages/{id}` | Versioned parent; inline title/rich_text/people/relation capped at 25 items |
| PATCH | `/v1/pages/{id}` | Properties, icon/cover, archive/trash and restore |
| GET | `/v1/pages/{id}/properties/{property_id}` | Single property item or list with `property_item.next_url` |
| GET | `/v1/blocks/{id}` | Blocks and synthesized child_page/child_database objects |
| GET | `/v1/blocks/{id}/children` | Scoped ordered children, including synchronized block references |
| PATCH | `/v1/blocks/{id}/children` | Append up to 100 top-level blocks; `after`; two levels of nesting |
| PATCH | `/v1/blocks/{id}` | Same-type content, archive/trash, restore |
| DELETE | `/v1/blocks/{id}` | Archive; returns block |
| POST | `/v1/databases` | 2022 properties; 2025 `initial_data_source.properties` |
| GET | `/v1/databases/{id}` | 2022 schema; 2025 data_sources identifiers/names |
| PATCH | `/v1/databases/{id}` | Metadata, archive; schema changes in 2022 |
| POST | `/v1/databases/{id}/query` | 2022 only; filters, sorts, projection, pagination |
| GET | `/v1/data_sources/{id}` | 2025 schema, parent database and database_parent |
| POST | `/v1/data_sources` | 2025; additional data source under a shared database |
| PATCH | `/v1/data_sources/{id}` | 2025; metadata, schema, archive |
| POST | `/v1/data_sources/{id}/query` | 2025 database-row query |
| GET | `/v1/comments?block_id=...` | Paginated unresolved page/block comments |
| POST | `/v1/comments` | New page discussion or reply by discussion_id |
| POST | `/v1/file_uploads` | Both supported versions; single_part or multi_part; optional filename/content_type |
| GET | `/v1/file_uploads` | Creator integration scope; status filter and pagination |
| GET | `/v1/file_uploads/{id}` | Lifecycle, size/type/name/expiry, creator, multipart progress |
| POST | `/v1/file_uploads/{id}/send` | Multipart `file`; multi_part also requires `part_number` |
| POST | `/v1/file_uploads/{id}/complete` | Complete multi_part upload after all parts arrive |
| GET | `/secure/{id}/{filename}` | Signed, expiring binary download; lazy seeded rendering |

Pagination uses `object: list`, `results`, `has_more`, `next_cursor`, `type`, and a
type-specific empty object. Defaults to 100, maximum 100. Cursors are opaque UUIDs
bound to token, resource, and query; reset invalidates them. Query pages retain vendor
property names and ids. `filter_properties` is accepted as repeated query parameters
and as a query-body array for connector compatibility. Search and query lists use
`type: page_or_database` in 2022 and `type: page_or_data_source` in 2025. A cursor
can be reused with its original resource, token, and query.

Database filters include title/rich_text text comparisons, number comparisons,
checkbox, select/status, multi_select, people, relation, date comparisons and relative
windows (`past_week`, `next_month`, etc.), formula result filters, and timestamp filters.
Compound `and`/`or` supports two nested compound levels. Multiple sorts are applied
in priority order. Schema rename preserves property ids and stored values; null removes
the schema property and row values. Each schema requires one title property.

Blocks support paragraph, heading_1/2/3, bulleted_list_item, numbered_list_item,
to_do, toggle, quote, callout, code, divider, table/table_row, image/file/pdf,
bookmark, synced_block, column_list/column, link_to_page, equation, table_of_contents,
and read-generated child_page/child_database. Rich text normalizes annotations,
plain_text, href, links, user/page/date mentions, and equations.

Writes reject malformed types, invalid Unicode and non-finite numbers with Notion
errors. JSON bodies are limited to 500 KiB; URL, email, phone, text, equation, color,
and code-language limits are validated. Rejected writes preserve properties, schema
options, blocks, and upload attachment expiry. Archived ancestors prevent writes;
restore ancestors before editing descendants. Tables keep their width and row cell
counts, and new column lists require two populated columns.

Single-part sends become `uploaded` immediately. Multipart uploads become `uploaded`
only after completion; parts assemble in part-number order. Attach using
`{"type":"file_upload","file_upload":{"id":"..."}}` in file blocks, icons/covers,
or files properties. Readback returns a `file` object with a fresh signed URL.
Pending uploads expire after an hour; attachment clears upload expiry. Download URLs
expire after an hour and fresh reads regenerate them. Upload bytes and lazily rendered
seed documents live in the content-addressed blob store. External files are returned
as external URLs and are never fetched by the emulator.

## Seeded workspace

Workspace: **Halcyon Advisory Group**. At seed `emulators`, anchor `2026-10-16`:
45 active employees plus the integration bot; 815 pages, 2,855 content blocks,
359 source-document pages, 4 databases/data sources, and 81 lazy binary files.
Counts can vary with the shared world. All identities and content come from `ctx.world`.

| Object | Stable id / account |
| --- | --- |
| Knowledge Base | `4a49903e-83ec-40da-b73a-d0e014586e93` |
| Client Engagements | `b24216f8-ab6f-4e8f-b8d6-2040d88fb6f6` |
| People & Culture (internal integration denied) | `118e12fb-d63a-42b9-b8d6-f01f7029e583` |
| Technology (internal integration denied) | `86405b69-25d0-437e-8c7e-7a40c41895e7` |
| Clients database | `8bd74718-8f6f-4766-abbd-723132d65a04` |
| Engagements database | `8de21413-ae41-4c13-bc11-864a9c100383` |
| Meeting Notes database | `b94b07ba-1db1-4c07-aa04-ff47ebac6136` |
| IT Runbooks database | `a7de15f6-2e5b-4e69-9eb1-76016e353c5f` |
| Northwind Logistics client row | `3befbe1b-584c-4eb2-9b43-b92d870e0e0d` |
| World document d0001 page | `9e99eab8-a03c-40d7-b365-a779cd18518a` |
| Maya Chen (partner), p018 | `3bf8e53c-838d-4a36-b545-06d31ef5626e`, `maya.chen@halcyonadvisory.example` |
| Ryan Murphy (non-leadership), p008 | `ryan.murphy@halcyonadvisory.example` |

Retrieve the database using version 2025 to discover its stable data-source UUID.
Teamspace roots mirror all world spaces, including personal spaces; folders become
pages, document DOCX/MD/TXT paragraphs become heading/paragraph blocks, spreadsheet
tables become table blocks, presentations become nested toggles, and PDFs/images
become file/image blocks. Document owners, modification times, and ACLs carry through.
Teamspace/folder creation predates imported documents, and imported block timestamps
follow their source pages rather than the seed's anchor date.

Clients has Name, Industry, Status, Owner, Client Since, Annual Revenue, Website,
and Engagements relation. Engagements has Name, Client relation, Service, Lead, Start,
End, Budget Hours, and Status. Meeting Notes includes past client meetings with
Attendees, Date, Client relation, agenda/summary, and available transcript excerpts.
IT Runbooks projects Technology documents with Owner, Last Reviewed, and Status.

## Webhooks and actions

`POST /_emulator/actions/configure_webhook` with `{"url":"http://consumer.example/notion"}`
synchronously sends `{"verification_token":"secret_..."}` to the receiver. It must
return a 2xx status. The action returns `subscription_id` and `verification_token`.
Subscriptions default to the seeded internal integration's visibility. Supply an optional
`token` to subscribe with an OAuth grant's visibility; revoked/expired grants stop delivery.
An optional `api_version` selects the webhook schema (`2022-06-28` by default or
`2025-09-03`). Content events require `read_content`; comment events require `read_comments`.

Mutations emit `page.created`, `page.content_updated`, `page.properties_updated`,
`page.deleted`, `page.undeleted`, `database.schema_updated` (2022),
`data_source.schema_updated` (2025), and `comment.created`. Payloads contain id, timestamp,
workspace_id/name, subscription_id, integration_id, type, authors, attempt_number,
entity `{id,type}`, and data with parent references. Public OAuth subscriptions also
include `accessible_by`. Property events use property IDs; schema events describe
created/updated/deleted properties.
Events are delivered through `ctx.webhooks.deliver` and appear in `/_emulator/webhooks`.
The signature is `X-Notion-Signature: sha256=<hex HMAC-SHA256(verification_token, raw_body)>`.
Sync, async, and manual core delivery modes are supported. Reset clears subscriptions.
Failed deliveries retry up to eight attempts, retaining the event ID and updating
`attempt_number` and the HMAC signature for each payload. The journal records the
last attempt's payload and response.

`mint_token` is also available through `/_emulator/actions/mint_token` and is used by
the core `/_emulator/tokens` endpoint.

## Known gaps

* Supports the two requested versions; newer API versions, workspace administration,
  teamspace management, database views/templates, and page markdown endpoints are absent.
* Authorization consent is headless and uses the account picker plus optional
  `shared_page_ids`; it does not reproduce Notion's interactive page-selection UI.
* Query formula filters evaluate stored scalar results. Formula computation supports
  `prop("Name")`, an optional numeric multiplier, scalar numbers/strings, and booleans;
  the general formula language and rollup computation are absent. Numeric overflow
  evaluates to a null number instead of causing an invalid JSON response.
  Text substring/prefix/suffix filters ignore case; equality uses an exact match.
  Select/status sorts use labels rather than Notion UI option order.
* Status schemas support options/groups, but group assignment and some vendor schema
  restrictions are simplified. Relation reads hide inaccessible target rows. Automatic
  two-way relation synchronization is absent. Data-source moves are rejected explicitly.
* Rich-text mention support is limited to user, page, and date. Page titles and user-email
  metadata are scoped to the reader; inaccessible page titles become `Untitled`.
* A request may append 100 top-level / 1,000 total block elements. Some specialized
  type-specific validation rules are not reproduced.
  Block type changes and API creation of child_page/child_database blocks are unsupported;
  use page/database creation endpoints. Column width ratios and synced editing restrictions
  are simplified; references read from the original synced block.
* File uploads support direct single/multipart only, with a 20 MiB per-part limit.
  External URL import, malware scanning, full MIME restrictions, and resumable transfer
  recovery are absent. No external file content is fetched.
* Comments support new page discussions and replies, with list/read scope checks.
  Resolution, deletion, comment attachments, and creation of arbitrary inline discussions
  are absent. Seeded comment discussions are empty.
* Webhooks deliver mutation events immediately, without Notion's aggregation/debounce
  or full event catalog. Async retry delays are accelerated for local tests rather
  than spread over 24 hours; sync/manual retries have no delay. Reverification and
  webhook management APIs are absent.
* Rate limits are simulated through core faults; normal requests are not throttled.
  Page web URLs identify objects; the Notion interactive document UI is not served.

## Verification

```powershell
uv run pytest tests/providers/notion -q
$env:EMULATORS_PROVIDERS = "notion"
uv run pytest tests/sdk -q -k notion
uv run pytest tests/e2e -q
uv run ruff check src/emulators/providers/notion tests/providers/notion tests/sdk/test_notion_http.py
uv run ruff format --check src/emulators/providers/notion tests/providers/notion tests/sdk/test_notion_http.py
```

The SDK-contract location intentionally uses ordinary sync/async httpx clients,
matching the brief's connector transport, through `emulator_intercept` and real
vendor hostnames. No Notion SDK is installed in this environment, so these are live
HTTP connector contracts rather than official Notion SDK verification.

The fidelity-audit regressions cover malformed writes across every write endpoint,
failed-write atomicity, empty capabilities, selected-page app access, scoped user
metadata, versioned list envelopes, cursor reuse, OAuth rotation/client binding,
upload metadata/progress, archive ancestors, schema invariants, seed timestamps,
webhook versions, and per-attempt signatures. An external in-process fuzz harness
also exercised 1,802 nested malformed-input cases without server exceptions.
The completed focused runs passed 144 provider tests, five live HTTP connector
contracts, and ten repository end-to-end tests. Ruff and the Notion secret-pattern
scan were clean. Three measured hub builds seeded Notion in 0.66–1.16 seconds.
