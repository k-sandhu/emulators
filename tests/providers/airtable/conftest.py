"""Real Airtable URLs with deterministic clock and owner PAT."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from emulators.core.clock import parse_instant
from emulators.providers.airtable.common import OWNER_TOKEN

API = "https://api.airtable.com"
CONTENT = "https://content.airtable.com"


@pytest.fixture
async def client(make_client: Callable[..., Any]) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client("airtable", base_url=API) as instance:
        instance.hub.clock.freeze(parse_instant("2026-10-16T12:00:00Z"))
        instance.headers["Authorization"] = "Bearer " + OWNER_TOKEN
        yield instance


async def base_id(client: httpx.AsyncClient, *, finance: bool = False) -> str:
    response = await client.get("/v0/meta/bases")
    assert response.status_code == 200, response.text
    return next(b["id"] for b in response.json()["bases"] if ("Finance" in b["name"]) == finance)


async def table_url(client: httpx.AsyncClient, name: str = "Clients", *, finance: bool = False) -> str:
    return API + "/v0/" + await base_id(client, finance=finance) + "/" + name
