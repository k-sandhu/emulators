from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from emulators.core.clock import isoformat_z
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


async def test_validation_notification_lifecycle_and_crud(make_client: Callable[..., Any]) -> None:
    received = []
    validations = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> Any:
        if "validationToken" in request.query_params:
            validations.append(request.query_params["existing"])
            return PlainTextResponse(request.query_params["validationToken"])
        received.append(await request.json())
        return {}

    async with make_client("microsoft", base_url=BASE) as client:
        client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        expires = isoformat_z(client.hub.clock.now() + timedelta(hours=1))
        body = {
            "changeType": "created,updated,deleted",
            "notificationUrl": "https://consumer.example/hook?existing=preserved",
            "lifecycleNotificationUrl": "https://consumer.example/hook?existing=lifecycle",
            "resource": "me/mailFolders('Inbox')/messages",
            "expirationDateTime": expires,
            "clientState": "synthetic-state",
        }
        created = await client.post("/v1.0/subscriptions", json=body)
        assert created.status_code == 201, created.text
        subscription = created.json()
        sid = subscription["id"]
        assert validations == ["preserved", "lifecycle"]
        assert len((await client.get("/v1.0/subscriptions")).json()["value"]) == 1
        assert (await client.get(f"/v1.0/subscriptions/{sid}")).json()["clientState"] == "synthetic-state"
        assert (await client.post("/v1.0/subscriptions", json=body)).status_code == 409
        await client.post(
            "/v1.0/me/sendMail",
            json={
                "message": {
                    "subject": "Subscribed delivery",
                    "toRecipients": [{"emailAddress": {"address": "maya.chen@halcyonadvisory.example"}}],
                }
            },
        )
        assert len(received) == 1
        notice = received[0]["value"][0]
        assert notice["subscriptionId"] == sid and notice["changeType"] == "created"
        assert notice["resourceData"]["@odata.type"] == "#microsoft.graph.message"
        assert notice["clientState"] == "synthetic-state" and notice["tenantId"]
        assert (
            await client.patch(f"/v1.0/subscriptions/{sid}", json={"expirationDateTime": expires})
        ).status_code == 200
        assert (await client.post(f"/v1.0/subscriptions/{sid}/reauthorize")).status_code == 204
        result = await client.post("/_emulator/actions/expire_subscription", json={"id": sid})
        assert result.json()["lifecycleEvent"] == "reauthorizationRequired"
        assert received[-1]["value"][0]["lifecycleEvent"] == "reauthorizationRequired"
        assert (await client.delete(f"/v1.0/subscriptions/{sid}")).status_code == 204
        assert (await client.get(f"/v1.0/subscriptions/{sid}")).status_code == 404


async def test_bad_validation_expiration_resource_and_cross_mailbox(make_client: Callable[..., Any]) -> None:
    receiver = FastAPI()

    @receiver.post("/bad")
    async def bad() -> Any:
        return {"validationToken": "wrong"}

    async with make_client("microsoft", base_url=BASE) as client:
        client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        body = {
            "changeType": "created",
            "notificationUrl": "https://consumer.example/bad",
            "resource": "me/events",
            "expirationDateTime": isoformat_z(client.hub.clock.now() + timedelta(hours=1)),
        }
        result = await client.post("/v1.0/subscriptions", json=body)
        assert result.status_code == 400 and "validation request failed" in result.text
        for changes in (
            {"resource": "notSupported"},
            {"expirationDateTime": "invalid"},
            {"expirationDateTime": isoformat_z(client.hub.clock.now() + timedelta(days=10))},
        ):
            assert (await client.post("/v1.0/subscriptions", json={**body, **changes})).status_code == 400
        body["resource"] = "users/arjun.mehta@halcyonadvisory.example/messages"
        assert (await client.post("/v1.0/subscriptions", json=body)).status_code == 403


async def test_directory_and_calendar_notifications_app_ownership_and_removal(
    make_client: Callable[..., Any],
) -> None:
    received = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> Any:
        if token := request.query_params.get("validationToken"):
            return PlainTextResponse(token)
        received.append(await request.json())
        return {}

    async with make_client("microsoft", base_url=BASE) as client:
        client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
        client.headers.update(bearer(await mint(client, BASE, app_only=True, client_id="subscription-app")))
        expires = isoformat_z(client.hub.clock.now() + timedelta(hours=1))
        subscription = (
            await client.post(
                "/v1.0/subscriptions",
                json={
                    "resource": "users",
                    "changeType": "updated",
                    "notificationUrl": "https://consumer.example/hook",
                    "lifecycleNotificationUrl": "https://consumer.example/hook",
                    "expirationDateTime": expires,
                },
            )
        ).json()
        await client.patch("/v1.0/users/maya.chen@halcyonadvisory.example", json={"jobTitle": "Principal"})
        assert received[-1]["value"][0]["resourceData"]["@odata.type"] == "#microsoft.graph.user"
        other = bearer(await mint(client, BASE, app_only=True, client_id="other-app"))
        assert (
            await client.get(f"/v1.0/subscriptions/{subscription['id']}", headers=other)
        ).status_code == 404
        removed = await client.post(
            "/_emulator/actions/expire_subscription",
            json={"id": subscription["id"], "lifecycleEvent": "subscriptionRemoved"},
        )
        assert removed.status_code == 200
        assert (await client.get(f"/v1.0/subscriptions/{subscription['id']}")).status_code == 404


async def test_calendar_and_drive_change_bus_integration(make_client: Callable[..., Any]) -> None:
    notices: list[dict[str, Any]] = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> Any:
        if token := request.query_params.get("validationToken"):
            return PlainTextResponse(token)
        notices.extend((await request.json())["value"])
        return {}

    async with make_client("microsoft", base_url=BASE) as client:
        client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        expires = isoformat_z(client.hub.clock.now() + timedelta(hours=1))
        for resource, change_type in (("me/events", "created,updated,deleted"), ("me/drive/root", "updated")):
            result = await client.post(
                "/v1.0/subscriptions",
                json={
                    "resource": resource,
                    "changeType": change_type,
                    "notificationUrl": "https://consumer.example/hook",
                    "expirationDateTime": expires,
                },
            )
            assert result.status_code == 201, result.text
        event = (
            await client.post(
                "/v1.0/me/events",
                json={
                    "subject": "Notification contract",
                    "start": {"dateTime": "2026-10-20T15:00:00", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-10-20T15:30:00", "timeZone": "UTC"},
                },
            )
        ).json()
        assert notices[-1]["resourceData"]["@odata.type"] == "#microsoft.graph.event"
        await client.delete(f"/v1.0/me/events/{event['id']}")
        assert notices[-1]["changeType"] == "deleted"
        response = await client.put(
            "/v1.0/me/drive/root:/subscription-contract.txt:/content", content=b"Synthetic"
        )
        assert response.status_code == 201, response.text
        assert notices[-1]["resourceData"]["@odata.type"] == "#microsoft.graph.driveItem"
        assert notices[-1]["changeType"] == "updated"
