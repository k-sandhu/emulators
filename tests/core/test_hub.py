"""End-to-end checks of the core: routing modes, control plane, faults, tokens, webhooks."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request

from emulators.core.auth import bearer_token
from emulators.core.errors import EmulatorError
from emulators.core.hub import EmulatorHub
from emulators.core.provider import Provider, ProviderContext, external_base, url_for_host


class EchoProvider(Provider):
    name = "echo"
    title = "Echo"
    hosts = ("api.echo.example", "*.files.echo.example")
    default_port = 49999

    def seed(self, ctx: ProviderContext) -> None:
        for person in ctx.world.employees[:5]:
            ctx.store.collection("users").put({"id": person.id, "email": person.email})

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        @app.get("/v1/users")
        async def users(request: Request) -> dict:
            grant = ctx.tokens.resolve(bearer_token(request))
            if grant is None:
                raise EmulatorError(401, self.error_body(401, "Unauthorized", "bad token"))
            return {
                "data": ctx.store.collection("users").values(),
                "self": external_base(request) + "/v1/users",
                "download": url_for_host(request, ctx, "eu.files.echo.example", "/blob/1"),
            }

        @app.post("/v1/notify")
        async def notify(payload: dict) -> dict:
            record = await ctx.webhooks.deliver(payload["url"], b'{"ok":true}', event_type="ping")
            return {"delivery": record["status"]}


@pytest.fixture
def hub() -> EmulatorHub:
    return EmulatorHub(providers=["echo"], extra_providers=[EchoProvider()], webhook_mode="sync")


async def token(client: httpx.AsyncClient, base: str) -> str:
    r = await client.post(f"{base}/_emulator/tokens", json={"user": "p001"})
    return r.json()["access_token"]


async def test_host_routing_keeps_real_hostnames(hub: EmulatorHub) -> None:
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        tok = await token(client, "https://api.echo.example")
        r = await client.get("https://api.echo.example/v1/users", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 5
    assert body["self"] == "https://api.echo.example/v1/users"
    assert body["download"] == "https://eu.files.echo.example/blob/1"


async def test_prefix_routing_rewrites_links(hub: EmulatorHub) -> None:
    transport = httpx.ASGITransport(app=hub.gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080") as client:
        tok = await token(client, "/echo")
        r = await client.get("/echo/v1/users", headers={"Authorization": f"Bearer {tok}"})
        index = await client.get("/")
    assert r.status_code == 200, r.text
    assert r.json()["self"] == "http://localhost:44080/echo/v1/users"
    assert r.json()["download"] == "http://localhost:44080/echo/blob/1"
    assert "echo" in index.json()["emulators"]


async def test_auth_faults_journal_and_reset(hub: EmulatorHub) -> None:
    async with httpx.AsyncClient(
        transport=hub.async_transport(), base_url="https://api.echo.example"
    ) as client:
        assert (await client.get("/v1/users")).status_code == 401
        tok = await token(client, "")
        auth = {"Authorization": f"Bearer {tok}"}
        await client.post("/_emulator/faults", json={"path": "/v1/users", "status": 429, "times": 1})
        throttled = await client.get("/v1/users", headers=auth)
        assert throttled.status_code == 429
        assert throttled.headers["retry-after"] == "2"
        assert (await client.get("/v1/users", headers=auth)).status_code == 200
        journal = (await client.get("/_emulator/requests", params={"path": "/v1/users"})).json()
        assert [e["status"] for e in journal["requests"]] == [401, 429, 200]
        assert journal["requests"][-1]["headers"]["authorization"].startswith("Bearer …")
        await client.put("/_emulator/state/users", json={"id": "x1", "email": "new@example.com"})
        assert len((await client.get("/v1/users", headers=auth)).json()["data"]) == 6
        await client.post("/_emulator/reset")
        tok = await token(client, "")
        users = await client.get("/v1/users", headers={"Authorization": f"Bearer {tok}"})
        assert len(users.json()["data"]) == 5


async def test_webhook_delivery_to_in_process_receiver(hub: EmulatorHub) -> None:
    received: list[bytes] = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> dict:
        received.append(await request.body())
        return {}

    hub.webhook_transport = httpx.ASGITransport(app=receiver)
    async with httpx.AsyncClient(
        transport=hub.async_transport(), base_url="https://api.echo.example"
    ) as client:
        r = await client.post("/v1/notify", json={"url": "http://consumer.test/hook"})
        assert r.json() == {"delivery": "delivered"}
        log = (await client.get("/_emulator/webhooks")).json()
    assert received == [b'{"ok":true}']
    assert log["deliveries"][0]["response_status"] == 200


async def test_unknown_host_rejected_in_strict_mode(hub: EmulatorHub) -> None:
    from emulators.testing.transport import AsyncEmulatorTransport, UnroutedHostError

    async with httpx.AsyncClient(transport=AsyncEmulatorTransport(hub, strict=True)) as client:
        with pytest.raises(UnroutedHostError):
            await client.get("https://example.org/")
