"""Run a hub's gateway on a real localhost port in a background thread.

Used by the ``emulator_server`` fixture, by :mod:`emulators.testing.intercept` (which
reroutes vendor hostnames here) and by SDKs that need a base URL::

    with EmulatorServer(EmulatorHub(providers=["stripe"])) as server:
        stripe.api_base = server.url_for("stripe")      # http://127.0.0.1:53817/stripe
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from types import TracebackType
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from emulators.core.hub import EmulatorHub


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class EmulatorServer:
    def __init__(self, hub: EmulatorHub, *, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.hub = hub
        self.host = host
        self.port = port or free_port()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url_for(self, provider: str) -> str:
        """Path-prefix base URL for one provider (``http://127.0.0.1:PORT/<provider>``)."""
        return f"{self.url}/{provider}"

    def start(self) -> EmulatorServer:
        config = uvicorn.Config(
            self.hub.gateway,
            host=self.host,
            port=self.port,
            log_level="warning",
            lifespan="off",
            proxy_headers=False,
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

        def run() -> None:
            asyncio.run(self._server.serve())  # type: ignore[union-attr]

        self._thread = threading.Thread(target=run, name=f"emulators-{self.port}", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("emulator server failed to start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._server = None
        self._thread = None

    def __enter__(self) -> EmulatorServer:
        return self.start()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.stop()
