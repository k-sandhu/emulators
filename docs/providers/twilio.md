# Twilio

The `twilio` provider emulates one Twilio account for Halcyon Advisory Group:
Programmable Messaging (SMS/MMS/WhatsApp), Messaging Services, Verify v2, Lookup
v2, basic Programmable Voice (Calls + Recordings) and the Conversations API. It
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
  paging envelope (`PageSize` default 50, max 1000).
- **v1/v2 APIs** (Messaging, Verify, Lookup, Conversations): ISO-8601 dates
  (`2026-10-16T14:00:00Z`), absolute `url` fields, and the
  `{"<key>": [...], "meta": {"page", "page_size", "first_page_url", …}}`
  paging envelope.
- **Errors**: `{"code": 21211, "message": …, "more_info":
  "https://www.twilio.com/docs/errors/21211", "status": 400}` everywhere,
  including for injected faults and framework 404s (code 20404).

## Auth flows and test credentials

HTTP Basic auth, exactly like Twilio. Three credential types authenticate
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
  this when constructed as `Client(key_sid, secret, account_sid)`).
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
- Wrong `{AccountSid}` in a path → `404` code 20404. Subaccounts created with
  `POST /2010-04-01/Accounts.json` are fully isolated: they see none of the
  parent's messages, calls or numbers, and cannot send from the parent's
  numbers (21606).

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

## Endpoints

Base below is `https://api.twilio.com` unless another host is named.
`{A}` = account SID.

### Accounts, keys, balance, usage

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/2010-04-01/Accounts.json` | List accounts (filters `FriendlyName`, `Status`) |
| POST | `/2010-04-01/Accounts.json` | Create a subaccount |
| GET | `/2010-04-01/Accounts/{A}.json` | Friendly name, status `active`, type `Full` |
| POST | `/2010-04-01/Accounts/{A}.json` | Update `FriendlyName`, `Status` |
| GET | `/2010-04-01/Accounts/{A}/Balance.json` | `{"balance": "152.34", "currency": "USD"}` |
| GET | `/2010-04-01/Accounts/{A}/Usage/Records.json` | `sms`, `calls`, `totalprice` computed from live state |
| POST/GET | `/2010-04-01/Accounts/{A}/Keys.json` | Create/list API keys (secret shown once) |
| GET/DELETE | `/2010-04-01/Accounts/{A}/Keys/{SK}.json` | Fetch/revoke (POST `…/SigningKeys.json` aliased) |

### Messages and media

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/2010-04-01/Accounts/{A}/Messages.json` | Create; see below |
| GET | `/2010-04-01/Accounts/{A}/Messages.json` | Filters `To`, `From`, `DateSent`, `DateSent<`, `DateSent>` (date or datetime); newest first |
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
sticky per recipient). `From` must be one of the account's numbers (or the
magic test sender, or an alphanumeric sender ID) — otherwise 21606. Body over
1600 characters → 21617; more than 10 `MediaUrl`s → 400; `SendAt` +
`ScheduleType=fixed` schedules the message and requires a Messaging Service
(21704 without one). `num_segments` follows the real GSM-7 (160/153, extension
characters count double) and UCS-2 (70/67, UTF-16 code units) rules;
`num_media`/`num_segments` are strings, like Twilio.

The create response is `201` with the **initial** status (`queued`, or
`accepted` via a Messaging Service, or `scheduled`), exactly like Twilio —
then the message progresses `queued → sending → sent → delivered`
(or `undelivered` for the `…0199`/`…0198` simulators), firing the
StatusCallback at each step. In `sync` webhook mode the whole progression has
completed by the time the create call returns, so an immediate `GET` shows
`delivered` with `date_sent` and a negative `price`. In `async` mode it
advances moments later; `POST /_emulator/actions/advance_message` forces it.

### Phone numbers

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/2010-04-01/Accounts/{A}/IncomingPhoneNumbers.json` | Filters `PhoneNumber`, `FriendlyName`, `Beta`; `…/Local|TollFree|Mobile.json` aliased |
| POST | `/2010-04-01/Accounts/{A}/IncomingPhoneNumbers.json` | Purchase by `PhoneNumber` or `AreaCode` |
| GET | `/2010-04-01/Accounts/{A}/IncomingPhoneNumbers/{PN}.json` | Fetch |
| POST | `/2010-04-01/Accounts/{A}/IncomingPhoneNumbers/{PN}.json` | Update `SmsUrl`, `VoiceUrl`, methods, fallbacks, `FriendlyName`, … |
| DELETE | `/2010-04-01/Accounts/{A}/IncomingPhoneNumbers/{PN}.json` | Release (204); removed from Messaging Service pools |
| GET | `/2010-04-01/Accounts/{A}/AvailablePhoneNumbers.json` | Country list (US, CA, GB) |
| GET | `/2010-04-01/Accounts/{A}/AvailablePhoneNumbers/{CC}.json` | One country |
| GET | `/2010-04-01/Accounts/{A}/AvailablePhoneNumbers/{CC}/Local.json` | Fictional `555-01XX` inventory; filters `AreaCode`, `Contains`, `SmsEnabled`; also `TollFree`, `Mobile`, `National`, `Voip`, … |

### Messaging Services — `https://messaging.twilio.com`

| Method | Path | Notes |
| --- | --- | --- |
| GET/POST | `/v1/Services` | List (meta paging) / create (`FriendlyName` required) |
| GET/POST/DELETE | `/v1/Services/{MG}` | Fetch / update / delete |
| GET/POST | `/v1/Services/{MG}/PhoneNumbers` | Sender pool; POST takes `PhoneNumberSid` |
| GET/DELETE | `/v1/Services/{MG}/PhoneNumbers/{PN}` | Fetch / remove from pool |

Inbound routing: if a pooled number receives a message and the service has an
`inbound_request_url`, that URL is used unless
`use_inbound_webhook_on_number` is true, in which case the number's own
`SmsUrl` wins. A service-level `status_callback` is used for its messages when
no per-message `StatusCallback` is given.

### Verify — `https://verify.twilio.com`

| Method | Path | Notes |
| --- | --- | --- |
| GET/POST | `/v2/Services` | List / create |
| GET/POST/DELETE | `/v2/Services/{VA}` | Fetch / update / delete |
| POST | `/v2/Services/{VA}/Verifications` | Start: `To`, `Channel` (`sms\|call\|email\|whatsapp`), `Locale`, `CustomCode`, `Amount`, `Payee` |
| GET | `/v2/Services/{VA}/Verifications/{VE}` | Fetch |
| POST | `/v2/Services/{VA}/Verifications/{VE}` | Update `Status=canceled\|approved` |
| POST | `/v2/Services/{VA}/VerificationCheck` | Check: `To` or `VerificationSid`, plus `Code` |

Verification codes are 6 digits (the service's `code_length`), valid for 10
minutes of emulator clock time, with at most 5 failed checks — after that (or
after expiry) checks return `404` code 20404. For tests, the real code is
visible as the `code` field in `GET /_emulator/state/verifications`, and the
universal code **`123456` is always accepted**. Starting an SMS/WhatsApp
verification also creates a normal Message record
(`"Your Halcyon Client Portal verification code is: …"`) from the firm's
number, so it shows up in `Messages` list/fetch like any other send.
`CustomCode` requires `custom_code_enabled` on the service (the seeded
service has it enabled).

### Lookup — `https://lookups.twilio.com`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/v2/PhoneNumbers/{number}?Fields=…` | Validation always included; packages `caller_name`, `line_type_intelligence`, `line_status`, `sms_pumping_risk`, `sim_swap` on request |

World people's mobiles resolve as `mobile` with their (upper-case) name as
`caller_name`; office/business numbers are `landline`; the account's own
Twilio numbers are `nonFixedVoip` / "Twilio Inc.". `+15005550001` and other
malformed numbers return `valid: false` with `validation_errors`.

### Voice

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/2010-04-01/Accounts/{A}/Calls.json` | Create: `To`, `From`, `Url` or `Twiml`, `StatusCallback`, `StatusCallbackEvent`, `Record` |
| GET | `/2010-04-01/Accounts/{A}/Calls.json` | Filters `To`, `From`, `Status`, `ParentCallSid`, `StartTime[<>]` |
| GET | `/2010-04-01/Accounts/{A}/Calls/{CA}.json` | Fetch |
| POST | `/2010-04-01/Accounts/{A}/Calls/{CA}.json` | `Status=completed` hangs up, `Status=canceled` cancels a ringing call; `Url`/`Twiml` accepted |
| GET/POST | `/2010-04-01/Accounts/{A}/Calls/{CA}/Recordings.json` | Call recordings |
| GET | `/2010-04-01/Accounts/{A}/Recordings.json` | List (filter `CallSid`) |
| GET/DELETE | `/2010-04-01/Accounts/{A}/Recordings/{RE}.json` | Fetch / delete |
| GET | `/2010-04-01/Accounts/{A}/Recordings/{RE}` (also `.wav`, `.mp3`) | Audio bytes (real WAV, generated lazily) |

Calls progress `queued → ringing → in-progress → completed` with a
deterministic duration (20–200 s derived from the call SID). When the call is
answered, the `Url` is POSTed with the standard call parameters (the TwiML
response is accepted but not executed — see Known gaps). `Record=true`
produces a Recording on completion.

### Conversations — `https://conversations.twilio.com`

| Method | Path | Notes |
| --- | --- | --- |
| GET/POST | `/v1/Conversations` | List / create (`UniqueName` must be unique) |
| GET/POST/DELETE | `/v1/Conversations/{CH or unique_name}` | Fetch / update / delete |
| GET/POST | `/v1/Conversations/{CH}/Participants` | Chat (`Identity`) or non-chat (`MessagingBinding.Address` + `ProxyAddress`) |
| GET/POST/DELETE | `/v1/Conversations/{CH}/Participants/{MB}` | Fetch / update read state / remove |
| GET/POST | `/v1/Conversations/{CH}/Messages` | List (by `index`) / post (`Author`, `Body`) |
| GET/POST/DELETE | `/v1/Conversations/{CH}/Messages/{IM}` | Fetch / edit / delete |

## Seeded data

All SIDs below are stable for the default seed. The account is
**Halcyon Advisory Group** (the world organisation).

**Numbers**

| Number | SID | Friendly name |
| --- | --- | --- |
| `+14165550188` | `PNd66f1e0771ecbffbf8346e189ce46b88` | Halcyon Client Line (Toronto, SMS/MMS/voice) |
| `+12125550188` | `PN1d3b03cd74fe5f7d75a36688a2ad5597` | Halcyon New York Line |
| `+14165550177` | `PNde67ca0c9f86bb1a0ac91e65ccba2a26` | Halcyon WhatsApp Sender (use as `whatsapp:+14165550177`) |

All numbers start with empty `sms_url`/`voice_url` — set them via the
IncomingPhoneNumbers update endpoint before simulating inbound traffic.

**Services**

- Messaging Service **Halcyon Client Notifications**
  `MG083ef8542df9f3b4f1400d6d21890e26`, pool = both SMS numbers.
- Verify Service **Halcyon Client Portal**
  `VA7ed05c28ddfbfbc6174e678f6a0a19a4` (lookup + custom codes enabled).
- Chat Service `IS1356539044eca1c65c5b96226a65f437` (Conversations).

**Message history** — 103 messages projected from the world's SMS/WhatsApp
conversations: the firm's number ↔ client contacts, e.g.
Christopher Ingram (`+14165550141`, christopher.ingram@northwindlogistics.example),
Stephanie Pearson (`+19025550144`), Kenji Walsh (`+14035550148`). The first
seeded message is `SMb7f24798069a3c9ec59a51106650b77f`
(`+14165550188 → +14165550141`, "Hi Christopher, this is Noah Williams…").
Four extra appointment-reminder sends went through the Messaging Service, so
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

Every webhook is a form-encoded POST signed with
`X-Twilio-Signature: base64(HMAC-SHA1(auth_token, url + concat(sorted(key +
value))))`, byte-for-byte what `twilio.request_validator.RequestValidator`
recomputes (verified by the test suites). Deliveries go through the core
webhook dispatcher, so they honor `EMULATORS_WEBHOOK_MODE` and appear in
`GET /_emulator/webhooks`.

- **Message status callbacks** (`StatusCallback`, falling back to the
  Messaging Service's, then the number's): `MessageSid`, `SmsSid`,
  `AccountSid`, `From`, `To`, `MessageStatus`, `SmsStatus`, `ApiVersion`,
  `MessagingServiceSid` (when applicable), plus `ErrorCode` on failure and
  `RawDlrDoneDate` on final states.
- **Inbound messages** (number `SmsUrl` or service `inbound_request_url`):
  the full Twilio parameter set — `ToCountry`, `ToState`, `SmsMessageSid`,
  `NumMedia`, `ToCity`, `FromZip`, `SmsSid`, `FromState`, `SmsStatus`,
  `FromCity`, `Body`, `FromCountry`, `To`, `MessagingServiceSid` (when the
  number is pooled), `ToZip`, `NumSegments`, `MessageSid`, `AccountSid`,
  `From`, `ApiVersion`, and per-attachment `MediaUrl{i}`,
  `MediaContentType{i}`, `MediaSid{i}` (absolute, downloadable URLs).
  Cities/countries are derived from area codes. If the endpoint replies with
  TwiML `<Response><Message to? from?>text<Media>url</Media></Message>…`,
  each `<Message>` becomes an `outbound-reply` Message (TwiML replies are
  processed in `sync` and `async` webhook modes).
- **Advanced Opt-Out**: inbound `STOP`/`STOPALL`/`UNSUBSCRIBE`/`CANCEL`/
  `END`/`QUIT` opts the sender out (auto-reply "You have successfully been
  unsubscribed…", not forwarded to your webhook); later sends to that number
  from that sender/service fail with 400 code 21610. `START`/`YES`/`UNSTOP`
  opts back in; `HELP`/`INFO` gets the help reply.
- **Call status callbacks**: `CallSid`, `AccountSid`, `From`, `To`,
  `CallStatus`, `ApiVersion`, `Direction`, `CallbackSource`,
  `SequenceNumber`, `Timestamp`, and on completion `CallDuration`,
  `Duration`, `SipResponseCode` — for each subscribed
  `StatusCallbackEvent` (`initiated`, `ringing`, `answered`, `completed`;
  default `completed` only).

## Actions (`POST /_emulator/actions/…`)

| Action | Payload | Effect |
| --- | --- | --- |
| `inbound_sms` | `{"from", "to", "body", "media_urls"?}` | Creates an inbound (`received`) Message and POSTs the destination's webhook; returns `{message, replies, webhook}` (`opted_out`/`opted_in` flags for keywords). WhatsApp works with `whatsapp:` addresses. |
| `advance_message` | `{"sid", "status"?}` | Advances a message along the delivery chain (firing callbacks), optionally to a named status |
| `inbound_call` | `{"from", "to"}` | Creates an inbound Call and POSTs the number's `VoiceUrl` |
| `advance_call` | `{"sid", "status"?}` | Advances a call's status, firing progress callbacks |
| `mint_token` | `{"test"?}` | Backs `POST /_emulator/tokens`: returns the account's Basic credentials |

## Known gaps

- TwiML is executed only for inbound-message `<Message>` replies; voice TwiML
  (`<Dial>`, `<Gather>`, `<Record>`, …) is fetched/accepted but not
  interpreted, and there are no Conferences, Queues, or SIP endpoints.
- Only the `api` (2010), Messaging, Verify, Lookups and Conversations products
  are implemented — no Studio, Sync, TaskRouter, Notify, Flex, Voice
  recordings transcription, Short Codes subresources, Alpha Sender management,
  incoming-number `Local/TollFree/Mobile` type filtering (they alias the flat
  list), or Verify rate-limits/entities/TOTP/push subresources.
- Recording `.mp3` downloads return WAV bytes with an `audio/mpeg` content
  type (no MP3 encoder); `.wav` and the bare media URL return real WAV.
- Outbound `MediaUrl` content is never fetched from the public internet; the
  emulator renders a local PNG/PDF/text body based on the URL's extension.
- Scheduled messages are never auto-sent when `SendAt` passes — cancel them,
  or advance them explicitly with the `advance_message` action.
- Lookup returns only the packages listed above; unknown `Fields` values are
  ignored rather than rejected, and coverage of carrier data is fictional.
- Verify `call`/`email` channels create the verification (and accept checks)
  but do not place a call or send mail; only `sms`/`whatsapp` create a
  Message record.
- Prices are flat per segment/minute approximations, not Twilio's rate card.
