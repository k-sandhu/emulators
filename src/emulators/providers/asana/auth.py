"""PATs and the user-delegated authorization-code/refresh-token protocol."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import OAuthError, redirect_with_params, render_account_picker
from emulators.core.provider import ProviderContext
from emulators.providers.asana import common as c

SCOPES = {
    "default",
    *[
        f"{resource}:{operation}"
        for resource in (
            "tasks",
            "projects",
            "project_sections",
            "users",
            "workspaces",
            "teams",
            "stories",
            "attachments",
            "tags",
            "webhooks",
        )
        for operation in ("read", "write", "delete")
    ],
}


def scope_list(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(s, str) or s not in SCOPES for s in value):
        c.invalid("Invalid OAuth scope")
    return value


def mint(ctx: ProviderContext, data: dict[str, Any]) -> dict[str, Any]:
    uid = c.user_id(ctx, data.get("user", "p018"))
    if uid is None:
        c.invalid("user: Not a valid user")
    if data.get("app_only") is not None and data["app_only"] is not False:
        c.invalid("Asana tokens require a user")
    seconds = data.get("expires_in", 3600)
    if not isinstance(seconds, int) or isinstance(seconds, bool) or abs(seconds) > 315360000:
        c.invalid("expires_in: must be an integer within ten years")
    client_id = c.string(data.get("client_id", "asana-client"), "client_id", empty=False)
    grant = ctx.tokens.issue_access(
        client_id=client_id,
        user_id=uid,
        scopes=scope_list(data.get("scopes", ["default"])),
        ttl=timedelta(seconds=seconds),
        prefix="1/",
    )
    return {"access_token": grant.token, "token_type": "bearer", "expires_in": seconds}


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/-/oauth_authorize")
    async def authorize(request: Request) -> Any:
        query = request.query_params
        client_id, redirect_uri = query.get("client_id"), query.get("redirect_uri")
        if not client_id or not redirect_uri or query.get("response_type") != "code":
            c.invalid("client_id, redirect_uri and response_type=code are required")
        try:
            target = urlsplit(redirect_uri)
            if target.scheme not in ("http", "https") or not target.hostname:
                raise ValueError
        except ValueError:
            c.invalid("Invalid redirect_uri")
        client = ctx.tokens.client(client_id)
        if client is None and ctx.tokens.lenient_clients:
            client = ctx.tokens.register_client(client_id)
        if client is None or (client["redirect_uris"] and redirect_uri not in client["redirect_uris"]):
            c.invalid("Unregistered client or redirect_uri")
        scopes = scope_list(query.get("scope", "default").split())
        challenge = query.get("code_challenge")
        method = query.get("code_challenge_method", "plain")
        if challenge and method not in ("plain", "S256"):
            c.invalid("Unsupported code_challenge_method")
        hint = query.get("login_hint")
        if not hint:
            return HTMLResponse(
                render_account_picker(
                    provider_title="Asana",
                    app_name=client["name"],
                    scopes=scopes,
                    accounts=[
                        {"display_name": u["name"], "email": u["email"], "login_hint": u["email"]}
                        for u in ctx.store["users"]
                    ],
                    action_base=str(request.url),
                )
            )
        uid = c.user_id(ctx, hint)
        if uid is None:
            c.invalid("Unknown login_hint")
        code = ctx.tokens.issue_code(
            client_id=client_id,
            user_id=uid,
            scopes=scopes,
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            code_challenge_method=method if challenge else None,
        )
        return RedirectResponse(
            redirect_with_params(redirect_uri, {"code": code.token, "state": query.get("state")}),
            status_code=302,
        )

    async def parameters(request: Request) -> dict[str, str]:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            raise OAuthError("invalid_request", "The request must be form encoded.")
        form = await request.form()
        return {key: value for key, value in form.items() if isinstance(value, str)}

    def pair(source: Any, refresh_token: str | None = None) -> dict[str, Any]:
        refresh = ctx.tokens.resolve(refresh_token, "refresh") if refresh_token else None
        if refresh is None:
            refresh = ctx.tokens.issue_refresh(
                client_id=source.client_id,
                user_id=source.user_id,
                scopes=source.scopes,
                ttl=None,
                prefix="1/",
            )
        access = ctx.tokens.issue_access(
            client_id=source.client_id,
            user_id=source.user_id,
            scopes=source.scopes,
            prefix="1/",
            extra={"refresh_token": refresh.token},
        )
        user = ctx.store["users"].get(source.user_id)
        return {
            "access_token": access.token,
            "expires_in": 3600,
            "token_type": "bearer",
            "refresh_token": refresh.token,
            "data": {
                "id": int(user["gid"]),
                "gid": user["gid"],
                "name": user["name"],
                "email": user["email"],
            },
        }

    @app.post("/-/oauth_token")
    async def token(request: Request) -> Any:
        try:
            data = await parameters(request)
            client = ctx.tokens.authenticate_client(data.get("client_id"), data.get("client_secret"))
            if not data.get("client_secret"):
                raise OAuthError("invalid_client", "client_secret is required.", 401)
            if data.get("grant_type") == "authorization_code":
                code = ctx.tokens.resolve(data.get("code"), "code")
                if code is None or code.redirect_uri != data.get("redirect_uri"):
                    raise OAuthError("invalid_grant", "The code or redirect_uri is invalid.")
                source = ctx.tokens.redeem_code(
                    data.get("code"),
                    client_id=client["id"],
                    redirect_uri=data.get("redirect_uri"),
                    code_verifier=data.get("code_verifier"),
                )
                return pair(source)
            if data.get("grant_type") == "refresh_token":
                source = ctx.tokens.resolve(data.get("refresh_token"), "refresh")
                if source is None or source.client_id != client["id"]:
                    raise OAuthError("invalid_grant", "The refresh token is invalid.")
                return pair(source, source.token)
            raise OAuthError("unsupported_grant_type", "Expected authorization_code or refresh_token.")
        except OAuthError as exc:
            return JSONResponse(
                {"error": exc.error, "error_description": exc.description}, status_code=exc.status
            )

    @app.post("/-/oauth_revoke")
    async def revoke(request: Request) -> Any:
        try:
            data = await parameters(request)
            client = ctx.tokens.authenticate_client(data.get("client_id"), data.get("client_secret"))
            if not data.get("client_secret") or not data.get("token"):
                raise OAuthError("invalid_request", "client_secret and token are required.")
            grant = ctx.tokens.get(data["token"])
            if grant and (grant.kind != "refresh" or grant.client_id != client["id"]):
                raise OAuthError("invalid_request", "Expected a refresh token issued to this client.")
            ctx.tokens.revoke(data["token"])
            for row in ctx.store["_grants"]:
                if row.get("extra", {}).get("refresh_token") == data["token"]:
                    ctx.tokens.revoke(row["id"])
            return Response(status_code=200)
        except OAuthError as exc:
            return JSONResponse(
                {"error": exc.error, "error_description": exc.description}, status_code=exc.status
            )
