"""Permission and edge cases that cut across the Web API areas."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .conftest import Slack


async def test_guests_have_restricted_actions(slack: Slack) -> None:
    guest = await slack.user_token("c002-1")
    channel = slack.channel_id("eng-bluefin-seafood")
    assert (
        await slack.err("conversations.invite", guest, channel=channel, users=slack.uid("p020"))
        == "restricted_action"
    )
    assert await slack.err("conversations.create", guest, name="guest-room") == "restricted_action"
    posted = await slack.ok("chat.postMessage", guest, channel=channel, text="hello from a guest")
    assert posted["message"]["user"] == slack.uid("c002-1")
    other = slack.channel_id("general")
    assert await slack.err("chat.postMessage", guest, channel=other, text="x") == "channel_not_found"
    assert await slack.err("conversations.history", guest, channel=other) == "channel_not_found"
    assert (await slack.ok("conversations.history", guest, channel=channel))["messages"]


async def test_archiving_needs_membership_or_admin(slack: Slack) -> None:
    public = slack.channel_without("p020")
    junior = await slack.mint(user="p020")
    assert await slack.err("conversations.archive", junior, channel=public) == "not_in_channel"
    admin = await slack.user_token("p035")
    assert await slack.ok("conversations.archive", admin, channel=public) == {"ok": True}
    assert await slack.err("conversations.archive", admin, channel=public) == "already_archived"


async def test_direct_message_visibility_and_members(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    im = next(
        c
        for c in (await slack.ok("conversations.list", eleanor, types="im", limit=1000))["channels"]
        if c["user"] == slack.uid("p002")
    )["id"]
    assert sorted((await slack.ok("conversations.members", eleanor, channel=im))["members"]) == sorted(
        [slack.uid("p001"), slack.uid("p002")]
    )
    assert (await slack.ok("conversations.history", eleanor, channel=im))["messages"]
    assert await slack.err("conversations.history", None, channel=im) == "channel_not_found"
    assert (
        await slack.err("conversations.members", await slack.user_token("p018"), channel=im)
        == "channel_not_found"
    )
    posted = await slack.ok("chat.postMessage", eleanor, channel=slack.uid("p002"), text="direct")
    assert posted["channel"] == im
    assert (
        await slack.err("conversations.join", eleanor, channel=im) == "method_not_supported_for_channel_type"
    )
    assert (
        await slack.err("conversations.leave", eleanor, channel=im) == "method_not_supported_for_channel_type"
    )
    assert (
        await slack.err("conversations.rename", eleanor, channel=im, name="x")
        == "method_not_supported_for_channel_type"
    )


async def test_search_in_a_direct_message_by_user(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    maya_handle = "maya.chen"
    await slack.ok("chat.postMessage", eleanor, channel=slack.uid("p018"), text="needle in a dm")
    found = (await slack.ok("search.messages", eleanor, query=f"needle in:@{maya_handle}"))["messages"][
        "matches"
    ]
    assert len(found) == 1 and found[0]["channel"]["is_im"] is True
    assert (await slack.ok("search.messages", eleanor, query="needle in:@nobody.here"))["messages"][
        "total"
    ] == 0


async def test_legacy_upload_into_a_thread(slack: Slack) -> None:
    channel = slack.channel_id("general")
    parent = (await slack.ok("chat.postMessage", channel=channel, text="parent"))["ts"]
    await slack.ok(
        "files.upload",
        None,
        content="x",
        filename="t.txt",
        channels=channel,
        thread_ts=parent,
        initial_comment="in thread",
    )
    replies = (await slack.ok("conversations.replies", channel=channel, ts=parent))["messages"]
    assert replies[1]["subtype"] == "file_share" and replies[1]["files"][0]["name"] == "t.txt"


async def test_broadcast_flag_needs_a_thread(slack: Slack) -> None:
    channel = slack.channel_id("general")
    body = await slack.ok("chat.postMessage", channel=channel, text="loose", reply_broadcast=True)
    assert "subtype" not in body["message"] and "thread_ts" not in body["message"]


async def test_replies_cursor_validation(slack: Slack) -> None:
    channel = slack.channel_id("general")
    parent = next(m for m in await slack.history(channel, limit=999) if m.get("reply_count"))
    assert (
        await slack.err("conversations.replies", channel=channel, ts=parent["ts"], cursor="Zm9v")
        == "invalid_cursor"
    )
    assert await slack.err("conversations.history", channel=channel, cursor="Zm9v") == "invalid_cursor"


async def test_profile_status_expiration_and_fields(slack: Slack) -> None:
    maya = await slack.user_token("p018")
    profile = (
        await slack.ok(
            "users.profile.set",
            maya,
            profile={"status_text": "Away", "status_emoji": ":palm_tree:", "status_expiration": 1893456000},
        )
    )["profile"]
    assert profile["status_expiration"] == 1893456000
    cleared = (await slack.ok("users.profile.set", maya, profile={"status_text": "", "status_emoji": ""}))[
        "profile"
    ]
    assert cleared["status_text"] == "" and cleared["status_emoji"] == ""


async def test_user_ids_in_text_survive_a_round_trip(slack: Slack) -> None:
    channel = slack.channel_id("general")
    uid = slack.uid("p018")
    body = await slack.ok(
        "chat.postMessage",
        channel=channel,
        text=f"ping <@{uid}> & <#{channel}|general> <!subteam^SABC|@audit>",
    )
    kinds = [e["type"] for e in body["message"]["blocks"][0]["elements"][0]["elements"]]
    assert kinds == ["text", "user", "text", "channel", "text", "usergroup"]
    stored = (await slack.history(channel, limit=1))[0]["text"]
    assert stored == f"ping <@{uid}> & <#{channel}|general> <!subteam^SABC|@audit>"


async def test_state_endpoints_expose_internal_collections(slack: Slack) -> None:
    state = (await slack.client.get("https://slack.com/_emulator/state")).json()
    assert {"users", "channels", "messages", "files", "incoming_webhooks", "usergroups"} <= set(state)
    one = (
        await slack.client.get(
            "https://slack.com/_emulator/state/channels", params={"id": slack.channel_id("general")}
        )
    ).json()
    assert one["name"] == "general" and uid_in(one["_members"], slack.uid("p001"))
    await slack.client.post("https://slack.com/_emulator/reset")
    assert (await slack.client.get("https://slack.com/_emulator/state")).json()["messages"] == state[
        "messages"
    ]


def uid_in(members: list[str], uid: str) -> bool:
    return uid in members
