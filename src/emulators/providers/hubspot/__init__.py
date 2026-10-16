"""HubSpot CRM v3 and associations v4 emulator."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext

from . import associations, auth, objects, schema, seed, webhooks
from .common import PORTAL_ID, PRIVATE_TOKEN, error_body
from .journal import HubSpotJournal

API_PREFIXES = ("/crm/", "/account-info/", "/oauth/v1/")


def rate_headers(ctx: ProviderContext, *, limited: bool = False) -> dict[str, str]:
    count = ctx.store.meta.get("api_requests", 0)
    return {
        "X-HubSpot-RateLimit-Interval-Milliseconds": "10000",
        "X-HubSpot-RateLimit-Max": "110",
        "X-HubSpot-RateLimit-Remaining": "0" if limited else "109",
        "X-HubSpot-RateLimit-Daily": "1000000",
        "X-HubSpot-RateLimit-Daily-Remaining": str(max(0, 1000000 - count)),
    }


class HubSpotHeadersMiddleware:
    """Adds HubSpot correlation and rate-limit headers to vendor API responses."""

    def __init__(self, app: ASGIApp, ctx: ProviderContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(API_PREFIXES):
            await self.app(scope, receive, send)
            return
        self.ctx.store.meta["api_requests"] = self.ctx.store.meta.get("api_requests", 0) + 1
        correlation = str(uuid4())
        start: Message | None = None

        async def wrapped(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                if message["status"] >= 400:
                    start = message
                    return
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in (b"x-hubspot-correlation-id",)
                ]
                headers.append((b"x-hubspot-correlation-id", correlation.encode()))
                for name, value in rate_headers(self.ctx).items():
                    headers.append((name.lower().encode(), value.encode()))
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body" and start is not None:
                body_correlation = correlation
                match = re.search(rb'"correlationId"\s*:\s*"([^"]+)"', message.get("body", b""))
                if match:
                    body_correlation = match.group(1).decode()
                headers = list(start.get("headers", []))
                headers.append((b"x-hubspot-correlation-id", body_correlation.encode()))
                for name, value in rate_headers(self.ctx).items():
                    headers.append((name.lower().encode(), value.encode()))
                await send({**start, "headers": headers})
                start = None
            await send(message)

        await self.app(scope, receive, wrapped)
        if start is not None:
            headers = list(start.get("headers", []))
            headers.append((b"x-hubspot-correlation-id", correlation.encode()))
            await send({**start, "headers": headers})


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
        app.add_middleware(HubSpotHeadersMiddleware, ctx=ctx)
        auth.build(app, ctx)
        schema.build(app, ctx)
        associations.build(app, ctx)
        objects.build(app, ctx)

    def error_body(self, status: int, code: str, message: str) -> dict[str, Any]:
        return error_body(status, message)

    def fault_response(
        self, rule: FaultRule, scope: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        status, headers, content = super().fault_response(rule, scope)
        correlation = str(uuid4())
        try:
            correlation = json.loads(content).get("correlationId", correlation)
        except (ValueError, AttributeError):
            pass
        headers = {
            "X-HubSpot-Correlation-Id": correlation,
            "X-HubSpot-RateLimit-Interval-Milliseconds": "10000",
            "X-HubSpot-RateLimit-Max": "110",
            "X-HubSpot-RateLimit-Remaining": "0" if status == 429 else "109",
            "X-HubSpot-RateLimit-Daily": "1000000",
            "X-HubSpot-RateLimit-Daily-Remaining": "999999",
            **headers,
        }
        return status, headers, content


PROVIDER = HubSpotProvider()
