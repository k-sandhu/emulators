"""Zoom: OAuth, REST API v2 (users, groups, meetings, past meetings, reports, cloud recordings),
meeting summaries, downloads and webhooks."""

from __future__ import annotations

import importlib
from typing import Any

from fastapi import FastAPI

from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext
from emulators.providers.zoom import common

MODULES = ("seed", "oauth")


def _modules() -> list[Any]:
    loaded = []
    for name in MODULES:
        try:
            loaded.append(importlib.import_module(f"{__name__}.{name}"))
        except ModuleNotFoundError as exc:
            if exc.name != f"{__name__}.{name}":
                raise
    return loaded


class ZoomProvider(Provider):
    name = "zoom"
    title = "Zoom"
    hosts = ("zoom.us", "api.zoom.us", "*.zoom.us", "marketplace.zoom.us", "file.zoom.us")
    default_port = 44106
    description = "Zoom OAuth, REST API v2 (users, meetings, past meetings, cloud recordings) and webhooks."
    credentials = {
        "client_id": "any (lenient); register_app pins scopes and redirect URIs",
        "client_secret": "any",
        "account_id": "the world's zoom_account_id (GET /_emulator/info shows it; see docs)",
        "oauth": "POST https://zoom.us/oauth/token (HTTP Basic client auth), GET https://zoom.us/oauth/authorize",
        "users": "every employee: <first.last>@halcyonadvisory.example (login_hint), owner eleanor.whitfield",
        "webhooks": "POST /_emulator/actions/configure_webhook {url, secret_token}",
    }

    def __init__(self) -> None:
        super().__init__()
        for module in _modules():
            if hasattr(module, "actions"):
                module.actions(self)

    def seed(self, ctx: ProviderContext) -> None:
        for module in _modules():
            if hasattr(module, "seed"):
                module.seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        app.add_middleware(common.TrackingMiddleware)
        for module in _modules():
            if hasattr(module, "register"):
                module.register(app, ctx)

    def error_body(self, status: int, code: str, message: str) -> Any:
        return common.generic_error_body(status, message)

    def fault_response(self, rule: FaultRule) -> tuple[int, dict[str, str], bytes]:
        return common.fault_response(rule)


PROVIDER = ZoomProvider()
