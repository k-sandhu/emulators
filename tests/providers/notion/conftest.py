from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from emulators.providers.notion.common import INTERNAL_TOKEN, OLD

BASE = "https://api.notion.com"


@pytest.fixture
async def client(make_client: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(
        "notion", base_url=BASE, headers={"Authorization": f"Bearer {INTERNAL_TOKEN}", "Notion-Version": OLD}
    ) as client:
        yield client


async def delegated(client: httpx.AsyncClient, user: str, **kwargs: Any) -> dict[str, str]:
    response = await client.post("/_emulator/tokens", json={"user": user, **kwargs})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}
