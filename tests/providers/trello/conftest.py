"""Clients retain Trello's production hostname and key/token authentication."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from emulators.core.hub import EmulatorHub

BASE = "https://api.trello.com"


@pytest.fixture
async def client(make_hub: Callable[..., EmulatorHub]) -> AsyncIterator[httpx.AsyncClient]:
    hub = make_hub("trello")
    async with httpx.AsyncClient(transport=hub.async_transport(), base_url=BASE) as client:
        client.hub = hub  # type: ignore[attr-defined]
        credentials = (await client.get("/_emulator/info")).json()["credentials"]
        client.params = httpx.QueryParams({"key": credentials["key"], "token": credentials["token"]})
        yield client


async def mint(client: httpx.AsyncClient, user: str = "p001", **extra: Any) -> dict[str, Any]:
    response = await client.post("/_emulator/tokens", json={"user": user, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def download_auth(client: httpx.AsyncClient, token: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f'OAuth oauth_consumer_key="{client.params["key"]}", oauth_token="{token or client.params["token"]}"'
    }


@pytest.fixture
async def board(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post("/1/boards", json={"name": "Integration board"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def card(client: httpx.AsyncClient, board: dict[str, Any]) -> dict[str, Any]:
    lists = (await client.get(f"/1/boards/{board['id']}/lists")).json()
    response = await client.post(
        "/1/cards", json={"idList": lists[0]["id"], "name": "Review work", "desc": "Client approval"}
    )
    assert response.status_code == 200, response.text
    return response.json()
