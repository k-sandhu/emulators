"""Box Content Cloud API v2.0: OAuth 2.0 / CCG / JWT auth, users, groups, folders, files,
uploads (simple, multipart-form and chunked), collaborations, shared links, search, events
and webhooks v2, metadata, comments, tasks, favourites and trash.

State is seeded from the shared world (see :mod:`emulators.providers.box.seed`); the routes
live in ``routes_*`` modules, each exposing ``register(app, ctx)``.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

from fastapi import FastAPI

from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext
from emulators.providers.box import errors

MODULES = (
    "oauth",  # account.box.com authorize, api.box.com /oauth2/token + revoke
    "routes_users",  # users, groups, memberships, avatars
    "routes_folders",  # folders, items listing, trash
    "routes_files",  # files, content download, versions, thumbnails, locks, copies
    "routes_upload",  # upload.box.com: simple + new version + preflight + chunked sessions
    "routes_collab",  # collaborations, shared links (shared_items), invitations
    "routes_search",  # search, metadata queries
    "routes_events",  # events, long polling
    "routes_metadata",  # metadata templates and instances
    "routes_social",  # comments, tasks, task assignments, web links
    "routes_misc",  # collections (favourites), recent items, zip downloads
    "routes_webhooks",  # webhooks v2
    "actions",  # /_emulator/actions/*
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


class BoxProvider(Provider):
    name = "box"
    title = "Box Content Cloud"
    hosts = (
        "api.box.com",
        "upload.box.com",
        "account.box.com",
        "app.box.com",
        "*.app.box.com",
        "dl.boxcloud.com",
        "*.boxcloud.com",
        "public.boxcloud.com",
        "realtime*.box.com",
    )
    default_port = 44103
    description = (
        "Box Content API v2.0: OAuth2 (authorization code, client credentials, JWT, token exchange), "
        "users, groups, folders, files, chunked uploads, collaborations, shared links, search, "
        "events, webhooks v2, metadata, comments and tasks."
    )
    credentials = {
        "client_id": "halcyon-box-app (any client id works; secrets are not checked)",
        "client_secret": "box-client-secret",
        "enterprise_id": "336533359 (CCG: box_subject_type=enterprise, box_subject_id=<enterprise id>)",
        "developer_tokens": "Bearer box-dev-<person id>, e.g. box-dev-p035 (admin Arjun Mehta), "
        "box-dev-p036 (co-admin), box-dev-p018 (Maya Chen); box-dev-enterprise = service account",
        "webhook_signature_keys": "primary 'box-primary-signature-key', secondary 'box-secondary-signature-key' "
        "(change with POST /_emulator/actions/configure_webhook_keys)",
        "users": "every employee: <first.last>@halcyonadvisory.example (login_hint on the authorize URL)",
    }

    def error_body(self, status: int, code: str, message: str) -> Any:
        default_code, default_message = errors.STATUS_DEFAULTS.get(status, ("error", message))
        return errors.error_body(status, default_code, default_message if status != 400 else message)

    def fault_response(self, rule: FaultRule) -> tuple[int, dict[str, str], bytes]:
        status = rule.status or 500
        headers = {"content-type": "application/json", **rule.headers}
        if status == 429 and not any(k.lower() == "retry-after" for k in headers):
            headers["Retry-After"] = "2"
        if status == 401 and not any(k.lower() == "www-authenticate" for k in headers):
            headers["WWW-Authenticate"] = errors.WWW_AUTHENTICATE
        if rule.body is not None:
            body = rule.body if isinstance(rule.body, (bytes, str)) else json.dumps(rule.body)
        else:
            body = json.dumps(self.error_body(status, "", ""))
        return status, headers, body.encode() if isinstance(body, str) else body

    def seed(self, ctx: ProviderContext) -> None:
        from emulators.providers.box import seed

        seed.seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        for module in _modules():
            if hasattr(module, "register"):
                module.register(app, ctx)


PROVIDER = BoxProvider()
