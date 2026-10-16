# Atlassian Cloud

Confluence REST v2, connector-oriented Confluence v1 reads, and a Jira Software REST v3 subset.
The provider is `atlassian`, with dedicated port **44116**. It projects the shared Halcyon Advisory
Group world; it never contacts Atlassian services.

## Hosts and routing

* `auth.atlassian.com`: 3LO authorization and token exchange.
* `api.atlassian.com`: accessible resources, profile, and OAuth product gateways.
* `halcyonadvisory.atlassian.net` and `*.atlassian.net`: Confluence and Jira site APIs.

The deterministic cloud ID is `8caa4fc6-a058-484a-9fb6-cf4f4db4cf39`.
Wildcard site hosts are aliases for this same tenant. A different cloud ID returns 404.

All product routes below also work at the OAuth gateways:

```text
https://api.atlassian.com/ex/confluence/8caa4fc6-a058-484a-9fb6-cf4f4db4cf39/wiki/api/v2/...
https://api.atlassian.com/ex/confluence/8caa4fc6-a058-484a-9fb6-cf4f4db4cf39/wiki/rest/api/...
https://api.atlassian.com/ex/jira/8caa4fc6-a058-484a-9fb6-cf4f4db4cf39/rest/api/3/...
```

Prefix mode uses `http://localhost:44080/atlassian`, and the dedicated provider app can use
`http://localhost:44116`. Pagination, API self links and binary downloads preserve the caller's
routing mode. Confluence `_links.base` identifies the site wiki; `_links.next` is directly fetchable.
Gateway attachment downloads retain the gateway prefix and accept the same Bearer token.

## Authentication

### OAuth 2.0 authorization code and rotating refresh tokens

`GET /authorize` requires `audience=api.atlassian.com`, `client_id`, `redirect_uri`, and
`response_type=code`. Supply `scope`, `state`, `prompt=consent` and
`login_hint=maya.chen@halcyonadvisory.example`. A login hint auto-approves and redirects with the
code and unchanged state; without a hint an account picker appears. World person IDs and account
IDs also work as login hints. PKCE plain/S256 is supported by the shared token service.

`POST /oauth/token` consumes **JSON**, with these bodies:

```json
{"grant_type":"authorization_code","client_id":"emulator-client","client_secret":"emulator-secret","redirect_uri":"https://consumer.example/callback","code":"<code>"}
```

```json
{"grant_type":"refresh_token","client_id":"emulator-client","client_secret":"emulator-secret","refresh_token":"<refresh_token>"}
```

Access tokens are registered RS256 JWTs with a 3600-second lifetime. Codes are single-use, bound
to client and redirect URI, and expire in ten minutes. `offline_access` requests receive a refresh
token; successful refresh invalidates the previous refresh token and issues a new one. Refresh
tokens expire after 90 days. Client credentials is not a supported 3LO grant. Unknown clients are
accepted in the default lenient mode. Strict mode accepts the seeded client credentials above.

Useful classic scopes are `read:confluence-content.all`, `read:confluence-space.summary`,
`search:confluence`, `read:confluence-user`, `write:confluence-content`, `read:jira-work`,
`write:jira-work`, `read:jira-user` and `offline_access`. The Confluence routes also recognize
their primary granular read/write scopes, including `read:page:confluence`, `read:space:confluence`,
`read:user:confluence`, `read:attachment:confluence` and `read:hierarchical-content:confluence`.
Ancestors use `read:content.metadata:confluence` and footer comments use `read:comment:confluence`.
Space-property mutations accept `read:space:confluence` plus `write:space:confluence`; page-property
mutations accept `read:page:confluence` plus `write:page:confluence`.
An authenticated token without a required scope receives 403.

### Site Basic authentication

Use any active world employee's **email + `halcyon-api-token`**, for example:

| Email | Access examples |
| --- | --- |
| `eleanor.whitfield@halcyonadvisory.example` | Partner; CLIENTS, KB, PARTNERS; read-only PEOPLE/TECH/FINANCE |
| `maya.chen@halcyonadvisory.example` | Advisory partner; CLIENTS, KB, PARTNERS |
| `olivia.grant@halcyonadvisory.example` | People space editor, including Salary Bands |
| `victoria.hale@halcyonadvisory.example` | Finance editor, including Payroll Calendar |
| `liam.fitzgerald@halcyonadvisory.example` | Staff reader of KB/PEOPLE; TECH editor; OPS issues |

Basic authentication is accepted on site routes, including wildcard aliases. OAuth API gateway
routes require Bearer credentials. Disabled employees cannot authenticate.

### Control-plane tokens and application access

`POST /_emulator/tokens` and `POST /_emulator/actions/mint_token` accept a world ID, email or
Atlassian account ID as `user`. With no `scopes` field, tokens receive the useful scopes listed
above. An explicitly supplied scope list is enforced. `expires_in` controls access-token lifetime.

```json
{"user":"p018","expires_in":3600}
```

Atlassian 3LO itself has no app-only grant. For application permission tests, the control plane's
`{"app_only":true}` token uses service principal **p035 (Arjun Mehta)**. Override this principal with
`EMULATORS_ATLASSIAN_APP_USER` or the hub's `APP_USER` setting. Application tokens obey that person's
space and issue permissions; they do not bypass restrictions. User-profile endpoints (`/me`,
`/myself`, `/user/current`) require delegated tokens.

## Endpoints

### Identity

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/authorize` | Account picker or auto-approved code redirect |
| POST | `/oauth/token` | JSON authorization-code / rotating refresh exchange |
| GET | `/oauth/token/accessible-resources` | Cloud ID, site URL, granted scopes |
| GET | `/me` | Atlassian profile; snake_case account fields |

### Confluence REST v2 (`/wiki/api/v2`)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/spaces` | `ids`, `keys`, `type=global|personal`, `status`, `sort`, description format, icon |
| GET | `/spaces/{id}` | Homepage and optional permission assignments |
| GET | `/spaces/{id}/pages` | `depth=root|all`; page filters and sort |
| GET | `/spaces/{id}/permissions` | User/group principals and vendor operation shapes |
| GET, POST | `/pages` | ID/space/title/status filters; create returns 200 |
| GET, PUT, DELETE | `/pages/{id}` | Read, optimistic update, trash, draft deletion or purge |
| GET | `/pages/{id}/children` | ChildPage shape, including `childPosition` |
| GET | `/pages/{id}/direct-children` | Adds the content `type` |
| GET | `/pages/{id}/descendants` | Depth-first tree traversal; `depth` 1–10, default 2 |
| GET | `/pages/{id}/ancestors` | Root-first `{id,type}` records; nearest `limit` ancestors |
| GET | `/pages/{id}/versions` | `sort=modified-date|-modified-date`; optional body format |
| GET | `/pages/{id}/labels` | Seeded global area labels and optional prefix filter |
| GET | `/labels/{id}/pages` | Visible pages carrying a label |
| GET | `/pages/{id}/operations` | Caller-specific read/update/delete operations |
| GET | `/pages/{id}/attachments` | Attachment metadata and downloadable URLs |
| GET | `/attachments/{id}` | Accepts numeric or `att`-prefixed attachment ID |
| GET | `/pages/{id}/footer-comments` | Colleague comments and optional body representation |
| GET, POST | `/blogposts` | Seeded area updates and creation |
| GET, PUT, DELETE | `/blogposts/{id}` | Versioned read/update/delete |
| GET, POST | `/{pages|spaces|blogposts}/{id}/properties` | Key filter; arbitrary JSON values |
| GET, PUT, DELETE | `/{pages|spaces|blogposts}/{id}/properties/{propertyId}` | Optimistic version updates |
| POST | `/users-bulk` | `{"accountIds":[...]}`, maximum 250 IDs |

Collection pagination defaults to 25 with a maximum of 250, returning `_links.next` and `Link:
<...>; rel="next"`. Follow the returned URL with the same credentials. Invalid cursors and limits
return 400. Page lists support `sort=-modified-date` and comma-separated or repeated `space-id`,
`id`, and `status` parameters. Default statuses are current and archived. A draft is visible only
to its owner; single-page reads use `get-draft=true` or an explicit `status`. Trashed content needs
`status=trashed`.

Page bodies support `body-format=storage|atlas_doc_format|view`. Without a requested representation,
`body` is empty. Single reads support `version`, `include-version=false`, `include-versions`,
`include-labels`, `include-properties` and `include-operations`. Updates require matching `id`,
`title`, `status`, `body`, and `version.number=current+1`; stale versions return 409. Creation also
accepts a nested body object. New pages default beneath the space homepage, unless `parentId` or
`root-level=true` is supplied. Cross-space parents and hierarchy cycles return 400.

`GET /wiki/download/attachments/{pageId}/{filename}` returns real generated Office/PDF/CSV/PNG bytes
with MIME type, content length, disposition and a content hash ETag. Rendering happens on first
attachment metadata/download access; bytes are cached in `ctx.store.blobs`.

### Confluence REST v1 (`/wiki/rest/api`)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/content/search` | CQL content results, expansions, `start`/`limit`, next link |
| GET | `/search` | CQL search results with content, excerpt highlighting, container and modification date |
| GET | `/content/{id}` | `expand=body.storage,version,ancestors,space,restrictions.read.restrictions.user,history.lastUpdated` |
| GET | `/content/{id}/restriction/byOperation` | Read/update user restrictions |
| GET | `/content/{id}/restriction/byOperation/{read|update}` | One operation's restrictions |
| GET | `/content/{id}/child/attachment` | v1 attachment metadata and binary links |
| GET | `/space` | Visible spaces, `spaceKey`, type and offset pagination |
| GET | `/user/current` | Confluence user shape |

CQL supports parentheses, `AND`, `OR`, `NOT`, `=`, `!=`, comparisons, `IN`, `NOT IN`, `~`, `!~`,
`IS EMPTY` and `IS NOT EMPTY`. Fields include `type`, `space`, `space.key`, `space.id`, `id`, `title`,
`text`, `ancestor`, `parent`, `label`, `creator`, `lastmodifier`, `created` and `lastmodified`.
It supports quoted dates such as `"2026/10/01"`, ISO dates, `currentUser()`, `now()`, `startOfDay()`
and `endOfDay()`, plus relative day/hour/week dates. `ORDER BY` accepts supported fields.

```text
type=page AND space=KB AND lastmodified > "2026/10/01" AND text ~ "revenue"
type=page AND ancestor=28580455262078844 AND label=client-engagement
```

Unsupported or malformed clauses produce 400, including unknown fields in an `OR` branch.
Space membership and inherited read restrictions filter results before pagination.

### Jira REST v3 (`/rest/api/3`)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/myself` | Current delegated Jira user |
| GET | `/project/search` | Visible OPS/ENG projects, `query`, `startAt`, `maxResults` (50 default, 100 maximum) |
| GET, POST | `/search/jql` | Enhanced bounded JQL; `nextPageToken` pagination |
| GET | `/issue/{keyOrId}` | All supported fields by default; `fields` projection/exclusion |
| POST | `/issue` | Returns 201 and `{id,key,self}` |
| PUT | `/issue/{keyOrId}` | `fields` and `update`; 204, or 200 with `returnIssue=true` |
| GET, POST | `/issue/{keyOrId}/transitions` | To Do / In Progress / Done; POST returns 204 |
| GET, POST | `/issue/{keyOrId}/comment` | ADF comments; GET uses offset pagination, POST returns 201 |
| GET | `/user/search` | Active employees by `query` or `accountId`; offset pagination |

Enhanced search defaults to **IDs only**, unlike single-issue reads. Request `fields` explicitly
or use `*all`, `*navigable` and exclusions such as `-description`. It defaults to 50 results with
a maximum of 5000. The final response has `isLast=true` and omits `nextPageToken`. Tokens are bound
to the JQL, fields, caller and page size.

JQL supports the same boolean/comparison syntax, with `project`, `status`, `statusCategory`,
`assignee`, `reporter`, `priority`, `key`/`issuekey`, `id`, `issuetype`/`type`, `labels`, `parent`,
`summary`, `description`, `text`, `created`, `updated` and `duedate`/`due`. For example:

```text
project = OPS AND status != Done AND assignee = currentUser() ORDER BY updated DESC
```

Issue fields include summary, ADF description, status/category, assignee, reporter, priority,
labels, created/updated, due date, issue type, project and an epic parent. Writable fields are
validated atomically. `update` supports `set`, plus label `add` and `remove`. Change status through
transitions (`11` To Do, `21` In Progress, `31` Done). Invalid and unavailable transitions return 400.

## Seeded data and permissions

With seed `emulators` and anchor `2026-10-16`:

* 47 employee records (45 active), 17 groups, 52 spaces, 768 pages and 3 blog posts.
* 359 world documents become pages, including wrappers for non-text files. Folder pages preserve
  the original folder hierarchy; every space has a homepage. Past client meetings become pages
  beneath `CLIENTS → client → Meeting Notes`, with attendees and transcript text when available.
* 219 attachments project XLSX/PPTX/PDF/CSV/PNG source documents. Page versions and colleague
  comments provide realistic sync traversal data. Labels identify areas and client engagements.
* 60 Jira issues: 15 engagement epics with PBC and fieldwork tasks (45 ENG issues), five IT runbook
  tasks, and ten world support cases (15 OPS issues). Each seeded issue has a comment.

| Resource | Stable example ID |
| --- | --- |
| KB space | `38039918718755833` |
| CLIENTS space | `68980425353704209` |
| CLIENTS homepage | `28580455262078844` |
| PEOPLE space | `2739938333765470` |
| TECH space | `66814625710201405` |
| PARTNERS space | `13807301151156243` |
| Northwind engagement letter (`d0001`) | `22006646768537502` |
| Salary Bands (`d0260`) | `56453106306980675` |
| Payroll Calendar (`d0252`) | `10977634250744140` |
| Project Lighthouse (`d0285`) | `19769549299191065` |
| Engagement epic / PBC task / fieldwork task | `ENG-1` / `ENG-2` / `ENG-3` |

Space roles expand `World.Space.members` and world group membership, with `g-all` represented by
`confluence-users`. Viewer access does not grant updates; owners/editors can update. Personal
spaces are private to their owners. PARTNERS is restricted to actual world partners, including
when a non-partner belongs to the world's broader leadership group. Broken-inheritance document
ACLs become explicit read/update restrictions. Read restrictions propagate through descendants;
space membership is still required. Invisible pages, attachments and issues return 404 and are
excluded from lists/search. Operations reflect caller edit rights.

OPS allows active staff; ENG uses the CLIENTS space's roles. Selected fieldwork issues have
engagement-team/partner issue security. For example, p026 can view ENG-1 but cannot view ENG-3;
p001 can view both. ENG viewer roles cannot mutate issues.

## Webhooks and actions

`POST /_emulator/actions/configure_webhook` registers a synthetic dynamic subscription:

```json
{"url":"https://consumer.example/hook","events":["page_created","page_updated","jira:issue_created","jira:issue_updated"],"secret":"test-signing-key"}
```

Omitting `events` subscribes to all four. The response omits the secret. Deliveries go through
`ctx.webhooks.deliver`, support sync/async/manual hub modes and appear in `/_emulator/webhooks`.
Signatures are `X-Hub-Signature: sha256=<hex HMAC-SHA256(secret, exact request bytes)>`.
Confluence payloads have `userAccountId`, `accountType`, `timestamp` and a page containing numeric
ID, version, space key, content type, self URL and millisecond creation/modification dates.
Jira payloads have `webhookEvent`, `timestamp`, user and issue objects, with a changelog for field
updates/transitions. Adding a Jira comment also emits `jira:issue_updated`.

The other provider action is `mint_token`, described above. Standard reset/snapshot/fault/journal
actions remain available. Reset restores seeded content, revokes issued tokens and removes
subscriptions. Fault-injected 429 responses include `Retry-After`, `X-RateLimit-Limit` and
`X-RateLimit-Remaining`. Product-specific fault path patterns choose the corresponding envelope.

## Known gaps

* No Confluence folder/whiteboard/database resources, space administration, label/restriction
  writes, attachment upload/delete/version history, or comment mutation. Blog posts implement
  CRUD and properties; page-specific child/label/attachment endpoints are not duplicated for blogs.
* Seeded historical versions carry the source document's current text; runtime updates retain
  exact previous bodies/titles. Full rich-text conversion, macro rendering, collaborative drafts,
  automatic reconciliation, rich ADF validation and full Confluence publish/delete workflows are
  not modeled. Draft creation currently requires a title. Trashing/purging acts on the named page.
* CQL/JQL text matching uses case-insensitive substring/wildcard matching, not a Lucene index.
  Advanced functions, full query-language grammars and locale-specific relevance/date handling
  are not supported. Search only includes current content. Other optional filters/expansions not
  listed above are not implemented.
* Jira workflows are the three global statuses above. Boards/sprints, custom fields, subtasks,
  issue deletion, worklogs, issue links, advanced edit operations and extra expansions are absent.
  Project roles are simplified; per-field administrative permissions are not independently modeled.
* Cursors use deterministic offsets over current filtered data, not stable concurrent snapshots;
  they do not model vendor cursor expiration. Jira search is immediately consistent.
* No Forge/Connect installation JWT validation, production consent UI, scope implication graph,
  organization administration, multi-tenant provisioning or automatic rate-limit accounting.
  The control-plane app-only service-principal convention is an emulator facility.
* Webhook registration is a control-plane action, not Jira's public registration API. Confluence
  deliveries use the requested HMAC convention rather than Connect authorization JWTs. Subscriptions
  have no JQL filters or expiry. Only the four listed mutation event types are generated.
* The shared fault callback receives no request path. A fault pattern containing `/rest/api/3`,
  `/ex/jira/` or `/wiki/rest/` selects the proper family; generic patterns default to Confluence v2.
  Supply an explicit `body` when a generic fault must use a Jira/v1 error envelope.

## Verification and references

```text
uv run pytest tests/providers/atlassian -q
EMULATORS_PROVIDERS=atlassian uv run pytest tests/sdk/test_atlassian_http.py -q
uv run ruff check src/emulators/providers/atlassian tests/providers/atlassian tests/sdk/test_atlassian_http.py
uv run ruff format --check src/emulators/providers/atlassian tests/providers/atlassian tests/sdk/test_atlassian_http.py
```

The HTTP contract suite uses unchanged httpx site and OAuth gateway requests through the real
local server and `emulator_intercept`. No additional vendor SDK dependency is needed.

Validation snapshot for this implementation:

* Focused provider and live HTTP contract suites: **74 passed** (72 provider, 2 HTTP contract).
* Ruff lint and formatting checks: clean on the provider and its tests.
* Full repository run during concurrent provider work: **2119 passed, 8 failed, 1 skipped**.
  Failures were confined to Google: three Calendar tests, one Drive query test, three Gmail tests,
  and `tests/sdk/test_google_workspace_sdk.py::test_workspace_service_account_impersonation`.
  The skip was the optional `mutagen.mp4` media check. No Google/core/dependency files were changed
  as part of the Atlassian implementation.

Wire references: [Confluence v2 OpenAPI](https://dac-static.atlassian.com/cloud/confluence/openapi-v2.v3.json),
[Confluence v1 content restrictions](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-content-restrictions/),
[Confluence 3LO](https://developer.atlassian.com/cloud/confluence/oauth-2-3lo-apps/),
[Jira v3 OpenAPI](https://developer.atlassian.com/cloud/jira/platform/swagger-v3.v3.json),
and [Atlassian webhook payloads](https://developer.atlassian.com/cloud/confluence/modules/webhook/).
