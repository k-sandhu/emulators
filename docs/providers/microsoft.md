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

## Directory endpoints

| Method | Graph path | Notes |
| --- | --- | --- |
| GET | `/me`, `/users`, `/users/{id-or-upn}` | Default 11 properties including ID; extended fields with `$select` |
| POST, PATCH, DELETE | `/users`, `/users/{id-or-upn}` | Create, update, delete; app or world tenant admin required |
| GET | `/users/delta`, `/groups/delta` | Snapshot pages, opaque resource-bound cursors, changed entities and deletion markers |
| GET | `/organization`, `/domains`, `/subscribedSkus` | Verified domains, licenses and service plans |
| GET | `/users/{id}/manager`, `/directReports`, `/memberOf`, `/transitiveMemberOf`, `/ownedObjects` | Also under `/me`; manager expansions support `$select` and `$levels=max` |
| GET | `/users/{id}/photo`, `/photo/$value`, `/photos`, `/photos/{size}`, `/photos/{size}/$value` | Also under `/me`; generated 64px PNG or `ImageNotFound` |
| POST | `/users/{id}/checkMemberGroups`, `/getMemberGroups` | Group ID collections |
| POST | `/directoryObjects/getByIds` | ID and optional type filtering |
| GET | `/directoryObjects/{id}` | User or group with directory object type annotation |
| GET | `/groups`, `/groups/{id}`, `/groups/{id}/members`, `/owners`, `/transitiveMembers` | World group types, membership and owners |
| POST | `/groups/{id}/members/$ref` | Owner or tenant admin; `@odata.id` body |
| DELETE | `/groups/{id}/members/{id}/$ref` | Owner or tenant admin |
| POST | `/$batch` | Up to 20 requests; dependencies, failure propagation, same-app dispatch |

Directory collections default to 100 records, capped at 999. OData filtering, ordering,
selection, searching and skip-token paging use the shared OData engine. `$search` and
`$count=true` on directory collections require `ConsistencyLevel: eventual`. Guest users
cannot enumerate users. User/group writes enforce world administrator or group-owner roles.
Every Graph response includes `request-id` and `client-request-id`, including failures
and injected faults. The latter echoes the caller's header when supplied.

## Outlook endpoints

In the table, `{mailbox}` is `/me` or `/users/{id-or-upn}`. Delegated access is restricted
to the caller's mailbox; app tokens may address any internal mailbox. Private contacts
and calendar data use the same ownership check.

| Method | Graph path | Notes |
| --- | --- | --- |
| GET | `{mailbox}/mailFolders`, `/mailFolders/{id}`, `/mailFolders/{id}/childFolders` | Well-known names and nested folders; computed item/unread/child counts |
| GET, POST | `{mailbox}/messages`, `/mailFolders/{id}/messages` | List or create draft; list default 10, maximum 1000 |
| GET | `{mailbox}/messages/{id}`, `/messages/{id}/$value` | Message JSON or RFC 822 MIME; folder-qualified GET aliases also supported |
| GET | `{mailbox}/messages/{id}/attachments`, `/attachments/{id}`, `/attachments/{id}/$value` | File attachment metadata, base64 content or bytes; folder-qualified aliases |
| PATCH, DELETE | `{mailbox}/messages/{id}` | Read/flag/category updates; drafts also editable; delete moves to Deleted Items |
| POST | `{mailbox}/sendMail` | JSON message or base64 MIME; internal delivery; `saveToSentItems` |
| POST | `{mailbox}/messages/{id}/send`, `/createReply`, `/reply`, `/replyAll`, `/forward`, `/move`, `/copy` | Correct action status codes, per-mailbox copies and new move/copy IDs |
| GET | `{mailbox}/mailFolders/{id}/messages/delta` | Folder-specific delta; move/delete tombstones |
| GET, PATCH | `{mailbox}/mailboxSettings` | Time zone, working hours and automatic replies |
| GET | `{mailbox}/calendars`, `/calendar`, `/calendars/{id}`, `/calendarGroups`, `/calendarGroups/{id}/calendars` | Default calendar and calendar group |
| GET, POST | `{mailbox}/events`, `/calendar/events`, `/calendars/{id}/events` | Single instances and series masters; create events and internal invitations |
| GET | `{mailbox}/calendarView`, `/calendar/calendarView`, `/calendars/{id}/calendarView` | Both date range parameters required; occurrences expanded |
| GET, PATCH, DELETE | `{mailbox}/events/{id}` | Calendar-qualified aliases; transactional event creation |
| GET | `{mailbox}/events/{id}/instances` | Seeded and runtime recurrence expansion, date range required |
| POST | `{mailbox}/events/{id}/accept`, `/decline`, `/tentativelyAccept`, `/cancel`, `/forward` | Attendee response state and organizer checks |
| POST | `{mailbox}/calendar/getSchedule` | Availability views and schedule items |
| POST | `{mailbox}/findMeetingTimes` | Simplified suggestion response |
| GET | `{mailbox}/events/delta`, `/calendarView/delta` | Delta links and changed events |
| GET, POST | `{mailbox}/contacts`, `/contactFolders/{id}/contacts` | Private relationship and colleague contacts; list/create |
| GET, PATCH, DELETE | `{mailbox}/contacts/{id}`, `/contactFolders/{id}/contacts/{id}` | Contact CRUD |
| GET, POST | `{mailbox}/contactFolders`, `/contactFolders/{id}/childFolders` | Folder hierarchy and creation |
| GET | `{mailbox}/contactFolders/{id}` | Folder metadata |

Mail supports `$search` terms, sender-qualified searches, read/attachment/date/sender filters,
and `Prefer: outlook.body-content-type="text"`. Attachment documents are rendered on demand
and cached in the blob store; uploaded attachment bytes also use the blob store.
Well-known folders include `inbox`, `drafts`, `sentitems`, `deleteditems`, `junkemail`,
`archive`, `outbox`, `conversationhistory` and `msgfolderroot`.

Calendar dates use seven fractional digits. `Prefer: outlook.timezone="Pacific Standard Time"`
and the world's Windows/IANA zones are supported. Teams events share join URLs with
onlineMeeting resources; Zoom/Meet links are included in event bodies. Runtime recurrence
supports daily, weekly, absolute/relative monthly and yearly patterns, preserving local
wall time across DST. CalendarView pagination preserves the original request path, query,
and routing prefix.

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

Supported resources include mailbox messages/folder messages/events, users, groups,
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

Expiration limits follow the public resource table: Outlook 10,080 minutes; directory
41,760; drive roots 42,300; Teams messages 4,320; callRecords/onlineMeeting 4,230.
Expiries below 45 minutes are increased to 45 minutes. Teams message subscriptions
longer than one hour require a lifecycle URL.

## Actions

All actions are POSTs under `/_emulator/actions/` (or `/microsoft/_emulator/actions/`).

| Action | Payload | Effect |
| --- | --- | --- |
| `mint_token` | `user`, `scopes`, `app_only`, `client_id`, `expires_in` | RS256 access token; also used by `/_emulator/tokens` |
| `approve_device_code` | `user_code`, `user` | Approve a pending device grant for a world user |
| `expire_subscription` | `id` or `subscription_id`, optional `lifecycleEvent` | Deliver `reauthorizationRequired` (default) or `subscriptionRemoved`; the latter removes the subscription |

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

## Reference and verification

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
