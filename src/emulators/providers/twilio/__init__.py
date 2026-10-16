"""Twilio: Programmable Messaging, Messaging Services, Verify, Lookup, Voice, Conversations."""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from emulators.core.provider import Provider, ProviderContext
from emulators.providers.twilio.common import error_body as _error_body
from emulators.providers.twilio.common import request_id as _request_id
from emulators.providers.twilio.common import route_path as _route_path
from emulators.providers.twilio.common import twilio_error_body as _twilio_error_body


class _TwilioHeaders:
    """Adds the headers every Twilio response carries (request id, duration, concurrency)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "twilio-request-id" not in headers:
                    headers["Twilio-Request-Id"] = _request_id()
                headers["Twilio-Request-Duration"] = f"{time.perf_counter() - started:.3f}"
                headers["Twilio-Concurrent-Requests"] = "1"
                headers.setdefault("X-Home-Region", "us1")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class TwilioProvider(Provider):
    name = "twilio"
    title = "Twilio"
    hosts = (
        "api.twilio.com",
        "messaging.twilio.com",
        "verify.twilio.com",
        "lookups.twilio.com",
        "conversations.twilio.com",
        "media.twiliocdn.com",
        "*.twilio.com",
    )
    default_port = 44108
    description = (
        "Programmable Messaging/SMS, Messaging Services, Verify, Lookup, basic Voice and "
        "Conversations with signed webhooks."
    )
    credentials: dict[str, Any] = {}  # filled from the seeded world in info()

    def __init__(self) -> None:
        super().__init__()
        from emulators.providers.twilio.messages import action_advance_message, action_inbound_sms
        from emulators.providers.twilio.voice import action_advance_call, action_inbound_call

        self.action("inbound_sms")(action_inbound_sms)
        self.action("advance_message")(action_advance_message)
        self.action("inbound_call")(action_inbound_call)
        self.action("advance_call")(action_advance_call)
        self.action("mint_token")(_mint_token)

    def error_body(self, status: int, code: str, message: str) -> Any:
        return _error_body(status, code, message)

    def error_body_for(self, scope: dict[str, Any] | None, status: int, code: str, message: str) -> Any:
        """Twilio's 404 names the requested path ("The requested resource /… was not found")."""
        path = _route_path(scope or {})
        if status == 404 and path and message.startswith("Resource not found"):
            return _twilio_error_body(404, 20404, f"The requested resource {path} was not found")
        if status == 405:
            return _twilio_error_body(405, 20004, "Method not allowed")
        return self.error_body(status, code, message)

    def error_headers_for(self, scope: dict[str, Any] | None, status: int) -> dict[str, str]:
        return {"Twilio-Request-Id": _request_id()}

    def seed(self, ctx: ProviderContext) -> None:
        from emulators.providers.twilio.seed import seed

        seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        from emulators.providers.twilio import (
            conversations,
            empty,
            lookup,
            messages,
            numbers,
            services,
            voice,
        )

        app.add_middleware(_TwilioHeaders)
        numbers.register(app, ctx)
        messages.register(app, ctx)
        voice.register(app, ctx)
        services.register(app, ctx)
        lookup.register(app, ctx)
        conversations.register(app, ctx)
        empty.register(app, ctx)

    def info(self, ctx: ProviderContext) -> dict[str, Any]:
        data = super().info(ctx)
        creds = ctx.store.meta.get("credentials") or {}
        data["credentials"] = {
            **creds,
            "account_friendly_name": ctx.world.org.name,
            "from_number": "+14165550188",
            "whatsapp_sender": "whatsapp:+14165550177",
            "messaging_service_sid": ctx.store.meta.get("messaging_service_sid"),
            "verify_service_sid": ctx.store.meta.get("verify_service_sid"),
        }
        return data


async def _mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Return the account's Basic credentials (Twilio has no OAuth bearer token)."""
    from emulators.providers.twilio.common import creds

    c = creds(ctx)
    test = bool(payload.get("test"))
    return {
        "access_token": c["test_auth_token"] if test else c["auth_token"],
        "token_type": "Basic",
        "account_sid": c["test_account_sid"] if test else c["account_sid"],
        "auth_token": c["test_auth_token"] if test else c["auth_token"],
        "api_key_sid": c["api_key_sid"],
        "api_key_secret": c["api_key_secret"],
    }


PROVIDER = TwilioProvider()
