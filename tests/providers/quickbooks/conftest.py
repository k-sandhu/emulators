"""Fixtures for QuickBooks provider tests (real Intuit hostnames, transport swapped)."""

from __future__ import annotations

import httpx
import pytest

REALM = "9341569385067351"
API = f"https://sandbox-quickbooks.api.intuit.com/v3/company/{REALM}"
HOST = "https://sandbox-quickbooks.api.intuit.com"


@pytest.fixture
async def qb(make_client):
    """Yield (client, headers) with an app-only token."""
    async with make_client("quickbooks") as client:
        r = await client.post(f"{HOST}/_emulator/tokens", json={"app_only": True})
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        yield client, {"Authorization": f"Bearer {token}", "Accept": "application/json"}


@pytest.fixture
async def qb_client(make_client):
    async with make_client("quickbooks") as client:
        yield client


async def mint_user(client: httpx.AsyncClient, user: str) -> dict[str, str]:
    r = await client.post(f"{HOST}/_emulator/tokens", json={"user": user})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}", "Accept": "application/json"}
