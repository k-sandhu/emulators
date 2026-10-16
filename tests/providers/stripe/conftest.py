from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from emulators.core.clock import Clock
from emulators.core.hub import EmulatorHub
from emulators.providers.stripe.wire import SECRET_KEY


@pytest.fixture
def stripe_hub() -> EmulatorHub:
    return EmulatorHub(
        providers=["stripe"],
        anchor="2026-10-16",
        clock=Clock(datetime(2026, 10, 16, tzinfo=UTC)),
        webhook_mode="sync",
    )


@pytest.fixture
async def client(stripe_hub: EmulatorHub) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=stripe_hub.async_transport(),
        base_url="https://api.stripe.com",
        headers={"Authorization": "Bearer " + SECRET_KEY},
    ) as client:
        yield client


async def post(
    client: httpx.AsyncClient, path: str, data: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    response = await client.post("/v1/" + path, data=data or {}, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


async def get(client: httpx.AsyncClient, path: str, **kwargs: Any) -> dict[str, Any]:
    response = await client.get("/v1/" + path, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()
