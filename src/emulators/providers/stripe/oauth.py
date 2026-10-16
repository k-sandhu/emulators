"""Connect's OAuth dialect, including scope reduction and connection revocation."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from emulators.core.auth import Grant, OAuthError, basic_credentials, bearer_token, redirect_with_params
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.providers.stripe.api import StripeAPI, parameters
from emulators.providers.stripe.wire import PUBLIC_KEY, SECRET_KEY, Record


def grant_scope(grant: Grant) -> str:
    return grant.extra.get("stripe_scope", grant.scopes[0] if grant.scopes else "read_only")


def revoke_connection(ctx: ProviderContext, client_id: str) -> None:
    for record in ctx.store.collection("_grants"):
        if record["client_id"] == client_id and (
            record.get("extra", {}).get("stripe_scope") or record["kind"] == "code"
        ):
            ctx.tokens.revoke(record["id"])


def authenticate(request: Request, ctx: ProviderContext, p: Record) -> None:
    credentials = basic_credentials(request)
    key = bearer_token(request) or (credentials[0] if credentials else None) or p.get("client_secret")
    if not key or not key.startswith("sk_test_"):
        raise OAuthError("invalid_client", "Invalid API key provided.", 401)
    if ctx.tokens.get(key) and not ctx.tokens.resolve(key):
        raise OAuthError("invalid_client", "Invalid API key provided.", 401)
    if not ctx.tokens.lenient_clients and key != SECRET_KEY:
        raise OAuthError("invalid_client", "Invalid API key provided.", 401)


def register_oauth(app: FastAPI, ctx: ProviderContext) -> None:
    async def read_parameters(request: Request) -> Record:
        try:
            return await parameters(request)
        except EmulatorError as exc:
            raise OAuthError("invalid_request", exc.body["error"]["message"]) from exc

    @app.get("/oauth/authorize")
    async def authorize(request: Request) -> Response:
        p: Record = {}
        try:
            p = await read_parameters(request)
            if not p.get("response_type"):
                raise OAuthError("invalid_request", "Missing response_type parameter.")
            if p["response_type"] != "code":
                raise OAuthError("unsupported_response_type", "Only response_type=code is supported.")
            scope = p.get("scope", "read_only")
            if scope not in {"read_only", "read_write"}:
                raise OAuthError("invalid_scope", "Invalid scope parameter.")
            client = ctx.tokens.authenticate_client(p.get("client_id"), None, require_secret=False)
            redirects = client.get("redirect_uris", [])
            redirect = p.get("redirect_uri") or (redirects[0] if redirects else None)
            try:
                target = urlsplit(redirect or "")
                valid = target.scheme in {"http", "https"} and target.hostname and not target.fragment
            except ValueError:
                valid = False
            if not valid or (redirects and redirect not in redirects):
                raise OAuthError("invalid_redirect_uri", "Invalid redirect_uri parameter.")
            grant = ctx.tokens.issue_code(
                client_id=client["id"],
                user_id=ctx.world.org.stripe_account_id,
                scopes=[scope],
                redirect_uri=redirect,
            )
            record = ctx.store.collection("_grants").get(grant.token)
            record["expires_at"] = ctx.clock.now() + timedelta(minutes=5)
            return RedirectResponse(
                redirect_with_params(
                    redirect, {"code": grant.token, "scope": scope, "state": p.get("state")}
                ),
                status_code=302,
            )
        except OAuthError as exc:
            return JSONResponse(
                {
                    "error": exc.error,
                    "error_description": exc.description,
                    **({"state": p["state"]} if "state" in p else {}),
                },
                status_code=exc.status,
            )

    @app.post("/oauth/token")
    async def token(request: Request) -> Response:
        try:
            p = await read_parameters(request)
            authenticate(request, ctx, p)
            kind = p.get("grant_type")
            if not kind:
                raise OAuthError("invalid_request", "Missing grant_type parameter.")
            if kind == "authorization_code":
                if not p.get("code"):
                    raise OAuthError("invalid_request", "Missing code parameter.")
                issued = ctx.tokens.get(p["code"], "code")
                if issued and issued.revoked:
                    revoke_connection(ctx, issued.client_id)
                    raise OAuthError("invalid_grant", "Authorization code has already been used.")
                grant = ctx.tokens.redeem_code(
                    p["code"],
                    client_id=p.get("client_id"),
                    redirect_uri=p.get("redirect_uri"),
                    code_verifier=None,
                )
                scope = grant_scope(grant)
            elif kind == "refresh_token":
                if not p.get("refresh_token"):
                    raise OAuthError("invalid_request", "Missing refresh_token parameter.")
                grant = ctx.tokens.resolve(p["refresh_token"], "refresh")
                if not grant or p.get("client_id", grant.client_id) != grant.client_id:
                    raise OAuthError("invalid_grant", "Invalid refresh token.")
                scope = p.get("scope", grant_scope(grant))
                if scope not in {"read_only", "read_write"} or (
                    grant_scope(grant) == "read_only" and scope == "read_write"
                ):
                    raise OAuthError("invalid_scope", "The requested scope exceeds the refresh grant.")
                for record in ctx.store.collection("_grants"):
                    if (
                        record["client_id"] == grant.client_id
                        and record["kind"] == "access"
                        and (record.get("extra", {}).get("stripe_scope") == scope)
                    ):
                        ctx.tokens.revoke(record["id"])
            else:
                raise OAuthError("unsupported_grant_type", "Unsupported grant_type parameter.")
        except OAuthError as exc:
            return JSONResponse(
                {"error": exc.error, "error_description": exc.description}, status_code=exc.status
            )
        access = ctx.tokens.issue_access(
            client_id=grant.client_id,
            user_id=None,
            app_only=True,
            scopes=[],
            prefix="sk_test_",
            ttl=None,
            extra={"stripe_scope": scope},
        )
        refresh = ctx.tokens.issue_refresh(
            client_id=grant.client_id,
            user_id=None,
            app_only=True,
            scopes=[],
            ttl=None,
            extra={"stripe_scope": scope},
        )
        return JSONResponse(
            {
                "access_token": access.token,
                "livemode": False,
                "refresh_token": refresh.token,
                "token_type": "bearer",
                "stripe_publishable_key": PUBLIC_KEY,
                "stripe_user_id": ctx.world.org.stripe_account_id,
                "scope": scope,
            }
        )

    @app.post("/oauth/deauthorize")
    async def deauthorize(request: Request) -> Response:
        try:
            p = await read_parameters(request)
            authenticate(request, ctx, p)
            if not p.get("client_id") or not p.get("stripe_user_id"):
                raise OAuthError("invalid_request", "Missing client_id or stripe_user_id parameter.")
            if p["stripe_user_id"] != ctx.world.org.stripe_account_id or not any(
                r["client_id"] == p["client_id"]
                and r["kind"] == "access"
                and not r["revoked"]
                and r.get("extra", {}).get("stripe_scope")
                for r in ctx.store.collection("_grants")
            ):
                raise OAuthError("invalid_client", "The account is not connected to this application.")
            # Also enforce the caller's control-plane scopes when one is used as its secret.
            StripeAPI(ctx, request, p).authorize("account", "write")
            revoke_connection(ctx, p["client_id"])
            return JSONResponse({"stripe_user_id": ctx.world.org.stripe_account_id})
        except OAuthError as exc:
            return JSONResponse(
                {"error": exc.error, "error_description": exc.description}, status_code=exc.status
            )
