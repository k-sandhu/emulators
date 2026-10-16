"""SOAP acknowledgement and notification batching regressions."""

from __future__ import annotations

from xml.etree import ElementTree as ET

import httpx
import pytest

from emulators.core.hub import EmulatorHub

from .conftest import BASE, Actor
from .test_control import soap_receiver


@pytest.mark.parametrize("ack", ["false", "", "TRUE"])
async def test_http_success_without_positive_soap_ack_is_retried(
    admin: Actor,
    client: httpx.AsyncClient,
    hub: EmulatorHub,
    ack: str,
) -> None:
    received: list[bytes] = []
    hub.webhook_transport = httpx.ASGITransport(app=soap_receiver(received, ack))
    await client.post(
        f"{BASE}/_emulator/actions/configure_outbound_message",
        json={
            "object": "Account",
            "url": "https://receiver.example/soap",
            "fields": ["Name"],
        },
    )
    await admin.create("Account", {"Name": "Retry acknowledgement"})
    delivery = (await client.get(f"{BASE}/_emulator/webhooks")).json()["deliveries"][0]
    assert delivery["status"] == "failed" and delivery["attempts"] == 3
    assert delivery["response_status"] == 200 and "Ack=true" in delivery["error"]
    assert len(received) == 3 and received[0] == received[1] == received[2]


async def test_positive_ack_after_a_negative_ack_marks_delivery_successful(
    admin: Actor,
    client: httpx.AsyncClient,
    hub: EmulatorHub,
) -> None:
    calls: list[bytes] = []

    async def receiver(request: httpx.Request) -> httpx.Response:
        calls.append(request.content)
        ack = "false" if len(calls) == 1 else "true"
        body = (
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            '<s:Body><notificationsResponse xmlns="http://soap.sforce.com/2005/09/outbound">'
            f"<Ack>{ack}</Ack></notificationsResponse></s:Body></s:Envelope>"
        )
        return httpx.Response(200, text=body, headers={"Content-Type": "text/xml"})

    hub.webhook_transport = httpx.MockTransport(receiver)
    await client.post(
        f"{BASE}/_emulator/actions/configure_outbound_message",
        json={
            "object": "Account",
            "url": "https://receiver.example/soap",
            "fields": ["Name"],
        },
    )
    await admin.create("Account", {"Name": "Retry once"})
    delivery = (await client.get(f"{BASE}/_emulator/webhooks")).json()["deliveries"][0]
    assert delivery["status"] == "delivered" and delivery["attempts"] == 2 and delivery["error"] is None
    assert calls[0] == calls[1]


async def test_outbound_notifications_batch_at_100_and_preserve_prefix_urls(
    hub: EmulatorHub,
) -> None:
    received: list[bytes] = []
    hub.webhook_transport = httpx.ASGITransport(app=soap_receiver(received))
    prefix = "http://localhost:44080/salesforce"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hub.gateway)) as client:
        body = (await client.post(f"{prefix}/_emulator/tokens", json={"user": "p035"})).json()
        await client.post(
            f"{prefix}/_emulator/actions/configure_outbound_message",
            json={"object": "Account", "url": "https://receiver.example/soap", "fields": ["Name"]},
        )
        response = await client.post(
            f"{prefix}/services/data/v62.0/composite/sobjects",
            headers={"Authorization": "Bearer " + body["access_token"]},
            json={"records": [{"attributes": {"type": "Account"}, "Name": f"Batch {n}"} for n in range(200)]},
        )
    assert response.status_code == 200 and all(row["success"] for row in response.json())
    ns = {"o": "http://soap.sforce.com/2005/09/outbound"}
    roots = [ET.fromstring(body) for body in received]
    assert [len(root.findall(".//o:Notification", ns)) for root in roots] == [100, 100]
    for root in roots:
        assert root.findtext(".//o:EnterpriseUrl", namespaces=ns).startswith(prefix + "/services/Soap/c/")
        assert root.findtext(".//o:PartnerUrl", namespaces=ns).startswith(prefix + "/services/Soap/u/")
