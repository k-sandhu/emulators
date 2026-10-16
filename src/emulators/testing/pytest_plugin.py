"""pytest plugin (auto-registered via the ``pytest11`` entry point).

Fixtures:

* ``emulator_hub`` (session) — an :class:`~emulators.core.hub.EmulatorHub` with every
  provider (or ``EMULATORS_PROVIDERS``), seeded once per session.
* ``emulators`` (function) — the same hub, reset to the seeded state before each test.
* ``emulator_transport`` — an ``httpx.AsyncBaseTransport`` serving vendor hostnames
  in-process; pass it wherever your code accepts ``transport=``.
* ``emulator_server`` (session) — the gateway on a real localhost port in a thread.
* ``emulator_intercept`` — routes *all* httpx/requests traffic for vendor hostnames to
  ``emulator_server`` for the duration of the test (for code without a transport seam).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from emulators.core.hub import EmulatorHub
    from emulators.testing.server import EmulatorServer


@pytest.fixture(scope="session")
def emulator_hub() -> EmulatorHub:
    from emulators.core.hub import EmulatorHub

    return EmulatorHub(webhook_mode="sync")


@pytest.fixture
def emulators(emulator_hub: EmulatorHub) -> EmulatorHub:
    emulator_hub.reset()
    return emulator_hub


@pytest.fixture
def emulator_transport(emulators: EmulatorHub):
    return emulators.async_transport()


@pytest.fixture(scope="session")
def emulator_server(emulator_hub: EmulatorHub) -> Iterator[EmulatorServer]:
    from emulators.testing.server import EmulatorServer

    with EmulatorServer(emulator_hub) as server:
        yield server


@pytest.fixture
def emulator_intercept(emulator_server: EmulatorServer, emulators: EmulatorHub) -> Iterator[EmulatorServer]:
    from emulators.testing.intercept import intercept

    with intercept(emulator_server):
        yield emulator_server
