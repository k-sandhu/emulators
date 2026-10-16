from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, Request

from emulators.core.hub import EmulatorHub


async def test_webhook_creation_property_change_deletion_and_both_signatures(
    client: httpx.AsyncClient,
) -> None:
    received: list[tuple[dict[str, str], bytes]] = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> dict:
        received.append((dict(request.headers), await request.body()))
        return {}

    client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    secret = "fictional-webhook-secret"
    url = "http://consumer.example/hook?next=https%3A%2F%2Fclient.example%2Fa%3Fb%26c"
    configured = await client.post(
        "/_emulator/actions/configure_webhook",
        json={
            "target_url": url,
            "client_secret": secret,
            "subscriptions": [
                "contact.creation",
                {"id": 9, "subscriptionType": "contact.propertyChange", "propertyName": "firstname"},
                "contact.deletion",
            ],
        },
    )
    assert configured.status_code == 200 and "client_secret" not in configured.json()
    contact = (
        await client.post("/crm/v3/objects/contacts", json={"properties": {"firstname": "Before"}})
    ).json()
    await client.patch(
        "/crm/v3/objects/contacts/" + contact["id"],
        json={"properties": {"firstname": "After", "lastname": "Ignored"}},
    )
    await client.delete("/crm/v3/objects/contacts/" + contact["id"])
    assert len(received) == 3
    events = [json.loads(raw)[0] for _, raw in received]
    assert [e["subscriptionType"] for e in events] == [
        "contact.creation",
        "contact.propertyChange",
        "contact.deletion",
    ]
    assert events[1]["propertyName"] == "firstname" and events[1]["propertyValue"] == "After"
    assert events[1]["subscriptionId"] == 9
    for (headers, raw), event in zip(received, events, strict=True):
        assert event["objectId"] == int(contact["id"]) and event["portalId"] == 24681357
        assert event["attemptNumber"] == 0 and event["changeSource"] == "CRM"
        timestamp = headers["x-hubspot-request-timestamp"]
        uri = "http://consumer.example/hook?next=https://client.example/a?b%26c"
        expected = base64.b64encode(
            hmac.new(
                secret.encode(), b"POST" + uri.encode() + raw + timestamp.encode(), hashlib.sha256
            ).digest()
        ).decode()
        assert hmac.compare_digest(headers["x-hubspot-signature-v3"], expected)
        assert headers["x-hubspot-signature"] == hashlib.sha256(secret.encode() + raw).hexdigest()
    journal = (await client.get("/_emulator/webhooks")).json()
    assert journal["count"] == 3 and all(d["status"] == "delivered" for d in journal["deliveries"])


async def test_webhook_batches_are_capped_at_100_and_subscription_filters(client: httpx.AsyncClient) -> None:
    batches: list[list[dict]] = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> dict:
        batches.append(await request.json())
        return {}

    client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    await client.post(
        "/_emulator/actions/configure_webhook",
        json={
            "target_url": "http://consumer.example/hook",
            "client_secret": "secret",
            "subscriptions": [
                {"id": 1, "subscriptionType": "contact.creation"},
                {"id": 2, "subscriptionType": "contact.creation"},
                {"id": 3, "subscriptionType": "company.creation", "active": False},
            ],
        },
    )
    result = await client.post(
        "/crm/v3/objects/contacts/batch/create",
        json={"inputs": [{"properties": {"firstname": f"Contact {i}"}} for i in range(100)]},
    )
    assert result.status_code == 201
    assert [len(batch) for batch in batches] == [100, 100]
    assert len({e["eventId"] for batch in batches for e in batch}) == 200
    await client.post("/crm/v3/objects/companies", json={"properties": {"name": "Unsubscribed"}})
    assert len(batches) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"target_url": "ftp://consumer.example/hook", "client_secret": "x"},
        {"target_url": "https://consumer.example/hook"},
        {
            "target_url": "https://consumer.example/hook",
            "client_secret": "secret",
            "subscriptions": ["unknown.event"],
        },
    ],
)
async def test_invalid_webhook_configuration(client: httpx.AsyncClient, payload: dict) -> None:
    assert (await client.post("/_emulator/actions/configure_webhook", json=payload)).status_code == 400


async def test_fault_envelope_headers_journal_and_reset(client: httpx.AsyncClient) -> None:
    await client.post(
        "/_emulator/faults", json={"path": "/crm/v3/objects/contacts", "status": 429, "times": 1}
    )
    throttled = await client.get("/crm/v3/objects/contacts")
    assert throttled.status_code == 429 and throttled.json()["category"] == "RATE_LIMITS"
    assert throttled.headers["retry-after"] == "2"
    assert throttled.headers["x-hubspot-ratelimit-remaining"] == "0"
    assert throttled.headers["x-hubspot-ratelimit-interval-milliseconds"] == "10000"
    before = (await client.post("/crm/v3/objects/contacts/search", json={})).json()["total"]
    item = (await client.post("/crm/v3/objects/contacts", json={"properties": {"firstname": "Reset"}})).json()
    assert (await client.post("/crm/v3/objects/contacts/search", json={})).json()["total"] == before + 1
    entries = (await client.get("/_emulator/requests", params={"path": "/crm/v3/objects/contacts"})).json()[
        "requests"
    ]
    assert entries[0]["status"] == 429
    assert all(e["headers"]["authorization"].startswith("Bearer …") for e in entries)
    assert (await client.get("/not-implemented")).json()["category"] == "OBJECT_NOT_FOUND"
    await client.post("/_emulator/reset")
    assert (await client.get("/crm/v3/objects/contacts/" + item["id"])).status_code == 404
    assert (await client.post("/crm/v3/objects/contacts/search", json={})).json()["total"] == before


async def test_prefix_host_aliases_dedicated_app_and_deterministic_seed(
    make_hub: Callable[..., EmulatorHub],
) -> None:
    hub: EmulatorHub = make_hub("hubspot")
    hub.clock.freeze(datetime(2026, 10, 16, tzinfo=UTC))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hub.gateway)) as client:
        info = (await client.get("https://api.hubapi.com/_emulator/info")).json()
        auth = {"Authorization": "Bearer " + info["credentials"]["private_app_token"]}
        for host in ("api.hubapi.com", "api.hubspot.com", "app.hubspot.com"):
            assert (
                await client.get(f"https://{host}/crm/v3/objects/contacts", headers=auth)
            ).status_code == 200
        prefix = await client.get(
            "http://localhost:44080/hubspot/crm/v3/objects/contacts?limit=1&properties=email", headers=auth
        )
        next_link = prefix.json()["paging"]["next"]["link"]
        assert next_link.startswith("http://localhost:44080/hubspot/crm/v3/objects/contacts?")
        assert "properties=email" in next_link
        assert (await client.get(next_link, headers=auth)).status_code == 200
        seed = (await client.get("https://api.hubapi.com/crm/v3/objects/companies", headers=auth)).json()
    other = EmulatorHub(providers=["hubspot"], anchor="2026-10-16")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=other.apps["hubspot"]), base_url="http://localhost:44113"
    ) as port:
        dedicated = await port.get("/crm/v3/objects/companies", headers=auth)
        assert dedicated.json()["results"] == seed["results"]
        assert dedicated.json()["paging"]["next"]["link"].startswith("http://localhost:44113/")
    hub.clock.unfreeze()
