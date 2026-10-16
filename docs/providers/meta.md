# Meta — WhatsApp Business Cloud API + Messenger Platform

The `meta` provider emulates the Meta Graph API surface behind WhatsApp Business
(Cloud API) and Messenger for Halcyon Advisory Group: one business, one WhatsApp
Business Account (WABA) with one phone number, and one Facebook Page. It runs on
port **44109**, through the gateway's `/meta` prefix, or through the vendor
hostnames below. There is no official Python SDK for these APIs, so the contract
suite (`tests/sdk/test_meta_http.py`) drives the real hostnames with plain
`httpx`/`requests`, which is how consumers integrate.

## Hosts

`graph.facebook.com` (API), `www.facebook.com` / `facebook.com` (OAuth dialog,
Page links), `lookaside.fbsbx.com` (WhatsApp media downloads),
`scontent.whatsapp.net`, `*.fbcdn.net` (profile pictures / CDN),
`graph.instagram.com` (routed here; only generic Graph errors — see Known gaps).

Paths are versioned: `/v21.0/...`. Versions `v17.0`–`v24.0` behave identically;
an unversioned path is served as the oldest supported version (`v17.0`) and
paging links keep whichever form you used. Responses carry `x-fb-trace-id`,
`x-fb-request-id`, `x-app-usage` and `x-business-use-case-usage` headers.

## Auth flows and test credentials

Auth is `Authorization: Bearer <token>` or the `access_token` query/body
parameter. If `appsecret_proof` is supplied it is verified as
hex `hmac_sha256(app_secret, access_token)`. Token types are enforced per
endpoint the way Meta does: WhatsApp requires a user or system-user token with
`whatsapp_business_messaging`/`whatsapp_business_management`, the Send API and
User Profile API require the Page token, `/me/accounts` requires a user token,
and `/{app-id}/subscriptions` requires the app token (`<app_id>|<app_secret>`).
Missing/invalid/expired tokens return Graph's `190` (subcodes `467` invalid,
`463` expired).

| Credential | Value |
| --- | --- |
| App id | `1876543210987654` ("Halcyon Integrations") |
| App secret | `0123456789abcdeffedcba9876543210` |
| App access token | `1876543210987654\|0123456789abcdeffedcba9876543210` |
| Webhook verify token | `halcyon-verify-token` |
| System-user token (WhatsApp) | `EAACEdEose0cBOZCHalcyonSystemUser0123456789abcdefghij` |
| Page access token | `EAACEdEose0cBOZCHalcyonPageAccess0123456789abcdefghij` |
| User token (Natalie Cho) | `EAACEdEose0cBOZCHalcyonUserNatalie0123456789abcdefgh` |
| Business id | `101315525793326` |
| WABA id | `596780097745584` |
| Phone number id | `515541599774134` (+1 416-555-0177) |
| Page id | `104857600123456` (`halcyonadvisory`) |
| System user id | `721808110923915` |

`POST /_emulator/tokens` mints tokens via the `mint_token` action:
`{"type": "user"|"page"|"system_user"|"app", "user": "<world person id|email>",
"scopes": [...], "expires_in": 3600}`. With no payload it mints a system-user
token (the WhatsApp workhorse); with `user` a user token for that employee;
page/system-user tokens never expire unless `expires_in` says otherwise.

OAuth works end to end: `GET https://www.facebook.com/v21.0/dialog/oauth?
client_id&redirect_uri&state&scope&response_type=code` (add
`login_hint=<employee email>` to auto-approve, otherwise an account picker is
rendered), then `GET|POST /v21.0/oauth/access_token` with the code for a
short-lived user token (`expires_in` 3600), `grant_type=fb_exchange_token` for a
60-day long-lived token (`expires_in` 5184000), and
`grant_type=client_credentials` for the app token. `GET /debug_token?
input_token=...` returns the real introspection shape (type, scopes,
granular scopes, `data_access_expires_at`, `profile_id` for page tokens), and
`/me`, `/me/accounts` and `/me/permissions` resolve by token type. Page access
tokens handed out by `/me/accounts` are the seeded page token above.

## Graph conventions

Errors are `{"error": {"message", "type", "code", "error_subcode"?,
"error_data"?, "fbtrace_id"}}`, including WhatsApp's 13xxxx codes with
`error_data.details`. `fields=` expansion supports comma lists, modifiers
(`.limit(5)`, `.summary(true)`, `.as(alias)`) and nesting
(`fields=participants,messages.limit(5){message,from,created_time}`); without
`fields` each object returns its usual default field set. Edges paginate with
`data` + `paging.cursors {before, after}` + absolute `paging.next` /
`paging.previous` links (`limit` default 25, max 100; `summary=true` adds
`total_count`). Simple batch requests (`POST /v21.0/` with a `batch` parameter)
are supported. Unknown objects return `(#803) Some of the aliases you requested
do not exist`; unsupported edges on known objects return `100`/subcode `33`.

## Endpoints

All paths below are under `/v{version}` (shown for `v21.0`).

### Graph / OAuth

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/v21.0/dialog/oauth` (www.facebook.com) | `login_hint` auto-approves; `response_type=token` gives a fragment token |
| GET/POST | `/oauth/access_token` | code exchange, `fb_exchange_token`, `client_credentials` |
| GET | `/debug_token?input_token=` | any valid app token as caller |
| GET | `/me` | user, Page, system user or app by token type |
| GET | `/me/accounts`, `/{user-id}/accounts` | Pages the user manages, with page tokens and `tasks` |
| GET/DELETE | `/me/permissions`, `/{user-id}/permissions` | granted scopes; DELETE revokes one |
| GET | `/{id}` | any seeded object: business, WABA, phone, Page, user, PSID, conversation, message, media, template, app |
| GET | `/{business-id}/owned_whatsapp_business_accounts` · `/owned_pages` · `/system_users` | |
| GET | `/{id}/picture` | 302 to the fbcdn URL, or JSON with `redirect=false`; the CDN URL serves real PNG bytes |
| GET/POST/DELETE | `/{app-id}/subscriptions` | app-webhook management (runs the hub.challenge handshake; app token only) |
| POST | `/` with `batch=[...]` | up to 50 sub-requests |

### WhatsApp Cloud API (`{pn}` = phone number id, `{waba}` = WABA id)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/{pn}/messages` | text, image, document, audio, video, sticker, location, contacts, interactive, template, reaction; `{status: "read", message_id}` marks read |
| POST | `/{pn}/media` | multipart `file`, `type`, `messaging_product` → `{id}` |
| GET | `/{media-id}` | metadata + signed lookaside `url`; fetching that URL requires Bearer (401 without) |
| DELETE | `/{media-id}` | `{success: true}` |
| GET/POST | `/{waba}/message_templates` | create starts `PENDING` (UTILITY auto-`APPROVED` in the default lenient mode) |
| DELETE | `/{waba}/message_templates?name=` | also `GET/POST/DELETE /{template-id}` |
| GET | `/{waba}` · `/{waba}/phone_numbers` · `/{pn}` | WABA and phone-number objects |
| GET/POST | `/{pn}/whatsapp_business_profile` | GET returns `{"data": [{...,"messaging_product": "whatsapp"}]}` |
| GET/POST/DELETE | `/{waba}/subscribed_apps` | |
| POST | `/{pn}/register` · `/{pn}/deregister` | accepts a 6-digit `pin` |

Send rules match the real API: free-form messages need a customer message in
the last 24 hours or they fail with `131047` (Re-engagement message); templates
must exist and be APPROVED (`132001`), with matching parameter counts
(`132000`); a malformed recipient fails `131026`; unknown types fail `131051`;
bad parameters fail `100`. A successful send returns
`{"messaging_product": "whatsapp", "contacts": [{"input", "wa_id"}],
"messages": [{"id": "wamid…", "message_status": "accepted"}]}` and then emits
`sent` → `delivered` → `read` status webhooks with `conversation` and
`pricing` objects. Well-formed numbers that are not seeded contacts are
accepted as new contacts (free-form still needs an inbound first).

### Messenger Platform

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/me/messages`, `/{page-id}/messages` | Send API incl. `sender_action` (`typing_on`, `typing_off`, `mark_seen`), quick replies, attachments, templates |
| GET | `/{page-id}/conversations?platform=messenger` | newest first; nested `messages{...}` via `fields` |
| GET | `/{conversation-id}` · `/{conversation-id}/messages` · `/{conversation-id}/participants` | edge returns id/`created_time` stubs by default |
| GET | `/{message-id}` | `from`, `to`, `message`, `attachments`, `tags` |
| GET | `/{psid}` | User Profile API (page token only) |
| POST | `/me/message_attachments` | JSON `{url, is_reusable}` or multipart `filedata` → `{attachment_id}` |
| GET/POST/DELETE | `/me/messenger_profile`, `/{page-id}/messenger_profile` | greeting, get_started, persistent_menu, ice_breakers → `{"result": "success"}` |
| GET | `/{page-id}/roles` · GET/POST/DELETE `/{page-id}/subscribed_apps` | page admins; subscribed fields |
| POST | `/{page-id}` | update about/phone/website/category |

The 24-hour standard window applies to `RESPONSE`/`UPDATE`; `MESSAGE_TAG` with
`CONFIRMED_EVENT_UPDATE`, `POST_PURCHASE_UPDATE` or `ACCOUNT_UPDATE` may send
any time, `HUMAN_AGENT` within 7 days. Outside the window is error `10`,
subcode `2018278`. Successful sends emit `message_deliveries` then
`message_reads` webhooks.

## Seeded data

WhatsApp threads come from the world's eight `whatsapp` conversations (business
number **+1 416-555-0177**, `wa_id` = digits of the contact's mobile). Paula
Iwasaki (`c011-4`, wa_id `13055550174`) sent a follow-up an hour before the hub
clock, so her 24-hour window is **open**; every other seeded thread's window is
closed. Contacts: Hamid Novak `13125550176`, Calvin Jablonski `12125550179`,
Michael Sullivan `15145550183`, Helen Kowal `15035550185`, Esther Tremblay
`16135550188`, Lucia Okonkwo `17185550192`, Dmitri Underwood `18285550194`.

Templates (all `en_US`): `hello_world` `6307561152976497` (no parameters),
`appointment_reminder` (3 body parameters), `document_request` (2),
`payment_reminder` (3) — APPROVED; `year_end_promo` `2990976638089861` —
REJECTED with `rejected_reason: INVALID_FORMAT`.

The Page **Halcyon Advisory Group** (`104857600123456`, username
`halcyonadvisory`, category "Accountant", 1284 fans) is managed by Natalie Cho
(`p040`, Facebook id `153784760597315`, ADMIN) and Zoe Anderson (`p042`,
`534169581565786`, EDITOR). Its six Messenger threads are with Alice Kaur
(PSID `9413472744959863`, conversation `t_153002327766496`, window **open** —
she also sent a recent follow-up), Owen Ingram (`4398397786864001`), Matthew
Zielinski, Ingrid Bennett, Mateo Sato and Melissa Grimaldi
(`7574642349760990`; every non-Alice window is closed, and Melissa's last
inbound is more than 7 days old, so `HUMAN_AGENT` fails for her too).

Phone object highlights: `quality_rating: GREEN`,
`code_verification_status: VERIFIED`, `messaging_limit_tier: TIER_1K`,
`platform_type: CLOUD_API`, `throughput: {level: STANDARD}`. Business profile
vertical is `PROF_SERVICES`.

## Webhooks

Configure with the action below or the Graph `/{app-id}/subscriptions` API;
both perform the verification GET (`hub.mode=subscribe`,
`hub.verify_token`, integer `hub.challenge`) and only store the subscription if
your endpoint echoes the challenge with HTTP 200. Deliveries are signed with
`X-Hub-Signature-256: sha256=<hex hmac_sha256(app_secret, raw_body)>` and the
legacy `X-Hub-Signature: sha1=<hex hmac_sha1(app_secret, raw_body)>`.

- WhatsApp (`object: "whatsapp_business_account"`, entry id = WABA id): inbound
  `messages` payloads with `contacts`, plus `statuses` payloads carrying
  `conversation {id, expiration_timestamp, origin {type}}` and
  `pricing {billable, pricing_model: "PMP", category}` (+ `errors` on failed).
- Messenger (`object: "page"`, entry id = Page id, `time` in ms): `message`
  (`mid`, text, quick_reply, attachments), `postback`, `delivery` and `read`
  objects under `entry[].messaging[]`.

## Actions

| Action | Payload | Effect |
| --- | --- | --- |
| `mint_token` | `{type, user, scopes, expires_in}` | also behind `POST /_emulator/tokens` |
| `configure_webhook` | `{object, callback_url, verify_token, app_secret?, fields?}` | verifies + stores a subscription; returns `{success, challenge, response_status}` |
| `inbound_whatsapp` | `{from (E.164 or world person id), text?, type?, media?, caption?, context_message_id?}` | customer message; opens the 24h window; image/document/audio/video media is real downloadable content |
| `deliver_statuses` | `{message_id, status, error_code?}` | emits one status webhook (works for Messenger mids too) |
| `approve_template` / `reject_template` | `{name, language?}` | template review |
| `inbound_messenger` | `{psid \| person, text?, postback?, quick_reply_payload?}` | user message/postback; opens the window |

## Known gaps

- Instagram messaging/graph endpoints are not modelled (`graph.instagram.com`
  only routes here); Messenger works for `platform=messenger` only.
- No Ads, Pages publishing (posts/feed), Groups, Events, or comments APIs;
  `/{page-id}/feed` and similar edges return Graph's `100`/33 error shape.
- WhatsApp: no group messaging, catalog/Flows/Calling, template analytics, or
  phone-number registration two-factor flows beyond accept-and-succeed;
  `register`/`deregister` do not change routability.
- Rate-limit errors (4/17/32/613) are only produced via fault injection, not by
  counting calls; `x-app-usage` values are nominal.
- Batch sub-requests share the caller's token and cover JSON/form bodies; named
  results (`{result=...}` references) and multipart batch parts are not
  supported. `suppress_http_code` and JSONP (`callback`) are ignored.
- Signing uses the seeded app secret unless a webhook config overrides it;
  multiple apps and multiple subscriptions per object keep only the latest
  callback per object type.
