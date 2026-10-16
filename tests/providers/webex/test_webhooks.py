from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any

import httpx
from fastapi import FastAPI, Request

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint


async def test_end_meeting_creates_artifacts_signed_filtered_hooks(
    client: httpx.AsyncClient, hub: EmulatorHub
) -> None:
    received: list[tuple[bytes, str]] = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> dict[str, bool]:
        received.append((await request.body(), request.headers["x-spark-signature"]))
        return {"ok": True}

    hub.webhook_transport = httpx.ASGITransport(app=receiver)
    meeting = (
        await client.post(
            "/v1/meetings",
            json={"title": "Meridian status", "start": "2026-10-16T13:00:00Z", "end": "2026-10-16T14:00:00Z"},
        )
    ).json()
    hooks = []
    for resource, event in (
        ("meetings", "ended"),
        ("recordings", "created"),
        ("meetingTranscripts", "created"),
    ):
        r = await client.post(
            "/v1/webhooks",
            json={
                "name": resource,
                "targetUrl": "https://consumer.example/hook",
                "resource": resource,
                "event": event,
                "secret": "webhook-test-secret",
            },
        )
        assert r.status_code == 200, r.text
        hooks.append(r.json())
    await client.post(
        "/v1/webhooks",
        json={
            "name": "nonmatching",
            "targetUrl": "https://consumer.example/hook",
            "resource": "meetings",
            "event": "ended",
            "filter": "hostEmail=nobody@example.invalid",
            "secret": "webhook-test-secret",
        },
    )
    outsider = bearer(await mint(client, "https://webexapis.com", user="p018"))
    await client.post(
        "/v1/webhooks",
        headers=outsider,
        json={
            "name": "private",
            "targetUrl": "https://consumer.example/hook",
            "resource": "meetings",
            "event": "ended",
            "secret": "webhook-test-secret",
        },
    )
    result = await client.post("/_emulator/actions/end_meeting", json={"meeting_id": meeting["id"]})
    assert result.status_code == 200, result.text
    artifacts = result.json()
    assert artifacts["meeting"]["id"].startswith(meeting["id"] + "_I_")
    assert artifacts["recording"]["meetingId"] == artifacts["meeting"]["id"]
    assert len(received) == 3
    hub.clock.advance(timedelta(seconds=1))
    assert [json.loads(raw)["resource"] for raw, _ in received] == [
        "meetings",
        "recordings",
        "meetingTranscripts",
    ]
    for raw, signature in received:
        assert hmac.compare_digest(signature, hmac.new(b"webhook-test-secret", raw, hashlib.sha1).hexdigest())
        event = json.loads(raw)
        assert all(
            k in event
            for k in (
                "id",
                "name",
                "targetUrl",
                "resource",
                "event",
                "orgId",
                "createdBy",
                "appId",
                "ownedBy",
                "status",
                "actorId",
                "data",
                "created",
            )
        )
        assert not any(k.startswith("_") for k in event["data"])
    assert (
        await client.post("/_emulator/actions/end_meeting", json={"meeting_id": meeting["id"]})
    ).status_code == 200
    assert len(received) == 3
    assert (
        len(
            (await client.get("/v1/recordings", params={"meetingId": artifacts["meeting"]["id"]})).json()[
                "items"
            ]
        )
        == 1
    )
    assert any(
        r["meetingId"] == artifacts["meeting"]["id"]
        for r in (await client.get("/v1/recordings")).json()["items"]
    )
    transcript = (
        await client.get("/v1/meetingTranscripts", params={"meetingId": artifacts["meeting"]["id"]})
    ).json()["items"][0]
    assert (await client.get(transcript["vttDownloadLink"])).text.startswith("WEBVTT")
    journal = (await client.get("/_emulator/webhooks")).json()
    assert journal["count"] == 3 and all(d["status"] == "delivered" for d in journal["deliveries"])
    assert (await client.get(f"/v1/webhooks/{hooks[0]['id']}", headers=outsider)).status_code == 404
    assert (
        await client.put(
            f"/v1/webhooks/{hooks[0]['id']}",
            json={"name": "updated", "targetUrl": "https://consumer.example/hook", "status": "active"},
        )
    ).json()["name"] == "updated"
    assert (await client.delete(f"/v1/webhooks/{hooks[0]['id']}")).status_code == 204
    assert (await client.get(f"/v1/webhooks/{hooks[0]['id']}")).status_code == 404


async def test_message_membership_room_hooks_and_manual_mode(
    client: httpx.AsyncClient, hub: EmulatorHub
) -> None:
    hub.context("webex").webhooks.mode = "manual"
    delivered: list[dict[str, Any]] = []

    def receiver(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content))
        return httpx.Response(204)

    hub.webhook_transport = httpx.MockTransport(receiver)
    room = (await client.post("/v1/rooms", json={"title": "Webhook room"})).json()
    for resource in ("rooms", "memberships", "messages"):
        r = await client.post(
            "/v1/webhooks",
            json={
                "name": resource,
                "targetUrl": "https://consumer.example/hook",
                "resource": resource,
                "event": "all",
                "filter": f"{'id' if resource == 'rooms' else 'roomId'}={room['id']}",
            },
        )
        assert r.status_code == 200
    await client.put(f"/v1/rooms/{room['id']}", json={"title": "Renamed"})
    member = (
        await client.post(
            "/v1/memberships", json={"roomId": room["id"], "personEmail": "maya.chen@halcyonadvisory.example"}
        )
    ).json()
    await client.put(f"/v1/memberships/{member['id']}", json={"isModerator": True})
    message = (await client.post("/v1/messages", json={"roomId": room["id"], "text": "Hello"})).json()
    await client.delete(f"/v1/messages/{message['id']}")
    await client.delete(f"/v1/memberships/{member['id']}")
    await client.delete(f"/v1/rooms/{room['id']}")
    assert not delivered
    await client.post("/_emulator/webhooks/flush")
    assert [(p["resource"], p["event"]) for p in delivered] == [
        ("rooms", "updated"),
        ("memberships", "created"),
        ("memberships", "updated"),
        ("messages", "created"),
        ("messages", "deleted"),
        ("memberships", "deleted"),
        ("rooms", "deleted"),
    ]


async def test_webhook_validation_and_owned_listing(client: httpx.AsyncClient) -> None:
    base = {
        "name": "test",
        "targetUrl": "https://consumer.example/hook",
        "resource": "messages",
        "event": "created",
    }
    for fields in ({"resource": "bad"}, {"event": "started"}, {"targetUrl": "relative"}, {"secret": 42}):
        assert (await client.post("/v1/webhooks", json={**base, **fields})).status_code == 400
    assert (await client.post("/v1/webhooks", json=base)).status_code == 200
    assert len((await client.get("/v1/webhooks")).json()["items"]) == 1
    outsider = bearer(await mint(client, "https://webexapis.com", user="p018"))
    assert (await client.get("/v1/webhooks", headers=outsider)).json() == {"items": []}


async def test_org_webhook_privileges_and_host_filter(client: httpx.AsyncClient, hub: EmulatorHub) -> None:
    received: list[dict[str, Any]] = []

    def receiver(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(204)

    hub.webhook_transport = httpx.MockTransport(receiver)
    hook = {
        "name": "all recordings",
        "targetUrl": "https://consumer.example/hook",
        "resource": "recordings",
        "event": "created",
        "ownedBy": "org",
        "filter": "hostEmail=sofia.alvarez@halcyonadvisory.example",
    }
    assert (await client.post("/v1/webhooks", json=hook)).status_code == 403
    service = bearer(await mint(client, "https://webexapis.com", app_only=True))
    created = await client.post("/v1/webhooks", json=hook, headers=service)
    assert created.status_code == 200 and created.json()["ownedBy"] == "org"
    meeting = (
        await client.post(
            "/v1/meetings",
            json={
                "title": "Service app import",
                "start": "2026-10-16T13:00:00Z",
                "end": "2026-10-16T14:00:00Z",
            },
        )
    ).json()
    assert (
        await client.post("/_emulator/actions/end_meeting", json={"meeting_id": meeting["id"]})
    ).status_code == 200
    assert len(received) == 1 and received[0]["ownedBy"] == "org"
    weak = bearer(await mint(client, "https://webexapis.com", user="p005", scopes=[]))
    assert (await client.get("/v1/webhooks", headers=weak)).status_code == 403
