"""Microsoft identity platform + Microsoft Graph (Entra ID, Outlook, OneDrive/SharePoint,
Teams, online meetings, call records).

The provider is split into modules so several people can work on it at once. Each module
may define ``seed(ctx)`` and ``register(app, ctx)``; both are called in ``MODULES`` order
(identity and directory first, because others reference users and groups).
"""

from __future__ import annotations

import importlib
from typing import Any

from fastapi import FastAPI

from emulators.core.provider import Provider, ProviderContext
from emulators.providers.microsoft import common

MODULES = (
    "identity",  # login.microsoftonline.com: OIDC discovery, authorize, token, JWKS
    "directory",  # organization, users, groups, members, manager/directReports, photos, $batch
    "mail",  # mailFolders (hierarchy), messages, attachments, sendMail, delta
    "calendar",  # calendars, events, calendarView, instances, findMeetingTimes
    "contacts",  # contactFolders, contacts
    "subscriptions",  # change notifications (validationToken handshake, clientState)
    "drive",  # OneDrive: drives, driveItems, content, upload sessions, permissions, delta, search
    "sites",  # SharePoint sites, document libraries, lists, list items
    "teams",  # teams, channels, channel messages, chats, members
    "meetings",  # onlineMeetings, transcripts, recordings, attendance reports, callRecords
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


class MicrosoftProvider(Provider):
    name = "microsoft"
    title = "Microsoft Graph & identity platform"
    hosts = (
        "graph.microsoft.com",
        "login.microsoftonline.com",
        "login.windows.net",
        "*.sharepoint.com",
        "graph.windows.net",
    )
    default_port = 44101
    description = "Entra ID, Outlook mail/calendar/contacts, OneDrive, SharePoint, Teams and online meetings."
    credentials = {
        "client_id": "any (lenient) — e.g. 00000000-0000-0000-0000-00000000c0de",
        "client_secret": "any",
        "tenant": "the world tenant id, 'common', 'organizations' or the verified domain",
        "users": "every employee: <first.last>@halcyonadvisory.example (password not required)",
    }

    def error_body(self, status: int, code: str, message: str) -> Any:
        return common.error_body(code, message)

    def seed(self, ctx: ProviderContext) -> None:
        for module in _modules():
            if hasattr(module, "seed"):
                module.seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        for module in _modules():
            if hasattr(module, "register"):
                module.register(app, ctx)


PROVIDER = MicrosoftProvider()
