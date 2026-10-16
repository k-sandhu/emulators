# Use the emulators from Python tests

Install as a test dependency (it is also a pytest plugin):

```bash
uv add --dev "emulators @ git+https://github.com/k-sandhu/emulators"
```

## Option 1 — swap the httpx transport (fastest, no network)

Most of our services build their httpx clients with a `transport=` seam. Keep the real URLs:

```python
import httpx
from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint

hub = EmulatorHub(providers=["microsoft"], anchor="2026-10-16", webhook_mode="sync")

async def test_lists_my_teams_meetings():
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        token = await mint(client, "https://graph.microsoft.com", user="maya.chen@halcyonadvisory.example")
        r = await client.get(
            "https://graph.microsoft.com/v1.0/me/calendarView",
            params={"startDateTime": "2026-10-01T00:00:00Z", "endDateTime": "2026-10-31T00:00:00Z"},
            headers=bearer(token),
        )
        assert r.status_code == 200
```

The fixtures `emulators` (hub reset before each test) and `emulator_transport` do this for you.

## Option 2 — intercept everything (official SDKs, code without a seam)

```python
def test_stripe_checkout(emulator_intercept):
    import stripe
    stripe.api_key = "sk_test_emulator"
    customer = stripe.Customer.create(email="laura@example.com")
```

`emulator_intercept` starts the gateway on a free localhost port and reroutes httpx, requests,
urllib, httplib2 and aiohttp traffic for vendor hostnames to it. Limit the providers for speed:
`EMULATORS_PROVIDERS=stripe pytest`.

## Getting tokens

* `POST https://<vendor host>/_emulator/tokens` with `{"user": "p018"}` (world person id or email),
  optional `scopes`, `app_only`, `client_id`, `expires_in` — returns a token in the vendor's format.
* Or run the real OAuth flow: every authorize endpoint accepts `login_hint=<email>` and redirects
  straight back with a code; client ids and secrets are accepted leniently.

## Asserting what your code did

```python
journal = (await client.get("https://api.zoom.us/_emulator/requests", params={"path": "/v2/users"})).json()
deliveries = (await client.get("https://api.stripe.com/_emulator/webhooks")).json()["deliveries"]
```

## Faults

```python
await client.post("https://graph.microsoft.com/_emulator/faults",
                  json={"path": "/v1.0/me/messages", "status": 429, "times": 2, "headers": {"Retry-After": "1"}})
```

Rules match by regex on the path (and optionally method), can skip the first N matches (`after`),
fire with a probability, or only add latency (`status: null, delay_ms: 500`).

## Receiving webhooks in-process

```python
hub.webhook_transport = httpx.ASGITransport(app=my_fastapi_app)
```

Deliveries then go straight into your app; with `webhook_mode="sync"` the triggering request waits
for them, so assertions right after the call are deterministic.
