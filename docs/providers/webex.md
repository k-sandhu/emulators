# Webex REST API v1

Provider name: `webex`. Dedicated port: **44114**. Hosts: `webexapis.com`,
`api.ciscospark.com`, and `*.webex.com`, including `halcyonadvisory.webex.com` and
`binaries.webex.com`. The gateway prefix is `/webex`. No Webex account is required.

```python
import httpx
from emulators.core.hub import EmulatorHub

hub = EmulatorHub(providers=["webex"], anchor="2026-10-16", webhook_mode="sync")
async with httpx.AsyncClient(transport=hub.async_transport()) as client:
    credentials = (await client.get("https://webexapis.com/_emulator/info")).json()["credentials"]
    response = await client.get(
        "https://webexapis.com/v1/meetings",
        params={"meetingType": "meeting", "from": "2026-09-01", "to": "2026-10-16"},
        headers={"Authorization": "Bearer " + credentials["admin_token"]},
    )
    response.raise_for_status()
    meetings = response.json()["items"]
```

## Authentication and permissions

All REST resources require `Authorization: Bearer <token>`. Temporary binary links
and avatar images are public capabilities; binary links expire after three hours.
The controllable hub clock governs OAuth and download expiration.

| Credential | Identity / behavior |
| --- | --- |
| `credentials.host_token` from `GET /_emulator/info` | Sofia Alvarez, `sofia.alvarez@halcyonadvisory.example`, world id `p005` |
| `credentials.admin_token` | Arjun Mehta, `arjun.mehta@halcyonadvisory.example`, world id `p035`; personal and admin scopes |
| `credentials.bot_token` | Halcyon Notes Bot, `halcyon-notes-bot@halcyonadvisory.example`; messaging scopes |
| `credentials.client_id` / `credentials.client_secret` | OAuth integration credentials from the same info response |

Seeded tokens are fictional fixtures with no expiration. Tokens minted at runtime
expire normally and are removed by reset. Clients are lenient by default; arbitrary
client ids/secrets work. `EmulatorHub(lenient_clients=False)` enables the shared
client registry's secret and redirect checks.

`GET /v1/authorize` accepts `response_type=code`, `client_id`, `redirect_uri`,
space-delimited `scope`, `state`, `login_hint`, `code_challenge`, and
`code_challenge_method` (`S256` or `plain`). An employee email or world id in
`login_hint` auto-approves; otherwise an account picker is returned. External
contacts, disabled users and bots cannot authorize integrations. The `inactive`
presence status is independent of account disablement and does not prevent login. Admin scopes
require an administrator identity.

`POST /v1/access_token` requires an `application/x-www-form-urlencoded` body. It
supports `authorization_code` and `refresh_token` grants, binds codes to the client
and redirect URI, verifies PKCE, and consumes authorization codes once. Refreshing
rotates the refresh token. Responses include `access_token`, `expires_in=1209600`,
`refresh_token`, `refresh_token_expires_in=7776000`, `token_type=Bearer`, and `scope`.

For setup, `POST /_emulator/tokens` accepts:

```json
{"user":"p005","scopes":["meeting:schedules_read","meeting:recordings_read","meeting:transcripts_read","spark:people_read"]}
```

The `user` may be a world id, Webex person id, or email. Omitting `scopes` gives a
delegated user `spark:all` and the personal meeting read/write scopes. An explicit
empty array grants no scopes. `{"app_only":true}` defaults to directory admin and
meeting admin scopes; an explicit scope list replaces these defaults. This is a
local service-app fixture, not an implementation of service-app provisioning.

Visibility is enforced independently of scope names:

- Delegated users can inspect meetings they host or are invited to. Only the host
  can update/delete a schedule, list attendance or import its recordings and transcripts.
- Full administrators with the corresponding `meeting:admin_*` scopes, and service
  apps with those scopes, can access organization meeting resources. A non-admin
  user cannot gain administrator privileges merely by minting an admin scope.
- A different `hostEmail` requires the resource's admin scope.
- Listing all people requires `spark-admin:people_read` and an administrator or
  service identity. Regular people tokens can search by email, display name or id.
- Rooms, messages and memberships require actual room membership; administrative
  meeting access does not bypass room membership. Locked-room changes and member
  promotion require a moderator. Bots see direct messages, their own group
  messages and group messages mentioning them. To list group messages, bots must
  supply `mentionedPeople=me` (or their own person id). All callers can filter
  mentions only for themselves; such lists cap `max` at 100.
- Service apps cannot impersonate `/people/me` or use personal messaging APIs.
- Directory responses omit presence across organization boundaries and when
  looking up comma-separated ids or specifying `excludeStatus=true`. Non-admin
  directory lookups omit other users' roles and licenses.

## Implemented endpoints

All paths below are relative to the relevant vendor host. Lists return
`{"items":[...]}`. Follow the RFC 5988 `Link: <url>; rel="next"` response header;
there is no cursor in the response body. Preserve the next URL as returned.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/v1/authorize` | Consent, login hint, state, PKCE and account picker |
| POST | `/v1/access_token` | Form-encoded code exchange and refresh |
| GET | `/v1/people/me` | Current person or bot |
| GET | `/v1/people` | `email`, display-name prefix, comma-separated `id` (up to 85), `excludeStatus`, managed `orgId`, admin `roles`, `max` |
| GET | `/v1/people/{id}` | Directory record with manager/managerId, roles, licenses and site URLs |
| GET | `/v1/organizations` | Halcyon organization; admin scope |
| GET | `/v1/roles` | Full administrator role; admin scope |
| GET | `/v1/licenses` | Meetings license capacity; admin scope |
| GET | `/avatars/{id}.png` | Lazily generated, cached PNG |
| GET | `/v1/meetings` | `meetingType` (default `meetingSeries`), `state`, `from`, `to`, `hostEmail`, `meetingSeriesId`, `siteUrl`, `max` |
| POST | `/v1/meetings` | Title, agenda, start/end, timezone, invitees, password, auto-record; admin hostEmail |
| GET | `/v1/meetings/{id}` | Series, scheduled meeting or ended instance |
| PUT, PATCH | `/v1/meetings/{id}` | Update series/schedule; PATCH `agenda:null` removes agenda; replace invitees when supplied |
| DELETE | `/v1/meetings/{id}` | Host/admin; series deletion removes schedules/invitees; ended instances are rejected; 204 |
| GET | `/v1/meetingInvitees` | Required `meetingId`; name/email/coHost; `max` |
| GET | `/v1/meetingParticipants` | Host/admin only; instance or parent `meetingId` (latest instance); attendance/device durations |
| GET | `/v1/recordings` | `from`, `to`, instance/scheduled/series `meetingId`, `hostEmail`, `siteUrl`, `format`, `topic`, `serviceType`, `integrationTag`, `status`, `max` |
| GET | `/v1/recordings/{id}` | Adds expiring `temporaryDirectDownloadLinks` |
| DELETE | `/v1/recordings/{id}` | Permanent deletion; subsequent detail/download returns 404; 204 |
| POST | `/v1/recordings/softDelete` | Moves 1–100 `recordingIds` into recycle bin; optional `siteUrl`; 204 |
| POST | `/v1/recordings/restore` | Restores `recordingIds` or `restoreAll:true`; optional `siteUrl`; 204 |
| POST | `/v1/recordings/purge` | Permanently removes deleted `recordingIds` or `purgeAll:true`; optional `siteUrl`; 204 |
| GET, HEAD | `/recordings/{id}/recording.{mp4,m4a,vtt}?token=...` | On binaries host; expiring capability, no auth; single byte ranges |
| GET | `/recording/{id}/{download,playback}` | Site-host HTML landing page; requires auth |
| GET | `/v1/meetingTranscripts` | Ended instance `meetingId` or `from`/`to`; `siteUrl`; `hostEmail` requires `meetingId`; `max` |
| GET | `/v1/meetingTranscripts/{id}/download` | `format=vtt` (default) or `txt`; requires bearer token |
| GET | `/v1/meetingTranscripts/{id}/snippets` | Text, speaker name/email, millisecond offset/duration; `max` |
| GET, POST | `/v1/rooms` | Membership list with `type`/`teamId` filters; group-room creation |
| GET, PUT, DELETE | `/v1/rooms/{id}` | Read, rename/lock, moderator delete; non-moderator DELETE leaves room; deletion cascades messages/memberships |
| GET, POST | `/v1/memberships` | `roomId`, `personId`, `personEmail`; add world person/bot |
| GET, PUT, DELETE | `/v1/memberships/{id}` | Read, change moderator status, leave/remove |
| GET | `/v1/messages` | Required `roomId`; `max`, `before`, `beforeMessage`, `parentId`, `mentionedPeople=me` or caller's id |
| POST | `/v1/messages` | Exactly one target; text/markdown (7439 UTF-8 bytes each), one file URL, parentId; personId/personEmail Markdown mentions |
| GET, DELETE | `/v1/messages/{id}` | Membership and author/moderator checks |
| GET, POST | `/v1/webhooks` | User/application-specific list/registration; requires read scope on the resource |
| GET, PUT, DELETE | `/v1/webhooks/{id}` | Same owner/application; PUT requires name/targetUrl; only `status=active` is accepted |

The default page size is 10 for meetings, attendance, recordings and transcript lists; 50 for
messages; 100 for directory, rooms, memberships, webhooks and snippets. Meeting,
recording, transcript, participant, invitee and snippet lists cap `max` at 100;
directory/messaging/webhook lists cap it at 1000. Invalid page sizes, cursors,
timestamps and JSON bodies return Webex error envelopes.

Meeting lists default to the next seven days for series/schedules and the previous
seven days for ended/ongoing instances. Specifying `meetingSeriesId` without a
`meetingType` returns its schedules and instances. Recording/transcript lists
default to the previous seven days; with only `from`, `to` defaults to `from+7 days`,
and with only `to`, `from` defaults to `to-7 days`. An explicit `meetingId` avoids
the default artifact date window. `from` is inclusive and `to` exclusive. A
recording's `meetingId` overrides `siteUrl`; transcript `meetingId` accepts only
ended instances. Explicit recording windows are not restricted to 30 days.

Scheduling validates the 128-character title and invitee display-name limits, 1300-character agenda,
1000-invitee limit, current/future start, IANA timezone and 10-minute to 23-hour
59-minute duration. Dates are normalized to minute precision. A start in the
current minute moves to the next minute; scheduling ten years or more ahead is
rejected. Failed writes validate before mutation.
JSON endpoints accept JSON media types and reject non-finite numbers, malformed
JSON, invalid UTF-8 strings and wrongly typed supported fields with vendor errors.

Every response includes `TrackingId`. Error bodies contain `message`,
`errors:[{"description":...}]`, and matching `trackingId`. Missing/invalid/expired
tokens return 401. Hidden resources return 404. Fault-injected 429s include
`Retry-After`; the standard journal and fault/reset/snapshot APIs are available.

## Seeded data

The seed projects all world people (133) plus Halcyon Notes Bot. The Halcyon
organization directory has 47 employees, including two departed employees, plus
the bot; external world contacts are searchable by email/id. The active employee
count is 45. Manager links, departments, timezones, fictional phone numbers,
administrator roles and licenses come from the shared world.

Meeting selection is the three-client projection from the brief: Northwind
Logistics, Atlas Freight Partners and Meridian Legal, with client meetings in
the last six weeks. Future world schedules for these clients are also included
as scheduling/action fixtures. For seed `emulators`, anchor `2026-10-16`, only
Meridian Legal has matching world meetings: **four completed instances, six
scheduled objects (including their historical schedules), two series, four
recordings, four transcripts, 31 invitees and 22 attendance records**. Northwind
and Atlas remain world clients; meetings are not fabricated for them.

| Resource | Concrete example |
| --- | --- |
| Sofia Alvarez / `p005` | `Y2lzY29zcGFyazovL3VzL1BFT1BMRS82M2VhYjBjMS02NWNhLTRlNjYtYTBiMS1kNmIxYzhlZDhjZGM` |
| Maya Chen / `p018` | `maya.chen@halcyonadvisory.example` (not invited to Meridian's seeded meetings) |
| Status update series | `ccdfd91a6f74a4b371729b13e1b027c5` |
| Status update instance, September 9 | `ccdfd91a6f74a4b371729b13e1b027c5_I_1788976800000` |
| Status update instance, September 23 | `ccdfd91a6f74a4b371729b13e1b027c5_I_1790186400000` |
| Status update instance, October 7 | `ccdfd91a6f74a4b371729b13e1b027c5_I_1791396000000` |
| Findings review, October 13 | `4cb8127b6440928deb8f46b6a3025b19_I_1791903600000` (host Daniel Kowalski, `p004`) |
| September 9 recording | `fed7e21d1a711b4078aa63be72232755` |
| Audit & Assurance General room | `Y2lzY29zcGFyazovL3VzL1JPT00vYmUzMDA4YWUtMTU1Yy00MmI0LWJhMjYtNDZhNTU0NzJmZmU4` |

Sofia hosts the three status updates; Daniel hosts the findings review. Use the
admin fixture with an explicit historical range or series id to import all four.
The non-recurring findings review is also modeled as a series with one dated
scheduled meeting. Ended instance responses omit join-only fields such as
password, meeting number, telephony, SIP address and auto-record preferences.

Seven rooms mirror the world's team General channels: Firmwide, Audit, Tax,
Advisory, Business Development, Partner Group and Bluefin Bookkeeping. They have
84 memberships and 143 world messages. Bots start with no room memberships;
send a direct message to the bot or add it to a room for bot tests.

Opaque ids decode to `ciscospark://us/<TYPE>/<uuid>` (base64url, no padding).
Meeting series, recordings and transcripts use 32 hexadecimal characters;
scheduled occurrences append `_YYYYMMDDTHHMMSSZ` to their series id, and ended
instances append `_I_<instance suffix>` to the series id. All meeting references
point to the same series/schedule/instance hierarchy. Timestamps use UTC ISO 8601
with milliseconds and `Z` by default.

Meeting, recording, transcript and attendance responses honor the optional IANA
`timezone` request header, rendering their timestamps with the selected UTC offset.
The scheduling `timezone` field remains the original meeting timezone. Invalid
timezone headers return 400; stored timestamps and capability expiry stay in UTC.

Binary bytes are generated on first download and cached in `ctx.store.blobs`.
MP4/M4A contain structurally valid silent AAC audio for the meeting duration.
Seeded VTT preserves world transcript timings and uses numbered cues followed by
`Name: text`; snippets preserve the same names, emails and times. TXT contains
one `Name: text` turn per line. A recording's `downloadUrl` is an HTML page; use
`temporaryDirectDownloadLinks.recordingDownloadLink` for MP4 bytes.
Recording `meetingId` accepts all three meeting-id forms; transcript lists require
an ended instance id and reject series or scheduled ids with 400.

## Webhooks and actions

Supported resources are `meetings`, `recordings`, `meetingTranscripts`,
`messages`, `memberships` and `rooms`. Events are `created`, `updated`, `deleted`
and `all`; meetings also accept `started` and `ended`. Mutations emit the events
they perform. Filters compare query-string key/value pairs to event fields;
meeting-related filters also resolve `hostEmail` and `hostUserId`. Host/owner
filters require `ownedBy=org`. Message filters support mention membership and
`hasFiles`/`hasAttachments`; boolean values use `true`/`false`.

Deliveries contain `id`, `name`, `targetUrl`, `resource`, `event`, `orgId`,
`createdBy`, `appId`, `ownedBy`, `status`, `actorId`, `data` and the registration's
`created` timestamp. Message notifications omit sensitive text/Markdown/file URLs;
room notifications omit titles. Fetch the resource using an authenticated GET.
Meeting events include `meetingId`/`hostPersonId`; artifact events include host
identity/email and the artifact's meeting references.
When a secret is configured, `X-Spark-Signature` is the lowercase hexadecimal
HMAC-SHA1 of the **exact request body bytes**, keyed with the UTF-8 secret.
No validation handshake is needed. Delivery uses the shared dispatcher with
sync, async/retry and manual/flush modes. Use `hub.webhook_transport` with an
ASGI or mock transport for an entirely local receiver.

Personal meeting subscriptions use `ownedBy=creator` and follow creator visibility.
`ownedBy=org` requires an administrator/service token with the relevant admin
meeting scope. Room subscriptions are restricted to current membership and bot
message visibility. Expiring the registration token does not expire its webhook;
a deleted/disabled creator is deactivated when an event is evaluated. Registrations
and deliveries are covered by snapshots/reset.

| Action | Payload | Result |
| --- | --- | --- |
| `POST /_emulator/actions/end_meeting` | `{"meeting_id":"<non-recurring series, scheduled or instance id>"}` | Ends the schedule, creates an ended instance, recording and transcript, emits `meetings.ended`, `recordings.created`, `meetingTranscripts.created` |
| `POST /_emulator/actions/mint_token` | Same fields as `/_emulator/tokens` | Resolves world/email/Webex ids and issues a scoped delegated/service token |

Ending an already-ended instance or the same schedule again is idempotent.
Ending a recurring series is rejected; a non-recurring series resolves to its
single schedule. New meetings without world transcript turns use
their agenda/title as host-attributed synthetic transcript content. A future
schedule ended by the action becomes a simulated one-minute meeting ending at
the hub clock. `EMULATORS_WEBEX_DOWNLOAD_TTL_SECONDS` overrides download TTL.

## Known gaps

- This covers the REST v1 subset in the endpoint table. Calling, devices, XML
  APIs, events/compliance endpoints, admin-specific `/v1/admin/recordings`, meeting
  summaries, closed captions, preferences, registrants and transcript editing are
  not implemented.
- OAuth service-app authorization/provisioning, device flows, OpenID Connect and
  token revocation endpoints are absent. Control-plane app-only tokens model
  scoped service access. Test tokens intentionally bypass real licensing/site
  enablement, policy and account approval workflows.
- Meeting recurrence is projected from world series but new recurring-series
  creation, recurring-series rescheduling (rejected with 400), recurrence edits,
  scheduling delegates, cohost management, breakout
  rooms and live call control are absent. PUT and PATCH both merge supported
  schedule fields. Historical scheduled objects remain inspectable. World client
  contacts are projected into separate synthetic organizations; federation policy
  is simplified. Only the listed query parameters are interpreted.
- Host/admin imports are implemented; shared recording grants, public recording
  passwords, compliance retention of purged recordings and recording edits are absent. Site
  playback/download pages are minimal authenticated HTML. MP4 has an audio track
  and no video track. Byte ranges support one range, not multipart ranges.
- Messaging supports JSON requests and stored external file URLs. It does not
  fetch external attachments, implement multipart uploads, serve file URLs, or
  render full Markdown/HTML/adaptive cards. Teams have stable ids on seeded rooms
  but no teams/team-membership API. Advanced moderation/announcement policy and
  organization-wide messaging compliance are absent. Team-space archival and
  protected General-space moderator rules are not modeled; team rooms otherwise
  use the group-room lifecycle described above.
- Webhook data omits sensitive messaging content; other resources can send
  smaller event-specific data in actual Webex. Only the mutations/actions above generate
  events; registering `meetings.started` does not start a meeting. Filter matching
  is equality-based. Service-app hook ownership uses the local client identity.
  Delivery-failure subscription deactivation and vendor-specific retry policies
  are not modeled beyond the shared dispatcher's retries. Attendance supports
  latest-instance parent resolution, but not its additional time/breakout filters.
- Pagination cursors are local opaque offsets, with the documented `Link`
  envelope and page bounds; behavior under concurrent insertions/deletions is
  not modeled. Throttling is driven through fault rules rather than live quotas.

## Verification and reference

```powershell
uv run pytest tests/providers/webex -q
$env:EMULATORS_PROVIDERS = 'webex'
uv run pytest tests/sdk/test_webex_http.py -q
uv run ruff check src/emulators/providers/webex tests/providers/webex tests/sdk/test_webex_http.py
uv run ruff format --check src/emulators/providers/webex tests/providers/webex tests/sdk/test_webex_http.py
```

The HTTP contract tests use `emulator_intercept` and unmodified real vendor URLs,
including an unauthenticated downloader on the binaries host. No Webex Python
SDK dependency is required or added.

Reference material: [Webex authentication](https://developer.webex.com/admin/docs/authentication),
[meeting model and state-specific attributes](https://developer.webex.com/meeting/docs/meetings),
[recording list filters](https://developer.webex.com/meeting/docs/api/v1/recordings/list-recordings),
[permanent recording deletion](https://developer.webex.com/meeting/docs/api/v1/recordings/delete-a-recording),
[transcript lists](https://developer.webex.com/meeting/docs/api/v1/meeting-transcripts/list-meeting-transcripts),
[attendance permissions](https://developer.webex.com/meeting/docs/api/v1/meeting-participants/list-meeting-participants),
[meeting resources](https://developer.webex.com/meeting/docs/api/guides/access-meeting-resources-guide),
[recording import workflow](https://developer.webex.com/blog/export-your-webex-meetings-for-ai-archives-and-automation),
[recording details](https://developer.webex.com/docs/api/v1/recordings/get-recording-details),
and [webhooks/signatures](https://developer.webex.com/messaging/docs/api/guides/webhooks).
