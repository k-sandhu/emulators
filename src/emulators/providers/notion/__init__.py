"""Notion public API emulator for integration and knowledge-ingestion tests."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI

from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext
from emulators.providers.notion import auth, blocks, databases, directory, extras, pages
from emulators.providers.notion.common import INTERNAL_TOKEN, LEGACY_TOKEN, error_body
from emulators.providers.notion.events import NotionWebhooks, configure_webhook
from emulators.providers.notion.journal import NotionJournal
from emulators.providers.notion.middleware import NotionHeaders
from emulators.providers.notion.seed import seed


class NotionProvider(Provider):
    name = "notion"
    title = "Notion"
    hosts = ("api.notion.com", "www.notion.so", "file.notion.so", "*.notion.so")
    default_port = 44115
    description = "Versioned Notion workspace with scoped pages, blocks, databases, OAuth, and files."
    credentials = {
        "internal_token": INTERNAL_TOKEN,
        "legacy_internal_token": LEGACY_TOKEN,
        "client_id": "notion-client",
        "client_secret": "notion-secret",
        "login_hint": "maya.chen@halcyonadvisory.example",
    }

    def __init__(self) -> None:
        super().__init__()
        self.action("mint_token")(auth.mint_token)
        self.action("configure_webhook")(configure_webhook)

    def error_body(self, status: int, code: str, message: str) -> dict[str, Any]:
        codes = {
            400: "validation_error",
            401: "unauthorized",
            403: "restricted_resource",
            404: "object_not_found",
            405: "invalid_request_url",
            409: "conflict_error",
            429: "rate_limited",
            500: "internal_server_error",
            503: "service_unavailable",
            504: "gateway_timeout",
        }
        return error_body(status, codes.get(status, "internal_server_error"), message)

    def seed(self, ctx: ProviderContext) -> None:
        seed(ctx)

    def fault_response(
        self, rule: FaultRule, scope: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        status, headers, content = super().fault_response(rule, scope)
        try:
            request_id = json.loads(content).get("request_id")
        except (ValueError, AttributeError):
            request_id = None
        if request_id:
            headers["x-request-id"] = request_id
        return status, headers, content

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        app.add_middleware(NotionHeaders)
        current = ctx.webhooks
        ctx.webhooks = NotionWebhooks(
            ctx.name,
            ctx.store,
            ctx.clock,
            current.ids,
            mode=current.mode,
            transport_factory=current.transport_factory,
            max_attempts=8,
            timeout_s=current.timeout_s,
        )
        ctx.journal = NotionJournal()
        auth.build(app, ctx)
        directory.build(app, ctx)
        pages.build(app, ctx)
        blocks.build(app, ctx)
        databases.build(app, ctx)
        extras.build(app, ctx)


PROVIDER = NotionProvider()
