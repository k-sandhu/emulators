"""HubSpot's legacy OAuth v1 flow, retained for CRM connector compatibility."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import Grant, OAuthError, redirect_with_params, render_account_picker
from emulators.core.provider import ProviderContext, external_base

from .common import APP_ID, PORTAL_ID, all_scopes, fail, owner_for


def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Mint a scoped OAuth access token, or an app token with app_only=true."""
    user = payload.get("user")
    owner = owner_for(ctx, user) if user else None
    if user and (owner is None or owner["archived"]):
        fail(400, "The requested user does not have an active HubSpot seat.")
    ttl = int(payload.get("expires_in", 1800))
    grant = ctx.tokens.issue_access(
        client_id=payload.get("client_id", "emulator-client"), user_id=owner["_world_id"] if owner else None,
        app_only=bool(payload.get("app_only", not user)), scopes=payload.get("scopes", all_scopes()),
        ttl=timedelta(seconds=ttl), token="CJ" + secrets.token_urlsafe(150),
    )
    return {"access_token": grant.token, "token_type": "bearer", "expires_in": ttl}


def token_metadata(ctx: ProviderContext, grant: Grant) -> dict[str, Any]:
    owner = owner_for(ctx, grant.user_id)
    result = {"token": grant.token, "user": owner["email"] if owner else "",
              "hub_domain": ctx.world.org.domain, "scopes": grant.scopes,
              "hub_id": PORTAL_ID, "app_id": APP_ID, "user_id": owner["userId"] if owner else 0,
              "token_type": grant.kind}
    if grant.kind == "access":
        result["expires_in"] = max(0, int((grant.expires_at - ctx.clock.now()).total_seconds())) if grant.expires_at else 0
    return result


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/oauth/authorize")
    async def authorize(request: Request) -> Response:
        params = request.query_params
        client_id, redirect = params.get("client_id"), params.get("redirect_uri")
        if not client_id or not redirect or urlparse(redirect).scheme not in ("http", "https"):
            fail(400, "client_id and a valid redirect_uri are required.")
        try:
            client = ctx.tokens.authenticate_client(client_id, None, require_secret=False)
        except OAuthError as exc:
            fail(400, exc.description)
        if client["redirect_uris"] and redirect not in client["redirect_uris"]:
            fail(400, "redirect_uri does not match the application's registered redirect URIs.")
        scopes = list(dict.fromkeys((params.get("scope", "oauth") + " " + params.get("optional_scope", "")).split()))
        if any(s not in all_scopes() for s in scopes):
            fail(400, "The requested scope is not supported by this account.")
        hint = params.get("login_hint")
        if not hint:
            accounts = [{"display_name": r["firstName"] + " " + r["lastName"], "email": r["email"],
                         "login_hint": r["email"]} for r in ctx.store["owners"] if not r["archived"]]
            return HTMLResponse(render_account_picker(provider_title="HubSpot", app_name=client["name"],
                scopes=scopes, accounts=accounts, action_base=external_base(request) + "/oauth/authorize?" + str(params)))
        owner = owner_for(ctx, hint)
        if owner is None or owner["archived"]:
            fail(400, "Account not found or does not have a HubSpot seat.")
        code = ctx.tokens.issue_code(client_id=client_id, user_id=owner["_world_id"], scopes=scopes,
                                     redirect_uri=redirect)
        return RedirectResponse(redirect_with_params(redirect, {"code": code.token, "state": params.get("state")}), status_code=302)

    @app.post("/oauth/v1/token")
    async def exchange(request: Request) -> Response:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            fail(400, "Content-Type must be application/x-www-form-urlencoded.")
        try:
            form = {k: v[-1] for k, v in parse_qs((await request.body()).decode()).items()}
        except UnicodeDecodeError:
            fail(400, "Invalid form encoding.")
        flow = form.get("grant_type")
        try:
            if not form.get("client_secret"):
                raise OAuthError("invalid_client", "client_secret is required.", 401)
            ctx.tokens.authenticate_client(form.get("client_id"), form.get("client_secret"))
            if flow == "authorization_code":
                pending = ctx.tokens.resolve(form.get("code"), "code")
                if pending is None or form.get("redirect_uri") != pending.redirect_uri:
                    raise OAuthError("invalid_grant", "Authorization code or redirect_uri is invalid.")
                grant = ctx.tokens.redeem_code(form.get("code"), client_id=form.get("client_id"),
                    redirect_uri=form.get("redirect_uri"), code_verifier=None)
                refresh = ctx.tokens.issue_refresh(client_id=grant.client_id, user_id=grant.user_id,
                    scopes=grant.scopes, ttl=None, token=str(uuid4()))
            elif flow == "refresh_token":
                grant = ctx.tokens.resolve(form.get("refresh_token"), "refresh")
                if grant is None or grant.client_id != form.get("client_id"):
                    raise OAuthError("invalid_grant", "refresh token is invalid, expired or revoked")
                refresh = grant
            else:
                raise OAuthError("unsupported_grant_type", "Unsupported grant_type.")
            access = ctx.tokens.issue_access(client_id=grant.client_id, user_id=grant.user_id,
                scopes=grant.scopes, ttl=timedelta(seconds=1800), token="CJ" + secrets.token_urlsafe(150))
            return JSONResponse({"token_type": "bearer", "refresh_token": refresh.token,
                "access_token": access.token, "expires_in": 1800})
        except OAuthError as exc:
            status = "BAD_REFRESH_TOKEN" if flow == "refresh_token" and exc.error == "invalid_grant" else exc.error.upper()
            return JSONResponse({"error": exc.error, "error_description": exc.description,
                "status": status, "message": exc.description}, status_code=exc.status)

    @app.get("/oauth/v1/access-tokens/{token}")
    async def access_info(token: str) -> dict[str, Any]:
        grant = ctx.tokens.resolve(token)
        if grant is None:
            fail(401, "Access token is invalid, expired or revoked.")
        return token_metadata(ctx, grant)

    @app.get("/oauth/v1/refresh-tokens/{token}")
    async def refresh_info(token: str) -> dict[str, Any]:
        grant = ctx.tokens.resolve(token, "refresh")
        if grant is None:
            fail(404, "Refresh token not found.")
        return token_metadata(ctx, grant)

    @app.delete("/oauth/v1/refresh-tokens/{token}", status_code=204)
    async def revoke(token: str) -> Response:
        if ctx.tokens.get(token, "refresh") is None:
            fail(404, "Refresh token not found.")
        ctx.tokens.revoke(token)
        return Response(status_code=204)
