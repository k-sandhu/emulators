"""OAuth 2.0 for Box: authorize page, token endpoint (all grant types) and revocation.

Grant types: ``authorization_code``, ``refresh_token`` (single use, rotating),
``client_credentials`` (CCG, enterprise or user subject), ``urn:ietf:params:oauth:grant-type:jwt-bearer``
(JWT app auth; claims are checked structurally, the signature is not verified) and
``urn:ietf:params:oauth:grant-type:token-exchange`` (downscoping).
"""

from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import Grant, OAuthError, basic_credentials, redirect_with_params, render_account_picker
from emulators.core.provider import ProviderContext
from emulators.providers.box import access, db, errors, tree
from emulators.providers.box.access import Principal
from emulators.providers.box.routing import Api
from emulators.providers.box.util import ALNUM

Record = dict[str, Any]

ACCESS_TTL = 3600
REFRESH_TTL = timedelta(days=60)
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


def alnum_token(n: int) -> str:
    return "".join(secrets.choice(ALNUM) for _ in range(n))


def issue_tokens(
    ctx: ProviderContext,
    user: Record,
    client_id: str,
    *,
    refresh: bool,
    restricted_to: list[Record] | None = None,
    ttl_seconds: int = ACCESS_TTL,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    """Mint an access token (and a refresh token for 3-legged flows) in Box's response shape."""
    access_grant = ctx.tokens.issue_access(
        client_id=client_id,
        user_id=user["id"],
        scopes=scopes or [],
        ttl=timedelta(seconds=ttl_seconds),
        token=alnum_token(32),
        extra={"restricted_to": restricted_to or []},
    )
    body: dict[str, Any] = {
        "access_token": access_grant.token,
        "expires_in": ttl_seconds,
        "restricted_to": restricted_to or [],
    }
    if refresh:
        refresh_grant = ctx.tokens.issue_refresh(
            client_id=client_id,
            user_id=user["id"],
            scopes=scopes or [],
            ttl=REFRESH_TTL,
            token=alnum_token(64),
            extra={"access": access_grant.token},
        )
        body["refresh_token"] = refresh_grant.token
    body["token_type"] = "bearer"
    return body


def service_account(ctx: ProviderContext) -> Record:
    for user in db.users(ctx):
        if user.get("_kind") == "service":
            return user
    raise errors.not_found()


def resolve_subject(ctx: ProviderContext, subject_type: str | None, subject_id: str | None) -> Record:
    """CCG / JWT subject -> acting user (service account for the enterprise)."""
    enterprise = ctx.world.org.box_enterprise_id
    if subject_type in (None, "", "enterprise"):
        if subject_id and subject_id != enterprise:
            raise OAuthError("invalid_grant", "Grant credentials are invalid")
        return service_account(ctx)
    if subject_type == "user":
        user = db.users(ctx).get(subject_id or "") if subject_id else None
        if user is None or user.get("_kind") in ("external", "service") or user.get("status") != "active":
            raise OAuthError("invalid_grant", "Grant credentials are invalid")
        return user
    raise OAuthError("invalid_request", "Invalid box_subject_type.")


def parse_resource(ctx: ProviderContext, resource: str) -> tuple[str, str]:
    match = re.search(r"/2\.0/(files|folders)/(\d+)/?$", resource)
    if not match:
        raise OAuthError("invalid_request", "Invalid resource. It must be a file or folder URL.")
    return ("file" if match.group(1) == "files" else "folder"), match.group(2)


def exchange_token(ctx: ProviderContext, form: dict[str, str]) -> dict[str, Any]:
    subject_token = form.get("subject_token")
    grant = ctx.tokens.resolve(subject_token) if subject_token else None
    if grant is None:
        raise OAuthError("invalid_grant", "The subject token is invalid or has expired")
    if form.get("subject_token_type", ACCESS_TOKEN_TYPE) != ACCESS_TOKEN_TYPE:
        raise OAuthError("invalid_request", "Unsupported subject_token_type")
    user = db.users(ctx).get(grant.user_id or "")
    if user is None:
        raise OAuthError("invalid_grant", "The subject token is invalid or has expired")
    scopes = [s for s in (form.get("scope") or "").split() if s]
    if not scopes:
        raise OAuthError("invalid_request", "The scope parameter is required")
    unknown = [s for s in scopes if s not in access.SCOPE_CAPS]
    if unknown:
        raise OAuthError("invalid_scope", f"Invalid scope: {unknown[0]}")
    principal = Principal(ctx, user, user)
    restricted: list[Record] = []
    shaper_target: Record | None = None
    if form.get("resource"):
        item_type, item_id = parse_resource(ctx, form["resource"])
        record = principal.folder_record(item_id) if item_type == "folder" else db.item(ctx, item_type, item_id)
        if record is None or not principal.raw_caps(record) or tree.is_trashed(ctx, record):
            raise OAuthError("invalid_grant", "The resource is not accessible with the subject token")
        shaper_target = record
    elif form.get("box_shared_link"):
        record = access.find_shared_link(ctx, form["box_shared_link"])
        if record is None or not access.link_is_live(ctx, record):
            raise OAuthError("invalid_grant", "The shared link is invalid")
        shaper_target = record
    for scope in scopes:
        entry: Record = {"scope": scope}
        if shaper_target is not None:
            seq = None if db.is_root(shaper_target) else str(shaper_target["_seq"])
            entry["object"] = {
                "type": shaper_target["type"],
                "id": shaper_target["id"],
                "sequence_id": seq,
                "etag": seq,
                "name": shaper_target["name"],
            }
        restricted.append(entry)
    remaining = ACCESS_TTL
    if grant.expires_at is not None:
        remaining = max(1, min(ACCESS_TTL, int((grant.expires_at - ctx.clock.now()).total_seconds())))
    tokens = issue_tokens(
        ctx, user, grant.client_id, refresh=False, restricted_to=restricted, ttl_seconds=remaining, scopes=scopes
    )
    tokens["issued_token_type"] = ACCESS_TOKEN_TYPE
    return tokens


def jwt_claims(assertion: str | None) -> dict[str, Any]:
    if not assertion:
        raise OAuthError("invalid_request", "Missing assertion")
    try:
        claims = jwt.decode(assertion, options={"verify_signature": False, "verify_aud": False, "verify_exp": False})
    except jwt.PyJWTError:
        raise OAuthError("invalid_grant", "Invalid JWT assertion") from None
    for required in ("iss", "sub", "box_sub_type"):
        if not claims.get(required):
            raise OAuthError("invalid_grant", f"Invalid JWT assertion: missing claim '{required}'")
    if claims["box_sub_type"] not in ("enterprise", "user"):
        raise OAuthError("invalid_grant", "Invalid JWT assertion: box_sub_type must be enterprise or user")
    return claims


async def form_of(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
    creds = basic_credentials(request)
    if creds:
        form.setdefault("client_id", creds[0])
        form.setdefault("client_secret", creds[1])
    return form


def token_response(body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)

    @app.get("/api/oauth2/authorize")
    async def authorize(request: Request) -> Response:
        q = request.query_params
        redirect_uri = q.get("redirect_uri")
        state = q.get("state")
        client_id = q.get("client_id")
        if not client_id:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Missing client_id"}, status_code=400
            )
        ctx.tokens.authenticate_client(client_id, None, require_secret=False)

        def fail(error: str, description: str) -> Response:
            if redirect_uri:
                return RedirectResponse(
                    redirect_with_params(
                        redirect_uri, {"error": error, "error_description": description, "state": state}
                    ),
                    status_code=302,
                )
            return JSONResponse({"error": error, "error_description": description}, status_code=400)

        if q.get("response_type") != "code":
            return fail("unsupported_response_type", "response_type must be 'code'")
        if not redirect_uri:
            return fail("invalid_request", "redirect_uri is required")
        hint = q.get("login_hint")
        if hint:
            user = access.lookup_user(ctx, hint)
            if user is None or user.get("status") != "active" or user.get("_kind") == "service":
                return fail("access_denied", "The user could not be authenticated")
            code = ctx.tokens.issue_code(
                client_id=client_id,
                user_id=user["id"],
                scopes=(q.get("scope") or "").split(),
                redirect_uri=redirect_uri,
                code_challenge=q.get("code_challenge"),
                code_challenge_method=q.get("code_challenge_method"),
            )
            return RedirectResponse(
                redirect_with_params(redirect_uri, {"code": code.token, "state": state}), status_code=302
            )
        accounts = [
            {"display_name": u["name"], "email": u["login"], "login_hint": u["login"]}
            for u in db.users(ctx)
            if u.get("status") == "active" and u.get("_kind") in ("managed", "external")
        ]
        client = ctx.tokens.client(client_id) or {}
        return HTMLResponse(
            render_account_picker(
                provider_title="Box",
                app_name=client.get("name", client_id),
                scopes=(q.get("scope") or "root_readwrite").split(),
                accounts=accounts,
                action_base=str(request.url),
            )
        )

    @app.post("/oauth2/token")
    async def token(request: Request) -> Response:
        form = await form_of(request)
        grant_type = form.get("grant_type")
        try:
            return token_response(handle_grant(ctx, grant_type, form))
        except OAuthError as exc:
            raise errors.oauth_error(exc.error, exc.description, exc.status) from None

    @app.post("/oauth2/revoke")
    async def revoke(request: Request) -> Response:
        form = await form_of(request)
        value = form.get("token")
        if not value:
            raise errors.oauth_error("invalid_request", "Missing token")
        for grant in (ctx.tokens.get(value, "access"), ctx.tokens.get(value, "refresh")):
            if grant is None:
                continue
            ctx.tokens.revoke(grant.token)
            sibling = grant.extra.get("access") or grant.extra.get("refresh")
            if sibling:
                ctx.tokens.revoke(sibling)
        return Response(status_code=200)

    del api


def handle_grant(ctx: ProviderContext, grant_type: str | None, form: dict[str, str]) -> dict[str, Any]:
    client_id = form.get("client_id")
    if grant_type == "authorization_code":
        client = ctx.tokens.authenticate_client(client_id, form.get("client_secret"))
        code = ctx.tokens.get(form.get("code"), "code")
        if code is None or code.revoked or ctx.tokens.resolve(form.get("code"), "code") is None:
            raise OAuthError("invalid_grant", "Auth code doesn't exist or is invalid for the client")
        redeemed = ctx.tokens.redeem_code(
            form.get("code"),
            client_id=client["id"],
            redirect_uri=form.get("redirect_uri"),
            code_verifier=form.get("code_verifier"),
        )
        user = db.users(ctx).get(redeemed.user_id or "")
        if user is None:
            raise OAuthError("invalid_grant", "Auth code doesn't exist or is invalid for the client")
        return issue_tokens(ctx, user, client["id"], refresh=True, scopes=redeemed.scopes)
    if grant_type == "refresh_token":
        client = ctx.tokens.authenticate_client(client_id, form.get("client_secret"))
        refresh = ctx.tokens.resolve(form.get("refresh_token"), "refresh")
        if refresh is None:
            raise OAuthError("invalid_grant", "Refresh token has expired")
        user = db.users(ctx).get(refresh.user_id or "")
        if user is None or user.get("status") != "active":
            raise OAuthError("invalid_grant", "Refresh token has expired")
        ctx.tokens.revoke(refresh.token)  # refresh tokens are single use and rotate
        old_access = refresh.extra.get("access")
        if old_access:
            ctx.tokens.revoke(old_access)
        return issue_tokens(ctx, user, client["id"], refresh=True, scopes=refresh.scopes)
    if grant_type == "client_credentials":
        client = ctx.tokens.authenticate_client(client_id, form.get("client_secret"))
        user = resolve_subject(ctx, form.get("box_subject_type"), form.get("box_subject_id"))
        return issue_tokens(ctx, user, client["id"], refresh=False)
    if grant_type == JWT_BEARER:
        claims = jwt_claims(form.get("assertion"))
        client = ctx.tokens.authenticate_client(form.get("client_id") or claims["iss"], form.get("client_secret"))
        user = resolve_subject(ctx, claims["box_sub_type"], str(claims["sub"]))
        return issue_tokens(ctx, user, client["id"], refresh=False)
    if grant_type == TOKEN_EXCHANGE:
        ctx.tokens.authenticate_client(client_id or "token-exchange", form.get("client_secret"), require_secret=False)
        return exchange_token(ctx, form)
    if not grant_type:
        raise OAuthError("invalid_request", "Invalid grant_type parameter or parameter missing")
    raise OAuthError("unsupported_grant_type", f"Unsupported grant_type: {grant_type}")


def grant_user(ctx: ProviderContext, grant: Grant) -> Record | None:
    return db.users(ctx).get(grant.user_id or "")
