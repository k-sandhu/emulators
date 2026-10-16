from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import stripe
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from emulators.core.hub import EmulatorHub
from tests.providers.stripe.conftest import get, post


async def test_customer_sources_tax_ids_balance_and_delete(client: httpx.AsyncClient) -> None:
    customer = await post(
        client, "customers", {"name": "Customer", "address[city]": "Toronto", "source": "tok_visa"}
    )
    assert customer["address"]["city"] == "Toronto" and customer["default_source"]
    sources = await get(client, f"customers/{customer['id']}/sources")
    assert sources["data"][0]["last4"] == "4242"
    tax = await post(client, f"customers/{customer['id']}/tax_ids", {"type": "ca_bn", "value": "123456789"})
    assert (await get(client, f"customers/{customer['id']}/tax_ids/{tax['id']}"))["type"] == "ca_bn"
    credit = await post(
        client, f"customers/{customer['id']}/balance_transactions", {"amount": -1000, "currency": "usd"}
    )
    assert credit["ending_balance"] == -1000
    assert len((await get(client, f"customers/{customer['id']}/balance_transactions"))["data"]) == 1
    for resource, record_id in (("sources", sources["data"][0]["id"]), ("tax_ids", tax["id"])):
        assert (await client.delete(f"/v1/customers/{customer['id']}/{resource}/{record_id}")).json()[
            "deleted"
        ]
    deleted = await client.delete("/v1/customers/" + customer["id"])
    assert deleted.json() == {"id": customer["id"], "object": "customer", "deleted": True}
    assert (await get(client, "customers/" + customer["id"]))["deleted"]


async def test_catalog_checkout_portal_and_expiry(client: httpx.AsyncClient) -> None:
    product = await post(client, "products", {"name": "Advisory Package", "metadata[service]": "advisory"})
    price = await post(
        client,
        "prices",
        {"product": product["id"], "currency": "usd", "unit_amount": 1000, "lookup_key": "test_price"},
    )
    assert price["type"] == "one_time"
    assert (await post(client, "products/" + product["id"], {"description": "Updated"}))[
        "description"
    ] == "Updated"
    assert not (await post(client, "prices/" + price["id"], {"active": "false"}))["active"]
    await post(client, "prices/" + price["id"], {"active": "true"})
    session = await post(
        client,
        "checkout/sessions",
        {
            "mode": "payment",
            "line_items[0][price]": price["id"],
            "line_items[0][quantity]": 2,
            "customer_email": "checkout@halcyonadvisory.example",
            "success_url": "https://halcyonadvisory.example/success",
            "cancel_url": "https://halcyonadvisory.example/cancel",
        },
    )
    assert session["amount_total"] == 2000 and session["url"].startswith(
        "https://checkout.stripe.com/c/pay/cs_test_"
    )
    assert (await client.get(session["url"])).status_code == 200
    assert len((await get(client, f"checkout/sessions/{session['id']}/line_items"))["data"]) == 1
    completed = (
        await client.post("/_emulator/actions/complete_checkout", json={"session": session["id"]})
    ).json()
    assert completed["status"] == "complete" and completed["payment_status"] == "paid"
    assert (await get(client, "payment_intents/" + completed["payment_intent"]))["amount_received"] == 2000
    portal = await post(
        client,
        "billing_portal/sessions",
        {"customer": completed["customer"], "return_url": "https://halcyonadvisory.example"},
    )
    assert (await client.get(portal["url"])).status_code == 200
    expired = await post(
        client, "checkout/sessions", {"mode": "setup", "success_url": "https://halcyonadvisory.example"}
    )
    assert (await post(client, f"checkout/sessions/{expired['id']}/expire"))["status"] == "expired"
    assert (
        await client.post("/_emulator/actions/complete_checkout", json={"session": expired["id"]})
    ).status_code == 400


async def test_file_upload_download_and_balance_payouts(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "https://files.stripe.com/v1/files",
        data={"purpose": "dispute_evidence"},
        files={"file": ("evidence.pdf", b"%PDF-1.4 test evidence", "application/pdf")},
    )
    assert response.status_code == 200, response.text
    file = response.json()
    assert file["purpose"] == "dispute_evidence" and file["size"] == 22
    assert (await client.get(file["url"])).content == b"%PDF-1.4 test evidence"
    assert (await get(client, "files/" + file["id"]))["filename"] == "evidence.pdf"
    balance = await get(client, "balance")
    assert balance["available"][0]["amount"] > 0
    payout = await post(client, "payouts", {"amount": 1000, "currency": "usd"})
    assert payout["status"] == "pending"
    assert (await post(client, f"payouts/{payout['id']}/cancel"))["status"] == "canceled"
    txn = await get(client, "balance_transactions/" + payout["balance_transaction"])
    assert txn["amount"] == -1000


async def test_webhook_official_signature_retry_filter_events(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub
) -> None:
    receiver = FastAPI()
    received: list[tuple[bytes, str]] = []

    @receiver.post("/hook")
    async def hook(request: Request) -> JSONResponse:
        received.append((await request.body(), request.headers["stripe-signature"]))
        stripe_hub.clock.advance(timedelta(seconds=1))
        return JSONResponse({}, status_code=500 if len(received) == 1 else 200)

    stripe_hub.webhook_transport = httpx.ASGITransport(app=receiver)
    endpoint = await post(
        client,
        "webhook_endpoints",
        {"url": "https://consumer.example/hook", "enabled_events[]": "customer.created"},
    )
    customer = await post(client, "customers", {"email": "webhook@halcyonadvisory.example"})
    assert len(received) == 2
    assert received[0][1] != received[1][1]
    for payload, signature in received:
        event = stripe.Webhook.construct_event(payload, signature, endpoint["secret"], tolerance=0)
        assert event.type == "customer.created" and event.data.object.id == customer["id"]
    retrieved = await get(client, "webhook_endpoints/" + endpoint["id"])
    assert "secret" not in retrieved
    events = await get(client, "events", params={"type": "customer.created"})
    event = await get(client, "events/" + events["data"][0]["id"])
    assert event["pending_webhooks"] == 0
    await post(client, "webhook_endpoints/" + endpoint["id"], {"disabled": "true"})
    await post(client, "customers", {"name": "No delivery"})
    assert len(received) == 2
    assert (await client.delete("/v1/webhook_endpoints/" + endpoint["id"])).json()["deleted"]


async def test_oauth_connect_roundtrip(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "https://connect.stripe.com/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "ca_test",
            "redirect_uri": "https://halcyonadvisory.example/oauth",
            "scope": "read_write",
            "state": "nonce",
        },
    )
    assert response.status_code == 302
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["nonce"]
    token = await client.post(
        "https://connect.stripe.com/oauth/token",
        data={"grant_type": "authorization_code", "client_id": "ca_test", "code": query["code"][0]},
    )
    assert token.status_code == 200, token.text
    data = token.json()
    assert data["stripe_user_id"].startswith("acct_")
    assert (
        await client.get("/v1/account", headers={"Authorization": "Bearer " + data["access_token"]})
    ).status_code == 200
    refreshed = await client.post(
        "https://connect.stripe.com/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": data["refresh_token"]},
    )
    assert refreshed.status_code == 200


@pytest.mark.parametrize("mode", ["async", "manual"])
async def test_webhook_pending_count_after_deferred_delivery(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub, mode: str
) -> None:
    receiver = FastAPI()
    received: list[bytes] = []

    @receiver.post("/hook")
    async def hook(request: Request) -> dict:
        received.append(await request.body())
        return {}

    ctx = stripe_hub.context("stripe")
    ctx.webhooks.mode = mode
    stripe_hub.webhook_transport = httpx.ASGITransport(app=receiver)
    await post(
        client,
        "webhook_endpoints",
        {"url": "https://consumer.example/hook", "enabled_events[]": "customer.created"},
    )
    await post(client, "customers", {"name": "Deferred webhook"})
    events = await get(client, "events", params={"type": "customer.created"})
    event_id = events["data"][0]["id"]
    if mode == "manual":
        assert not received and events["data"][0]["pending_webhooks"] == 1
        await client.post("/_emulator/webhooks/flush")
    else:
        await stripe_hub.drain_webhooks()
    assert len(received) == 1
    assert (await get(client, "events/" + event_id))["pending_webhooks"] == 0
