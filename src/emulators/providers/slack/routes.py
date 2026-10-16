"""HTTP surface: Web API dispatcher, OAuth/OIDC pages, incoming webhooks, file and upload hosts."""

from __future__ import annotations

import html
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from emulators.core.provider import ProviderContext
from emulators.providers.slack import (  # noqa: F401  (imported for @api registration)
    access,
    interactivity,
    m_chat,
    m_conversations,
    m_files,
    m_oauth,
    m_reactions,
    m_reminders,
    m_search,
    m_users,
)
from emulators.providers.slack.incoming import handle_incoming
from emulators.providers.slack.runtime import dispatch

STATUS_ERRORS = {
    400: "invalid_arguments",
    401: "invalid_auth",
    403: "access_denied",
    404: "unknown_method",
    405: "invalid_request",
    429: "ratelimited",
    500: "internal_error",
    502: "fatal_error",
    503: "service_unavailable",
    504: "fatal_error",
}


def error_body(status: int, code: str, message: str) -> dict[str, Any]:
    """Injected faults and framework errors in Slack's ``{"ok": false, "error": ...}`` shape."""
    return {"ok": False, "error": STATUS_ERRORS.get(status, "fatal_error")}


def _signin_page(note: str = "") -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><title>Sign in | Halcyon Advisory Group Slack"
        "</title></head><body><h1>Sign in to Halcyon Advisory Group</h1>"
        f"<p>This is the Slack emulator; there are no real accounts. {html.escape(note)}</p></body></html>"
    )


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.api_route("/api/{method}", methods=["GET", "POST"])
    async def web_api(method: str, request: Request) -> Response:
        return await dispatch(ctx, request, method)

    @app.get("/api")
    @app.get("/api/")
    async def api_index() -> Response:
        return JSONResponse({"ok": False, "error": "unknown_method"})

    # -- OAuth / OpenID Connect -------------------------------------------------------------
    @app.get("/oauth/v2/authorize")
    @app.get("/oauth/authorize")
    @app.get("/oauth")
    async def oauth_authorize(request: Request) -> Response:
        return m_oauth.authorize(ctx, request, openid=False)

    @app.get("/openid/connect/authorize")
    async def openid_authorize(request: Request) -> Response:
        return m_oauth.authorize(ctx, request, openid=True)

    @app.get("/.well-known/openid-configuration")
    async def openid_configuration(request: Request) -> Response:
        return JSONResponse(m_oauth.discovery_document(ctx, request))

    @app.get("/openid/connect/keys")
    async def openid_keys() -> Response:
        return JSONResponse(ctx.signing_key.jwks())

    # -- incoming webhooks (hooks.slack.com) ------------------------------------------------
    @app.post("/services/{team}/{bot}/{secret}")
    async def incoming_webhook(team: str, bot: str, secret: str, request: Request) -> Response:
        return await handle_incoming(ctx, request, f"{team}/{bot}/{secret}")

    @app.post("/commands/{team}/{channel}/{secret}")
    @app.post("/actions/{team}/{channel}/{secret}")
    async def response_url(team: str, channel: str, secret: str, request: Request) -> Response:
        return await interactivity.handle_response_url(ctx, request, f"{team}/{channel}/{secret}")

    # -- files.slack.com --------------------------------------------------------------------
    @app.get("/files-pri/{team_file}/download/{name:path}")
    async def download_file(team_file: str, name: str, request: Request) -> Response:
        return m_files.serve_file(ctx, request, team_file, True)

    @app.get("/files-pri/{team_file}/{name:path}")
    async def view_file(team_file: str, name: str, request: Request) -> Response:
        return m_files.serve_file(ctx, request, team_file, False)

    @app.get("/files-tmb/{team_file}/{name:path}")
    async def thumbnail(team_file: str, name: str, request: Request) -> Response:
        return m_files.serve_file(ctx, request, team_file, False)

    @app.api_route("/upload/v1/{token}", methods=["POST", "PUT"])
    async def upload(token: str, request: Request) -> Response:
        return await m_files.receive_upload(ctx, token, request)

    # -- workspace web pages ----------------------------------------------------------------
    @app.get("/")
    async def workspace_home() -> Response:
        return _signin_page()

    @app.get("/archives/{channel_id}/{message}")
    async def permalink(channel_id: str, message: str) -> Response:
        channel = access.channel_by_id(ctx, channel_id)
        if channel is None or channel.get("is_im") or channel.get("is_mpim") or channel.get("is_private"):
            return _signin_page("Sign in to view this conversation.")
        return _signin_page(f"Message in #{channel['name']}.")

    @app.get("/files/{user}/{file_id}/{name}")
    async def file_page(user: str, file_id: str, name: str) -> Response:
        return _signin_page("Sign in to view this file.")

    @app.get("/robots.txt")
    async def robots() -> Response:
        return PlainTextResponse("User-agent: *\nDisallow: /\n")
