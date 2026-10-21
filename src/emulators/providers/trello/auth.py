"""Legacy Trello key/token grants and the personal-token authorization page."""

from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext

from .common import KEY, Record, fail, http_url, integer, member, names, text


def authenticate(
    ctx: ProviderContext, request: Request, params: Record, *, write: bool = False
) -> Grant | None:
    key, token = params.get("key"), params.get("token")
    header = request.headers.get("authorization", "")
    if header.lower().startswith("oauth "):
        pairs = {k: unquote(v) for k, v in re.findall(r'(\w+)="([^"]*)"', header[6:])}
        key, token = pairs.get("oauth_consumer_key", key), pairs.get("oauth_token", token)
    elif header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not key and not header.lower().startswith("bearer "):
        fail(401, "invalid key")
    if key is not None and (not isinstance(key, str) or not ctx.tokens.client(key)):
        fail(401, "invalid key")
    if not token:
        if not key:
            fail(401, "invalid key")
        if write:
            fail(401, "invalid token")
        return None
    if not isinstance(token, str):
        fail(401, "invalid token")
    grant = ctx.tokens.resolve(token)
    if not grant or not grant.user_id or grant.app_only:
        fail(401, "invalid token")
    if key and grant.client_id != key:
        fail(401, "invalid token")
    if not ctx.store["members"].get(grant.user_id):
        fail(401, "invalid token")
    required = "write" if write else "read"
    if required not in grant.scopes and "*" not in grant.scopes:
        fail(401, "unauthorized permission requested")
    return grant


def mint_token(ctx: ProviderContext, payload: Record) -> Record:
    if payload.get("app_only"):
        fail(400, "Trello requires a member grant")
    who = member(ctx, payload.get("user", payload.get("user_id", "p001")))
    key = text(payload.get("client_id", KEY), "client_id", 128)
    if not ctx.tokens.client(key):
        ctx.tokens.register_client(key, payload.get("client_secret", "emulator-secret"))
    scopes = names(payload.get("scopes", ["read", "write", "account"]), "scopes")
    if any(scope not in ("read", "write", "account", "*") for scope in scopes):
        fail(400, "invalid scope")
    ttl = integer(payload.get("expires_in"), "expires_in", 3600, 315360000, 0)
    grant = ctx.tokens.issue_access(
        client_id=key, user_id=who["id"], scopes=scopes, ttl=timedelta(seconds=ttl), length=64
    )
    return {
        "access_token": grant.token,
        "token": grant.token,
        "key": key,
        "idMember": who["id"],
        "token_type": "bearer",
        "expires_in": ttl,
        "scope": ",".join(scopes),
    }


def authorize(ctx: ProviderContext, params: Record) -> Response:
    key = params.get("key")
    if not isinstance(key, str) or not ctx.tokens.client(key):
        fail(401, "invalid key")
    if params.get("response_type", "token") != "token":
        fail(400, "invalid response_type")
    scopes = names(params.get("scope", "read"), "scope")
    if any(s not in ("read", "write", "account") for s in scopes):
        fail(400, "invalid scope")
    expiry = params.get("expiration", "30days")
    periods = {
        "1hour": timedelta(hours=1),
        "1day": timedelta(days=1),
        "30days": timedelta(days=30),
        "never": None,
    }
    if not isinstance(expiry, str) or expiry not in periods:
        fail(400, "invalid expiration")
    callback = params.get("callback_method", "fragment")
    if callback != "fragment":
        fail(400, "invalid callback_method")
    target = http_url(params["return_url"], "return_url") if params.get("return_url") else None
    if not params.get("login_hint"):
        return HTMLResponse(
            "<html><body><h1>Authorize Halcyon Trello</h1><p>Use login_hint with a world member email to select the local account.</p></body></html>"
        )
    who = member(ctx, params["login_hint"])
    grant = ctx.tokens.issue_access(
        client_id=key, user_id=who["id"], scopes=scopes, ttl=periods[expiry], length=64
    )
    if target and callback == "fragment":
        parts = urlsplit(target)
        fragment = urlencode([*parse_qsl(parts.fragment), ("token", grant.token)])
        return RedirectResponse(urlunsplit((*parts[:4], fragment)), status_code=302)
    return HTMLResponse("<html><body><pre>" + grant.token + "</pre></body></html>")
