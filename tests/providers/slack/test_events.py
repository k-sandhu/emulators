"""Events API: URL verification, signed deliveries, subscriptions and retries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any

from slack_sdk.signature import SignatureVerifier

if TYPE_CHECKING:
    from .conftest import Receiver, Slack

SLACK = "https://slack.com"
URL = "http://consumer.test/slack/events"


async def configure(slack: Slack, **extra: Any) -> dict[str, Any]:
    r = await slack.client.post(
        f"{SLACK}/_emulator/actions/configure_events", json={"request_url": URL, **extra}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def action(slack: Slack, action_name: str, /, **payload: Any) -> dict[str, Any]:
    r = await slack.client.post(f"{SLACK}/_emulator/actions/{action_name}", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def of_type(receiver: Receiver, event_type: str) -> list[dict[str, Any]]:
    return [e for e in receiver.events if e["event"]["type"] == event_type]


async def test_url_verification_handshake(slack: Slack, receiver: Receiver) -> None:
    result = await configure(slack)
    assert result["verified"] is True and result["events"][0] == "message.channels"
    app = slack.ctx.store.collection("app_config").get("app")
    assert (
        result["signing_secret"] == app["signing_secret"]
        and result["verification_token"] == app["verification_token"]
    )
    (handshake,) = receiver.requests
    payload = handshake["payload"]
    assert payload["type"] == "url_verification" and payload["token"] == app["verification_token"]
    assert len(payload["challenge"]) > 20
    verifier = SignatureVerifier(app["signing_secret"])
    assert verifier.is_valid_request(handshake["body"], handshake["headers"])
    assert app["request_url"] == URL


async def test_challenge_may_be_plain_text(slack: Slack, receiver: Receiver) -> None:
    receiver.challenge_mode = "text"
    assert (await configure(slack))["verified"] is True


async def test_failed_verification_does_not_save_the_configuration(slack: Slack, receiver: Receiver) -> None:
    receiver.challenge_mode = "wrong"
    r = await slack.client.post(f"{SLACK}/_emulator/actions/configure_events", json={"request_url": URL})
    assert r.status_code == 400
    assert r.json()["error"] == "url_verification_failed" and "challenge" in r.json()["detail"]
    assert slack.ctx.store.collection("app_config").get("app")["request_url"] is None
    unreachable = await slack.client.post(
        f"{SLACK}/_emulator/actions/configure_events", json={"request_url": "http://nowhere.invalid:1/x"}
    )
    assert unreachable.status_code == 400
    skipped = await configure(slack, skip_verification=True)
    assert skipped["verified"] is False
    assert (
        await slack.client.post(f"{SLACK}/_emulator/actions/configure_events", json={})
    ).status_code == 400


async def test_message_event_envelope_and_signature(slack: Slack, receiver: Receiver) -> None:
    secret = "my-own-signing-secret"
    await configure(slack, signing_secret=secret)
    receiver.requests.clear()
    general = slack.channel_id("general")
    posted = await action(
        slack, "post_as_user", user="p018", channel="general", text=f"Hello <@{slack.uid('p001')}>"
    )
    assert posted["ok"] and posted["message"]["user"] == slack.uid("p018")
    (delivery,) = receiver.requests
    body = delivery["payload"]
    app = slack.ctx.store.collection("app_config").get("app")
    assert body["type"] == "event_callback" and body["token"] == app["verification_token"]
    assert body["team_id"] == slack.ctx.world.org.slack_team_id and body["api_app_id"] == app["app_id"]
    assert body["event_id"].startswith("Ev") and isinstance(body["event_time"], int)
    assert body["authorizations"] == [
        {
            "enterprise_id": None,
            "team_id": body["team_id"],
            "user_id": app["bot_user_id"],
            "is_bot": True,
            "is_enterprise_install": False,
        }
    ]
    assert body["is_ext_shared_channel"] is False
    context = json.loads(base64.urlsafe_b64decode(body["event_context"][2:] + "=="))
    assert body["event_context"].startswith("4-") and context == {
        "et": "message", "tid": body["team_id"], "aid": app["app_id"], "cid": general,
    }  # fmt: skip
    event = body["event"]
    assert event["type"] == "message" and event["channel"] == general and event["channel_type"] == "channel"
    assert event["user"] == slack.uid("p018") and event["text"].startswith("Hello <@U")
    assert event["ts"] == event["event_ts"] == posted["ts"] and event["team"] == body["team_id"]
    assert event["blocks"][0]["type"] == "rich_text" and event["client_msg_id"]
    headers = delivery["headers"]
    stamp = headers["x-slack-request-timestamp"]
    expected = (
        "v0="
        + hmac.new(secret.encode(), f"v0:{stamp}:".encode() + delivery["body"], hashlib.sha256).hexdigest()
    )
    assert headers["x-slack-signature"] == expected
    assert SignatureVerifier(secret).is_valid_request(delivery["body"], headers)
    assert not SignatureVerifier("other-secret").is_valid_request(delivery["body"], headers)
    assert headers["content-type"] == "application/json" and "x-slack-retry-num" not in headers
    assert headers["user-agent"].startswith("Slackbot 1.0")


async def test_message_subscriptions_follow_membership_and_conversation_type(
    slack: Slack, receiver: Receiver
) -> None:
    await configure(slack, events=["message.channels", "message.groups", "message.im"])
    receiver.requests.clear()
    await action(slack, "post_as_user", user="p001", channel="social", text="bot is not in social")
    assert receiver.events == []
    await action(slack, "post_as_user", user="p001", channel="general", text="public")
    await action(
        slack, "post_as_user", user="p018", channel="eng-bluefin-seafood-fees", text="private with bot"
    )
    await action(slack, "post_as_user", user="p001", channel="partners", text="private without bot")
    bot = slack.ctx.store.collection("app_config").get("app")["bot_user_id"]
    im = (await slack.ok("conversations.open", await slack.user_token("p001"), users=bot))["channel"]["id"]
    await slack.ok("chat.postMessage", await slack.user_token("p001"), channel=im, text="dm to bot")
    kinds = {e["event"]["text"]: e["event"]["channel_type"] for e in of_type(receiver, "message")}
    assert kinds == {"public": "channel", "private with bot": "group", "dm to bot": "im"}


async def test_unsubscribed_events_are_not_delivered(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["reaction_added"])
    receiver.requests.clear()
    await action(slack, "post_as_user", user="p001", channel="general", text="ignored")
    assert receiver.events == []


async def test_bot_does_not_receive_its_own_messages(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["message.channels", "app_mention"])
    receiver.requests.clear()
    channel = slack.channel_id("general")
    posted = await slack.ok("chat.postMessage", channel=channel, text="my own words")
    await slack.ok("chat.update", channel=channel, ts=posted["ts"], text="edited")
    await slack.ok("chat.delete", channel=channel, ts=posted["ts"])
    assert receiver.events == []
    bot = slack.ctx.store.collection("app_config").get("app")["bot_user_id"]
    await slack.ok("chat.postMessage", channel=channel, text=f"talking to myself <@{bot}>")
    assert receiver.events == []


async def test_app_mention(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["app_mention"])
    receiver.requests.clear()
    bot = slack.ctx.store.collection("app_config").get("app")["bot_user_id"]
    posted = await action(
        slack, "post_as_user", user="p018", channel="general", text=f"<@{bot}> run the report"
    )
    (mention,) = receiver.events
    event = mention["event"]
    assert event["type"] == "app_mention" and event["user"] == slack.uid("p018")
    assert event["text"] == f"<@{bot}> run the report" and event["ts"] == posted["ts"]
    assert event["channel"] == slack.channel_id("general") and event["team"]


async def test_thread_reply_events_carry_thread_ts(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["message.channels"])
    parent = await action(slack, "post_as_user", user="p018", channel="general", text="parent")
    receiver.requests.clear()
    reply = await action(
        slack,
        "post_as_user",
        user="p001",
        channel="general",
        text="reply",
        thread_ts=parent["ts"],
        reply_broadcast=True,
    )
    (event,) = [e["event"] for e in receiver.events]
    assert (
        event["thread_ts"] == parent["ts"]
        and event["ts"] == reply["ts"]
        and event["subtype"] == "thread_broadcast"
    )


async def test_edit_and_delete_events(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["message.channels"])
    posted = await action(slack, "post_as_user", user="p018", channel="general", text="v1")
    receiver.requests.clear()
    maya = await slack.user_token("p018")
    channel = slack.channel_id("general")
    await slack.ok("chat.update", maya, channel=channel, ts=posted["ts"], text="v2")
    await slack.ok("chat.delete", maya, channel=channel, ts=posted["ts"])
    changed, deleted = [e["event"] for e in receiver.events]
    assert changed["subtype"] == "message_changed" and changed["message"]["text"] == "v2"
    assert changed["previous_message"]["text"] == "v1" and changed["hidden"] is True
    assert deleted["subtype"] == "message_deleted" and deleted["deleted_ts"] == posted["ts"]
    assert deleted["previous_message"]["text"] == "v2" and deleted["channel"] == channel


async def test_reaction_events(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["reaction_added", "reaction_removed"])
    channel = slack.channel_id("general")
    target = (await slack.history(channel, limit=1))[0]
    receiver.requests.clear()
    maya = await slack.user_token("p018")
    await slack.ok("reactions.add", maya, channel=channel, timestamp=target["ts"], name="rocket")
    await action(slack, "add_reaction", user="p001", channel="general", ts=target["ts"], name="tada")
    await slack.ok("reactions.remove", maya, channel=channel, timestamp=target["ts"], name="rocket")
    added = of_type(receiver, "reaction_added")
    assert [e["event"]["reaction"] for e in added] == ["rocket", "tada"]
    event = added[0]["event"]
    assert event["user"] == slack.uid("p018") and event["item"] == {
        "type": "message",
        "channel": channel,
        "ts": target["ts"],
    }
    assert event["item_user"] == target["user"] and event["event_ts"]
    assert [e["event"]["reaction"] for e in of_type(receiver, "reaction_removed")] == ["rocket"]
    shown = (await slack.history(channel, limit=1))[0]
    assert {r["name"] for r in shown["reactions"]} >= {"tada"}


async def test_membership_events(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["member_joined_channel", "member_left_channel"])
    created = (await slack.ok("conversations.create", name="events-room"))["channel"]["id"]
    receiver.requests.clear()
    maya = slack.uid("p018")
    await slack.ok("conversations.invite", channel=created, users=maya)
    await action(slack, "user_join", user="p019", channel="events-room")
    await slack.ok("conversations.kick", channel=created, user=maya)
    await action(slack, "user_leave", user="p019", channel="events-room")
    joined = of_type(receiver, "member_joined_channel")
    assert [e["event"]["user"] for e in joined] == [maya, slack.uid("p019")]
    assert joined[0]["event"]["inviter"] == slack.ctx.store.collection("app_config").get("app")["bot_user_id"]
    assert joined[0]["event"]["channel"] == created and joined[0]["event"]["channel_type"] == "C"
    assert "inviter" not in joined[1]["event"]
    assert [e["event"]["user"] for e in of_type(receiver, "member_left_channel")] == [maya, slack.uid("p019")]
    receiver.requests.clear()
    await action(slack, "user_join", user="p020", channel="social")  # the bot is not in #social
    assert receiver.events == []


async def test_workspace_events(slack: Slack, receiver: Receiver) -> None:
    await configure(
        slack, events=["channel_created", "team_join", "file_shared", "channel_rename", "channel_archive"]
    )
    receiver.requests.clear()
    created = (await slack.ok("conversations.create", name="brand-new"))["channel"]
    private = (await slack.ok("conversations.create", name="hidden-room", is_private=True))["channel"]
    assert [e["event"]["channel"]["name"] for e in of_type(receiver, "channel_created")] == ["brand-new"]
    event = of_type(receiver, "channel_created")[0]["event"]
    assert event["channel"]["id"] == created["id"] and event["channel"]["creator"] == created["creator"]
    await slack.ok("conversations.rename", channel=created["id"], name="renamed-room")
    assert of_type(receiver, "channel_rename")[0]["event"]["channel"]["name"] == "renamed-room"
    await slack.ok("conversations.archive", channel=created["id"])
    assert of_type(receiver, "channel_archive")[0]["event"]["channel"] == created["id"]
    await slack.ok("conversations.archive", channel=private["id"])
    assert len(of_type(receiver, "channel_archive")) == 1
    joined = await action(slack, "team_join", first_name="Taylor", last_name="Quinn", title="Associate")
    event = of_type(receiver, "team_join")[0]["event"]
    assert event["user"]["id"] == joined["user"]["id"] and event["user"]["real_name"] == "Taylor Quinn"
    assert (await slack.ok("users.info", user=joined["user"]["id"]))["user"]["profile"][
        "title"
    ] == "Associate"
    assert (
        joined["user"]["id"]
        in (await slack.ok("conversations.members", channel=slack.channel_id("general")))["members"]
    )
    assert (
        await slack.client.post(
            f"{SLACK}/_emulator/actions/team_join", json={"first_name": "Taylor", "last_name": "Quinn"}
        )
    ).status_code == 409


async def test_file_shared_event(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["file_shared", "message.channels"])
    receiver.requests.clear()
    uploaded = await action(
        slack,
        "upload_file",
        user="p018",
        channel="general",
        filename="plan.txt",
        content="the plan",
        comment="Plan",
    )
    shared = of_type(receiver, "file_shared")
    assert len(shared) == 1
    event = shared[0]["event"]
    assert event["file_id"] == uploaded["file"] == event["file"]["id"] and event["user_id"] == slack.uid(
        "p018"
    )
    assert event["channel_id"] == slack.channel_id("general")
    message = of_type(receiver, "message")[0]["event"]
    assert message["subtype"] == "file_share" and message["files"][0]["name"] == "plan.txt"
    receiver.requests.clear()
    await slack.ok(
        "files.upload", None, content="by bot", filename="bot.txt", channels=slack.channel_id("general")
    )
    assert [e["event"]["type"] for e in receiver.events] == ["file_shared"]


async def test_pin_events(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["pin_added", "pin_removed"])
    channel = slack.channel_id("general")
    ts = (await slack.ok("chat.postMessage", channel=channel, text="pin me"))["ts"]
    receiver.requests.clear()
    await slack.ok("pins.add", channel=channel, timestamp=ts)
    await slack.ok("pins.remove", channel=channel, timestamp=ts)
    added, removed = [e["event"] for e in receiver.events]
    assert added["type"] == "pin_added" and added["item"]["message"]["ts"] == ts and added["pin_count"] >= 1
    assert removed["type"] == "pin_removed"


async def test_retries_use_new_signatures_and_retry_headers(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["message.channels"])
    receiver.requests.clear()
    receiver.fail_times = 2
    await action(slack, "post_as_user", user="p018", channel="general", text="flaky consumer")
    assert len(receiver.requests) == 3
    first, second, third = receiver.requests
    assert "x-slack-retry-num" not in first["headers"]
    assert (second["headers"]["x-slack-retry-num"], third["headers"]["x-slack-retry-num"]) == ("1", "2")
    assert second["headers"]["x-slack-retry-reason"] == "http_error"
    assert first["body"] == second["body"] == third["body"]
    secret = slack.ctx.store.collection("app_config").get("app")["signing_secret"]
    for request in receiver.requests:
        assert SignatureVerifier(secret).is_valid_request(request["body"], request["headers"])
    log = (await slack.client.get(f"{SLACK}/_emulator/webhooks", params={"event_type": "message"})).json()
    assert log["deliveries"][-1]["status"] == "delivered" and log["deliveries"][-1]["attempts"] == 3


async def test_exhausted_retries_are_journalled_as_failed(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["message.channels"])
    receiver.fail_times = 10
    await action(slack, "post_as_user", user="p018", channel="general", text="never works")
    log = (await slack.client.get(f"{SLACK}/_emulator/webhooks", params={"status": "failed"})).json()
    assert log["count"] == 1 and log["deliveries"][0]["attempts"] == 3


async def test_shared_channel_events_are_flagged(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["message.channels"])
    receiver.requests.clear()
    await action(
        slack, "post_as_user", user="c004-1", channel="ext-pinnacle-robotics", text="from the client"
    )
    (delivery,) = receiver.events
    assert delivery["is_ext_shared_channel"] is True
    assert delivery["event"]["user_team"] != slack.ctx.world.org.slack_team_id


async def test_incoming_webhook_posts_are_delivered_as_bot_messages(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["message.channels"])
    receiver.requests.clear()
    hook = next(
        h
        for h in slack.ctx.store.collection("incoming_webhooks")
        if h["channel_id"] == slack.channel_id("general")
    )
    r = await slack.client.post(
        f"https://hooks.slack.com/services/{hook['id']}", json={"text": "build finished", "username": "ci"}
    )
    assert r.text == "ok"
    (delivery,) = receiver.events
    assert delivery["event"]["subtype"] == "bot_message" and delivery["event"]["username"] == "ci"
    assert delivery["event"]["bot_id"] == hook["bot_id"]


async def test_disable_events_stops_deliveries(slack: Slack, receiver: Receiver) -> None:
    await configure(slack)
    receiver.requests.clear()
    assert (await action(slack, "disable_events"))["ok"]
    await action(slack, "post_as_user", user="p018", channel="general", text="nobody listens")
    assert receiver.requests == []


async def test_manual_mode_queues_until_flushed(slack: Slack, receiver: Receiver) -> None:
    await configure(slack, events=["message.channels"])
    receiver.requests.clear()
    slack.ctx.webhooks.mode = "manual"
    try:
        await action(slack, "post_as_user", user="p018", channel="general", text="queued")
        assert receiver.requests == []
        flushed = (await slack.client.post(f"{SLACK}/_emulator/webhooks/flush")).json()
        assert flushed["flushed"] == 1 and len(receiver.events) == 1
    finally:
        slack.ctx.webhooks.mode = "sync"


async def test_async_delivery_mode_with_one_retry() -> None:
    import httpx

    from emulators.core.hub import EmulatorHub

    from .conftest import Receiver

    consumer = Receiver()
    hub = EmulatorHub(
        providers=["slack"],
        anchor="2026-10-16",
        webhook_mode="async",
        webhook_transport=httpx.ASGITransport(app=consumer.app),
    )
    async with httpx.AsyncClient(transport=hub.async_transport(), base_url=SLACK) as client:
        configured = await client.post(
            "/_emulator/actions/configure_events", json={"request_url": URL, "events": ["message.channels"]}
        )
        assert configured.status_code == 200, configured.text
        consumer.requests.clear()
        consumer.fail_times = 1
        posted = await client.post(
            "/_emulator/actions/post_as_user",
            json={"user": "p018", "channel": "general", "text": "async mode"},
        )
        assert posted.status_code == 200
        await hub.drain_webhooks()
    assert [r["headers"].get("x-slack-retry-num") for r in consumer.requests] == [None, "1"]
    assert consumer.events[-1]["event"]["text"] == "async mode"
