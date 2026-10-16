"""Google OAuth grants use the shared token store so reset also resets identity."""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import (
    Grant,
    OAuthError,
    basic_credentials,
    redirect_with_params,
    render_account_picker,
)
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.google.common import google_error, principal, user_id
from emulators.world.model import Person

TOKEN_URI = "https://oauth2.googleapis.com/token"
ISSUER = "https://accounts.google.com"


def certificate(ctx: ProviderContext) -> str:
    """google-auth expects X.509 certificates at the legacy certs endpoint."""
    cached = ctx.store.meta.get("google_oidc_certificate")
    if cached:
        return cached
    key = ctx.signing_key
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "accounts.google.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(serialization.load_pem_public_key(key.public_pem.encode()))
        .serial_number(1)
        .not_valid_before(ctx.clock.now() - timedelta(days=1))
        .not_valid_after(ctx.clock.now() + timedelta(days=3650))
        .sign(key._private, hashes.SHA256())
    )
    result = cert.public_bytes(serialization.Encoding.PEM).decode()
    ctx.store.meta["google_oidc_certificate"] = result
    return result


def resolve_person(ctx: ProviderContext, value: str | None) -> Person | None:
    return next((p for p in ctx.world.people if value in (p.id, p.email, user_id(ctx.world, p.id))), None)


def claims(ctx: ProviderContext, person: Person) -> dict[str, Any]:
    return {
        "sub": user_id(ctx.world, person.id),
        "email": person.email,
        "email_verified": True,
        "hd": person.email.split("@")[-1],
        "name": person.display_name,
        "given_name": person.first_name,
        "family_name": person.last_name,
        "picture": f"https://lh3.googleusercontent.com/a/default-user=s96?{person.username}",
    }


def issue_access(
    ctx: ProviderContext,
    *,
    client_id: str,
    user: str | None,
    scopes: list[str],
    app_only: bool = False,
    extra: dict[str, Any] | None = None,
    expires_in: int = 3599,
) -> Grant:
    return ctx.tokens.issue_access(
        client_id=client_id,
        user_id=user,
        scopes=scopes,
        app_only=app_only,
        prefix="ya29.a0",
        length=194,
        ttl=timedelta(seconds=expires_in),
        extra=extra or {},
    )


def token_response(ctx: ProviderContext, source: Grant, *, refresh: bool = False) -> dict[str, Any]:
    access = issue_access(
        ctx,
        client_id=source.client_id,
        user=source.user_id,
        scopes=source.scopes,
        app_only=source.app_only,
        extra={**source.extra, "source_token": source.token},
    )
    result: dict[str, Any] = {
        "access_token": access.token,
        "expires_in": 3599,
        "scope": " ".join(access.scopes),
        "token_type": "Bearer",
    }
    if refresh:
        existing = next(
            (
                g
                for g in ctx.store.collection("_grants")
                if g["kind"] == "refresh"
                and g["client_id"] == source.client_id
                and g["user_id"] == source.user_id
                and g["scopes"] == source.scopes
                and not g["revoked"]
            ),
            None,
        )
        if existing is None:
            grant = ctx.tokens.issue_refresh(
                client_id=source.client_id,
                user_id=source.user_id,
                scopes=source.scopes,
                prefix="1//0g",
                length=96,
                ttl=None,
                extra=source.extra.copy(),
            )
            result["refresh_token"] = grant.token
        else:
            result["refresh_token"] = existing["token"]
    person = resolve_person(ctx, source.extra.get("sub_person") or source.user_id)
    if "openid" in source.scopes and person:
        now = ctx.clock.timestamp()
        digest = hashlib.sha256(access.token.encode()).digest()[:16]
        payload = {
            **claims(ctx, person),
            "iss": ISSUER,
            "aud": source.client_id,
            "azp": source.client_id,
            "iat": now,
            "exp": now + 3599,
            "at_hash": base64.urlsafe_b64encode(digest).rstrip(b"=").decode(),
        }
        if source.nonce:
            payload["nonce"] = source.nonce
        result["id_token"] = ctx.signing_key.sign(payload)
        ctx.store.collection("google_id_tokens").put_as(
            result["id_token"], {"claims": payload, "access_token": access.token}
        )
    return result


async def form_data(request: Request) -> dict[str, str]:
    body = parse_qs((await request.body()).decode(), keep_blank_values=True)
    return {key: values[-1] for key, values in body.items()}


def seed(ctx: ProviderContext) -> None:
    @ctx.provider.action("mint_token")
    def mint(context: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Mint a Google access token; subject impersonates a domain user."""
        person = resolve_person(context, payload.get("user"))
        subject = resolve_person(context, payload.get("subject"))
        if payload.get("user") and person is None:
            raise google_error(400, "Unknown user.", reason="invalid")
        if payload.get("subject") and subject is None:
            raise google_error(400, "Unknown subject.", reason="invalid")
        scopes = payload.get("scopes", [])
        if isinstance(scopes, str):
            scopes = scopes.split()
        grant = issue_access(
            context,
            client_id=payload.get("client_id", "emulator-client"),
            user=person.id if person else None,
            scopes=scopes,
            app_only=payload.get("app_only", person is None),
            extra={"sub_person": subject.id} if subject else {},
            expires_in=int(payload.get("expires_in", 3599)),
        )
        return {
            "access_token": grant.token,
            "token_type": "Bearer",
            "expires_in": int(payload.get("expires_in", 3599)),
        }


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/o/oauth2/auth")
    @app.get("/o/oauth2/v2/auth")
    async def authorize(request: Request) -> Response:
        q = request.query_params
        redirect = q.get("redirect_uri", "")
        client_id = q.get("client_id", "")
        if not redirect or not client_id:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Missing required parameter"},
                status_code=400,
            )
        client = ctx.tokens.authenticate_client(client_id, None, require_secret=False)
        if client.get("redirect_uris") and redirect not in client["redirect_uris"]:
            return JSONResponse({"error": "redirect_uri_mismatch"}, status_code=400)
        error = None
        if q.get("response_type") != "code":
            error = "unsupported_response_type"
        if q.get("code_challenge_method", "plain") not in ("plain", "S256"):
            error = "invalid_request"
        if q.get("error") == "access_denied":
            error = "access_denied"
        person = resolve_person(ctx, q.get("login_hint"))
        if q.get("login_hint") and (not person or not person.active):
            error = "access_denied"
        if error:
            return RedirectResponse(
                redirect_with_params(redirect, {"error": error, "state": q.get("state")}), status_code=302
            )
        scopes = q.get("scope", "").split()
        if person is None:
            return HTMLResponse(
                render_account_picker(
                    provider_title="Google",
                    app_name=client_id,
                    scopes=scopes,
                    accounts=[
                        {"display_name": p.display_name, "email": p.email, "login_hint": p.email}
                        for p in ctx.world.active_employees
                    ],
                    action_base=str(request.url),
                )
            )
        if q.get("include_granted_scopes") == "true":
            for g in ctx.store.collection("_grants"):
                if g["client_id"] == client_id and g["user_id"] == person.id and not g["revoked"]:
                    scopes = list(dict.fromkeys([*scopes, *g["scopes"]]))
        code = ctx.tokens.issue_code(
            client_id=client_id,
            user_id=person.id,
            scopes=scopes,
            redirect_uri=redirect,
            code_challenge=q.get("code_challenge"),
            code_challenge_method=q.get("code_challenge_method"),
            nonce=q.get("nonce"),
            extra={"access_type": q.get("access_type", "online")},
        )
        return RedirectResponse(
            redirect_with_params(
                redirect,
                {
                    "code": code.token,
                    "scope": " ".join(scopes),
                    "authuser": "0",
                    "prompt": q.get("prompt", "consent"),
                    "state": q.get("state"),
                },
            ),
            status_code=302,
        )

    @app.post("/oauth2/v4/token")
    @app.post("/token")
    async def token(request: Request) -> Response:
        data = await form_data(request)
        try:
            grant_type = data.get("grant_type")
            if grant_type == "urn:ietf:params:oauth:grant-type:jwt-bearer":
                assertion = data.get("assertion", "")
                try:
                    header = jwt.get_unverified_header(assertion)
                    payload = jwt.decode(assertion, options={"verify_signature": False})
                    now = ctx.clock.timestamp()
                    if (
                        header.get("alg") != "RS256"
                        or not isinstance(payload.get("iss"), str)
                        or "@" not in payload["iss"]
                        or not payload.get("scope")
                        or payload.get("aud") not in (TOKEN_URI, "https://www.googleapis.com/oauth2/v4/token")
                        or not isinstance(payload.get("iat"), (int, float))
                        or not isinstance(payload.get("exp"), (int, float))
                        or payload["iat"] > now + 60
                        or payload["exp"] <= now
                        or payload["exp"] - payload["iat"] > 3600
                    ):
                        raise ValueError
                except (jwt.PyJWTError, ValueError, TypeError):
                    raise OAuthError(
                        "invalid_grant",
                        "Invalid JWT: Token must be a short-lived token (60 minutes) and in a reasonable timeframe.",
                    ) from None
                subject = resolve_person(ctx, payload.get("sub"))
                if payload.get("sub") and (subject is None or not subject.active or subject.is_external):
                    raise OAuthError("invalid_grant", "Invalid email or User ID")
                source = ctx.tokens.issue(
                    "code",
                    client_id=payload["iss"],
                    user_id=None,
                    scopes=payload["scope"].split(),
                    app_only=True,
                    extra={"sub_person": subject.id} if subject else {},
                )
                result = token_response(ctx, source)
                ctx.tokens.revoke(source.token)
            else:
                creds = basic_credentials(request)
                client_id = creds[0] if creds else data.get("client_id")
                secret = creds[1] if creds else data.get("client_secret")
                ctx.tokens.authenticate_client(client_id, secret, require_secret=True)
                if grant_type == "authorization_code":
                    source = ctx.tokens.resolve(data.get("code"), "code")
                    if source is None or source.redirect_uri != data.get("redirect_uri"):
                        raise OAuthError("invalid_grant", "Bad Request")
                    source = ctx.tokens.redeem_code(
                        data.get("code"),
                        client_id=client_id,
                        redirect_uri=data.get("redirect_uri"),
                        code_verifier=data.get("code_verifier"),
                    )
                    result = token_response(ctx, source, refresh=source.extra.get("access_type") == "offline")
                elif grant_type == "refresh_token":
                    source = ctx.tokens.resolve(data.get("refresh_token"), "refresh")
                    if source is None or source.client_id != client_id:
                        raise OAuthError("invalid_grant", "Bad Request")
                    person = resolve_person(ctx, source.user_id)
                    if person is not None and not person.active:
                        raise OAuthError("invalid_grant", "Token has been expired or revoked.")
                    result = token_response(ctx, source)
                else:
                    raise OAuthError("unsupported_grant_type", "Invalid grant_type")
            return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        except OAuthError as exc:
            return JSONResponse(
                {"error": exc.error, "error_description": exc.description}, status_code=exc.status
            )

    @app.get("/tokeninfo")
    @app.post("/tokeninfo")
    async def tokeninfo(request: Request) -> Response:
        data = dict(request.query_params)
        if request.method == "POST":
            data.update(await form_data(request))
        if data.get("id_token"):
            record = ctx.store.collection("google_id_tokens").get(data["id_token"])
            if (
                not record
                or record["claims"]["exp"] <= ctx.clock.timestamp()
                or not ctx.tokens.resolve(record["access_token"])
            ):
                return JSONResponse(
                    {"error": "invalid_token", "error_description": "Invalid Value"}, status_code=400
                )
            result = {**record["claims"], "expires_in": str(record["claims"]["exp"] - ctx.clock.timestamp())}
        else:
            grant = ctx.tokens.resolve(data.get("access_token"))
            if not grant:
                return JSONResponse(
                    {"error": "invalid_token", "error_description": "Invalid Value"}, status_code=400
                )
            exp = int(grant.expires_at.timestamp()) if grant.expires_at else ctx.clock.timestamp() + 3599
            result = {
                "azp": grant.client_id,
                "aud": grant.client_id,
                "scope": " ".join(grant.scopes),
                "exp": str(exp),
                "expires_in": str(exp - ctx.clock.timestamp()),
                "access_type": grant.extra.get("access_type", "online"),
            }
            person = resolve_person(ctx, grant.extra.get("sub_person") or grant.user_id)
            if person:
                result.update(
                    {"sub": user_id(ctx.world, person.id), "email": person.email, "email_verified": "true"}
                )
        return JSONResponse(result)

    @app.get("/v1/userinfo")
    @app.get("/oauth2/v3/userinfo")
    @app.get("/oauth2/v2/userinfo")
    async def userinfo(request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        person = resolve_person(ctx, actor.person_id)
        if person is None:
            raise google_error(401, "Invalid Credentials", reason="authError")
        result = claims(ctx, person)
        if request.url.path.endswith("v2/userinfo"):
            result["id"] = result.pop("sub")
            result["verified_email"] = result.pop("email_verified")
        return result

    @app.get("/oauth2/v3/certs")
    async def jwks() -> Response:
        return JSONResponse(ctx.signing_key.jwks(), headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/oauth2/v1/certs")
    async def certs() -> dict[str, str]:
        return {ctx.signing_key.kid: certificate(ctx)}

    @app.get("/.well-known/openid-configuration")
    async def discovery(request: Request) -> dict[str, Any]:
        return {
            "issuer": ISSUER,
            "authorization_endpoint": url_for_host(request, ctx, "accounts.google.com", "/o/oauth2/v2/auth"),
            "token_endpoint": url_for_host(request, ctx, "oauth2.googleapis.com", "/token"),
            "userinfo_endpoint": url_for_host(request, ctx, "openidconnect.googleapis.com", "/v1/userinfo"),
            "revocation_endpoint": url_for_host(request, ctx, "oauth2.googleapis.com", "/revoke"),
            "jwks_uri": url_for_host(request, ctx, "www.googleapis.com", "/oauth2/v3/certs"),
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "scopes_supported": ["openid", "email", "profile"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "claims_supported": [
                "iss",
                "aud",
                "sub",
                "email",
                "email_verified",
                "hd",
                "name",
                "picture",
                "given_name",
                "family_name",
                "iat",
                "exp",
                "nonce",
                "at_hash",
            ],
            "code_challenge_methods_supported": ["plain", "S256"],
        }

    @app.post("/revoke")
    async def revoke(request: Request) -> Response:
        data = await form_data(request)
        value = request.query_params.get("token") or data.get("token", "")
        grant = ctx.tokens.get(value)
        if grant is None:
            raise EmulatorError(400, {"error": "invalid_token"})
        # Google revokes the client's associated access and refresh grants together.
        for row in ctx.store.collection("_grants"):
            if (
                row["client_id"] == grant.client_id
                and row["user_id"] == grant.user_id
                and row.get("extra", {}).get("sub_person") == grant.extra.get("sub_person")
            ):
                ctx.tokens.revoke(row["id"])
        return Response(status_code=200)
