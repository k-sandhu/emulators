"""Shared fixtures for provider tests.

Provider tests talk to the emulator exactly like a consumer would: real vendor URLs,
real HTTP semantics, only the transport swapped::

    from emulators.testing.helpers import bearer, mint

    async def test_me(make_client):
        async with make_client("microsoft") as client:
            token = await mint(client, "https://graph.microsoft.com", user="p018")
            r = await client.get("https://graph.microsoft.com/v1.0/me",
                                 headers={"Authorization": f"Bearer {token}"})
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

import httpx
import pytest

from emulators.core.clock import parse_instant
from emulators.core.hub import EmulatorHub

ANCHOR = "2026-10-16"
MESSAGING_TIME = parse_instant(f"{ANCHOR}T12:00:00Z")
MESSAGING_PROVIDERS = {"meta", "twilio"}
# Pin the world for the plugin's session hub (emulator_server / emulator_intercept) too.
os.environ.setdefault("EMULATORS_ANCHOR", ANCHOR)
if os.name == "nt":
    # A stale, ACL-locked %TEMP%/pytest-of-<user> breaks tmp_path on some Windows machines.
    import tempfile
    from pathlib import Path

    _root = Path(tempfile.gettempdir()) / "emulators-pytest"
    _root.mkdir(exist_ok=True)
    os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_root))


@lru_cache(maxsize=32)
def _hub(providers: tuple[str, ...]) -> EmulatorHub:
    return EmulatorHub(providers=list(providers), anchor=ANCHOR, webhook_mode="sync")


@pytest.fixture
def make_hub() -> Callable[..., EmulatorHub]:
    """``make_hub("microsoft")`` → a hub (cached per provider set) reset to seed state."""

    def factory(*providers: str) -> EmulatorHub:
        hub = _hub(tuple(providers))
        hub.reset()
        hub.webhook_transport = None
        if MESSAGING_PROVIDERS.intersection(providers) and not os.environ.get("EMULATORS_CLOCK"):
            # Messaging windows and scheduled sends must not age with the calendar.
            hub.clock.freeze(MESSAGING_TIME)
        return hub

    return factory


@pytest.fixture
def emulators(emulator_hub: EmulatorHub, request: pytest.FixtureRequest) -> EmulatorHub:
    """Pin messaging SDK fixtures while retaining live timestamps for webhook verifiers."""
    emulator_hub.reset()
    messaging_sdk = any(request.path.stem.startswith(f"test_{provider}_") for provider in MESSAGING_PROVIDERS)
    if messaging_sdk and not os.environ.get("EMULATORS_CLOCK"):
        emulator_hub.clock.freeze(MESSAGING_TIME)
    return emulator_hub


@pytest.fixture
def make_client(make_hub: Callable[..., EmulatorHub]) -> Callable[..., Any]:
    @asynccontextmanager
    async def factory(*providers: str, **kwargs: Any) -> AsyncIterator[httpx.AsyncClient]:
        hub = make_hub(*providers)
        async with httpx.AsyncClient(transport=hub.async_transport(), timeout=30, **kwargs) as client:
            client.hub = hub  # type: ignore[attr-defined]
            yield client

    return factory
