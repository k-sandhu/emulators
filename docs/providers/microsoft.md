# Microsoft identity, directory and Outlook

This provider emulates Microsoft Entra ID and Microsoft Graph locally. Identity endpoints
answer on `login.microsoftonline.com` and `login.windows.net`; Graph answers on
`graph.microsoft.com`. The gateway prefix is `/microsoft` and the dedicated port is 44101.
Every Graph route below is available under both `/v1.0` and `/beta`.

See [Files, SharePoint, Teams and meetings](microsoft-graph-files-teams.md) for the other
Microsoft resource areas. Those modules share user IDs, meeting join URLs, authentication,
and the change notification bus with this implementation.

## Running and authentication

```python
from emulators.core.hub import EmulatorHub
import httpx

hub = EmulatorHub(providers=["microsoft"], anchor="2026-10-16", webhook_mode="sync")
async with httpx.AsyncClient(transport=hub.async_transport()) as client:
    response = await client.post(
        "https://graph.microsoft.com/_emulator/tokens",
        json={"user": "maya.chen@halcyonadvisory.example", "scopes": ["User.Read", "Mail.Read"]},
    )
    token = response.json()["access_token"]
    response = await client.get(
        "https://graph.microsoft.com/v1.0/me/calendarView",
        params={"startDateTime": "2026-10-01T00:00:00Z", "endDateTime": "2026-11-01T00:00:00Z"},
        headers={"Authorization": f"Bearer {token}"},
    )
```

Tokens minted through the control plane and OAuth endpoints are RS256 JWTs, registered in
the shared token service. JWTs contain tenant and object IDs, delegated `scp` or application
`roles`, client IDs, timestamps, issuer, and Graph audience. Application tokens use
`{"app_only": true}`; `/me` requires delegated authentication. Access tokens expire according
to the hub clock. Reset invalidates runtime grants, subscriptions, mutations and delta cursors.

Any client ID/secret and synthetic password work with the default lenient client registry.
Example client ID: `00000000-0000-0000-0000-00000000c0de`. No real credentials are used.

The tenant segment accepts the world tenant GUID, `common`, `organizations`,
`halcyonadvisory.example`, and `halcyonadvisory.onmicrosoft.com`. `consumers` and unknown
tenants produce AAD-shaped errors. Disabled employees cannot sign in.

Supported OAuth grants: authorization code with PKCE S256/plain; refresh token;
client credentials with `https://graph.microsoft.com/.default`; password; device code;
and JWT bearer on behalf of a delegated token. Client assertions are checked for structure,
client binding and expiry, with intentionally loose signature validation. Request `openid`
for an ID token, `offline_access` for a refresh token, and `client_info=1` for MSAL account
cache metadata. V1 token requests accept `resource`.

Delegated `.default` grants expand to configured consented permissions instead of putting
`.default` in `scp`. Tests can register a client with `delegated_scopes=["User.Read", ...]`
through `hub.context("microsoft").tokens.register_client(...)`; lenient clients use
the documented defaults in `identity.DEFAULT_DELEGATED_SCOPES`. Graph-prefixed and short
scope names are equivalent during code redemption and refresh. Refresh tokens rotate
when the original grant included `offline_access`, including after scope narrowing.
V2 client credentials require `scope`, return no `scope` response field, and authentication
failures return 401. V1 expiry fields are strings and device discovery uses `verification_url`.

Authorization supports `code`, `id_token`, and hybrid `code id_token` responses;
`response_mode=query|fragment|form_post`; state, nonce and login hints. Without `login_hint`,
the shared account picker lists active employees. The form-post response submits itself.

## Identity endpoints

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/{tenant}/v2.0/.well-known/openid-configuration` | V2 discovery, local routing-aware endpoints |
| GET | `/{tenant}/.well-known/openid-configuration` | V1 discovery |
| GET | `/{tenant}/discovery/v2.0/keys`, `/{tenant}/discovery/keys` | RSA JWKS, including `common` |
| GET | `/common/discovery/instance` | MSAL instance metadata |
| GET | `/common/userrealm/{upn}` | Managed realm discovery |
| GET | `/{tenant}/oauth2/v2.0/authorize`, `/{tenant}/oauth2/authorize` | Account picker, code/PKCE, hybrid and ID token |
| POST | `/{tenant}/oauth2/v2.0/token`, `/{tenant}/oauth2/token` | Form-encoded grants |
| POST | `/{tenant}/oauth2/v2.0/devicecode`, `/{tenant}/oauth2/devicecode` | Device code, 900-second lifetime, 5-second polling interval |
| GET | `/{tenant}/oauth2/v2.0/logout`, `/{tenant}/oauth2/logout` | Optional post-logout redirect |
| GET, POST | `https://graph.microsoft.com/oidc/userinfo` | Discovered OIDC endpoint; pairwise subject, names, email and photo URL |

## Directory endpoints

| Method | Graph path | Notes |
| --- | --- | --- |
| GET | `/me`, `/users`, `/users/{id-or-upn}` | Default 11 properties including ID; extended fields with `$select` |
| POST, PATCH, DELETE | `/users`, `/users/{id-or-upn}` | Create, update, delete; app or world tenant admin required |
| PATCH | `/me` | Self-service profile fields; directory privilege changes require an administrator |
| GET | `/users/delta`, `/groups/delta` | Snapshot pages, opaque resource-bound cursors, changed entities and deletion markers |
| GET | `/organization`, `/domains`, `/subscribedSkus`, each also `/{id}` | Verified domains, licenses and service plans |
| GET | `/users/{id}/manager`, `/directReports`, `/memberOf`, `/transitiveMemberOf`, `/ownedObjects` | Also under `/me`; manager expansions support `$select` and `$levels=max` |
| GET | `/users/{id}/photo`, `/photo/$value`, `/photos`, `/photos/{size}`, `/photos/{size}/$value` | Also under `/me`; generated 64px PNG or `ImageNotFound` |
| POST | `/users/{id}/checkMemberGroups`, `/getMemberGroups`, `/checkMemberObjects`, `/getMemberObjects` | Also under `/me`; transitive ID collections and membership checks |
| POST | `/directoryObjects/getByIds` | ID and optional type filtering |
| GET | `/directoryObjects/{id}` | User or group with directory object type annotation |
| GET | `/groups`, `/groups/{id}`, `/groups/{id}/members`, `/owners`, `/transitiveMembers` | World group types, membership and owners |
| POST, PATCH, DELETE | `/groups`, `/groups/{id}` | Create as administrator/application; update/delete as owner or administrator |
| POST | `/groups/{id}/members/$ref`, `/groups/{id}/owners/$ref` | Owner or tenant admin; `@odata.id` body |
| DELETE | `/groups/{id}/members/{id}/$ref`, `/groups/{id}/owners/{id}/$ref` | Owner or tenant admin; final owner cannot be removed |
| GET | `/users/$count`, `/groups/$count`, `/groups/{id}/members/$count` | Plain text; `ConsistencyLevel: eventual` required; other directory relationships also support count |
| GET | `/groups/{id}/members/microsoft.graph.user`, `/me/memberOf/microsoft.graph.group` | User/group casts on directory relationship collections, including transitive variants |
| POST | `/$batch` | Up to 20 requests; case-insensitive IDs/dependencies, failure propagation, same-app dispatch; canonical content headers, no subresponse Content-Length, binary request bodies decoded from base64url and binary responses base64 encoded |

Directory collections default to 100 records, capped at 999. OData filtering, ordering,
selection, searching and skip-token paging use the shared OData engine. `$search` and
`$count=true` on directory collections require `ConsistencyLevel: eventual`. Guest users
cannot enumerate users. User/group writes enforce world administrator or group-owner roles.
Every Graph response includes `request-id` and `client-request-id`, including failures
and injected faults. Each error's `innerError` uses the same IDs; the latter echoes
the caller's header when supplied. Graph 401s include the Bearer authorization challenge.
User defaults also apply to directory relationships and delta; directory objects retain
`@odata.type` when projected. Explicit `$select` can request additional properties.
OData key syntax such as `/users('maya.chen@halcyonadvisory.example')` and
`/me/mailFolders('Inbox')/messages` is accepted.
Directory relationship filters, searches, ordering and type casts require
`ConsistencyLevel: eventual` and `$count=true`; count segments need the header alone.

Collections and delta honor `Prefer: odata.maxpagesize=N`, echo `Preference-Applied`,
and preserve literal `$top` and `$skiptoken` keys in paging links. Unknown root property
names in `$select`, `$filter` and `$orderby` are rejected for users, groups, messages and
events. JSON mutation endpoints reject malformed JSON and nonobject payloads with 400.

## Outlook endpoints

In the table, `{mailbox}` is `/me` or `/users/{id-or-upn}`. Delegated access is restricted
to the caller's mailbox; app tokens may address any internal mailbox. Private contacts
and calendar data use the same ownership check.

| Method | Graph path | Notes |
| --- | --- | --- |
| GET | `{mailbox}/mailFolders`, `/mailFolders/{id}`, `/mailFolders/{id}/childFolders` | Well-known names and nested folders; computed item/unread/child counts |
| POST | `{mailbox}/mailFolders`, `/mailFolders/{id}/childFolders` | Create folders with a parent and optional hidden state |
| GET | `{mailbox}/mailFolders/delta` | Initial folder hierarchy and resource-bound change cursor |
| GET, POST | `{mailbox}/messages`, `/mailFolders/{id}/messages` | List or create draft; list default 10, maximum 1000 |
| GET | `{mailbox}/messages/{id}`, `/messages/{id}/$value` | Message JSON or RFC 822 MIME; folder-qualified GET aliases also supported |
| GET | `{mailbox}/messages/{id}/attachments`, `/attachments/{id}`, `/attachments/{id}/$value` | File attachment metadata, base64 content or bytes; folder-qualified aliases |
| POST | `{mailbox}/messages/{id}/attachments` | File attachments smaller than 3 MiB; folder-qualified alias supported |
| PATCH, DELETE | `{mailbox}/messages/{id}`, `/mailFolders/{fid}/messages/{id}` | Read/flag/category updates; drafts also editable; delete moves to Deleted Items; folder membership checked |
| POST | `{mailbox}/sendMail` | JSON message or base64 MIME; internal delivery; `saveToSentItems` |
| POST | `{mailbox}/messages/{id}/send`, `/createReply`, `/createReplyAll`, `/createForward`, `/reply`, `/replyAll`, `/forward`, `/move`, `/copy` | Draft creation returns 201; sending returns 202; per-mailbox copies and new move/copy IDs |
| GET | `{mailbox}/messages/$count`, `/mailFolders/{id}/messages/$count` | Plain-text mailbox/folder message count |
| GET | `{mailbox}/mailFolders/{id}/messages/delta` | Folder-specific delta; move/delete tombstones |
| GET, PATCH | `{mailbox}/mailboxSettings` | Time zone, working hours and automatic replies |
| GET | `{mailbox}/calendars`, `/calendar`, `/calendars/{id}`, `/calendarGroups`, `/calendarGroups/{id}/calendars` | Default calendar and calendar group |
| POST | `{mailbox}/calendars` | Create an additional calendar; events remain scoped to their calendar |
| GET, POST | `{mailbox}/events`, `/calendar/events`, `/calendars/{id}/events` | Single instances and series masters; create events and internal invitations |
| GET | `{mailbox}/calendarView`, `/calendar/calendarView`, `/calendars/{id}/calendarView` | Both date range parameters required; occurrences expanded |
| GET, PATCH, DELETE | `{mailbox}/events/{id}` | Calendar-qualified aliases; transactional event creation |
| GET | `{mailbox}/events/{id}/instances` | Seeded and runtime recurrence expansion, date range required |
| POST | `{mailbox}/events/{id}/accept`, `/decline`, `/tentativelyAccept`, `/cancel`, `/forward` | Attendee response state and organizer checks |
| POST | `{mailbox}/events/{id}/snoozeReminder` | Validated `newReminderTime`; returns 204; calendar-qualified aliases |
| GET, POST | `{mailbox}/events/{id}/attachments` | List or create small file attachments; calendar-qualified aliases |
| GET, DELETE | `{mailbox}/events/{id}/attachments/{aid}` | Metadata or delete; `/{aid}/$value` downloads bytes |
| POST | `{mailbox}/calendar/getSchedule` | Availability views and schedule items |
| POST | `{mailbox}/findMeetingTimes` | Simplified suggestion response |
| GET | `{mailbox}/events/delta`, `/calendarView/delta` | Delta links and changed events |
| GET, POST | `{mailbox}/contacts`, `/contactFolders/{id}/contacts` | Private relationship and colleague contacts; list/create |
| GET, PATCH, DELETE | `{mailbox}/contacts/{id}`, `/contactFolders/{id}/contacts/{id}` | Contact CRUD |
| GET | `{mailbox}/contacts/delta`, `/contactFolders/{id}/contacts/delta` | Contact changes and deletion markers |
| GET, POST | `{mailbox}/contactFolders`, `/contactFolders/{id}/childFolders` | Folder hierarchy and creation |
| GET | `{mailbox}/contactFolders/{id}` | Folder metadata |

Mail supports `$search` terms, sender-qualified searches, read/attachment/date/sender filters,
and `Prefer: outlook.body-content-type="text"` with `Preference-Applied` when honored.
Messages are newest first by default; folder collections default to ten items.
Message and event bodies normalize `HTML`/`Text` to `html`/`text`. Sending requires at
least one valid recipient and a recognized importance value; rejected sends do not
create drafts. Attachment documents are rendered on demand
and cached in the blob store; uploaded attachment bytes also use the blob store.
Well-known folders include `inbox`, `drafts`, `sentitems`, `deleteditems`, `junkemail`,
`archive`, `outbox`, `conversationhistory` and `msgfolderroot`.

Calendar dates use seven fractional digits. `Prefer: outlook.timezone="Pacific Standard Time"`
and the world's Windows/IANA zones are supported. Teams events share join URLs with
onlineMeeting resources; Zoom/Meet links are included in event bodies. Runtime recurrence
supports daily, weekly, absolute/relative monthly and yearly patterns, preserving local
wall time across DST. CalendarView pagination preserves the original request path, query,
and routing prefix.
Contacts carry `changeKey`, `@odata.etag`, and the Outlook create defaults. Directory
users created at runtime receive mailbox settings and an empty contacts folder.
Meeting-time suggestions normalize both ends to Graph's seven-digit `dateTimeTimeZone`
form. Free/busy codes are `0` free/workingElsewhere, `1` tentative, `2` busy and `3` oof;
unknown mailboxes report `ErrorMailRecipientNotFound`.

## Seeded data

For seed `emulators`, anchor `2026-10-16`:

| Entity | Example / count |
| --- | --- |
| Tenant | `7800e61c-0c01-4c6d-8295-e2587268006a` |
| Maya Chen | `maya.chen@halcyonadvisory.example`, world `p018`, Graph `02084459-8781-4fc6-ad74-ba491d0c93bb` |
| Administrator | `arjun.mehta@halcyonadvisory.example`, world `p035`, Graph `a9e53fd6-4578-43f2-ae76-045f8fb9105e` |
| Other administrator | `sarah.johansson@halcyonadvisory.example`, world `p036` |
| Disabled account | `paul.henderson@halcyonadvisory.example`, world `p046` |
| Executive Office group | `838b7bd4-aa69-4fc9-b4c2-981dd1304dc6` |
| Directory | 47 employees including 2 disabled, 21 active-client primary-contact guests, 17 groups |
| Outlook | 628 message copies, 510 folders, 982 calendar records including masters/occurrences, 640 contacts |

Guests use `#EXT#` UPNs and accepted invitation state. Department and All Staff groups are
Microsoft 365 groups; role groups are security groups; office distribution lists retain
their mail-enabled shape. Messages live in sender Sent Items and recipient Inbox/label
folders. Every internal employee has mailbox settings and a calendar. Contacts follow
client ownership/engagement membership and department colleague relationships.

## Subscriptions and webhooks

| Method | Graph path | Notes |
| --- | --- | --- |
| GET, POST | `/subscriptions` | List the caller's subscriptions or create one |
| GET, PATCH, DELETE | `/subscriptions/{id}` | Get, renew/update URL or remove |
| POST | `/subscriptions/{id}/reauthorize` | Reauthorization acknowledgement |

Supported resources include mailbox messages/folder messages/events/contacts, users, groups,
drive roots, channel/chat messages, callRecords and communications onlineMeeting artifacts.
Mailbox, drive and conversation access is checked when creating the subscription.
Creation POSTs a `validationToken` query parameter to notification and lifecycle URLs,
preserving existing query parameters. A subscriber must return HTTP 200, `text/plain`,
with exactly the decoded token. HTTPS URLs are required; in-process test transports can
serve those URLs without real TLS or external network access.

Basic Graph notifications have a `value` array containing subscription ID/expiry,
changeType, resource, resourceData (`@odata.type`, `@odata.id`, `@odata.etag`, `id`),
clientState and tenantId. Graph basic webhooks use clientState verification, **no HMAC
signature**. Resource writes publish through the shared change bus; webhook delivery
uses the hub's sync/async/manual dispatcher and delivery journal.
Outlook notification paths use Graph's `Users/{id}/Messages/{id}` casing (and Events/Contacts),
including notifications for folder-scoped subscriptions.

Expiration limits are Outlook 10,070 minutes (the review's verified service limit); directory
41,760; drive roots 42,300; Teams messages 4,320; callRecords/onlineMeeting 4,230.
Expiries below 45 minutes are increased to 45 minutes. Teams message subscriptions
longer than one hour require a lifecycle URL.
Duplicate subscriptions retain the documented 409 behavior for the same application,
resource and change types; see the review decisions below.

## Actions

All actions are POSTs under `/_emulator/actions/` (or `/microsoft/_emulator/actions/`).

| Action | Payload | Effect |
| --- | --- | --- |
| `mint_token` | `user`, `scopes`, `app_only`, `client_id`, `expires_in` | RS256 access token; also used by `/_emulator/tokens` |
| `approve_device_code` | `user_code`, `user` | Approve a pending device grant for a world user |
| `expire_subscription` | `id` or `subscription_id`, optional `lifecycleEvent` | Deliver `reauthorizationRequired` (default) or `subscriptionRemoved`; the latter removes the subscription |

Seeded Outlook invitations include the same Zoom series number/password and Google
Meet code as those providers' projections. Non-Teams join links live in the event
body; `onlineMeeting` remains null for those events. Cross-system calendar and
transcript traversal is covered by `tests/e2e/test_cross_system.py`.

## Known gaps

- Client registration/consent, Conditional Access, MFA, federation, external guest login,
  and full permission-scope consent enforcement are not modeled. Access checks enforce
  tenant admin, group ownership, mailbox identity and file/conversation visibility.
  Newly created synthetic directory users do not become world people or OAuth accounts.
- Passwords are synthetic; client assertion signatures are deliberately not trusted or
  validated against external certificates. Prompt/domain hints do not emulate UI policy.
- Directory soft-delete/recycle-bin recovery, arbitrary directory object types, license
  assignment writes, photo resizing/uploads and advanced Graph `$metadata` are absent.
- Mail delivery is local to internal recipients. External SMTP, transport rules, MIME
  fidelity beyond basic bodies/attachments, item/reference attachments and attachment
  upload sessions are absent. Only the default mailbox/calendar is seeded.
- Calendar suggestions return a simplified valid slot, without optimization. Complex
  recurrence exceptions, complete organizer/attendee message workflows, occurrence-level
  recurrence editing and Outlook cancellation/deleted-folder behavior are simplified.
  Calendar delta does not reproduce every recurring-series exception/tombstone rule.
- Encrypted rich notifications (`includeResourceData=true`), resource-specific delegated
  consent, notification batching/coalescing, automatic lifecycle scheduling, and
  production delivery latency are absent. Expiry suppresses further notifications;
  lifecycle delivery is explicitly simulated with the action.
- Delta cursors are retained for the lifetime of the local store. There is no production
  delta-token expiry/retention policy; reset clears them.
- Type/name validation for complex query properties is partial. UserInfo shares the
  provider's lenient permission-consent model; optional claims are not scope-filtered.

## Review decisions

The suspected duplicate-subscription change was checked against Microsoft's
[create-subscription reference](https://learn.microsoft.com/en-us/graph/api/subscription-post-subscriptions?view=graph-rest-1.0#duplicate-subscription-behavior),
which still specifies 409 for duplicates. That behavior is retained and regression tested.
Microsoft's [scheduleInformation reference](https://learn.microsoft.com/en-us/graph/api/resources/scheduleinformation?view=graph-rest-1.0)
explicitly maps workingElsewhere to `0` for backward compatibility, so the suggested `4`
mapping was not adopted. Other showAs statuses and missing-mailbox errors are fixed.
The subscription lifetime table currently lists 10,080 minutes for Outlook; this emulator
uses the brief's confirmed 10,070-minute boundary, with tests at and above the boundary.

## Reference and verification

Batch validation follows the official [JSON batching reference](https://learn.microsoft.com/en-us/graph/json-batching):
IDs are unique without regard to case, bodies require an explicit Content-Type, and
binary request bodies are base64url decoded before reaching the resource handler.
Header names and values are checked against
[HTTP field syntax](https://www.rfc-editor.org/rfc/rfc9110.html#section-5.5) before
dispatch; control characters and unencodable values receive Graph-shaped 400 errors,
while HTTP token punctuation, horizontal tabs, and Latin-1 field bytes are preserved.
The full JSON envelope rejects nonfinite numbers and invalid Unicode before writes.
URLs and JSON bodies are serialized during preflight; malformed Unicode or an
unbuildable URL receives a Graph-shaped 400, while valid Unicode paths/text work.
The entire batch structure and dependency graph are checked before dispatch, so a
malformed batch cannot leave an earlier write applied. Valid batches remain a set
of separate requests; individual failures do not roll back successful requests.
`tests/providers/microsoft/test_batch.py` covers those contracts and binary download
verification after a batch upload.

Wire shapes follow the official [users reference](https://learn.microsoft.com/en-us/graph/api/user-list?view=graph-rest-1.0),
[authorization-code protocol](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow),
[subscription reference](https://learn.microsoft.com/en-us/graph/api/resources/subscription?view=graph-rest-1.0),
and [webhook delivery protocol](https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks).

```powershell
uv run pytest tests/providers/microsoft -q
$env:EMULATORS_PROVIDERS = 'microsoft'
uv run pytest tests/sdk/test_microsoft_msal_sdk.py -q
```

The MSAL tests use the official library and real vendor hostnames through
`emulator_intercept`, covering client credentials, username/password with account-cache
refresh, and authorization-code flow with PKCE and nonce validation.
They also verify delegated `.default` consent, forced refresh to `User.Read`, and the
OIDC-discovered UserInfo endpoint. Review regressions live in `test_review_*.py` and
cover malformed payloads, projections, IDs, paging, directory writes and Outlook additions.
