"""httpx transports that serve vendor hostnames from an :class:`EmulatorHub` in-process.

Consumer code keeps its real URLs (``https://graph.microsoft.com/v1.0/me``) and only
swaps the transport::

    hub = EmulatorHub()
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        r = await client.get("https://graph.microsoft.com/v1.0/me", headers=auth)

Requests for hostnames no provider claims are passed to ``fallback`` (a real network
transport by default) or rejected when ``strict=True`` — useful to prove a test never
leaks to the internet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from emulators.core.hub import EmulatorHub


class UnroutedHostError(httpx.ConnectError):
    pass


class AsyncEmulatorTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        hub: EmulatorHub,
        *,
        fallback: httpx.AsyncBaseTransport | None = None,
        strict: bool = False,
    ) -> None:
        self.hub = hub
        self._asgi = httpx.ASGITransport(app=hub.gateway)  # type: ignore[arg-type]
        self._fallback = fallback
        self.strict = strict

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if self.hub.provider_for_host(host) is not None:
            return await self._asgi.handle_async_request(request)
        if self.strict:
            raise UnroutedHostError(f"emulators: no provider serves host '{host}'", request=request)
        if self._fallback is None:
            self._fallback = httpx.AsyncHTTPTransport()
        return await self._fallback.handle_async_request(request)

    async def aclose(self) -> None:
        if self._fallback is not None:
            await self._fallback.aclose()
