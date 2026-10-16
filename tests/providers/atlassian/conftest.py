from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint

SITE = "https://halcyonadvisory.atlassian.net"


@pytest.fixture
def hub(make_hub: Callable[..., EmulatorHub]) -> EmulatorHub:
    return make_hub("atlassian")


@pytest.fixture
async def client(hub: EmulatorHub) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=hub.async_transport(), base_url=SITE) as client:
        yield client


@pytest.fixture
async def auth(client: httpx.AsyncClient) -> dict[str, str]:
    return bearer(await mint(client, SITE, user="p001"))
