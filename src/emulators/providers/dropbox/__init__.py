"""Dropbox API v2 -- files, sharing, users, auth and Dropbox Business team endpoints.

Modules (routes register themselves with :mod:`dispatch` on import):

``seed``           project the Halcyon world into a Business team
``model`` ``ops``  namespaces / entries / access control / mutations
``dispatch``       RPC + content-upload + content-download plumbing, auth, path roots
``oauth``          ``/oauth2/authorize`` and ``/oauth2/token`` on www.dropbox.com / api.dropboxapi.com
``files_*``        the ``files/*`` namespace
``sharing_*``      the ``sharing/*`` namespace (folders, members, links)
``users`` ``team`` ``team_log``  account, team and audit-log routes
``webhooks``       webhook registration, change notifications and long-polling
``actions``        ``/_emulator/actions/*`` (tokens, webhooks, simulated edits)
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI

from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext


class DropboxProvider(Provider):
    name = "dropbox"
    title = "Dropbox API v2 (incl. Dropbox Business)"
    hosts = (
        "api.dropboxapi.com",
        "content.dropboxapi.com",
        "notify.dropboxapi.com",
        "www.dropbox.com",
        "api.dropbox.com",
        "dl.dropboxusercontent.com",
        "*.dl.dropboxusercontent.com",
    )
    default_port = 44104
    description = (
        "Dropbox API v2: files, sharing, users and Business team endpoints for the Halcyon Advisory Group "
        "team, with OAuth 2 (code + PKCE, refresh tokens), content upload/download, shared links and webhooks."
    )
    credentials = {
        "app_key / app_secret": "any (lenient) -- e.g. halcyon_app_key / halcyon_app_secret",
        "user token": "POST /_emulator/tokens {\"user\": \"maya.chen@halcyonadvisory.example\"}",
        "team token": "POST /_emulator/tokens {\"user\": \"arjun.mehta@halcyonadvisory.example\", \"team\": true}",
        "oauth": "GET www.dropbox.com/oauth2/authorize?...&login_hint=<email> auto-approves",
        "team": "dbtid:AAD94f565a6beeb52a733f8bef7d77b04 (Halcyon Advisory Group)",
        "admins": "Arjun Mehta (Team admin), Sarah Johansson (User management admin)",
    }

    def seed(self, ctx: ProviderContext) -> None:
        from emulators.providers.dropbox import seed

        seed.seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        from emulators.providers.dropbox import (  # noqa: F401  (imports register routes)
            actions,
            dispatch,
            files_batch,
            files_content,
            files_core,
            files_search,
            files_transfer,
            links_web,
            oauth,
            sharing_folders,
            sharing_links,
            team,
            team_log,
            users,
            webhooks,
        )

        actions.register(self, ctx)
        webhooks.register(app, ctx)
        oauth.register(app, ctx)
        links_web.register(app, ctx)
        dispatch.register(app, ctx)

    # -- error shapes ----------------------------------------------------------------
    def error_body(self, status: int, code: str, message: str) -> Any:
        if status == 401:
            return {"error_summary": "invalid_access_token/...", "error": {".tag": "invalid_access_token"}}
        if status == 429:
            return {
                "error_summary": "too_many_requests/...",
                "error": {"reason": {".tag": "too_many_requests"}, "retry_after": 1},
            }
        return {"error_summary": f"{code}/...", "error": {".tag": "other"}, "message": message}

    def fault_response(self, rule: FaultRule) -> tuple[int, dict[str, str], bytes]:
        status = rule.status or 500
        headers = {"content-type": "application/json", **rule.headers}
        if rule.body is not None:
            body = rule.body if isinstance(rule.body, (bytes, str)) else json.dumps(rule.body)
            return status, headers, body.encode() if isinstance(body, str) else body
        if status in (401, 429):
            if status == 429 and not any(k.lower() == "retry-after" for k in headers):
                headers["Retry-After"] = "1"
            return status, headers, json.dumps(self.error_body(status, "", "")).encode()
        if status == 409:
            err = {".tag": "too_many_write_operations"}
            return status, headers, json.dumps({"error_summary": "too_many_write_operations/...", "error": err}).encode()
        headers["content-type"] = "text/plain; charset=utf-8"
        text = {400: "Error in call to API function: injected bad request", 503: "Service Unavailable"}.get(
            status, "Internal Server Error"
        )
        return status, headers, text.encode()


PROVIDER = DropboxProvider()
