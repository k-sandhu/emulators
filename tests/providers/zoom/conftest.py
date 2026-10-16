"""Zoom provider fixtures: an in-process client on the real hostnames plus token helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from emulators.testing.helpers import bearer, mint

API = "https://api.zoom.us"
OAUTH = "https://zoom.us"
V2 = API + "/v2"

OWNER = "eleanor.whitfield@halcyonadvisory.example"
ADMIN = "arjun.mehta@halcyonadvisory.example"  # world admin (p035) -> Zoom role "Admin"
HOST = "benjamin.adler@halcyonadvisory.example"  # p019, hosts five recurring client status meetings
MAYA = "maya.chen@halcyonadvisory.example"  # p018, hosts the weekly Advisory Deal Review
STAFF = "ryan.murphy@halcyonadvisory.example"  # p008, hosts nothing and is not an admin

#: Meetings seeded from the world (see docs/providers/zoom.md).
BLUEFIN_ID = 6448198817  # recurring "Bluefin Seafood - Status Update", host Benjamin Adler
ORCHID_ID = (
    4442862647  # recurring "Orchid Hospitality Group - Status Update" (three recordings, no awkward UUIDs)
)
ADVISORY_ID = 3507027577  # weekly "Advisory Deal Review", host Maya Chen


def double_encode(uuid: str) -> str:
    """How API clients must put a UUID starting with '/' or containing '//' into a path."""
    return quote(quote(uuid, safe=""), safe="")


def single_encode(uuid: str) -> str:
    return quote(uuid, safe="")


@pytest.fixture
async def zoom(make_client: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client("zoom") as client:
        yield client


@pytest.fixture
def ctx(zoom: httpx.AsyncClient) -> Any:
    return zoom.hub.context("zoom")  # type: ignore[attr-defined]


async def token_for(client: httpx.AsyncClient, user: str | None = None, **payload: Any) -> dict[str, str]:
    token = await mint(client, API, **({"user": user} if user else {"app_only": True}), **payload)
    return bearer(token)


@pytest.fixture
async def s2s(zoom: httpx.AsyncClient) -> dict[str, str]:
    """Account-level (Server-to-Server) credentials, unrestricted scopes."""
    return await token_for(zoom)


@pytest.fixture
async def host(zoom: httpx.AsyncClient) -> dict[str, str]:
    return await token_for(zoom, HOST)


@pytest.fixture
async def maya(zoom: httpx.AsyncClient) -> dict[str, str]:
    return await token_for(zoom, MAYA)


@pytest.fixture
async def staff(zoom: httpx.AsyncClient) -> dict[str, str]:
    return await token_for(zoom, STAFF)


@pytest.fixture
async def admin(zoom: httpx.AsyncClient) -> dict[str, str]:
    return await token_for(zoom, ADMIN)


def instances_of(ctx: Any, meeting_id: int) -> list[dict[str, Any]]:
    return sorted(
        (i for i in ctx.store.collection("instances") if i["meeting_id"] == meeting_id),
        key=lambda i: i["start_time"],
    )


def special_uuids(ctx: Any) -> tuple[str, str]:
    """A recorded instance whose UUID starts with '/', and one containing '//'."""
    uuids = [r["id"] for r in ctx.store.collection("recordings")]
    return (
        next(u for u in uuids if u.startswith("/")),
        next(u for u in uuids if "//" in u and not u.startswith("/")),
    )
