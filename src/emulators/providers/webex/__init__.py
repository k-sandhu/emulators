"""Webex REST API v1 emulator."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI

from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext

from . import auth, hooks, meetings, messaging, people, recordings, seed
from .common import TrackingMiddleware, error_body


class WebexProvider(Provider):
    name = "webex"
    title = "Webex"
    hosts = ("webexapis.com", "api.ciscospark.com", "*.webex.com")
    default_port = 44114
    description = "Webex meetings, recordings, speaker transcripts, messaging and OAuth integrations."
    credentials = {
        "client_id": "webex-emulator",
        "client_secret": "webex-emulator-secret",
        "host_email": "sofia.alvarez@halcyonadvisory.example",
        "host_token": "webex-host-token",
        "admin_email": "arjun.mehta@halcyonadvisory.example",
        "admin_token": "webex-admin-token",
        "bot_email": "halcyon-notes-bot@halcyonadvisory.example",
        "bot_token": "webex-bot-token",
    }

    def __init__(self) -> None:
        super().__init__()
        self.action("mint_token")(auth.mint_token)
        self.action("end_meeting")(hooks.end_meeting)

    def seed(self, ctx: ProviderContext) -> None:
        seed.seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        for module in (auth, people, meetings, recordings, messaging, hooks):
            module.register(app, ctx)
        app.add_middleware(TrackingMiddleware)

    def error_body(self, status: int, code: str, message: str) -> Any:
        return error_body(status, message)

    def fault_response(self, rule: FaultRule) -> tuple[int, dict[str, str], bytes]:
        status, headers, raw = super().fault_response(rule)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            payload = {}
        headers["TrackingId"] = (
            payload.get("trackingId", error_body(status, "Injected fault.")["trackingId"])
            if isinstance(payload, dict)
            else error_body(status, "Injected fault.")["trackingId"]
        )
        return status, headers, raw


PROVIDER = WebexProvider()
