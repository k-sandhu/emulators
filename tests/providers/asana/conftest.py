from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

BASE = "https://app.asana.com/api/1.0"


@pytest.fixture
async def asana_client(make_client: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(
        "asana", base_url=BASE, headers={"Authorization": "Bearer asana-dev-p018"}
    ) as client:
        yield client


async def workspace(client: httpx.AsyncClient) -> str:
    response = await client.get(BASE + "/workspaces")
    assert response.status_code == 200, response.text
    return response.json()["data"][0]["gid"]


async def project(client: httpx.AsyncClient, **extra: Any) -> dict[str, Any]:
    response = await client.post(
        BASE + "/projects",
        json={"data": {"name": "Contract project", "workspace": await workspace(client), **extra}},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def task(client: httpx.AsyncClient, **extra: Any) -> dict[str, Any]:
    data = {"name": "Contract task", **extra}
    if not data.get("projects") and not data.get("parent"):
        data.setdefault("workspace", await workspace(client))
    response = await client.post(BASE + "/tasks", json={"data": data})
    assert response.status_code == 201, response.text
    return response.json()["data"]
