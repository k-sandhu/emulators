# Slack: Web API, OAuth v2, Events API, incoming webhooks, files

One Slack workspace, *Halcyon Advisory Group* (`halcyonadvisory.slack.com`), projected from the
shared world: every active employee is a member, the world's team channels are Slack channels,
world chats are DMs and group DMs, and department groups are user groups. Runtime requests
never call Slack; the only outbound traffic is Events API / interactivity deliveries to the
URLs a test configures.

## Hosts and routing

| Host | What it serves |
| --- | --- |
| `slack.com`, `api.slack.com` | `/api/<method>` (Web API), `/oauth/v2/authorize`, `/openid/connect/authorize`, `/.well-known/openid-configuration`, `/openid/connect/keys` |
| `halcyonadvisory.slack.com` (`*.slack.com`) | workspace pages: `/` sign-in stub, `/archives/<C>/p<ts>` permalinks, `/files/<U>/<F>/<name>` |
| `files.slack.com` | `/files-pri/<T>-<F>/<name>` (+ `/download/<name>`), `/files-tmb/...`, `/upload/v1/<token>` |
| `hooks.slack.com` | incoming webhooks `/services/T/B/<secret>`; `response_url`s `/commands/...` and `/actions/...` |

The same app answers every host. With the gateway it is also reachable at
`http://localhost:44080/slack/...` (dedicated port 44107). Absolute URLs the emulator generates
(file URLs, `upload_url`, webhook URLs) follow the routing mode, so they work for real hostnames
and for prefix mode. Workspace URLs (`auth.test.url`, permalinks, OAuth configuration links)
use `https://halcyonadvisory.slack.com/...` in host mode and the local base in prefix mode.
Incoming webhook URLs returned by `/_emulator/info` follow the same routing rules.

```python
import httpx
from slack_sdk import WebClient
from emulators.testing.server import EmulatorServer
from emulators.core.hub import EmulatorHub

hub = EmulatorHub(providers=["slack"], anchor="2026-10-16", webhook_mode="sync")
with EmulatorServer(hub) as server:
    info = httpx.get(server.url_for("slack") + "/_emulator/info").json()
    client = WebClient(token=info["credentials"]["tokens"]["bot"], base_url=server.url_for("slack") + "/api/")
    client.chat_postMessage(channel="#general", text="hello")
```

With `emulator_intercept` the SDK's default `https://slack.com/api/` works unchanged
(`slack_sdk` sync and async clients, `requests`, `httpx`).

## Authentication, tokens and scopes

Every method needs a token (`Authorization: Bearer <token>`, or a `token` form/query parameter;
JSON bodies require the header). Missing token is `not_authed`; unknown is `invalid_auth`;
revoked `token_revoked`; expired (rotation) `token_expired`; deactivated user `account_inactive`.
`missing_scope` carries `needed` and `provided`. Authenticated responses have `X-OAuth-Scopes`;
methods declaring static scopes have `X-Accepted-OAuth-Scopes`. Web API responses, including
injected rate limits, carry `X-Slack-Req-Id`. Token kinds: bot, user, and app-level;
methods a kind cannot call return `not_allowed_token_type` (e.g. `search.messages` with a bot).

Seeded tokens (deterministic, also in `GET /_emulator/info` under `credentials.tokens`):

| Label | Token | Acts as |
| --- | --- | --- |
| `bot` | `xoxb-…` (see info) | Halcyon Bot (`UED5AF609AC`), typical scopes (no `chat:write.public`/`.customize`) |
| `bot_full` | `xoxb-…` (see info) | Halcyon Bot with `chat:write.public`, `chat:write.customize`, `links:*` |
| `bot_readonly` | `xoxb-…` (see info) | Halcyon Bot with only `channels:read groups:read users:read team:read` |
| `app_level` | `xapp-…` (see info) | app-level token (`connections:write`...) |
| `user:p001` | `xoxp-…` (see info) | Eleanor Whitfield (primary owner) |
| `user:p035` | `xoxp-…` (see info) | Arjun Mehta (workspace admin) |
| `user:p018` | `xoxp-…` (see info) | Maya Chen (regular member) |
| `user:p046` | `xoxp-…` (see info) | Paul Henderson (departed: `account_inactive`) |
| `user:c002-1` | `xoxp-…` (see info) | Stephanie Pearson (multi-channel guest) |

Token values are deterministic per seed but are not printed here (they have the exact shape of real Slack secrets); read them from `GET /_emulator/info` → `credentials`. Other credentials: team `T07CC5F644F`, app
`A0D70F73363`, client id `661099083756.1541543804193`, client secret (see info), signing secret (see info),
verification token (see info).

`POST /_emulator/tokens` (overridden): `{"user": "p018|email|U...", "type": "bot|user|app",
"scopes": [...], "expires_in": 60}`. No `user` mints a bot token. Empty scopes or `["*"]` grants
the full default set for that kind; explicit scopes are enforced exactly. Response has
`access_token`, `scope`, `user_id`, `team_id` (and `bot_id`).

### OAuth v2 install flow

`GET /oauth/v2/authorize?client_id&scope&user_scope&redirect_uri&state&team` redirects to
`redirect_uri?code=...&state=...`. `login_hint` (e-mail, `U...` id, world person id or username)
approves headlessly; without it an account picker page lists the active members. `login_hint=deny`
redirects with `error=access_denied`. The emulator-only `channel` parameter picks the channel for
the `incoming-webhook` scope (default `#general`). `POST /api/oauth.v2.access` takes client
credentials via Basic or form fields, `code`, `redirect_uri`; codes are single use, client-bound,
and require the original `redirect_uri` when one was supplied during authorization. OAuth install
codes and OpenID Connect codes cannot be exchanged through each other's token endpoints.
PKCE `code_challenge`/`code_challenge_method` are enforced at exchange with `code_verifier`.
Response:
`app_id`, `authed_user {id, scope, access_token, token_type}`, `scope`, `token_type: "bot"`,
`access_token`, `bot_user_id`, `team {id, name}`, `enterprise: null`, `is_enterprise_install`,
`incoming_webhook {channel, channel_id, configuration_url, url}`. Errors: `invalid_code`,
`bad_redirect_uri`, `invalid_client_id`, `bad_client_secret` (strict mode), `invalid_grant_type`.

Token rotation: `POST /_emulator/actions/configure_app {"token_rotation": true}` (or
`EMULATORS_SLACK_TOKEN_ROTATION=true`). Tokens become `xoxe.xoxb-1-...`/`xoxe.xoxp-1-...` with
`refresh_token` and `expires_in: 43200`; `grant_type=refresh_token` rotates both. Refresh tokens
are bound to their issuing client; a rejected cross-client exchange does not consume the token.
Expired tokens answer `token_expired` (advance the clock with `POST /_emulator/clock`).

### Sign in with Slack (OpenID Connect)

`/openid/connect/authorize` (same `login_hint` rules, `nonce` echoed), `openid.connect.token`
returns `access_token` + RS256 `id_token` (verify with `/openid/connect/keys`),
`openid.connect.userInfo` returns claims limited by the `openid`/`email`/`profile` scopes
(`https://slack.com/user_id`, `.../team_id`, `.../team_domain`, `.../user_image_*`).

## Web API conventions

* `GET` (query) or `POST` (`application/x-www-form-urlencoded`, `application/json` with Bearer
  auth, `multipart/form-data` for uploads). Structured parameters (`blocks`, `attachments`,
  `metadata`, `profile`, `files`) are accepted JSON-encoded in form bodies or native in JSON.
* HTTP 200 with `{"ok": true, ...}` or `{"ok": false, "error": "..."}`; unknown methods are
  `unknown_method`. A JSON POST without `charset` gets `warning: missing_charset` like Slack.
  Only injected rate limits use HTTP 429 (`{"ok": false, "error": "ratelimited"}` + `Retry-After`;
  create one with `POST /_emulator/faults`).
* Cursor pagination: `limit` + `cursor`, `response_metadata.next_cursor` (`""` on the last page;
  cursors are base64 `team:<id>`, `user:<id>`, `next_ts:<ts>`...). Bad cursor: `invalid_cursor`;
  `limit` below 1: `invalid_limit`; above the maximum it is clamped.
* `ts` values are unique per conversation, strictly increasing, derived from the emulator clock
  (seeded messages keep the world timestamps).
* Malformed JSON, invalid multipart bodies, invalid charsets, incorrectly typed scalar fields,
  fractional integer arguments and non-finite/oversized numeric arguments produce Slack errors.
  Valid Unicode survives message rendering and file upload/download.
* Visibility is enforced per caller: private channels, DMs and group DMs are `channel_not_found`
  for non-members; bots must be members to read history (`not_in_channel`), users may read public
  channels they have not joined; posting needs membership (`not_in_channel`) unless the bot has
  `chat:write.public`; guests only see conversations they belong to; archived channels reject
  writes (`is_archived`).

## Endpoints

Scopes are what the emulator enforces (any listed alternative suffices); *U* means user tokens only.

| Method | Scopes / notes |
| --- | --- |
| `api.test` | none; echoes `args`, `error=` forces an error |
| `auth.test`, `auth.revoke` (`test=true`), `auth.teams.list` | any token |
| `oauth.v2.access`, `openid.connect.token` | client credentials |
| `openid.connect.userInfo` | U, `openid` (+`email`,`profile`) |
| `conversations.list` | per type: `channels:read` `groups:read` `mpim:read` `im:read`; `types` filtered by granted scopes; `exclude_archived` applied after slicing the virtual page; `limit` (100/1000) |
| `conversations.info` | read scope of the type; `include_num_members` |
| `conversations.history` | `*:history`; `oldest` `latest` `inclusive` `limit` (100/999); `has_more`, `pin_count`; thread replies hidden, broadcasts shown |
| `conversations.replies` | `*:history`; `ts` of parent or reply; parent counts toward `limit`; ascending timestamp order; `oldest`/`latest`/`inclusive` apply to parent and replies; final cursor is empty; membership messages return `thread_not_found` |
| `conversations.members` | read scope; deactivated users omitted |
| `conversations.join` | `channels:join` / `channels:write`; `warning: already_in_channel` |
| `conversations.leave` `.invite` `.kick` `.archive` `.unarchive` `.rename` `.setTopic` `.setPurpose` `.open` `.close` `.mark` | write scope of the type (`channels:manage`/`channels:write`, `groups:write`, `im:write`, `mpim:write`); `.setTopic` also accepts the type's `*:write.topic` |
| `conversations.create` | `channels:manage`/`channels:write` (`groups:write` for `is_private`); name rules (`invalid_name_*`, `name_taken`) |
| `chat.postMessage` | `chat:write`; channel id, `#name` or user id (opens the DM); `text` `blocks` `attachments` `thread_ts` `reply_broadcast` `metadata`; `username`/`icon_*` only with `chat:write.customize` |
| `chat.update` `.delete` `.meMessage` | `chat:write`; authors only (admins may delete); empty block/attachment arrays clear fields; metadata can be updated/removed; deleted thread parents become tombstones; deleting the last reply retains `thread_ts` and `reply_count: 0` |
| `chat.postEphemeral` | `chat:write`; stored in `ephemeral_messages`, never in history |
| `chat.getPermalink` | any; replies get `?thread_ts=...&cid=...` |
| `chat.scheduleMessage` `.scheduledMessages.list` `chat.deleteScheduledMessage` | `chat:write`; posted lazily when the clock passes `post_at` (`time_in_past`, `time_too_far`) |
| `chat.unfurl` | `links:write`; accepted, no-op |
| `users.list` `.info` `.getPresence` | `users:read`; `profile.email` needs `users:read.email` |
| `users.lookupByEmail` | `users:read.email`; deactivated users return `users_not_found` |
| `users.profile.get` / `.set` | `users.profile:read` / U `users.profile:write` (admins may edit others) |
| `users.conversations` | type scopes; `user` defaults to the caller |
| `users.setPresence` | U `users:write` |
| `team.info`, `team.profile.get` | `team:read`, `users.profile:read` |
| `usergroups.list` `.users.list` | `usergroups:read` |
| `usergroups.create` `.update` `.users.update` `.disable` `.enable` | `usergroups:write`; user tokens must be admins |
| `reactions.add` `.remove` `.get` `.list` | `reactions:write` / `reactions:read` (messages and files) |
| `pins.add` `.remove` `.list` | `pins:write` / `pins:read`; adds a `pinned_item` message |
| `bookmarks.list` `.add` `.edit` `.remove` | `bookmarks:read` / `bookmarks:write` |
| `emoji.list` | `emoji:read` |
| `dnd.info` `.teamInfo` / `.setSnooze` `.endSnooze` `.endDnd` | `dnd:read` / U `dnd:write` |
| `bots.info` | `users:read` |
| `files.upload` (legacy) | `files:write`; multipart `file` or editable snippet `content`; `channels` accepts names/IDs; title defaults to filename without extension; `initial_comment`. `EMULATORS_SLACK_LEGACY_FILES_UPLOAD=false` answers `method_deprecated` like real Slack today |
| `files.getUploadURLExternal`, `files.completeUploadExternal` | `files:write`; upload bytes with `POST <upload_url>` (raw or multipart) -> `OK - <n>` |
| `files.list` `.info` `.delete` | `files:read` / `files:write`; `user` `channel` `types` `ts_from` `ts_to` `count` `page` |
| `files.sharedPublicURL` `.revokePublicURL` | U `files:write` |
| `search.messages` `.files` `.all` | U `search:read`; modifiers `in:` `from:` `to:` `with:` `after:` `before:` `on:` `during:` `has:link|reaction|pin|file|emoji` `is:thread`, `-negation`, `"phrases"`; `sort` score/timestamp, `sort_dir`, `count` (20/100), `page`, `highlight`, `cursor=*` |
| `reminders.add` `.list` `.info` `.complete` `.delete` | U `reminders:*`; `time` as unix ts, seconds, "in 15 minutes", "tomorrow", "at 3pm"; Slackbot DMs when due |
| `views.open` `.push` `.update` `.publish` | `trigger_id` from the interactivity actions below (`invalid_trigger_id`, `exchanged_trigger_id`, `expired_trigger_id`, `hash_conflict`) |

File URLs: `url_private` / `url_private_download` on `files.slack.com` need `Authorization: Bearer`
with `files:read` and visibility of the file; without a token they answer a 302 to the sign-in
page like Slack, with a token lacking the scope 403, for invisible files 404.
Guests cannot discover or download files from public channels they have not joined. File objects,
including those embedded in messages or search results, omit inaccessible private share locations.
External uploads validate the declared byte length; completion validates every requested file and
share before consuming any pending upload. Failed batches can be corrected and retried, and a
successful completion is single use. Filenames are URL-encoded, with Unicode-safe download headers;
control characters in uploaded filenames become underscores.
Files shared into public channels report `is_public: true` independently of `public_url_shared`.
User tokens may enable public links for another uploader's file when it is shared in a public channel.

## Seeded data

Workspace `T07CC5F644F`, domain `halcyonadvisory`, e-mail domain `halcyonadvisory.example`.

* **Users**: all 45 active employees (id `U` + 10 uppercase hex, stable per world person) plus the
  two departed employees as `deleted: true`, `USLACKBOT`, and the bot user `halcyon_bot`
  (`UED5AF609AC`). Eleanor Whitfield (`UA3CCCCDC1B`, p001) is primary owner; Arjun Mehta
  (`UD83028359A`) and Sarah Johansson (IT) are admins. `name` is the world username
  (`maya.chen`), `profile.email` the world e-mail, time zones and phones from the world.
  Two client contacts are guests: Stephanie Pearson (`U76DF3A6BCF`, `is_restricted`) and
  Felix Dempsey (`U6D624A5A46`, `is_restricted` + `is_ultra_restricted`). Contacts of four clients
  are Slack Connect users (`is_stranger`, other `team_id`, not in `users.list`).
* **Public channels**: `general` (`C8193FBC2F8`, `is_general`), `announcements`, `it-help`, `social`,
  `audit-general`, `audit-fieldwork`, `audit-technical-accounting`, `tax-general`, `tax-updates`,
  `tax-busy-season`, `advisory-general`, `advisory-deals`, `bd-general`, `bd-pipeline`,
  `eng-bluefin-seafood`, `eng-bluefin-seafood-requests`.
* **Private channels** (ids start with `G`): `advisory-deal-team-confidential`, `partners`
  (`G972623DA35`), `partners-finance`, `eng-bluefin-seafood-fees`.
* **Slack Connect** (`is_ext_shared`, client contacts as members): `ext-pinnacle-robotics`,
  `ext-maple-main-bakery`, `ext-harborview-property-management`, `ext-ironwood-construction`.
* The bot is a member of every public channel except `social` and `bd-general` (so
  `not_in_channel` has a public example), of every Slack Connect channel and of
  `eng-bluefin-seafood-fees`; it is not in the other private channels.
* **Messages**: ~760, with world threads (`thread_ts`, `reply_count`, `reply_users`,
  `latest_reply`), reactions (`+1`, `joy`, `heart`), `<@U...>` mentions and generated `rich_text`
  blocks; 26 world chats become 23 DMs and 3 group DMs (`mpdm-...-1`), plus a welcome DM from
  the bot to Eleanor.
* **Files**: documents from the world attached to announcements and engagement channels (real
  DOCX/XLSX/PDF bytes; `files.list`, download with a token). Repeated references to the same
  world document retain every message/share reference and reuse the cached binary.
* **User groups**: `@exec` `@audit` `@tax` `@advisory` `@finance` `@people` `@it` `@growth`
  (departments) and `@partners` `@leadership` `@managers` `@it-admins` (roles); ids `S...`.
* **Pins** in `general`, `announcements`, `it-help`; **bookmarks** in `general`, `it-help`,
  `audit-fieldwork`, `tax-updates`; custom emoji `halcyon`, `ship-it`...
* **Incoming webhooks** (listed in `/_emulator/info`): `general`, `it-help`, `audit-fieldwork`,
  `tax-updates`, `advisory-deals`, `bd-pipeline`, `eng-bluefin-seafood`, e.g.
  `https://hooks.slack.com/services/T…/B…/… (see info)` posts to
  `#general`.

## Incoming webhooks

`POST https://hooks.slack.com/services/T.../B.../<secret>` with JSON (or `payload=<json>` form):
`text`, `blocks`, `attachments`, `thread_ts`. App incoming webhooks ignore `channel`, `username`,
`icon_emoji`/`icon_url` overrides. Answers plain
text: `200 ok`; `404 no_service`; `400 invalid_payload` / `no_text` / `invalid_blocks` /
`invalid_attachments`; `410 channel_is_archived`. Messages carry `subtype: bot_message`, the
webhook's own `bot_id`, `username` and `bot_profile`. `oauth.v2.access` with `incoming-webhook`
and `POST /_emulator/actions/create_webhook` create more.

## Events API

Configure with `POST /_emulator/actions/configure_events`:
`{"request_url": "...", "signing_secret": "...", "events": ["message.channels", ...]}`.
The emulator first POSTs `{"token", "challenge", "type": "url_verification"}` (signed) and requires
the challenge back as JSON `{"challenge"}`, plain text or form; otherwise the call fails with
`url_verification_failed` and nothing is saved (`skip_verification: true` bypasses it). Defaults:
`message.channels message.groups message.im app_mention member_joined_channel reaction_added
channel_created file_shared team_join`; also supported: `message.mpim`, `member_left_channel`,
`reaction_removed`, `channel_rename`, `channel_archive`, `channel_unarchive`, `pin_added`,
`pin_removed`, `user_change`.

Deliveries are `event_callback` envelopes (`token`, `team_id`, `api_app_id`, `event`, `type`,
`event_id` `Ev...`, `event_time`, `authorizations`, `is_ext_shared_channel`, `event_context`)
signed `X-Slack-Signature: v0=<hmac_sha256(signing_secret, "v0:{ts}:{body}")>` with
`X-Slack-Request-Timestamp` (verify with `slack_sdk.signature.SignatureVerifier`). Failed
deliveries are retried (initial attempt plus 3 retries) with `X-Slack-Retry-Num` and
`X-Slack-Retry-Reason` (`http_error`, `http_timeout`, `connection_failed`) and a fresh signature.
The callback timeout is 3 seconds. `X-Slack-No-Retry: 1` in a failure response stops retries for
that delivery; up to two redirects are followed. Webhook mode (`sync`/`async`/`manual`) and the
journal (`GET /_emulator/webhooks`) use the shared dispatcher through a Slack-local subclass.

Rules: conversation events (messages, reactions, joins, files, pins) are only delivered for
conversations the app's bot user belongs to; workspace events (`channel_created` for public
channels, `team_join`, `user_change`, `channel_rename`/archive for public channels) always. The
app never receives messages (or edits/deletes) authored by its own bot user. Webhook posts are
delivered as `bot_message` events. Message events cover edits (`message_changed`), deletes
(`message_deleted`), `thread_broadcast`, `file_share`, `channel_join` and friends.

## Interactivity

`slash_command`, `interact` (`block_actions`), `submit_view` and `close_view` actions play the user
and POST signed Slack bodies (form fields for commands, `payload=<json>` for interactions) to
`request_url`, or to the default set with `configure_interactivity {"interactivity_url": ...}`.
Immediate slash-command replies are applied (plain text and JSON `ephemeral`, `in_channel` as the
bot). `response_url`s (`https://hooks.slack.com/commands|actions/T/<n>/<secret>`, 5 uses,
30 minutes) support `response_type`, `replace_original`, `delete_original`, `thread_ts`.
`trigger_id` actions mint triggers for `views.*`. Deliveries show up in `/_emulator/webhooks`.

## Control-plane actions (`POST /_emulator/actions/<name>`)

| Action | Payload | Effect |
| --- | --- | --- |
| `mint_token` | see above | Slack-shaped token |
| `configure_events` / `disable_events` | `request_url`, `signing_secret?`, `events?`, `skip_verification?` | Events API subscription |
| `configure_app` | `token_rotation?`, `client_id?`, `client_secret?`, `redirect_uris?` | app settings |
| `post_as_user` | `user`, `channel`, `text`, `thread_ts?`, `blocks?`, `reply_broadcast?` | a human posts (fires events; public channels auto-join) |
| `add_reaction` / `remove_reaction` | `user`, `channel`, `ts`, `name` | human reaction |
| `user_join` / `user_leave` | `user`, `channel`, `inviter?` | membership change |
| `team_join` | `first_name`, `last_name`, `email?`, `title?` | new workspace member (joins `#general`) |
| `create_channel` | `name`, `is_private?`, `creator?`, `members?` | channel without permission checks |
| `upload_file` | `user`, `channel`, `filename`, `content` or `content_base64`, `comment?` | human file upload |
| `create_webhook` | `channel`, `label?` | new incoming webhook URL |
| `set_presence` | `user`, `presence` | presence |
| `flush_scheduled` | none | post due scheduled messages now |
| `slash_command` / `interact` / `submit_view` / `close_view` / `trigger_id` / `configure_interactivity` | see Interactivity | interactivity |
| `tokens` | none | the seeded token map |

`user` accepts a `U...` id, e-mail or world person id (`p018`); `channel` an id or name.

## Settings

`EMULATORS_SLACK_TOKEN_ROTATION` (`true` enables rotating tokens) and
`EMULATORS_SLACK_LEGACY_FILES_UPLOAD` (`false` makes `files.upload` answer `method_deprecated`).

## Known gaps

* No Socket Mode (`apps.connections.open`, WebSocket), RTM, `admin.*`, canvases, lists, calls,
  workflows, `conversations.inviteShared`/Connect invites, Enterprise Grid, `oauth.access` v1.
* No built-in rate limiting (inject 429s with `/_emulator/faults`).
* Retry delays are compressed for local tests; Slack's minute-scale retry schedule, delayed events,
  and automatic subscription disabling are not simulated.
* Avatar and emoji image URLs (`secure.gravatar.com`, `emoji.slack-edge.com`, `a.slack-edge.com`)
  are static strings, not served; thumbnails (`thumb_*`) serve the original bytes.
* `unfurl_*`, `link_names`, `parse` are accepted and ignored; text is stored as sent.
* Message search is substring-based (no stemming), UTC date filters.
  Unparseable date modifiers (including invalid `during:` months/years) are ignored.
* Guests are limited to their conversations but still see the full `users.list`.
* `files.upload` is implemented because many clients still use it; real Slack has retired it.
* Block Kit validation checks block types and basic structure, rather than every element schema.
* Message content above 40,000 characters returns `msg_too_long`; Slack can truncate some message
  submissions with a `message_truncated` warning instead.
* Public file links are metadata only; anonymous `slack-files.com` hosting is not implemented.

## Fidelity audit references

The HTTP regressions in `tests/providers/slack/test_fidelity_audit.py` and the official SDK
contracts cover malformed writes, thread pagination, file visibility and completion, OAuth
binding/PKCE, webhook overrides and retry behavior. Source references:
[thread paging](https://docs.slack.dev/reference/methods/conversations.replies/),
[message updates](https://docs.slack.dev/reference/methods/chat.update/),
[thread lifecycle](https://docs.slack.dev/messaging/retrieving-messages/),
[email lookup](https://docs.slack.dev/reference/methods/users.lookupByEmail/),
[OAuth](https://docs.slack.dev/reference/methods/oauth.v2.access/),
[file completion](https://docs.slack.dev/reference/methods/files.completeUploadExternal/),
[legacy upload shapes](https://docs.slack.dev/reference/methods/files.upload/),
[incoming webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/),
[Events API retries](https://docs.slack.dev/apis/events-api/), and
[conversation scope filters](https://docs.slack.dev/apis/web-api/using-the-conversations-api/).
