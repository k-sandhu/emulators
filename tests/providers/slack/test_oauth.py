"""OAuth v2 install flow, token rotation and Sign in with Slack (OpenID Connect)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt

from emulators.core.hub import EmulatorHub

if TYPE_CHECKING:
    from .conftest import Slack

SLACK = "https://slack.com"
REDIRECT = "https://consumer.example/slack/callback"


def creds(slack: Slack) -> tuple[str, str]:
    app = slack.ctx.store.collection("app_config").get("app")
    return app["client_id"], app["client_secret"]


async def authorize(
    slack: Slack, hint: str | None = "maya.chen@halcyonadvisory.example", **params: Any
) -> httpx.Response:
    client_id, _ = creds(slack)
    query: dict[str, Any] = {"client_id": client_id, "redirect_uri": REDIRECT, "state": "xyz", **params}
    if hint:
        query["login_hint"] = hint
    return await slack.client.get(f"{SLACK}/oauth/v2/authorize", params=query)


def code_of(response: httpx.Response) -> str:
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT
    query = parse_qs(location.query)
    assert query["state"] == ["xyz"]
    return query["code"][0]


async def exchange(slack: Slack, code: str, **extra: Any) -> dict[str, Any]:
    client_id, secret = creds(slack)
    r = await slack.client.post(
        f"{SLACK}/api/oauth.v2.access",
        auth=(client_id, secret),
        data={"code": code, "redirect_uri": REDIRECT, **extra},
    )
    assert r.status_code == 200
    return r.json()


async def test_install_flow_returns_bot_and_user_tokens(slack: Slack) -> None:
    response = await authorize(slack, scope="chat:write,channels:read", user_scope="users:read,search:read")
    body = await exchange(slack, code_of(response))
    app = slack.ctx.store.collection("app_config").get("app")
    assert body["ok"] is True and body["app_id"] == app["app_id"]
    assert body["token_type"] == "bot" and body["access_token"].startswith("xoxb-")
    assert body["scope"] == "chat:write,channels:read" and body["bot_user_id"] == app["bot_user_id"]
    assert body["team"] == {"id": slack.ctx.world.org.slack_team_id, "name": "Halcyon Advisory Group"}
    assert body["enterprise"] is None and body["is_enterprise_install"] is False
    user = body["authed_user"]
    assert (
        user["id"] == slack.uid("p018")
        and user["token_type"] == "user"
        and user["access_token"].startswith("xoxp-")
    )
    assert user["scope"] == "users:read,search:read"
    assert "incoming_webhook" not in body
    bot_auth = await slack.ok("auth.test", body["access_token"])
    assert bot_auth["user_id"] == app["bot_user_id"] and bot_auth["bot_id"] == app["bot_id"]
    user_auth = await slack.ok("auth.test", user["access_token"])
    assert user_auth["user_id"] == slack.uid("p018") and "bot_id" not in user_auth
    scoped = await slack.api(
        "chat.postMessage", body["access_token"], channel=slack.channel_id("general"), text="hi"
    )
    assert scoped["ok"] is True
    assert await slack.err("users.list", body["access_token"]) == "missing_scope"
    assert (await slack.ok("search.messages", user["access_token"], query="coffee"))["ok"]


async def test_user_scope_only_install_and_no_user_scope_install(slack: Slack) -> None:
    user_only = await exchange(slack, code_of(await authorize(slack, user_scope="users:read")))
    assert "access_token" not in user_only and "bot_user_id" not in user_only
    assert user_only["authed_user"]["scope"] == "users:read"
    bot_only = await exchange(slack, code_of(await authorize(slack, scope="chat:write")))
    assert bot_only["authed_user"] == {"id": slack.uid("p018")}
    assert bot_only["access_token"].startswith("xoxb-")


async def test_login_hint_accepts_email_user_id_person_id_and_username(slack: Slack) -> None:
    for hint in ("maya.chen@halcyonadvisory.example", slack.uid("p018"), "p018", "maya.chen"):
        body = await exchange(slack, code_of(await authorize(slack, hint, scope="chat:write")))
        assert body["authed_user"]["id"] == slack.uid("p018"), hint


async def test_authorize_without_login_hint_renders_account_picker(slack: Slack) -> None:
    response = await authorize(slack, None, scope="chat:write")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    assert "login_hint=maya.chen%40halcyonadvisory.example" in response.text
    assert "Halcyon Bot" in response.text and "paul.henderson" not in response.text


async def test_authorize_errors(slack: Slack) -> None:
    client_id, _ = creds(slack)
    assert (await slack.client.get(f"{SLACK}/oauth/v2/authorize")).status_code == 400
    unknown = await authorize(slack, "nobody@halcyonadvisory.example", scope="chat:write")
    assert unknown.status_code == 404
    departed = await authorize(slack, slack.person_email("p046"), scope="chat:write")
    assert departed.status_code == 404
    other_team = await authorize(slack, scope="chat:write", team="TOTHERTEAM")
    assert other_team.status_code == 404
    denied = await authorize(slack, "deny", scope="chat:write")
    location = urlsplit(denied.headers["location"])
    assert parse_qs(location.query) == {"error": ["access_denied"], "state": ["xyz"]}
    no_redirect = await slack.client.get(
        f"{SLACK}/oauth/v2/authorize",
        params={"client_id": client_id, "login_hint": "p018", "scope": "chat:write"},
    )
    assert no_redirect.status_code == 200 and "code=" in no_redirect.text


async def test_code_exchange_errors(slack: Slack) -> None:
    client_id, secret = creds(slack)
    assert (await exchange(slack, "not-a-code"))["error"] == "invalid_code"
    code = code_of(await authorize(slack, scope="chat:write"))
    assert (await exchange(slack, code, redirect_uri="https://evil.example/cb"))[
        "error"
    ] == "bad_redirect_uri"
    code = code_of(await authorize(slack, scope="chat:write"))
    assert (await exchange(slack, code))["ok"] is True
    assert (await exchange(slack, code))["error"] == "invalid_code"
    r = await slack.client.post(
        f"{SLACK}/api/oauth.v2.access", data={"client_id": client_id, "client_secret": secret}
    )
    assert r.json()["error"] == "invalid_code"
    r = await slack.client.post(f"{SLACK}/api/oauth.v2.access", data={"code": "x"})
    assert r.json()["error"] == "invalid_client_id"
    r = await slack.client.post(
        f"{SLACK}/api/oauth.v2.access",
        data={"client_id": client_id, "client_secret": secret, "grant_type": "password"},
    )
    assert r.json()["error"] == "invalid_grant_type"
    other_client = code_of(await authorize(slack, scope="chat:write"))
    r = await slack.client.post(
        f"{SLACK}/api/oauth.v2.access",
        data={"client_id": "other.client", "client_secret": "s", "code": other_client},
    )
    assert r.json()["error"] == "invalid_client_id"


async def test_client_credentials_are_checked_in_strict_mode() -> None:
    hub = EmulatorHub(providers=["slack"], anchor="2026-10-16", lenient_clients=False)
    ctx = hub.context("slack")
    app = ctx.store.collection("app_config").get("app")
    async with httpx.AsyncClient(transport=hub.async_transport(), base_url=SLACK) as client:
        wrong = await client.post(
            "/api/oauth.v2.access", data={"client_id": app["client_id"], "client_secret": "bad", "code": "c"}
        )
        assert wrong.json()["error"] == "bad_client_secret"
        unknown = await client.post(
            "/api/oauth.v2.access", data={"client_id": "who.dis", "client_secret": "x", "code": "c"}
        )
        assert unknown.json()["error"] == "invalid_client_id"
        good = await client.post(
            "/api/oauth.v2.access",
            data={"client_id": app["client_id"], "client_secret": app["client_secret"], "code": "c"},
        )
        assert good.json()["error"] == "invalid_code"


async def test_incoming_webhook_scope_creates_a_webhook(slack: Slack) -> None:
    general = slack.channel_id("it-help")
    body = await exchange(
        slack, code_of(await authorize(slack, scope="chat:write,incoming-webhook", channel=general))
    )
    hook = body["incoming_webhook"]
    assert hook["channel"] == "#it-help" and hook["channel_id"] == general
    assert hook["url"].startswith("https://hooks.slack.com/services/T") and hook[
        "configuration_url"
    ].startswith("https://halcyonadvisory.slack.com/services/B")
    posted = await slack.client.post(hook["url"], json={"text": "installed!"})
    assert (posted.status_code, posted.text) == (200, "ok")
    assert (await slack.history(general, limit=1))[0]["text"] == "installed!"
    default = await exchange(slack, code_of(await authorize(slack, scope="incoming-webhook")))
    assert default["incoming_webhook"]["channel"] == "#general"


async def test_token_rotation_refresh_flow(slack: Slack) -> None:
    configured = await slack.client.post(
        f"{SLACK}/_emulator/actions/configure_app", json={"token_rotation": True}
    )
    assert configured.json()["token_rotation"] is True
    client_id, secret = creds(slack)
    body = await exchange(slack, code_of(await authorize(slack, scope="chat:write", user_scope="users:read")))
    assert body["access_token"].startswith("xoxe.xoxb-1-") and body["refresh_token"].startswith("xoxe-1-")
    assert body["expires_in"] == 43200
    assert (
        body["authed_user"]["access_token"].startswith("xoxe.xoxp-1-")
        and body["authed_user"]["refresh_token"]
    )
    assert (await slack.ok("auth.test", body["access_token"]))["ok"]
    r = await slack.client.post(
        f"{SLACK}/api/oauth.v2.access",
        data={
            "client_id": client_id,
            "client_secret": secret,
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
        },
    )
    refreshed = r.json()
    assert refreshed["ok"] is True and refreshed["token_type"] == "bot"
    assert (
        refreshed["access_token"] != body["access_token"]
        and refreshed["refresh_token"] != body["refresh_token"]
    )
    assert refreshed["expires_in"] == 43200 and refreshed["scope"] == "chat:write"
    again = await slack.client.post(
        f"{SLACK}/api/oauth.v2.access",
        data={
            "client_id": client_id,
            "client_secret": secret,
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
        },
    )
    assert again.json()["error"] == "invalid_refresh_token"
    user_refresh = await slack.client.post(
        f"{SLACK}/api/oauth.v2.access",
        data={
            "client_id": client_id,
            "client_secret": secret,
            "grant_type": "refresh_token",
            "refresh_token": body["authed_user"]["refresh_token"],
        },
    )
    assert user_refresh.json()["token_type"] == "user" and user_refresh.json()["access_token"].startswith(
        "xoxe.xoxp-1-"
    )
    slack.hub.clock.advance(timedelta(hours=13))
    try:
        assert await slack.err("auth.test", refreshed["access_token"]) == "token_expired"
    finally:
        slack.hub.clock.advance(timedelta(hours=-13))


async def test_refresh_requires_rotation_to_be_enabled(slack: Slack) -> None:
    client_id, secret = creds(slack)
    r = await slack.client.post(
        f"{SLACK}/api/oauth.v2.access",
        data={
            "client_id": client_id,
            "client_secret": secret,
            "grant_type": "refresh_token",
            "refresh_token": "xoxe-1-x",
        },
    )
    assert r.json()["error"] == "invalid_refresh_token"


# -- Sign in with Slack --------------------------------------------------------------------------


async def oidc_login(slack: Slack, scope: str = "openid email profile", **extra: Any) -> dict[str, Any]:
    client_id, secret = creds(slack)
    r = await slack.client.get(
        f"{SLACK}/openid/connect/authorize",
        params={
            "response_type": "code",
            "scope": scope,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "state": "xyz",
            "nonce": "n-0S6_WzA2Mj",
            "login_hint": "maya.chen@halcyonadvisory.example",
            **extra,
        },
    )
    code = code_of(r)
    token = await slack.client.post(
        f"{SLACK}/api/openid.connect.token",
        data={
            "client_id": client_id,
            "client_secret": secret,
            "code": code,
            "redirect_uri": REDIRECT,
            "grant_type": "authorization_code",
        },
    )
    return token.json()


async def test_sign_in_with_slack(slack: Slack) -> None:
    client_id, _ = creds(slack)
    token = await oidc_login(slack)
    assert (
        token["ok"] is True and token["token_type"] == "Bearer" and token["access_token"].startswith("xoxp-")
    )
    claims = slack.ctx.signing_key.verify(token["id_token"], audience=client_id)
    assert claims["iss"] == "https://slack.com" and claims["sub"] == slack.uid("p018")
    assert (
        claims["nonce"] == "n-0S6_WzA2Mj"
        and claims["https://slack.com/team_id"] == slack.ctx.world.org.slack_team_id
    )
    assert claims["email"] == slack.person_email("p018") and claims["email_verified"] is True
    assert (
        claims["name"] == "Maya Chen" and claims["given_name"] == "Maya" and claims["family_name"] == "Chen"
    )
    assert claims["https://slack.com/team_domain"] == "halcyonadvisory"
    header = jwt.get_unverified_header(token["id_token"])
    jwks = (await slack.client.get(f"{SLACK}/openid/connect/keys")).json()
    assert header["kid"] in {k["kid"] for k in jwks["keys"]} and header["alg"] == "RS256"
    info = (
        await slack.client.get(
            f"{SLACK}/api/openid.connect.userInfo",
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
    ).json()
    assert (
        info["ok"] is True
        and info["sub"] == slack.uid("p018")
        and info["email"] == slack.person_email("p018")
    )
    assert info["https://slack.com/user_id"] == slack.uid("p018") and info["picture"].startswith("https://")
    assert info["https://slack.com/user_image_192"].startswith("https://")
    assert info["name"] == "Maya Chen" and info["https://slack.com/team_name"] == "Halcyon Advisory Group"


async def test_openid_scopes_limit_claims(slack: Slack) -> None:
    token = await oidc_login(slack, scope="openid")
    info = (
        await slack.client.post(
            f"{SLACK}/api/openid.connect.userInfo",
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
    ).json()
    assert set(info) == {"ok", "sub", "https://slack.com/user_id", "https://slack.com/team_id"}
    email_only = await oidc_login(slack, scope="openid email")
    info = (
        await slack.client.post(
            f"{SLACK}/api/openid.connect.userInfo",
            headers={"Authorization": f"Bearer {email_only['access_token']}"},
        )
    ).json()
    assert info["email"] and "name" not in info
    plain = await slack.mint(user="p018", scopes=["users:read"])
    body = await slack.api("openid.connect.userInfo", plain)
    assert body["error"] == "missing_scope" and body["needed"] == "openid"
    assert await slack.err("openid.connect.userInfo", None) == "not_allowed_token_type"


async def test_openid_errors_and_discovery(slack: Slack) -> None:
    client_id, secret = creds(slack)
    bad = await slack.client.post(
        f"{SLACK}/api/openid.connect.token",
        data={"client_id": client_id, "client_secret": secret, "code": "nope"},
    )
    assert bad.json()["error"] == "invalid_code"
    no_openid = await slack.client.get(
        f"{SLACK}/openid/connect/authorize",
        params={
            "response_type": "code",
            "scope": "email",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "login_hint": "p018",
        },
    )
    assert no_openid.status_code == 400
    doc = (await slack.client.get(f"{SLACK}/.well-known/openid-configuration")).json()
    assert doc["issuer"] == "https://slack.com"
    assert doc["token_endpoint"] == "https://slack.com/api/openid.connect.token"
    assert doc["userinfo_endpoint"] == "https://slack.com/api/openid.connect.userInfo"
    assert doc["jwks_uri"] == "https://slack.com/openid/connect/keys"
    assert doc["id_token_signing_alg_values_supported"] == ["RS256"]


async def test_authorize_is_also_served_on_the_workspace_domain(slack: Slack) -> None:
    client_id, _ = creds(slack)
    r = await slack.client.get(
        "https://halcyonadvisory.slack.com/oauth/v2/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "state": "xyz",
            "scope": "chat:write",
            "login_hint": "p018",
        },
    )
    body = await exchange(slack, code_of(r))
    assert body["ok"] is True and body["authed_user"]["id"] == slack.uid("p018")
    legacy = await slack.client.get(
        "https://halcyonadvisory.slack.com/oauth",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "state": "xyz",
            "scope": "chat:write",
            "login_hint": "p018",
        },
    )
    assert legacy.status_code == 302


async def test_authorize_through_the_prefix_gateway_builds_picker_links(slack: Slack) -> None:
    client_id, _ = creds(slack)
    transport = httpx.ASGITransport(app=slack.hub.gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080") as client:
        page = await client.get(
            "/slack/oauth/v2/authorize",
            params={"client_id": client_id, "redirect_uri": REDIRECT, "scope": "chat:write"},
        )
    assert page.status_code == 200
    assert 'href="http://localhost:44080/slack/oauth/v2/authorize?' in page.text
