"""Microsoft identity platform endpoints, backed by the shared grant store."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import OAuthError, basic_credentials, redirect_with_params, render_account_picker
from emulators.core.clock import isoformat_z
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext, external_base
from emulators.providers.microsoft import common as c


def aad(ctx: ProviderContext, code: int, message: str, error: str = "invalid_grant") -> EmulatorError:
    stamp = isoformat_z(ctx.clock.now())
    trace, correlation = str(uuid.uuid4()), str(uuid.uuid4())
    description = (
        f"AADSTS{code}: {message}\r\nTrace ID: {trace}\r\nCorrelation ID: {correlation}\r\nTimestamp: {stamp}"
    )
    return EmulatorError(
        400,
        {
            "error": error,
            "error_description": description,
            "error_codes": [code],
            "timestamp": stamp,
            "trace_id": trace,
            "correlation_id": correlation,
            "error_uri": f"https://login.microsoftonline.com/error?code={code}",
        },
    )


def check_tenant(ctx: ProviderContext, tenant: str) -> None:
    if tenant.lower() not in {
        c.tenant_id(ctx.world),
        "common",
        "organizations",
        ctx.world.org.domain,
        f"{ctx.world.org.slug}.onmicrosoft.com",
    }:
        raise aad(ctx, 90002, f"Tenant '{tenant}' not found.", "invalid_request")


def resolve_user(ctx: ProviderContext, value: str | None) -> Any:
    person = next(
        (
            p
            for p in ctx.world.people
            if value
            and value.casefold()
            in {
                p.id.casefold(),
                p.email.casefold(),
                c.user_id(ctx.world, p.id).casefold(),
                c.upn(ctx.world, p).casefold(),
            }
        ),
        None,
    )
    if person is None:
        raise aad(ctx, 50034, "The user account does not exist in the directory.")
    record = ctx.store["users"].get(c.user_id(ctx.world, person.id))
    if not person.active or (record and not record.get("accountEnabled", True)):
        raise aad(ctx, 50057, "User account is disabled.")
    return person


def id_token(
    ctx: ProviderContext,
    person: Any,
    client_id: str,
    nonce: str | None = None,
    access_token: str | None = None,
    code: str | None = None,
) -> str:
    timestamp = ctx.clock.timestamp()
    claims = {
        "aud": client_id,
        "iss": f"https://login.microsoftonline.com/{c.tenant_id(ctx.world)}/v2.0",
        "iat": timestamp,
        "nbf": timestamp,
        "exp": timestamp + 3600,
        "tid": c.tenant_id(ctx.world),
        "oid": c.user_id(ctx.world, person.id),
        "sub": c.object_id(ctx.world, person.id, client_id),
        "preferred_username": person.email,
        "email": person.email,
        "name": person.display_name,
        "given_name": person.first_name,
        "family_name": person.last_name,
        "ver": "2.0",
    }
    if nonce is not None:
        claims["nonce"] = nonce
    for key, value in (("at_hash", access_token), ("c_hash", code)):
        if value:
            claims[key] = (
                base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()[:16]).decode().rstrip("=")
            )
    return ctx.signing_key.sign(claims)


def issue_tokens(
    ctx: ProviderContext,
    *,
    user: str | None,
    client_id: str,
    scopes: list[str],
    app_only: bool = False,
    expires_in: int = 3600,
    nonce: str | None = None,
    audience: str = "https://graph.microsoft.com",
    include_client_info: bool = False,
) -> dict[str, Any]:
    person = resolve_user(ctx, user) if not app_only else None
    timestamp = ctx.clock.timestamp()
    oid = (
        c.user_id(ctx.world, person.id) if person else c.object_id(ctx.world, "service-principal", client_id)
    )
    claims: dict[str, Any] = {
        "aud": audience,
        "iss": f"https://sts.windows.net/{c.tenant_id(ctx.world)}/",
        "iat": timestamp,
        "nbf": timestamp,
        "exp": timestamp + expires_in,
        "tid": c.tenant_id(ctx.world),
        "oid": oid,
        "sub": oid,
        "appid": client_id,
        "azp": client_id,
        "ver": "1.0",
        "uti": secrets.token_urlsafe(16),
    }
    permission_scopes = [
        s.removeprefix("https://graph.microsoft.com/")
        for s in scopes
        if s not in ("openid", "profile", "offline_access", "email")
    ]
    if app_only:
        claims["roles"] = [s for s in permission_scopes if s != ".default"] or [
            "User.Read.All",
            "Group.Read.All",
            "Mail.ReadWrite",
            "Mail.Send",
            "Calendars.ReadWrite",
        ]
        claims["idtyp"] = "app"
    else:
        claims.update(
            {
                "scp": " ".join(permission_scopes),
                "upn": person.email,
                "unique_name": person.email,
                "preferred_username": person.email,
                "name": person.display_name,
                "given_name": person.first_name,
                "family_name": person.last_name,
            }
        )
    token = ctx.signing_key.sign(claims)
    ctx.tokens.issue_access(
        token=token,
        client_id=client_id,
        user_id=person.id if person else None,
        scopes=scopes,
        app_only=app_only,
        ttl=timedelta(seconds=expires_in),
        extra={"oid": oid},
    )
    response: dict[str, Any] = {
        "token_type": "Bearer",
        "scope": " ".join(scopes),
        "expires_in": expires_in,
        "ext_expires_in": expires_in,
        "access_token": token,
    }
    if "offline_access" in scopes and person:
        response["refresh_token"] = ctx.tokens.issue_refresh(
            client_id=client_id, user_id=person.id, scopes=scopes, nonce=nonce
        ).token
    if "openid" in scopes and person:
        response["id_token"] = id_token(ctx, person, client_id, nonce, access_token=token)
    if include_client_info:
        response["client_info"] = (
            base64.urlsafe_b64encode(json.dumps({"uid": oid, "utid": c.tenant_id(ctx.world)}).encode())
            .decode()
            .rstrip("=")
        )
    return response


def seed(ctx: ProviderContext) -> None:
    def mint(context: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Mint an RS256 Graph access token for an employee or application."""
        return issue_tokens(
            context,
            user=payload.get("user"),
            client_id=payload.get("client_id", "emulator-client"),
            scopes=payload.get("scopes", []),
            app_only=bool(payload.get("app_only", payload.get("user") is None)),
            expires_in=int(payload.get("expires_in", 3600)),
        )

    def approve(context: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Approve a pending device sign-in using its user_code and a world user."""
        record = context.store["_ms_device_codes"].first(user_code=payload.get("user_code"))
        if record is None:
            raise aad(context, 70020, "The device code is invalid or expired.")
        person = resolve_user(context, payload.get("user"))
        record["user"] = person.id
        return {"user_code": record["user_code"], "status": "approved"}

    ctx.provider.actions["mint_token"] = mint
    ctx.provider.actions["approve_device_code"] = approve


async def form(request: Request) -> dict[str, str]:
    return {k: v[-1] for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items()}


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/common/discovery/instance")
    async def instance(request: Request) -> dict[str, Any]:
        endpoint = request.query_params.get("authorization_endpoint", "")
        tenant = endpoint.split("/")[3] if len(endpoint.split("/")) > 3 else "common"
        return {
            "tenant_discovery_endpoint": f"{external_base(request)}/{tenant}/v2.0/.well-known/openid-configuration",
            "api-version": "1.1",
            "metadata": [
                {
                    "preferred_network": "login.microsoftonline.com",
                    "preferred_cache": "login.windows.net",
                    "aliases": [
                        "login.microsoftonline.com",
                        "login.windows.net",
                        "login.microsoft.com",
                        "sts.windows.net",
                    ],
                }
            ],
        }

    @app.get("/common/userrealm/{username}")
    async def realm(username: str) -> dict[str, Any]:
        return {
            "ver": "1.0",
            "account_type": "Managed",
            "domain_name": ctx.world.org.domain,
            "cloud_instance_name": "microsoftonline.com",
            "cloud_audience_urn": "urn:federation:MicrosoftOnline",
        }

    @app.get("/{tenant}/v2.0/.well-known/openid-configuration")
    @app.get("/{tenant}/.well-known/openid-configuration")
    async def discovery(tenant: str, request: Request) -> dict[str, Any]:
        check_tenant(ctx, tenant)
        base = f"{external_base(request)}/{tenant}"
        v2 = "/v2.0/" in request.url.path
        oauth = base + ("/oauth2/v2.0" if v2 else "/oauth2")
        return {
            "issuer": f"https://login.microsoftonline.com/{c.tenant_id(ctx.world)}/v2.0"
            if v2
            else f"https://sts.windows.net/{c.tenant_id(ctx.world)}/",
            "authorization_endpoint": oauth + "/authorize",
            "token_endpoint": oauth + "/token",
            "device_authorization_endpoint": oauth + "/devicecode",
            "end_session_endpoint": oauth + "/logout",
            "jwks_uri": base + "/discovery/v2.0/keys",
            "userinfo_endpoint": "https://graph.microsoft.com/oidc/userinfo",
            "response_types_supported": ["code", "id_token", "code id_token"],
            "response_modes_supported": ["query", "fragment", "form_post"],
            "subject_types_supported": ["pairwise"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "scopes_supported": ["openid", "profile", "email", "offline_access"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "private_key_jwt",
                "client_secret_basic",
            ],
            "claims_supported": [
                "sub",
                "iss",
                "aud",
                "exp",
                "iat",
                "nonce",
                "oid",
                "tid",
                "name",
                "preferred_username",
            ],
            "authorization_response_iss_parameter_supported": False,
        }

    @app.get("/{tenant}/discovery/v2.0/keys")
    @app.get("/{tenant}/discovery/keys")
    async def keys(tenant: str) -> dict[str, Any]:
        check_tenant(ctx, tenant)
        return ctx.signing_key.jwks()

    @app.get("/{tenant}/oauth2/v2.0/authorize")
    @app.get("/{tenant}/oauth2/authorize")
    async def authorize(tenant: str, request: Request) -> Response:
        check_tenant(ctx, tenant)
        q = request.query_params
        redirect_uri = q.get("redirect_uri")
        if not redirect_uri:
            raise aad(ctx, 900144, "The request body must contain redirect_uri.", "invalid_request")
        params: dict[str, Any] = {"state": q.get("state")}
        response_type = q.get("response_type", "code").split()
        mode = q.get("response_mode", "fragment" if "id_token" in response_type else "query")
        if mode not in {"query", "fragment", "form_post"}:
            raise aad(ctx, 900144, "Unsupported response_mode.", "invalid_request")
        try:
            if response_type not in (["code"], ["code", "id_token"], ["id_token", "code"], ["id_token"]):
                raise aad(ctx, 700054, "Unsupported response_type.", "unsupported_response_type")
            if q.get("code_challenge_method", "plain") not in {"plain", "S256"}:
                raise aad(ctx, 50148, "Unsupported code challenge method.", "invalid_request")
            if not q.get("login_hint"):
                accounts = [
                    {"display_name": p.display_name, "email": p.email, "login_hint": p.email}
                    for p in ctx.world.active_employees
                ]
                return HTMLResponse(
                    render_account_picker(
                        provider_title="Microsoft",
                        app_name=q.get("client_id", ""),
                        scopes=q.get("scope", "").split(),
                        accounts=accounts,
                        action_base=external_base(request)
                        + f"/{tenant}/oauth2/v2.0/authorize?"
                        + urlencode(dict(q)),
                    )
                )
            person = resolve_user(ctx, q.get("login_hint"))
            client_id = q.get("client_id", "")
            ctx.tokens.authenticate_client(client_id, None, require_secret=False)
            params["session_state"] = str(uuid.uuid4())
            if "code" in response_type:
                grant = ctx.tokens.issue_code(
                    client_id=client_id,
                    user_id=person.id,
                    scopes=q.get("scope", "").split(),
                    redirect_uri=redirect_uri,
                    code_challenge=q.get("code_challenge"),
                    code_challenge_method=q.get("code_challenge_method"),
                    nonce=q.get("nonce"),
                )
                params["code"] = grant.token
            if "id_token" in response_type:
                params["id_token"] = id_token(ctx, person, client_id, q.get("nonce"), code=params.get("code"))
        except (EmulatorError, OAuthError) as exc:
            if isinstance(exc, OAuthError):
                params.update({"error": exc.error, "error_description": exc.description})
            else:
                params.update({k: exc.body[k] for k in ("error", "error_description")})
        params = {k: v for k, v in params.items() if v is not None}
        if mode == "form_post":
            inputs = "".join(
                f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v), quote=True)}">'
                for k, v in params.items()
            )
            return HTMLResponse(
                f'<!doctype html><html><body><form method="post" action="{html.escape(redirect_uri, quote=True)}">'
                f"{inputs}<noscript><button>Continue</button></noscript></form>"
                "<script>document.forms[0].submit()</script></body></html>"
            )
        return RedirectResponse(
            redirect_with_params(redirect_uri, params, fragment=mode == "fragment"), status_code=302
        )

    @app.post("/{tenant}/oauth2/v2.0/devicecode")
    @app.post("/{tenant}/oauth2/devicecode")
    async def devicecode(tenant: str, request: Request) -> dict[str, Any]:
        check_tenant(ctx, tenant)
        data = await form(request)
        code, user_code = secrets.token_urlsafe(48), secrets.token_hex(4).upper()
        ctx.store["_ms_device_codes"].put(
            {
                "id": code,
                "client_id": data.get("client_id"),
                "scopes": data.get("scope", "").split(),
                "user_code": user_code,
                "expires": ctx.clock.timestamp() + 900,
                "user": None,
                "used": False,
            }
        )
        return {
            "device_code": code,
            "user_code": user_code,
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
            "message": f"To sign in, open https://microsoft.com/devicelogin and enter the code {user_code}.",
        }

    @app.post("/{tenant}/oauth2/v2.0/token")
    @app.post("/{tenant}/oauth2/token")
    async def token(tenant: str, request: Request) -> Response:
        check_tenant(ctx, tenant)
        data = await form(request)
        creds = basic_credentials(request)
        client_id = data.get("client_id") or (creds[0] if creds else None)
        client_secret = data.get("client_secret") or (creds[1] if creds else None)
        try:
            if data.get("client_assertion"):
                if (
                    data.get("client_assertion_type")
                    != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ):
                    raise aad(ctx, 700027, "Invalid client assertion type.", "invalid_client")
                try:
                    assertion = jwt.decode(data["client_assertion"], options={"verify_signature": False})
                    if assertion.get("iss") != client_id or assertion.get("sub") != client_id:
                        raise ValueError
                    if assertion.get("exp", 0) <= ctx.clock.timestamp():
                        raise ValueError
                except (jwt.PyJWTError, ValueError, TypeError):
                    raise aad(ctx, 700027, "Client assertion is invalid.", "invalid_client") from None
            ctx.tokens.authenticate_client(client_id, client_secret, require_secret=False)
            client_id = str(client_id)
            scopes = data.get("scope", "").split()
            kind = data.get("grant_type")
            nonce = None
            user = None
            app_only = kind == "client_credentials"
            if kind == "authorization_code":
                existing = ctx.tokens.resolve(data.get("code"), "code")
                if existing and existing.redirect_uri != data.get("redirect_uri"):
                    raise OAuthError(
                        "invalid_grant", "redirect_uri does not match the authorization request."
                    )
                grant = ctx.tokens.redeem_code(
                    data.get("code"),
                    client_id=client_id,
                    redirect_uri=data.get("redirect_uri"),
                    code_verifier=data.get("code_verifier"),
                )
                user, nonce = grant.user_id, grant.nonce
                scopes = scopes or grant.scopes
                if not set(scopes).issubset(set(grant.scopes)):
                    raise aad(ctx, 70011, "The requested scope is invalid.", "invalid_scope")
            elif kind == "refresh_token":
                grant = ctx.tokens.resolve(data.get("refresh_token"), "refresh")
                if not grant or grant.client_id != client_id:
                    raise aad(ctx, 70008, "The provided authorization code or refresh token has expired.")
                user, nonce = grant.user_id, grant.nonce
                scopes = scopes or grant.scopes
                if not set(scopes).issubset(set(grant.scopes)):
                    raise aad(ctx, 70011, "The requested scope is invalid.", "invalid_scope")
            elif kind == "password":
                user = resolve_user(ctx, data.get("username")).id
            elif kind == "client_credentials":
                if scopes and not all(s.endswith("/.default") for s in scopes):
                    raise aad(
                        ctx,
                        1002012,
                        "Client credential flows must have a scope value with /.default.",
                        "invalid_scope",
                    )
            elif kind == "urn:ietf:params:oauth:grant-type:device_code":
                record = ctx.store["_ms_device_codes"].get(data.get("device_code"))
                if not record or record["used"] or record["expires"] <= ctx.clock.timestamp():
                    raise aad(ctx, 70020, "The device code has expired.", "expired_token")
                if record["client_id"] != client_id:
                    raise aad(ctx, 70000, "The device code belongs to another client.")
                if not record["user"]:
                    raise aad(ctx, 70016, "Authorization is pending.", "authorization_pending")
                user, scopes = record["user"], record["scopes"]
                record["used"] = True
            elif kind == "urn:ietf:params:oauth:grant-type:jwt-bearer":
                grant = ctx.tokens.resolve(data.get("assertion"))
                if not grant or grant.app_only or data.get("requested_token_use") != "on_behalf_of":
                    raise aad(ctx, 50013, "Assertion failed signature validation.")
                user = grant.user_id
            else:
                raise aad(ctx, 70003, "Unsupported grant_type.", "unsupported_grant_type")
            response = issue_tokens(
                ctx,
                user=user,
                client_id=client_id,
                scopes=scopes,
                app_only=app_only,
                nonce=nonce,
                audience=data.get("resource", "https://graph.microsoft.com"),
                include_client_info=data.get("client_info") == "1",
            )
            if "/v2.0/" not in request.url.path:
                response.update(
                    {
                        "resource": data.get("resource", "https://graph.microsoft.com"),
                        "expires_on": str(ctx.clock.timestamp() + 3600),
                        "not_before": str(ctx.clock.timestamp()),
                    }
                )
            return JSONResponse(response, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        except OAuthError as exc:
            raise aad(
                ctx, 70008 if exc.error == "invalid_grant" else 7000215, exc.description, exc.error
            ) from None

    @app.get("/{tenant}/oauth2/v2.0/logout")
    @app.get("/{tenant}/oauth2/logout")
    async def logout(tenant: str, request: Request) -> Response:
        check_tenant(ctx, tenant)
        redirect = request.query_params.get("post_logout_redirect_uri")
        return (
            RedirectResponse(redirect, status_code=302)
            if redirect
            else HTMLResponse("You signed out of your account.")
        )
