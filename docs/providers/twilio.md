# Twilio

The `twilio` provider emulates one Twilio account for Halcyon Advisory Group:
Programmable Messaging (SMS/MMS/WhatsApp), Messaging Services, Verify v2, Lookup
v1/v2, basic Programmable Voice (Calls + Recordings) and the Conversations API. It
runs on port **44108**, through the gateway's `/twilio` prefix, or through the
vendor hostnames below. No Twilio network connection is made.

## Hosts

`api.twilio.com`, `messaging.twilio.com`, `verify.twilio.com`,
`lookups.twilio.com`, `conversations.twilio.com`, `media.twiliocdn.com`, and any
other `*.twilio.com` host (regional/edge hostnames such as
`api.us1.twilio.com` route here too).

Wire conventions follow Twilio exactly:

- **2010 API** (`api.twilio.com/2010-04-01/...`): paths end in `.json`,
  requests are `application/x-www-form-urlencoded` with PascalCase parameters
  (`To`, `From`, `Body`, repeatable `MediaUrl`, `StatusCallback`, …), responses
  are snake_case JSON with RFC 2822 GMT dates
  (`"Thu, 16 Oct 2026 14:00:00 +0000"`), relative `uri` fields and the
  `first_page_uri` / `next_page_uri` / `page` / `page_size` / `start` / `end`
  paging envelope (`PageSize` default 50, max 1000). Every paging link repeats
  the request's filters (`To=…`, `State=…`), so following `next_page_uri` /
  `next_page_url` never drops them; only pages after the first carry a
  `PageToken`.
- **v1/v2 APIs** (Messaging, Verify, Lookup, Conversations): ISO-8601 dates
  (`2026-10-16T14:00:00Z`), absolute `url` fields, and the
  `{"<key>": [...], "meta": {"page", "page_size", "first_page_url", …}}`
  paging envelope.
- **Errors**: `{"code": 21211, "message": …, "more_info":
  "https://www.twilio.com/docs/errors/21211", "status": 400}` everywhere,
  including for injected faults and framework 404s (code 20404, message
  `The requested resource <path> was not found`). A 401 also carries Twilio's
  `detail` (`Your AccountSid or AuthToken was incorrect.`); a 405 is code 20004;
  required-parameter errors on the v1/v2 APIs are 400 / 20001
  (`Missing required parameter X in the post body`); Verify uses 60200 for bad
  parameters, 60202/60203 (429) for check/send limits.
- **Headers**: every response (errors and injected faults included) carries
  `Twilio-Request-Id` (`RQ` + 32 hex), `Twilio-Request-Duration` and
  `Twilio-Concurrent-Requests`.

## Auth flows and test credentials

HTTP Basic auth, exactly like Twilio. Four credential types authenticate
(values below are for the default seed `emulators`; fetch live values any time
from `GET /_emulator/info` or `POST /_emulator/tokens`):

| Credential | Where to get it |
| --- | --- |
| Account SID (live) | `world.org.twilio_account_sid` (`AC` + 32 hex) — `GET /_emulator/info` → `credentials` |
| Auth Token (live) | `GET /_emulator/info` → `credentials` |
| Account SID / Auth Token (test credentials) | `GET /_emulator/info` → `credentials` |
| API Key SID / Secret | `GET /_emulator/info` → `credentials` |

Literal values are deliberately not printed here: they are deterministic per seed, but they have
the exact shape of real Twilio secrets and would trip secret scanners.

- Use `AccountSid:AuthToken`, or an API key pair
  `SK…:secret` together with the account SID in the path (the official SDK does
  this when constructed as `Client(key_sid, secret, account_sid)`). API keys
  exist only while they exist: deleting one (including the seeded key) revokes
  it immediately.
- Missing or unknown credentials → `401 {"code": 20003, "message":
  "Authenticate", …}`. By default the emulator is **lenient**: any password is
  accepted for a known SID/key. Set `EMULATORS_TWILIO_STRICT_AUTH=1` to require
  the exact token/secret. A `Bearer` grant from `POST /_emulator/tokens` is
  also accepted, and that endpoint simply returns the Basic credentials above
  (Twilio has no OAuth bearer flow).
- The **test credentials** alias the same seeded account (the response
  `account_sid` echoes the test SID) but operate like Twilio test credentials:
  the magic numbers below apply and **no status-callback or inbound webhooks
  are fired** for test-mode messages/calls. For determinism, the magic-number
  failures also apply with the live credentials.
- Wrong `{AccountSid}` in a path → `404` code 20404.
- **Subaccounts** created with `POST /2010-04-01/Accounts.json` are fully
  isolated: they see none of the parent's messages, calls or numbers, and
  cannot send from the parent's numbers (21606). They authenticate with their
  own SID and `auth_token` and can only address themselves (any other account
  path → `403` 20003 `Permission Denied`; they cannot create subaccounts and
  `GET /Accounts.json` lists only themselves). The parent's credentials reach
  every subaccount. API keys belong to the account whose `/Keys` created them
  and act as that account only, and the v1/v2 resources (Messaging and Verify
  Services, Conversations) are scoped to the credentials' own account, so a
  subaccount sees none of the parent's services or conversations and cannot
  pool, verify or send through them. A subaccount's `Status` can be `active`, `suspended` or
  `closed`; a suspended/closed account refuses writes with `403` 20005
  (`Account not active`) while reads still work, a closed account cannot be
  reopened, and the main account cannot be suspended or closed.

### Magic numbers (deterministic failure simulators)

| Number | As `To` | As `From` |
| --- | --- | --- |
| `+15005550001` | 400 **21211** invalid number | 400 **21212** invalid From |
| `+15005550002` | 400 **21612** cannot route | — |
| `+15005550003` | 400 **21408** no international permission | — |
| `+15005550004` | 400 **21610** blocked / opted out | — |
| `+15005550009` | 400 **21614** not a mobile number | — |
| `+15005550006` | — | valid test sender (always allowed) |
| `+15005550007` | — | 400 **21606** not owned by the account |
| `+15005550008` | — | 400 **21611** queue full / MPS exceeded |
| any `…0199` | delivers as **undelivered**, error **30003** | — |
| any `…0198` | delivers as **undelivered**, error **30005** | — |

### Settings (environment variables / `EmulatorHub(settings=...)`)

| Variable | Effect |
| --- | --- |
| `EMULATORS_TWILIO_STRICT_AUTH=1` | Require the exact auth token / API secret / subaccount token |
| `EMULATORS_TWILIO_AUTO_ADVANCE=0` | Messages stay `queued`/`accepted` and calls stay `queued` after creation; drive each transition yourself with `advance_message` / `advance_call` (and, for calls, `POST …/Calls/{CA}.json`) so a test can observe, cancel or hang up an in-progress call |

## Endpoints

Base below is `https://api.twilio.com` unless another host is named.
`{A}` = account SID.

### Accounts, keys, balance, usage

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/2010-04-01/Accounts.json` | List accounts (filters `FriendlyName`, `Status`) |
| POST | `/2010-04-01/Accounts.json` | Create a subaccount (default name `SubAccount Created at 2026-10-16 9:05 AM`) |
| GET | `/2010-04-01/Accounts/{A}.json` | Friendly name, status `active`, type `Full` |
| POST | `/2010-04-01/Accounts/{A}.json` | Update `FriendlyName`, `Status` (`active`/`suspended`/`closed`) |
| GET | `/2010-04-01/Accounts/{A}/Balance.json` | `{"balance": "152.34", "currency": "USD"}` |
| GET | `/2010-04-01/Accounts/{A}/Usage/Records.json` | `sms`, `calls`, `totalprice` computed from live state; `StartDate`/`EndDate`/`Category` |
| GET | `/2010-04-01/Accounts/{A}/Usage/Records/{Period}.json` | `Daily`, `Monthly`, `Yearly`, `AllTime`, `Today`, `Yesterday`, `ThisMonth`, `LastMonth` |
| POST/GET | `/2010-04-01/Accounts/{A}/Keys.json` | Create/list API keys (`secret` only in the create response) |
| GET/POST/DELETE | `/2010-04-01/Accounts/{A}/Keys/{SK}.json` | Fetch / rename / revoke (POST `…/SigningKeys.json` aliased) |

### Messages and media

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/2010-04-01/Accounts/{A}/Messages.json` | Create; see below |
| GET | `/2010-04-01/Accounts/{A}/Messages.json` | Filters `To`, `From`, `DateSent`, `DateSent<`, `DateSent>` (date or datetime, inclusive bounds, 400 when malformed; unsent messages never match); newest `date_sent` first |
| GET | `/2010-04-01/Accounts/{A}/Messages/{SM}.json` | Fetch one |
| POST | `/2010-04-01/Accounts/{A}/Messages/{SM}.json` | `Body=""` redacts; `Status=canceled` cancels a scheduled message |
| DELETE | `/2010-04-01/Accounts/{A}/Messages/{SM}.json` | 204 |
| POST | `/2010-04-01/Accounts/{A}/Messages/{SM}/Feedback.json` | `Outcome=confirmed\|unconfirmed` |
| GET | `/2010-04-01/Accounts/{A}/Messages/{SM}/Media.json` | List media (`media_list`) |
| GET | `/2010-04-01/Accounts/{A}/Messages/{SM}/Media/{ME}.json` | Media metadata |
| GET | `/2010-04-01/Accounts/{A}/Messages/{SM}/Media/{ME}` | Media **bytes** (real PNG/PDF/… rendered lazily, blob-cached) |
| DELETE | `/2010-04-01/Accounts/{A}/Messages/{SM}/Media/{ME}.json` | 204 |

Message create accepts `To` / `From` in E.164 or `whatsapp:+1…` form, or a
`MessagingServiceSid` instead of `From` (a sender is picked from the pool,
sticky per recipient). Validation, with Twilio's codes: `To` missing 21604,
`From` missing 21603, no `Body`/`MediaUrl`/`ContentSid` 21602, bad numbers
21211 (NANP numbers need `+1` + 10 digits with a 2-9 area code), `From` not one
of the account's numbers (or the magic test sender, or an alphanumeric sender
ID) 21606, mixed channels (`whatsapp:` on one side only) 21910, a `whatsapp:`
From that is neither the registered WhatsApp sender nor Twilio's published
WhatsApp sandbox number (`whatsapp:+14155238886`, always accepted) 63007, Body over 1600
characters 21617, more than 10 `MediaUrl`s 21623, a non-http(s) `MediaUrl`
21620, a malformed `StatusCallback` 21609, a bad `ContentSid` (`HX…`) 21655
(`ContentSid` stands in for `Body`; the emulator has no template store, so
`body` stays empty). `StatusCallback` is always a POST (Twilio has no
`StatusCallbackMethod` for messages).

`SendAt` + `ScheduleType=fixed` schedules a message and requires a Messaging
Service (21704 without one). `SendAt` must be ISO 8601 and 15 minutes to 35 days
ahead of the emulator clock (otherwise 400 / 35114; an unsupported
`ScheduleType` is 35115). Scheduled messages are released when the emulator
clock passes `SendAt`: the next read or list of messages moves them
`scheduled → queued → sent → delivered`, so `POST /_emulator/clock`
(`advance_seconds`) is all a test needs.

`num_segments` follows the real GSM-7 (160/153, extension characters count
double) and UCS-2 (70/67, UTF-16 code units) rules; `num_media`/`num_segments`
are strings (a media-only message is one segment, with `body: ""`), like
Twilio.

The create response is `201` with the **initial** status (`queued`, or
`accepted` via a Messaging Service, or `scheduled`), exactly like Twilio —
then the message progresses `queued → sending → sent → delivered`
(or `undelivered` for the `…0199`/`…0198` simulators), firing the
StatusCallback at each step. In `sync` webhook mode the whole progression has
completed by the time the create call returns, so an immediate `GET` shows
`delivered` with `date_sent` and a negative `price`. In `async` mode it
advances moments later (and `hub.drain_webhooks()` waits for it);
`POST /_emulator/actions/advance_message` forces it, and
`EMULATORS_TWILIO_AUTO_ADVANCE=0` leaves it entirely to the action. `failed`
and `undelivered` messages always carry an `error_code` (30003/30005 for the
simulators, else 30008 `Unknown error`); `failed` messages have no `price`.

### Phone numbers

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/2010-04-01/Accounts/{A}/IncomingPhoneNumbers.json` | Filters `PhoneNumber` (exact, partial like `+1416`, or `*` wildcard), `FriendlyName`, `Beta` |
| GET/POST | `/2010-04-01/Accounts/{A}/IncomingPhoneNumbers/{Local\|TollFree\|Mobile}.json` | Type-filtered list / purchase (`+1 8xx` numbers are toll-free, `+447…` mobile) |
| POST | `/2010-04-01/Accounts/{A}/IncomingPhoneNumbers.json` | Purchase by `PhoneNumber` or `AreaCode` (neither: 400 / 20001; duplicate 21422; invalid 21421; empty area code 21452) |
| GET | `/2010-04-01/Accounts/{A}/IncomingPhoneNumbers/{PN}.json` | Fetch |
| POST | `/2010-04-01/Accounts/{A}/IncomingPhoneNumbers/{PN}.json` | Update `SmsUrl`, `VoiceUrl`, methods, fallbacks, `StatusCallback`, `FriendlyName`, … (bad URL 21402, bad method 21403; `PhoneNumber` is purchase-only) |
| DELETE | `/2010-04-01/Accounts/{A}/IncomingPhoneNumbers/{PN}.json` | Release (204); removed from Messaging Service pools |
| GET | `/2010-04-01/Accounts/{A}/AvailablePhoneNumbers.json` | Country list (US, CA, GB) |
| GET | `/2010-04-01/Accounts/{A}/AvailablePhoneNumbers/{CC}.json` | One country (404 for others) |
| GET | `/2010-04-01/Accounts/{A}/AvailablePhoneNumbers/{CC}/Local.json` | Fictional `555-01XX` inventory minus numbers you already own; filters `AreaCode`, `Contains`; also `TollFree`, `Mobile`, `National`, `Voip`, … |

An IncomingPhoneNumber's `status_callback` is for **voice** call status (as in
Twilio); message status callbacks come from the per-message `StatusCallback` or
the Messaging Service's.

### Messaging Services — `https://messaging.twilio.com`

| Method | Path | Notes |
| --- | --- | --- |
| GET/POST | `/v1/Services` | List (meta paging) / create (`FriendlyName` required, else 400 / 20001) |
| GET/POST/DELETE | `/v1/Services/{MG}` | Fetch / update / delete; URLs, methods, `ValidityPeriod` (1-14400, default 14400) and `ScanMessageContent` are validated |
| GET/POST | `/v1/Services/{MG}/PhoneNumbers` | Sender pool; POST takes `PhoneNumberSid` |
| GET/DELETE | `/v1/Services/{MG}/PhoneNumbers/{PN}` | Fetch / remove from pool |

A number belongs to one sender pool at a time: adding it to the same service
again is `409` / 21710, adding it while it sits in another service `409` /
21712. Pool entries list `capabilities` as `["MMS", "SMS", "Voice"]`.

Inbound routing: if a pooled number receives a message and the service has an
`inbound_request_url`, that URL is used unless
`use_inbound_webhook_on_number` is true, in which case the number's own
`SmsUrl` wins. A service-level `status_callback` is used for its messages when
no per-message `StatusCallback` is given.

### Verify — `https://verify.twilio.com`

| Method | Path | Notes |
| --- | --- | --- |
| GET/POST | `/v2/Services` | List / create (`FriendlyName` required; `CodeLength` 4-10; `LookupEnabled` defaults to false) |
| GET/POST/DELETE | `/v2/Services/{VA}` | Fetch / update / delete |
| POST | `/v2/Services/{VA}/Verifications` | Start: `To`, `Channel` (`sms\|call\|email\|whatsapp\|sna`) both required, `Locale`, `CustomCode` (4-10 digits), `Amount`, `Payee` |
| GET | `/v2/Services/{VA}/Verifications/{VE}` | Fetch (`pending`, `approved`, `canceled`, `expired`, `max_attempts_reached`) |
| POST | `/v2/Services/{VA}/Verifications/{VE}` | Update `Status=canceled\|approved` (404 unless still pending) |
| POST | `/v2/Services/{VA}/VerificationCheck` | Check: `To` or `VerificationSid`, plus a 4-10 digit `Code` |

Verification codes are 6 digits (the service's `code_length`), valid for 10
minutes of emulator clock time. Starting a verification for a recipient that
already has a pending one **re-sends on the same verification** (same `sid`, the
same code and original expiry, one more entry in `send_code_attempts`) — a sixth
start returns `429` / 60203. A wrong code returns `200` with `status: pending`; five wrong
checks are allowed and the sixth returns `429` / 60202 and marks the
verification `max_attempts_reached`. Checking an unknown, expired, canceled,
approved or exhausted verification is `404` / 20404. For tests, the real code is
visible as the `code` field in `GET /_emulator/state/verifications`, and the
universal code **`123456` is always accepted**. The verification's `lookup` is
Verify's `{"carrier": {name, type, mobile_country_code, …}}` object (empty for
email). Starting an SMS/WhatsApp verification also creates a normal Message
record (`"Your Halcyon Client Portal verification code is: …"`) from the firm's
SMS number (or WhatsApp sender), so it shows up in `Messages` list/fetch like
any other send. `CustomCode` requires `custom_code_enabled` on the service (the
seeded service has it enabled).

Resends follow Twilio's [code validity contract](https://www.twilio.com/docs/verify/api/rate-limits-and-timeouts):
the initial code remains valid for the original ten-minute window; resending does not extend it.
Rejected Messaging/Verify service updates leave existing settings unchanged.

### Lookup — `https://lookups.twilio.com`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/v2/PhoneNumbers/{number}?Fields=…&CountryCode=…` | Validation always included; packages `caller_name`, `line_type_intelligence`, `line_status`, `sms_pumping_risk`, `sim_swap` on request |
| GET | `/v1/PhoneNumbers/{number}?Type=carrier&Type=caller-name` | The v1 shape used by the SDKs' deprecated `client.lookups.phone_numbers(...)`; 404 for invalid numbers |

National-format input is accepted (`CountryCode`, default `US`; `020 7946 0100`
with `GB`). Invalid numbers return `valid: false` with Lookup v2's vocabulary in
`validation_errors`: `NOT_A_NUMBER`, `INVALID_COUNTRY_CODE`, `TOO_SHORT`,
`TOO_LONG`, `INVALID_BUT_POSSIBLE` (NANP area codes starting 0/1 and
`+15005550001`). World people's mobiles resolve as `mobile` with their
(upper-case) name as `caller_name`; office/business numbers are `landline`; the
account's own Twilio numbers are `nonFixedVoip` / "Twilio Inc.".

### Voice

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/2010-04-01/Accounts/{A}/Calls.json` | Create: `To` (21201), `From` (21213; not owned 21210), `Url` or `Twiml` or `ApplicationSid` (21205), `StatusCallback`, `StatusCallbackEvent` (repeated or space separated), `StatusCallbackMethod`, `Record` |
| GET | `/2010-04-01/Accounts/{A}/Calls.json` | Filters `To`, `From`, `Status`, `ParentCallSid`, `StartTime[<>]` |
| GET | `/2010-04-01/Accounts/{A}/Calls/{CA}.json` | Fetch |
| POST | `/2010-04-01/Accounts/{A}/Calls/{CA}.json` | `Status=completed` hangs up an in-progress call, `Status=canceled` cancels a queued/ringing one; anything on a finished call is `400` / 21220; `Url`/`Twiml` accepted |
| GET/POST | `/2010-04-01/Accounts/{A}/Calls/{CA}/Recordings.json` | Call recordings |
| GET/DELETE | `/2010-04-01/Accounts/{A}/Calls/{CA}/Recordings/{RE}.json` | The recording through its call |
| GET | `/2010-04-01/Accounts/{A}/Recordings.json` | List (filter `CallSid`) |
| GET/DELETE | `/2010-04-01/Accounts/{A}/Recordings/{RE}.json` | Fetch / delete |
| GET | `/2010-04-01/Accounts/{A}/Recordings/{RE}` (also `.wav`, `.mp3`) | Audio bytes: real WAV, and a real (silent) MPEG-1 Layer III stream for `.mp3`, generated lazily |

Calls progress `queued → ringing → in-progress → completed` with a
deterministic duration (20–200 s derived from the call SID) when they finish on
their own; a call a test holds in progress (`EMULATORS_TWILIO_AUTO_ADVANCE=0`)
lasts as long as the emulator clock says. When the call is answered, the `Url`
is POSTed with the standard call parameters (the TwiML response is accepted but
not executed — see Known gaps). `Record=true` produces a Recording on
completion. Status callbacks carry the real parameter set (`CallStatus`
`initiated`/`ringing`/`in-progress`/`completed`, `Called`, `Caller`, geography
fields, `SequenceNumber`); busy, no-answer, failed and canceled calls report
under the `completed` event.

### Conversations — `https://conversations.twilio.com`

| Method | Path | Notes |
| --- | --- | --- |
| GET/POST | `/v1/Conversations` | List (`State` filter) / create (`UniqueName` must be unique, else `409` / 50353; `Attributes` must be a JSON object string) |
| GET/POST/DELETE | `/v1/Conversations/{CH or unique_name}` | Fetch / update (`State` is `active`/`inactive`/`closed`) / delete |
| GET/POST | `/v1/Conversations/{CH}/Participants` | Chat (`Identity`) or non-chat (`MessagingBinding.Address` + `ProxyAddress`); duplicates `409` / 50433 and 50416 |
| GET/POST/DELETE | `/v1/Conversations/{CH}/Participants/{MB}` | Fetch / update read state / remove |
| GET/POST | `/v1/Conversations/{CH}/Messages` | List (by `index`, `Order=asc\|desc`) / post (`Author`, `Body`) |
| GET/POST/DELETE | `/v1/Conversations/{CH}/Messages/{IM}` | Fetch / edit / delete |

Message `index` values are never reused, and `delivery` is Twilio's
`all`/`some`/`none` summary over the *non-chat* (SMS/WhatsApp) recipients — `null`
when there are none. Posting a Conversations message does not also create a
Programmable Messaging record (see Known gaps).

## Seeded data

All SIDs below are stable for the default seed. The account is
**Halcyon Advisory Group** (the world organisation).

**Numbers**

| Number | SID | Friendly name |
| --- | --- | --- |
| `+14165550188` | `PNd66f1e0771ecbffbf8346e189ce46b88` | Halcyon Client Line (Toronto, SMS/MMS/voice) |
| `+12125550188` | `PN1d3b03cd74fe5f7d75a36688a2ad5597` | Halcyon New York Line |
| `+14165550177` | `PNde67ca0c9f86bb1a0ac91e65ccba2a26` | Halcyon WhatsApp Sender (use as `whatsapp:+14165550177`; the only valid WhatsApp `From`) |

All numbers start with empty `sms_url`/`voice_url` — set them via the
IncomingPhoneNumbers update endpoint before simulating inbound traffic.

**Services**

- Messaging Service **Halcyon Client Notifications**
  `MG083ef8542df9f3b4f1400d6d21890e26`, pool = both SMS numbers.
- Verify Service **Halcyon Client Portal**
  `VA7ed05c28ddfbfbc6174e678f6a0a19a4` (lookup + custom codes enabled).
- Chat Service `IS1356539044eca1c65c5b96226a65f437` (Conversations).

**Message history** — 105 messages: 99 projected from the world's SMS/WhatsApp
conversations, four reminders and two inbound MMS (an `image/png` and an
`application/pdf`, downloadable from their Media URLs): the firm's number ↔ client contacts, e.g.
Christopher Ingram (`+14165550141`, christopher.ingram@northwindlogistics.example),
Stephanie Pearson (`+19025550144`), Kenji Walsh (`+14035550148`). The first
seeded message is `SMb7f24798069a3c9ec59a51106650b77f`
(`+14165550188 → +14165550141`, "Hi Christopher, this is Noah Williams…").
Outbound messages are handed to the carrier two seconds after creation
(`date_sent` follows `date_created`); SMS is `delivered` and WhatsApp history is
`read`. Four extra appointment-reminder sends went through the Messaging
Service (mornings, two days before the anchor date), so
`Messages?From=+14165550188` mixes direct and service sends; WhatsApp history
is addressable with `From=whatsapp:+14165550177`.

**Calls** — three completed outbound calls from `+14165550188`, e.g.
`CA3f0c55638cbd314ea2055b3909b60bdc` to `+14165550141` (90 s) with recording
`REb2b70511ea23363e4125661209db165d`.

**Conversations** — the same 18 SMS/WhatsApp threads as Conversations API
resources, e.g. `CHc6cdf0f8602212d2e8be244d8301115f`
(`unique_name: conv-sms-001`), each with a `halcyon-advisory` chat participant
and an SMS/WhatsApp-binding participant for the contact.

## Webhooks and signature scheme

Every webhook is signed with
`X-Twilio-Signature: base64(HMAC-SHA1(auth_token, url + concat(sorted(key +
value))))`, byte-for-byte what `twilio.request_validator.RequestValidator`
recomputes (verified by the test suites). `POST` webhooks carry a
`application/x-www-form-urlencoded; charset=UTF-8` form body; **`GET` webhooks**
(a number configured with `SmsMethod=GET`, a call with
`StatusCallbackMethod=GET`) carry the parameters in the query string and the
signature covers that full URL (validate with an empty parameter set). All carry
`User-Agent: TwilioProxy/1.1` and an `I-Twilio-Idempotency-Token`. Deliveries go
through the core webhook dispatcher, so they honor `EMULATORS_WEBHOOK_MODE` and
appear in `GET /_emulator/webhooks`; each delivery is attempted once (Twilio
does not retry HTTP errors).

- **Message status callbacks** (`StatusCallback`, falling back to the
  Messaging Service's): `MessageSid`, `SmsSid`,
  `AccountSid`, `From`, `To`, `MessageStatus`, `SmsStatus`, `ApiVersion`,
  `MessagingServiceSid` (when applicable), plus `ErrorCode` on failure and
  `RawDlrDoneDate` on final states.
- **Inbound messages** (number `SmsUrl` or service `inbound_request_url`):
  for SMS the full Twilio parameter set — `ToCountry`, `ToState`, `SmsMessageSid`,
  `NumMedia`, `ToCity`, `FromZip`, `SmsSid`, `FromState`, `SmsStatus`,
  `FromCity`, `Body`, `FromCountry`, `To`, `MessagingServiceSid` (when the
  number is pooled), `ToZip`, `NumSegments`, `MessageSid`, `AccountSid`,
  `From`, `ApiVersion`, and per-attachment `MediaUrl{i}`,
  `MediaContentType{i}`, `MediaSid{i}` (absolute, downloadable URLs).
  Cities/countries are derived from area codes. **WhatsApp** inbound uses
  WhatsApp's set instead: `ProfileName`, `WaId`, `MessageType`,
  `ReferralNumMedia`, `NumMedia`, … (no geography fields). If the endpoint
  fails (non-2xx), the number's `SmsFallbackUrl` is called with `ErrorCode=11200`
  and `ErrorUrl`. If the endpoint replies with
  TwiML `<Response><Message to? from?>text<Media>url</Media></Message>…`,
  each `<Message>` becomes an `outbound-reply` Message (TwiML replies are
  processed in `sync` and `async` webhook modes).
- **Advanced Opt-Out**: inbound `STOP`/`STOPALL`/`UNSUBSCRIBE`/`CANCEL`/
  `END`/`QUIT` opts the sender out (auto-reply "You have successfully been
  unsubscribed…", not forwarded to your webhook); later sends to that number
  from that sender/service fail with 400 code 21610. `START`/`YES`/`UNSTOP`
  opts back in; `HELP`/`INFO` gets the help reply.
- **Call status callbacks**: `CallSid`, `AccountSid`, `From`, `To`, `Called`,
  `Caller`, `CallStatus`, `ApiVersion`, `Direction`, `CallbackSource`,
  `SequenceNumber`, `Timestamp`, city/state/country fields, and on completion
  `CallDuration`, `Duration`, `SipResponseCode` — for each subscribed
  `StatusCallbackEvent` (`initiated`, `ringing`, `answered`, `completed`;
  default `completed` only).

## Actions (`POST /_emulator/actions/…`)

| Action | Payload | Effect |
| --- | --- | --- |
| `inbound_sms` | `{"from", "to", "body", "media_urls"?}` | Creates an inbound (`received`) Message and POSTs the destination's webhook; returns `{message, replies, webhook}` (`opted_out`/`opted_in` flags for keywords). WhatsApp works with `whatsapp:` addresses. Values are coerced to text, so wrongly typed JSON never 500s. |
| `advance_message` | `{"sid", "status"?}` | Advances a message along the delivery chain (firing callbacks), optionally to a named status (`queued` releases a scheduled message now; `sending`, `sent`, `delivered`, `undelivered`, `failed`, `canceled` (scheduled only), `read`). Unknown statuses and backwards moves (e.g. `delivered` → `sending`) are 400 |
| `inbound_call` | `{"from", "to"}` | Creates an inbound Call and POSTs the number's `VoiceUrl` |
| `advance_call` | `{"sid", "status"?}` | Advances a call's status (`ringing`, `in-progress`, `completed`, `busy`, `no-answer`, `canceled`, `failed`), firing progress callbacks; moves out of a finished call or backwards are 400 |
| `mint_token` | `{"test"?}` | Backs `POST /_emulator/tokens`: returns the account's Basic credentials |

## Known gaps

- TwiML is executed only for inbound-message `<Message>` replies; voice TwiML
  (`<Dial>`, `<Gather>`, `<Record>`, …) is fetched/accepted but not
  interpreted. Conferences, Queues, Addresses, Applications, OutgoingCallerIds,
  ConnectApps, Notifications, Transcriptions and ShortCodes exist only as
  always-empty lists (no create/update), and there are no SIP endpoints.
- Only the `api` (2010), Messaging, Verify, Lookups and Conversations products
  are implemented — no Studio, Sync, TaskRouter, Notify, Flex, Voice
  recordings transcription, Short Codes subresources, Alpha Sender management,
  Conversations webhooks/scoped webhooks (the `webhooks` link is dangling),
  or Verify rate-limits/entities/TOTP/push subresources.
- **Test credentials** are an alias of the main account and may call every
  endpoint; real test credentials only work on Messages, Calls and
  IncomingPhoneNumbers (anything else is 403 / 20008). The magic numbers also
  apply with live credentials (documented above), and live-credential sends from
  the account's own numbers are accepted with test credentials too.
- Recording `.mp3` downloads are valid but silent MPEG audio; `.wav` and the
  bare media URL return real WAV. A call that completes by itself reports an
  `end_time` up to ~3 minutes after the emulator clock (its simulated
  duration); calls held in progress by a test use real clock time instead.
- Outbound `MediaUrl` content is never fetched from the public internet; the
  emulator renders a small valid file of the content type implied by the URL's
  extension (PNG, JPEG, GIF, PDF, WAV, MP3, MP4, CSV, Markdown, vCard, else
  text), so the bytes always match the advertised `content_type`.
- Some numeric error codes are best-effort: the HTTP statuses follow the
  documented behaviour but the codes could not all be checked against a live
  account — scheduling (35114 window/format, 35115 `ScheduleType`), content
  templates (21655), number purchase (21421/21422/21452), sender pools
  (21710/21712) and Conversations conflicts (50353, 50416, 50433, 50057).
- Opt-out keywords are always intercepted (never forwarded to your webhook);
  Twilio's Advanced Opt-Out forwards them with an `OptOutType` parameter.
- Lookup returns only the packages listed above; unknown `Fields` values are
  ignored rather than rejected, and coverage of carrier data and country
  dialling plans is a small fictional subset. Valid but unlisted calling codes
  are treated as invalid country codes.
- Verify `call`/`email` channels create the verification (and accept checks)
  but do not place a call or send mail; only `sms`/`whatsapp` create a
  Message record. Creating a Conversations message does not send an SMS.
- Webhook responses other than the inbound-message TwiML are not interpreted
  (a voice `Url` replying with TwiML does not change the call).
- Prices are flat per segment/minute approximations, not Twilio's rate card.

