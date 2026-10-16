"""Slash commands, block actions, modals (views.*) and response_url."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from slack_sdk.signature import SignatureVerifier

if TYPE_CHECKING:
    from .conftest import Slack

SLACK = "https://slack.com"
URL = "http://consumer.test/slack/interact"


class Consumer:
    """An app endpoint that records signed requests and answers with a configurable body."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply: Any = None
        self.app = FastAPI()

        @self.app.post("/slack/interact")
        async def interact(request: Request) -> Response:
            body = await request.body()
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            self.requests.append({"headers": dict(request.headers), "body": body, "form": form})
            if self.reply is None:
                return Response(status_code=200)
            if isinstance(self.reply, str):
                return PlainTextResponse(self.reply)
            return JSONResponse(self.reply)

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.last["form"]["payload"])


@pytest.fixture
def consumer(slack: Slack) -> Consumer:
    app = Consumer()
    slack.hub.webhook_transport = httpx.ASGITransport(app=app.app)
    return app


async def action(slack: Slack, action_name: str, /, **payload: Any) -> dict[str, Any]:
    r = await slack.client.post(f"{SLACK}/_emulator/actions/{action_name}", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def verify(slack: Slack, request: dict[str, Any]) -> bool:
    secret = slack.ctx.store.collection("app_config").get("app")["signing_secret"]
    return SignatureVerifier(secret).is_valid_request(request["body"], request["headers"])


def hook_path(url: str) -> str:
    return url.removeprefix("https://hooks.slack.com")


async def test_slash_command_request_is_signed_and_complete(slack: Slack, consumer: Consumer) -> None:
    result = await action(
        slack,
        "slash_command",
        user="p018",
        channel="general",
        command="/ticket",
        text="open printer",
        request_url=URL,
    )
    assert result["status"] == "delivered" and result["response_status"] == 200
    form = consumer.last["form"]
    app = slack.ctx.store.collection("app_config").get("app")
    assert verify(slack, consumer.last)
    assert consumer.last["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert form["command"] == "/ticket" and form["text"] == "open printer"
    assert form["user_id"] == slack.uid("p018") and form["user_name"] == "maya.chen"
    assert form["channel_id"] == slack.channel_id("general") and form["channel_name"] == "general"
    assert form["team_id"] == slack.ctx.world.org.slack_team_id and form["team_domain"] == "halcyonadvisory"
    assert form["api_app_id"] == app["app_id"] and form["token"] == app["verification_token"]
    assert form["is_enterprise_install"] == "false"
    assert form["response_url"].startswith(f"https://hooks.slack.com/commands/{form['team_id']}/")
    assert form["trigger_id"] == result["trigger_id"]
    journal = (
        await slack.client.get(f"{SLACK}/_emulator/webhooks", params={"event_type": "slash_command"})
    ).json()
    assert journal["count"] == 1 and journal["deliveries"][0]["status"] == "delivered"


async def test_slash_command_immediate_replies(slack: Slack, consumer: Consumer) -> None:
    general = slack.channel_id("general")
    consumer.reply = "only you can see this"
    plain = await action(
        slack, "slash_command", user="p018", channel="general", command="/echo", request_url=URL
    )
    assert plain["applied"] == "ephemeral"
    stored = [e for e in slack.ctx.store.collection("ephemeral_messages") if e["user"] == slack.uid("p018")]
    assert stored[-1]["text"] == "only you can see this" and stored[-1]["channel"] == general
    consumer.reply = {"response_type": "in_channel", "text": "visible to everyone"}
    shared = await action(
        slack, "slash_command", user="p018", channel="general", command="/echo", request_url=URL
    )
    assert shared["applied"].startswith("in_channel:")
    newest = (await slack.history(general, limit=1))[0]
    assert (
        newest["text"] == "visible to everyone"
        and newest["bot_id"]
        and newest["ts"] == shared["applied"].split(":")[1]
    )
    consumer.reply = None
    assert (
        await action(slack, "slash_command", user="p018", channel="general", command="/echo", request_url=URL)
    )["applied"] is None
    bad = await slack.client.post(
        f"{SLACK}/_emulator/actions/slash_command",
        json={"user": "p018", "channel": "general", "command": "echo", "request_url": URL},
    )
    assert bad.status_code == 400
    missing_url = await slack.client.post(
        f"{SLACK}/_emulator/actions/slash_command",
        json={"user": "p018", "channel": "general", "command": "/echo"},
    )
    assert missing_url.status_code == 400
    wrong_path = await action(
        slack,
        "slash_command",
        user="p018",
        channel="general",
        command="/echo",
        request_url="http://consumer.test/other",
    )
    assert (
        wrong_path["status"] == "failed"
        and wrong_path["response_status"] == 404
        and wrong_path["applied"] is None
    )


async def test_response_url_behaviours(slack: Slack, consumer: Consumer) -> None:
    general = slack.channel_id("general")
    result = await action(
        slack, "slash_command", user="p018", channel="general", command="/status", request_url=URL
    )
    url = result["response_url"]
    channel_messages = len(await slack.history(general, limit=999))
    ephemeral = await slack.client.post(url, json={"text": "just for you"})
    assert (ephemeral.status_code, ephemeral.text) == (200, "ok")
    assert len(await slack.history(general, limit=999)) == channel_messages
    posted = await slack.client.post(url, json={"text": "for the channel", "response_type": "in_channel"})
    assert posted.text == "ok"
    assert (await slack.history(general, limit=1))[0]["text"] == "for the channel"
    assert (await slack.client.post(url, content=b"nope")).text == "invalid_payload"
    assert (await slack.client.post(url, json={})).text == "no_text"
    for number in ("third", "fourth", "fifth"):
        assert (await slack.client.post(url, json={"text": number})).text == "ok"
    exhausted = await slack.client.post(url, json={"text": "sixth"})
    assert (exhausted.status_code, exhausted.text) == (400, "used_url")
    unknown = await slack.client.post("https://hooks.slack.com/commands/T0/1/xyz", json={"text": "x"})
    assert (unknown.status_code, unknown.text) == (404, "no_service")
    other = await action(
        slack, "slash_command", user="p018", channel="general", command="/status", request_url=URL
    )
    slack.hub.clock.advance(timedelta(minutes=31))
    try:
        assert (await slack.client.post(other["response_url"], json={"text": "late"})).text == "used_url"
    finally:
        slack.hub.clock.advance(timedelta(minutes=-31))


async def test_block_actions_payload_and_replace_original(slack: Slack, consumer: Consumer) -> None:
    general = slack.channel_id("general")
    blocks = [
        {
            "type": "actions",
            "block_id": "approve",
            "elements": [
                {
                    "type": "button",
                    "action_id": "approve_btn",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": "inv-1043",
                }
            ],
        }
    ]
    posted = await slack.ok("chat.postMessage", channel=general, text="Approve invoice 1043?", blocks=blocks)
    result = await action(
        slack,
        "interact",
        user="p001",
        channel="general",
        message_ts=posted["ts"],
        actions=[
            {
                "action_id": "approve_btn",
                "block_id": "approve",
                "value": "inv-1043",
                "text": {"type": "plain_text", "text": "Approve"},
            }
        ],
        request_url=URL,
    )
    assert result["status"] == "delivered" and verify(slack, consumer.last)
    payload = consumer.payload
    assert payload["type"] == "block_actions" and payload["user"]["id"] == slack.uid("p001")
    assert (
        payload["user"]["username"] == "eleanor.whitfield" and payload["team"]["domain"] == "halcyonadvisory"
    )
    assert payload["container"] == {
        "type": "message",
        "message_ts": posted["ts"],
        "channel_id": general,
        "is_ephemeral": False,
    }
    assert payload["channel"]["id"] == general and payload["message"]["ts"] == posted["ts"]
    (clicked,) = payload["actions"]
    assert (
        clicked["action_id"] == "approve_btn"
        and clicked["value"] == "inv-1043"
        and clicked["type"] == "button"
    )
    assert clicked["action_ts"] and payload["trigger_id"] and payload["api_app_id"]
    assert payload["response_url"].startswith("https://hooks.slack.com/actions/")
    replaced = await slack.client.post(
        payload["response_url"], json={"replace_original": True, "text": "Approved by Eleanor"}
    )
    assert replaced.text == "ok"
    shown = (await slack.history(general, limit=1))[0]
    assert shown["ts"] == posted["ts"] and shown["text"] == "Approved by Eleanor" and shown["edited"]
    deleted = await slack.client.post(payload["response_url"], json={"delete_original": True})
    assert deleted.text == "ok"
    assert posted["ts"] not in {m["ts"] for m in await slack.history(general, limit=999)}
    missing = await slack.client.post(
        f"{SLACK}/_emulator/actions/interact", json={"user": "p001", "channel": "general", "request_url": URL}
    )
    assert missing.status_code == 400


async def test_default_interactivity_url(slack: Slack, consumer: Consumer) -> None:
    await action(slack, "configure_interactivity", interactivity_url=URL)
    await action(slack, "interact", user="p001", channel="general", actions=[{"action_id": "go"}])
    assert consumer.payload["actions"][0]["action_id"] == "go"


async def open_modal(slack: Slack, token: str | None = None, **extra: Any) -> dict[str, Any]:
    trigger = (await action(slack, "trigger_id", user="p018"))["trigger_id"]
    view = {
        "type": "modal",
        "callback_id": "ticket_form",
        "title": {"type": "plain_text", "text": "New ticket"},
        "submit": {"type": "plain_text", "text": "Create"},
        "private_metadata": "ctx-1",
        "blocks": [
            {
                "type": "input",
                "block_id": "title",
                "label": {"type": "plain_text", "text": "Title"},
                "element": {"type": "plain_text_input", "action_id": "value"},
            }
        ],
        **extra,
    }
    return await slack.ok("views.open", token, trigger_id=trigger, view=view)


async def test_views_open_update_push(slack: Slack) -> None:
    opened = (await open_modal(slack))["view"]
    app = slack.ctx.store.collection("app_config").get("app")
    assert (
        opened["id"].startswith("V") and opened["type"] == "modal" and opened["callback_id"] == "ticket_form"
    )
    assert opened["team_id"] == slack.ctx.world.org.slack_team_id and opened["app_id"] == app["app_id"]
    assert opened["root_view_id"] == opened["id"] and opened["previous_view_id"] is None
    assert opened["state"] == {"values": {}} and opened["hash"] and opened["private_metadata"] == "ctx-1"
    assert opened["blocks"][0]["block_id"] == "title" and opened["close"] is None
    assert "_user" not in opened
    new_view = {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Updated"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "done"}}],
    }
    updated = (await slack.ok("views.update", view_id=opened["id"], view=new_view, hash=opened["hash"]))[
        "view"
    ]
    assert (
        updated["id"] == opened["id"]
        and updated["title"]["text"] == "Updated"
        and updated["hash"] != opened["hash"]
    )
    assert (
        await slack.err("views.update", view_id=opened["id"], view=new_view, hash=opened["hash"])
        == "hash_conflict"
    )
    assert await slack.err("views.update", view_id="VNOSUCH", view=new_view) == "not_found"
    trigger = (await action(slack, "trigger_id", user="p018"))["trigger_id"]
    pushed = (await slack.ok("views.push", trigger_id=trigger, view=new_view))["view"]
    assert pushed["previous_view_id"] == opened["id"] and pushed["root_view_id"] == opened["id"]
    by_external = await open_modal(slack, external_id="ext-1")
    renamed = (await slack.ok("views.update", external_id="ext-1", view=new_view))["view"]
    assert renamed["id"] == by_external["view"]["id"]


async def test_trigger_id_rules(slack: Slack) -> None:
    view = {"type": "modal", "title": {"type": "plain_text", "text": "t"}, "blocks": [{"type": "divider"}]}
    assert await slack.err("views.open", trigger_id="not-a-trigger", view=view) == "invalid_trigger_id"
    assert await slack.err("views.open", trigger_id="1.2.abcdef", view=view) == "invalid_trigger_id"
    assert await slack.err("views.open", view=view) == "invalid_arguments"
    trigger = (await action(slack, "trigger_id", user="p018"))["trigger_id"]
    await slack.ok("views.open", trigger_id=trigger, view=view)
    assert await slack.err("views.open", trigger_id=trigger, view=view) == "exchanged_trigger_id"
    stale = (await action(slack, "trigger_id", user="p018"))["trigger_id"]
    slack.hub.clock.advance(timedelta(seconds=45))
    try:
        assert await slack.err("views.open", trigger_id=stale, view=view) == "expired_trigger_id"
    finally:
        slack.hub.clock.advance(timedelta(seconds=-45))
    fresh = (await action(slack, "trigger_id", user="p018"))["trigger_id"]
    assert (
        await slack.err(
            "views.open", trigger_id=fresh, view={"type": "modal", "blocks": [{"type": "divider"}]}
        )
        == "invalid_arguments"
    )
    assert await slack.err("views.open", trigger_id=fresh, view={"type": "bogus"}) == "invalid_arguments"
    assert await slack.err("views.open", trigger_id=fresh, view="nope") == "invalid_arguments"
    long_title = {
        "type": "modal",
        "title": {"type": "plain_text", "text": "x" * 25},
        "blocks": [{"type": "divider"}],
    }
    assert await slack.err("views.open", trigger_id=fresh, view=long_title) == "invalid_arguments"


async def test_views_publish_home_tab(slack: Slack) -> None:
    maya = slack.uid("p018")
    home = {"type": "home", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Welcome"}}]}
    first = (await slack.ok("views.publish", user_id=maya, view=home))["view"]
    assert first["type"] == "home" and first["id"].startswith("V")
    again = (await slack.ok("views.publish", user_id=maya, view=home, hash=first["hash"]))["view"]
    assert again["id"] != first["id"]
    assert await slack.err("views.publish", user_id=maya, view=home, hash=first["hash"]) == "hash_conflict"
    assert await slack.err("views.publish", user_id="UNOSUCH", view=home) == "user_not_found"
    assert (
        await slack.err(
            "views.publish",
            user_id=maya,
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "x"},
                "blocks": [{"type": "divider"}],
            },
        )
        == "invalid_arguments"
    )


async def test_view_submission_and_close(slack: Slack, consumer: Consumer) -> None:
    opened = (await open_modal(slack, notify_on_close=True))["view"]
    consumer.reply = {"response_action": "errors", "errors": {"title": "Too short"}}
    submitted = await action(
        slack,
        "submit_view",
        view_id=opened["id"],
        user="p018",
        values={"title": {"value": {"type": "plain_text_input", "value": "x"}}},
        request_url=URL,
    )
    assert submitted["delivered"] is True and submitted["response_json"] == {
        "response_action": "errors",
        "errors": {"title": "Too short"},
    }
    assert verify(slack, consumer.last)
    payload = consumer.payload
    assert payload["type"] == "view_submission" and payload["user"]["id"] == slack.uid("p018")
    assert payload["view"]["id"] == opened["id"] and payload["view"]["callback_id"] == "ticket_form"
    assert payload["view"]["state"]["values"]["title"]["value"]["value"] == "x"
    assert payload["trigger_id"] and payload["response_urls"] == []
    closed = await action(slack, "close_view", callback_id="ticket_form", user="p018", request_url=URL)
    assert closed["delivered"] is True and consumer.payload["type"] == "view_closed"
    quiet = (await open_modal(slack))["view"]
    skipped = await action(slack, "close_view", view_id=quiet["id"], user="p018", request_url=URL)
    assert skipped["delivered"] is False
    missing = await slack.client.post(
        f"{SLACK}/_emulator/actions/submit_view", json={"view_id": "VNOSUCH", "request_url": URL}
    )
    assert missing.status_code == 404
