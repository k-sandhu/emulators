"""Token handling, scopes, request encodings and the response envelope of the Web API."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .conftest import Slack

SLACK = "https://slack.com"


async def test_missing_and_invalid_tokens(slack: Slack) -> None:
    r = await slack.client.post(f"{SLACK}/api/auth.test")
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "not_authed"}
    r = await slack.client.post(f"{SLACK}/api/auth.test", headers={"Authorization": "Bearer xoxb-nope"})
    assert r.json() == {"ok": False, "error": "invalid_auth"}


async def test_token_in_form_and_query_parameters(slack: Slack) -> None:
    token = await slack.bot()
    form = await slack.client.post(f"{SLACK}/api/auth.test", data={"token": token})
    assert form.json()["ok"] is True
    query = await slack.client.get(f"{SLACK}/api/auth.test", params={"token": token})
    assert query.json()["ok"] is True


async def test_json_bodies_require_bearer_authorization(slack: Slack) -> None:
    token = await slack.bot()
    r = await slack.client.post(f"{SLACK}/api/auth.test", json={"token": token})
    assert r.json() == {"ok": False, "error": "not_authed"}
    r = await slack.client.post(
        f"{SLACK}/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        content=b'{"channel": "%s", "text": "from json"}' % slack.channel_id("general").encode(),
    )
    body = r.json()
    assert body["ok"] is True and "warning" not in body
    assert body["message"]["text"] == "from json"


async def test_missing_charset_warning_on_json_without_charset(slack: Slack) -> None:
    token = await slack.bot()
    r = await slack.client.post(
        f"{SLACK}/api/auth.test", headers={"Authorization": f"Bearer {token}"}, json={}
    )
    body = r.json()
    assert body["ok"] is True
    assert body["warning"] == "missing_charset"
    assert body["response_metadata"]["warnings"] == ["missing_charset"]


async def test_unknown_method_and_bad_post_types(slack: Slack) -> None:
    token = await slack.bot()
    auth = {"Authorization": f"Bearer {token}"}
    r = await slack.client.post(f"{SLACK}/api/nope.method", headers=auth)
    assert r.json()["error"] == "unknown_method"
    r = await slack.client.post(
        f"{SLACK}/api/auth.test", headers={**auth, "Content-Type": "text/plain"}, content=b"x"
    )
    assert r.json()["error"] == "invalid_post_type"
    r = await slack.client.post(f"{SLACK}/api/auth.test", headers=auth, content=b"abc")
    assert r.json()["error"] == "missing_post_type"
    r = await slack.client.post(
        f"{SLACK}/api/auth.test",
        headers={**auth, "Content-Type": "application/json; charset=utf-8"},
        content=b"{",
    )
    assert r.json()["error"] == "invalid_json"


async def test_auth_test_for_bot_and_user_tokens(slack: Slack) -> None:
    bot = await slack.ok("auth.test")
    assert bot["url"] == "https://halcyonadvisory.slack.com/"
    assert bot["team"] == "Halcyon Advisory Group"
    assert bot["team_id"] == slack.ctx.world.org.slack_team_id
    assert bot["user"] == "halcyon_bot" and bot["bot_id"].startswith("B")
    assert bot["user_id"].startswith("U") and bot["is_enterprise_install"] is False
    user = await slack.ok("auth.test", await slack.user_token("p001"))
    assert user["user"] == "eleanor.whitfield" and user["user_id"] == slack.uid("p001")
    assert "bot_id" not in user


async def test_missing_scope_reports_needed_and_provided(slack: Slack) -> None:
    readonly = (await slack.tokens())["bot_readonly"]
    body = await slack.api("chat.postMessage", readonly, channel=slack.channel_id("general"), text="x")
    assert body["ok"] is False and body["error"] == "missing_scope"
    assert body["needed"] == "chat:write"
    assert set(body["provided"].split(",")) == {"channels:read", "groups:read", "users:read", "team:read"}


async def test_scope_headers(slack: Slack) -> None:
    token = (await slack.tokens())["bot_readonly"]
    r = await slack.client.post(f"{SLACK}/api/users.list", headers={"Authorization": f"Bearer {token}"})
    assert r.headers["x-oauth-scopes"] == "channels:read,groups:read,users:read,team:read"
    assert r.headers["x-accepted-oauth-scopes"] == "users:read"
    assert r.headers["x-slack-req-id"]


async def test_token_type_restrictions(slack: Slack) -> None:
    assert await slack.err("search.messages", None, query="audit") == "not_allowed_token_type"
    app_token = (await slack.tokens())["app_level"]
    assert await slack.err("auth.test", app_token) == "not_allowed_token_type"
    assert await slack.err("users.profile.set", None, name="title", value="x") == "not_allowed_token_type"


async def test_deactivated_user_token_is_inactive(slack: Slack) -> None:
    token = await slack.user_token("p046")
    assert await slack.err("auth.test", token) == "account_inactive"


async def test_revoke_and_test_flag(slack: Slack) -> None:
    token = await slack.mint(user="p018")
    assert (await slack.ok("auth.revoke", token, test=True))["revoked"] is False
    assert (await slack.ok("auth.test", token))["user_id"] == slack.uid("p018")
    assert (await slack.ok("auth.revoke", token))["revoked"] is True
    assert await slack.err("auth.test", token) == "token_revoked"


async def test_expiring_tokens(slack: Slack) -> None:
    token = await slack.mint(user="p018", expires_in=60)
    assert (await slack.ok("auth.test", token))["ok"]
    slack.hub.clock.advance(timedelta(seconds=61))
    try:
        assert await slack.err("auth.test", token) == "token_expired"
    finally:
        slack.hub.clock.advance(timedelta(seconds=-61))


async def test_minted_tokens_have_slack_shapes_and_scopes(slack: Slack) -> None:
    r = await slack.client.post(f"{SLACK}/_emulator/tokens", json={"user": "p018", "scopes": ["users:read"]})
    body = r.json()
    assert body["access_token"].startswith("xoxp-") and body["scope"] == "users:read"
    assert await slack.err("chat.postMessage", body["access_token"], channel="x", text="y") == "missing_scope"
    bot = await slack.client.post(f"{SLACK}/_emulator/tokens", json={})
    assert bot.json()["access_token"].startswith("xoxb-") and bot.json()["bot_id"].startswith("B")
    app = await slack.client.post(f"{SLACK}/_emulator/tokens", json={"type": "app"})
    assert app.json()["access_token"].startswith("xapp-1-")
    missing = await slack.client.post(f"{SLACK}/_emulator/tokens", json={"user": "nobody@x.example"})
    assert missing.status_code == 404


async def test_api_test_echoes_arguments(slack: Slack) -> None:
    body = (await slack.client.post(f"{SLACK}/api/api.test", data={"foo": "bar"})).json()
    assert body == {"ok": True, "args": {"foo": "bar"}}
    failing = (
        await slack.client.post(f"{SLACK}/api/api.test", data={"error": "my_error", "foo": "1"})
    ).json()
    assert failing["ok"] is False and failing["error"] == "my_error" and failing["args"]["foo"] == "1"


async def test_rate_limit_fault_uses_http_429(slack: Slack) -> None:
    await slack.client.post(
        f"{SLACK}/_emulator/faults", json={"path": "/api/chat.postMessage", "status": 429, "times": 1}
    )
    token = await slack.bot()
    first = await slack.client.post(
        f"{SLACK}/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        data={"channel": slack.channel_id("general"), "text": "hi"},
    )
    assert first.status_code == 429
    assert first.headers["retry-after"] == "2"
    assert first.json() == {"ok": False, "error": "ratelimited"}
    second = await slack.ok("chat.postMessage", token, channel=slack.channel_id("general"), text="hi")
    assert second["ok"]


async def test_injected_error_body_is_passed_through(slack: Slack) -> None:
    await slack.client.post(
        f"{SLACK}/_emulator/faults",
        json={"path": "/api/users.list", "status": 200, "body": {"ok": False, "error": "internal_error"}},
    )
    r = await slack.client.post(
        f"{SLACK}/api/users.list", headers={"Authorization": f"Bearer {await slack.bot()}"}
    )
    assert r.status_code == 200 and r.json() == {"ok": False, "error": "internal_error"}


async def test_prefix_routing_through_gateway(slack: Slack) -> None:
    transport = httpx.ASGITransport(app=slack.hub.gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080") as client:
        r = await client.post(
            "/slack/api/auth.test", headers={"Authorization": f"Bearer {await slack.bot()}"}
        )
        assert r.json()["ok"] is True
        assert r.json()["url"] == "https://halcyonadvisory.slack.com/"
