# Consumer examples

From the repository root, install the existing development and SDK groups:

```powershell
uv sync
```

All examples use fictional Halcyon data, seed `emulators` and anchor `2026-10-16`.
The Python programs create their own hubs and need no running service, vendor
account, credentials or internet connection. Each exposes a `main()` function,
asserts its outcome, and writes a small result when run as a script.

## Python: in-process HTTP

```powershell
uv run python examples/python_inprocess.py
```

`python_inprocess.py` creates an `EmulatorHub` for Microsoft and Stripe, mints a
delegated Graph token through the control plane and reads Maya Chen's profile and
Stripe customers. `httpx.AsyncClient(transport=hub.async_transport())` preserves
the real `https://graph.microsoft.com` and `https://api.stripe.com` URLs while all
requests execute in-process. `main()` is asynchronous.

## Python: official SDKs

```powershell
uv run python examples/official_sdks.py
```

`official_sdks.py` starts an `EmulatorServer` on a temporary localhost port and
wraps calls in `emulators.testing.intercept.intercept(server)`. MSAL obtains a
Microsoft app token, Google API discovery lists Directory users, Stripe's
`StripeClient` lists customers, and Slack's `WebClient` lists workspace users.
The clients retain their default vendor hosts. Google uses its bundled static
discovery document, so discovery also works offline. Interception and the server
are closed on exit. `main()` is synchronous.

## Python: signed webhooks into FastAPI

```powershell
uv run python examples/webhook_receiver.py
```

`webhook_receiver.py` builds a tiny FastAPI receiver and assigns its
`httpx.ASGITransport` to `hub.webhook_transport`. Creating a Stripe customer
delivers `customer.created`; creating a Zoom meeting delivers `meeting.created`.
The receiver verifies raw request bytes with `stripe.Webhook.construct_event`
and Zoom's `v0:{timestamp}:{body}` HMAC-SHA256 scheme, including a five-minute
timestamp window. Zoom's URL validation challenge is answered and verified too.
The Stripe secret comes from webhook endpoint registration; the Zoom secret is
synthetic example configuration. No webhook socket is opened. `main()` is
asynchronous; `create_app(secrets, received)` can also be reused in a test.

## Node 22: fetch over HTTPS

Start the gateway in one terminal:

```powershell
uv run emulators serve --providers microsoft --anchor 2026-10-16
```

The gateway creates a local CA and server certificate in `.emulators/certs`.
In a second terminal, trust that CA **before starting Node**:

```powershell
$env:NODE_EXTRA_CA_CERTS = (Resolve-Path .emulators/certs/ca.pem).Path
node examples/node/fetch-graph.mjs
```

For a POSIX shell:

```bash
NODE_EXTRA_CA_CERTS="$PWD/.emulators/certs/ca.pem" node examples/node/fetch-graph.mjs
```

`fetch-graph.mjs` mints a delegated token and fetches Maya's Graph profile using
the gateway's `/microsoft` prefix at `https://localhost:44443`. Set
`EMULATORS_GATEWAY` to use a different gateway URL. TLS verification stays
enabled. A custom `--certs-dir` requires the corresponding CA path. To preserve
real vendor URLs for a larger Node application, configure local DNS/hosts or
Docker network aliases to route those names to the HTTPS gateway and retain the
same CA trust setting; this standalone example needs no DNS changes.

## Regression checks

```powershell
uv run pytest tests/e2e/test_examples.py tests/e2e/test_cross_system.py -q
```

`test_examples.py` imports and runs all three Python entry points.
`test_cross_system.py` uses one resettable hub with all 16 providers, with vendor
hostnames and API-discovered IDs. It reconciles employee identities and departure
status, org charts, CRM clients, ledger invoices, Stripe card payments, real
DOCX/PDF/XLSX downloads, Notion/Confluence pages and restricted-document access,
calendars and meeting transcripts, and CRM phone numbers in SMS/WhatsApp traffic.

The immutable world supplies expected values; tests never read provider stores or
use control-plane state dumps. Vendor differences are preserved: Stripe uses
legal client names, QBO zeros voided invoices while Xero retains their face value,
Webex presence is separate from license/login state, and external Zoom/Meet guests
may lack email addresses. Transcript comparisons preserve speaker order and words
while allowing different VTT cue boundaries. WhatsApp Cloud API has no history
read endpoint: Twilio checks seeded WhatsApp history, and Meta checks signed
inbound webhook projection for the same world contacts.
