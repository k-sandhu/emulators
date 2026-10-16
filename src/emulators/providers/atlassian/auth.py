"""Atlassian 3LO and site API tokens share the core grant registry."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import (
    Grant,
    OAuthError,
    basic_credentials,
    bearer_token,
    redirect_with_params,
    render_account_picker,
)
from emulators.core.provider import ProviderContext

from .common import API_TOKEN, CLOUD_ID, DEFAULT_SCOPES, Record, fail, person_record, site_url


def issue_access(
    ctx: ProviderContext,
    *,
    user_id: str | None,
    scopes: list[str],
    client_id: str,
    expires_in: int = 3600,
    app_only: bool = False,
) -> Grant:
    now = ctx.clock.timestamp()
    token = ctx.signing_key.sign(
        {
            "iss": "https://auth.atlassian.com",
            "aud": "api.atlassian.com",
            "sub": user_id or client_id,
            "iat": now,
            "exp": now + expires_in,
            "scope": " ".join(scopes),
            "jti": ctx.ids.token(n=24),
        }
    )
    grant = ctx.tokens.issue_access(
        client_id=client_id,
        user_id=user_id,
        scopes=scopes,
        token=token,
        ttl=timedelta(seconds=expires_in),
        app_only=app_only,
    )
    # The shared token service treats a zero timedelta as an unlimited lifetime.
    if expires_in == 0:
        grant.expires_at = ctx.clock.now()
        ctx.store["_grants"].put(grant.to_record())
    return grant


def mint_token(ctx: ProviderContext, payload: Record) -> Record:
    """Mint a registered RS256 access token using world ids, account ids or emails."""
    value = payload.get("user")
    if value is not None and (not isinstance(value, str) or not value):
        raise OAuthError("invalid_request", "user must be a non-empty string.")
    user = person_record(ctx, value) if value else None
    if value and not user:
        raise OAuthError("invalid_request", "Unknown user.")
    try:
        expires_value = payload.get("expires_in", 3600)
        if isinstance(expires_value, (bool, float, dict, list)):
            raise ValueError
        expires = int(expires_value)
        if not -2147483648 <= expires <= 2147483647:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise OAuthError("invalid_request", "expires_in must be an integer.") from None
    scopes = payload.get("scopes", DEFAULT_SCOPES)
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
        raise OAuthError("invalid_request", "scopes must be a list of strings.")
    app_only = payload.get("app_only", user is None)
    if not isinstance(app_only, bool):
        raise OAuthError("invalid_request", "app_only must be a boolean.")
    if not isinstance(payload.get("client_id", "emulator-client"), str) or not payload.get(
        "client_id", "emulator-client"
    ):
        raise OAuthError("invalid_request", "client_id must be a string.")
    if not app_only and not user:
        raise OAuthError("invalid_request", "Delegated tokens require a user.")
    grant = issue_access(
        ctx,
        user_id=user["id"] if user and not app_only else None,
        scopes=scopes,
        client_id=payload.get("client_id", "emulator-client"),
        expires_in=expires,
        app_only=app_only,
    )
    return {
        "access_token": grant.token,
        "token_type": "Bearer",
        "expires_in": expires,
        "scope": " ".join(scopes),
    }


def authenticate(request: Request, ctx: ProviderContext, *, scopes: tuple[str, ...] = ()) -> Grant:
    cloud_id = request.path_params.get("cloud_id")
    if cloud_id and cloud_id != CLOUD_ID:
        fail(request, 404, "Cloud resource not found.")
    basic = basic_credentials(request)
    grant = None
    if basic and "/ex/" not in request.url.path and request.url.hostname != "api.atlassian.com":
        user = person_record(ctx, basic[0])
        if (
            user
            and user["email"].casefold() == basic[0].casefold()
            and secrets.compare_digest(basic[1].encode(), API_TOKEN.encode())
        ):
            grant = Grant("", "access", "api-token", user["id"], DEFAULT_SCOPES, ctx.clock.now(), None)
    else:
        grant = ctx.tokens.resolve(bearer_token(request))
    if grant is None:
        fail(request, 401, "Authentication required. Invalid or expired credentials.")
    if grant.user_id:
        user = person_record(ctx, grant.user_id)
        if not user or user["accountStatus"] != "active":
            fail(request, 401, "Account is not active.")
    if scopes and not grant.has_scope(*scopes):
        fail(request, 403, "The token does not have the required scopes.")
    request.state.principal = grant.user_id or grant.client_id
    return grant


def confluence_auth(request: Request, ctx: ProviderContext, resource: str = "page") -> Grant:
    path = request.url.path
    if resource == "page":
        if path.endswith("/ancestors"):
            resource = "metadata"
        elif path.endswith("/footer-comments"):
            resource = "comment"
        elif path.endswith("/direct-children"):
            resource = "hierarchy"
        elif "/restriction/" in path:
            resource = "restriction"
    if request.method in ("POST", "PUT", "DELETE") and resource != "user":
        properties = "properties" in path.split("/")
        granular = (
            "write:space:confluence"
            if resource == "space"
            else (
                "delete:page:confluence"
                if request.method == "DELETE" and not properties
                else "write:page:confluence"
            )
        )
        grant = authenticate(request, ctx, scopes=("write:confluence-content", granular))
        if properties and not grant.has_scope("write:confluence-content"):
            required_read = "read:space:confluence" if resource == "space" else "read:page:confluence"
            if not grant.has_scope(required_read):
                fail(request, 403, "The token does not have the required read and write scopes.")
        return grant
    classic = {
        "space": "read:confluence-space.summary",
        "user": "read:confluence-user",
        "search": "search:confluence",
    }.get(resource, "read:confluence-content.all")
    granular = {
        "space": "read:space:confluence",
        "user": "read:user:confluence",
        "attachment": "read:attachment:confluence",
        "hierarchy": "read:hierarchical-content:confluence",
        "metadata": "read:content.metadata:confluence",
        "comment": "read:comment:confluence",
        "restriction": "read:content-details:confluence",
        "search": "read:content-details:confluence",
    }.get(resource, "read:page:confluence")
    return authenticate(request, ctx, scopes=(classic, granular))


def jira_auth(request: Request, ctx: ProviderContext, *, users: bool = False) -> Grant:
    scope = "read:jira-user" if users else "read:jira-work"
    if request.method in ("POST", "PUT", "DELETE") and not request.url.path.endswith("/search/jql"):
        scope = "write:jira-work"
    return authenticate(request, ctx, scopes=(scope,))


def build_auth(app: FastAPI, ctx: ProviderContext) -> None:
    @app.exception_handler(OAuthError)
    async def oauth_error(_: Request, exc: OAuthError) -> JSONResponse:
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description}, status_code=exc.status
        )

    @app.get("/authorize")
    async def authorize(request: Request) -> Response:
        q = request.query_params
        client = ctx.tokens.authenticate_client(q.get("client_id"), None, require_secret=False)
        redirect_uri = q.get("redirect_uri")
        if not redirect_uri or q.get("response_type") != "code" or q.get("audience") != "api.atlassian.com":
            raise OAuthError("invalid_request", "audience, redirect_uri and response_type=code are required.")
        if client["redirect_uris"] and redirect_uri not in client["redirect_uris"]:
            raise OAuthError("invalid_request", "The redirect URI is not registered.")
        if q.get("code_challenge_method") not in (None, "plain", "S256") or (
            "code_challenge_method" in q and not q.get("code_challenge")
        ):
            raise OAuthError("invalid_request", "Invalid PKCE challenge or method.")
        scopes = q.get("scope", "").split()
        hint = q.get("login_hint")
        if not hint:
            accounts = [
                {"display_name": u["displayName"], "email": u["email"], "login_hint": u["email"]}
                for u in ctx.store["users"]
                if u["accountStatus"] == "active"
            ]
            return HTMLResponse(
                render_account_picker(
                    provider_title="Atlassian",
                    app_name=client["name"],
                    scopes=scopes,
                    accounts=accounts,
                    action_base=str(request.url),
                )
            )
        user = person_record(ctx, hint)
        if not user or user["accountStatus"] != "active":
            raise OAuthError("access_denied", "Unknown or inactive login_hint.")
        code = ctx.tokens.issue_code(
            client_id=client["id"],
            user_id=user["id"],
            scopes=scopes,
            redirect_uri=redirect_uri,
            code_challenge=q.get("code_challenge"),
            code_challenge_method=q.get("code_challenge_method"),
        )
        return RedirectResponse(
            redirect_with_params(redirect_uri, {"code": code.token, "state": q.get("state")}), status_code=302
        )

    @app.post("/oauth/token")
    async def token(payload: dict[str, Any]) -> Record:
        for key in (
            "client_id",
            "client_secret",
            "grant_type",
            "code",
            "redirect_uri",
            "code_verifier",
            "refresh_token",
        ):
            if key in payload and (not isinstance(payload[key], str) or not payload[key]):
                raise OAuthError("invalid_request", f"{key} must be a non-empty string.")
        client = ctx.tokens.authenticate_client(payload.get("client_id"), payload.get("client_secret"))
        grant_type = payload.get("grant_type")
        with ctx.store.lock:
            if grant_type == "authorization_code":
                code = ctx.tokens.resolve(payload.get("code"), "code")
                if code and code.redirect_uri != payload.get("redirect_uri"):
                    raise OAuthError("invalid_grant", "redirect_uri must match the authorization request.")
                original = ctx.tokens.redeem_code(
                    payload.get("code"),
                    client_id=client["id"],
                    redirect_uri=payload.get("redirect_uri"),
                    code_verifier=payload.get("code_verifier"),
                )
            elif grant_type == "refresh_token":
                original = ctx.tokens.get(payload.get("refresh_token"), "refresh")
                if (
                    not original
                    or original.client_id != client["id"]
                    or (original.expires_at is not None and original.expires_at <= ctx.clock.now())
                ):
                    raise OAuthError("invalid_grant", "Invalid refresh token.")
                rotated_at = original.extra.get("rotated_at")
                if original.revoked and (rotated_at is None or ctx.clock.timestamp() - rotated_at > 600):
                    # Reuse after the leeway revokes the entire refresh-token family.
                    family_id = original.extra.get("family", original.token)
                    for record in ctx.store["_grants"]:
                        if record.get("extra", {}).get("family") == family_id:
                            record["revoked"] = True
                    raise OAuthError("invalid_grant", "Invalid refresh token.")
                if not original.revoked:
                    record = ctx.store["_grants"].get(original.token)
                    record["extra"]["rotated_at"] = ctx.clock.timestamp()
                ctx.tokens.revoke(original.token)
            else:
                raise OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token.")
            user = person_record(ctx, original.user_id)
            if not user or user["accountStatus"] != "active":
                raise OAuthError("invalid_grant", "Account is not active.")
            grant = issue_access(
                ctx, user_id=original.user_id, scopes=original.scopes, client_id=client["id"]
            )
            result: Record = {
                "access_token": grant.token,
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": " ".join(grant.scopes),
            }
            if "offline_access" in grant.scopes:
                refresh = ctx.tokens.issue_refresh(
                    client_id=client["id"],
                    user_id=grant.user_id,
                    scopes=grant.scopes,
                    extra={"family": original.extra.get("family", original.token)},
                )
                result["refresh_token"] = refresh.token
            return result

    @app.get("/oauth/token/accessible-resources")
    async def resources(request: Request) -> list[Record]:
        grant = authenticate(request, ctx)
        return [
            {
                "id": CLOUD_ID,
                "url": site_url(request, ctx),
                "name": ctx.world.org.name,
                "scopes": grant.scopes,
                "avatarUrl": site_url(request, ctx, "/wiki/images/logo/default-space-logo.svg"),
            }
        ]

    @app.get("/me")
    async def me(request: Request) -> Record:
        grant = authenticate(request, ctx, scopes=("read:me",))
        user = person_record(ctx, grant.user_id)
        if grant.app_only or not user:
            fail(request, 403, "This resource requires a user grant.")
        return {
            "account_id": user["id"],
            "account_type": "atlassian",
            "email": user["email"],
            "name": user["displayName"],
            "picture": site_url(request, ctx, "/wiki/aa-avatar/" + user["id"]),
            "locale": "en-US",
            "zoneinfo": user["timeZone"],
            "email_verified": True,
        }
