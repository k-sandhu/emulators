"""Headless public OAuth and internal integration credentials."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from emulators.core.auth import (
    Grant,
    OAuthError,
    basic_credentials,
    redirect_with_params,
    render_account_picker,
)
from emulators.core.provider import ProviderContext
from emulators.providers.notion.common import (
    BOT_ID,
    allowed,
    body,
    fail,
    invalid,
    person_id,
    public,
    uid,
    user_value,
    version,
    world_user,
)

CAPABILITIES = [
    "read_content",
    "update_content",
    "insert_content",
    "read_comments",
    "insert_comments",
    "read_user_with_email",
]


def capability(grant: Grant, wanted: str) -> None:
    if wanted not in grant.scopes:
        fail(403, "restricted_resource", f"This integration does not have {wanted} capability.")


def bot(ctx: ProviderContext, grant: Grant) -> dict[str, Any]:
    result = public(ctx.store["users"].get(BOT_ID))
    if pid := world_user(ctx, grant.user_id):
        result["bot"]["owner"] = {"type": "user", "user": user_value(ctx, person_id(pid), grant)}
    return result


def issue_pair(ctx: ProviderContext, source: Grant) -> dict[str, Any]:
    access = ctx.tokens.issue_access(
        client_id=source.client_id,
        user_id=source.user_id,
        scopes=source.scopes,
        prefix="ntn_",
        ttl=timedelta(hours=1),
        extra={k: v for k, v in source.extra.items() if k not in ("access_token", "refresh_token")},
    )
    refresh = ctx.tokens.issue_refresh(
        client_id=source.client_id,
        user_id=source.user_id,
        scopes=source.scopes,
        prefix="ntn_",
        extra={**source.extra, "access_token": access.token},
    )
    ctx.store["_grants"].get(access.token)["extra"] = {**source.extra, "refresh_token": refresh.token}
    return {
        "access_token": access.token,
        "token_type": "bearer",
        "refresh_token": refresh.token,
        "bot_id": BOT_ID,
        "workspace_id": ctx.store.meta["workspace_id"],
        "workspace_name": ctx.world.org.name,
        "workspace_icon": None,
        "owner": bot(ctx, access)["bot"]["owner"],
        "duplicated_template_id": None,
        "request_id": ctx.ids.guid(),
    }


def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Mint a Notion token, optionally as an employee or a limited-capability integration."""
    person = world_user(ctx, payload.get("user"))
    if payload.get("user") and person is None:
        invalid("user must be an active employee's world id, Notion UUID, or email.")
    extra = {}
    if "shared_page_ids" in payload:
        if not isinstance(payload["shared_page_ids"], list):
            invalid("shared_page_ids must be an array.")
        extra["shared_page_ids"] = [uid(v) for v in payload["shared_page_ids"]]
    seconds = payload.get("expires_in", 3600)
    if not isinstance(seconds, int) or isinstance(seconds, bool) or abs(seconds) > 315360000:
        invalid("expires_in must be an integer within ten years.")
    scopes = payload.get("scopes", CAPABILITIES)
    if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
        invalid("scopes must be an array of capability names.")
    if "app_only" in payload and not isinstance(payload["app_only"], bool):
        invalid("app_only must be boolean.")
    if not isinstance(payload.get("client_id", "notion-client"), str):
        invalid("client_id must be a string.")
    grant = ctx.tokens.issue_access(
        client_id=payload.get("client_id", "notion-client"),
        user_id=person,
        scopes=scopes,
        app_only=bool(payload.get("app_only", person is None)),
        prefix="ntn_",
        ttl=timedelta(seconds=seconds) if seconds else timedelta(microseconds=1),
        extra=extra,
    )
    return {
        "access_token": grant.token,
        "token_type": "bearer",
        "expires_in": seconds,
        "bot_id": BOT_ID,
        "workspace_id": ctx.store.meta["workspace_id"],
    }


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/v1/oauth/authorize")
    async def authorize(request: Request) -> Any:
        params = request.query_params
        client_id, redirect_uri = params.get("client_id"), params.get("redirect_uri")
        if (
            not client_id
            or not redirect_uri
            or params.get("response_type") != "code"
            or params.get("owner", "user") != "user"
        ):
            invalid("client_id, redirect_uri, response_type=code and owner=user are required.")
        client = ctx.tokens.client(client_id)
        if client is None and ctx.tokens.lenient_clients:
            client = ctx.tokens.register_client(client_id)
        if client is None or (client["redirect_uris"] and redirect_uri not in client["redirect_uris"]):
            fail(400, "invalid_request", "Unknown client or unregistered redirect_uri.")
        if not params.get("login_hint"):
            return HTMLResponse(
                render_account_picker(
                    provider_title="Notion",
                    app_name=client["name"],
                    scopes=["Selected workspace pages"],
                    accounts=[
                        {"display_name": p.display_name, "email": p.email, "login_hint": p.email}
                        for p in ctx.world.active_employees
                    ],
                    action_base=str(request.url),
                )
            )
        person = world_user(ctx, params.get("login_hint"))
        if person is None:
            invalid("login_hint must identify an active workspace member.")
        extra = {}
        if selected := params.get("shared_page_ids"):
            extra["shared_page_ids"] = [uid(value) for value in selected.split(",")]
            check = Grant("", "access", client_id, person, [], ctx.clock.now(), None)
            for page_id in extra["shared_page_ids"]:
                record = ctx.store["pages"].get(page_id) or ctx.store["databases"].get(page_id)
                if not record or not allowed(ctx, check, record):
                    fail(
                        403, "restricted_resource", "Cannot share a page the authorizing user cannot access."
                    )
        code = ctx.tokens.issue_code(
            client_id=client_id, user_id=person, scopes=CAPABILITIES, redirect_uri=redirect_uri, extra=extra
        )
        return RedirectResponse(
            redirect_with_params(redirect_uri, {"code": code.token, "state": params.get("state")}),
            status_code=302,
        )

    def client_auth(request: Request) -> str:
        credentials = basic_credentials(request)
        if credentials is None:
            fail(401, "invalid_client", "Basic client authentication is required.")
        try:
            ctx.tokens.authenticate_client(*credentials)
        except OAuthError as exc:
            fail(exc.status, exc.error, exc.description)
        return credentials[0]

    @app.post("/v1/oauth/token")
    async def token(request: Request) -> dict[str, Any]:
        version(request)
        client_id = client_auth(request)
        payload = await body(request)
        for field in ("grant_type", "code", "redirect_uri", "refresh_token"):
            if field in payload and not isinstance(payload[field], str):
                fail(400, "invalid_request", f"{field} must be a string.")
        try:
            if payload.get("grant_type") == "authorization_code":
                grant = ctx.tokens.resolve(payload.get("code"), "code")
                if grant and grant.redirect_uri != payload.get("redirect_uri"):
                    raise OAuthError(
                        "invalid_grant", "redirect_uri does not match the authorization request."
                    )
                grant = ctx.tokens.redeem_code(
                    payload.get("code"),
                    client_id=client_id,
                    redirect_uri=payload.get("redirect_uri"),
                    code_verifier=None,
                )
            elif payload.get("grant_type") == "refresh_token":
                grant = ctx.tokens.resolve(payload.get("refresh_token"), "refresh")
                if grant is None or grant.client_id != client_id:
                    raise OAuthError("invalid_grant", "Refresh token is invalid.")
                ctx.tokens.revoke(grant.token)
                if previous := grant.extra.get("access_token"):
                    ctx.tokens.revoke(previous)
            else:
                raise OAuthError("unsupported_grant_type", "Expected authorization_code or refresh_token.")
        except OAuthError as exc:
            fail(exc.status, exc.error, exc.description)
        return issue_pair(ctx, grant)

    @app.post("/v1/oauth/introspect")
    async def introspect(request: Request) -> dict[str, Any]:
        version(request)
        client_id = client_auth(request)
        payload = await body(request)
        if not isinstance(payload.get("token"), str) or not payload["token"]:
            fail(400, "invalid_request", "token must be a non-empty string.")
        grant = ctx.tokens.resolve(payload.get("token"))
        result = {"active": bool(grant and grant.client_id == client_id), "request_id": ctx.ids.guid()}
        if result["active"]:
            result.update(scope=" ".join(grant.scopes), iat=int(grant.issued_at.timestamp()))
        return result

    @app.post("/v1/oauth/revoke")
    async def revoke(request: Request) -> JSONResponse:
        version(request)
        client_id = client_auth(request)
        payload = await body(request)
        if not isinstance(payload.get("token"), str) or not payload["token"]:
            fail(400, "invalid_request", "token must be a non-empty string.")
        grant = ctx.tokens.get(payload.get("token"))
        if grant and grant.client_id == client_id:
            ctx.tokens.revoke(grant.token)
            for field in ("refresh_token", "access_token"):
                if linked := grant.extra.get(field):
                    ctx.tokens.revoke(linked)
        return JSONResponse({})
