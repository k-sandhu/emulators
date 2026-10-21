"""Airtable Web API with relational bases, typed records, attachments and webhooks."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from emulators.core.provider import Provider, ProviderContext

from . import auth, hooks, metadata, records, seed
from .common import OWNER_TOKEN, READER_TOKEN, mint_token


class AirtableProvider(Provider):
    name = "airtable"
    title = "Airtable"
    hosts = ("api.airtable.com", "content.airtable.com", "v5.airtableusercontent.com", "airtable.com")
    default_port = 44119
    description = "Relational Halcyon bases, scoped PAT/OAuth, typed record CRUD/upsert, attachments and signed webhooks."
    credentials = {
        "owner_token": OWNER_TOKEN,
        "reader_token": READER_TOKEN,
        "client_id": "airtable-emulator",
        "client_secret": "airtable-emulator-secret",
        "redirect_uri": "https://consumer.example/callback",
    }

    def __init__(self) -> None:
        super().__init__()
        self.action("mint_token")(mint_token)
        self.action("revoke_token")(auth.revoke_token)
        self.action("replay_webhook_notification")(hooks.simulate_change)

    def seed(self, ctx: ProviderContext) -> None:
        seed.seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        for module in (auth, metadata, hooks, records):
            module.register(app, ctx)

    def error_body(self, status: int, code: str, message: str) -> Any:
        kind = {
            400: "INVALID_REQUEST_UNKNOWN",
            401: "AUTHENTICATION_REQUIRED",
            403: "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            422: "INVALID_REQUEST_UNKNOWN",
            429: "TOO_MANY_REQUESTS",
        }.get(status, "SERVER_ERROR")
        return {"error": {"type": kind, "message": message}}


PROVIDER = AirtableProvider()
