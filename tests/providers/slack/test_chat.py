"""chat.* methods."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .conftest import Slack


async def test_post_message_response_and_history(slack: Slack) -> None:
    channel = slack.channel_id("general")
    body = await slack.ok("chat.postMessage", channel=channel, text="Hello *team* from the bot")
    assert body["channel"] == channel
    ts = body["ts"]
    assert ts.count(".") == 1 and len(ts.split(".")[1]) == 6
    message = body["message"]
    app = slack.ctx.store.collection("app_config").get("app")
    assert message["type"] == "message" and message["text"] == "Hello *team* from the bot"
    assert message["user"] == app["bot_user_id"] and message["bot_id"] == app["bot_id"]
    assert message["app_id"] == app["app_id"] and message["team"] == slack.ctx.world.org.slack_team_id
    assert (
        message["bot_profile"]["name"] == "Halcyon Bot" and message["bot_profile"]["app_id"] == app["app_id"]
    )
    elements = message["blocks"][0]["elements"][0]["elements"]
    assert elements[0] == {"type": "text", "text": "Hello "}
    assert elements[1] == {"type": "text", "text": "team", "style": {"bold": True}}
    newest = (await slack.history(channel, limit=1))[0]
    assert newest["ts"] == ts and newest["bot_id"] == app["bot_id"]


async def test_rich_text_blocks_for_mentions_links_emoji_lists_and_code(slack: Slack) -> None:
    channel = slack.channel_id("general")
    uid = slack.uid("p018")
    text = f"Hi <@{uid}> see <https://example.com/doc|the doc> :tada: <!here>\n• one\n• two\n```code here```"
    body = await slack.ok("chat.postMessage", channel=channel, text=text)
    blocks = body["message"]["blocks"][0]["elements"]
    kinds = [b["type"] for b in blocks]
    assert kinds == ["rich_text_section", "rich_text_list", "rich_text_preformatted"]
    inline = {e["type"]: e for e in blocks[0]["elements"]}
    assert inline["user"]["user_id"] == uid
    assert inline["link"] == {"type": "link", "url": "https://example.com/doc", "text": "the doc"}
    assert inline["emoji"]["name"] == "tada" and inline["broadcast"]["range"] == "here"
    assert [i["elements"][0]["text"] for i in blocks[1]["elements"]] == ["one", "two"]
    assert blocks[2]["elements"][0]["text"] == "code here"


async def test_channel_by_name_and_user_id_direct_message(slack: Slack) -> None:
    by_name = await slack.ok("chat.postMessage", channel="#general", text="by name")
    assert by_name["channel"] == slack.channel_id("general")
    plain = await slack.ok("chat.postMessage", channel="general", text="by bare name")
    assert plain["channel"] == slack.channel_id("general")
    maya = slack.uid("p018")
    dm = await slack.ok("chat.postMessage", channel=maya, text="Hello Maya")
    assert dm["channel"].startswith("D")
    again = await slack.ok("chat.postMessage", channel=maya, text="Second")
    assert again["channel"] == dm["channel"]
    token = await slack.user_token("p018")
    inbox = await slack.history(dm["channel"], token)
    assert [m["text"] for m in inbox] == ["Second", "Hello Maya"]
    assert await slack.err("chat.postMessage", channel=slack.uid("p046"), text="x") == "is_archived"


async def test_validation_errors(slack: Slack) -> None:
    channel = slack.channel_id("general")
    assert await slack.err("chat.postMessage", channel=channel) == "no_text"
    assert await slack.err("chat.postMessage", channel=channel, text="") == "no_text"
    assert await slack.err("chat.postMessage", channel=channel, text="x" * 40_001) == "msg_too_long"
    assert (await slack.ok("chat.postMessage", channel=channel, text="y" * 40_000))["ok"]
    assert await slack.err("chat.postMessage", channel="C0NOTREALXX", text="x") == "channel_not_found"
    assert await slack.err("chat.postMessage", channel="#nope-nope", text="x") == "channel_not_found"
    assert await slack.err("chat.postMessage", text="x") == "channel_not_found"
    assert await slack.err("chat.postMessage", channel=channel, text="x", thread_ts="bad") == "invalid_ts"
    assert (
        await slack.err("chat.postMessage", channel=channel, text="x", thread_ts="1.000001")
        == "thread_not_found"
    )


async def test_bot_must_be_member_unless_chat_write_public(slack: Slack) -> None:
    social = slack.channel_id("social")
    assert await slack.err("chat.postMessage", channel=social, text="x") == "not_in_channel"
    full = (await slack.tokens())["bot_full"]
    assert (await slack.ok("chat.postMessage", full, channel=social, text="allowed"))["ok"]
    assert (
        await slack.err("chat.postMessage", None, channel=slack.channel_id("partners"), text="x")
        == "channel_not_found"
    )
    assert (
        await slack.err("chat.postMessage", full, channel=slack.channel_id("partners"), text="x")
        == "channel_not_found"
    )
    ben = await slack.user_token("p001")
    assert (
        await slack.err("chat.postMessage", ben, channel=slack.channel_without("p001"), text="x")
        == "not_in_channel"
    )


async def test_blocks_and_attachments(slack: Slack) -> None:
    channel = slack.channel_id("general")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Invoice 1043"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Paid* in full"}},
        {"type": "divider"},
    ]
    attachments = [{"color": "#36a64f", "title": "Receipt", "text": "ok"}, {"text": "second"}]
    body = await slack.ok(
        "chat.postMessage", channel=channel, blocks=blocks, attachments=attachments, text="Invoice"
    )
    message = body["message"]
    assert [b["type"] for b in message["blocks"]] == ["header", "section", "divider"]
    assert all(b["block_id"] for b in message["blocks"])
    assert [a["id"] for a in message["attachments"]] == [1, 2]
    only_blocks = await slack.ok("chat.postMessage", channel=channel, blocks=blocks)
    assert only_blocks["message"]["text"] == ""
    assert await slack.err("chat.postMessage", channel=channel, blocks="not json") == "invalid_blocks_format"
    assert await slack.err("chat.postMessage", channel=channel, blocks=[{"nope": 1}]) == "invalid_blocks"
    assert (
        await slack.err("chat.postMessage", channel=channel, blocks=[{"type": "bogus"}]) == "invalid_blocks"
    )
    assert (
        await slack.err("chat.postMessage", channel=channel, blocks=[{"type": "divider"}] * 51)
        == "invalid_blocks"
    )
    assert await slack.err("chat.postMessage", channel=channel, attachments="nope") == "invalid_attachments"
    assert (
        await slack.err("chat.postMessage", channel=channel, attachments=[{}] * 101) == "too_many_attachments"
    )


async def test_json_encoded_post_with_blocks(slack: Slack) -> None:
    body = await slack.api(
        "chat.postMessage",
        json={
            "channel": slack.channel_id("general"),
            "text": "json",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hello"}}],
        },
    )
    assert body["ok"] and body["message"]["blocks"][0]["type"] == "section"


async def test_threads_and_broadcast(slack: Slack) -> None:
    channel = slack.channel_id("general")
    parent = await slack.ok("chat.postMessage", channel=channel, text="parent")
    one = await slack.ok("chat.postMessage", channel=channel, text="one", thread_ts=parent["ts"])
    assert one["message"]["thread_ts"] == parent["ts"]
    two = await slack.ok("chat.postMessage", channel=channel, text="two", thread_ts=one["ts"])
    assert two["message"]["thread_ts"] == parent["ts"]
    replies = await slack.ok("conversations.replies", channel=channel, ts=parent["ts"])
    assert [m["text"] for m in replies["messages"]] == ["parent", "one", "two"]
    top = replies["messages"][0]
    assert top["reply_count"] == 2 and top["reply_users_count"] == 1 and top["latest_reply"] == two["ts"]
    assert top["subscribed"] is True
    assert one["message"]["parent_user_id"] == top["user"]


async def test_metadata_and_customization(slack: Slack) -> None:
    channel = slack.channel_id("general")
    meta = {"event_type": "invoice_paid", "event_payload": {"invoice": "INV-1043"}}
    body = await slack.ok("chat.postMessage", channel=channel, text="with metadata", metadata=meta)
    assert body["message"]["metadata"] == meta
    assert (
        await slack.err("chat.postMessage", channel=channel, text="x", metadata="oops")
        == "invalid_metadata_format"
    )
    assert (
        await slack.err("chat.postMessage", channel=channel, text="x", metadata={"event_payload": {}})
        == "invalid_metadata_format"
    )
    plain = await slack.ok(
        "chat.postMessage", channel=channel, text="no customization", username="Ghost", icon_emoji=":ghost:"
    )
    assert "username" not in plain["message"] and "subtype" not in plain["message"]
    full = (await slack.tokens())["bot_full"]
    custom = await slack.ok(
        "chat.postMessage", full, channel=channel, text="custom", username="Ghost", icon_emoji=":ghost:"
    )
    assert custom["message"]["username"] == "Ghost" and custom["message"]["icons"] == {"emoji": ":ghost:"}
    assert custom["message"]["subtype"] == "bot_message"


async def test_user_tokens_post_as_the_user(slack: Slack) -> None:
    token = await slack.user_token("p001")
    body = await slack.ok("chat.postMessage", token, channel=slack.channel_id("general"), text="From Eleanor")
    message = body["message"]
    assert message["user"] == slack.uid("p001") and "bot_id" not in message and "bot_profile" not in message


async def test_update_message(slack: Slack) -> None:
    channel = slack.channel_id("general")
    posted = await slack.ok("chat.postMessage", channel=channel, text="draft")
    updated = await slack.ok("chat.update", channel=channel, ts=posted["ts"], text="final *text*")
    assert updated["text"] == "final *text*" and updated["ts"] == posted["ts"]
    message = updated["message"]
    assert message["edited"]["user"] == posted["message"]["user"] and message["edited"]["ts"]
    assert message["blocks"][0]["elements"][0]["elements"][1]["style"] == {"bold": True}
    assert (await slack.history(channel, limit=1))[0]["text"] == "final *text*"
    assert await slack.err("chat.update", channel=channel, ts=posted["ts"]) == "no_text"
    assert await slack.err("chat.update", channel=channel, ts="1.000001", text="x") == "message_not_found"
    token = await slack.user_token("p001")
    assert (
        await slack.err("chat.update", token, channel=channel, ts=posted["ts"], text="hijack")
        == "cant_update_message"
    )
    with_blocks = await slack.ok(
        "chat.update",
        channel=channel,
        ts=posted["ts"],
        blocks=[{"type": "divider"}],
        attachments=[{"text": "a"}],
    )
    assert with_blocks["message"]["blocks"][0]["type"] == "divider"
    assert with_blocks["message"]["text"] == "final *text*"


async def test_delete_message_and_permissions(slack: Slack) -> None:
    channel = slack.channel_id("general")
    eleanor = await slack.user_token("p001")
    by_user = await slack.ok("chat.postMessage", eleanor, channel=channel, text="user message")
    assert await slack.err("chat.delete", channel=channel, ts=by_user["ts"]) == "cant_delete_message"
    mine = await slack.ok("chat.postMessage", channel=channel, text="bot message")
    maya = await slack.user_token("p018")
    assert await slack.err("chat.delete", maya, channel=channel, ts=mine["ts"]) == "cant_delete_message"
    assert await slack.ok("chat.delete", channel=channel, ts=mine["ts"]) == {
        "ok": True,
        "channel": channel,
        "ts": mine["ts"],
    }
    admin = await slack.user_token("p035")
    assert (await slack.ok("chat.delete", admin, channel=channel, ts=by_user["ts"]))["ok"]
    assert await slack.err("chat.delete", channel=channel, ts=mine["ts"]) == "message_not_found"
    stamps = {m["ts"] for m in await slack.history(channel, limit=999)}
    assert mine["ts"] not in stamps and by_user["ts"] not in stamps


async def test_deleting_a_thread_parent_leaves_a_tombstone(slack: Slack) -> None:
    channel = slack.channel_id("general")
    parent = await slack.ok("chat.postMessage", channel=channel, text="parent")
    await slack.ok("chat.postMessage", channel=channel, text="reply", thread_ts=parent["ts"])
    await slack.ok("chat.delete", channel=channel, ts=parent["ts"])
    shown = next(m for m in await slack.history(channel, limit=999) if m["ts"] == parent["ts"])
    assert shown["subtype"] == "tombstone" and shown["reply_count"] == 1
    assert (await slack.ok("conversations.replies", channel=channel, ts=parent["ts"]))["messages"][1][
        "text"
    ] == "reply"


async def test_deleting_a_reply_updates_thread_counters(slack: Slack) -> None:
    channel = slack.channel_id("general")
    parent = await slack.ok("chat.postMessage", channel=channel, text="parent")
    reply = await slack.ok("chat.postMessage", channel=channel, text="reply", thread_ts=parent["ts"])
    await slack.ok("chat.delete", channel=channel, ts=reply["ts"])
    shown = next(m for m in await slack.history(channel, limit=999) if m["ts"] == parent["ts"])
    assert shown["reply_count"] == 0 and shown["thread_ts"] == parent["ts"]


async def test_post_ephemeral(slack: Slack) -> None:
    channel = slack.channel_id("general")
    maya = slack.uid("p018")
    body = await slack.ok("chat.postEphemeral", channel=channel, user=maya, text="only you")
    assert body["message_ts"].count(".") == 1
    stored = slack.ctx.store.collection("ephemeral_messages").get(body["message_ts"])
    assert stored["user"] == maya and stored["text"] == "only you"
    assert body["message_ts"] not in {m["ts"] for m in await slack.history(channel, limit=999)}
    assert (
        await slack.err("chat.postEphemeral", channel=channel, user=slack.uid("p046"), text="x")
        == "user_not_in_channel"
    )
    assert (
        await slack.err("chat.postEphemeral", channel=channel, user="UNOSUCHUSER", text="x")
        == "user_not_in_channel"
    )
    assert await slack.err("chat.postEphemeral", channel=channel, user=maya) == "no_text"


async def test_get_permalink(slack: Slack) -> None:
    channel = slack.channel_id("general")
    parent = await slack.ok("chat.postMessage", channel=channel, text="p")
    link = (await slack.ok("chat.getPermalink", channel=channel, message_ts=parent["ts"]))["permalink"]
    assert link == f"https://halcyonadvisory.slack.com/archives/{channel}/p{parent['ts'].replace('.', '')}"
    reply = await slack.ok("chat.postMessage", channel=channel, text="r", thread_ts=parent["ts"])
    reply_link = (await slack.ok("chat.getPermalink", channel=channel, message_ts=reply["ts"]))["permalink"]
    assert reply_link.endswith(f"?thread_ts={parent['ts']}&cid={channel}")
    assert await slack.err("chat.getPermalink", channel=channel, message_ts="1.000001") == "message_not_found"
    assert (
        await slack.err("chat.getPermalink", channel="C0NOTREALXX", message_ts=parent["ts"])
        == "channel_not_found"
    )


async def test_me_message(slack: Slack) -> None:
    channel = slack.channel_id("general")
    body = await slack.ok("chat.meMessage", channel=channel, text="waves")
    shown = (await slack.history(channel, limit=1))[0]
    assert shown["ts"] == body["ts"] and shown["subtype"] == "me_message"
    assert await slack.err("chat.meMessage", channel=channel) == "no_text"


async def test_scheduled_messages_are_posted_when_the_clock_passes(slack: Slack) -> None:
    channel = slack.channel_id("general")
    now = int(time.time())
    assert (
        await slack.err("chat.scheduleMessage", channel=channel, text="x", post_at=now - 60) == "time_in_past"
    )
    assert (
        await slack.err("chat.scheduleMessage", channel=channel, text="x", post_at=now + 200 * 86400)
        == "time_too_far"
    )
    assert (
        await slack.err("chat.scheduleMessage", channel=channel, text="x", post_at="soon") == "invalid_time"
    )
    assert await slack.err("chat.scheduleMessage", channel=channel, post_at=now + 600) == "no_text"
    scheduled = await slack.ok("chat.scheduleMessage", channel=channel, text="later", post_at=now + 3600)
    assert scheduled["scheduled_message_id"].startswith("Q") and scheduled["post_at"] == now + 3600
    cancelled = await slack.ok("chat.scheduleMessage", channel=channel, text="cancelled", post_at=now + 7200)
    listing = (await slack.ok("chat.scheduledMessages.list", channel=channel))["scheduled_messages"]
    assert {s["id"] for s in listing} == {
        scheduled["scheduled_message_id"],
        cancelled["scheduled_message_id"],
    }
    assert listing[0]["post_at"] == now + 3600 and listing[0]["text"] == "later"
    await slack.ok(
        "chat.deleteScheduledMessage", channel=channel, scheduled_message_id=cancelled["scheduled_message_id"]
    )
    assert (
        await slack.err(
            "chat.deleteScheduledMessage",
            channel=channel,
            scheduled_message_id=cancelled["scheduled_message_id"],
        )
        == "invalid_scheduled_message_id"
    )
    slack.hub.clock.advance(timedelta(hours=3))
    try:
        newest = await slack.history(channel, limit=3)
    finally:
        slack.hub.clock.advance(timedelta(hours=-3))
    assert newest[0]["text"] == "later"
    assert (await slack.ok("chat.scheduledMessages.list"))["scheduled_messages"] == []


async def test_unfurl_accepts_unfurls(slack: Slack) -> None:
    token = (await slack.tokens())["bot_full"]
    ok = await slack.ok(
        "chat.unfurl", token, channel="C1", ts="1.2", unfurls={"https://x.example": {"text": "x"}}
    )
    assert ok["ok"]
    assert await slack.err("chat.unfurl", token) == "invalid_arguments"
