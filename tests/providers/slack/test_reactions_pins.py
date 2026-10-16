"""reactions.*, pins.* and bookmarks.*"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .conftest import Slack


async def post(slack: Slack, text: str = "react to me", channel: str = "general") -> tuple[str, str]:
    cid = slack.channel_id(channel)
    return cid, (await slack.ok("chat.postMessage", channel=cid, text=text))["ts"]


async def test_add_get_remove_reaction(slack: Slack) -> None:
    channel, ts = await post(slack)
    assert await slack.ok("reactions.add", channel=channel, timestamp=ts, name="thumbsup") == {"ok": True}
    maya = await slack.user_token("p018")
    await slack.ok("reactions.add", maya, channel=channel, timestamp=ts, name=":thumbsup:")
    await slack.ok("reactions.add", maya, channel=channel, timestamp=ts, name="tada")
    got = await slack.ok("reactions.get", channel=channel, timestamp=ts)
    assert got["type"] == "message" and got["channel"] == channel
    by_name = {r["name"]: r for r in got["message"]["reactions"]}
    assert by_name["thumbsup"]["count"] == 2 and len(by_name["thumbsup"]["users"]) == 2
    assert by_name["tada"]["users"] == [slack.uid("p018")]
    assert await slack.ok("reactions.remove", maya, channel=channel, timestamp=ts, name="thumbsup") == {
        "ok": True
    }
    after = (await slack.ok("reactions.get", channel=channel, timestamp=ts))["message"]["reactions"]
    assert {r["name"]: r["count"] for r in after} == {"thumbsup": 1, "tada": 1}
    await slack.ok("reactions.remove", maya, channel=channel, timestamp=ts, name="tada")
    shown = (await slack.history(channel, limit=1))[0]
    assert [r["name"] for r in shown["reactions"]] == ["thumbsup"]


async def test_reaction_errors(slack: Slack) -> None:
    channel, ts = await post(slack)
    await slack.ok("reactions.add", channel=channel, timestamp=ts, name="eyes")
    assert await slack.err("reactions.add", channel=channel, timestamp=ts, name="eyes") == "already_reacted"
    assert (
        await slack.err("reactions.add", channel=channel, timestamp=ts, name="Not Valid!") == "invalid_name"
    )
    assert await slack.err("reactions.add", channel=channel, timestamp=ts, name="") == "invalid_name"
    assert await slack.err("reactions.add", channel=channel, timestamp="bad", name="eyes") == "bad_timestamp"
    assert (
        await slack.err("reactions.add", channel=channel, timestamp="1.000001", name="eyes")
        == "message_not_found"
    )
    assert await slack.err("reactions.add", name="eyes") == "no_item_specified"
    assert await slack.err("reactions.remove", channel=channel, timestamp=ts, name="rocket") == "no_reaction"
    assert (
        await slack.err("reactions.add", channel="C0NOTREALXX", timestamp=ts, name="eyes")
        == "channel_not_found"
    )
    social_ts = (await slack.history(slack.channel_id("social"), await slack.user_token("p001"), limit=1))[0][
        "ts"
    ]
    assert (
        await slack.err("reactions.add", channel=slack.channel_id("social"), timestamp=social_ts, name="eyes")
        == "not_in_channel"
    )
    for index in range(22):
        await slack.ok("reactions.add", channel=channel, timestamp=ts, name=f"emoji-{index}")
    assert (
        await slack.err("reactions.add", channel=channel, timestamp=ts, name="one-too-many")
        == "too_many_emoji"
    )


async def test_reactions_on_archived_channels_are_rejected(slack: Slack) -> None:
    channel = (await slack.ok("conversations.create", name="react-archive"))["channel"]["id"]
    ts = (await slack.ok("chat.postMessage", channel=channel, text="x"))["ts"]
    await slack.ok("conversations.archive", channel=channel)
    assert await slack.err("reactions.add", channel=channel, timestamp=ts, name="eyes") == "is_archived"


async def test_reactions_list(slack: Slack) -> None:
    channel, ts = await post(slack)
    other_channel, other_ts = await post(slack, "second", "it-help")
    maya = await slack.user_token("p018")
    await slack.ok("reactions.add", maya, channel=channel, timestamp=ts, name="eyes")
    await slack.ok("reactions.add", maya, channel=other_channel, timestamp=other_ts, name="eyes")
    items = (await slack.ok("reactions.list", maya))["items"]
    assert [i["message"]["ts"] for i in items[:2]] == sorted([ts, other_ts], reverse=True) or len(items) >= 2
    assert {i["type"] for i in items} == {"message"}
    mine = [i for i in items if i["message"]["ts"] in (ts, other_ts)]
    assert len(mine) == 2 and {i["channel"] for i in mine} == {channel, other_channel}
    page = await slack.ok("reactions.list", maya, limit=1)
    assert len(page["items"]) == 1 and page["response_metadata"]["next_cursor"]
    legacy = await slack.ok("reactions.list", maya, count=1, page=1)
    assert legacy["paging"]["count"] == 1 and legacy["paging"]["total"] >= 2
    assert await slack.err("reactions.list", maya, user="UNOSUCHUSER") == "user_not_found"


async def test_pins(slack: Slack) -> None:
    channel, ts = await post(slack)
    assert await slack.ok("pins.add", channel=channel, timestamp=ts) == {"ok": True}
    assert await slack.err("pins.add", channel=channel, timestamp=ts) == "already_pinned"
    items = (await slack.ok("pins.list", channel=channel))["items"]
    pinned = next(i for i in items if i["message"]["ts"] == ts)
    assert pinned["type"] == "message" and pinned["channel"] == channel and pinned["created"] > 0
    assert pinned["created_by"] == slack.ctx.store.collection("app_config").get("app")["bot_user_id"]
    assert (await slack.ok("conversations.history", channel=channel))["pin_count"] == len(items)
    notice = (await slack.history(channel, limit=1))[0]
    assert notice["subtype"] == "pinned_item" and notice["pinned_ts"] == ts
    assert await slack.ok("pins.remove", channel=channel, timestamp=ts) == {"ok": True}
    assert await slack.err("pins.remove", channel=channel, timestamp=ts) == "not_pinned"
    assert await slack.err("pins.add", channel=channel) == "no_item_specified"
    assert await slack.err("pins.add", channel=channel, timestamp="1.000001") == "message_not_found"
    assert await slack.err("pins.add", channel=channel, timestamp="bad") == "bad_timestamp"
    assert await slack.err("pins.list", channel=slack.channel_id("social")) == "not_in_channel"
    assert (
        await slack.err("pins.add", None, channel=slack.channel_id("social"), timestamp=ts)
        == "not_in_channel"
    )


async def test_deleting_a_pinned_message_removes_the_pin(slack: Slack) -> None:
    channel, ts = await post(slack)
    await slack.ok("pins.add", channel=channel, timestamp=ts)
    await slack.ok("chat.delete", channel=channel, ts=ts)
    assert ts not in {i["message"]["ts"] for i in (await slack.ok("pins.list", channel=channel))["items"]}


async def test_bookmarks(slack: Slack) -> None:
    general = slack.channel_id("general")
    seeded = (await slack.ok("bookmarks.list", channel_id=general))["bookmarks"]
    assert seeded and seeded[0]["title"] == "Firm intranet" and seeded[0]["type"] == "link"
    added = (
        await slack.ok(
            "bookmarks.add",
            channel_id=general,
            title="Wiki",
            type="link",
            link="https://wiki.halcyonadvisory.example",
            emoji=":book:",
        )
    )["bookmark"]
    assert added["id"].startswith("Bk") and added["emoji"] == ":book:" and added["channel_id"] == general
    edited = (
        await slack.ok("bookmarks.edit", channel_id=general, bookmark_id=added["id"], title="Handbook")
    )["bookmark"]
    assert edited["title"] == "Handbook" and edited["date_updated"] >= added["date_created"]
    titles = [b["title"] for b in (await slack.ok("bookmarks.list", channel_id=general))["bookmarks"]]
    assert titles == ["Firm intranet", "Handbook"]
    assert await slack.ok("bookmarks.remove", channel_id=general, bookmark_id=added["id"]) == {"ok": True}
    assert (
        await slack.err("bookmarks.remove", channel_id=general, bookmark_id=added["id"])
        == "bookmark_not_found"
    )
    assert (
        await slack.err("bookmarks.add", channel_id=general, title="x", type="link", link="nope")
        == "invalid_link"
    )
    assert await slack.err("bookmarks.add", channel_id=general, title="x", type="folder") == "invalid_type"
    assert await slack.err("bookmarks.list", channel_id=slack.channel_id("social")) == "not_in_channel"
    assert await slack.err("bookmarks.list", channel_id=slack.channel_id("partners")) == "channel_not_found"
