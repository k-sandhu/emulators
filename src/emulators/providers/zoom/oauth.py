"""Zoom OAuth: authorization code (+PKCE), refresh (rotating), Server-to-Server
``account_credentials`` and chatbot ``client_credentials`` grants, revoke, token minting.

Token-endpoint failures use Zoom's ``{"reason": "...", "error": "..."}`` body. Zoom has no
``scope`` parameter on the authorize URL (scopes are configured on the Marketplace app), so the
scopes of a grant come from the app registered with the ``register_app`` action, defaulting to a
broad admin set for unregistered (lenient) clients.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import (
    Grant,
    OAuthError,
    basic_credentials,
    redirect_with_params,
    render_account_picker,
)
from emulators.core.provider import Provider, ProviderContext
from emulators.providers.zoom.common import (
    DEFAULT_SCOPES,
    JSON_TYPE,
    ZoomJSONResponse,
    externalize,
    zoom_token,
    zoom_user_id,
)
from emulators.world.model import Person

ACCESS_TTL_S = 3599
REFRESH_TTL = timedelta(days=90)
CHATBOT_SCOPES = "imchat:bot"
NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}


def resolve_person(ctx: ProviderContext, value: str | None) -> Person | None:
    if not value:
        return None
    needle = value.strip().lower()
    for person in ctx.world.employees:
        if needle in (person.id.lower(), person.email.lower(), zoom_user_id(ctx.world, person.id).lower()):
            return person
    return None


def split_scopes(value: Any) -> list[str]:
    if isinstance(value, str):
        return value.replace(",", " ").split()
    return [str(v) for v in (value or [])]


def app_scopes(ctx: ProviderContext, client: dict[str, Any], fallback: str) -> list[str]:
    return split_scopes(client.get("scopes")) or split_scopes(fallback)


def issue_pair(
    ctx: ProviderContext,
    *,
    client_id: str,
    user: Person | None,
    scopes: list[str],
    with_refresh: bool,
    chain: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Issue an access token (and a refresh token for user-level grants) in Zoom's response shape."""
    uid = zoom_user_id(ctx.world, user.id) if user else ctx.world.org.zoom_account_id
    refresh = None
    extra: dict[str, Any] = dict(chain or {})
    if with_refresh:
        refresh = ctx.tokens.issue_refresh(
            client_id=client_id,
            user_id=user.id if user else None,
            scopes=scopes,
            token=zoom_token(ctx, kind="refresh", uid=uid, client_id=client_id, ttl_s=int(REFRESH_TTL.total_seconds())),
            ttl=REFRESH_TTL,
        )
        extra["refresh"] = refresh.token
    access = ctx.tokens.issue_access(
        client_id=client_id,
        user_id=user.id if user else None,
        scopes=scopes,
        app_only=user is None,
        ttl=timedelta(seconds=ACCESS_TTL_S),
        token=zoom_token(ctx, kind="access", uid=uid, client_id=client_id, ttl_s=ACCESS_TTL_S),
        extra=extra,
    )
    body: dict[str, Any] = {"access_token": access.token, "token_type": "bearer"}
    if refresh is not None:
        body["refresh_token"] = refresh.token
    body["expires_in"] = ACCESS_TTL_S
    body["scope"] = " ".join(scopes)
    body["api_url"] = "https://api.zoom.us"
    return body


def oauth_failure(error: str, reason: str, status: int = 400) -> JSONResponse:
    return ZoomJSONResponse({"reason": reason, "error": error}, status_code=status, headers=NO_STORE)


async def collect_params(request: Request) -> dict[str, str]:
    """Zoom accepts token-endpoint parameters in the query string as well as a form body."""
    params = {k: v for k, v in request.query_params.items()}
    raw = await request.body()
    if raw:
        content_type = request.headers.get("content-type", "")
        if "json" in content_type:
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
            params.update({k: str(v) for k, v in data.items()} if isinstance(data, dict) else {})
        else:
            params.update({k: v[-1] for k, v in parse_qs(raw.decode(), keep_blank_values=True).items()})
    return params


def client_auth(request: Request, params: dict[str, str]) -> tuple[str | None, str | None]:
    basic = basic_credentials(request)
    if basic:
        return basic
    return params.get("client_id"), params.get("client_secret")


def _failure(exc: OAuthError) -> JSONResponse:
    if exc.error == "invalid_client":
        return oauth_failure("invalid_client", "Invalid client_id or client_secret", 401)
    if exc.error == "invalid_grant":
        reason = exc.description
        if "PKCE" in reason:
            return oauth_failure("invalid_request", "Invalid code_verifier.")
        if "redirect_uri" in reason:
            return oauth_failure("invalid_request", "Redirect URI mismatch.")
        return oauth_failure("invalid_grant", "Invalid authorization code")
    return oauth_failure(exc.error, exc.description or exc.error, exc.status)


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/oauth/authorize")
    async def authorize(request: Request) -> Response:
        q = request.query_params
        client_id, redirect_uri = q.get("client_id", ""), q.get("redirect_uri", "")
        if not client_id or not redirect_uri:
            return oauth_failure("invalid_request", "Missing required parameter: client_id or redirect_uri")
        if q.get("response_type", "code") != "code":
            return oauth_failure("unsupported_response_type", "Invalid response_type")
        client = ctx.tokens.authenticate_client(client_id, None, require_secret=False)
        if client.get("redirect_uris") and redirect_uri not in client["redirect_uris"]:
            return oauth_failure("invalid_request", "Redirect URI mismatch.")
        state = q.get("state")
        if q.get("error") == "access_denied":
            return RedirectResponse(
                redirect_with_params(redirect_uri, {"error": "access_denied", "state": state}), status_code=302
            )
        hint = q.get("login_hint")
        person = resolve_person(ctx, hint)
        if hint and (person is None or not person.active):
            return RedirectResponse(
                redirect_with_params(
                    redirect_uri,
                    {"error": "access_denied", "error_description": "The user does not exist.", "state": state},
                ),
                status_code=302,
            )
        scopes = split_scopes(q.get("scope")) or app_scopes(ctx, client, DEFAULT_SCOPES)
        if person is None:
            return HTMLResponse(
                render_account_picker(
                    provider_title="Zoom",
                    app_name=client.get("name") or client_id,
                    scopes=scopes,
                    accounts=[
                        {"display_name": p.display_name, "email": p.email, "login_hint": p.email}
                        for p in ctx.world.active_employees
                    ],
                    action_base=str(request.url),
                )
            )
        code = ctx.tokens.issue_code(
            client_id=client_id,
            user_id=person.id,
            scopes=scopes,
            redirect_uri=redirect_uri,
            code_challenge=q.get("code_challenge"),
            code_challenge_method=q.get("code_challenge_method"),
        )
        return RedirectResponse(redirect_with_params(redirect_uri, {"code": code.token, "state": state}), status_code=302)

    @app.post("/oauth/token")
    async def token(request: Request) -> Response:
        params = await collect_params(request)
        client_id, client_secret = client_auth(request, params)
        grant_type = params.get("grant_type")
        try:
            if not client_id:
                raise OAuthError("invalid_client", "missing client", 401)
            client = ctx.tokens.authenticate_client(client_id, client_secret)
            if not grant_type:
                return oauth_failure("invalid_request", "Missing grant type")
            if grant_type == "authorization_code":
                if not params.get("code"):
                    return oauth_failure("invalid_request", "Missing authorization code")
                grant = ctx.tokens.redeem_code(
                    params["code"],
                    client_id=client_id,
                    redirect_uri=params.get("redirect_uri"),
                    code_verifier=params.get("code_verifier"),
                )
                user = resolve_person(ctx, grant.user_id)
                body = issue_pair(
                    ctx, client_id=client_id, user=user, scopes=grant.scopes, with_refresh=True
                )
            elif grant_type == "refresh_token":
                old = ctx.tokens.resolve(params.get("refresh_token"), "refresh")
                if old is None or old.client_id != client_id:
                    return oauth_failure("invalid_request", "Invalid Token!")
                ctx.tokens.revoke(old.token)  # refresh tokens rotate: the previous one dies here
                body = issue_pair(
                    ctx,
                    client_id=client_id,
                    user=resolve_person(ctx, old.user_id),
                    scopes=old.scopes,
                    with_refresh=True,
                )
            elif grant_type == "account_credentials":
                account_id = params.get("account_id")
                if not account_id:
                    return oauth_failure("invalid_request", "Missing account_id")
                if account_id not in (ctx.world.org.zoom_account_id, client.get("account_id")):
                    return oauth_failure("invalid_request", "Invalid Token!")
                body = issue_pair(
                    ctx,
                    client_id=client_id,
                    user=None,
                    scopes=app_scopes(ctx, client, DEFAULT_SCOPES),
                    with_refresh=False,
                )
            elif grant_type == "client_credentials":
                body = issue_pair(
                    ctx,
                    client_id=client_id,
                    user=None,
                    scopes=app_scopes(ctx, client, CHATBOT_SCOPES),
                    with_refresh=False,
                )
            else:
                return oauth_failure("unsupported_grant_type", "Invalid grant type")
        except OAuthError as exc:
            return _failure(exc)
        return ZoomJSONResponse(externalize(request, ctx, body), headers=NO_STORE)

    @app.post("/oauth/revoke")
    async def revoke(request: Request) -> Response:
        params = await collect_params(request)
        client_id, client_secret = client_auth(request, params)
        try:
            if not client_id:
                raise OAuthError("invalid_client", "missing client", 401)
            ctx.tokens.authenticate_client(client_id, client_secret)
        except OAuthError as exc:
            return _failure(exc)
        token_value = params.get("token")
        grant = ctx.tokens.get(token_value)
        if grant is not None:
            ctx.tokens.revoke(grant.token)
            if grant.kind == "refresh":
                for record in ctx.store.collection("_grants"):
                    if record.get("extra", {}).get("refresh") == grant.token:
                        record["revoked"] = True
        return Response(
            json.dumps({"status": "success"}), media_type=JSON_TYPE, headers=NO_STORE
        )


# -- actions ------------------------------------------------------------------------------


def actions(provider: Provider) -> None:
    @provider.action("mint_token")
    def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Mint an access token without an OAuth dance.

        ``{"user": "<person id|email|zoom id>", "scopes": [...], "app_only": bool, "expires_in": s}``.
        No ``user`` (or ``app_only``) gives an account-level (Server-to-Server style) token. Omit
        ``scopes`` for an unrestricted token; pass them to exercise scope enforcement."""
        from emulators.core.errors import EmulatorError

        person = resolve_person(ctx, payload.get("user"))
        if payload.get("user") and person is None:
            raise EmulatorError(400, {"reason": "Unknown user", "error": "invalid_request"})
        app_only = bool(payload.get("app_only", person is None))
        ttl = int(payload.get("expires_in", ACCESS_TTL_S))
        client_id = payload.get("client_id", "emulator-client")
        scopes = split_scopes(payload.get("scopes"))
        uid = zoom_user_id(ctx.world, person.id) if person else ctx.world.org.zoom_account_id
        grant: Grant = ctx.tokens.issue_access(
            client_id=client_id,
            user_id=None if app_only else (person.id if person else None),
            scopes=scopes,
            app_only=app_only,
            ttl=timedelta(seconds=ttl),
            token=zoom_token(ctx, kind="access", uid=uid, client_id=client_id, ttl_s=ttl),
        )
        return {"access_token": grant.token, "token_type": "bearer", "expires_in": ttl, "scope": " ".join(scopes)}

    @provider.action("register_app")
    def register_app(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Register an OAuth app: ``{client_id, client_secret, redirect_uris, scopes, name, account_id}``.

        Registered apps get exactly ``scopes`` on their tokens (space separated or a list); unknown
        clients are accepted leniently and get a broad default scope set."""
        client_id = payload.get("client_id") or "emulator-client"
        record = ctx.tokens.register_client(
            client_id,
            payload.get("client_secret"),
            redirect_uris=list(payload.get("redirect_uris") or []),
            name=payload.get("name") or client_id,
            scopes=split_scopes(payload.get("scopes")),
            account_id=payload.get("account_id"),
        )
        return {k: v for k, v in record.items() if k != "client_secret"}
