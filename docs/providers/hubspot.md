# HubSpot CRM

The `hubspot` provider serves `api.hubapi.com`, `api.hubspot.com`, and
`app.hubspot.com`. Its dedicated port is **44113**; gateway prefix routing uses
`http://localhost:44080/hubspot`. Generated pagination links preserve the selected
routing mode and requested properties.

```python
import httpx
from emulators.core.hub import EmulatorHub

hub = EmulatorHub(providers=["hubspot"], anchor="2026-10-16", webhook_mode="sync")
client = httpx.AsyncClient(transport=hub.async_transport())
```

## Authentication and test credentials

Portal ID: **24681357**. App ID: **135790**. These credentials are fictional:

| Credential | Value |
| --- | --- |
| Private app Bearer token | `pat-na1-<uuid>` — read it from `emulators.providers.hubspot.common.PRIVATE_TOKEN` or `GET /_emulator/info` (not printed here so secret scanners don't flag it) |
| OAuth client ID | `emulator-client` |
| OAuth client secret | `emulator-secret` |
| Broad-access user | `maya.chen@halcyonadvisory.example` (`p018`) |
| Owned-only user | `zoe.anderson@halcyonadvisory.example` (`p042`) |

OAuth authorization uses
`https://app.hubspot.com/oauth/authorize?client_id=...&redirect_uri=...&scope=...&state=...`.
`login_hint` accepts a seated employee's world ID, email, owner ID, or numeric user
ID and automatically approves; omitting it displays an account picker. `optional_scope`
values that this account does not support are dropped rather than rejected, matching HubSpot's best-effort optional scopes; unsupported required `scope` values return 400. Authorization codes are single use and bind the client and redirect URI.
Unknown OAuth clients are accepted by the shared core's default lenient client policy;
registered redirect URI allowlists are enforced.

Exchange codes at `POST https://api.hubapi.com/oauth/v1/token` using
`application/x-www-form-urlencoded` with `grant_type=authorization_code`, `code`,
`client_id`, `client_secret`, and `redirect_uri`. Refresh using the same endpoint with
`grant_type=refresh_token`, `refresh_token`, `client_id`, and `client_secret`.
Responses contain lowercase `token_type: bearer`, an opaque `CJ...` access token, UUID
refresh token, and `expires_in: 1800`. Refresh-token revocation leaves existing access
tokens valid, as described in the [legacy OAuth reference](https://developers.hubspot.com/docs/api-reference/legacy/authentication/oauth-tokens/v1/guide).

The private app token has all implemented scopes and no expiry. Control-plane minting
supports `{"user":"p042","scopes":["crm.objects.deals.read"]}` or
`{"app_only":true}` at `POST /_emulator/tokens`. Omitted scopes grant all implemented
scopes; an explicit empty list grants none. `expires_in` defaults to 1800. Only employees
with HubSpot seats can receive delegated tokens. Private and app-only tokens see all
records permitted by their scopes.

Object endpoints require the matching `crm.objects.<object>.read` or `.write` scope.
Tickets additionally accept legacy `tickets`. Activities accept contact scopes;
activity reads also accept `crm.objects.<activity>.read`. Property reads accept object
read or `crm.schemas.<object>.read`; definition writes require schema write scopes.
Owners require `crm.objects.owners.read`. Account details accept `oauth` or
`account-info.security.read`; usage requires the latter. Missing scopes return 403
`MISSING_SCOPES` with `requiredGranularScopes`. Bad or expired tokens return 401
`INVALID_AUTHENTICATION`.

## Permissions

Seats include partners, managers, administrators, Business Development & Marketing,
and employees who own world deals or support cases. Departments become owner teams.
Delegated users `p042`, `p043`, `p044`, and `p045` have owned-only access. Their list,
get, search, batch, update, archive, and association requests are filtered by
`hubspot_owner_id`; inaccessible records return 404. Their newly created objects
default to their own owner ID, and they cannot transfer ownership. Products are a
shared catalog. Other seated employees have account-wide access. This explicit
emulator policy models user-level OAuth permissions; ordinary account-level HubSpot
app authorization does not automatically inherit an installing user's UI permissions.

World deals retain their original owners, so the seeded BD representatives have no
owned deals. Create a deal with owner `240546578877472` (Zoe) to test allowed access,
and compare with a seeded partner-owned deal to test denial.

## Endpoints

`{object}` supports `contacts`, `companies`, `deals`, `tickets`, `products`,
`line_items`, `calls`, `emails`, `meetings`, `notes`, and `tasks`. Numeric object type
aliases (`0-1`, `0-2`, `0-3`, etc.) are accepted.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/oauth/authorize` | Account picker or automatic approval with `login_hint`; state preserved |
| POST | `/oauth/v1/token` | Form-encoded authorization-code and refresh grants |
| GET | `/oauth/v1/access-tokens/{token}` | Portal, user, scope, and remaining lifetime metadata |
| GET, DELETE | `/oauth/v1/refresh-tokens/{token}` | Refresh metadata or revocation; DELETE 204 |
| GET | `/crm/v3/objects/{object}` | Default limit 10, maximum 100; numeric `after`, `archived`, properties, history, associations |
| POST | `/crm/v3/objects/{object}` | Create with properties and optional inline associations; 201 |
| GET | `/crm/v3/objects/{object}/{id}` | `idProperty=email` for contacts or a custom unique property |
| PATCH | `/crm/v3/objects/{object}/{id}` | Update properties; supports `idProperty`; empty string/null clears a value |
| DELETE | `/crm/v3/objects/{object}/{id}` | Archive, 204; retrieve with `archived=true` |
| POST | `/crm/v3/objects/{object}/batch/read` | Up to 100 inputs; top-level `idProperty`, properties, history |
| POST | `/crm/v3/objects/{object}/batch/create` | Up to 100; 201; preserves `objectWriteTraceId` |
| POST | `/crm/v3/objects/{object}/batch/update` | Up to 100; 200 |
| POST | `/crm/v3/objects/{object}/batch/archive` | Up to 100; successful archive returns 204 |
| POST | `/crm/v3/objects/{object}/batch/upsert` | `idProperty` per input (omitted means `hs_object_id`, which only updates); includes `new` |
| POST | `/crm/v3/objects/{object}/search` | OR groups of AND filters, query, sort, offset cursor; default 10, maximum 200 |
| GET | `/crm/v4/objects/{from}/{id}/associations/{to}` | Directional labels; default/max limit 500; numeric cursor |
| PUT | `/crm/v4/objects/{from}/{id}/associations/default/{to}/{toId}` | Unlabeled relation; 200 with COMPLETE batch envelope |
| PUT | `/crm/v4/objects/{from}/{id}/associations/{to}/{toId}` | Array of association category/type specifications; 201 |
| DELETE | `/crm/v4/objects/{from}/{id}/associations/{to}/{toId}` | Removes both directions; 204 |
| POST | `/crm/v4/associations/{from}/{to}/batch/read` | Up to 1000 source IDs; COMPLETE or 207 with partial errors; `toObjectId` values are integers |
| POST | `/crm/v4/associations/{from}/{to}/batch/create` | Labeled batch create `{from, to, types}`; 201 or 207 |
| POST | `/crm/v4/associations/{from}/{to}/batch/associate/default` | Unlabeled batch create `{from, to}`; 200 or 207 |
| POST | `/crm/v4/associations/{from}/{to}/batch/archive` | Remove all associations for `{from, to: [...]}` pairs; 204 |
| POST | `/crm/v4/associations/{from}/{to}/batch/labels/archive` | Remove only the specified association types; 204 |
| POST | `/crm/v3/associations/{from}/{to}/batch/read` | Legacy name-typed (`contact_to_company`) batch read; 200 or 207 |
| POST | `/crm/v3/associations/{from}/{to}/batch/create` | Legacy batch create `{from, to, type}`; 201 or 207 |
| POST | `/crm/v3/associations/{from}/{to}/batch/archive` | Legacy batch remove by type name; 204 |
| GET, POST | `/crm/v4/associations/{from}/{to}/labels` | Built-in labels; create a custom label with optional `inverseLabel` (200) |
| GET, POST | `/crm/v3/properties/{object}` | Read catalog or create a custom definition; 201 for create |
| GET, PATCH, DELETE | `/crm/v3/properties/{object}/{name}` | Read, update (label/options/group; name/type/fieldType immutable), or archive a custom property; 204 |
| GET, POST | `/crm/v3/properties/{object}/groups` | Read/create property groups |
| GET, PATCH, DELETE | `/crm/v3/properties/{object}/groups/{name}` | Read, rename, or archive a group |
| GET | `/crm/v3/pipelines/{object}` | Deals and tickets |
| GET | `/crm/v3/pipelines/{object}/{pipeline}` | Default Sales Pipeline, custom Advisory Engagements, Support Pipeline |
| GET | `/crm/v3/pipelines/{object}/{pipeline}/stages` | Stage labels, probabilities, and open/closed metadata |
| GET | `/crm/v3/pipelines/{object}/{pipeline}/stages/{stage}` | Individual stage |
| GET | `/crm/v3/owners` | Default limit 100, maximum 500; email, archived, after filters |
| GET | `/crm/v3/owners/{id}` | Owner ID or `idProperty=userId`; list also filters by `email` and `userId`; departments appear in teams |
| GET | `/account-info/v3/details` | Portal, currency, timezone, UTC offset, hosting location |
| GET | `/account-info/v3/api-usage/daily/private-apps` | Usage summary with `currentUsage`, `usageLimit`, `fetchStatus` |

Batch errors return 207 with `status: COMPLETE`, successes, `errors`, and `numErrors`.
Batch **create** is atomic unless multi-status is opted into by giving every input a
unique `objectWriteTraceId`, as HubSpot requires; without it the first failure aborts
the request and nothing is created. Batch update and upsert inputs may carry their own
`idProperty`. Every vendor API response (including errors and injected faults) carries
`X-HubSpot-Correlation-Id` — matching the error body's `correlationId` — and
`X-HubSpot-RateLimit-*` headers. Creating a deal or ticket without a pipeline uses the
default pipeline (`default` / `0`); companies require at least one of `name` or
`domain`. Deal `hs_deal_stage_probability`, `hs_is_closed`, and `hs_is_closed_won` are
HubSpot-calculated (read-only) and follow stage changes. Activities created without
`hs_timestamp` default it to the creation time. A `PATCH` that changes no values is a
no-op: no `updatedAt` bump, no history entry, and no property-change webhook.
Search supports `EQ`, `NEQ`, `LT`, `LTE`, `GT`, `GTE`, `BETWEEN`, `IN`, `NOT_IN`,
`HAS_PROPERTY`, `NOT_HAS_PROPERTY`, `CONTAINS_TOKEN`, and `NOT_CONTAINS_TOKEN`.
Up to five groups, six filters per group, and 18 filters in total are accepted.
Sorts accept a property string (prefix `-` for descending) or
`{"propertyName":"amount","direction":"DESCENDING"}`. Search stops at 10,000
results; archived objects are excluded. Numeric/date comparisons use property types;
enumeration comparisons preserve case for every operator, including token searches.
The complete search request body is limited to 3,000 characters, including filters.
Search sees writes immediately.
These limits and enumeration case rules follow the official
[CRM Search guide](https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm).

All values are stored as HubSpot property strings. Dates/datetimes accept ISO values
or epoch milliseconds. Requested existing properties without a value return null;
unknown properties are ignored on reads and rejected on writes. History records use
HubSpot value/timestamp/source fields. Contacts expose `lastmodifieddate`; other
objects expose `hs_lastmodifieddate` by default.

The implementation follows the vendor's [published OpenAPI specifications](https://github.com/HubSpot/HubSpot-public-api-spec-collection),
including different response envelopes for default and labeled associations. Common
directional IDs include contact→company 1, company→contact 2, deal→contact 3,
contact→deal 4, deal→company 5, and company→deal 6. Default unlabeled
contact→company uses 279; its inverse uses 280. Primary relationships also carry the
unlabeled association. Labeled and unlabeled types share the legacy v3 type name (`contact_to_company`), so object-response association expansions contain one entry per associated record. Activity/contact/company/deal and ticket associations are seeded.

## Seeded data

For seed `emulators` and anchor `2026-10-16`:

| Resource | Count | Example |
| --- | --- | --- |
| Companies | 42 | Northwind Logistics: `90540105789335`, domain `northwindlogistics.example` |
| Contacts | 90 | Christopher Ingram: `191422113450928`, `christopher.ingram@northwindlogistics.example` |
| Deals | 44 | Northwind FY2025 Review: `224546882199931`, stage `closedwon` |
| Tickets | 10 | World support cases with original subjects, priorities and owners |
| Products | 10 | World service items; product `187449833612435` is Audit & Assurance Services |
| Line items | 30 | One world service item attached to each won deal |
| Meetings | 80 | Client meetings, original titles/times, outcomes and descriptions |
| Emails | 304 | World client messages with incoming/outgoing direction |
| Notes | 24 | Client descriptions |
| Tasks | 10 | Next steps from open deals |
| Calls | 4 | Phone-origin support cases |
| Owners | 17 | Maya Chen: `165508596117968`; Zoe Anderson: `240546578877472` |

Company and contact `client_since` and `preferred_meeting_platform` are seeded custom
properties. Client companies and prospect companies derive from world clients;
additional lead companies derive from world leads. Deal stages map world qualification,
analysis, proposal, negotiation, and closed stages into Sales Pipeline stage IDs.
The Advisory Engagements pipeline (`1000001`, stages `10000001` through `10000007`)
is available for newly created deals. Activity
relations connect the relevant client company, contacts, and deals. Numeric IDs derive
from world IDs and remain stable across hubs. No seed content requires binary rendering.

## Webhooks and actions

`POST /_emulator/actions/configure_webhook`:

```json
{
  "target_url": "http://consumer.example/hubspot-hook",
  "client_secret": "fictional-webhook-secret",
  "subscriptions": [
    "contact.creation",
    {"id": 2, "subscriptionType": "contact.propertyChange", "propertyName": "email"},
    "contact.deletion",
    "deal.creation"
  ]
}
```

Subscriptions accept strings or objects with `subscriptionType` (or `eventType`),
optional `id`, `propertyName`, and `active`. Supported events are creation,
propertyChange, and deletion for contacts, companies, deals, and tickets. Writes and
archives trigger delivery through `ctx.webhooks`. Bodies are JSON arrays of at most
100 events, with portal/app/object IDs, subscription/event IDs, millisecond occurrence
time, `attemptNumber: 0`, `changeSource: CRM`, and `sourceId`. Property changes include
`propertyName` and `propertyValue`.

`X-HubSpot-Request-Timestamp` is milliseconds from the emulator clock.
`X-HubSpot-Signature-v3` is base64(HMAC-SHA256(client_secret,
`POST + uri + body + timestamp`)). The URI applies HubSpot's selective percent decoding,
preserving encodings such as `%26`. Legacy `X-HubSpot-Signature` is SHA256 hex of
`client_secret + body`, with `X-HubSpot-Signature-Version: v1`. See the
[request validation reference](https://developers.hubspot.com/docs/apps/legacy-apps/authentication/validating-requests).
The shared webhook journal and manual flush endpoints are available; tests can set
`hub.webhook_transport` to an in-process receiver.

`POST /_emulator/actions/mint_token` is also available with the same payload as
`POST /_emulator/tokens`. Standard reset, snapshots, request journal, and fault actions
are inherited. Injecting a 429 produces the HubSpot error envelope, `Retry-After`, and
`X-HubSpot-RateLimit-*` headers.
OAuth token path segments and credential form fields are redacted in the request journal.

## Known gaps

- This provider targets the requested legacy OAuth v1, CRM v3, and associations v4
  contracts. Date-based APIs, OAuth v3, API keys, account switching, and PKCE are absent.
- Quotes, custom CRM object schemas, merge/GDPR deletion, files, marketing APIs, and
  legacy engagement APIs are not implemented (v3 association batch endpoints are).
- Pipelines are read-only. Custom association label update/delete, property batch endpoints,
  and association limit administration
  are absent. The property catalog contains fields used by this projection rather
  than every HubSpot default field (common `hs_lead_status` and calculated deal properties are included). Supported association pairs are the seeded CRM
  and activity pairs; there is no arbitrary object-pair schema.
- Contacts upsert by `email` is a partial update here; real HubSpot does not support
  partial upserts with `email` as the `idProperty` and expects all properties to be sent.
- Search is synchronous and uses a local token matcher rather than HubSpot's delayed
  search index. Association-property search filters and sensitive-data scopes are absent.
- Archive is immediate; deleted IDs must exist. Archived unique values can be reused.
  Batch processing is synchronous; errors are reported per input without async jobs.
- Webhook subscriptions are configured by the emulator action, not the developer app
  management API. Delivery grouping is immediate per mutation/batch. The shared
  dispatcher retries the original payload, so `attemptNumber` remains zero on retries.
- Rate limiting is fault driven. Usage is a diagnostic count from the bounded request
  journal, not billing-grade daily accounting. Unspecified quota/product-tier checks
  and automatic vendor-calculated CRM properties are absent.
- Seed histories contain the initial value and subsequent emulator edits; historical
  changes before the world anchor are not synthesized. Owner restrictions are the
  explicit policy above rather than configurable HubSpot permission sets.

## Verification

```powershell
uv run pytest tests/providers/hubspot -q
$env:EMULATORS_PROVIDERS = "hubspot"
uv run pytest tests/sdk/test_hubspot_http.py -q
uv run ruff check src/emulators/providers/hubspot tests/providers/hubspot tests/sdk/test_hubspot_http.py
uv run ruff format --check src/emulators/providers/hubspot tests/providers/hubspot tests/sdk/test_hubspot_http.py
```

No HubSpot SDK dependency is installed. The SDK-marked HTTP contract suite follows
HubSpot client call shapes and uses `emulator_intercept` to exercise vendor URLs over
the live local gateway.
