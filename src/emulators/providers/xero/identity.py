"""OAuth code/PKCE and Custom Connections, backed by the hub token service."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import (
    Grant,
    OAuthError,
    basic_credentials,
    redirect_with_params,
    render_account_picker,
)
from emulators.core.clock import isoformat_z
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext, url_for_host

from .common import FULL_SCOPES, actor, guid, main_tenant, missing, problem, public, validation


async def parameters(request: Request) -> dict[str, str]:
    """OAuth token requests use form fields, never JSON or uploaded files."""
    if (
        request.headers.get("content-type", "").split(";", 1)[0].lower()
        != "application/x-www-form-urlencoded"
    ):
        raise OAuthError("invalid_request", "A form-encoded request body is required.")
    try:
        form = await request.form()
    except (ValueError, UnicodeDecodeError) as exc:
        raise OAuthError("invalid_request", "Invalid form-encoded body.") from exc
    if any(not isinstance(v, str) for v in form.values()):
        raise OAuthError("invalid_request", "Form values must be strings.")
    return {key: str(value) for key, value in form.items()}


def connect(ctx: ProviderContext, grant: Grant) -> None:
    tenants = [main_tenant(ctx)] if grant.app_only else ctx.store.collection("_tenants").ids()
    for tenant in tenants:
        identifier = guid("connection", f"{grant.client_id}/{grant.user_id}/{tenant}")
        if existing := ctx.store.collection("_connections").get(identifier):
            existing["authEventId"] = grant.extra["authentication_event_id"]
            existing["updatedDateUtc"] = isoformat_z(ctx.clock.now())
            continue
        name = ctx.store.collection("_tenants").get(tenant)["name"]
        ctx.store.collection("_connections").put(
            {
                "id": identifier,
                "authEventId": grant.extra["authentication_event_id"],
                "tenantId": tenant,
                "tenantType": "ORGANISATION",
                "tenantName": name,
                "createdDateUtc": isoformat_z(ctx.clock.now()),
                "updatedDateUtc": isoformat_z(ctx.clock.now()),
                "_client": grant.client_id,
                "_user": grant.user_id,
            }
        )


def issue(
    ctx: ProviderContext,
    *,
    client: str,
    user: str | None,
    scopes: list[str],
    app_only: bool,
    extra: dict[str, Any] | None = None,
    ttl: int = 1800,
    new_connection: bool = False,
    refresh: bool = True,
) -> dict[str, Any]:
    metadata = dict(extra or {})
    metadata.setdefault("authentication_event_id", ctx.ids.guid())
    metadata.setdefault("family", ctx.ids.guid())
    now = ctx.clock.timestamp()
    claims = {
        "iss": "https://identity.xero.com",
        "aud": "https://identity.xero.com/resources",
        "nbf": now,
        "iat": now,
        "exp": now + ttl,
        "jti": ctx.ids.guid(),
        "client_id": client,
        "sub": guid("user", user) if user else client,
        "scope": scopes,
        "authentication_event_id": metadata["authentication_event_id"],
    }
    if user:
        claims["xero_userid"] = guid("user", user)
    else:
        claims["xero_tenant_id"] = main_tenant(ctx)
    token = ctx.signing_key.sign(claims)
    grant = ctx.tokens.issue_access(
        client_id=client,
        user_id=user,
        scopes=scopes,
        ttl=timedelta(seconds=ttl if ttl else -1),
        app_only=app_only,
        token=token,
        extra=metadata,
    )
    if new_connection:
        connect(ctx, grant)
    result: dict[str, Any] = {
        "access_token": token,
        "expires_in": ttl,
        "token_type": "Bearer",
        "scope": " ".join(scopes),
    }
    if refresh and not app_only and "offline_access" in scopes:
        result["refresh_token"] = ctx.tokens.issue_refresh(
            client_id=client, user_id=user, scopes=scopes, ttl=timedelta(days=60), extra=metadata
        ).token
    if not app_only and "openid" in scopes:
        person = ctx.world.person(user)
        identity = {
            **claims,
            "aud": client,
            "sub": guid("user", user),
            "name": person.display_name,
            "given_name": person.first_name,
            "family_name": person.last_name,
            "email": person.email,
            "email_verified": True,
        }
        if metadata.get("nonce"):
            identity["nonce"] = metadata["nonce"]
        result["id_token"] = ctx.signing_key.sign(identity)
    return result


def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Mint an RS256 access token, optionally restricted to explicit scopes."""
    value = payload.get("user")
    scopes = payload.get("scopes", FULL_SCOPES)
    client_id = payload.get("client_id", "emulator-client")
    if value is not None and not isinstance(value, str):
        validation("user must be an employee ID or email string.")
    if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
        validation("scopes must be an array of strings.")
    if not isinstance(client_id, str) or not client_id:
        validation("client_id must be a non-empty string.")
    if "app_only" in payload and not isinstance(payload["app_only"], bool):
        validation("app_only must be a boolean.")
    try:
        if isinstance(payload.get("expires_in"), bool):
            raise ValueError
        ttl = int(payload.get("expires_in", 1800))
        ctx.clock.now() + timedelta(seconds=ttl)
    except (ValueError, TypeError, OverflowError):
        validation("expires_in must be an integer within the supported date range.")
    user = None
    if value:
        person = next((p for p in ctx.world.people if value in {p.id, p.email, guid("user", p.id)}), None)
        if person is None or person.is_external or not person.active:
            raise EmulatorError(403, problem(403))
        user = person.id
    app_only = bool(payload.get("app_only", user is None))
    if app_only:
        user = None
    elif user is None:
        raise EmulatorError(403, problem(403))
    return issue(
        ctx,
        client=client_id,
        user=user,
        scopes=scopes,
        app_only=app_only,
        ttl=ttl,
        new_connection=True,
        refresh=False,
    )


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/identity/connect/authorize")
    async def authorize(request: Request) -> Response:
        q = request.query_params
        client_id, redirect = q.get("client_id"), q.get("redirect_uri")
        if not client_id or not redirect or q.get("response_type") != "code":
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            client = ctx.tokens.authenticate_client(client_id, None, require_secret=False)
        except OAuthError as exc:
            return JSONResponse({"error": exc.error}, status_code=exc.status)
        if client["redirect_uris"] and redirect not in client["redirect_uris"]:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Invalid redirect_uri"}, status_code=400
            )
        if q.get("code_challenge_method", "S256") != "S256" or (
            q.get("code_challenge_method") and not q.get("code_challenge")
        ):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        scopes = q.get("scope", "").split()
        hint = q.get("login_hint")
        if not hint:
            return HTMLResponse(
                render_account_picker(
                    provider_title="Xero",
                    app_name=client["name"],
                    scopes=scopes,
                    accounts=[
                        {"display_name": p.display_name, "email": p.email, "login_hint": p.email}
                        for p in ctx.world.active_employees
                    ],
                    action_base=str(request.url),
                )
            )
        person = next((p for p in ctx.world.active_employees if hint in {p.id, p.email}), None)
        if person is None:
            return RedirectResponse(
                redirect_with_params(redirect, {"error": "access_denied", "state": q.get("state")}),
                status_code=302,
            )
        grant = ctx.tokens.issue_code(
            client_id=client_id,
            user_id=person.id,
            scopes=scopes,
            redirect_uri=redirect,
            code_challenge=q.get("code_challenge"),
            code_challenge_method="S256" if q.get("code_challenge") else None,
            nonce=q.get("nonce"),
        )
        return RedirectResponse(
            redirect_with_params(redirect, {"code": grant.token, "state": q.get("state")}), status_code=302
        )

    @app.post("/connect/token")
    async def token(request: Request) -> Response:
        try:
            form = await parameters(request)
            client_id, secret = basic_credentials(request) or (
                form.get("client_id"),
                form.get("client_secret"),
            )
            kind = form.get("grant_type")
            code = ctx.tokens.resolve(str(form.get("code", "")), "code")
            refresh_source = ctx.tokens.resolve(str(form.get("refresh_token", "")), "refresh")
            public_client = (
                kind == "authorization_code" and code is not None and code.code_challenge is not None
            ) or (kind == "refresh_token" and refresh_source is not None and refresh_source.extra.get("pkce"))
            ctx.tokens.authenticate_client(
                client_id,
                secret,
                require_secret=not public_client,
            )
            if kind == "authorization_code":
                if code and form.get("redirect_uri") != code.redirect_uri:
                    raise OAuthError("invalid_grant", "redirect_uri does not match.")
                source = ctx.tokens.redeem_code(
                    form.get("code"),
                    client_id=client_id,
                    redirect_uri=form.get("redirect_uri"),
                    code_verifier=form.get("code_verifier"),
                )
                result = issue(
                    ctx,
                    client=source.client_id,
                    user=source.user_id,
                    scopes=source.scopes,
                    app_only=False,
                    extra={"nonce": source.nonce, "pkce": source.code_challenge is not None},
                    new_connection=True,
                )
            elif kind == "refresh_token":
                source = refresh_source
                if source is None or source.client_id != client_id:
                    raise OAuthError("invalid_grant", "Invalid refresh token.")
                record = ctx.store.collection("_grants").get(source.token)
                grace = source.extra.get("rotated_at")
                if grace is not None and ctx.clock.timestamp() - grace >= 1800:
                    raise OAuthError("invalid_grant", "The refresh token grace period has expired.")
                if grace is None:
                    record["extra"]["rotated_at"] = ctx.clock.timestamp()
                extra = {k: v for k, v in source.extra.items() if k != "rotated_at"}
                result = issue(
                    ctx,
                    client=source.client_id,
                    user=source.user_id,
                    scopes=source.scopes,
                    app_only=False,
                    extra=extra,
                )
            elif kind == "client_credentials":
                result = issue(
                    ctx,
                    client=str(client_id),
                    user=None,
                    scopes=str(form.get("scope", "")).split(),
                    app_only=True,
                    new_connection=True,
                )
            else:
                raise OAuthError("unsupported_grant_type")
        except OAuthError as exc:
            return JSONResponse(
                {"error": exc.error, "error_description": exc.description},
                status_code=exc.status,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.post("/connect/revocation")
    async def revoke(request: Request) -> Response:
        try:
            form = await parameters(request)
            client_id, secret = basic_credentials(request) or (
                form.get("client_id"),
                form.get("client_secret"),
            )
            ctx.tokens.authenticate_client(client_id, secret)
        except OAuthError as exc:
            return JSONResponse({"error": exc.error}, status_code=exc.status)
        grant = ctx.tokens.get(str(form.get("token", "")))
        if grant and grant.client_id == client_id:
            for record in ctx.store.collection("_grants"):
                if record.get("extra", {}).get("family") == grant.extra.get("family"):
                    record["revoked"] = True
            connections = ctx.store.collection("_connections")
            for record in connections.values():
                if record["_client"] == client_id and record["_user"] == grant.user_id:
                    connections.delete(record["id"])
        return Response(status_code=200)

    @app.get("/.well-known/openid-configuration")
    async def discovery(request: Request) -> dict[str, Any]:
        return {
            "issuer": "https://identity.xero.com",
            "authorization_endpoint": url_for_host(
                request, ctx, "login.xero.com", "/identity/connect/authorize"
            ),
            "token_endpoint": url_for_host(request, ctx, "identity.xero.com", "/connect/token"),
            "revocation_endpoint": url_for_host(request, ctx, "identity.xero.com", "/connect/revocation"),
            "jwks_uri": url_for_host(
                request, ctx, "identity.xero.com", "/.well-known/openid-configuration/jwks"
            ),
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "scopes_supported": FULL_SCOPES,
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
        }

    @app.get("/.well-known/openid-configuration/jwks")
    async def jwks() -> dict[str, Any]:
        return ctx.signing_key.jwks()

    @app.get("/connections")
    async def connections(request: Request) -> list[dict[str, Any]]:
        grant = actor(ctx, request)
        result = ctx.store.collection("_connections").filter(
            lambda c: (
                c["_client"] == grant.client_id
                and c["_user"] == grant.user_id
                and (
                    not request.query_params.get("authEventId")
                    or c["authEventId"] == request.query_params["authEventId"]
                )
            )
        )
        return public(result)

    @app.delete("/connections/{connection_id}")
    async def disconnect(request: Request, connection_id: str) -> Response:
        grant = actor(ctx, request)
        record = ctx.store.collection("_connections").get(connection_id)
        if not record or record["_client"] != grant.client_id or record["_user"] != grant.user_id:
            missing()
        ctx.store.collection("_connections").delete(connection_id)
        return Response(status_code=204)
