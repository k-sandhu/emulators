"""Webhook subscription handshake (action + Graph subscriptions API) and delivery journal."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from tests.providers.meta.conftest import GRAPH, GRAPH_HOST, SYS, Receiver, action


async def test_configure_webhook_requires_challenge_echo(
    client: httpx.AsyncClient, receiver: Receiver
) -> None:
    ok = await receiver.configure(client, "whatsapp_business_account")
    assert ok["success"] is True
    assert isinstance(ok["challenge"], int)

    bad_receiver = Receiver(expected_verify_token="a-different-token")
    client.hub.webhook_transport = httpx.ASGITransport(app=bad_receiver.app)  # type: ignore[attr-defined]
    failed = await action(
        client,
        "configure_webhook",
        {
            "object": "page",
            "callback_url": "http://consumer.test/hook",
            "verify_token": "not-the-token",
        },
    )
    assert failed["success"] is False
    assert failed["error"] == "challenge_mismatch"


async def test_graph_subscriptions_api(
    client: httpx.AsyncClient, ids: SimpleNamespace, receiver: Receiver
) -> None:
    app_token = f"{ids.app_id}|{ids.app_secret}"
    headers = {"Authorization": f"Bearer {app_token}"}
    created = await client.post(
        f"{GRAPH}/{ids.app_id}/subscriptions",
        headers=headers,
        json={
            "object": "page",
            "callback_url": "http://consumer.test/hook",
            "verify_token": ids.verify_token,
            "fields": "messages,messaging_postbacks",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json() == {"success": True}
    listing = await client.get(f"{GRAPH}/{ids.app_id}/subscriptions", headers=headers)
    entry = listing.json()["data"][0]
    assert entry["object"] == "page"
    assert {f["name"] for f in entry["fields"]} == {"messages", "messaging_postbacks"}
    denied = await client.get(f"{GRAPH}/{ids.app_id}/subscriptions", headers=SYS)
    assert denied.status_code == 403
    deleted = await client.delete(
        f"{GRAPH}/{ids.app_id}/subscriptions", headers=headers, params={"object": "page"}
    )
    assert deleted.json() == {"success": True}
    assert (await client.get(f"{GRAPH}/{ids.app_id}/subscriptions", headers=headers)).json()["data"] == []


async def test_delivery_journal_records_signature_headers(
    client: httpx.AsyncClient, ids: SimpleNamespace, receiver: Receiver
) -> None:
    await receiver.configure(client, "whatsapp_business_account")
    await action(client, "inbound_whatsapp", {"from": "+13055550174", "text": "journal me"})
    journal = await client.get(f"{GRAPH_HOST}/_emulator/webhooks")
    deliveries = journal.json()["deliveries"]
    assert deliveries[-1]["status"] == "delivered"
    assert deliveries[-1]["response_status"] == 200
    headers = {k.lower(): v for k, v in deliveries[-1]["headers"].items()}
    assert headers["x-hub-signature-256"].startswith("sha256=")
    assert headers["x-hub-signature"].startswith("sha1=")


async def test_no_delivery_without_subscription(client: httpx.AsyncClient, receiver: Receiver) -> None:
    result = await action(client, "inbound_whatsapp", {"from": "+13055550174", "text": "anyone there?"})
    assert result["webhook_delivered"] is False
    assert receiver.deliveries == []
