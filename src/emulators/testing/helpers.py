"""Small helpers for tests that drive emulators over HTTP."""

from __future__ import annotations

from typing import Any

import httpx


async def mint(client: httpx.AsyncClient, base: str, **payload: Any) -> str:
    """Mint an access token through the control plane.

    ``user`` is a world person id (``"p018"``) or email; ``scopes``, ``client_id``,
    ``app_only`` and ``expires_in`` are optional.
    """
    r = await client.post(f"{base.rstrip('/')}/_emulator/tokens", json=payload)
    r.raise_for_status()
    return r.json()["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
