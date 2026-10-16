"""OAuth 2.0 / OIDC endpoints: authorize, token (5 grants), revoke, introspect, userinfo, identity, JWKS."""

from __future__ import annotations

import base64
import hashlib
import html
import re
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import (
    OAuthError,
    basic_credentials,
    bearer_token,
    redirect_with_params,
    render_account_picker,
)
from emulators.core.clock import utc
from emulators.core.provider import ProviderContext, external_base, url_for_host
from emulators.core.webhooks import hmac_sha256_b64
from emulators.providers.salesforce import auth as sfauth
from emulators.providers.salesforce.common import INSTANCE_HOST, LOGIN_HOST, SfError, invalid_session
from emulators.providers.salesforce.org import Org, Record

SCOPES = [
    "id",
    "api",
    "web",
    "full",
    "refresh_token",
    "offline_access",
    "openid",
    "profile",
    "email",
    "address",
    "phone",
    "custom_permissions",
    "wave_api",
    "eclair_api",
    "chatter_api",
    "lightning",
    "content",
    "visualforce",
]
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def instance_url(request: Request, ctx: ProviderContext) -> str:
    return url_for_host(request, ctx, INSTANCE_HOST, "").rstrip("/")


def identity_url(request: Request, ctx: ProviderContext, org: Org, user: Record) -> str:
    return url_for_host(request, ctx, LOGIN_HOST, f"/id/{org.org_id18}/{user['Id']}")


def oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


def _form(request_body: bytes) -> dict[str, str]:
    return {
        k: v[0] for k, v in parse_qs(request_body.decode("utf-8", "replace"), keep_blank_values=True).items()
    }


def _scope_list(raw: str | None) -> list[str]:
    return [s for s in (raw or "").replace("+", " ").split() if s]


def id_token_for(
    ctx: ProviderContext,
    org: Org,
    request: Request,
    user: Record,
    client_id: str,
    access_token: str,
    nonce: str | None,
) -> str:
    now = ctx.clock.timestamp()
    digest = hashlib.sha256(access_token.encode()).digest()[:16]
    claims: dict[str, Any] = {
        "at_hash": base64.urlsafe_b64encode(digest).decode().rstrip("="),
        "sub": identity_url(request, ctx, org, user),
        "aud": client_id,
        "iss": f"https://{LOGIN_HOST}",
        "exp": now + 180,
        "iat": now,
        "email": user.get("Email"),
        "email_verified": True,
        "name": user.get("Name"),
        "preferred_username": user.get("Username"),
        "given_name": user.get("FirstName"),
        "family_name": user.get("LastName"),
        "zoneinfo": user.get("TimeZoneSidKey"),
        "organization_id": org.org_id18,
        "user_id": user["Id"],
    }
    if nonce:
        claims["nonce"] = nonce
    return ctx.signing_key.sign(claims)


def token_body(
    ctx: ProviderContext,
    org: Org,
    request: Request,
    user: Record,
    grant: Any,
    *,
    client_id: str,
    client_secret: str | None,
    scopes: list[str],
    refresh: str | None = None,
    nonce: str | None = None,
    style: str = "full",
) -> dict[str, Any]:
    issued_at = str(int(ctx.clock.now().timestamp() * 1000))
    id_url = identity_url(request, ctx, org, user)
    scope_text = " ".join(scopes)
    if style == "jwt":
        return {
            "access_token": grant.token,
            "scope": scope_text,
            "instance_url": instance_url(request, ctx),
            "id": id_url,
            "token_type": "Bearer",
        }
    body: dict[str, Any] = {"access_token": grant.token}
    if refresh:
        body["refresh_token"] = refresh
    body["signature"] = hmac_sha256_b64(client_secret or "emulator-client-secret", id_url + issued_at)
    body["scope"] = scope_text
    if "openid" in scopes:
        body["id_token"] = id_token_for(ctx, org, request, user, client_id, grant.token, nonce)
    body["instance_url"] = instance_url(request, ctx)
    body["id"] = id_url
    body["token_type"] = "Bearer"
    body["issued_at"] = issued_at
    return body


def effective_scopes(requested: list[str]) -> tuple[list[str], bool]:
    """(scopes to grant, issue a refresh token?) — a connected app without explicit scopes gets api + refresh."""
    if not requested:
        return ["refresh_token", "api"], True
    wants_refresh = bool({"refresh_token", "offline_access"} & set(requested))
    ordered = [s for s in requested if s not in ("offline_access", "refresh_token")]
    if wants_refresh:
        ordered.insert(0, "refresh_token")
    return ordered, wants_refresh


def register(app: FastAPI, ctx: ProviderContext, org: Org) -> None:
    # -- discovery ------------------------------------------------------------------
    @app.get("/.well-known/openid-configuration", include_in_schema=False)
    @app.get("/services/.well-known/openid-configuration", include_in_schema=False)
    async def discovery(request: Request) -> dict[str, Any]:
        base = url_for_host(request, ctx, LOGIN_HOST, "").rstrip("/")
        return {
            "issuer": f"https://{LOGIN_HOST}",
            "authorization_endpoint": f"{base}/services/oauth2/authorize",
            "token_endpoint": f"{base}/services/oauth2/token",
            "revocation_endpoint": f"{base}/services/oauth2/revoke",
            "userinfo_endpoint": f"{base}/services/oauth2/userinfo",
            "jwks_uri": f"{base}/id/keys",
            "registration_endpoint": f"{base}/services/oauth2/register",
            "introspection_endpoint": f"{base}/services/oauth2/introspect",
            "scopes_supported": SCOPES,
            "response_types_supported": ["code", "token", "token id_token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
                "private_key_jwt",
            ],
            "claims_supported": [
                "active",
                "address",
                "email",
                "email_verified",
                "family_name",
                "given_name",
                "is_app_installed",
                "language",
                "locale",
                "name",
                "nickname",
                "organization_id",
                "phone_number",
                "phone_number_verified",
                "photos",
                "picture",
                "preferred_username",
                "profile",
                "sub",
                "updated_at",
                "urls",
                "user_id",
                "user_type",
                "zoneinfo",
            ],
            "grant_types_supported": [
                "authorization_code",
                "implicit",
                "refresh_token",
                "password",
                "client_credentials",
                JWT_BEARER,
            ],
            "code_challenge_methods_supported": ["S256"],
        }

    @app.get("/id/keys", include_in_schema=False)
    @app.get("/services/oauth2/jwks", include_in_schema=False)
    async def jwks() -> dict[str, Any]:
        return ctx.signing_key.jwks()

    # -- authorize ------------------------------------------------------------------
    @app.get("/services/oauth2/authorize", include_in_schema=False)
    async def authorize(request: Request) -> Response:
        q = dict(request.query_params)
        redirect_uri = q.get("redirect_uri")
        client_id = q.get("client_id")
        response_type = q.get("response_type", "code")
        if not client_id:
            return oauth_error("invalid_request", "missing required parameter: client_id")
        if not redirect_uri:
            return oauth_error("invalid_request", "missing required parameter: redirect_uri")
        if response_type not in ("code", "token", "token id_token"):
            return _redirect_error(redirect_uri, q, "unsupported_response_type", "invalid response type")
        challenge, method = q.get("code_challenge"), q.get("code_challenge_method")
        if method and not challenge:
            return _redirect_error(redirect_uri, q, "invalid_request", "code_challenge required with method")
        if challenge:
            if method and method != "S256":
                return _redirect_error(
                    redirect_uri, q, "invalid_request", "code_challenge_method must be S256"
                )
            if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", challenge):
                return _redirect_error(redirect_uri, q, "invalid_request", "invalid code_challenge")
        scopes_requested = _scope_list(q.get("scope"))
        hint = q.get("login_hint")
        if not hint:
            accounts = [
                {"display_name": u["Name"], "email": u["Email"], "login_hint": u["Username"]}
                for u in org.coll("User")
                if u.get("IsActive") and u.get("_person_id") != "integration"
            ]
            return HTMLResponse(
                render_account_picker(
                    provider_title="Salesforce",
                    app_name=client_id,
                    scopes=scopes_requested,
                    accounts=accounts,
                    action_base=external_base(request) + "/services/oauth2/authorize?" + urlencode(q),
                )
            )
        user = sfauth.user_by_login(org, hint)
        if user is None or not user.get("IsActive"):
            return _redirect_error(redirect_uri, q, "access_denied", "end-user denied authorization")
        scopes, wants_refresh = effective_scopes(scopes_requested)
        if (
            response_type != "code"
        ):  # the user-agent (implicit) flow only returns a refresh token when asked for
            wants_refresh = bool({"refresh_token", "offline_access"} & set(scopes_requested))
            if not wants_refresh:
                scopes = [s for s in scopes if s != "refresh_token"]
        ctx.tokens.authenticate_client(client_id, None, require_secret=False)
        if response_type == "code":
            code = ctx.tokens.issue(
                "code",
                client_id=client_id,
                user_id=user["Id"],
                scopes=scopes,
                ttl=timedelta(minutes=10),
                token=sfauth.new_code(),
                redirect_uri=redirect_uri,
                code_challenge=challenge,
                code_challenge_method="S256" if challenge else None,
                nonce=q.get("nonce"),
                extra={"refresh": wants_refresh},
            )
            return RedirectResponse(
                redirect_with_params(redirect_uri, {"code": code.token, "state": q.get("state")}),
                status_code=302,
            )
        refresh = None
        if wants_refresh:
            refresh = ctx.tokens.issue_refresh(
                token=sfauth.new_refresh_token(),
                client_id=client_id,
                user_id=user["Id"],
                scopes=scopes,
                ttl=None,
            ).token
        grant = sfauth.issue_session(ctx, org, user, client_id=client_id, scopes=scopes, refresh=refresh)
        body = token_body(
            ctx,
            org,
            request,
            user,
            grant,
            client_id=client_id,
            client_secret=None,
            scopes=scopes,
            refresh=refresh,
            nonce=q.get("nonce"),
        )
        body["state"] = q.get("state")
        return RedirectResponse(redirect_with_params(redirect_uri, body, fragment=True), status_code=302)

    def _redirect_error(redirect_uri: str, q: dict[str, str], error: str, description: str) -> Response:
        return RedirectResponse(
            redirect_with_params(
                redirect_uri, {"error": error, "error_description": description, "state": q.get("state")}
            ),
            status_code=302,
        )

    # -- token ------------------------------------------------------------------------
    @app.post("/services/oauth2/token", include_in_schema=False)
    async def token(request: Request) -> Response:
        form = _form(await request.body())
        basic = basic_credentials(request)
        client_id = form.get("client_id") or (basic[0] if basic else None)
        client_secret = form.get("client_secret") or (basic[1] if basic else None)
        grant_type = form.get("grant_type")
        if not grant_type:
            return oauth_error("invalid_request", "grant type not supported")
        try:
            if grant_type == JWT_BEARER:
                return _jwt_bearer(request, form)
            if not client_id:
                return oauth_error("invalid_client_id", "client identifier invalid")
            try:
                ctx.tokens.authenticate_client(client_id, client_secret)
            except OAuthError:
                return oauth_error("invalid_client", "invalid client credentials")
            if grant_type == "authorization_code":
                return _authorization_code(request, form, client_id, client_secret)
            if grant_type == "refresh_token":
                return _refresh(request, form, client_id, client_secret)
            if grant_type == "password":
                return _password(request, form, client_id, client_secret)
            if grant_type == "client_credentials":
                return _client_credentials(request, form, client_id, client_secret)
        except OAuthError as exc:
            return oauth_error(exc.error, exc.description, exc.status)
        return oauth_error("unsupported_grant_type", "grant type not supported")

    def _authorization_code(
        request: Request, form: dict[str, str], client_id: str, secret: str | None
    ) -> Response:
        try:
            grant = ctx.tokens.redeem_code(
                form.get("code"),
                client_id=client_id,
                redirect_uri=form.get("redirect_uri"),
                code_verifier=form.get("code_verifier"),
            )
        except OAuthError as exc:  # translate the shared messages into Salesforce's wording
            text = exc.description.lower()
            if "redirect_uri" in text:
                raise OAuthError("invalid_grant", "redirect_uri must match configuration") from exc
            if "pkce" in text:
                raise OAuthError("invalid_grant", "invalid code verifier") from exc
            if "different client" in text:
                raise OAuthError("invalid_grant", "authentication failure") from exc
            raise OAuthError("invalid_grant", "expired authorization code") from exc
        user = org.find_user(grant.user_id)
        if user is None or not user.get("IsActive"):
            raise OAuthError("invalid_grant", "inactive user")
        refresh = None
        if grant.extra.get("refresh"):
            refresh = ctx.tokens.issue_refresh(
                token=sfauth.new_refresh_token(),
                client_id=client_id,
                user_id=user["Id"],
                scopes=grant.scopes,
                ttl=None,
            ).token
        session = sfauth.issue_session(
            ctx, org, user, client_id=client_id, scopes=grant.scopes, refresh=refresh
        )
        return JSONResponse(
            token_body(
                ctx,
                org,
                request,
                user,
                session,
                client_id=client_id,
                client_secret=secret,
                scopes=grant.scopes,
                refresh=refresh,
                nonce=grant.nonce,
            )
        )

    def _refresh(request: Request, form: dict[str, str], client_id: str, secret: str | None) -> Response:
        refresh_grant = ctx.tokens.resolve(form.get("refresh_token"), "refresh")
        if refresh_grant is None:
            raise OAuthError("invalid_grant", "expired access/refresh token")
        user = org.find_user(refresh_grant.user_id)
        if user is None or not user.get("IsActive"):
            raise OAuthError("invalid_grant", "inactive user")
        session = sfauth.issue_session(
            ctx,
            org,
            user,
            client_id=refresh_grant.client_id,
            scopes=refresh_grant.scopes,
            refresh=refresh_grant.token,
        )
        body = token_body(
            ctx,
            org,
            request,
            user,
            session,
            client_id=client_id,
            client_secret=secret,
            scopes=refresh_grant.scopes,
            refresh=None,
        )
        return JSONResponse(body)

    def _password(request: Request, form: dict[str, str], client_id: str, secret: str | None) -> Response:
        user = sfauth.user_by_login(org, form.get("username"))
        if (
            user is None
            or not user.get("IsActive")
            or not sfauth.check_password(org, ctx, user, form.get("password", ""))
        ):
            sfauth.record_failed_login(user)
            raise OAuthError("invalid_grant", "authentication failure")
        scopes = _scope_list(form.get("scope")) or ["api"]
        session = sfauth.issue_session(ctx, org, user, client_id=client_id, scopes=scopes)
        return JSONResponse(
            token_body(
                ctx, org, request, user, session, client_id=client_id, client_secret=secret, scopes=scopes
            )
        )

    def _client_credentials(
        request: Request, form: dict[str, str], client_id: str, secret: str | None
    ) -> Response:
        user = sfauth.integration_user(org)
        scopes = _scope_list(form.get("scope")) or ["api"]
        session = sfauth.issue_session(ctx, org, user, client_id=client_id, scopes=scopes, app_only=True)
        return JSONResponse(
            token_body(
                ctx, org, request, user, session, client_id=client_id, client_secret=secret, scopes=scopes
            )
        )

    def _jwt_bearer(request: Request, form: dict[str, str]) -> Response:
        assertion = form.get("assertion")
        if not assertion:
            raise OAuthError("invalid_request", "assertion is missing")
        try:
            claims = jwt.decode(
                assertion, options={"verify_signature": False, "verify_aud": False, "verify_exp": False}
            )
        except jwt.PyJWTError as exc:
            raise OAuthError("invalid_grant", "invalid assertion") from exc
        client_id, sub, aud = claims.get("iss"), claims.get("sub"), claims.get("aud")
        if not client_id or not sub:
            raise OAuthError("invalid_grant", "invalid assertion")
        audiences = (aud,) if isinstance(aud, str) else tuple(aud or ())
        if not any(str(a).rstrip("/").endswith(".salesforce.com") for a in audiences):
            raise OAuthError("invalid_grant", "audience is invalid")
        exp = claims.get("exp")
        if exp is not None:
            try:
                if int(float(exp)) < ctx.clock.timestamp():
                    raise OAuthError("invalid_grant", "expired authorization code")
            except (TypeError, ValueError) as exc:
                raise OAuthError("invalid_grant", "invalid assertion") from exc
        user = sfauth.user_by_login(org, str(sub))
        if user is None or not user.get("IsActive"):
            raise OAuthError("invalid_grant", "no user found")
        ctx.tokens.authenticate_client(str(client_id), None, require_secret=False)
        scopes = ["api", "web"]
        session = sfauth.issue_session(ctx, org, user, client_id=str(client_id), scopes=scopes)
        return JSONResponse(
            token_body(
                ctx,
                org,
                request,
                user,
                session,
                client_id=str(client_id),
                client_secret=None,
                scopes=scopes,
                style="jwt",
            )
        )

    # -- revoke / introspect -------------------------------------------------------------
    @app.api_route("/services/oauth2/revoke", methods=["GET", "POST"], include_in_schema=False)
    async def revoke(request: Request) -> Response:
        form = {**dict(request.query_params), **_form(await request.body())}
        value = form.get("token")
        if not value:
            return oauth_error("unsupported_token_type", "token type not supported")
        grant = ctx.tokens.get(value)
        if grant is not None:
            ctx.tokens.revoke(value)
            if grant.kind == "refresh":
                for rec in ctx.store.collection("_grants"):
                    if rec.get("extra", {}).get("refresh_token") == value:
                        rec["revoked"] = True
        return Response(status_code=200)

    @app.post("/services/oauth2/introspect", include_in_schema=False)
    async def introspect(request: Request) -> Response:
        form = _form(await request.body())
        grant = ctx.tokens.resolve(form.get("token"), "access") or ctx.tokens.resolve(
            form.get("token"), "refresh"
        )
        if grant is None:
            return JSONResponse({"active": False})
        user = org.find_user(grant.user_id)
        body: dict[str, Any] = {
            "active": True,
            "scope": " ".join(grant.scopes),
            "client_id": grant.client_id,
            "username": user["Username"] if user else None,
            "sub": identity_url(request, ctx, org, user) if user else None,
            "token_type": "access_token" if grant.kind == "access" else "refresh_token",
            "exp": int(utc(grant.expires_at).timestamp()) if grant.expires_at else None,
            "iat": int(utc(grant.issued_at).timestamp()),
            "nbf": int(utc(grant.issued_at).timestamp()),
        }
        return JSONResponse({k: v for k, v in body.items() if v is not None})

    # -- identity -----------------------------------------------------------------------
    def _caller(request: Request) -> tuple[Record, Any]:
        token = (
            bearer_token(request)
            or request.query_params.get("oauth_token")
            or request.query_params.get("access_token")
        )
        grant = ctx.tokens.resolve(token) if token else None
        user = (
            org.find_user(grant.user_id)
            if grant and grant.user_id
            else (sfauth.integration_user(org) if grant else None)
        )
        if grant is None or user is None or not user.get("IsActive"):
            raise invalid_session()
        return user, grant

    def _offset_ms(user: Record) -> int:
        try:
            return int(
                ctx.clock.now().astimezone(ZoneInfo(user["TimeZoneSidKey"])).utcoffset().total_seconds()
                * 1000
            )  # type: ignore[union-attr]
        except Exception:
            return 0

    @app.api_route("/services/oauth2/userinfo", methods=["GET", "POST"], include_in_schema=False)
    async def userinfo(request: Request) -> dict[str, Any]:
        user, _ = _caller(request)
        base = instance_url(request, ctx)
        photo = user.get("FullPhotoUrl")
        return {
            "sub": identity_url(request, ctx, org, user),
            "user_id": user["Id"],
            "organization_id": org.org_id18,
            "preferred_username": user["Username"],
            "nickname": user.get("CommunityNickname"),
            "name": user["Name"],
            "email": user["Email"],
            "email_verified": True,
            "given_name": user.get("FirstName"),
            "family_name": user["LastName"],
            "zoneinfo": user["TimeZoneSidKey"],
            "photos": {"picture": photo, "thumbnail": user.get("SmallPhotoUrl")},
            "profile": f"{base}/{user['Id']}",
            "picture": photo,
            "address": {"country": (user.get("Country") or "")[:2].upper() or None},
            "is_salesforce_integration_user": user.get("_person_id") == "integration",
            "urls": _urls(request, org),
            "active": bool(user.get("IsActive")),
            "user_type": str(user.get("UserType", "Standard")).upper(),
            "language": user.get("LanguageLocaleKey"),
            "locale": user.get("LocaleSidKey"),
            "utcOffset": _offset_ms(user),
            "updated_at": user.get("LastModifiedDate"),
            "is_app_installed": True,
        }

    def _urls(request: Request, org_: Org) -> dict[str, str]:
        base = instance_url(request, ctx)
        oid = org_.org_id18
        return {
            "enterprise": f"{base}/services/Soap/c/{{version}}/{oid}",
            "metadata": f"{base}/services/Soap/m/{{version}}/{oid}",
            "partner": f"{base}/services/Soap/u/{{version}}/{oid}",
            "rest": f"{base}/services/data/v{{version}}/",
            "sobjects": f"{base}/services/data/v{{version}}/sobjects/",
            "search": f"{base}/services/data/v{{version}}/search/",
            "query": f"{base}/services/data/v{{version}}/query/",
            "recent": f"{base}/services/data/v{{version}}/recent/",
            "profile": f"{base}/",
            "feeds": f"{base}/services/data/v{{version}}/chatter/feeds",
            "feed_items": f"{base}/services/data/v{{version}}/chatter/feed-items",
            "feed_elements": f"{base}/services/data/v{{version}}/chatter/feed-elements",
            "groups": f"{base}/services/data/v{{version}}/chatter/groups",
            "users": f"{base}/services/data/v{{version}}/chatter/users",
            "custom_domain": base,
        }

    @app.get("/id/{org_id}/{user_id}", include_in_schema=False)
    async def identity(org_id: str, user_id: str, request: Request) -> dict[str, Any]:
        _caller(request)
        if org_id not in (org.org_id18, org.org_id15):
            raise SfError(404, "NOT_FOUND", "The requested resource does not exist")
        user = org.user(user_id)
        if user is None:
            raise SfError(404, "NOT_FOUND", "The requested resource does not exist")
        base = instance_url(request, ctx)
        return {
            "id": identity_url(request, ctx, org, user),
            "asserted_user": True,
            "user_id": user["Id"],
            "organization_id": org.org_id18,
            "username": user["Username"],
            "nick_name": user.get("CommunityNickname"),
            "display_name": user["Name"],
            "email": user["Email"],
            "email_verified": True,
            "first_name": user.get("FirstName"),
            "last_name": user["LastName"],
            "timezone": user["TimeZoneSidKey"],
            "photos": {"picture": user.get("FullPhotoUrl"), "thumbnail": user.get("SmallPhotoUrl")},
            "addr_street": user.get("Street"),
            "addr_city": user.get("City"),
            "addr_state": user.get("State"),
            "addr_country": (user.get("Country") or "")[:2].upper() or None,
            "addr_zip": user.get("PostalCode"),
            "mobile_phone": user.get("MobilePhone"),
            "mobile_phone_verified": bool(user.get("MobilePhone")),
            "is_lightning_login_user": False,
            "status": {"created_date": None, "body": None},
            "urls": _urls(request, org),
            "active": bool(user.get("IsActive")),
            "user_type": "STANDARD",
            "language": user.get("LanguageLocaleKey"),
            "locale": user.get("LocaleSidKey"),
            "utcOffset": _offset_ms(user),
            "last_modified_date": user.get("LastModifiedDate"),
            "is_app_installed": True,
            "custom_domain": base,
        }

    @app.get("/secur/frontdoor.jsp", include_in_schema=False)
    async def frontdoor(request: Request, sid: str | None = None, retURL: str | None = None) -> Response:
        grant = ctx.tokens.resolve(sid) if sid else None
        if grant is None:
            return HTMLResponse(
                f"<html><body>{html.escape('Session expired or invalid')}</body></html>", status_code=200
            )
        return RedirectResponse(retURL or "/lightning/page/home", status_code=302)
