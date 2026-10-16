# Architecture

```
                         ┌──────────────────────────── EmulatorHub ────────────────────────────┐
 consumer code           │  World (one synthetic firm, deterministic per seed + anchor date)   │
 (unchanged URLs)        │      │ seed(ctx) per provider                                        │
                         │      ▼                                                               │
 https://graph.microsoft.com ──► gateway ── Host routing ──► microsoft app ─┐                   │
 https://api.stripe.com      ──►   │                         stripe app    ─┤ each app =        │
 http://localhost:44080/zoom ──►   └─ path-prefix routing ─► zoom app      ─┤ vendor routes     │
 http://localhost:44106      ──────────── dedicated port ──► zoom app      ─┘ + /_emulator/*    │
                         │                                                                     │
                         │  per provider: Store (collections + blobs) · TokenService · Clock   │
                         │  · IdFactory · WebhookDispatcher · RequestJournal · FaultInjector   │
                         └─────────────────────────────────────────────────────────────────────┘
```

## Building blocks (`src/emulators/core`)

| Module | Responsibility |
|---|---|
| `hub.py` | Discovers providers, builds the world, creates one `ProviderContext` per provider, seeds and snapshots state, builds the gateway (host routing, then path-prefix routing, plus `/_emulator` hub endpoints: index, health, reset-all, clock). |
| `provider.py` | The `Provider` base class (name, hosts, default port, `seed`, `build`, `error_body`, actions) and URL helpers: `external_base(request)` and `url_for_host(request, ctx, host, path)`. |
| `app.py` | Builds a provider's FastAPI app: exception handlers that render vendor error envelopes, the control plane router, and the middleware. |
| `middleware.py` | Pure-ASGI middleware: computes the client-visible base URL, records the request journal, applies fault rules (status, latency, `Retry-After`). |
| `store.py` | `Store` of named `Collection`s holding records in the vendor's wire shape, a content-addressed `BlobStore`, snapshot/restore. |
| `auth.py` | `TokenService` (clients, codes with PKCE, access/refresh tokens, expiry against the hub clock), RS256 `SigningKey` with JWKS, account-picker page. |
| `webhooks.py` | `WebhookDispatcher` with `async`/`sync`/`manual` modes, retries, per-attempt signing callbacks, delivery journal; HMAC helpers. |
| `odata.py` | OData v4 `$filter/$orderby/$select/$expand/$search/$top/$skip/$count` engine for Graph. |
| `clock.py`, `ids.py`, `errors.py`, `tls.py` | Controllable clock; deterministic ids (GUIDs, Stripe prefixes, Salesforce 18-char ids...); `EmulatorError`; local CA for the HTTPS gateway. |

## The world (`src/emulators/world`)

`build_world(seed, anchor)` returns a frozen description of *Halcyon Advisory Group*: offices,
departments, an org chart of 47 employees (2 departed), 24 client companies with contacts, vendors,
engagements, directory groups, collaboration teams with channels and messages, direct chats,
document spaces (team areas with group-based membership and personal drives) holding ~360 real
documents with folder hierarchy, explicit shares and restricted items, ~165 meetings over a
nine-week window (past ones with participation records, recordings and transcripts), ~180 email
threads, a chart of accounts, ~115 invoices with payments, ~150 vendor bills, CRM deals, leads and
cases, Stripe products and card-paying customers, and SMS/WhatsApp/Messenger conversations.

Providers translate it into their own models — an `Engagement` becomes a QuickBooks sub-customer,
a Salesforce `Engagement__c`, a Teams team, a Slack channel and a folder in every storage
provider. Dates are relative to the anchor (midnight UTC of the anchor date), so the data always
looks recent; pin `EMULATORS_ANCHOR` for byte-stable fixtures.

`documents.py` and `media.py` render real files: DOCX/XLSX/PPTX/PDF/CSV/Markdown/PNG and
WAV/M4A/MP4 (valid AAC)/WebVTT/SRT, so downstream parsers, transcription and diarization
pipelines get realistic inputs.

## Routing modes

1. **In-process transport** (Python tests): `httpx.AsyncClient(transport=hub.async_transport())`.
   Requests keep their real URLs; the gateway routes by `Host`.
2. **Interception** (official SDKs and code without a transport seam):
   `emulators.testing.intercept.intercept(EmulatorServer(hub))` patches httpx, requests, urllib,
   httplib2 and aiohttp to send vendor hostnames to a local server, preserving `Host` and
   `X-Forwarded-Proto` so generated links stay on the real hostnames.
3. **HTTPS gateway with real hostnames** (containers, other languages): resolve vendor hostnames
   to the emulator (Docker network aliases or hosts file) and trust the generated CA.
4. **Base URL override**: `http://localhost:44080/<provider>/...` or `http://localhost:<port>/...`
   for clients that let you configure the API base.

Absolute URLs in responses (`@odata.nextLink`, `download_url`, `instance_url`...) are always built
from the base the client used, so every mode round-trips.

## Isolation and determinism

* Ids are derived from the seed and world ids; seeding is deterministic and takes well under a
  second per provider.
* `POST /_emulator/reset` restores the post-seed snapshot (deep copy); blobs are shared.
* The hub clock can be frozen or advanced (`POST /_emulator/clock`), which drives token expiry,
  subscription renewals, message status progressions and "recent" data.
