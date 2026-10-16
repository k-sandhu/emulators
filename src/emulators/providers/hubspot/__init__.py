"""HubSpot CRM v3 and associations v4 emulator."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext

from . import associations, auth, objects, schema, seed, webhooks
from .common import PORTAL_ID, PRIVATE_TOKEN, error_body
from .journal import HubSpotJournal


class HubSpotProvider(Provider):
    name = "hubspot"
    title = "HubSpot CRM"
    hosts = ("api.hubapi.com", "app.hubspot.com", "api.hubspot.com")
    default_port = 44113
    description = "OAuth, CRM objects, search, associations, properties, pipelines, owners, and webhooks."
    credentials = {
        "portal_id": PORTAL_ID,
        "private_app_token": PRIVATE_TOKEN,
        "client_id": "emulator-client",
        "client_secret": "emulator-secret",
    }

    def __init__(self) -> None:
        super().__init__()
        self.actions["mint_token"] = auth.mint_token
        self.actions["configure_webhook"] = webhooks.configure

    def seed(self, ctx: ProviderContext) -> None:
        seed.seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        ctx.journal = HubSpotJournal()
        auth.build(app, ctx)
        schema.build(app, ctx)
        associations.build(app, ctx)
        objects.build(app, ctx)

    def error_body(self, status: int, code: str, message: str) -> dict[str, Any]:
        return error_body(status, message)

    def fault_response(self, rule: FaultRule) -> tuple[int, dict[str, str], bytes]:
        status, headers, content = super().fault_response(rule)
        if status == 429:
            headers = {
                "X-HubSpot-RateLimit-Interval-Milliseconds": "10000",
                "X-HubSpot-RateLimit-Max": "110",
                "X-HubSpot-RateLimit-Remaining": "0",
                "X-HubSpot-RateLimit-Daily": "1000000",
                "X-HubSpot-RateLimit-Daily-Remaining": "999999",
                **headers,
            }
        return status, headers, content


PROVIDER = HubSpotProvider()
