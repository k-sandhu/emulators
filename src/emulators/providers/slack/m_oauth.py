"""OAuth v2 (install), Sign in with Slack (OpenID Connect) and the auth.* / api.test methods."""

from __future__ import annotations

import base64
import hashlib
import html
import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from emulators.core.auth import OAuthError, basic_credentials, redirect_with_params, render_account_picker
from emulators.core.provider import ProviderContext, external_base, url_for_host
from emulators.providers.slack import access
from emulators.providers.slack.incoming import create_webhook
from emulators.providers.slack.runtime import APP, BOT, USER, Call, SlackError, api
from emulators.providers.slack.seed_users import BOT_SCOPES
from emulators.providers.slack.util import WORKSPACE_DOMAIN, WORKSPACE_URL, digits_from

ROTATION_TTL = timedelta(hours=12)
OIDC_SCOPES = ("openid", "email", "profile")


def _split_scopes(raw: str | None) -> list[str]:
    return [s for s in re.split(r"[,\s]+", raw or "") if s]


def _team_digits(ctx: ProviderContext) -> str:
    return digits_from(access.team_id(ctx), "team", length=11)


def new_bot_token(ctx: ProviderContext, *, rotating: bool = False) -> str:
    if rotating:
        return "xoxe.xoxb-1-" + ctx.ids.urlsafe(120)
    return f"xoxb-{_team_digits(ctx)}-{ctx.ids.digits(13)}-{ctx.ids.base62(24)}"


def new_user_token(ctx: ProviderContext, *, rotating: bool = False) -> str:
    if rotating:
        return "xoxe.xoxp-1-" + ctx.ids.urlsafe(120)
    return f"xoxp-{_team_digits(ctx)}-{ctx.ids.digits(12)}-{ctx.ids.digits(13)}-{ctx.ids.hex(32)}"


def _auth_error(error: OAuthError) -> SlackError:
    text = error.description.lower()
    if "secret" in text:
        return SlackError("bad_client_secret")
    if "redirect_uri" in text:
        return SlackError("bad_redirect_uri")
    if "different client" in text:
        return SlackError("invalid_client_id")
    if "unknown client" in text or "client_id" in text:
        return SlackError("invalid_client_id")
    return SlackError("invalid_code")


def authenticate_app(call: Call) -> tuple[str, dict[str, Any]]:
    """Client credentials from Basic auth or the form; the seeded app plus any lenient client."""
    ctx, p = call.ctx, call.params
    basic = basic_credentials(call.request)
    client_id = p.str("client_id") or (basic[0] if basic else None)
    secret = p.str("client_secret") or (basic[1] if basic else None)
    if not client_id:
        raise SlackError("invalid_client_id")
    try:
        ctx.tokens.authenticate_client(client_id, secret)
    except OAuthError as exc:
        raise _auth_error(exc) from None
    config = access.app_config(ctx)
    if not ctx.tokens.lenient_clients and secret != (ctx.tokens.client(client_id) or {}).get("client_secret"):
        raise SlackError("bad_client_secret")
    return client_id, config


def _issue_user_tokens(
    ctx: ProviderContext, client_id: str, config: dict[str, Any], uid: str, scopes: list[str], *, kind: str
) -> dict[str, Any]:
    """Issue a bot or user access token (rotating when the app opted in); returns wire fields."""
    rotating = bool(config.get("token_rotation")) and kind in (BOT, USER)
    extra = {"kind": kind, "slack_user": uid, "app_id": config["app_id"], "bot_id": config["bot_id"]}
    token = new_bot_token(ctx, rotating=rotating) if kind == BOT else new_user_token(ctx, rotating=rotating)
    grant = ctx.tokens.issue_access(
        token=token,
        client_id=client_id,
        user_id=uid if kind == USER else None,
        scopes=scopes,
        ttl=ROTATION_TTL if rotating else None,
        app_only=kind != USER,
        extra=extra,
    )
    out: dict[str, Any] = {"access_token": grant.token}
    if rotating:
        refresh = ctx.tokens.issue_refresh(
            token="xoxe-1-" + ctx.ids.urlsafe(120),
            client_id=client_id,
            user_id=uid if kind == USER else None,
            scopes=scopes,
            ttl=None,
            extra=extra,
        )
        out.update(refresh_token=refresh.token, expires_in=int(ROTATION_TTL.total_seconds()))
    return out


def _team_stub(ctx: ProviderContext) -> dict[str, str]:
    return {"id": access.team_id(ctx), "name": ctx.world.org.name}


@api("oauth.v2.access", auth=False)
async def oauth_v2_access(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    client_id, config = authenticate_app(call)
    grant_type = p.str("grant_type", "authorization_code")
    if grant_type == "refresh_token":
        return _refresh(call, client_id, config)
    if grant_type != "authorization_code":
        raise SlackError("invalid_grant_type")
    code = p.str("code")
    if not code:
        raise SlackError("invalid_code")
    try:
        grant = ctx.tokens.redeem_code(
            code,
            client_id=client_id,
            redirect_uri=p.str("redirect_uri"),
            code_verifier=p.str("code_verifier"),
        )
    except OAuthError as exc:
        raise _auth_error(exc) from None
    uid = grant.extra["slack_user"]
    bot_scopes = list(grant.scopes)
    user_scopes = list(grant.extra.get("user_scopes", []))
    body: dict[str, Any] = {"app_id": config["app_id"]}
    authed: dict[str, Any] = {"id": uid}
    if user_scopes:
        issued = _issue_user_tokens(ctx, client_id, config, uid, user_scopes, kind=USER)
        authed.update(scope=",".join(user_scopes), token_type="user", **issued)
    body["authed_user"] = authed
    if bot_scopes:
        issued = _issue_user_tokens(ctx, client_id, config, config["bot_user_id"], bot_scopes, kind=BOT)
        body.update(scope=",".join(bot_scopes), token_type="bot", bot_user_id=config["bot_user_id"], **issued)
    body.update(team=_team_stub(ctx), enterprise=None, is_enterprise_install=False)
    if "incoming-webhook" in bot_scopes:
        channel = access.channel_by_id(ctx, grant.extra.get("channel")) or access.channel_by_name(
            ctx, "general"
        )
        if channel is not None and access.channel_kind(channel) == "public":
            hook = create_webhook(
                ctx,
                channel["id"],
                created_by=uid,
                created=int(ctx.clock.now().timestamp()),
                label=f"Installed by {ctx.world.org.name}",
                seed=ctx.ids.hex(8),
            )
            body["incoming_webhook"] = {
                "channel": "#" + channel["name"],
                "channel_id": channel["id"],
                "configuration_url": f"{WORKSPACE_URL}/services/{hook['bot_id']}",
                "url": url_for_host(call.request, ctx, "hooks.slack.com", f"/services/{hook['id']}"),
            }
    return body


def _refresh(call: Call, client_id: str, config: dict[str, Any]) -> dict[str, Any]:
    ctx = call.ctx
    if not config.get("token_rotation"):
        raise SlackError("invalid_refresh_token")
    refresh = ctx.tokens.resolve(call.params.str("refresh_token"), "refresh")
    if refresh is None:
        raise SlackError("invalid_refresh_token")
    ctx.tokens.revoke(refresh.token)
    extra = refresh.extra
    kind = extra.get("kind", BOT)
    uid = extra["slack_user"]
    issued = _issue_user_tokens(ctx, client_id, config, uid, list(refresh.scopes), kind=kind)
    body: dict[str, Any] = {
        "app_id": config["app_id"],
        "token_type": "user" if kind == USER else "bot",
        "scope": ",".join(refresh.scopes),
        **issued,
        "team": _team_stub(ctx),
        "enterprise": None,
        "is_enterprise_install": False,
    }
    if kind == BOT:
        body["bot_user_id"] = uid
    return body


# -- authorize pages ---------------------------------------------------------------------------


def _page(title: str, message: str, status: int = 400) -> HTMLResponse:
    body = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head>"
        f"<body><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>"
    )
    return HTMLResponse(body, status_code=status)


def _clean_path(request: Request) -> str:
    path = request.url.path
    root = request.scope.get("root_path", "")
    return path[len(root) :] if root and path.startswith(root) else path


def _login_user(ctx: ProviderContext, hint: str) -> dict[str, Any] | None:
    user = access.resolve_user_ref(ctx, hint)
    if user is None:
        wanted = hint.lower()
        user = next((u for u in access.col(ctx, access.USERS) if u["name"].lower() == wanted), None)
    if user is None or user.get("deleted") or user.get("is_bot") or user["id"] == access.SLACKBOT:
        return None
    return user


def authorize(ctx: ProviderContext, request: Request, *, openid: bool) -> Response:
    """``/oauth/v2/authorize`` and ``/openid/connect/authorize``: headless with ``login_hint``."""
    q = request.query_params
    client_id = q.get("client_id")
    if not client_id:
        return _page("invalid_client_id", "The client_id parameter is missing.")
    team = q.get("team")
    if team and team not in (access.team_id(ctx), WORKSPACE_DOMAIN):
        return _page("team_not_found", f"No workspace matches '{team}'.", 404)
    if openid:
        if q.get("response_type", "code") != "code":
            return _page("unsupported_response_type", "Only response_type=code is supported.")
        scopes = _split_scopes(q.get("scope")) or ["openid"]
        user_scopes: list[str] = []
        if "openid" not in scopes:
            return _page("invalid_scope", "The openid scope is required.")
    else:
        scopes = _split_scopes(q.get("scope"))
        user_scopes = _split_scopes(q.get("user_scope"))
        if not scopes and not user_scopes:
            scopes = list(BOT_SCOPES)
    redirect_uri = q.get("redirect_uri")
    state = q.get("state")
    hint = q.get("login_hint")
    if hint in ("deny", "access_denied") and redirect_uri:
        return RedirectResponse(
            redirect_with_params(redirect_uri, {"error": "access_denied", "state": state}), 302
        )
    if not hint:
        accounts = [
            {
                "display_name": u["real_name"],
                "email": u["profile"]["email"],
                "login_hint": u["profile"]["email"],
            }
            for u in access.col(ctx, access.USERS)
            if u.get("_person")
            and not u.get("deleted")
            and not u.get("is_stranger")
            and not u.get("is_restricted")
        ]
        base = {k: v for k, v in q.items() if k != "login_hint"}
        action = f"{external_base(request)}{_clean_path(request)}?{urlencode(base)}"
        page = render_account_picker(
            provider_title="Slack",
            app_name=access.app_config(ctx)["name"],
            scopes=[*scopes, *(f"user: {s}" for s in user_scopes)],
            accounts=accounts,
            action_base=action,
        )
        return HTMLResponse(page)
    user = _login_user(ctx, hint)
    if user is None:
        return _page("user_not_found", f"No active member matches login_hint '{hint}'.", 404)
    code = ctx.tokens.issue_code(
        client_id=client_id,
        user_id=user["id"],
        scopes=scopes,
        redirect_uri=redirect_uri,
        nonce=q.get("nonce"),
        extra={
            "slack_user": user["id"],
            "user_scopes": user_scopes,
            "channel": q.get("channel"),
            "openid": openid,
        },
    )
    if not redirect_uri:
        return _page("Authorization complete", f"code={code.token}", 200)
    return RedirectResponse(redirect_with_params(redirect_uri, {"code": code.token, "state": state}), 302)


# -- OpenID Connect ----------------------------------------------------------------------------


def _image_claims(user: dict[str, Any]) -> dict[str, str]:
    profile = user["profile"]
    return {
        f"https://slack.com/user_image_{size}": profile[f"image_{size}"]
        for size in (24, 32, 48, 72, 192, 512)
    }


def _identity_claims(ctx: ProviderContext, user: dict[str, Any], scopes: list[str]) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "sub": user["id"],
        "https://slack.com/user_id": user["id"],
        "https://slack.com/team_id": access.team_id(ctx),
    }
    if "email" in scopes:
        claims.update(
            email=user["profile"]["email"],
            email_verified=True,
            date_email_verified=user.get("updated", 0),
        )
    if "profile" in scopes:
        profile = user["profile"]
        claims.update(
            {
                "name": user["real_name"],
                "picture": profile["image_512"],
                "given_name": profile.get("first_name", ""),
                "family_name": profile.get("last_name", ""),
                "locale": user.get("locale", "en-US"),
                "https://slack.com/team_name": ctx.world.org.name,
                "https://slack.com/team_domain": WORKSPACE_DOMAIN,
                **_image_claims(user),
            }
        )
    return claims


@api("openid.connect.token", auth=False)
async def openid_token(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    client_id, config = authenticate_app(call)
    if p.str("grant_type", "authorization_code") != "authorization_code":
        raise SlackError("invalid_grant_type")
    code = p.str("code")
    if not code:
        raise SlackError("invalid_code")
    try:
        grant = ctx.tokens.redeem_code(
            code,
            client_id=client_id,
            redirect_uri=p.str("redirect_uri"),
            code_verifier=p.str("code_verifier"),
        )
    except OAuthError as exc:
        raise _auth_error(exc) from None
    uid = grant.extra["slack_user"]
    user = access.user_by_id(ctx, uid)
    assert user is not None
    token = new_user_token(ctx)
    ctx.tokens.issue_access(
        token=token,
        client_id=client_id,
        user_id=uid,
        scopes=list(grant.scopes),
        ttl=None,
        extra={
            "kind": USER,
            "slack_user": uid,
            "app_id": config["app_id"],
            "bot_id": config["bot_id"],
            "openid": True,
        },
    )
    now = ctx.clock.now()
    claims = {
        "iss": "https://slack.com",
        "aud": client_id,
        "iat": int(now.timestamp()),
        "auth_time": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "at_hash": base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest()[:16])
        .rstrip(b"=")
        .decode(),
        **_identity_claims(ctx, user, list(grant.scopes)),
    }
    if grant.nonce:
        claims["nonce"] = grant.nonce
    return {"access_token": token, "token_type": "Bearer", "id_token": ctx.signing_key.sign(claims)}


@api("openid.connect.userInfo", kinds=(USER,))
async def openid_userinfo(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    scopes = list(call.me.scopes)
    if "openid" not in scopes:
        raise SlackError("missing_scope", needed="openid", provided=",".join(scopes))
    user = access.actor_user(ctx, call.me)
    return _identity_claims(ctx, user, scopes)


def discovery_document(ctx: ProviderContext, request: Request) -> dict[str, Any]:
    def at(path: str) -> str:
        return url_for_host(request, ctx, "slack.com", path)

    return {
        "issuer": "https://slack.com",
        "authorization_endpoint": at("/openid/connect/authorize"),
        "token_endpoint": at("/api/openid.connect.token"),
        "userinfo_endpoint": at("/api/openid.connect.userInfo"),
        "jwks_uri": at("/openid/connect/keys"),
        "scopes_supported": list(OIDC_SCOPES),
        "response_types_supported": ["code"],
        "response_modes_supported": ["form_post"],
        "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "claims_supported": ["sub", "auth_time", "iss"],
        "claims_parameter_supported": False,
        "request_parameter_supported": False,
        "request_uri_parameter_supported": True,
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
    }


# -- auth.* and api.test -----------------------------------------------------------------------


@api("api.test", auth=False)
async def api_test(call: Call) -> dict[str, Any]:
    args = {k: v for k, v in call.params.data.items() if k != "token"}
    error = call.params.str("error")
    if error:
        raise SlackError(error, args=args)
    return {"args": args}


@api("auth.test", kinds=(BOT, USER))
async def auth_test(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    user = access.actor_user(ctx, call.me)
    body: dict[str, Any] = {
        "url": WORKSPACE_URL + "/",
        "team": ctx.world.org.name,
        "user": user["name"],
        "team_id": access.team_id(ctx),
        "user_id": user["id"],
        "is_enterprise_install": False,
    }
    if call.me.is_bot:
        body["bot_id"] = call.me.bot_id
    return body


@api("auth.revoke", kinds=(BOT, USER, APP))
async def auth_revoke(call: Call) -> dict[str, Any]:
    if call.params.bool("test"):
        return {"revoked": False}
    call.ctx.tokens.revoke(call.me.token)
    return {"revoked": True}


@api("auth.teams.list", kinds=(BOT, USER))
async def auth_teams_list(call: Call) -> dict[str, Any]:
    return {"teams": [_team_stub(call.ctx)], "response_metadata": {"next_cursor": ""}}
