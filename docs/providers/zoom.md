# Zoom emulator

OAuth (authorization code + PKCE, refresh rotation, Server-to-Server `account_credentials`, chatbot
`client_credentials`), REST API v2 (users, groups, meetings, past meetings, report and dashboard APIs,
cloud recordings, AI Companion meeting summaries), recording downloads with the real two-hop redirect,
and signed webhooks with the endpoint validation handshake. Provider name `zoom`, dedicated port `44106`.

Wire compatibility is the goal: a client that works against Zoom (for example one that lists
`/v2/users/me/recordings`, fetches `/v2/meetings/{uuid}/recordings` with a double-encoded UUID and downloads
the VTT/M4A/MP4 `download_url` with a Bearer header) works unchanged. `tests/sdk/test_zoom_http.py` runs that
exact flow with plain `httpx` and `requests` over real sockets.

The fidelity audit uses Zoom's published [Meetings OpenAPI](https://developers.zoom.us/api-hub/meetings/methods/endpoints.json),
[Users OpenAPI](https://developers.zoom.us/api-hub/users/methods/endpoints.json),
[Accounts/Dashboard OpenAPI](https://developers.zoom.us/api-hub/accounts/methods/endpoints.json),
[OAuth reference](https://developers.zoom.us/docs/integrations/oauth/), and
[webhook delivery rules](https://developers.zoom.us/docs/api/webhooks/). Request constraints are stored locally in
`request_meetings.py`, `request_users.py`, and `request_settings.py`; serving requests never fetches a schema.

## Hosts

| Host | Used for |
| --- | --- |
| `zoom.us` | `/oauth/authorize`, `/oauth/token`, `/oauth/revoke` |
| `api.zoom.us` | `/v2/...` REST API |
| `us06web.zoom.us` (any `*.zoom.us`) | `join_url`, `start_url`, `share_url`, `play_url`, `download_url` (`/rec/...`), avatars |
| `ssrweb.zoom.us` | the signed `https://ssrweb.zoom.us/file/...` the download URL redirects to |
| `marketplace.zoom.us`, `file.zoom.us` | accepted, served by the same app |

All hosts, plus `http://localhost:44080/zoom/...` (prefix mode) and the dedicated port, reach the same app.
Absolute URLs in responses follow the base the client used (`https://us06web.zoom.us/...` in host mode,
`http://localhost:44080/zoom/...` in prefix mode, including the `api_url` of token responses).
Webhook payloads have no request to follow, so they carry the canonical `https://us06web.zoom.us/...` URLs;
set `EMULATORS_ZOOM_PUBLIC_BASE=http://localhost:44106` to rewrite them for non-DNS setups.

Every response carries `x-zm-trackingid: v=2.0;clid=us;rid=WEB_<32 hex>` (errors, 404s, 204s, downloads
and injected faults included) and JSON as `application/json;charset=UTF-8`.

## Auth flows and test credentials

Clients are lenient by default: any `client_id`/`client_secret` is accepted. Find the synthetic account id and
test credential guidance at `GET /_emulator/info`. Use these local values instead of real credentials.

| Flow | Request | Notes |
| --- | --- | --- |
| Authorization code | `GET https://zoom.us/oauth/authorize?response_type=code&client_id=&redirect_uri=&state=&code_challenge=&code_challenge_method=S256&login_hint=<email>` | `login_hint` (email, world person id or Zoom id) auto-approves and `302`s to `redirect_uri?code=&state=`; without it a "choose an account" page lists every active employee (links add `login_hint`). Departed or unknown users get `error=access_denied` on the redirect. |
| Code exchange | `POST https://zoom.us/oauth/token` with HTTP Basic client auth, `grant_type=authorization_code&code=&redirect_uri=&code_verifier=` | PKCE verified when a challenge was sent; codes are single use. |
| Refresh | `grant_type=refresh_token&refresh_token=` | Refresh tokens **rotate**: the old one is dead after use (`{"reason":"Invalid Token!","error":"invalid_request"}`). |
| Server-to-Server | `POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=<id>` | Parameters may ride in the query string or a form body. No refresh token. Wrong `account_id` -> `Invalid Token!`. |
| Chatbot | `grant_type=client_credentials` | Scope `imchat:bot` only: every `/v2` call answers `4700` (no API scopes). |
| Revoke | `POST https://zoom.us/oauth/revoke` (Basic auth, `token=`) | `{"status":"success"}`; the token must belong to the authenticated client. A refresh token and its paired access token are revoked together. |
| Shortcut | `POST /_emulator/tokens {"user": "<id\|email>", "scopes": [...], "app_only": bool, "expires_in": s}` | Returns a Zoom-format token. No `user` (or `app_only`) = account-level app. Omit `scopes` for an unrestricted token. |

Token response: `{"access_token": "<issued token>", "token_type": "bearer", "refresh_token": ..., "expires_in": 3599,
"scope": "...", "api_url": "https://api.zoom.us"}`. Tokens are JWT-shaped (`{"sv":"000001","alg":"HS512",...}`; payload has
`uid`, `aid`, `cid`, `iat`, `exp`) but are validated by lookup, not signature. Token errors use
`{"reason": "...", "error": "..."}` (`invalid_client` is `401`, the rest `400`).

`POST /_emulator/actions/register_app {client_id, client_secret, redirect_uris, scopes, name}` pins an app's scopes and
redirect URIs (Zoom has no `scope` parameter on `/authorize`: scopes belong to the app). Unregistered clients get
`meeting:read:admin meeting:write:admin recording:read:admin recording:write:admin user:read:admin user:write:admin
report:read:admin group:read:admin group:write:admin dashboard_meetings:read:admin meeting_summary:read:admin`.
The authorize URL cannot expand those configured scopes. An explicitly empty registered scope list grants no API
access. Code exchange requires the original `redirect_uri`. Deleted or deactivated directory users cannot authorize,
redeem codes, or refresh, even if their underlying world person is still active.

### Identity, scopes and visibility

* **Delegated (user-level) tokens**: `me` is the signed-in employee. A user sees their own profile, settings,
  hosted meetings, past meetings and recordings. Anything about someone else, `GET /users`, groups, the report,
  dashboard and account-recordings APIs answer `403 {"code":200,"message":"No permission."}`, unless the user is an
  account admin **and** the token holds the `:admin` scope. Alternative hosts (the meeting's
  `settings.alternative_hosts`) may read the meeting but not edit, delete or fetch its past data.
* **Server-to-Server tokens** are account-level: they see the whole account when they hold `:admin` scopes. `me` is the
  account owner. A non-admin scope (`meeting:read`) on such a token gives no reach over other users.
* **Roles**: owner = Eleanor Whitfield (`role_id` `0`), admins = the world's IT admins (`role_id` `1`), everyone else
  Member (`2`).
* **Scope enforcement**: when a token carries scopes, both dialects are understood - classic (`meeting:read:admin`)
  and granular (`meeting:read:list_meetings:admin`). A missing scope is
  `401 {"code":4700,"message":"Invalid access token, does not contain scopes:[recording:read]."}` (classic apps see the classic
  name, `[x:admin]` for account-level endpoints; granular apps see `[meeting:read:meeting, meeting:read:meeting:admin]`).
  Tokens minted with `/_emulator/tokens` without scopes are unrestricted (identity rules still apply).
* Granular scopes are endpoint specific: for example `group:update:group:admin`, `group:write:member:admin`,
  `group:delete:member:admin`, `group:read:list_members:admin`, `user:update:email`, `user:update:status:admin`,
  `user:read:list_permissions`, `cloud_recording:read:recording_settings:admin`,
  `cloud_recording:update:recover_single_recording:admin`, and `report:read:user:admin`.
  Dashboard granular scopes use `dashboard:read:list_meetings:admin`, `dashboard:read:meeting:admin`, and
  `dashboard:read:list_meeting_participants:admin`.
* Deactivated or deleted users lose their tokens. Departed employees (`paul.henderson`, `yuki.tanaka`) are
  `status: "inactive"` and cannot sign in.
* API errors: `{"code": 124, "message": "Invalid access token."}` / `"Access token is expired."` (401),
  `{"code": 3001, "message": "Meeting does not exist: 123."}` (404), `{"code": 1001, "message": "User does not exist: x."}` (404),
  `{"code": 3301, "message": "This recording does not exist."}` (404), `{"code": 3003, "message": "You are not the meeting host."}` (403),
  `{"code": 300, "message": "Validation Failed.", "errors": [{"field": ..., "message": "Invalid field."}]}` / `Invalid parameter: x.` /
  `Request Body should be a valid JSON object.` (400), `429` with `Retry-After` and `X-RateLimit-*` via fault injection.

Pagination tokens are opaque, reusable for the same query and principal, expire after 15 minutes, and are invalidated
by reset. Changing filters, using another identity, or tampering with a token returns `400` / code `300`. The final
page returns `next_page_token: ""`. Nested write fields are validated before mutation, including strict JSON scalar
types, strings, arrays of members/registrants, settings, and recurrence. Invalid batches and meeting updates leave
the original resource unchanged. Simulation actions also validate inputs before changing state.

## Endpoints

IDs: `{userId}` is a 22-char user id, an email, or `me`. `{meetingId}` is 10-11 digits. Where the table says
**id or UUID**, the path value may also be a meeting UUID, single or double URL-encoded (see below).

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/v2/users` | `status` active\|inactive\|pending, `role_id`, `license`, `page_size` (30, max 300, silently capped), `next_page_token`, `page_number`, `include_fields=host_key`. Admin only. |
| POST | `/v2/users` | `action` create\|autoCreate\|custCreate\|ssoCreate, `user_info`. Admin; `201 {id, first_name, last_name, email, type}`; duplicate = `409 1005`. |
| GET | `/v2/users/email?email=` | `{existed_email}` |
| GET / PATCH / DELETE | `/v2/users/{userId}` | Full record (`role_name`, `personal_meeting_url`, `cms_user_id`, `jid`, `account_id`, `account_number`, `host_key`, `cluster` `us06`, `job_title`, `company`, `location`, `manager`). PATCH is `204`; `type` and group assignment require admins. DELETE `action=disassociate\|delete`, `204`, not the owner. Owned meetings/recordings leave the account; `transfer_email` with `transfer_meeting`/`transfer_recording` moves those assets to an active user. |
| PUT | `/v2/users/{userId}/status`, `/email` | activate / deactivate (revokes tokens); change email. |
| GET / PATCH | `/v2/users/{userId}/settings` | Subset (`schedule_meeting`, `in_meeting`, `email_notification`, `recording`, `telephony`, `feature`, `tsp`); PATCH overlays. |
| GET | `/v2/users/{userId}/token`, `/permissions` | zak token; role permissions. |
| GET / POST / PATCH / DELETE | `/v2/groups`, `/v2/groups/{id}` | Department, role and "All Staff" world groups. |
| GET / POST / DELETE | `/v2/groups/{id}/members`, `/members/{memberId}` | `group_ids` on users stay in sync. |
| GET | `/v2/users/{userId}/meetings` | `type` scheduled\|live\|upcoming\|upcoming_meetings\|previous_meetings, `from`/`to`, `timezone`, paging. Series appear once (type 8). Instant meetings are excluded; upcoming/previous are based on scheduled times, including meetings that were never started. |
| GET | `/v2/users/{userId}/upcoming_meetings` | One row per occurrence in the next 24 hours, including meetings hosted by the user and invitations from the world or `settings.meeting_invitees`. |
| POST | `/v2/users/{userId}/meetings` | `topic`, `type` 1\|2\|3\|8, `start_time` (`...Z` = GMT, bare = local in `timezone`), `duration` (1..1440), `password`, `default_password`, `agenda`, `recurrence`, `settings`, `schedule_for`. `201` with `start_url` (zak), `join_url ...?pwd=`, password fields, settings, occurrences. Recurrence supports up to 60 occurrences. Emits `meeting.created`. |
| GET | `/v2/meetings/{meetingId}` | `occurrence_id`, `show_previous_occurrences`. Occurrences default to the ones still to come. |
| PATCH / DELETE | `/v2/meetings/{meetingId}` | `204`; type updates persist; `occurrence_id` edits/deletes one occurrence (including agenda/settings overrides); emit `meeting.updated` (with `old_object`) / `meeting.deleted`. |
| PUT | `/v2/meetings/{meetingId}/status` | `end` (live meetings; records when `auto_recording` is `cloud`) or `recover` (deleted meeting). |
| GET | `/v2/meetings/{meetingId}/invitation` | `{invitation, sip_links}` in Zoom's invitation text format. |
| GET / POST / PUT / DELETE | `/v2/meetings/{meetingId}/registrants`, `/registrants/status`, `/registrants/{id}` | Needs `settings.approval_type` 0 or 1 (else `400 3000`). |
| GET | `/v2/past_meetings/{id or UUID}` | `{uuid, id, host_id, type, topic, user_name, user_email, start_time, end_time, duration, total_minutes, participants_count, dept, source}`. |
| GET | `/v2/past_meetings/{meetingId}/instances` | `{meetings: [{start_time, uuid}]}` |
| GET | `/v2/past_meetings/{id or UUID}/participants` | `{page_count, page_size, total_records, next_page_token, participants}`; guests have empty `user_id`/`user_email`. |
| GET | `/v2/report/meetings/{id or UUID}`, `/participants` | Adds `has_*` flags, `customer_key`, `attentiveness_score`; `include_fields=registrant_id`. Admin. |
| GET | `/v2/report/users/{userId}/meetings`, `/v2/report/daily`, `/v2/report/users` | `from`/`to` required (max 30 days) except `daily`. |
| GET | `/v2/metrics/meetings`, `/{id or UUID}`, `/{id or UUID}/participants` | Dashboard; `type` past\|pastOne\|live (default live, also enforced on detail/participants); `pastOne` selects one-user meetings. Lists accept dates or timestamps and `query_date_type`. Durations are `HH:MM:SS`. Admin. |
| GET | `/v2/users/{userId}/recordings` | `from`/`to` `YYYY-MM-DD` (default today..tomorrow per the current reference, max 30 days else `400 300`; empty values mean "not given"), `page_size`, `next_page_token`, `trash`, `trash_type`, `meeting_id`, `mc`. Trash ignores date filters. Newest first. |
| GET | `/v2/accounts/{accountId}/recordings` | Account-wide (`me` or the account id). Admin. |
| GET | `/v2/meetings/{id or UUID}/recordings` | `include_fields=download_access_token`, `ttl` (max 604800). A meeting id returns its latest recorded instance. |
| DELETE | `/v2/meetings/{id or UUID}/recordings`, `/recordings/{recordingId}` | `action=trash` (default) \| `delete`; `204`. Permanently deleting a trashed recording or file is supported. |
| PUT | `/v2/meetings/{id or UUID}/recordings/status`, `/recordings/{recordingId}/status` | `{"action":"recover"}`. |
| GET / PATCH | `/v2/meetings/{id or UUID}/recordings/settings` | `share_recording`, `viewer_download`, `password`, `topic`, `on_demand`, `auto_delete`, ...; password/topic edits stay consistent with the recordings object. |
| GET | `/v2/meetings/{id or UUID}/meeting_summary` | AI Companion summary built from the transcript (404 when untranscribed). |
| GET | `/v2/meetings/meeting_summaries` | Account list; `from`/`to`, `host_id`. Admin. |
| GET | `https://us06web.zoom.us/rec/download/<token>` | `Authorization: Bearer` or `?access_token=` (OAuth or download token); `302` to ssrweb. |
| GET / HEAD | `https://ssrweb.zoom.us/file/<fileId>/<name>?Expires&Signature&Key-Pair-Id` | Streams bytes: `Content-Type`, `Content-Length`, `Accept-Ranges`, `Range` (206/416). |
| GET | `/rec/play/<token>`, `/rec/share/<token>`, `/j/<id>`, `/s/<id>`, `/w/<id>`, `/p/v2/<id>` | Minimal HTML pages and a PNG avatar so the URLs in responses resolve. |

### UUIDs in paths

Meeting UUIDs are 24-character base64 strings ending in `==` and may contain `/` and `+`. The seed forces awkward
shapes on four recorded instances: `/so8orPT901tqlutcTMBrg==` and `/LR0CSUlTXK8BbVpvEfjjA==` start with `/`;
`uA//JbX+yDbGxWcCUqecRg==` and `CVm+imKaKdBfi/r//YlgQg==` contain `//` (unforced UUIDs never do). As on Zoom,
send those double-URL-encoded (`%252Fso8or...%253D%253D`). The emulator decodes until no `%` is left, so
single-encoded and unencoded forms also work wherever a UUID is accepted.

## Seeded data

* **Users**: 47 (45 `active`, `paul.henderson@` and `yuki.tanaka@` `inactive`), ids derived from the world: e.g.
  `eleanor.whitfield@halcyonadvisory.example` = `ZhXFydzDVOWXYmMwOXbNkQ` (Owner), `arjun.mehta@` = `83AwRK8ca-8yACV063o8HA`
  and `sarah.johansson@` = `Diwjg8Oz8B35idknIQ7Y2w` (Admins), `benjamin.adler@` = `RhkoU4l7EIIxoEMWgC6wrw`,
  `maya.chen@` = `sxFklUMUbBqquw0ahzMA0g`. Type 2 (Licensed) for most; type 1 (Basic) for three staff (e.g.
  `rajesh.iyer@`); meeting hosts are always Licensed. 13 groups: the 8 departments, Leadership Team, Partners,
  People Managers, IT Administrators, All Staff (`group_ids` on every user). `employee_unique_id` is the world employee
  id (`HAG-1019`), `dept`/`job_title`/`manager` come from the org chart.
* **Meetings**: the world's Zoom meetings (14 Zoom meetings, 26 held instances, 20 recorded). Recurring series are one
  type-8 meeting with `occurrences`; the cancelled occurrence is `status: "deleted"`.

  | Meeting id | Topic | Host | Type | Occurrences | Held | Recorded |
  | --- | --- | --- | --- | --- | --- | --- |
  | 6448198817 | Bluefin Seafood - Status Update | benjamin.adler | 8 (biweekly) | 5 | 3 | 2 |
  | 27717228648 | Harborview Property Management - Status Update | benjamin.adler | 8 | 5 | 3 | 3 |
  | 32310423666 | Riverbend Veterinary Clinics - Status Update | benjamin.adler | 8 | 5 | 3 | 2 |
  | 4442862647 | Orchid Hospitality Group - Status Update | benjamin.adler | 8 | 5 | 3 | 3 |
  | 7061217323 | Brightwater Health Clinics - Status Update | sofia.alvarez | 8 | 5 | 3 | 3 |
  | 98053790901 | Starling Media Group - Status Update | benjamin.adler | 8 | 5 | 3 | 1 |
  | 3507027577 | Advisory Deal Review | maya.chen | 8 (weekly) | 9 | 6 | 4 |
  | 38088950285 / 7569509647 | Riverbend / Orchid - Findings Review | maya.chen | 2 | 1 | 1 | 1 each |
  | 6048581623, 24346828773, 4106704630, 6844811235 | Brightwater, Bluefin, Harborview, Starling - Findings Review | daniel.kowalski / maya.chen | 2 | 1 | 0 | 0 |
  | 5149343055 | Intro: Willowbrook Senior Living x Halcyon | natalie.cho | 2 | 1 | 0 | 0 |

  Each meeting has a numeric `password` (`h323_password`/`pstn_password` the same), `encrypted_password` (the `pwd=` token),
  a zak `start_url`, `settings` (waiting room and host video for client meetings, `auto_recording: "cloud"` where a
  recording exists, `alternative_hosts` = the second team member).
* **Past meetings**: every held instance has `participants` from the world (internal staff with 16-digit `user_id` and
  email; client contacts are guests with empty `user_id` and `user_email`, as Zoom reports participants outside the host's
  account; set `EMULATORS_ZOOM_GUEST_EMAILS=1` to expose their emails). Start/end times are the first join and last
  leave, so they differ slightly from the schedule.
* **Cloud recordings**: 20, one per recorded instance, newest `2026-10-15` (Advisory Deal Review). Files: `MP4`
  (`shared_screen_with_speaker_view`), `M4A` (`audio_only`), `TRANSCRIPT` VTT (`audio_transcript`) and `TIMELINE` JSON for
  transcribed meetings, `CHAT` TXT (`chat_file`) when the meeting had chat. The MP4/M4A are valid ISO-BMFF with AAC audio
  (silent; no video track), the VTT is Zoom style (`1`, `00:00:41.410 --> ...`, `Benjamin Adler: text`), chat lines are
  `HH:MM:SS<TAB> From  Name to  Everyone:<TAB>text`. Times in transcripts/chat count from `recording_start`.
  Recording REST responses use a string `type` (e.g. `"8"`); recording webhook objects use a numeric `type` (`8`).
  Invitation dial-in numbers use fictional NANP 555-01xx numbers.

## Webhooks

Configure local subscriptions through this action:

```
POST /_emulator/actions/configure_webhook
{"url": "http://consumer.test/zoom/hook", "secret_token": "...", "events": ["meeting.ended", "recording.completed"]}
```

`events` defaults to all of `meeting.created|updated|deleted|started|ended|participant_joined|participant_left`,
`recording.completed|transcript_completed|trashed|deleted|recovered`, `user.created|updated|deleted|activated|deactivated`.
Before saving, the emulator sends `{"payload":{"plainToken":...},"event":"endpoint.url_validation","event_ts":...}` and
requires `{"plainToken", "encryptedToken": hex(hmac_sha256(secret_token, plainToken))}`; a failing handshake returns
`400 endpoint_validation_failed` and nothing is saved (`skip_validation: true` bypasses it). Re-posting a URL replaces its
subscription; `remove_webhook {id|url}` deletes. `verification_token` (legacy) is sent verbatim in an `authorization`
header; deliveries also carry `x-zm-trackingid` and `User-Agent: Zoom Marketplace/1.0a`.

Deliveries are `POST` JSON `{"event", "event_ts" (ms), "payload": {"account_id", "operator"?, "operator_id"?, "object": {...}}}`
with headers `x-zm-request-timestamp` (unix seconds) and `x-zm-signature: v0=<hex hmac_sha256(secret_token, "v0:{ts}:{body}")>`
(recomputed on every retry). Server/transport failures receive up to three retries after the initial attempt;
redirects and 4xx responses are terminal. The response timeout is three seconds. The body is compact UTF-8 JSON without ASCII escaping, so a consumer that
re-serialises the parsed JSON (`JSON.stringify`) verifies the same signature. Payloads follow Zoom's shapes: ids are
integers in `meeting.created/updated/deleted` and strings in `meeting.started/ended/participant_*`; `meeting.updated` carries
`old_object` and `time_stamp`; `recording.completed` carries the meeting-recordings object (`recording_files` with
`download_url`, `share_url`, `password`, `participant_audio_files`) and a top-level **`download_token`**, which authorises
that meeting's `download_url`s as Bearer or `?access_token=` for 24 hours.

API writes emit events immediately: `POST/PATCH/DELETE` meetings, recordings trash/delete/recover, user create/update/delete.

## Actions (`POST /_emulator/actions/<name>`)

| Action | Payload | Effect |
| --- | --- | --- |
| `configure_webhook`, `remove_webhook` | see above | Manage subscriptions. |
| `register_app` | `{client_id, client_secret?, redirect_uris?, scopes?, name?, account_id?}` | Pin an OAuth app's scopes/redirects. |
| `mint_token` | `{user?, scopes?, app_only?, expires_in?}` | Same as `/_emulator/tokens`, Zoom token format. |
| `start_meeting` | `{meeting_id (or uuid), participants?, occurrence_id?}` | Live instance for the next scheduled occurrence (own UUID), `meeting.started`, then `meeting.participant_joined` for the host and each participant (person id / email / name / `{user, name, join_offset_minutes, stay_minutes}`; `"all"` = invited attendees). `GET /meetings/{id}` shows `status: "started"`; it appears under `type=live` and in `/metrics/meetings?type=live`. |
| `participant_join` / `participant_leave` | `{meeting_id, user\|email\|name \| participant_id}` | One more join/leave with the event. Unknown people join as guests. |
| `end_meeting` | `{meeting_id, record?=true, transcribe?=true, duration_minutes?, chat?}` | `participant_left` for everyone still in, `meeting.ended`, creates the past meeting and participants and, with `record`, generates the cloud recording and emits `recording.completed` then `recording.transcript_completed`. `duration_minutes` defaults to the scheduled duration (simulated: nothing waits). `chat` = `[{from, text, offset_s}]`. |
| `replay_event` | `{event, meeting_id \| uuid}` | Re-send `meeting.started/ended/participant_*` or `recording.completed/transcript_completed/trashed/recovered` for any existing instance (e.g. the seeded recordings). |

Also: `PUT /meetings/{id}/status {"action":"end"}` ends a live meeting through the API, recording when
`auto_recording` is `cloud`.

## Settings (environment)

* `EMULATORS_ZOOM_GUEST_EMAILS=1` - participants outside the account expose their email.
* `EMULATORS_ZOOM_PUBLIC_BASE=<base>` - URL base used inside webhook payloads.

## Known gaps

* Not implemented: webinars, Zoom Phone/Rooms/Team Chat, tracking-field definitions, meeting polls/templates/livestream,
  recording registrants and analytics, `GET /past_meetings/{id}/polls`, Dashboard QoS endpoints, SCIM, the legacy JWT
  app flow, `user.settings_updated` and `app_deauthorized` events, `device_code` grant, and the Marketplace event
  subscription REST API (use `configure_webhook`).
* Participants are not tracked through breakouts/waiting rooms; `status` is always `in_meeting`.
* Recordings contain audio only (no H.264 track) and silent AAC; the MP4 is a valid container that ffprobe/mutagen read.
* `from`/`to` defaults on recording, summary and dashboard lists use the hub clock (`/_emulator/clock` freezes it).
* Several error codes beyond the documented ones (`3002`, `3322`, `4130`, `4132`, `3043`) are plausible stand-ins; HTTP
  statuses and envelopes are Zoom's, exact messages for rarely hit failures may differ.
* Users created through `POST /v2/users` exist in the Zoom directory only; they cannot hold delegated tokens because the
  world has no matching person.
* `GET /meetings/{id}` accepts meeting ids only (UUID lookups belong to past meetings and recordings).
* Rate limiting is only simulated through fault injection (`POST /_emulator/faults {"status":429}`), not enforced.
* Webhook retry delays are shortened for tests; production uses 5, 20 and 60 minutes. Sync/manual mode retries
  immediately. URL validation is run when configuring a subscription, without periodic revalidation.
* Revocation pairs access/refresh tokens conservatively; full app deauthorization and cross-refresh-family revocation
  are not modeled. JWT-shaped tokens are opaque emulator grants, not cryptographically verifiable Zoom tokens.
* Master-account scopes, account-locked passcode policies, meeting expiry/retention windows, six-month listing caps,
  scheduling privileges, and full settings/license policy enforcement are not modeled. User settings remain a subset;
  unsupported request fields may be ignored. The 30-day recording range remains the original brief's approximation
  of Zoom's documented one-month range. Default recording dates follow the current reference (today..tomorrow),
  superseding the original brief's yesterday..today default.
