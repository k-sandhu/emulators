from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from .conftest import BASE, create_invoice


async def test_intent_handshake_and_signed_ordered_invoice_contact_events(client: httpx.AsyncClient) -> None:
    received: list[tuple[bytes, str, bool]] = []
    receiver = FastAPI()
    key = "fictional-signing-key"

    @receiver.post("/xero")
    async def hook(request: Request) -> Response:
        body = await request.body()
        signature = request.headers["x-xero-signature"]
        expected = base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()
        valid = hmac.compare_digest(signature, expected)
        received.append((body, signature, valid))
        return Response(status_code=200 if valid else 401)

    client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    configured = await client.post(
        "/_emulator/actions/configure_webhook",
        json={"url": "https://consumer.example/xero", "signing_key": key},
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["validation_statuses"] == [401, 200]
    assert [r[2] for r in received] == [False, True]
    assert all(json.loads(r[0])["events"] == [] for r in received)
    created = await client.put(
        BASE + "/Contacts", json={"Name": "Webhook contact", "EmailAddress": "webhooks@contact.example"}
    )
    assert created.status_code == 200
    cid = created.json()["Contacts"][0]["ContactID"]
    assert (
        await client.post(BASE + "/Contacts/" + cid, json={"Name": "Webhook contact revised"})
    ).status_code == 200
    invoice = await create_invoice(client)
    assert (
        await client.post(BASE + "/Invoices/" + invoice["InvoiceID"], json={"Reference": "Updated"})
    ).status_code == 200
    events = [json.loads(body) for body, _, valid in received[2:] if valid]
    assert [e["firstEventSequence"] for e in events] == [1, 2, 3, 4]
    assert [e["events"][0]["eventType"] for e in events] == ["CREATE", "UPDATE", "CREATE", "UPDATE"]
    assert [e["events"][0]["eventCategory"] for e in events] == ["CONTACT", "CONTACT", "INVOICE", "INVOICE"]
    assert events[2]["events"][0]["resourceId"] == invoice["InvoiceID"]
    assert (
        events[2]["events"][0]["resourceUrl"]
        == "https://api.xero.com" + BASE + "/Invoices/" + invoice["InvoiceID"]
    )
    assert events[2]["events"][0]["tenantId"] == client.headers["xero-tenant-id"]
    journal = (await client.get("/_emulator/webhooks")).json()["deliveries"]
    assert len(journal) == 4
    assert all(d["status"] == "delivered" for d in journal)
    assert all(d["headers"]["x-xero-signature"] for d in journal)


async def test_bad_handshake_disables_delivery(client: httpx.AsyncClient) -> None:
    receiver = FastAPI()

    @receiver.post("/xero")
    async def hook() -> Response:
        return Response(status_code=200)

    client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    response = await client.post(
        "/_emulator/actions/configure_webhook",
        json={"url": "https://consumer.example/xero", "signing_key": "test"},
    )
    assert response.status_code == 400
    await create_invoice(client)
    assert (await client.get("/_emulator/webhooks")).json()["deliveries"] == []
