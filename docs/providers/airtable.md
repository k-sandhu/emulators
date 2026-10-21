# Airtable

The `airtable` provider implements a useful Airtable Web API subset on port **44119**,
the gateway's `/airtable` prefix, and these real hosts:

| Host | Surface |
| --- | --- |
| `api.airtable.com` | `/v0` records, metadata and webhooks |
| `content.airtable.com` | Direct attachment uploads |
| `v5.airtableusercontent.com` | Expiring, public attachment download capabilities |
| `airtable.com` | OAuth authorization, token exchange and revocation |

Requests retain vendor URLs when using the emulator transport or Docker hostname
aliases. Attachment URLs also work through the gateway prefix and dedicated port.
The provider makes no vendor or external-attachment network requests.

## Authentication and permissions

Send `Authorization: Bearer <token>`. Seeded PATs:

| Token | User | Access |
| --- | --- | --- |
| `pat-airtable-owner` | Eleanor Whitfield, `eleanor.whitfield@halcyonadvisory.example` | Creator in both bases |
| `pat-airtable-reader` | Sofia Alvarez, `sofia.alvarez@halcyonadvisory.example` | Read-only in Client Operations; no Finance access |

`POST /_emulator/tokens` accepts `user` (world ID, Airtable user ID, or email),
`scopes`, optional `bases` (base-ID allowlist), and `expires_in` (default 3600).
It returns an opaque PAT. Tokens always act as an active user. App-only grants are
rejected because this provider does not model Airtable service accounts.

Supported scopes are `data.records:read`, `data.records:write`, `schema.bases:read`,
`webhook:manage`, and `user.email:read`. An omitted `bases` restriction grants only
the user's existing base access; it never grants additional roles. Reads require
read access, record writes require editor access, and webhook creation/deletion/
refresh require creator access. Partners and administrators are creators; other
active employees are editors, with Sofia deliberately read-only. Finance membership
is limited to partners, administrators and Finance employees. Missing, expired,
revoked or inactive-user tokens receive Airtable-shaped 401 errors. Missing scopes
and inaccessible bases receive 403 `INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND`.

OAuth uses `airtable-emulator` / `airtable-emulator-secret` and registered redirect
`https://consumer.example/callback`. Authorization requires S256 PKCE, a 16–1024
character state and supported scopes. Supply `login_hint=<world ID or email>` for
headless consent, or use the account picker. Access tokens expire after one hour;
refresh tokens expire after 60 days and rotate, revoking the preceding access and
refresh tokens. Refresh can reduce scopes. Revocation invalidates the grant family.
Public clients use a body `client_id`; confidential clients use HTTP Basic auth.
Client registration is lenient by default, as elsewhere in the repository; pass
`EmulatorHub(lenient_clients=False)` to enforce registered clients and secrets.

## Seeded relational organization

IDs have Airtable's 17-character shape and are deterministic for the world seed.
For the default seed and anchor `2026-10-16`:

| Base / table | ID | Records |
| --- | --- | ---: |
| Halcyon Client Operations | `app4a9903997412f2` | |
| Employees | `tbl7d012f5cb133d5` | 47, including 45 active |
| Clients | `tblfa6fe80e47336d` | 24 |
| Engagements | `tbld724627c2db464` | 30 |
| Documents | `tbl52b05b2016e99e` | 250 |
| Halcyon Finance | `appfbc17061608f4d` | |
| Employees | `tbla4a75547849898` | 47 |
| Clients | `tbl8f95b9aa96e6e4` | 24 |
| Engagements | `tbl8364e88efc4e94` | 30 |
| Documents | `tblb887d0e9a9e4e4` | 9 |
| Invoices | `tbl982221da852447` | 115 |

The 576 records come from the same world used by the other emulators. Employee
manager/direct-report links and client/engagement links have reciprocal schema
definitions and update both directions. Other links connect client owners,
engagement teams, documents and invoices within each base. Deleting a record
removes references to it. Personal, restricted and trashed source documents are
excluded; Finance documents remain in the restricted Finance base. These Airtable
bases are sharing boundaries: every base member can read its projected records.

Invoice `Balance` is a computed formula over `Total` and `Paid` and cannot be
written. Client tables include a filtered **Active clients** view; all tables have
a **Grid view**. Departed employees remain historical directory records but cannot
authenticate. Seed construction takes approximately 0.25 seconds and renders no
binary files until an attachment is requested.

## Endpoints

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/v0/meta/whoami` | User ID and scopes; email only with `user.email:read` |
| GET | `/v0/meta/bases` | Accessible bases and effective `permissionLevel` |
| GET | `/v0/meta/bases/{baseId}/tables` | Fields, types, options, primary fields and views; `include[]=visibleFieldIds` |
| GET | `/v0/{baseId}/{tableIdOrName}` | List/filter/sort/page records |
| POST | `/v0/{baseId}/{tableIdOrName}/listRecords` | Same list options in a JSON body; timezone/locale may remain query parameters |
| GET | `/v0/{baseId}/{tableIdOrName}/{recordId}` | One record |
| POST | `/v0/{baseId}/{tableIdOrName}` | Single `{fields}` or batch `{records:[{fields},...]}` create |
| PATCH, PUT | `/v0/{baseId}/{tableIdOrName}` | Batch updates, or `performUpsert` with 1–3 `fieldsToMergeOn` |
| PATCH, PUT | `/v0/{baseId}/{tableIdOrName}/{recordId}` | Merge specified fields / replace and clear unspecified fields |
| DELETE | `/v0/{baseId}/{tableIdOrName}/{recordId}` | `{id, deleted:true}` |
| DELETE | `/v0/{baseId}/{tableIdOrName}?records[]=...` | Batch deletion; `{records:[{id,deleted:true},...]}` |
| POST | `/v0/{baseId}/{recordId}/{attachmentFieldIdOrName}/uploadAttachment` | On content host; `{file:<base64>,filename,contentType}`; 5 MiB maximum; response fields keyed by field ID |
| GET, HEAD | `/attachments/{attachmentId}?token=...` | On content/download host; two-hour capability, no Bearer token required |
| GET, POST | `/v0/bases/{baseId}/webhooks` | List owned registrations / create |
| DELETE | `/v0/bases/{baseId}/webhooks/{webhookId}` | Remove; `{}` |
| POST | `/v0/bases/{baseId}/webhooks/{webhookId}/refresh` | Extend active hook seven days |
| POST | `/v0/bases/{baseId}/webhooks/{webhookId}/enableNotifications` | `{enable:true|false}`; `{}` |
| GET | `/v0/bases/{baseId}/webhooks/{webhookId}/payloads` | Transaction cursor and up to 50 payloads; refreshes active hook |
| GET | `/oauth2/v1/authorize` | Consent, state and S256 PKCE |
| POST | `/oauth2/v1/token` | Form-encoded code exchange and refresh |
| POST | `/oauth2/v1/revoke` | Form-encoded grant-family revocation |

Batch writes and deletes accept at most 10 records and validate atomically. Invalid
records, references, duplicate record IDs or ambiguous upsert matches leave state
unchanged and emit no webhooks. Fields can be supplied by name or ID. Empty values
(`null`, `""`, `[]`, `false`) are omitted from responses; numeric zero is retained.
Number/currency, checkbox, dates, single select, collaborator, linked-record,
attachment and text fields validate their wire types. `typecast:true` supports
numeric strings and common checkbox strings. Computed cells remain read-only.

Lists support `fields[]`, `filterByFormula`, `view`, `sort[n][field]`,
`sort[n][direction]`, `pageSize` 1–100, `maxRecords`, opaque `offset`,
`returnFieldsByFieldId`, `recordMetadata[]=commentCount` (zero), `cellFormat`,
`timeZone`, and `userLocale`. POST list options use arrays/objects directly.
Offsets bind to the table, query, client, user, scopes and token base restriction;
they expire after one hour and reset invalidates them. String cell formatting
requires a timezone and locale. Duplicate/malformed/unsupported query parameters
are rejected with Airtable error envelopes.

The safe formula subset supports field names/IDs in braces, numeric/string
literals, comparisons, arithmetic, concatenation `&`, parentheses, `AND`, `OR`,
`NOT`, `IF`, `FIND`, `SEARCH`, `LOWER`, `UPPER`, `LEN`, `CONCATENATE`, `RECORD_ID`,
`TRUE`, `FALSE`, `BLANK`, `IS_AFTER` and `IS_BEFORE`. Parsing is bounded to 16,000
characters, 512 tokens and 32 nested expressions; unsupported formulas receive
422 `INVALID_FILTER_BY_FORMULA`. Per-record formula arithmetic errors exclude
that record. No request text is evaluated as Python or JavaScript.

## Attachments and webhooks

Files live in content-addressed blobs. Seeded documents render real Office, PDF,
CSV, Markdown, text or PNG bytes. Direct uploads preserve exactly the supplied
bytes. External URL attachments are never fetched: supported document extensions
produce a small synthetic file; other extensions produce text. Download links
are unguessable, expire after two hours, and are independent of access-token life.

Webhook creation returns `macSecretBase64` once. Notifications contain
`{base:{id},webhook:{id},timestamp}` and
`X-Airtable-Content-MAC: hmac-sha256=<hex HMAC-SHA256(decoded secret, exact body)>`.
Payloads carry transaction numbers, timestamps, `publicApi` source metadata and
`changedTablesById` with created/changed/deleted records and V2 cell values.
Supported specifications use `dataTypes:["tableData"]`, optional table-ID
`recordChangeScope`, `changeTypes`, `fromSources`, and `watchDataInFieldIds`.
Includes support previous values and explicitly included/all unchanged fields.
Hooks expire after seven days; active refresh/payload reads extend their life.
Payloads expire after seven days. A base permits 10 hooks; each OAuth integration
permits 2 per base. Personal tokens can access the base's hooks; OAuth tokens can
access only their integration's hooks, subject to base roles and token scopes.
Notifications honor the framework's sync/async/manual delivery modes and journal.

## Actions

| Action | Payload | Result |
| --- | --- | --- |
| `mint_token` | `{user,scopes?,bases?,expires_in?}` | Scoped delegated PAT |
| `revoke_token` | `{token}` | Revoke one local token |
| `replay_webhook_notification` | `{webhook_id}` | Replay an active hook's signed notification without manufacturing a transaction |

## Known gaps

- Base/table/field/view creation and schema mutation, enterprise administration,
  interfaces, comments, record revision history and Airtable Sync are not implemented.
- OAuth consent grants all accessible bases; use control-plane `bases` restrictions
  to test narrower grants. Service accounts and workspace administration are absent.
- Formula support is the bounded subset above, without rollups/lookups, dynamic
  time functions, full Airtable coercion/error semantics or regex/date functions.
- Typecasting does not create select options, collaborators or linked records.
  Only the seeded field types are supported. String formatting is deliberately
  simple and is not a full locale renderer. Views are seeded and cannot be edited.
- Record cursors use live ordered results rather than an immutable snapshot;
  concurrent insertions/deletions can shift offsets. The two seeded bases fit one
  metadata page. Metadata base iteration does not synthesize additional pages.
- Attachment thumbnails, range requests, remote attachment ingestion and async
  ingestion failures are absent. Attachment URLs use a local capability path,
  preserving the vendor content hostname rather than its opaque CDN URL layout.
- Hooks report the directly mutated table; inverse-link side effects do not emit
  separate table payloads. View-scoped/schema webhook specifications are rejected.
  Dispatch retries follow the framework rather than Airtable's production schedule;
  notification coalescing and delivery-failure deactivation are not modeled.
- Live rate and billing quotas are not enforced; use framework 429 fault injection.

## Validation and primary references

```powershell
uv run --frozen pytest tests/providers/airtable -q
$env:EMULATORS_PROVIDERS='airtable'
uv run --frozen pytest tests/sdk/test_airtable_http.py tests/sdk/test_airtable_sdk.py -q
uv run --frozen ruff check src/emulators/providers/airtable tests/providers/airtable tests/sdk/test_airtable_http.py tests/sdk/test_airtable_sdk.py
uv run --frozen ruff format --check src/emulators/providers/airtable tests/providers/airtable tests/sdk/test_airtable_http.py tests/sdk/test_airtable_sdk.py
```

The official **airtable.js 0.12.2** contract uses a real local gateway socket and
the SDK's supported `endpointUrl` configuration. It checks select/pagination,
formulas, field IDs, create/find/update/replace, batches, deletes and SDK errors.
Install it outside the repository; tests never download packages themselves:

```powershell
npm.cmd install --prefix "$env:TEMP/emulators-airtable-sdk" --no-save --no-package-lock airtable@0.12.2
uv run --frozen pytest tests/sdk/test_airtable_sdk.py -q
```

Set `AIRTABLE_SDK_NODE_MODULES` to another external `node_modules` directory if
needed. The SDK case skips only when Node or that optional package is absent.
No repository runtime or development dependencies were changed.

Primary contracts: [records/listing](https://airtable.com/developers/web/api/list-records),
[batch update/upsert](https://airtable.com/developers/web/api/update-multiple-records),
[base schema](https://airtable.com/developers/web/api/get-base-schema),
[attachment upload](https://airtable.com/developers/web/api/upload-attachment),
[OAuth](https://airtable.com/developers/web/api/oauth-reference),
[webhook delivery](https://airtable.com/developers/web/api/webhooks-overview),
[webhook specification](https://airtable.com/developers/web/api/model/webhooks-specification),
[payloads](https://airtable.com/developers/web/api/list-webhook-payloads),
[field values](https://airtable.com/developers/web/api/field-model),
and the [official Airtable JavaScript client](https://github.com/Airtable/airtable.js).
