"""Delegated OAuth authorization with mandatory S256 PKCE and rotating refresh tokens."""

from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import OAuthError, basic_credentials, redirect_with_params, render_account_picker
from emulators.core.provider import ProviderContext, external_base

from .common import SCOPES, invalid, text


def user_for(ctx: ProviderContext, key: str) -> dict[str, Any] | None:
    return next(
        (
            u
            for u in ctx.store.collection("users")
            if key in (u["id"], u["email"], u["_world"]) and u["_active"]
        ),
        None,
    )


def oauth_error(kind: str, message: str, status: int = 400) -> Any:
    from emulators.core.errors import EmulatorError

    raise EmulatorError(status, {"error": kind, "error_description": message})


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/oauth2/v1/authorize")
    async def authorize(request: Request) -> Response:
        q = dict(request.query_params)
        if len(q) != len(request.query_params.multi_items()):
            oauth_error("invalid_request", "Duplicate authorization parameters.")
        for key in (
            "client_id",
            "redirect_uri",
            "response_type",
            "scope",
            "state",
            "code_challenge",
            "code_challenge_method",
        ):
            if not isinstance(q.get(key), str) or not q[key]:
                oauth_error("invalid_request", f"{key} is required.")
        try:
            redirect = urlparse(q["redirect_uri"])
            if redirect.scheme != "https" and not (
                redirect.scheme == "http" and redirect.hostname in {"localhost", "127.0.0.1"}
            ):
                raise ValueError
            if not redirect.hostname or redirect.fragment or redirect.username or ".." in redirect.path:
                raise ValueError
            client = ctx.tokens.authenticate_client(q["client_id"], None, require_secret=False)
            if client["redirect_uris"] and q["redirect_uri"] not in client["redirect_uris"]:
                raise ValueError
        except (ValueError, OAuthError):
            oauth_error("invalid_request", "Invalid client or redirect_uri.")
        scopes = q["scope"].split()
        if not scopes or len(set(scopes)) != len(scopes) or set(scopes) - set(SCOPES):
            oauth_error("invalid_scope", "Invalid scope selection.")
        if (
            not re.fullmatch(r"[A-Za-z0-9._-]{16,1024}", q["state"])
            or q["code_challenge_method"] != "S256"
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", q["code_challenge"])
        ):
            oauth_error("invalid_request", "state and S256 PKCE must meet Airtable's requirements.")
        if not q.get("login_hint"):
            return HTMLResponse(
                render_account_picker(
                    provider_title="Airtable",
                    app_name=client["name"],
                    scopes=scopes,
                    accounts=[
                        {"display_name": u["name"], "email": u["email"], "login_hint": u["email"]}
                        for u in ctx.store.collection("users")
                        if u["_active"]
                    ],
                    action_base=external_base(request) + "/oauth2/v1/authorize?" + str(request.query_params),
                )
            )
        user = user_for(ctx, q["login_hint"])
        if user is None:
            oauth_error("invalid_request", "Unknown or inactive user.")
        code = ctx.tokens.issue_code(
            client_id=client["id"],
            user_id=user["id"],
            scopes=scopes,
            redirect_uri=q["redirect_uri"],
            code_challenge=q["code_challenge"],
            code_challenge_method="S256",
        )
        return RedirectResponse(
            redirect_with_params(
                q["redirect_uri"],
                {
                    "code": code.token,
                    "state": q["state"],
                    "code_challenge": q["code_challenge"],
                    "code_challenge_method": "S256",
                },
            ),
            status_code=302,
        )

    async def form(request: Request) -> dict[str, str]:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            oauth_error("invalid_request", "Expected form-encoded token parameters.")
        try:
            data = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True, errors="strict")
        except (ValueError, UnicodeError):
            oauth_error("invalid_request", "Invalid form data.")
        if any(len(v) != 1 for v in data.values()):
            oauth_error("invalid_request", "Duplicate token parameters.")
        return {k: v[0] for k, v in data.items()}

    def client_for(request: Request, data: dict[str, str]) -> dict[str, Any]:
        basic = basic_credentials(request)
        cid = basic[0] if basic else data.get("client_id")
        if basic and data.get("client_id", cid) != cid:
            oauth_error("invalid_client", "Mismatched client identities.", 401)
        try:
            client = ctx.tokens.authenticate_client(cid, basic[1] if basic else None)
        except OAuthError as exc:
            oauth_error(exc.error, exc.description, exc.status)
        if client.get("client_secret") and not basic:
            oauth_error("invalid_client", "HTTP Basic client authentication is required.", 401)
        if not client.get("client_secret") and basic:
            oauth_error("invalid_client", "Public clients must not send HTTP Basic credentials.", 401)
        return client

    @app.post("/oauth2/v1/token")
    async def token(request: Request) -> JSONResponse:
        data = await form(request)
        client = client_for(request, data)
        try:
            if data.get("grant_type") == "authorization_code":
                previous = ctx.tokens.resolve(data.get("code"), "code")
                if (
                    previous is None
                    or previous.redirect_uri != data.get("redirect_uri")
                    or not re.fullmatch(r"[A-Za-z0-9._-]{43,128}", data.get("code_verifier", ""))
                ):
                    raise OAuthError("invalid_grant", "Invalid authorization grant.")
                grant = ctx.tokens.redeem_code(
                    data.get("code"),
                    client_id=client["id"],
                    redirect_uri=data.get("redirect_uri"),
                    code_verifier=data.get("code_verifier"),
                )
            elif data.get("grant_type") == "refresh_token":
                grant = ctx.tokens.resolve(data.get("refresh_token"), "refresh")
                if grant is None or grant.client_id != client["id"]:
                    raise OAuthError("invalid_grant", "Invalid refresh token.")
            else:
                raise OAuthError("unsupported_grant_type", "Unsupported grant_type.")
            user = user_for(ctx, grant.user_id)
            if user is None:
                raise OAuthError("invalid_grant", "The authorizing user is inactive.")
            scopes = data.get("scope", " ".join(grant.scopes)).split()
            if not scopes or len(set(scopes)) != len(scopes) or set(scopes) - set(grant.scopes):
                raise OAuthError("invalid_scope", "Refresh scopes may only reduce existing scopes.")
        except OAuthError as exc:
            oauth_error(exc.error, exc.description, exc.status)
        family = grant.extra.get("family", secrets.token_urlsafe(24))
        for existing in ctx.store.collection("_grants"):
            if existing.get("extra", {}).get("family") == family:
                ctx.tokens.revoke(existing["id"])
        ctx.tokens.revoke(grant.token)
        extra = {**grant.extra, "family": family}
        access = ctx.tokens.issue_access(
            client_id=client["id"], user_id=grant.user_id, scopes=scopes, ttl=timedelta(hours=1), extra=extra
        )
        refresh = ctx.tokens.issue_refresh(
            client_id=client["id"], user_id=grant.user_id, scopes=scopes, ttl=timedelta(days=60), extra=extra
        )
        return JSONResponse(
            {
                "access_token": access.token,
                "refresh_token": refresh.token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": " ".join(scopes),
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @app.post("/oauth2/v1/revoke")
    async def revoke(request: Request) -> dict[str, Any]:
        data = await form(request)
        client = client_for(request, data)
        grant = ctx.tokens.get(data.get("token"))
        if grant and grant.client_id == client["id"]:
            family = grant.extra.get("family")
            for existing in ctx.store.collection("_grants"):
                if existing["id"] == grant.token or (
                    family and existing.get("extra", {}).get("family") == family
                ):
                    ctx.tokens.revoke(existing["id"])
        return {}


def revoke_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    token = text(payload.get("token"), "token", empty=False)
    if not ctx.tokens.revoke(token):
        invalid("Unknown token.")
    return {"revoked": True}
