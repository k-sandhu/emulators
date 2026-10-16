"""The hub: discovers providers, seeds them from one shared world, and routes traffic.

Routing (see :meth:`EmulatorHub.gateway`):

1. **Host routing** — a request whose ``Host`` is a real vendor hostname
   (``graph.microsoft.com``, ``api.stripe.com``, ``acme-my.sharepoint.com``...) goes to the
   provider that claims it. This is what the in-process httpx transport, the
   ``requests``/``httpx`` interceptors, Docker network aliases and the HTTPS gateway use,
   so consumer code keeps its hard-coded ``https://`` URLs.
2. **Path-prefix routing** — ``http://localhost:44080/<provider>/...`` for apps that let
   you override a base URL.
3. ``/_emulator/...`` on the gateway itself lists providers and resets everything.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import threading
from collections.abc import Iterable
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from emulators.core.app import build_provider_app
from emulators.core.auth import TokenService
from emulators.core.clock import Clock, parse_instant
from emulators.core.ids import IdFactory
from emulators.core.middleware import FaultInjector, RequestJournal
from emulators.core.provider import Provider, ProviderContext, env_settings
from emulators.core.store import BlobStore, Store
from emulators.core.webhooks import WebhookDispatcher

DEFAULT_SEED = "emulators"


def discover_providers(names: Iterable[str] | None = None) -> dict[str, Provider]:
    """Import ``emulators.providers.<pkg>`` modules exposing ``PROVIDER``.

    With ``names``, only those packages are imported, so a half-finished provider can't
    break a hub that doesn't use it.
    """
    import emulators.providers as root

    wanted = set(names) if names is not None else None
    found: dict[str, Provider] = {}
    for info in sorted(pkgutil.iter_modules(root.__path__), key=lambda m: m.name):
        if info.name.startswith("_") or (wanted is not None and info.name not in wanted):
            continue
        module = importlib.import_module(f"{root.__name__}.{info.name}")
        provider = getattr(module, "PROVIDER", None)
        if isinstance(provider, Provider):
            found[provider.name] = provider
    return found


class EmulatorHub:
    """A set of provider emulators sharing a clock, a seed and a synthetic world."""

    def __init__(
        self,
        providers: Iterable[str] | None = None,
        *,
        seed: str | None = None,
        anchor: str | None = None,
        clock: Clock | None = None,
        webhook_mode: str | None = None,
        webhook_transport: httpx.AsyncBaseTransport | None = None,
        settings: dict[str, dict[str, str]] | None = None,
        lenient_clients: bool = True,
        extra_providers: Iterable[Provider] = (),
    ) -> None:
        from emulators.world.generator import build_world

        self.seed = seed or os.environ.get("EMULATORS_SEED", DEFAULT_SEED)
        self.clock = clock or Clock(
            parse_instant(os.environ["EMULATORS_CLOCK"]) if os.environ.get("EMULATORS_CLOCK") else None
        )
        anchor_text = anchor or os.environ.get("EMULATORS_ANCHOR")
        anchor_dt = parse_instant(anchor_text) if anchor_text else self.clock.now()
        self.world = build_world(self.seed, anchor_dt)
        self.webhook_mode = webhook_mode or os.environ.get("EMULATORS_WEBHOOK_MODE", "async")
        self.webhook_transport = webhook_transport
        self.snapshots: dict[str, dict[str, Any]] = {}
        self._seeded: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

        wanted = list(providers) if providers is not None else _env_list("EMULATORS_PROVIDERS")
        extras = {extra.name: extra for extra in extra_providers}
        available = discover_providers([w for w in wanted if w not in extras] if wanted else None)
        available.update(extras)
        if wanted:
            unknown = sorted(set(wanted) - set(available))
            if unknown:
                raise ValueError(f"unknown provider(s) {unknown}; available: {sorted(available)}")
            available = {name: available[name] for name in wanted}
        self.providers = available
        self.contexts: dict[str, ProviderContext] = {}
        self.apps: dict[str, Any] = {}
        blobs = BlobStore()
        for name, provider in self.providers.items():
            store = Store(blobs)
            ids = IdFactory(f"{self.seed}/{name}")
            merged = {
                **env_settings(name),
                **{k.upper(): v for k, v in (settings or {}).get(name, {}).items()},
            }
            ctx = ProviderContext(
                name=name,
                provider=provider,
                store=store,
                clock=self.clock,
                ids=ids,
                tokens=TokenService(store, self.clock, lenient_clients=lenient_clients),
                webhooks=WebhookDispatcher(
                    name,
                    store,
                    self.clock,
                    ids.fork("webhooks"),
                    mode=self.webhook_mode,
                    transport_factory=lambda: self.webhook_transport,
                ),
                journal=RequestJournal(),
                faults=FaultInjector(),
                world=self.world,
                hub=self,
                settings=merged,
            )
            self.contexts[name] = ctx
            self._seed(name)
            self.apps[name] = build_provider_app(ctx)
        self._gateway: Starlette | None = None

    # -- state -----------------------------------------------------------------------
    def _seed(self, name: str) -> None:
        ctx = self.contexts[name]
        ctx.store.clear()
        ctx.provider.seed(ctx)
        self._seeded[name] = ctx.store.snapshot()

    def reset(self, name: str | None = None) -> None:
        """Restore seeded state, clear journals, faults and webhook logs."""
        with self._lock:
            for provider_name in [name] if name else list(self.contexts):
                ctx = self.contexts[provider_name]
                ctx.store.restore(self._seeded[provider_name])
                ctx.journal.clear()
                ctx.faults.clear()

    def context(self, name: str) -> ProviderContext:
        return self.contexts[name]

    def provider_for_host(self, host: str) -> str | None:
        host = host.lower().split(":")[0]
        for name, provider in self.providers.items():
            if provider.matches_host(host):
                return name
        return None

    def hosts(self) -> dict[str, list[str]]:
        return {name: list(p.hosts) for name, p in self.providers.items()}

    # -- routing ---------------------------------------------------------------------
    @property
    def gateway(self) -> Starlette:
        if self._gateway is None:
            self._gateway = self._build_gateway()
        return self._gateway

    def _build_gateway(self) -> Any:
        hub = self

        async def index(request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "emulators": {
                        name: {
                            "title": p.title,
                            "hosts": list(p.hosts),
                            "prefix": f"{str(request.base_url).rstrip('/')}/{name}",
                            "docs": f"{str(request.base_url).rstrip('/')}/{name}/_emulator/docs",
                        }
                        for name, p in hub.providers.items()
                    },
                    "seed": hub.seed,
                    "anchor": hub.world.anchor.isoformat(),
                }
            )

        async def reset_all(request: Request) -> JSONResponse:
            hub.reset()
            return JSONResponse({"status": "reset", "providers": list(hub.providers)})

        async def clock(request: Request) -> JSONResponse:
            from datetime import timedelta

            if request.method == "POST":
                payload = await request.json()
                if "advance_seconds" in payload:
                    hub.clock.advance(timedelta(seconds=float(payload["advance_seconds"])))
                if payload.get("freeze_at"):
                    hub.clock.freeze(parse_instant(payload["freeze_at"]))
                elif payload.get("freeze"):
                    hub.clock.freeze()
                if payload.get("unfreeze"):
                    hub.clock.unfreeze()
            return JSONResponse({"now": hub.clock.now().isoformat(), "frozen": hub.clock.frozen})

        async def health(request: Request) -> JSONResponse:
            return JSONResponse({"status": "ok", "providers": list(hub.providers)})

        routes: list[Any] = [
            Route("/", index),
            Route("/_emulator", index),
            Route("/_emulator/health", health),
            Route("/_emulator/reset", reset_all, methods=["POST"]),
            Route("/_emulator/clock", clock, methods=["GET", "POST"]),
        ]
        routes += [Mount(f"/{name}", app=app) for name, app in self.apps.items()]
        prefix_app = Starlette(routes=routes)

        async def dispatch(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] in ("http", "websocket"):
                host = ""
                for key, value in scope.get("headers", []):
                    if key == b"x-forwarded-host":
                        host = value.decode("latin-1")
                        break
                    if key == b"host":
                        host = value.decode("latin-1")
                name = hub.provider_for_host(host) if host else None
                if name is not None:
                    await hub.apps[name](scope, receive, send)
                    return
            elif scope["type"] == "lifespan":
                await prefix_app(scope, receive, send)
                return
            await prefix_app(scope, receive, send)

        return dispatch

    # -- testing helpers ------------------------------------------------------------
    def async_transport(self) -> httpx.AsyncBaseTransport:
        """An httpx transport that serves vendor hostnames from this hub in-process."""
        from emulators.testing.transport import AsyncEmulatorTransport

        return AsyncEmulatorTransport(self)

    async def drain_webhooks(self) -> None:
        for ctx in self.contexts.values():
            await ctx.webhooks.drain()


def _env_list(name: str) -> list[str] | None:
    raw = os.environ.get(name, "").strip()
    if not raw or raw.lower() == "all":
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]
