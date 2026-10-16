"""Integration consent, PKCE and rotating refresh grants."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import OAuthError, redirect_with_params, render_account_picker
from emulators.core.provider import ProviderContext, external_base

from .common import PERSONAL_SCOPES, SERVICE_SCOPES, fail, person, required


def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve world ids and emails to the same Webex subject as OAuth consent."""
    app_only = bool(payload.get("app_only", payload.get("user") is None))
    user = person(ctx, payload.get("user"))
    if not app_only and not user:
        fail(400, "Unknown Webex user.")
    scopes = payload.get("scopes", SERVICE_SCOPES if app_only else PERSONAL_SCOPES)
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        fail(400, "scopes must be an array of strings.")
    try:
        ttl = int(payload.get("expires_in", 1209600))
    except (TypeError, ValueError):
        fail(400, "expires_in must be an integer.")
    grant = ctx.tokens.issue_access(
        client_id=payload.get("client_id", "emulator-client"),
        user_id=user["id"] if user else None,
        app_only=app_only,
        scopes=scopes,
        ttl=timedelta(seconds=ttl),
    )
    if ttl == 0:
        ctx.store.collection("_grants").update(grant.token, expires_at=ctx.clock.now())
    return {"access_token": grant.token, "token_type": "Bearer", "expires_in": ttl}


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/v1/authorize")
    async def authorize(request: Request) -> Response:
        query = dict(request.query_params)
        required(query, "client_id", "redirect_uri", "response_type")
        if query["response_type"] != "code":
            fail(400, "response_type must be code.")
        if query.get("code_challenge_method", "plain") not in ("S256", "plain"):
            fail(400, "Unsupported PKCE challenge method.")
        try:
            client = ctx.tokens.authenticate_client(query["client_id"], None, require_secret=False)
        except OAuthError as exc:
            fail(exc.status, exc.description)
        if client["redirect_uris"] and query["redirect_uri"] not in client["redirect_uris"]:
            fail(400, "redirect_uri is not registered for this client.")
        scopes = query.get("scope", "spark:all").split()
        if not query.get("login_hint"):
            return HTMLResponse(
                render_account_picker(
                    provider_title="Webex",
                    app_name=client["name"],
                    scopes=scopes,
                    accounts=[
                        {"display_name": p.display_name, "email": p.email, "login_hint": p.email}
                        for p in ctx.world.active_employees
                    ],
                    action_base=external_base(request) + "/v1/authorize?" + str(request.query_params),
                )
            )
        user = person(ctx, query["login_hint"])
        if not user or user.get("_external") or user["type"] == "bot" or user["status"] == "inactive":
            fail(400, "The requested account cannot authorize this integration.")
        if any("admin" in scope or "compliance" in scope for scope in scopes) and not user.get("_admin"):
            fail(403, "Administrator consent is required for the requested scopes.")
        code = ctx.tokens.issue_code(
            client_id=client["id"],
            user_id=user["id"],
            scopes=scopes,
            redirect_uri=query["redirect_uri"],
            code_challenge=query.get("code_challenge"),
            code_challenge_method=query.get("code_challenge_method"),
        )
        return RedirectResponse(
            redirect_with_params(query["redirect_uri"], {"code": code.token, "state": query.get("state")}),
            status_code=302,
        )

    @app.post("/v1/access_token")
    async def token(request: Request) -> JSONResponse:
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            fail(400, "The token endpoint requires application/x-www-form-urlencoded content.")
        form = {
            k: v[-1] for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items()
        }
        try:
            client = ctx.tokens.authenticate_client(form.get("client_id"), form.get("client_secret"))
            if form.get("grant_type") == "authorization_code":
                code = ctx.tokens.resolve(form.get("code"), "code")
                if code and code.redirect_uri != form.get("redirect_uri"):
                    raise OAuthError(
                        "invalid_grant", "redirect_uri does not match the authorization request."
                    )
                grant = ctx.tokens.redeem_code(
                    form.get("code"),
                    client_id=client["id"],
                    redirect_uri=form.get("redirect_uri"),
                    code_verifier=form.get("code_verifier"),
                )
            elif form.get("grant_type") == "refresh_token":
                grant = ctx.tokens.resolve(form.get("refresh_token"), "refresh")
                if grant is None or grant.client_id != client["id"]:
                    raise OAuthError("invalid_grant", "The refresh token is invalid or has expired.")
                ctx.tokens.revoke(grant.token)
            else:
                raise OAuthError("unsupported_grant_type", "Unsupported grant_type.")
        except OAuthError as exc:
            fail(exc.status, exc.description)
        access = ctx.tokens.issue_access(
            client_id=grant.client_id,
            user_id=grant.user_id,
            scopes=grant.scopes,
            ttl=timedelta(days=14),
            app_only=grant.app_only,
        )
        refresh = ctx.tokens.issue_refresh(
            client_id=grant.client_id,
            user_id=grant.user_id,
            scopes=grant.scopes,
            ttl=timedelta(days=90),
            app_only=grant.app_only,
        )
        return JSONResponse(
            {
                "access_token": access.token,
                "expires_in": 1209600,
                "refresh_token": refresh.token,
                "refresh_token_expires_in": 7776000,
                "token_type": "Bearer",
                "scope": " ".join(grant.scopes),
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
