"""Control plane: faults, journal, reset, outbound messages and the Salesforce actions."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import httpx
from fastapi import FastAPI, Request

from .conftest import BASE, Actor

Make = Callable[..., Awaitable[Actor]]
ADMIN = f"{BASE}/_emulator"


async def test_fault_injection_uses_salesforce_error_envelopes(
    admin: Actor, client: httpx.AsyncClient
) -> None:
    await client.post(f"{ADMIN}/faults", json={"path": "/services/data/.*/query", "status": 503, "times": 1})
    r = await admin.get("/query", params={"q": "SELECT Id FROM Account"})
    assert r.status_code == 503
    assert r.json() == [{"message": "Server Unavailable", "errorCode": "SERVER_UNAVAILABLE"}]
    assert (
        await admin.get("/query", params={"q": "SELECT Id FROM Account"})
    ).status_code == 200  # the fault fired once
    await client.post(
        f"{ADMIN}/faults", json={"path": "/sobjects/Account", "method": "POST", "status": 429, "times": 1}
    )
    throttled = await admin.post("/sobjects/Account", {"Name": "Throttled"})
    assert throttled.status_code == 429 and throttled.headers["retry-after"] == "2"
    assert throttled.json()[0]["errorCode"] == "REQUEST_LIMIT_EXCEEDED"
    await client.post(f"{ADMIN}/faults", json={"path": "/limits", "status": 401, "times": 1})
    assert (await admin.get("/limits")).json() == [
        {"message": "Session expired or invalid", "errorCode": "INVALID_SESSION_ID"}
    ]
    custom = [
        {
            "message": "unable to obtain exclusive access to this record or 1 records: 001000000000000",
            "errorCode": "UNABLE_TO_LOCK_ROW",
        }
    ]
    await client.post(
        f"{ADMIN}/faults",
        json={"path": "/sobjects/Account/", "method": "PATCH", "status": 400, "body": custom, "times": 1},
    )
    rid = await admin.create("Account", {"Name": "Locked"})
    locked = await admin.patch(f"/sobjects/Account/{rid}", {"Rating": "Hot"})
    assert locked.status_code == 400 and locked.json() == custom
    assert (await admin.patch(f"/sobjects/Account/{rid}", {"Rating": "Hot"})).status_code == 204
    unknown = await admin.get("/nothing")
    assert unknown.status_code == 404 and unknown.json() == [
        {"message": "The requested resource does not exist", "errorCode": "NOT_FOUND"}
    ]


async def test_request_journal_records_calls_with_redacted_tokens(
    admin: Actor, client: httpx.AsyncClient
) -> None:
    await admin.get("/limits")
    await admin.post("/sobjects/Account", {"Name": "Journaled"})
    journal = (await client.get(f"{ADMIN}/requests", params={"path": "/services/data"})).json()["requests"]
    assert [(e["method"], e["status"]) for e in journal[-2:]] == [("GET", 200), ("POST", 201)]
    assert journal[-1]["body"] == {"Name": "Journaled"}
    assert journal[-1]["host"] == "halcyonadvisory.my.salesforce.com"
    assert admin.token not in str(journal[-1]["headers"])
    assert journal[-1]["headers"]["authorization"].startswith("Bearer …")


async def test_reset_restores_seed_state_and_id_counters(admin: Actor, client: httpx.AsyncClient) -> None:
    first = await admin.create("Account", {"Name": "Ephemeral"})
    await admin.delete(f"/sobjects/Account/{(await admin.ids('SELECT Id FROM Account LIMIT 1'))[0]}")
    assert (await client.post(f"{ADMIN}/reset")).status_code == 200
    token = (await client.post(f"{ADMIN}/tokens", json={"user": "p035"})).json()["access_token"]
    after = Actor(client, token, "", "p035")
    assert (await after.query("SELECT COUNT() FROM Account"))["totalSize"] == 41
    assert await after.create("Account", {"Name": "Ephemeral"}) == first  # counters are part of the snapshot
    info = (await client.get(f"{ADMIN}/info")).json()
    assert info["name"] == "salesforce" and info["collections"]["Account"] >= 41
    assert {
        "mint_token",
        "convert_lead",
        "configure_outbound_message",
        "set_api_usage",
        "undelete",
        "expire_sessions",
    } <= set(info["actions"])
    assert (
        info["credentials"]["password"] == "Halcyon2026!"
        and info["credentials"]["security_token"] == "EMULATORTOKEN"
    )


def soap_receiver(received: list[bytes], ack: str = "true") -> FastAPI:
    app = FastAPI()

    @app.post("/soap")
    async def soap(request: Request) -> Any:
        received.append(await request.body())
        from fastapi.responses import Response

        body = (
            '<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soapenv:Body><notificationsResponse xmlns="http://soap.sforce.com/2005/09/outbound"><Ack>{ack}</Ack></notificationsResponse>'
            "</soapenv:Body></soapenv:Envelope>"
        )
        return Response(body, media_type="text/xml")

    return app


async def test_outbound_messages_are_soap_notifications(admin: Actor, client: httpx.AsyncClient) -> None:
    received: list[bytes] = []
    client.hub.webhook_transport = httpx.ASGITransport(app=soap_receiver(received))  # type: ignore[attr-defined]
    cfg = (await client.post(f"{ADMIN}/actions/configure_outbound_message", json={
        "object": "Account", "url": "http://consumer.test/soap", "events": ["create", "update", "delete"],
        "fields": ["Name", "Industry", "OwnerId"],
    })).json()  # fmt: skip
    assert (
        cfg["id"].startswith("04k")
        and cfg["object"] == "Account"
        and cfg["events"] == ["create", "update", "delete"]
    )
    rid = await admin.create("Account", {"Name": "Notify <Me> & Co", "Industry": "Retail"})
    assert len(received) == 1
    root = ET.fromstring(received[0])
    ns = {"o": "http://soap.sforce.com/2005/09/outbound", "sf": "urn:sobject.enterprise.soap.sforce.com"}
    notifications = root.find(".//o:notifications", ns)
    assert notifications is not None
    assert notifications.findtext("o:OrganizationId", namespaces=ns) == "00D5g0000049673EAA"
    assert notifications.findtext("o:ActionId", namespaces=ns) == cfg["id"]
    assert notifications.findtext("o:EnterpriseUrl", namespaces=ns).startswith(
        "https://halcyonadvisory.my.salesforce.com/services/Soap/c/"
    )
    item = notifications.find("o:Notification", ns)
    assert item.findtext("o:Id", namespaces=ns).startswith("04l")
    sobject = item.find("o:sObject", ns)
    assert sobject.findtext("sf:Id", namespaces=ns) == rid
    assert (
        sobject.findtext("sf:Name", namespaces=ns) == "Notify <Me> & Co"
    )  # XML escaped on the wire, unescaped by the parser
    assert sobject.findtext("sf:Industry", namespaces=ns) == "Retail" and sobject.find("sf:Phone", ns) is None
    await admin.patch(f"/sobjects/Account/{rid}", {"Industry": "Technology"})
    await admin.delete(f"/sobjects/Account/{rid}")
    assert len(received) == 3
    log = (await client.get(f"{ADMIN}/webhooks")).json()["deliveries"]
    assert [d["event_type"] for d in log] == [
        "outbound.Account.create",
        "outbound.Account.update",
        "outbound.Account.delete",
    ]
    assert log[0]["status"] == "delivered" and log[0]["headers"]["Content-Type"].startswith("text/xml")
    # batched collections produce one message with several notifications
    out = (
        await admin.post(
            "/composite/sobjects",
            {"records": [{"attributes": {"type": "Account"}, "Name": f"N{i}"} for i in range(3)]},
        )
    ).json()
    assert len(out) == 3 and len(received) == 4
    assert ET.fromstring(received[3]).findall(".//o:Notification", ns).__len__() == 3
    # other objects and rolled-back requests send nothing
    await admin.create("Contact", {"LastName": "Silent"})
    await admin.post("/composite", {"allOrNone": True, "compositeRequest": [
        {"method": "POST", "url": "/services/data/v62.0/sobjects/Account", "referenceId": "a", "body": {"Name": "Never"}},
        {"method": "POST", "url": "/services/data/v62.0/sobjects/Account", "referenceId": "b", "body": {}},
    ]})  # fmt: skip
    assert len(received) == 4
    listed = (await client.post(f"{ADMIN}/actions/list_outbound_messages")).json()
    assert len(listed["outboundMessages"]) == 1
    assert (await client.post(f"{ADMIN}/actions/clear_outbound_messages")).json() == {"removed": 1}
    await admin.create("Account", {"Name": "After clear"})
    assert len(received) == 4
    bad = await client.post(
        f"{ADMIN}/actions/configure_outbound_message", json={"object": "Widget", "url": "http://x"}
    )
    assert bad.status_code == 400
    assert (
        await client.post(f"{ADMIN}/actions/configure_outbound_message", json={"object": "Account"})
    ).status_code == 400


async def test_outbound_message_delivery_failures_are_journaled(
    admin: Actor, client: httpx.AsyncClient
) -> None:
    client.hub.webhook_transport = httpx.ASGITransport(app=FastAPI())  # type: ignore[attr-defined]  # 404 for every route
    await client.post(
        f"{ADMIN}/actions/configure_outbound_message",
        json={"object": "Lead", "url": "http://consumer.test/gone"},
    )
    assert (
        await admin.post("/sobjects/Lead", {"LastName": "Lost", "Company": "Lost Co"})
    ).status_code == 201  # the API call still succeeds
    delivery = (await client.get(f"{ADMIN}/webhooks")).json()["deliveries"][0]
    assert delivery["status"] == "failed" and delivery["attempts"] == 3 and delivery["response_status"] == 404


async def test_convert_lead_action(admin: Actor, client: httpx.AsyncClient) -> None:
    lead = (
        await admin.rows(
            "SELECT Id, Company, LastName, Email FROM Lead WHERE IsConverted = false AND Status = 'Open - Not Contacted' LIMIT 1"
        )
    )[0]
    r = await client.post(
        f"{ADMIN}/actions/convert_lead", json={"lead_id": lead["Id"], "opportunity_name": "Converted Deal"}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert (
        out["success"] is True
        and out["accountId"].startswith("001")
        and out["opportunityId"].startswith("006")
    )
    converted = (await admin.get(f"/sobjects/Lead/{lead['Id']}")).json()
    assert (converted["IsConverted"], converted["Status"], converted["ConvertedAccountId"]) == (
        True,
        "Closed - Converted",
        out["accountId"],
    )
    assert converted["ConvertedContactId"] == out["contactId"] and converted["ConvertedDate"] == "2026-10-16"
    acct = (await admin.get(f"/sobjects/Account/{out['accountId']}")).json()
    assert acct["Name"] == lead["Company"] and acct["Type"] == "Prospect"
    contact = (await admin.get(f"/sobjects/Contact/{out['contactId']}")).json()
    assert (
        contact["LastName"] == lead["LastName"]
        and contact["AccountId"] == out["accountId"]
        and contact["Email"] == lead["Email"]
    )
    opp = (await admin.get(f"/sobjects/Opportunity/{out['opportunityId']}")).json()
    assert (
        opp["Name"] == "Converted Deal"
        and opp["StageName"] == "Prospecting"
        and opp["CloseDate"] == "2026-11-15"
    )
    role = await admin.rows(
        f"SELECT ContactId, IsPrimary, Role FROM OpportunityContactRole WHERE OpportunityId = '{out['opportunityId']}'"
    )
    assert role[0]["ContactId"] == out["contactId"] and role[0]["IsPrimary"] is True
    again = await client.post(f"{ADMIN}/actions/convert_lead", json={"lead_id": lead["Id"]})
    assert again.status_code == 400
    assert (
        await client.post(f"{ADMIN}/actions/convert_lead", json={"lead_id": "00Q000000000000AAA"})
    ).status_code == 404
    other = (await admin.ids("SELECT Id FROM Lead WHERE IsConverted = false LIMIT 1"))[0]
    no_opp = (
        await client.post(
            f"{ADMIN}/actions/convert_lead", json={"lead_id": other, "do_not_create_opportunity": True}
        )
    ).json()
    assert no_opp["opportunityId"] is None


async def test_undelete_and_expire_sessions_actions(
    admin: Actor, client: httpx.AsyncClient, actor: Make
) -> None:
    rid = await admin.create("Account", {"Name": "Recycled"})
    await admin.delete(f"/sobjects/Account/{rid}")
    assert (await admin.get(f"/sobjects/Account/{rid}")).status_code == 404
    r = await client.post(f"{ADMIN}/actions/undelete", json={"id": rid})
    assert r.json() == {"id": rid, "undeleted": True}
    assert (await admin.get(f"/sobjects/Account/{rid}")).json()["Name"] == "Recycled"
    assert (
        await client.post(f"{ADMIN}/actions/undelete", json={"id": rid})
    ).status_code == 404  # not deleted any more
    hannah = await actor("p011")
    assert (await hannah.get("/limits")).status_code == 200
    revoked = (await client.post(f"{ADMIN}/actions/expire_sessions", json={"user": "p011"})).json()
    assert revoked["revoked"] >= 1
    assert (await hannah.get("/limits")).status_code == 401
    assert (await admin.get("/limits")).status_code == 200
    assert (await client.post(f"{ADMIN}/actions/expire_sessions", json={"user": "ghost"})).status_code == 404


async def test_minted_token_expiry_and_deactivated_users(
    admin: Actor, client: httpx.AsyncClient, actor: Make
) -> None:
    short = await actor("p011", expires_in=60)
    assert (await short.get("/limits")).status_code == 200
    client.hub.clock.advance(timedelta(seconds=61))  # type: ignore[attr-defined]
    assert (await short.get("/limits")).status_code == 401
    hannah = await actor("p011")
    assert (await admin.patch(f"/sobjects/User/{hannah.user_id}", {"IsActive": False})).status_code == 204
    assert (await hannah.get("/limits")).status_code == 401  # a deactivated user's sessions stop working
