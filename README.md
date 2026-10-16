# emulators

**High-fidelity local emulators of the SaaS APIs our products integrate with** — so integration
tests can run against realistic users, org charts, folder hierarchies, permissions, meetings,
transcripts, invoices and conversations, with the vendors' exact wire formats, and without a
single vendor account.

| Category | Emulated services | Highlights |
|---|---|---|
| Identity & directory | Microsoft Entra ID, Google Workspace Directory | OAuth 2.0/OIDC (code + PKCE, refresh, client credentials, device code, service accounts), RS256 JWTs + JWKS, users, groups, org units, manager chains |
| Storage | OneDrive & SharePoint, Google Drive, Box, Dropbox | folder trees, shared drives/sites/team folders, ACLs and sharing links enforced per user, uploads (simple, multipart, resumable/chunked), delta/changes feeds, real DOCX/XLSX/PPTX/PDF content |
| Email & calendar | Outlook (Graph), Gmail, Google Calendar | mailboxes with folders/labels, MIME, attachments, send, delta/history; calendars with recurrence, free/busy |
| Meetings (audio/video) | Microsoft Teams, Zoom, Google Meet, Webex | online meetings, participants, cloud recordings (valid MP4/M4A), transcripts (WebVTT), attendance, webhooks |
| Messaging | Slack, Teams chat, Twilio (SMS/MMS, Verify, Lookup), WhatsApp Cloud API, Messenger | channels/threads/DMs, inbound simulation, delivery status callbacks, 24-hour windows, opt-outs, signed webhooks |
| Payments | Stripe | customers, payment methods, PaymentIntents state machine with test cards, subscriptions, invoices, refunds, disputes, events + signed webhooks, test clocks |
| CRM | Salesforce, HubSpot | SOQL/SOSL, composite, Bulk 2.0, describe, role-hierarchy sharing; HubSpot CRM v3 objects, associations, search, pipelines |
| Accounting | QuickBooks Online, Xero | chart of accounts, customers/jobs, invoices, payments, bills, reports that tie out, CDC, webhooks |

Every emulator is seeded from **one shared synthetic organisation** — *Halcyon Advisory Group*, a
45-person accounting and advisory firm with 24 clients — so the same person, client, document
and meeting line up across every system. All data is fictional (`*.example` emails, fictional
phone ranges).

## Quick start

```bash
uv sync
uv run emulators serve            # gateway http://localhost:44080 (+ HTTPS :44443, per-provider ports)
open http://localhost:44080/_emulator/ui
```

or with Docker:

```bash
docker compose up -d --build      # vendor hostnames as network aliases, see docs/howto/docker-compose.md
```

### From Python tests — keep the real URLs

```python
import httpx
from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint

hub = EmulatorHub(providers=["zoom"], anchor="2026-10-16")

async def test_import_recordings():
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        token = await mint(client, "https://api.zoom.us", user="maya.chen@halcyonadvisory.example")
        r = await client.get("https://api.zoom.us/v2/users/me/recordings", headers=bearer(token),
                             params={"from": "2026-09-20", "to": "2026-10-16"})
        assert r.json()["meetings"]
```

Official SDKs work unchanged through the `emulator_intercept` pytest fixture, which reroutes
httpx/requests/urllib/httplib2/aiohttp traffic for vendor hostnames to a local server:

```python
def test_stripe(emulator_intercept):
    import stripe
    stripe.api_key = "sk_test_emulator"
    assert stripe.Customer.list(limit=3).data
```

## How it works

* **Hostname routing.** A request for `graph.microsoft.com`, `api.stripe.com`,
  `halcyonadvisory-my.sharepoint.com`… is routed to the right emulator by `Host`. That's what makes
  the in-process transport, SDK interception, Docker network aliases and the HTTPS gateway (local
  CA via `emulators certs`) all work without changing consumer URLs. Path-prefix
  (`/microsoft/v1.0/me`) and per-provider ports are available for clients with a base-URL setting.
* **Realistic state.** Each provider projects the world into its own model with stable ids, then
  serves it from an in-memory store with snapshot-based `reset`.
* **Auth like the vendor.** Real OAuth/OIDC endpoints (authorize with `login_hint` auto-consent or
  an account picker, token, refresh, revoke, JWKS) plus `POST /_emulator/tokens` for instant tokens.
* **Permissions enforced.** Delegated tokens only see what that user could see (drive ACLs, shared
  drives, private channels, CRM sharing rules, mailbox ownership); app tokens see what apps can.
* **Webhooks with real signatures.** Stripe, Zoom, Slack, Twilio, Meta, Dropbox, Box, Graph,
  Google push, QuickBooks, Xero, HubSpot, Webex — with handshake/validation flows, retries and a
  delivery journal. Deliver over the network or straight into an ASGI app in-process.
* **Control plane** on every provider: `/_emulator/{health,info,reset,state,requests,faults,tokens,webhooks,actions,docs}`.
  Inject 429s/500s/latency, inspect every request your code made, simulate inbound SMS, end a
  meeting to generate its recording and transcript, advance the clock…

## The synthetic world

```bash
uv run emulators world            # summary + vendor ids (tenant, realm, account sids…)
uv run emulators world people     # 133 people: employees (org chart), client and vendor contacts
uv run emulators world meetings --json
```

Useful fixtures: Maya Chen (`p018`, Partner, Advisory, New York), Eleanor Whitfield (`p001`, CEO),
Arjun Mehta (`p035`, Director of Technology, tenant admin), departed employees `p046`/`p047`,
clients `c001` Northwind Logistics … `c024` Foxglove Fashion House. Dates are relative to the anchor
date; pin it with `EMULATORS_ANCHOR=2026-10-16` for byte-stable fixtures.

## Documentation

* [Architecture](docs/architecture.md) · [Use from Python tests](docs/howto/python-tests.md) ·
  [Docker with real hostnames](docs/howto/docker-compose.md)
* Provider references: [docs/providers/](docs/providers/) — hosts, auth, endpoints, seeded data,
  webhooks, actions and known gaps for each emulator.
* Decisions: [0001 hostname routing](docs/decisions/0001-hostname-routing.md) ·
  [0002 shared synthetic world](docs/decisions/0002-shared-synthetic-world.md)
* Contributing: [AGENTS.md](AGENTS.md)

## Development

```bash
uv sync
uv run pytest -q                       # unit, provider, SDK-contract and consumer-flow tests
uv run ruff check . && uv run ruff format --check .
uv run python scripts/gen_compose.py   # after adding hostnames
```

License: MIT.
