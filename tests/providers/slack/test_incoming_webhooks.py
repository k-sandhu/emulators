"""Incoming webhooks on hooks.slack.com."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .conftest import Slack

HOOKS = "https://hooks.slack.com"


def hook_for(slack: Slack, channel: str) -> dict:
    cid = slack.channel_id(channel)
    return next(h for h in slack.ctx.store.collection("incoming_webhooks") if h["channel_id"] == cid)


async def test_seeded_webhooks_are_listed_in_info(slack: Slack) -> None:
    info = (await slack.client.get("https://slack.com/_emulator/info")).json()
    urls = info["credentials"]["incoming_webhooks"]
    assert len(urls) == 7
    assert all(
        u.startswith(f"https://hooks.slack.com/services/{slack.ctx.world.org.slack_team_id}/B") for u in urls
    )
    assert len({u.rsplit("/", 1)[1] for u in urls}) == len(urls)
    assert all(len(u.rsplit("/", 1)[1]) == 24 for u in urls)


async def test_post_text_message(slack: Slack) -> None:
    hook = hook_for(slack, "general")
    r = await slack.client.post(f"{HOOKS}/services/{hook['id']}", json={"text": "Deploy *finished*"})
    assert r.status_code == 200 and r.text == "ok" and r.headers["content-type"].startswith("text/plain")
    message = (await slack.history(slack.channel_id("general"), limit=1))[0]
    app = slack.ctx.store.collection("app_config").get("app")
    assert message["text"] == "Deploy *finished*" and message["subtype"] == "bot_message"
    assert message["bot_id"] == hook["bot_id"] and message["app_id"] == app["app_id"]
    assert message["username"] == "incoming-webhook" and "user" not in message
    assert message["bot_profile"]["name"] == "incoming-webhook"
    assert message["blocks"][0]["type"] == "rich_text"


async def test_post_blocks_attachments_and_customisation(slack: Slack) -> None:
    hook = hook_for(slack, "it-help")
    payload = {
        "text": "fallback",
        "username": "Pager",
        "icon_emoji": ":rotating_light:",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "*Alert*"}}],
        "attachments": [{"color": "danger", "text": "disk full"}],
    }
    assert (await slack.client.post(f"{HOOKS}/services/{hook['id']}", json=payload)).text == "ok"
    message = (await slack.history(slack.channel_id("it-help"), limit=1))[0]
    assert message["username"] == "Pager" and message["icons"] == {"emoji": ":rotating_light:"}
    assert message["blocks"][0]["type"] == "section" and message["blocks"][0]["block_id"]
    assert message["attachments"][0]["id"] == 1 and message["attachments"][0]["text"] == "disk full"
    icon = {"text": "x", "icon_url": "https://img.example/a.png"}
    await slack.client.post(f"{HOOKS}/services/{hook['id']}", json=icon)
    assert (await slack.history(slack.channel_id("it-help"), limit=1))[0]["icons"] == {
        "image_64": "https://img.example/a.png"
    }


async def test_form_encoded_payload_and_threads(slack: Slack) -> None:
    hook = hook_for(slack, "general")
    channel = slack.channel_id("general")
    parent = await slack.ok("chat.postMessage", channel=channel, text="parent")
    r = await slack.client.post(
        f"{HOOKS}/services/{hook['id']}",
        data={"payload": json.dumps({"text": "from form", "thread_ts": parent["ts"]})},
    )
    assert r.text == "ok"
    replies = (await slack.ok("conversations.replies", channel=channel, ts=parent["ts"]))["messages"]
    assert [m["text"] for m in replies] == ["parent", "from form"]


async def test_error_responses_are_plain_text_with_status_codes(slack: Slack) -> None:
    hook = hook_for(slack, "general")
    url = f"{HOOKS}/services/{hook['id']}"
    team = slack.ctx.world.org.slack_team_id
    unknown = await slack.client.post(f"{HOOKS}/services/{team}/BNOSUCHBOT/secret")
    assert (unknown.status_code, unknown.text) == (404, "no_service")
    assert (await slack.client.post(url, content=b"not json")).text == "invalid_payload"
    assert (await slack.client.post(url, content=b"not json")).status_code == 400
    assert (await slack.client.post(url, json=["list"])).text == "invalid_payload"
    empty = await slack.client.post(url, json={})
    assert (empty.status_code, empty.text) == (400, "no_text")
    assert (await slack.client.post(url, json={"text": 5})).text == "invalid_payload"
    assert (await slack.client.post(url, json={"blocks": [{"type": "bogus"}]})).text == "invalid_blocks"
    assert (await slack.client.post(url, json={"attachments": "x"})).text == "invalid_attachments"
    assert (await slack.client.post(url, json={"text": "x" * 40_001})).text == "msg_too_long"
    assert (await slack.client.get(url)).status_code == 405


async def test_archived_channel_returns_410(slack: Slack) -> None:
    created = (
        await slack.client.post(
            "https://slack.com/_emulator/actions/create_webhook", json={"channel": "general"}
        )
    ).json()
    path = created["url"].removeprefix(HOOKS)
    channel = (await slack.ok("conversations.create", name="short-lived"))["channel"]["id"]
    made = (
        await slack.client.post(
            "https://slack.com/_emulator/actions/create_webhook", json={"channel": channel}
        )
    ).json()
    await slack.ok("conversations.archive", channel=channel)
    gone = await slack.client.post(made["url"], json={"text": "hi"})
    assert (gone.status_code, gone.text) == (410, "channel_is_archived")
    assert (await slack.client.post(f"{HOOKS}{path}", json={"text": "still fine"})).text == "ok"


async def test_create_webhook_action(slack: Slack) -> None:
    created = (
        await slack.client.post(
            "https://slack.com/_emulator/actions/create_webhook",
            json={"channel": "bd-pipeline", "label": "CRM"},
        )
    ).json()
    assert created["ok"] and created["url"].startswith("https://hooks.slack.com/services/T")
    assert (await slack.client.post(created["url"], json={"text": "from CRM"})).text == "ok"
    assert (await slack.history(slack.channel_id("bd-pipeline"), limit=1))[0]["text"] == "from CRM"
    bot_id = created["url"].split("/")[-2]
    assert (await slack.ok("bots.info", bot=bot_id))["bot"]["name"] == "incoming-webhook"
    missing = await slack.client.post(
        "https://slack.com/_emulator/actions/create_webhook", json={"channel": "nope"}
    )
    assert missing.status_code == 404


async def test_webhooks_work_through_the_prefix_gateway(slack: Slack) -> None:
    hook = hook_for(slack, "general")
    transport = httpx.ASGITransport(app=slack.hub.gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080") as client:
        r = await client.post(f"/slack/services/{hook['id']}", json={"text": "via prefix"})
        assert r.text == "ok"
    assert (await slack.history(slack.channel_id("general"), limit=1))[0]["text"] == "via prefix"
