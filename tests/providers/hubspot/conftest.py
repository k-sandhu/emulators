from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer

BASE = "https://api.hubapi.com"


@pytest.fixture
async def client(make_hub: Callable[..., EmulatorHub]) -> AsyncIterator[httpx.AsyncClient]:
    hub: EmulatorHub = make_hub("hubspot")
    async with httpx.AsyncClient(transport=hub.async_transport(), base_url=BASE) as result:
        result.hub = hub  # type: ignore[attr-defined]
        info = (await result.get("/_emulator/info")).json()
        result.headers.update(bearer(info["credentials"]["private_app_token"]))
        yield result
