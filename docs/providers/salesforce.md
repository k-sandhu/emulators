# Salesforce

The `salesforce` provider emulates one Salesforce Enterprise Edition org (`00D5g0000049673`) for Halcyon
Advisory Group: OAuth 2.0 / OIDC, SOAP login, the REST API (sObjects, SOQL, SOSL, composite, describe),
Bulk API 2.0, Salesforce Files and a role-hierarchy sharing model. It runs on port **44105**, through the
gateway's `/salesforce` prefix, or through the vendor hostnames below. There are no vendor network calls;
configured outbound-message receivers can be contacted.

`simple-salesforce` works unchanged (SOAP username/password/security-token login, session id + instance URL,
OAuth password, client-credentials and JWT-bearer logins, the sObject helpers, `query_all`, `search`,
`restful`, `bulk2`); see `tests/sdk/test_salesforce_sdk.py`.

## Hosts and URLs

`login.salesforce.com`, `test.salesforce.com`, `halcyonadvisory.my.salesforce.com`, `*.my.salesforce.com`,
`*.lightning.force.com`, `*.my.salesforce-setup.com`.

Token responses return `instance_url` = `https://halcyonadvisory.my.salesforce.com` and `id` =
`https://login.salesforce.com/id/<18-char org id>/<user id>`. Those URLs follow the routing mode the client
used: with the gateway prefix (`http://localhost:44080/salesforce`) or the per-port listener they point back at
that base, and in host mode they keep the port the client used. `nextRecordsUrl`, `attributes.url`,
`Location` and Bulk `contentUrl` are relative like the real service, so they work in every mode.

API versions `v31.0` through `v66.0` are accepted (`GET /services/data/` lists them with the real release
names: v62.0 is "Winter '25"); anything else is a 404. `simple-salesforce` defaults to v59.0, the examples below
use v62.0.

## Auth flows and test credentials

| Item | Value |
| --- | --- |
| Username | `<first.last>@halcyonadvisory.example.crm` for every employee (departed staff cannot sign in), `integration@halcyonadvisory.example.crm` |
| Password | `Halcyon2026!` for everyone |
| Security token | `EMULATORTOKEN` (append to the password, or omit it) |
| Admins | `arjun.mehta@…` and `sarah.johansson@…` (System Administrator) |
| Consumer key / secret | anything (lenient connected apps); secrets are not verified |
| Org id | `00D5g0000049673` (15) / `00D5g0000049673EAA` (18) |

* `GET /services/oauth2/authorize` — web-server flow (`response_type=code`), implicit (`token`), PKCE
  (`S256` only, as in Salesforce: a challenge without a method is read as `S256`; `plain`, a method without a
  challenge or a malformed challenge redirect with `error=invalid_request`), `state`, `nonce`, `scope`. With
  `login_hint=<username or email>` it redirects straight back with the code; without it, it renders an account
  picker. An unknown hint redirects with `error=access_denied`. The implicit flow only returns a
  `refresh_token` when `refresh_token`/`offline_access` was requested.
* `POST /services/oauth2/token` — `authorization_code`, `refresh_token`, `password` (username + password
  [+ token]), `client_credentials` (runs as the integration user, no refresh token) and
  `urn:ietf:params:oauth:grant-type:jwt-bearer` (`iss`=consumer key, `sub`=username, `aud`=a salesforce.com
  login host, `exp`; the signature is **not** verified). Responses have Salesforce's field order
  (`access_token`, `refresh_token`, `signature`, `scope`, `id_token`, `instance_url`, `id`, `token_type`,
  `issued_at`). Access tokens look like `00D5g0000049673!AQ0AQ…`, last two hours and are refused as
  `INVALID_SESSION_ID` after expiry, revocation or user deactivation. A refresh token is issued when
  `refresh_token`/`offline_access` is requested (or, for the code flow, no scope is). `openid` adds an RS256 `id_token`
  (JWKS at `/id/keys` and `/services/oauth2/jwks`). Authorization codes are single-use and bound to their
  client, redirect URI (required during exchange), and PKCE challenge. Refresh tokens are bound to their
  issuing client; refresh does not rotate them by default. JWT claims reject wrong types, malformed audiences
  and non-finite expiration values; `prn` is supported, expiration allows Salesforce's 3-minute clock skew,
  and a supplied `jti` cannot be replayed.
* Errors are `{"error": "invalid_grant", "error_description": "authentication failure"}` (also
  `invalid_client_id`, `unsupported_grant_type`, `expired access/refresh token`, `audience is invalid`...).
* `POST /services/oauth2/revoke` (revoking a refresh token also kills its access tokens),
  `POST /services/oauth2/introspect` (client credentials in the form or Basic header; only the issuing
  client can inspect a token, unless its registry entry has `introspect_all_tokens=True`),
  `GET|POST /services/oauth2/userinfo`, `GET /id/{orgId}/{userId}`
  (identity document), `GET /.well-known/openid-configuration`.
* `POST /services/Soap/u/{version}` (partner) and `/services/Soap/c/{version}` (enterprise): `login`
  (`serverUrl`, `metadataServerUrl`, `sessionId`, `userId`, `userInfo`), plus `logout` and `getUserInfo`.
  Bad credentials give the HTTP 500 SOAP fault `sf:INVALID_LOGIN`; versions outside 31.0-66.0 give
  `UNSUPPORTED_API_VERSION`. Other SOAP operations return a fault.
* REST auth is `Authorization: Bearer <token>` (or `OAuth <token>`). Missing/bad tokens return `401`
  `[{"message": "Session expired or invalid", "errorCode": "INVALID_SESSION_ID"}]`.
  Data endpoints require `api` or `full`; identity-only scopes cannot query or change records (`401
  INVALID_SESSION_ID`, `This session is not valid for use with the REST API`). `chatter_api` also permits
  the implemented Chatter user endpoints. Introspection reports deactivated users as inactive. See
  [Salesforce scopes](https://help.salesforce.com/s/articleView?id=remoteaccess_oauth_tokens_scopes.htm&language=en_US&type=5)
  and [token introspection](https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oidc_token_introspection_endpoint.htm&language=en_US&type=5).
* `POST /_emulator/tokens` (the `mint_token` action) takes `{user, scopes, client_id, app_only, expires_in}` where
  `user` is a world person id (`p012`), email, Username or Salesforce user Id, and returns a Salesforce-format
  token plus `instance_url`, `id`, `user_id`.

Settings (`EMULATORS_SALESFORCE_<KEY>` or hub `settings={"salesforce": {...}}`): `LENIENT_PASSWORDS=1` accepts any
password; `DAILY_API_LIMIT` (default 15000); `BULK_INPROGRESS_POLLS=N` makes Bulk jobs report `InProgress` for N
polls before `JobComplete`.

## Sharing model and permissions

Enforced for every token (the integration user and System Administrators see and edit everything).

| Object | Org-wide default | Read | Edit |
| --- | --- | --- | --- |
| Account, Contact, Campaign | Public Read Only | everyone | owner, users above the owner in the role hierarchy, admins |
| Opportunity, Lead, Case, `Engagement__c` | Private | owner, role ancestors, admins | same |
| OpportunityContactRole, OpportunityLineItem, CampaignMember | controlled by parent | parent's readers | parent's editors |
| Task, Event | owner / role ancestors, or access to the related record | | |
| ContentDocument/Version/Link, Attachment | by owner, or by access to any record the file is linked to / its parent | | owner or editor of the linked record |
| Product2, Pricebook2, PricebookEntry, User, UserRole, Profile | public read | everyone | admins |

Users in the *same* role (for example the two Audit Managers) cannot see each other's private records; only users
whose role is *above* the owner's do. Profiles: System Administrator (Arjun, Sarah), Sales User (business
development), Read Only (Office Manager, L&D Coordinator, IT Support), Standard User (everyone else) and
Salesforce API Only System Integrations (the integration user). Read Only cannot create/update/delete anything;
only Sales User and admins can create Campaigns; only admins can write setup objects.

Behaviour: queries silently filter invisible rows (also aggregates, semi-joins, subqueries, SOSL, Bulk query);
`GET/PATCH/DELETE` on an invisible record is `404 NOT_FOUND`; on a visible but non-editable record `403
INSUFFICIENT_ACCESS_OR_READONLY` (`insufficient access rights on object id`); creating without the object
permission is `403 INSUFFICIENT_ACCESS_OR_READONLY` (`entity type cannot be inserted: Account`); pointing a
lookup at a record you cannot see is `400 INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY`. Relationship fields
into records the caller cannot see (`What.Name`, `Account.Name`) come back `null`. `describe` reports
`createable/updateable/deletable` per caller.

The role hierarchy mirrors the org chart: *Managing Partner* → *Audit Partner*, *Tax Partner*, *Advisory
Partner*, *Chief Operating Officer*, *Chief Financial Officer*, *Director of Business Development*,
*Executive Assistant* → per-department *Manager* and *Staff* roles (COO → Head of People & Culture, Director of
Technology). Query `SELECT Id, Name, ParentRoleId FROM UserRole` for the whole tree.

## Endpoints

All paths are under `/services/data/vNN.N` unless noted.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/services/data/` | version list, no auth |
| GET | `/` | resource map (`sobjects`, `query`, `limits`, `identity` ...) |
| GET | `/limits`, `/limits/recordCount?sObjects=` | `DailyApiRequests` decrements per call; `Sforce-Limit-Info: api-usage=N/15000` on authenticated data responses; `403 REQUEST_LIMIT_EXCEEDED` past the limit |
| GET | `/sobjects` | describeGlobal (28 objects incl. `Engagement__c`) |
| GET | `/sobjects/{Type}` | object info + `recentItems` |
| GET | `/sobjects/{Type}/describe` (+ `/layouts`, `/compactLayouts`, `/approvalLayouts`) | full field metadata; `If-Modified-Since` → 304 |
| POST | `/sobjects/{Type}` | create → `201 {id, success, errors}` + `Location`; JSON or `multipart/form-data` (`entity_content` + binary part) |
| GET/PATCH/DELETE | `/sobjects/{Type}/{id}` | `fields=`; PATCH/DELETE → 204; 15- and 18-char ids; `If-Modified-Since`, `If-Unmodified-Since` |
| GET/PATCH/DELETE | `/sobjects/{Type}/{ExtIdField}/{value}` | external-id get / upsert (201 created, 204 updated) / delete; 300 for several matches |
| GET | `/sobjects/{Type}/{id}/{relationship}` | child relationship (`Contacts`) as a query result, or the parent record (`Account`, `Owner`) |
| GET | `/sobjects/{Type}/{id}/{BlobField}` | `VersionData`, `Body` bytes |
| GET | `/sobjects/{Type}/updated`, `/deleted` `?start=&end=` | 30-day window, minute resolution |
| GET | `/sobjects/{Type}/listviews` (+ `/{id}`, `/describe`, `/results`) | "All" and "My" views |
| POST/GET/DELETE | `/sobjects/User/{id}/password` | set / status / reset |
| GET | `/recent` | the caller's recently viewed/created records |
| GET | `/query`, `/queryAll`, `/query/{locator}` | SOQL; `Sforce-Query-Options: batchSize=200..2000`; `explain=`; cursors belong to the querying user, expire after two days, and can be replayed before expiry (`400 INVALID_QUERY_LOCATOR` otherwise) |
| GET | `/search?q=FIND {...}` | SOSL |
| GET/POST | `/parameterizedSearch` | `q`, `sobject`, `{Type}.fields`, `in`, `overallLimit` ... |
| POST | `/composite` | ≤25 subrequests; at most 5 query/queryAll/collection operations; `allOrNone`, `@{ref.id}` / `@{ref.records[0].Id}` / `@{ref[0].id}` references; collection subrequests participate in rollback |
| POST | `/composite/batch` | ≤25, boolean `haltOnError`; each executed subrequest counts toward API usage, while ordinary composite requests count once |
| POST | `/composite/tree/{Type}` | nested create with `referenceId`s, all-or-nothing, ≤200 records |
| POST/PATCH/DELETE | `/composite/sobjects` | writes ≤200, boolean `allOrNone`; `PATCH /composite/sobjects/{Type}/{ExtIdField}` upserts one type with an external-ID field and required `attributes.type` |
| GET/POST | `/composite/sobjects/{Type}` | retrieve ≤2000 records: GET `ids` + `fields`; POST `recordIds` + `fieldNames` (both required arrays); missing/invisible IDs produce null entries |
| POST/GET/PATCH/DELETE/PUT | `/jobs/ingest[/{id}[/batches]]`, `/jobs/ingest/{id}/successfulResults|failedResults|unprocessedrecords` | Bulk API 2.0 ingest |
| POST/GET/PATCH/DELETE | `/jobs/query[/{id}[/results]]` | Bulk API 2.0 query (`Sforce-Locator`, `Sforce-NumberOfRecords`) |
| GET | `/chatter/users/me`, `/chatter/users/{id}` | current user |
| any | `/services/apexrest/...` | `404 NOT_FOUND` (no Apex) |

`POST` with `?_HttpMethod=PATCH` (or `PUT`, `DELETE`, `GET`) is treated as that method, as in Salesforce, for
clients that cannot send `PATCH`. A request body that is not a JSON object (arrays, `NaN`, nested of the wrong
shape) is `400 JSON_PARSER_ERROR`; non-numeric `limit`/`maxRecords`/`overallLimit` parameters are
`400 INVALID_PARAMETER_VALUE`.

Errors are always arrays: `[{"message", "errorCode"[, "fields"]}]` — `INVALID_FIELD`, `INVALID_TYPE`,
`MALFORMED_QUERY` (with the query text, a caret and `ERROR at Row:R:Column:C`), `MALFORMED_ID`, `NOT_FOUND`,
`ENTITY_IS_DELETED`, `REQUIRED_FIELD_MISSING`, `INVALID_FIELD_FOR_INSERT_UPDATE`, `JSON_PARSER_ERROR`,
`INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST`, `STRING_TOO_LONG`, `NUMBER_OUTSIDE_VALID_RANGE`,
`INVALID_EMAIL_ADDRESS`, `DUPLICATE_VALUE`, `FIELD_INTEGRITY_EXCEPTION`, `INVALID_CROSS_REFERENCE_KEY`,
`FIELD_CUSTOM_VALIDATION_EXCEPTION`, `INSUFFICIENT_ACCESS_OR_READONLY`,
`INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY`, `REQUEST_LIMIT_EXCEEDED`, `INVALID_QUERY_LOCATOR`,
`DELETE_FAILED`, `CIRCULAR_DEPENDENCY`, `DUPLICATE_EXTERNAL_ID`, `INVALID_PARAMETER_VALUE`, `METHOD_NOT_ALLOWED`. Composite and collection responses use the `statusCode`/`errors` shapes of the real
service (`PROCESSING_HALTED`, `ALL_OR_NONE_OPERATION_ROLLED_BACK`).

Record JSON follows the real service: `attributes {type, url}` first, then every field in describe order,
datetimes `2026-10-15T14:22:05.000+0000`, dates `2026-10-15`, compound `BillingAddress` objects, relationship
fields nested in queries, child subqueries as `{totalSize, done, records}` or `null`, aggregates as
`AggregateResult` rows with `expr0...`. Ids are 18-character case-safe with the real key prefixes (`001`, `003`,
`006`, `00Q`, `500`, `005`, `00E`, `00e`, `00T`, `00U`, `701`, `00v`, `00K`, `00k`, `01s`, `01t`, `01u`, `069`,
`068`, `06A`, `00P`, `a0X`, `750`, `01g`); 15-character IDs preserve case, while 18-character IDs are
case-insensitive, including lookup values and Bulk job URLs.

Collection POST retrieval uses Salesforce's
[documented body names and 2000-record limit](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-composite-sobjects-collections-retrieve-post.html).
Composite requests support collection subrequests and the
[outer/inner allOrNone rules](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-composite-allornone.html).
Malformed reference IDs, non-string headers and incorrectly typed flags fail before writes occur.

## SOQL and SOSL

A real tokenizer and recursive-descent parser. Supported: field lists with relationship paths (to 5 levels),
`FIELDS(ALL|STANDARD|CUSTOM)` (`LIMIT ≤ 200` for ALL/CUSTOM), `COUNT()`, `COUNT/COUNT_DISTINCT/SUM/AVG/MIN/MAX`
with `GROUP BY` (+ `ROLLUP`, `CUBE`, `GROUPING()`) and `HAVING`, date functions (`CALENDAR_YEAR`, `DAY_ONLY`,
`FISCAL_QUARTER`, `HOUR_IN_DAY`...; they read datetimes in **UTC** unless the field is wrapped in
`convertTimezone()`, which uses the caller's zone), child subqueries, `TYPEOF`, `toLabel`, `FORMAT` (en_US text,
also around aggregates such as `FORMAT(SUM(Amount))`), `WHERE` with
`= != <> < <= > >= LIKE IN NOT IN INCLUDES EXCLUDES`, `AND/OR/NOT` (mixing `AND` and `OR` without parentheses is
the same syntax error Salesforce gives), `null`, booleans, numbers (incl. `1.0E7`), quoted strings with escapes,
date and datetime literals in every ISO form (impossible ones such as `2026-02-30` are `MALFORMED_QUERY`), all
relative date literals (`TODAY`, `LAST_N_DAYS:n`,
`THIS_QUARTER`, `NEXT_N_MONTHS:n`, fiscal variants...) evaluated against the hub clock **in the caller's time
zone**, semi- and anti-joins, `ORDER BY` several fields with `ASC/DESC NULLS FIRST/LAST`, `LIMIT`,
`OFFSET ≤ 2000`, `USING SCOPE mine|everything|team|...` (unknown scopes are a syntax error),
`WITH SECURITY_ENFORCED|USER_MODE|SYSTEM_MODE`,
`FOR VIEW|REFERENCE|UPDATE`, `ALL ROWS`, `FROM Account a` aliases. Text comparison is case-insensitive;
`!=` includes nulls; keys come back with the field's API-name casing. Polymorphic `Who`/`What`/`Owner`
relationships resolve by id prefix; their nested record is typed `Name` (`attributes.type`) unless selected with
`TYPEOF`, and `What.Type` / `Owner.Type` give the concrete sObject (`Opportunity`, `User`, `Queue`). Conditions
nested deeper than 100 levels are rejected as too complex. `ContentDocumentLink` queries need a `LinkedEntityId`/`ContentDocumentId`
filter, as in Salesforce.

SOSL: `FIND {…} IN ALL|NAME|EMAIL|PHONE|SIDEBAR FIELDS RETURNING Obj(fields WHERE … ORDER BY … LIMIT n), … LIMIT n`.
Terms are case-insensitive tokens with `*`/`?` wildcards, `"phrases"`, `AND`, `OR`, `AND NOT`, parentheses
(juxtaposed terms are OR-ed); phone fields match by digits.

## Seeded data

All of it comes from the shared Halcyon world; seeding takes about 0.3 s and ids are stable across runs.

| Object | Count | Notes |
| --- | --- | --- |
| Organization | 1 | `Id 00D5g0000049673EAA`, Enterprise Edition, `InstanceName NA224`, `IsSandbox false` |
| User | 48 | 47 employees (`p001`→`0055g0000000001AAA` … `p047`) + integration user `0055g0000000048AAA`; `ManagerId` mirrors the org chart; `TimeZoneSidKey` is IANA |
| UserRole / Profile | 23 / 5 | see *Sharing* |
| OpportunityStage / LeadStatus / CaseStatus | 10 / 4 / 4 | the standard picklists as queryable setup rows (`DefaultProbability`, `IsClosed`, `IsWon`...) |
| Account | 41 | 24 clients (`0015g0000000001AAA` Northwind Logistics … `…024`), 14 vendors (`Type Vendor`, from `…025`), 3 prospects created by converted leads |
| Contact | 89 | 72 client contacts (`0035g0000000001AAA` Christopher Ingram …), 14 vendor contacts, 3 converted-lead contacts; secondary contacts `ReportsToId` the primary |
| Opportunity | 47 | the 44 world deals (`0065g0000000001AAA` …), `ForecastCategoryName` Pipeline/Best Case/Commit/Closed/Omitted, `Engagement__c` lookup, plus 3 from converted leads |
| OpportunityContactRole / OpportunityLineItem | 60 / 30 | primary contact as *Decision Maker*; line items for won deals |
| Product2 / Pricebook2 / PricebookEntry | 10 / 1 / 10 | "Standard Price Book" |
| Lead | 18 | world statuses; 3 converted (`ConvertedAccountId/ContactId/OpportunityId`) |
| Case | 10 | `CaseNumber` `00001000`…`00001009`; next is `00001010` |
| Task / Event | 316 / 77 | `Email: <subject>` tasks (`TaskSubtype Email`, 298), 18 open follow-ups (one overdue); events for client meetings |
| Campaign / CampaignMember | 3 / 20 | *Year-End Tax Planning Webinar 2026* (`7015g0000000001AAA`), *Fall Audit Readiness Roundtable 2026*, *SR&ED Credit Workshop 2026* |
| `Engagement__c` | 30 | custom object (`keyPrefix a0X`): `Account__c`→`Account__r`, `Service__c`, `Status__c`, `Start_Date__c`, `End_Date__c`, `Budget_Hours__c`, `Hourly_Rate__c`, `Fixed_Fee__c`, `Billing_Type__c`, `Lead__c`→`Lead__r` (User), `Engagement_Code__c` (`e001`, external id) |
| ContentDocument / ContentVersion / ContentDocumentLink | 242 / 242 / 694 | every client document as a real DOCX/XLSX/PDF/PPTX file linked to its Account (and engagement) |

Account custom fields: `Client_Since__c`, `Payment_Terms__c` (`Net 15`…), `Preferred_Meeting_Platform__c`,
`Service_Lines__c` (multipicklist), `QuickBooks_Customer_Id__c` (external id, unique; best-effort alignment with
the QuickBooks emulator: the numeric part of the world client id).

Example calls (host mode):

```python
r = httpx.post("https://login.salesforce.com/services/oauth2/token", data={
    "grant_type": "password", "client_id": "any", "client_secret": "any",
    "username": "rajesh.iyer@halcyonadvisory.example.crm", "password": "Halcyon2026!EMULATORTOKEN"})
h = {"Authorization": "Bearer " + r.json()["access_token"]}
httpx.get("https://halcyonadvisory.my.salesforce.com/services/data/v62.0/query", headers=h,
          params={"q": "SELECT Id, Name, Owner.Name FROM Opportunity"})   # only Rajesh's own opportunities
```

## Business rules

Opportunity: `StageName` drives `Probability`, `IsClosed`, `IsWon`, `ForecastCategory(Name)` (stage table:
Prospecting 10, Qualification 20, Needs Analysis 40, Value Proposition 50, Id. Decision Makers 55, Perception
Analysis 60, Proposal/Price Quote 65, Negotiation/Review 80, Closed Won 100, Closed Lost 0), `ExpectedRevenue`,
`Fiscal*` from `CloseDate`, `LastStageChangeDate`; line items roll up into `Amount`/`HasOpportunityLineItem`.
Contact/Lead names are computed; Case gets autonumbered `CaseNumber` and `IsClosed`/`ClosedDate`; Task `IsClosed`
and `AccountId`; Event end/duration/`ActivityDate`; one primary contact role per opportunity; unique campaign
members; ContentVersion creates the ContentDocument and links. Deleting is a soft delete (`IsDeleted`, visible
to `queryAll`, `deleted`), cascading to children (Account → Contacts, Opportunities, Cases, tasks, events,
links) and clearing other lookups. Org validation rules: opportunity amount cannot be negative; engagement end
date must not precede its start (`FIELD_CUSTOM_VALIDATION_EXCEPTION`). Lookups can be written by external id:
`{"Account": {"QuickBooks_Customer_Id__c": "1"}}`; the field must be an external id (otherwise `INVALID_FIELD`:
*Provided external ID field does not exist or is not accessible*), the match must be unique and visible to the
caller (`DUPLICATE_EXTERNAL_ID`), and the request may not also set the lookup's own Id field.

Ranges and integrity: `Opportunity.Probability` is 0-100, `*Latitude` ±90, `*Longitude` ±180 and line-item
`Quantity` must be positive (`NUMBER_OUTSIDE_VALID_RANGE`). Events keep start, end and duration consistent: a new
`EndDateTime` wins and recomputes the duration, a new `DurationInMinutes` moves the end, and moving only the
start keeps the length; `ShowAs` defaults to `Busy`. Self-referencing hierarchies (parent account, parent campaign,
parent role, reports-to, parent case, user manager) cannot loop (`CIRCULAR_DEPENDENCY`). Records that are still referenced
cannot be deleted (`DELETE_FAILED`): a product, price book entry or price book used by opportunity products or
opportunities (the standard price book never can be), and a role that has users or child roles. `Campaign`
roll-ups (`NumberOfLeads`, `NumberOfContacts`, `NumberOfResponses`, `NumberOfOpportunities`, won counts and
amounts) start at 0 and are recomputed when members, opportunities or lead conversion change. An activity is
visible and editable through either its `WhoId` or its `WhatId` record.

## Bulk API 2.0

`POST /jobs/ingest` {`object`, `operation` insert|update|upsert|delete|hardDelete, `externalIdFieldName`,
`contentType CSV`, `lineEnding`, `columnDelimiter`} → `PUT …/batches` (CSV) → `PATCH {"state": "UploadComplete"}`;
the job runs at once and `GET` reports `JobComplete` with `numberRecordsProcessed/Failed`. Result CSVs have every
cell quoted: `"sf__Id","sf__Created",…`, `"sf__Id","sf__Error",…` (`REQUIRED_FIELD_MISSING:Required fields are
missing: [Name]:Name --`), and the header-only `unprocessedrecords`. A header with an unknown, duplicate or blank
column (or no `Id` for update/delete) fails the whole job (`state Failed`, `errorMessage InvalidBatch : Field name
not found : Foo`). Blank cells are ignored, `#N/A` clears a field,
`Account.QuickBooks_Customer_Id__c` style columns resolve lookups. `POST /jobs/query` (`query`/`queryAll`) → `GET
…/results?maxRecords=&locator=` (`maxRecords` ≥ 1) returns a CSV with `Sforce-Locator` (`null` at the end) and
`Sforce-NumberOfRecords`. Locators belong to one job; malformed, negative, out-of-range and cross-job locators
return `400 InvalidJob`. Reusing a valid locator repeats that page; the final page returns `Sforce-Locator: null`.
Malformed UTF-8 uploads are rejected; malformed CSV quoting, mismatched column counts and CSV parser failures
fail the job without inserting partial rows. Queries with subqueries or aggregates end `Failed` with an `errorMessage`. Jobs belong to
their creator (admins see all). Row-level sharing and object permissions apply.

## Webhooks (outbound messages) and control plane

Salesforce outbound messages are SOAP posts, not signed webhooks; the emulator sends the same envelope
(`<notifications xmlns="http://soap.sforce.com/2005/09/outbound">` with `OrganizationId`, `ActionId`,
`SessionId xsi:nil`, `EnterpriseUrl`, `PartnerUrl` and one `<Notification>` per record) through
`ctx.webhooks.deliver`, so they appear in `GET /_emulator/webhooks` and respect the webhook mode. Delivery
requires both a successful HTTP status and a SOAP `notificationsResponse/Ack` of `true` (or `1`); missing,
negative or malformed acknowledgements are retried, retaining the same notification IDs. Notifications are
split into messages of at most 100 records, and SOAP service URLs follow host/prefix/per-port routing.
Changes in a request are grouped by operation; rolled-back requests send nothing. See
[Salesforce outbound-message considerations](https://help.salesforce.com/articleView?id=workflow_om_considerations.htm&type=5).

Actions (`POST /_emulator/actions/<name>`):

| Action | Payload | Effect |
| --- | --- | --- |
| `mint_token` | `{user, scopes, client_id, app_only, expires_in}` | session token (also `POST /_emulator/tokens`) |
| `configure_outbound_message` | `{object, url, events: [create,update,delete,undelete], fields, name}` | register a SOAP outbound message (`url` must be http(s); `events` and `fields` are validated against the schema) |
| `list_outbound_messages`, `clear_outbound_messages` | | inspect / remove them |
| `set_api_usage` | `{used, remaining, max}` | provoke `REQUEST_LIMIT_EXCEEDED` |
| `convert_lead` | `{lead_id, do_not_create_opportunity, opportunity_name, account_id, contact_id, converted_status}` | the SOAP `convertLead` call (`account_id`/`contact_id` must exist, `converted_status` must be a converted `LeadStatus`) |
| `undelete` | `{id}` | restore from the recycle bin |
| `expire_sessions` | `{user}` | revoke every token of a user |

Every action answers `400 {"error": ...}` for payloads of the wrong type (a string where a number or list is
expected, negative `expires_in`, and so on) instead of failing.

Faults (`POST /_emulator/faults`) answer with Salesforce error arrays: 401 `INVALID_SESSION_ID`, 403/429
`REQUEST_LIMIT_EXCEEDED`, 409 `UNABLE_TO_LOCK_ROW`, 500 `UNKNOWN_EXCEPTION`, 502/503/504 `SERVER_UNAVAILABLE`.

## Compatibility audit regressions

Regression tests cover private-record lookups through `/sobjects/{Type}/Id/{id}` as well as direct IDs,
queries and SDK helpers. The same record is inaccessible to unrelated staff and accessible to its owner
or an administrator. Collection retrieval preserves null slots for invisible records.

`NEXT_N_DAYS:n` and `NEXT_90_DAYS` exclude today, following the
[SOQL date-literal reference](https://developer.salesforce.com/docs/platform/salesforce-soql-sosl/guide/sforce-api-calls-soql-select-dateformats.html).
Numeric and relative-date overflow, deeply nested functions and invalid timezone offsets produce vendor
errors instead of server errors. Multipart metadata uses the same JSON-object and finite-constant checks as
ordinary JSON requests. List-view page sizes, tree attribute types and composite headers are validated.

Opportunity products require an explicit `UnitPrice` or `TotalPrice`. A quantity-only update preserves
the total and adjusts the unit price, matching the
[documented opportunity-product behavior](https://help.salesforce.com/s/articleView?id=000381172&language=en_US&type=1).
Conflicting prices and attempts to clear a required price return DML errors; failed saves preserve parent rollups.
Event date arithmetic cannot overflow into a 500 response. Password reset returns a password that actually
authenticates and replaces the previous password. Unicode download filenames use an ASCII fallback plus
`filename*=UTF-8''...`; unsafe stored content-type headers fall back to `application/octet-stream`.

Audit tests live in `test_adversarial.py`, `test_auth_audit.py`, `test_outbound_audit.py` and the
Salesforce SDK contract tests. They use synthetic credentials; obtain current test credentials from
`GET /_emulator/info`.

## Known gaps

* No Streaming API / CometD, Change Data Capture, platform events, PushTopics or Pub/Sub API (use outbound
  messages or poll `updated`/`deleted`).
* SOAP API: only `login`, `logout`, `getUserInfo`. No Metadata, Tooling (`/tooling` is 404), Apex REST/execute
  anonymous, Chatter feeds, UI API, Connect API, reports/dashboards, Flows, approvals, Bulk API 1.0 (XML).
* Sharing: org-wide defaults and the role hierarchy only; no sharing rules, manual shares, teams, territories,
  public groups, queues (the `Group` object exists but is empty) or permission sets. Field-level security is the
  profile's object permission plus per-field `createable`/`updateable`.
* No record types (Master only), multi-currency, person accounts, duplicate/matching rules, triggers or flows;
  only the two validation rules above. Lead conversion is an emulator action; there is no `merge`.
* Data: `LastActivityDate` is computed at seed time and not maintained afterwards (the `Campaign` roll-ups
  are); `LastViewedDate` is updated by GET/`FOR VIEW`/create. `updated`/`deleted` use minute windows and a
  30-day retention.
* Request bodies nested thousands of levels deep are refused by the hub's request logging before they reach the
  provider (a hub limitation, not a Salesforce one).
* SOQL: no bind variables, `WITH DATA CATEGORY`, `convertCurrency`, geolocation functions, `FOR UPDATE` locking
  or pagination of child subqueries; `toLabel`/`FORMAT` assume en_US labels. SOSL has no snippets, highlighting,
  spell correction or `WITH DIVISION`. Describe layouts are minimal.
* Limits: only `DailyApiRequests` is metered (no concurrency, per-app or bulk-batch limits).
* Auth: client secrets and registered callback allowlists are lenient by default (authorization-code
  redirect binding is enforced); IP ranges, MFA and JWT signatures are not verified. Optional connected-app
  refresh-token rotation, dynamic client registration and child-session introspection are not implemented.
  Sandbox logins (`test.salesforce.com`) reach the same org.
* Outbound messages use three attempts with short retry delays instead of Salesforce's 24-hour queue.
  Queued payloads are snapshots, null fields are omitted, and session IDs are not included. No callback
  validation handshake is required by this SOAP protocol.
* Bulk jobs execute synchronously when closed; polling delays simulate asynchronous progress. CSV fields
  are subject to Python's CSV parser field-size limit (normally 128 KiB). XML response negotiation and
  OAuth `format=xml|urlencoded` are not implemented.
* Files: no ContentNote, ContentDistribution or chunked uploads; `ContentDocument` sharing is approximated by
  the visibility of the records it is linked to.
