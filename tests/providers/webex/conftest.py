from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from emulators.core.clock import Clock
from emulators.core.hub import EmulatorHub


@pytest.fixture
def hub() -> EmulatorHub:
    return EmulatorHub(
        providers=["webex"],
        anchor="2026-10-16",
        webhook_mode="sync",
        clock=Clock(datetime(2026, 10, 16, 12, tzinfo=UTC)),
    )


@pytest.fixture
async def client(hub: EmulatorHub) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=hub.async_transport(),
        base_url="https://webexapis.com",
        headers={"Authorization": "Bearer webex-host-token"},
    ) as client:
        yield client
