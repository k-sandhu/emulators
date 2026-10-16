"""Dropbox API v2 -- files, sharing, users, auth and Dropbox Business team endpoints.

Modules (routes register themselves with :mod:`dispatch` on import):

``seed``                project the Halcyon world into a Business team
``model`` ``ops``       namespaces / entries / access control / path resolution / mutations
``common`` ``catalog``  wire helpers (errors, args, ids, content hash) and static catalogs
``dispatch``            RPC + content-upload + content-download plumbing, auth, path roots, CORS
``oauth``               ``/oauth2/authorize`` and ``/oauth2/token`` (www.dropbox.com / api.dropboxapi.com)
``files_core``          list_folder (+cursors), get_metadata, create_folder, delete, move, copy
``files_content``       upload (+sessions), download, zip, export, temporary links, thumbnails, previews
``files_batch``         batch variants and async-job polling
``files_misc``          revisions, restore, tags, locks, copy references, save_url
``files_search``        search_v2
``file_properties``     property templates and groups
``file_requests``       file requests (uploads are simulated)
``fileutil``            shared path / write-target / quota helpers
``sharing_folders``     shared folders and their members; ``sharing_files`` file members
``sharing_links``       shared links API; ``links_web`` the public pages and temporary links
``users`` ``team`` ``team_security`` ``team_log``  account, team, devices/apps and audit-log routes
``webhooks``            webhook registration, change notifications and long-polling
``legacy``              deprecated routes still called by older SDKs
``imaging``             thumbnail encoders (PNG, baseline JPEG)
``actions``             ``/_emulator/actions/*`` (tokens, webhooks, quota, simulated uploads)
"""

from __future__ import annotations

import json
import secrets
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
        "user token": 'POST /_emulator/tokens {"user": "maya.chen@halcyonadvisory.example"}',
        "team token": 'POST /_emulator/tokens {"user": "arjun.mehta@halcyonadvisory.example", "team": true}',
        "oauth": "GET www.dropbox.com/oauth2/authorize?...&login_hint=<email> auto-approves",
        "team": "dbtid:AAD94f565a6beeb52a733f8bef7d77b04 (Halcyon Advisory Group)",
        "admins": "Arjun Mehta (Team admin), Sarah Johansson (User management admin)",
    }

    def seed(self, ctx: ProviderContext) -> None:
        from emulators.providers.dropbox import seed, team_log

        seed.seed(ctx)
        team_log.seed_events(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        from emulators.providers.dropbox import (  # noqa: F401  (importing registers the routes)
            actions,
            dispatch,
            file_properties,
            file_requests,
            files_batch,
            files_content,
            files_core,
            files_misc,
            files_search,
            legacy,
            links_web,
            oauth,
            sharing_files,
            sharing_folders,
            sharing_links,
            team,
            team_log,
            team_security,
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

    def error_headers_for(self, scope: dict[str, Any] | None, status: int) -> dict[str, str]:
        return {"X-Dropbox-Request-Id": secrets.token_hex(16)}

    def fault_response(
        self, rule: FaultRule, scope: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        status = rule.status or 500
        headers = {
            "content-type": "application/json",
            **self.error_headers_for(scope, status),
            **rule.headers,
        }
        if rule.body is not None:
            body = rule.body if isinstance(rule.body, (bytes, str)) else json.dumps(rule.body)
            return status, headers, body.encode() if isinstance(body, str) else body
        if status in (401, 429):
            if status == 429 and not any(k.lower() == "retry-after" for k in headers):
                headers["Retry-After"] = "1"
            return status, headers, json.dumps(self.error_body(status, "", "")).encode()
        if status == 409:
            err = {".tag": "too_many_write_operations"}
            return (
                status,
                headers,
                json.dumps({"error_summary": "too_many_write_operations/...", "error": err}).encode(),
            )
        headers["content-type"] = "text/plain; charset=utf-8"
        text = {400: "Error in call to API function: injected bad request", 503: "Service Unavailable"}.get(
            status, "Internal Server Error"
        )
        return status, headers, text.encode()


PROVIDER = DropboxProvider()
