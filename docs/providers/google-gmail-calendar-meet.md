# Google Workspace: Gmail v1, Calendar v3, Meet v2

This is Google part B. Identity, Directory and Drive are described in
[google.md](google.md). All records project the shared Halcyon Advisory Group world.
Runtime requests never call Google. Webhooks only call configured test receivers.

## Hosts and routing

| API | Vendor base |
| --- | --- |
| Gmail | `https://gmail.googleapis.com/gmail/v1` and `https://www.googleapis.com/gmail/v1` |
| Gmail media/multipart upload | `https://www.googleapis.com/upload/gmail/v1` and the Gmail host |
| Calendar | `https://www.googleapis.com/calendar/v3` (also `calendar-json.googleapis.com`) |
| Meet | `https://meet.googleapis.com/v2` |

The gateway also accepts `/google/<API path>`, and Google has a dedicated listener
on port 44102. Generated Meet, Calendar and artifact links use the request's routing
mode. Static vendor image URLs are metadata and are never fetched by the emulator.

```python
from emulators.core.hub import EmulatorHub

hub = EmulatorHub(providers=["google"], anchor="2026-10-16", webhook_mode="sync")
# httpx.AsyncClient(transport=hub.async_transport()) can use real vendor URLs.
```

## Authentication and permissions

Part A supplies Google authorization-code/PKCE, refresh and service-account JWT bearer
flows. Test clients may use any client ID/secret with the default lenient OAuth registry.
For direct tests, POST `/_emulator/tokens` on a Google host:

```json
{"user":"p018","scopes":["https://mail.google.com/","https://www.googleapis.com/auth/calendar","https://www.googleapis.com/auth/meetings.space.created"]}
```

Use the returned `access_token` as a bearer token or `access_token` query parameter.
An empty scope list is deliberately lenient for control-plane setup; nonempty scope
lists are checked. Readonly grants cannot mutate resources, Gmail metadata grants
cannot search or retrieve bodies, and permanent Gmail deletion requires
`https://mail.google.com/`. Missing, invalid and expired credentials return 401.

Gmail requires an active internal user. `{userId}` is `me` or that user's email;
other mailboxes return 403. Unimpersonated app-only credentials return 403 across
these three APIs. A service-account subject acts as the impersonated user.

Calendar IDs are employee emails; `primary` resolves to the caller. Users own their
primary calendars and subscribe to colleagues in the same department as readers.
The CEO's executive assistant is a writer on the CEO's calendar. Other internal users
get domain free/busy access, without event details: event reads return 404 and
unauthorized writes return 403. Private event details are hidden from readers.
Secondary calendar creation, ownership and subscriptions are mutable.

Meet conference discovery and artifacts require the organizer or an invited attendee.
Spaces use the same membership, and only their owner may patch or end them. Newly
created spaces initially belong to their creator. Calendar-created Meet spaces also
include internal invitees.

## Seeded data and concrete examples

With seed `emulators` and anchor `2026-10-16`:

| World identity/resource | API example |
| --- | --- |
| `p018`, Maya Chen | `maya.chen@halcyonadvisory.example`; Gmail message `3659850f48e4ac3d` |
| `p001`, Eleanor Whitfield, CEO | `eleanor.whitfield@halcyonadvisory.example`; Calendar event `af95368e3e4e7767e5530737c4d5367b` |
| `p003`, Priya Raman, executive assistant | `priya.raman@halcyonadvisory.example`; writer on Eleanor's calendar |
| `m0005`, Granite Peak Outfitters kickoff | Organizer `p018`; starts `2026-09-09T14:00:00Z` |
| Meeting code for `m0005` | `npm-koju-usa` |
| Space for `m0005` | `spaces/1b-iJJK-ylF6` |
| Conference for `m0005` | `conferenceRecords/1lzuWlsoCsZeLQVBN66BvpJGWxzBhrSv09gZ` |
| Recording Drive file for `m0005` | `1kpbIYEYcLuqWcM5ycLWTHMzWTNChABT6` |

The projection contains 45 mailboxes and primary calendars, 628 mailbox message
copies, 216 Calendar event masters/single events and 766 seeded recurring instances.
There are 35 Meet spaces, 20 historical conferences, 15 recordings and 12 transcripts.
World records determine which past conferences actually have artifacts.

Every internal mail participant receives a mailbox copy with sender/recipient labels,
per-user unread and starred state, importance, categories and nested user-label names.
Seeded messages have stable 16-hex IDs and real RFC 2822 MIME. Attachments render lazily
to valid Office/PDF/etc. bytes and are cached in the content-addressed blob store.

Meet links, conference IDs, recording file IDs and transcript document IDs agree with
Calendar and Part A's Drive projection. Recording downloads use
`GET https://www.googleapis.com/drive/v3/files/{driveDestination.file}?alt=media`.

## Gmail endpoints

Paths below are relative to `/gmail/v1/users/{userId}`.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/profile` | Email, message/thread totals, current history ID |
| GET | `/messages` | `q`, repeated `labelIds`, `includeSpamTrash`, paging; newest first |
| GET | `/messages/{id}` | `format=full/metadata/minimal/raw`, repeated `metadataHeaders` |
| GET | `/messages/{id}/attachments/{attachmentId}` | Base64url bytes and size |
| POST | `/messages/send` | JSON `raw` or media/multipart upload; internal delivery and replies |
| POST | `/messages` | Official `messages.insert` path; mailbox-only insertion |
| POST | `/messages/insert`, `/messages/import` | Insert alias; import with `internalDateSource` |
| POST | `/messages/{id}/modify` | Add/remove existing labels, history records |
| POST | `/messages/batchModify`, `/messages/batchDelete` | Up to 1000 message IDs; 204 |
| POST | `/messages/{id}/trash`, `/messages/{id}/untrash` | TRASH label transitions |
| DELETE | `/messages/{id}` | Permanent deletion; 204 |
| GET | `/threads`, `/threads/{id}` | Search/paging; get expands mailbox messages and formats |
| POST | `/threads/{id}/modify`, `/threads/{id}/trash`, `/threads/{id}/untrash` | Apply to all current messages |
| DELETE | `/threads/{id}` | Permanent mailbox deletion; 204 |
| GET, POST | `/labels` | List system/user labels; create `Label_<n>` |
| GET, PATCH, PUT, DELETE | `/labels/{id}` | Get adds message/thread and unread counts; system labels cannot be deleted |
| GET, POST | `/drafts` | List/create; draft messages carry DRAFT |
| GET, PUT, DELETE | `/drafts/{id}` | Update replaces the draft's message ID |
| POST | `/drafts/send` | Send and remove draft; returns SENT message |
| GET | `/history` | Required `startHistoryId`, `historyTypes`, `labelId`, paging |
| POST | `/watch`, `/stop` | Pub/Sub watch registration and removal |
| GET | `/settings/sendAs`, `/settings/vacation` | Primary identity and disabled vacation response |
| GET, POST | `/settings/filters` | Criteria/action resources; incoming internal mail applies supported label actions |

Upload variants are `/upload/gmail/v1/users/{userId}/messages`, `/messages/send`,
`/messages/import`, `/drafts`, `/drafts/{id}` (PUT), and `/drafts/send`.
Media accepts `message/rfc822`; multipart accepts metadata plus RFC 2822 bytes.
Recipients in To/Cc/Bcc receive INBOX/UNREAD copies; Bcc addresses are removed from
delivered MIME. Unknown external recipients are accepted without external delivery.

Search supports `from:`, `to:`, `cc:`, `bcc:`, `subject:`, `label:`, `in:`
(inbox/sent/trash/spam/anywhere), `is:` (unread/read/starred/important),
`has:attachment`, `filename:`, `after:`, `before:`, `newer_than:`, `older_than:`,
`larger:`, `smaller:`, `size:`, `rfc822msgid:`, quoted phrases, implicit AND, OR,
negation, braces, parentheses and field groups such as `subject:(budget OR review)`.
Date-only boundaries use America/Los_Angeles, as Gmail documents. Default page size
is 100, maximum 500. Empty list responses omit resource arrays. Too-old history IDs
(below the seed floor 1000) return 404; every mutation advances the mailbox history.

## Calendar endpoints

Paths are relative to `/calendar/v3`.

| Method | Path | Notes |
| --- | --- | --- |
| GET, POST | `/users/me/calendarList` | Subscriptions, roles, preferences, paging |
| GET, PATCH, PUT, DELETE | `/users/me/calendarList/{calendarId}` | Subscription properties/removal |
| POST | `/calendars` | Create owned secondary calendar |
| GET, PATCH, PUT, DELETE | `/calendars/{calendarId}` | Metadata; primary calendars cannot be deleted |
| POST | `/calendars/{calendarId}/clear` | Cancel events, retaining sync tombstones |
| GET | `/calendars/{calendarId}/acl` | Owner-visible sharing rules |
| GET, POST | `/calendars/{calendarId}/events` | List/insert; query, time bounds, recurrence expansion, sync |
| GET, PATCH, PUT, DELETE | `/calendars/{calendarId}/events/{eventId}` | ETags, cancellation, invitation copies |
| GET | `/calendars/{calendarId}/events/{eventId}/instances` | Recurring instances and exceptions |
| POST | `/calendars/{calendarId}/events/{eventId}/move` | Change organizer to a writable destination calendar |
| POST | `/calendars/{calendarId}/events/import` | Required external `iCalUID` |
| POST | `/calendars/{calendarId}/events/quickAdd` | Summary with optional ISO date and HH:MM |
| POST | `/calendars/{calendarId}/events/watch` | Push channel and immediate sync notification |
| POST | `/channels/stop` | Owner/resource ID matching; 204 |
| POST | `/freeBusy` | Domain availability; merge overlapping/clipped busy intervals |
| GET | `/colors` | Calendar and event palettes |
| GET | `/users/me/settings`, `/users/me/settings/{setting}` | Timezone, locale and display defaults |

Events list defaults to 250 results, maximum 2500. `orderBy=startTime` requires
`singleEvents=true`. `timeMin` filters event end times and `timeMax` filters starts,
both exclusively. Final pages return `nextSyncToken`; intermediate pages only return
`nextPageToken`. Sync tokens are user/calendar bound, expire after seven clock days,
include cancellation tombstones, and reject incompatible query parameters. Invalid
or expired sync tokens return 410 with reason `fullSyncRequired`.

Seeded recurrence masters share iCalUIDs across invitees. Instances use
`<masterId>_<YYYYMMDDTHHMMSSZ>`, `recurringEventId` and `originalStartTime`.
Runtime weekly instances preserve local start time through DST. Calendar inserts with
`conferenceDataVersion=1` and `conferenceData.createRequest` create a usable Meet space
and successful conference data. Zoom and Teams meetings retain their respective links.

## Meet endpoints

Paths are relative to `/v2`. Meet errors use `{error:{code,message,status}}`.

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/spaces` | Create space with access/entry-point config |
| GET, PATCH | `/spaces/{idOrMeetingCode}` | Patch requires `updateMask`; config fields support camel/snake masks |
| POST | `/spaces/{id}:endActiveConference` | Owner-only active conference termination |
| GET | `/conferenceRecords`, `/conferenceRecords/{id}` | Authorized historical/live conferences |
| GET | `/conferenceRecords/{id}/participants`, `.../participants/{id}` | Internal signed-in users, external anonymous/phone participants |
| GET | `.../participants/{id}/participantSessions`, `.../participantSessions/{id}` | Joined/left sessions |
| GET | `/conferenceRecords/{id}/recordings`, `.../recordings/{id}` | STARTED or FILE_GENERATED; Drive destination when generated |
| GET | `/conferenceRecords/{id}/transcripts`, `.../transcripts/{id}` | Docs destination when generated |
| GET | `.../transcripts/{id}/entries`, `.../entries/{id}` | `transcriptEntries`, speaker participant, language and timestamps |

Conference filters support `space.name`, `space.meeting_code`, `start_time`, `end_time`,
quoted comparisons, AND/OR, parentheses and `end_time IS NULL`. Participants filter on
`earliest_start_time`/`latest_end_time`; sessions on `start_time`/`end_time`, including
active `IS NULL`. Filters are validated even when no resources are visible.
Conference pages default to 25/max 100; participants and sessions 100/max 250;
recordings/transcripts/entries 10/max 100. Conference and participant/session lists
are newest first; artifacts and transcript entries are ascending by start time.

## Webhooks and actions

All deliveries use `ctx.webhooks.deliver`, respecting sync/async/manual mode and
appearing in `/_emulator/webhooks`. In-process test receivers can use
`hub.webhook_transport = httpx.ASGITransport(app=receiver)`.

| Action | Payload | Effect |
| --- | --- | --- |
| `configure_pubsub_push` | `{topicName, endpoint}` | Map synthetic topic to test receiver |
| `start_conference` | `{meeting_id:"m0005"}` | Start a seeded Meet meeting, expose active space/conference/participants/sessions and STARTED artifacts |
| `end_conference` | `{meeting_id:"m0005"}` | End active sessions and generate FILE_GENERATED recording/transcript resources |

Gmail watch pushes use the Pub/Sub envelope: `message.data` is standard base64 JSON
containing `emailAddress` and string `historyId`; the envelope also has `messageId`,
`publishTime` and `subscription`. Label include/exclude filters and seven-day watch
expiration are enforced. Push simulation has no Google OIDC signature.

Calendar sends empty-body notifications with `X-Goog-Channel-ID`,
`X-Goog-Channel-Token` when configured, `X-Goog-Resource-ID`,
`X-Goog-Resource-URI`, `X-Goog-Resource-State` (`sync`/`exists`) and increasing
`X-Goog-Message-Number`. Google Calendar does not attach an HMAC signature.
Stopping a channel or an expired channel suppresses later delivery.

## Known gaps

- No resumable Gmail upload sessions, HTTP batch envelopes, external SMTP, spam/virus
  scanning, automatic importance classification or complete label-color validation.
  User filters support from/to/subject/query/negatedQuery/attachment/size matching and
  label changes; forwarding and full Gmail filter administration are absent. Search
  relative months/years use 30/365 days. Seed size estimates become exact on MIME read.
- Calendar ACL mutation, resource calendars, group expansion, calendarList incremental
  sync, nondefault event-type rules and outbound invitation email are absent.
  `sendUpdates` is accepted; internal invitation copies are replicated independently.
  Quick Add implements the documented subset above, not Google's natural-language engine.
- Recurrence accepts one DAILY/WEEKLY/MONTHLY/YEARLY RRULE with INTERVAL/COUNT/UNTIL,
  weekly BYDAY, monthly positive BYMONTHDAY and Monday WKST. Other recurrence forms
  return 400. Unbounded expansion without a timeMax uses a one-year horizon; scans are
  capped at 100 years. Seeded series cover the world's fixed scheduling horizon.
- Fine-grained application-created/owned-resource OAuth scope restrictions and Google
  add-on current-message bindings are not fully modelled beyond endpoint scope checks
  and user ACL enforcement. List continuation is not snapshot-isolated from concurrent
  writes. A test should avoid mutations midway through page traversal.
- Meet conferences retain deterministic seed history past Google's 30-day retention.
  Workspace Events subscriptions are not implemented. Live-action artifact metadata
  is generated immediately; Drive bytes exist for the seeded recording/transcript
  files supplied by Part A, not every newly simulated future artifact.
- Framework-injected Meet faults use the Google provider's shared legacy error body.
  Supply an explicit new-style fault `body` to test those failures. A route-aware core
  fault-error hook would remove this limitation without changing Part A's APIs.
- Google partial-response `fields` projection is not implemented for these APIs.

## Verification

```powershell
uv run pytest tests/providers/google -q
$env:EMULATORS_PROVIDERS='google'
uv run pytest tests/sdk/test_google_workspace_sdk.py tests/sdk/test_google_drive_sdk.py -q
```

The Workspace SDK tests use `google-api-python-client` with
`build("gmail", "v1", credentials=..., static_discovery=True)` and Calendar v3 through
`emulator_intercept`. Meet HTTP tests run the Parlance filter/artifact traversal and
download a real MP4 through Part A's Drive endpoint; only that download test skips if
the Drive module is absent.

Reference contracts: [Gmail messages](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages),
[Calendar events list](https://developers.google.com/workspace/calendar/api/v3/reference/events/list),
[Meet conference records](https://developers.google.com/workspace/meet/api/reference/rest/v2/conferenceRecords/list),
[Meet transcript entries](https://developers.google.com/workspace/meet/api/reference/rest/v2/conferenceRecords.transcripts.entries/list).
