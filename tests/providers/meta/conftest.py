"""Fixtures for the Meta provider tests: Graph client, ids, webhook receiver."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from emulators.providers.meta.common import (
    APP_ID,
    APP_SECRET,
    PAGE_ACCESS_TOKEN,
    SYSTEM_USER_TOKEN,
    USER_ACCESS_TOKEN,
    VERIFY_TOKEN,
    digits_only,
)

GRAPH = "https://graph.facebook.com/v21.0"
GRAPH_HOST = "https://graph.facebook.com"
HOOK_URL = "http://consumer.test/hook"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


SYS = auth(SYSTEM_USER_TOKEN)
PAGE = auth(PAGE_ACCESS_TOKEN)
USER = auth(USER_ACCESS_TOKEN)


@pytest.fixture
async def client(make_client: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client("meta") as http:
        yield http


@pytest.fixture
async def ids(client: httpx.AsyncClient) -> SimpleNamespace:
    """Seeded ids/credentials, read from the control plane like a test writer would."""
    r = await client.get(f"{GRAPH_HOST}/_emulator/info")
    r.raise_for_status()
    creds = r.json()["credentials"]
    return SimpleNamespace(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        verify_token=VERIFY_TOKEN,
        business_id=creds["business_id"],
        waba_id=creds["waba_id"],
        phone_number_id=creds["phone_number_id"],
        page_id=creds["page_id"],
        system_user_id=creds["system_user_id"],
    )


def wa_number(client: httpx.AsyncClient, display_name: str) -> str:
    """E.164 (digits) for a seeded WhatsApp contact by world display name."""
    hub = client.hub  # type: ignore[attr-defined]
    for conv in hub.world.conversations:
        if conv.channel != "whatsapp":
            continue
        person = hub.world.person(conv.person_id)
        if person.display_name == display_name:
            return digits_only(person.phone_mobile or "")
    raise AssertionError(f"no whatsapp contact named {display_name}")


# Paula Iwasaki has a recent inbound (24h window open); Hamid Novak's is closed.
OPEN_WA_NAME = "Paula Iwasaki"
CLOSED_WA_NAME = "Hamid Novak"


class Receiver:
    """A consumer webhook endpoint: handles hub.challenge and captures deliveries."""

    def __init__(self, expected_verify_token: str = VERIFY_TOKEN) -> None:
        self.expected_verify_token = expected_verify_token
        self.deliveries: list[dict[str, Any]] = []
        self.verifications: list[dict[str, str]] = []
        self.app = FastAPI()

        @self.app.get("/hook")
        async def verify(request: Request) -> PlainTextResponse:
            params = request.query_params
            self.verifications.append(dict(params))
            if (
                params.get("hub.mode") == "subscribe"
                and params.get("hub.verify_token") == self.expected_verify_token
            ):
                return PlainTextResponse(params.get("hub.challenge", ""))
            return PlainTextResponse("forbidden", status_code=403)

        @self.app.post("/hook")
        async def receive(request: Request) -> dict[str, Any]:
            body = await request.body()
            self.deliveries.append(
                {
                    "body": body,
                    "headers": dict(request.headers),
                    "json": json.loads(body) if body else None,
                }
            )
            return {}

    def payloads(self, obj: str | None = None) -> list[dict[str, Any]]:
        return [d["json"] for d in self.deliveries if obj is None or (d["json"] or {}).get("object") == obj]

    async def configure(self, client: httpx.AsyncClient, obj: str) -> dict[str, Any]:
        r = await client.post(
            f"{GRAPH_HOST}/_emulator/actions/configure_webhook",
            json={"object": obj, "callback_url": HOOK_URL, "verify_token": VERIFY_TOKEN},
        )
        r.raise_for_status()
        return r.json()


@pytest.fixture
async def receiver(client: httpx.AsyncClient) -> AsyncIterator[Receiver]:
    hook = Receiver()
    client.hub.webhook_transport = httpx.ASGITransport(app=hook.app)  # type: ignore[attr-defined]
    yield hook
    client.hub.webhook_transport = None  # type: ignore[attr-defined]


async def action(client: httpx.AsyncClient, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    r = await client.post(f"{GRAPH_HOST}/_emulator/actions/{name}", json=payload)
    r.raise_for_status()
    return r.json()


async def mint(client: httpx.AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    r = await client.post(f"{GRAPH_HOST}/_emulator/tokens", json=payload)
    r.raise_for_status()
    return r.json()
