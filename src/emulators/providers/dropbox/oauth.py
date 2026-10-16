"""OAuth 2 for Dropbox: ``/oauth2/authorize`` (www.dropbox.com) and ``/oauth2/token``
(api.dropboxapi.com / api.dropbox.com).

* authorization code flow with optional PKCE (S256/plain), ``token_access_type=offline`` for refresh
  tokens, ``response_type=token`` (implicit) and the "no redirect" access-code page;
* ``login_hint=<email>`` auto-approves; otherwise a small account picker is rendered;
* requesting any team scope (``members.read``, ``team_data.member`` ...) yields a *team* token that
  can act as members with ``Dropbox-API-Select-User`` -- like an app of type "Team member file access".
"""

from __future__ import annotations

import html
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import OAuthError, basic_credentials, redirect_with_params, render_account_picker
from emulators.core.provider import ProviderContext, external_base
from emulators.providers.dropbox import model as m

ACCESS_TTL = 14400
DEFAULT_SCOPES = (
    "account_info.read",
    "account_info.write",
    "contacts.read",
    "contacts.write",
    "file_requests.read",
    "file_requests.write",
    "files.content.read",
    "files.content.write",
    "files.metadata.read",
    "files.metadata.write",
    "sharing.read",
    "sharing.write",
)
TEAM_SCOPES = {
    "team_info.read",
    "team_info.write",
    "team_data.member",
    "team_data.content.read",
    "team_data.content.write",
    "team_data.team_space",
    "team_data.governance.read",
    "team_data.governance.write",
    "members.read",
    "members.write",
    "members.delete",
    "groups.read",
    "groups.write",
    "events.read",
    "sessions.list",
    "sessions.modify",
    "files.team_metadata.read",
    "files.team_metadata.write",
}


def is_team_scopes(scopes: list[str]) -> bool:
    return any(s in TEAM_SCOPES for s in scopes)


def mint(
    ctx: ProviderContext,
    acct: dict[str, Any],
    *,
    client_id: str,
    scopes: list[str],
    team: bool,
    offline: bool,
    ttl: int = ACCESS_TTL,
) -> dict[str, Any]:
    """Issue an access token (+ refresh token when ``offline``) and build the token response."""
    refresh = None
    if offline:
        refresh = ctx.tokens.issue_refresh(
            client_id=client_id,
            user_id=acct["person"],
            scopes=scopes,
            ttl=None,
            prefix="",
            length=64,
            extra={"team": team},
        )
    access = ctx.tokens.issue_access(
        client_id=client_id,
        user_id=acct["person"],
        scopes=scopes,
        ttl=timedelta(seconds=ttl),
        prefix="sl.B",
        length=136,
        extra={"team": team, "refresh": refresh.token if refresh else None},
    )
    body: dict[str, Any] = {
        "access_token": access.token,
        "token_type": "bearer",
        "expires_in": ttl,
    }
    if refresh:
        body["refresh_token"] = refresh.token
    body["scope"] = " ".join(scopes)
    body["uid"] = str(acct["uid"])
    body["account_id"] = acct["id"]
    if acct.get("is_team"):
        body["team_id"] = m.team_info(ctx)["id"]
    return body


def _token_error(exc: OAuthError) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if exc.status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="Dropbox"'
    return JSONResponse(
        {"error": exc.error, "error_description": exc.description}, status_code=exc.status, headers=headers
    )


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head>"
        f"<body style='font-family:system-ui;max-width:560px;margin:40px auto'>{body}</body></html>",
        status_code=status,
    )


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/oauth2/authorize")
    @app.get("/1/oauth2/authorize")
    async def authorize(request: Request) -> Response:
        q = request.query_params
        client_id = q.get("client_id")
        response_type = q.get("response_type", "code")
        redirect_uri = q.get("redirect_uri")
        state = q.get("state")
        if not client_id:
            return _page("Dropbox", "<h1>Error</h1><p>Missing client_id.</p>", 400)
        scopes = (q.get("scope") or "").split() or list(DEFAULT_SCOPES)
        team = is_team_scopes(scopes) or q.get("include_granted_scopes") == "team"
        if response_type not in ("code", "token"):
            if redirect_uri:
                url = redirect_with_params(
                    redirect_uri,
                    {"error": "unsupported_response_type", "error_description": "Invalid response_type", "state": state},
                )
                return RedirectResponse(url, status_code=302)
            return _page("Dropbox", "<h1>Error</h1><p>Invalid response_type.</p>", 400)
        hint = q.get("login_hint")
        acct = None
        if hint:
            acct = m.account_by_email(ctx, hint) or m.account_by_person(ctx, hint)
        if acct is None or acct["status"] == "suspended":
            people = [
                {"display_name": a["display_name"], "email": a["email"], "login_hint": a["email"]}
                for a in ctx.store.collection(m.ACCOUNTS)
                if a["status"] == "active" and (a["is_team"] or not team)
            ]
            base = external_base(request) + request.url.path
            rest = urlencode([(k, v) for k, v in q.multi_items() if k != "login_hint"])
            page = render_account_picker(
                provider_title="Dropbox",
                app_name=client_id,
                scopes=scopes,
                accounts=people,
                action_base=base + ("?" + rest if rest else ""),
            )
            return HTMLResponse(page)
        ctx.tokens.authenticate_client(client_id, None, require_secret=False)
        offline = q.get("token_access_type") == "offline"
        if response_type == "token":
            body = mint(ctx, acct, client_id=client_id, scopes=scopes, team=team, offline=False)
            fragment = {
                "access_token": body["access_token"],
                "token_type": "bearer",
                "expires_in": body["expires_in"],
                "uid": body["uid"],
                "account_id": body["account_id"],
                "scope": body["scope"],
                "state": state,
            }
            if "team_id" in body:
                fragment["team_id"] = body["team_id"]
            if not redirect_uri:
                return _page("Dropbox", "<h1>Access granted</h1>")
            return RedirectResponse(redirect_with_params(redirect_uri, fragment, fragment=True), status_code=302)
        grant = ctx.tokens.issue_code(
            client_id=client_id,
            user_id=acct["person"],
            scopes=scopes,
            redirect_uri=redirect_uri,
            code_challenge=q.get("code_challenge"),
            code_challenge_method=q.get("code_challenge_method"),
            extra={"offline": offline, "team": team},
        )
        if not redirect_uri:
            return _page(
                "Dropbox - access code",
                f"<h1>Enter this code into {html.escape(client_id)}</h1><p><code id='code'>{grant.token}</code></p>",
            )
        return RedirectResponse(
            redirect_with_params(redirect_uri, {"code": grant.token, "state": state}), status_code=302
        )

    @app.post("/oauth2/token")
    @app.post("/1/oauth2/token")
    async def token(request: Request) -> Response:
        raw = (await request.body()).decode("utf-8", errors="replace")
        form = {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        for k, v in request.query_params.items():
            form.setdefault(k, v)
        creds = basic_credentials(request)
        client_id = form.get("client_id") or (creds[0] if creds else None)
        secret = form.get("client_secret") or (creds[1] if creds else None)
        grant_type = form.get("grant_type")
        try:
            ctx.tokens.authenticate_client(client_id, secret, require_secret=False)
            if grant_type == "authorization_code":
                grant = ctx.tokens.redeem_code(
                    form.get("code"),
                    client_id=client_id,
                    redirect_uri=form.get("redirect_uri"),
                    code_verifier=form.get("code_verifier"),
                )
                acct = m.account_by_person(ctx, grant.user_id or "")
                if acct is None:
                    raise OAuthError("invalid_grant", "code is invalid or has expired")
                body = mint(
                    ctx,
                    acct,
                    client_id=client_id or "",
                    scopes=grant.scopes,
                    team=bool(grant.extra.get("team")),
                    offline=bool(grant.extra.get("offline")),
                )
            elif grant_type == "refresh_token":
                refresh = ctx.tokens.resolve(form.get("refresh_token"), "refresh")
                if refresh is None:
                    raise OAuthError("invalid_grant", "refresh token is invalid or revoked")
                if client_id and refresh.client_id != client_id:
                    raise OAuthError("invalid_grant", "refresh token was issued to a different client")
                acct = m.account_by_person(ctx, refresh.user_id or "")
                if acct is None:
                    raise OAuthError("invalid_grant", "refresh token is invalid or revoked")
                scopes = (form.get("scope") or "").split() or list(refresh.scopes)
                access = ctx.tokens.issue_access(
                    client_id=refresh.client_id,
                    user_id=refresh.user_id,
                    scopes=scopes,
                    ttl=timedelta(seconds=ACCESS_TTL),
                    prefix="sl.B",
                    length=136,
                    extra={"team": bool(refresh.extra.get("team")), "refresh": refresh.token},
                )
                body = {
                    "access_token": access.token,
                    "token_type": "bearer",
                    "expires_in": ACCESS_TTL,
                    "scope": " ".join(scopes),
                }
            elif grant_type is None:
                raise OAuthError("invalid_request", "missing required parameter: grant_type")
            else:
                raise OAuthError("unsupported_grant_type", f"unsupported grant_type: {grant_type}")
        except OAuthError as exc:
            return _token_error(exc)
        return JSONResponse(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
