"""Intuit OAuth 2.0 / OpenID Connect endpoints.

Access tokens are long JWE-looking opaque strings and refresh tokens look like
``AB11...``; both are backed by the shared :class:`TokenService` grants.
Refresh tokens rotate, but the previous one stays usable until the rotated
token is itself used (Intuit keeps the old one valid for ~24h).
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import (
    OAuthError,
    basic_credentials,
    bearer_token,
    redirect_with_params,
    render_account_picker,
)
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.quickbooks.common import realm
from emulators.world.model import Person

CLIENT_ID = "AB0Emu1atorQuickBooksC1ientId000001"
CLIENT_SECRET = "emu1ator-quickbooks-secret"
ISSUER = "https://oauth.platform.intuit.com/op/v1"
SCOPES = ["com.intuit.quickbooks.accounting", "openid", "profile", "email", "phone", "address"]


def _access_token() -> str:
    head = "eyJlbmMiOiJBMTI4Q0JDLUhTMjU2IiwiYWxnIjoiZGlyIn0"
    return f"{head}..{secrets.token_urlsafe(90)}.{secrets.token_urlsafe(40)}"[:220]


def _refresh_token() -> str:
    return "AB11" + secrets.token_urlsafe(50)[:60]


def find_user(ctx: ProviderContext, hint: str | None) -> dict[str, Any] | None:
    if not hint:
        return None
    users = ctx.store.collection("_users")
    record = users.get(hint)
    if record is not None:
        return record
    for record in users:
        if record.get("email", "").lower() == hint.lower():
            return record
    return None


def default_user(ctx: ProviderContext) -> dict[str, Any]:
    for record in ctx.store.collection("_users"):
        if record.get("role") == "full" and record.get("active"):
            return record
    raise RuntimeError("no seed user")


def person_for(ctx: ProviderContext, user: dict[str, Any]) -> Person:
    return ctx.world.person(str(user["id"]))


def issue_tokens(
    ctx: ProviderContext,
    *,
    client_id: str,
    user: dict[str, Any],
    scopes: list[str],
    previous_refresh: str | None = None,
) -> dict[str, Any]:
    access = ctx.tokens.issue_access(
        client_id=client_id,
        user_id=str(user["id"]),
        scopes=scopes or list(SCOPES),
        ttl=timedelta(seconds=3600),
        token=_access_token(),
        extra={"realm_id": realm(ctx)},
    )
    refresh = ctx.tokens.issue_refresh(
        client_id=client_id,
        user_id=str(user["id"]),
        scopes=scopes or list(SCOPES),
        ttl=timedelta(seconds=8726400),
        token=_refresh_token(),
        extra={"realm_id": realm(ctx), "previous": previous_refresh},
    )
    body: dict[str, Any] = {
        "token_type": "bearer",
        "access_token": access.token,
        "expires_in": 3600,
        "refresh_token": refresh.token,
        "x_refresh_token_expires_in": 8726400,
    }
    if "openid" in (scopes or []):
        body["id_token"] = _id_token(ctx, client_id=client_id, user=user)
    return body


def _id_token(ctx: ProviderContext, *, client_id: str, user: dict[str, Any]) -> str:
    person = person_for(ctx, user)
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "aud": [client_id],
        "sub": user["sub"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "auth_time": int(now.timestamp()),
        "email": person.email,
        "given_name": person.first_name,
        "family_name": person.last_name,
        "realmid": realm(ctx),
    }
    return ctx.signing_key.sign(claims, headers={"kid": ctx.signing_key.kid})


async def _form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    raw = await request.body()
    if "application/json" in content_type and raw:
        try:
            data = json.loads(raw)
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _oauth_error(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status)


def _discovery(request: Request, ctx: ProviderContext, sandbox: bool) -> dict[str, Any]:
    userinfo_host = "sandbox-accounts.platform.intuit.com" if sandbox else "accounts.platform.intuit.com"
    # keep the exact hosts intuitlib/python-quickbooks expect
    userinfo = url_for_host(request, ctx, userinfo_host, "/v1/openid_connect/userinfo")
    return {
        "issuer": ISSUER,
        "authorization_endpoint": url_for_host(request, ctx, "appcenter.intuit.com", "/connect/oauth2"),
        "token_endpoint": url_for_host(request, ctx, "oauth.platform.intuit.com", "/oauth2/v1/tokens/bearer"),
        "userinfo_endpoint": userinfo,
        "revocation_endpoint": url_for_host(
            request, ctx, "oauth.platform.intuit.com", "/oauth2/v1/tokens/revoke"
        ),
        "jwks_uri": url_for_host(request, ctx, "oauth.platform.intuit.com", "/op/v1/jwks"),
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": SCOPES,
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        "claims_supported": ["sub", "email", "given_name", "family_name", "phone_number", "address"],
        "service_documentation": "https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization",
        "ui_locales_supported": ["en-US"],
    }


def register_identity(app: FastAPI, ctx: ProviderContext, provider: Any) -> None:
    @app.get("/.well-known/openid_sandbox_configuration")
    @app.get("/.well-known/openid_sandbox_configuration/")
    async def discovery_sandbox(request: Request) -> Any:
        return _discovery(request, ctx, True)

    @app.get("/.well-known/openid_configuration")
    @app.get("/.well-known/openid_configuration/")
    async def discovery_prod(request: Request) -> Any:
        return _discovery(request, ctx, False)

    @app.get("/op/v1/jwks")
    async def jwks() -> Any:
        return ctx.signing_key.jwks()

    @app.get("/v1/openid_connect/userinfo")
    async def userinfo(request: Request) -> Any:
        grant = ctx.tokens.resolve(bearer_token(request))
        if grant is None:
            return JSONResponse(
                {"error": "invalid_token", "error_description": "Access token is invalid or expired"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer [REDACTED]"invalid_token"'},
            )
        user = find_user(ctx, grant.user_id) or default_user(ctx)
        person = person_for(ctx, user)
        office = ctx.world.office(person.office_id) if person.office_id else None
        address = office.address if office else ctx.world.org.address
        return {
            "sub": user["sub"],
            "email": person.email,
            "emailVerified": True,
            "givenName": person.first_name,
            "familyName": person.last_name,
            "phoneNumber": person.phone_business or person.phone_mobile or "",
            "phoneNumberVerified": True,
            "address": {
                "streetAddress": address.street,
                "locality": address.city,
                "region": address.state,
                "postalCode": address.postal_code,
                "country": address.country,
            },
        }

    @app.get("/connect/oauth2")
    async def authorize(request: Request) -> Response:
        params = request.query_params
        client_id = params.get("client_id") or ""
        redirect_uri = params.get("redirect_uri") or ""
        state = params.get("state")
        scope_text = params.get("scope") or " ".join(SCOPES)
        scopes = [s for s in scope_text.replace(",", " ").split() if s]
        if params.get("response_type", "code") != "code":
            if redirect_uri:
                return RedirectResponse(
                    redirect_with_params(
                        redirect_uri, {"error": "unsupported_response_type", "state": state}
                    ),
                    status_code=302,
                )
            return _oauth_error("unsupported_response_type")
        if not redirect_uri:
            return HTMLResponse("<h1>Intuit (emulator)</h1><p>redirect_uri is required.</p>", status_code=400)
        try:
            ctx.tokens.authenticate_client(client_id, None, require_secret=False)
        except OAuthError as exc:
            return RedirectResponse(
                redirect_with_params(redirect_uri, {"error": exc.error, "state": state}), status_code=302
            )
        user = find_user(ctx, params.get("login_hint"))
        if user is None:
            base = (
                str(request.url).split("?")[0]
                + "?"
                + urlencode({k: v for k, v in params.items() if k != "login_hint"})
            )
            accounts = [
                {
                    "display_name": r["display_name"],
                    "email": r["email"],
                    "login_hint": r["email"],
                }
                for r in ctx.store.collection("_users")
                if r.get("active")
            ]
            return HTMLResponse(
                render_account_picker(
                    provider_title="Intuit QuickBooks",
                    app_name=client_id or "QuickBooks app",
                    scopes=scopes,
                    accounts=accounts[:60],
                    action_base=base,
                )
            )
        code = ctx.tokens.issue_code(
            client_id=client_id,
            user_id=str(user["id"]),
            scopes=scopes,
            redirect_uri=redirect_uri,
            extra={"realm_id": realm(ctx)},
        )
        return RedirectResponse(
            redirect_with_params(redirect_uri, {"code": code.token, "state": state, "realmId": realm(ctx)}),
            status_code=302,
        )

    @app.post("/oauth2/v1/tokens/bearer")
    @app.post("/v1/tokens/bearer")
    async def token_endpoint(request: Request) -> Response:
        form = await _form(request)
        credentials = basic_credentials(request)
        client_id = (credentials[0] if credentials else None) or form.get("client_id")
        client_secret = (credentials[1] if credentials else None) or form.get("client_secret")
        try:
            ctx.tokens.authenticate_client(client_id, client_secret)
        except OAuthError as exc:
            return _oauth_error(exc.error, exc.status)
        assert client_id
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            try:
                code = ctx.tokens.redeem_code(
                    form.get("code"),
                    client_id=client_id,
                    redirect_uri=form.get("redirect_uri"),
                    code_verifier=form.get("code_verifier"),
                )
            except OAuthError as exc:
                return _oauth_error(exc.error, exc.status)
            user = find_user(ctx, code.user_id) or default_user(ctx)
            return JSONResponse(
                issue_tokens(ctx, client_id=client_id, user=user, scopes=code.scopes),
                headers={"Cache-Control": "no-store"},
            )
        if grant_type == "refresh_token":
            presented = form.get("refresh_token") or ""
            grant = _resolve_refresh(ctx, presented)
            if grant is None:
                return _oauth_error("invalid_grant")
            user = find_user(ctx, grant.user_id) or default_user(ctx)
            return JSONResponse(
                issue_tokens(
                    ctx,
                    client_id=client_id,
                    user=user,
                    scopes=grant.scopes,
                    previous_refresh=presented,
                ),
                headers={"Cache-Control": "no-store"},
            )
        if grant_type == "client_credentials":
            return _oauth_error("unsupported_grant_type")
        return _oauth_error("unsupported_grant_type")

    @app.post("/oauth2/v1/tokens/revoke")
    @app.post("/v2/oauth2/tokens/revoke")
    @app.post("/v1/tokens/revoke")
    async def revoke_endpoint(request: Request) -> Response:
        form = await _form(request)
        credentials = basic_credentials(request)
        client_id = (credentials[0] if credentials else None) or form.get("client_id")
        client_secret = (credentials[1] if credentials else None) or form.get("client_secret")
        try:
            ctx.tokens.authenticate_client(client_id, client_secret)
        except OAuthError as exc:
            return _oauth_error(exc.error, exc.status)
        token_value = form.get("token") or form.get("refresh_token") or form.get("access_token")
        if token_value:
            record = ctx.store.collection("_grants").get(token_value)
            if record is not None:
                user_id = record.get("user_id")
                ctx.tokens.revoke(token_value)
                # revoking a refresh token disconnects the app for that user
                if record.get("kind") == "refresh" and user_id:
                    ctx.tokens.revoke_user(str(user_id))
        return Response(status_code=200)

    @app.get("/api/v1/user/current")
    async def current_user(request: Request) -> Any:
        grant = ctx.tokens.resolve(bearer_token(request))
        if grant is None:
            raise EmulatorError(401, {"error": "invalid_token"})
        user = find_user(ctx, grant.user_id) or default_user(ctx)
        person = person_for(ctx, user)
        return {
            "firstName": person.first_name,
            "lastName": person.last_name,
            "emailAddress": person.email,
            "phoneNumber": person.phone_business or "",
        }

    _ = provider
    _ = stable_hash


def _resolve_refresh(ctx: ProviderContext, presented: str) -> Any:
    """Intuit keeps the just-rotated refresh token valid until its successor is used.

    Rotation leaves the predecessor grant live; the first time the successor is
    presented, the predecessor is revoked.
    """
    grant = ctx.tokens.resolve(presented, "refresh")
    if grant is None:
        return None
    previous = (grant.extra or {}).get("previous")
    if previous and str(previous) != presented:
        ctx.tokens.revoke(str(previous))
    return grant
