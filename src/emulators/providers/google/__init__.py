"""Google identity (OAuth 2.0 / OIDC) and Google Workspace APIs: Admin SDK Directory,
Drive v3, Gmail v1, Calendar v3 and Meet REST v2.

Like the Microsoft provider it is split into modules that each may define ``seed(ctx)``
and ``register(app, ctx)``, called in ``MODULES`` order.
"""

from __future__ import annotations

import importlib
from typing import Any

from fastapi import FastAPI

from emulators.core.provider import Provider, ProviderContext
from emulators.providers.google import common

MODULES = (
    "oauth",  # accounts.google.com, oauth2.googleapis.com: authorize, token, tokeninfo, userinfo, certs
    "directory",  # admin.googleapis.com/admin/directory/v1: users, groups, members, orgunits, customers
    "drive",  # www.googleapis.com/drive/v3 (+ /upload): files, permissions, drives, changes, about
    "gmail",  # gmail.googleapis.com/gmail/v1: messages, threads, labels, history, send, attachments
    "calendar",  # www.googleapis.com/calendar/v3: calendarList, calendars, events, freeBusy, watch
    "meet",  # meet.googleapis.com/v2: spaces, conferenceRecords, participants, recordings, transcripts
)


def _modules() -> list[Any]:
    loaded = []
    for name in MODULES:
        try:
            loaded.append(importlib.import_module(f"{__name__}.{name}"))
        except ModuleNotFoundError as exc:
            if exc.name != f"{__name__}.{name}":
                raise
    return loaded


class GoogleProvider(Provider):
    name = "google"
    title = "Google identity & Workspace APIs"
    hosts = (
        "accounts.google.com",
        "oauth2.googleapis.com",
        "openidconnect.googleapis.com",
        "www.googleapis.com",
        "admin.googleapis.com",
        "gmail.googleapis.com",
        "drive.googleapis.com",
        "calendar-json.googleapis.com",
        "meet.googleapis.com",
        "content.googleapis.com",
        "docs.googleapis.com",
        "sheets.googleapis.com",
        "pubsub.googleapis.com",
        "drive.google.com",
        "docs.google.com",
        "meet.google.com",
    )
    default_port = 44102
    description = "OAuth/OIDC, Admin Directory, Drive v3, Gmail v1, Calendar v3 and Meet REST v2."
    credentials = {
        "client_id": "any (lenient), e.g. 1234567890-emulator.apps.googleusercontent.com",
        "client_secret": "any",
        "users": "every employee: <first.last>@halcyonadvisory.example",
    }

    def error_body(self, status: int, code: str, message: str) -> Any:
        return common.error_body(status, message)

    def seed(self, ctx: ProviderContext) -> None:
        for module in _modules():
            if hasattr(module, "seed"):
                module.seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        for module in _modules():
            if hasattr(module, "register"):
                module.register(app, ctx)


PROVIDER = GoogleProvider()
