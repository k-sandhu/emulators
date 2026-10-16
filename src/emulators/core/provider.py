"""Provider contract: how an emulated SaaS plugs into the hub.

A provider package (``emulators.providers.<name>``) exposes a module attribute
``PROVIDER`` — an instance of a :class:`Provider` subclass. The hub discovers it, gives
it a :class:`ProviderContext` (store, clock, ids, tokens, webhooks, world...), calls
:meth:`Provider.seed` to project the shared synthetic organisation into the vendor's
data model, and mounts the app built by :meth:`Provider.build`.

Provider modules never import each other; anything shared lives in ``emulators.core``
or ``emulators.world``.
"""

from __future__ import annotations

import fnmatch
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request

from emulators.core.auth import SigningKey, TokenService
from emulators.core.clock import Clock
from emulators.core.ids import IdFactory
from emulators.core.middleware import FaultInjector, FaultRule, RequestJournal
from emulators.core.store import Store
from emulators.core.webhooks import WebhookDispatcher

if TYPE_CHECKING:
    from emulators.core.hub import EmulatorHub
    from emulators.world.model import World

ActionHandler = Callable[["ProviderContext", dict[str, Any]], Awaitable[Any] | Any]


@dataclass
class ProviderContext:
    """Everything a provider needs at runtime. One per provider per hub."""

    name: str
    provider: Provider
    store: Store
    clock: Clock
    ids: IdFactory
    tokens: TokenService
    webhooks: WebhookDispatcher
    journal: RequestJournal
    faults: FaultInjector
    world: World
    hub: EmulatorHub
    settings: dict[str, str] = field(default_factory=dict)

    @property
    def signing_key(self) -> SigningKey:
        return SigningKey.shared()

    def setting(self, key: str, default: str | None = None) -> str | None:
        """``EMULATORS_<PROVIDER>_<KEY>`` env vars, overridable per hub."""
        return self.settings.get(key.upper(), default)


def external_base(request: Request) -> str:
    """Base URL the client used to reach this provider (no trailing slash)."""
    base = request.scope.get("state", {}).get("external_base")
    if base:
        return base.rstrip("/")
    return str(request.base_url).rstrip("/")


def host_mode(request: Request, ctx: ProviderContext) -> bool:
    """True when the client addressed a real vendor hostname (``graph.microsoft.com``)."""
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(":")[0]
    return ctx.provider.matches_host(host)


def url_for_host(request: Request, ctx: ProviderContext, real_host: str, path: str) -> str:
    """Absolute URL on another vendor host, e.g. a SharePoint download or a Zoom
    ``download_url``. In host-routing mode the real hostname is used (so clients' host
    allowlists accept it); otherwise the URL stays on the base the client is using."""
    if not path.startswith("/"):
        path = "/" + path
    if host_mode(request, ctx):
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host_header = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        port = host_header.split(":")[1] if ":" in host_header else ""
        default_port = (proto == "https" and port == "443") or (proto == "http" and port == "80")
        suffix = f":{port}" if port and not default_port else ""
        return f"{proto}://{real_host}{suffix}{path}"
    return external_base(request) + path


class Provider:
    """Base class for provider definitions. Subclasses set the class attributes."""

    name: str = ""
    title: str = ""
    #: Real vendor hostnames this provider answers for; ``*`` wildcards allowed.
    hosts: tuple[str, ...] = ()
    #: Dedicated port when running one listener per provider.
    default_port: int = 0
    description: str = ""
    #: Shown by ``/_emulator/info`` and ``emulators info``: well-known test credentials.
    credentials: dict[str, Any] = {}

    def __init__(self) -> None:
        self.actions: dict[str, ActionHandler] = {}

    # -- lifecycle -----------------------------------------------------------------
    def seed(self, ctx: ProviderContext) -> None:
        """Populate ``ctx.store`` from ``ctx.world``. Must be deterministic."""

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        """Register the vendor API routes on ``app``."""
        raise NotImplementedError

    # -- errors --------------------------------------------------------------------
    def error_body(self, status: int, code: str, message: str) -> Any:
        """The vendor's error envelope; used for 404s, validation errors and faults."""
        return {"error": {"code": code, "message": message}}

    def error_body_for(self, scope: dict[str, Any] | None, status: int, code: str, message: str) -> Any:
        """Route-aware error envelope. Override when one provider serves APIs with different
        error styles (Google Meet vs Drive, Microsoft login vs Graph). ``scope`` is the ASGI
        scope (``scope["path"]``, headers...) or ``None``."""
        return self.error_body(status, code, message)

    def error_headers_for(self, scope: dict[str, Any] | None, status: int) -> dict[str, str]:
        """Extra headers on emulator-generated errors (request ids, API version echoes)."""
        return {}

    def fault_response(
        self, rule: FaultRule, scope: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        status = rule.status or 500
        headers = {
            "content-type": "application/json",
            **self.error_headers_for(scope, status),
            **rule.headers,
        }
        if status == 429 and not any(k.lower() == "retry-after" for k in headers):
            headers["Retry-After"] = "2"
        if rule.body is not None:
            body = rule.body if isinstance(rule.body, (bytes, str)) else json.dumps(rule.body)
        else:
            code, message = FAULT_DEFAULTS.get(status, ("InternalServerError", "Injected fault."))
            body = json.dumps(self.error_body_for(scope, status, code, message))
        return status, headers, body.encode() if isinstance(body, str) else body

    # -- helpers -------------------------------------------------------------------
    def matches_host(self, host: str) -> bool:
        host = host.lower().split(":")[0]
        return any(fnmatch.fnmatch(host, pattern) for pattern in self.hosts)

    def action(self, name: str) -> Callable[[ActionHandler], ActionHandler]:
        """Register a ``POST /_emulator/actions/<name>`` handler (simulate inbound SMS,
        end a meeting, advance a subscription...)."""

        def decorator(fn: ActionHandler) -> ActionHandler:
            self.actions[name] = fn
            return fn

        return decorator

    def info(self, ctx: ProviderContext) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "hosts": list(self.hosts),
            "default_port": self.default_port,
            "description": self.description,
            "credentials": self.credentials,
            "actions": sorted(self.actions),
            "collections": {
                n: len(ctx.store.collection(n)) for n in ctx.store.names() if not n.startswith("_")
            },
        }


FAULT_DEFAULTS: dict[int, tuple[str, str]] = {
    400: ("BadRequest", "Injected bad request."),
    401: ("InvalidAuthenticationToken", "Access token has expired or is not yet valid."),
    403: ("Forbidden", "Injected forbidden response."),
    404: ("NotFound", "Injected not found."),
    409: ("Conflict", "Injected conflict."),
    410: ("Gone", "Injected gone (expired cursor)."),
    429: ("TooManyRequests", "Too many requests. Please retry after the specified delay."),
    500: ("InternalServerError", "Injected internal server error."),
    502: ("BadGateway", "Injected bad gateway."),
    503: ("ServiceUnavailable", "The service is temporarily unavailable."),
    504: ("GatewayTimeout", "Injected gateway timeout."),
}


def env_settings(provider: str) -> dict[str, str]:
    prefix = f"EMULATORS_{provider.upper()}_"
    return {k[len(prefix) :]: v for k, v in os.environ.items() if k.startswith(prefix)}
