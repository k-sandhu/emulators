"""Webhook registration (challenge handshake), signed change notifications and debouncing."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

RYAN = "ryan.murphy@halcyonadvisory.example"


class Receiver:
    def __init__(self) -> None:
        self.app = FastAPI()
        self.bodies: list[tuple[bytes, dict[str, str]]] = []
        self.challenge_ok = True

        @self.app.get("/hook")
        async def challenge(challenge: str) -> PlainTextResponse:
            return PlainTextResponse(
                challenge if self.challenge_ok else "nope", headers={"X-Content-Type-Options": "nosniff"}
            )

        @self.app.post("/hook")
        async def notify(request: Request) -> dict[str, Any]:
            self.bodies.append((await request.body(), dict(request.headers)))
            return {}

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(b) for b, _ in self.bodies]


@pytest.fixture
def receiver(hub: Any) -> Receiver:
    r = Receiver()
    hub.webhook_transport = httpx.ASGITransport(app=r.app)
    return r


async def register(dbx: Any, **extra: Any) -> httpx.Response:
    return await dbx.client.post(
        f"{dbx.API}/_emulator/actions/configure_webhook",
        json={"url": "http://consumer.test/hook", "app_secret": "shh", "app_key": "my-app", **extra},
    )


async def test_registration_verifies_the_challenge(dbx: Any, receiver: Receiver) -> None:
    receiver.challenge_ok = False
    bad = await register(dbx)
    assert bad.status_code == 400 and "verification failed" in bad.json()["error"]
    receiver.challenge_ok = True
    ok = await register(dbx)
    assert ok.status_code == 200 and ok.json()["verified"] is True
    listed = (await dbx.client.post(f"{dbx.API}/_emulator/actions/list_webhooks")).json()
    assert listed["webhooks"][0]["app_key"] == "my-app" and "secret" not in json.dumps(listed)
    missing = await dbx.client.post(
        f"{dbx.API}/_emulator/actions/configure_webhook", json={"url": "http://consumer.test/hook"}
    )
    assert missing.status_code == 400


async def test_notifications_are_signed_and_target_authorised_accounts(dbx: Any, receiver: Receiver) -> None:
    assert (await register(dbx)).status_code == 200
    maya = await dbx.token(dbx.MAYA, client_id="my-app")
    ryan = await dbx.token(RYAN, client_id="my-app")
    other = await dbx.token(dbx.ELEANOR, client_id="another-app")
    maya_acct = await dbx.account_id(dbx.MAYA)
    ryan_acct = await dbx.account_id(RYAN)
    eleanor_acct = await dbx.account_id(dbx.ELEANOR)
    root = await dbx.root(maya)
    await dbx.put(maya, "/Client Engagements/webhook-test.txt", b"hi", path_root=root)
    assert len(receiver.bodies) == 1
    body, headers = receiver.bodies[0]
    assert headers["x-dropbox-signature"] == hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    payload = json.loads(body)
    accounts = set(payload["list_folder"]["accounts"])
    assert {maya_acct, ryan_acct} <= accounts and eleanor_acct not in accounts
    assert len(payload["delta"]["users"]) == len(accounts) and all(
        isinstance(u, int) for u in payload["delta"]["users"]
    )
    # private change: only the owner is told
    await dbx.put(maya, "/private.txt", b"p")
    assert receiver.payloads[-1]["list_folder"]["accounts"] == [maya_acct]
    # reads and failed writes do not notify
    count = len(receiver.bodies)
    await dbx.ok(maya, "files/list_folder", {"path": ""})
    await dbx.upload(maya, "files/upload", {"path": "/private.txt"}, b"dup")
    assert len(receiver.bodies) == count
    # the notification is only a hint: the app calls list_folder/continue
    cursor = (
        await dbx.ok(
            ryan,
            "files/list_folder/get_latest_cursor",
            {"path": "/Client Engagements"},
            path_root=await dbx.root(ryan),
        )
    )["cursor"]
    await dbx.put(maya, "/Client Engagements/second.txt", b"2", path_root=root)
    delta = await dbx.ok(ryan, "files/list_folder/continue", {"cursor": cursor})
    assert [e["name"] for e in delta["entries"]] == ["second.txt"]
    assert other


async def test_one_notification_per_request_even_for_batches(dbx: Any, receiver: Receiver) -> None:
    assert (await register(dbx)).status_code == 200
    maya = await dbx.token(dbx.MAYA, client_id="my-app")
    for i in range(3):
        await dbx.put(maya, f"/batch/{i}.txt", b"x")
    receiver.bodies.clear()
    await dbx.ok(maya, "files/delete_batch", {"entries": [{"path": f"/batch/{i}.txt"} for i in range(3)]})
    assert len(receiver.bodies) == 1


async def test_team_token_covers_every_member_and_actions(dbx: Any, receiver: Receiver) -> None:
    assert (await register(dbx, app_key="team-app")).status_code == 200
    await dbx.team_token(client_id="team-app")
    maya = await dbx.token(dbx.MAYA)
    await dbx.put(maya, "/Client Engagements/x.txt", b"x", path_root=await dbx.root(maya))
    accounts = set(receiver.payloads[-1]["list_folder"]["accounts"])
    assert len(accounts) > 5  # everyone in the Client Engagements groups
    receiver.bodies.clear()
    r = await dbx.client.post(f"{dbx.API}/_emulator/actions/trigger_webhook", json={"accounts": [dbx.MAYA]})
    assert r.status_code == 200 and len(r.json()["delivered"]) == 1
    assert receiver.payloads[0]["list_folder"]["accounts"] == [await dbx.account_id(dbx.MAYA)]
    removed = await dbx.client.post(
        f"{dbx.API}/_emulator/actions/remove_webhook", json={"app_key": "team-app"}
    )
    assert removed.json() == {"removed": True}
    receiver.bodies.clear()
    await dbx.put(maya, "/Client Engagements/y.txt", b"y", path_root=await dbx.root(maya))
    assert receiver.bodies == []


async def test_async_delivery_mode_and_manual_flush() -> None:
    from emulators.core.hub import EmulatorHub

    for mode in ("async", "manual"):
        hub = EmulatorHub(providers=["dropbox"], anchor="2026-10-16", webhook_mode=mode)
        receiver = Receiver()
        hub.webhook_transport = httpx.ASGITransport(app=receiver.app)
        async with httpx.AsyncClient(transport=hub.async_transport(), timeout=30) as client:
            base = "https://api.dropboxapi.com"
            reg = await client.post(
                f"{base}/_emulator/actions/configure_webhook",
                json={"url": "http://consumer.test/hook", "app_secret": "s", "app_key": "my-app"},
            )
            assert reg.status_code == 200
            token = (
                await client.post(
                    f"{base}/_emulator/tokens",
                    json={"user": "maya.chen@halcyonadvisory.example", "client_id": "my-app"},
                )
            ).json()["access_token"]
            r = await client.post(
                "https://content.dropboxapi.com/2/files/upload",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Dropbox-API-Arg": '{"path": "/w.txt"}',
                    "Content-Type": "application/octet-stream",
                },
                content=b"x",
            )
            assert r.status_code == 200
            if mode == "manual":
                assert receiver.bodies == []
                await client.post(f"{base}/_emulator/webhooks/flush")
            else:
                await hub.drain_webhooks()
            assert len(receiver.bodies) == 1
            log = (
                await client.get(f"{base}/_emulator/webhooks", params={"event_type": "file_changes"})
            ).json()
            assert log["count"] == 1 and log["deliveries"][0]["status"] == "delivered"
