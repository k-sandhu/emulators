"""conversations.* methods: listing, visibility, history paging and channel management."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .conftest import Slack


async def all_pages(slack: Slack, method: str, key: str, token: str | None = None, **params: object) -> list:
    items: list = []
    cursor = ""
    while True:
        body = await slack.ok(method, token, cursor=cursor or None, **params)
        items += body[key]
        cursor = body["response_metadata"]["next_cursor"]
        if not cursor:
            return items


# -- list / info -------------------------------------------------------------------------------


async def test_list_defaults_to_public_channels_with_membership_flags(slack: Slack) -> None:
    channels = (await slack.ok("conversations.list", limit=1000))["channels"]
    assert channels and all(c["is_channel"] and not c["is_private"] for c in channels)
    by_name = {c["name"]: c for c in channels}
    assert by_name["general"]["is_member"] is True
    assert by_name["social"]["is_member"] is False
    assert by_name["general"]["num_members"] > 40
    assert by_name["general"]["context_team_id"] == slack.ctx.world.org.slack_team_id
    assert by_name["general"]["topic"]["value"]
    assert by_name["general"]["purpose"]["value"]
    assert by_name["general"]["previous_names"] == []


async def test_list_pagination_walks_every_channel_once(slack: Slack) -> None:
    everything = (await slack.ok("conversations.list", limit=1000))["channels"]
    first = await slack.ok("conversations.list", limit=5)
    assert len(first["channels"]) == 5
    cursor = first["response_metadata"]["next_cursor"]
    assert base64.b64decode(cursor).decode().startswith("team:C")
    paged = await all_pages(slack, "conversations.list", "channels", limit=5)
    assert [c["id"] for c in paged] == [c["id"] for c in everything]
    assert await slack.err("conversations.list", cursor="bm90LWEtY3Vyc29y") == "invalid_cursor"
    assert await slack.err("conversations.list", limit=0) == "invalid_limit"


async def test_list_types_visibility_and_scopes(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    private = (await slack.ok("conversations.list", eleanor, types="private_channel", limit=1000))["channels"]
    assert "partners" in {c["name"] for c in private}
    bot_private = (await slack.ok("conversations.list", None, types="private_channel", limit=1000))[
        "channels"
    ]
    assert "partners" not in {c["name"] for c in bot_private}
    assert "eng-bluefin-seafood-fees" in {c["name"] for c in bot_private}
    assert await slack.err("conversations.list", None, types="bogus") == "invalid_types"
    readonly = (await slack.tokens())["bot_readonly"]
    body = await slack.api("conversations.list", readonly, types="im")
    assert body["error"] == "missing_scope" and body["needed"] == "im:read"
    mixed = await slack.ok(
        "conversations.list", eleanor, types="public_channel,private_channel,im", limit=1000
    )
    kinds = {(c.get("is_im", False), c.get("is_private", False)) for c in mixed["channels"]}
    assert kinds == {(True, False), (False, True), (False, False)}
    crew = await slack.mint(user="p013")
    mpim = await slack.ok("conversations.list", crew, types="mpim")
    assert mpim["channels"] and all(c["is_mpim"] and c["is_private"] for c in mpim["channels"])


async def test_list_exclude_archived(slack: Slack) -> None:
    channel = slack.channel_id("social")
    eleanor = await slack.user_token("p001")
    await slack.ok("conversations.archive", eleanor, channel=channel)
    names = {c["name"] for c in (await slack.ok("conversations.list", limit=1000))["channels"]}
    assert "social" in names
    active = {
        c["name"]
        for c in (await slack.ok("conversations.list", exclude_archived=True, limit=1000))["channels"]
    }
    assert "social" not in active
    archived = next(
        c for c in (await slack.ok("conversations.list", limit=1000))["channels"] if c["name"] == "social"
    )
    assert archived["is_archived"] is True


async def test_guests_only_see_their_channels(slack: Slack) -> None:
    guest = await slack.user_token("c002-1")
    names = {c["name"] for c in (await slack.ok("conversations.list", guest, limit=1000))["channels"]}
    assert names == {"eng-bluefin-seafood", "eng-bluefin-seafood-requests"}
    assert (
        await slack.err("conversations.info", guest, channel=slack.channel_id("general"))
        == "channel_not_found"
    )
    assert (
        await slack.err("conversations.join", guest, channel=slack.channel_id("general"))
        == "channel_not_found"
    )


async def test_info_public_private_and_missing(slack: Slack) -> None:
    social = (
        await slack.ok("conversations.info", channel=slack.channel_id("social"), include_num_members=True)
    )["channel"]
    assert social["is_member"] is False and social["num_members"] > 5
    assert (
        "num_members"
        not in (await slack.ok("conversations.info", channel=slack.channel_id("general")))["channel"]
    )
    private = slack.channel_id("partners")
    assert await slack.err("conversations.info", None, channel=private) == "channel_not_found"
    mine = (await slack.ok("conversations.info", await slack.user_token("p001"), channel=private))["channel"]
    assert mine["is_private"] and mine["is_group"] and not mine["is_channel"] and mine["id"].startswith("G")
    assert await slack.err("conversations.info", channel="C0NOTREALXX") == "channel_not_found"
    assert await slack.err("conversations.info", channel="") == "channel_not_found"


async def test_info_for_direct_messages_is_viewer_relative(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    ims = (await slack.ok("conversations.list", eleanor, types="im", limit=1000))["channels"]
    im = next(c for c in ims if c["user"] == slack.uid("p002"))
    assert im["is_im"] and im["id"].startswith("D") and "name" not in im
    marcus = await slack.mint(user="p002")
    seen = (await slack.ok("conversations.info", marcus, channel=im["id"]))["channel"]
    assert seen["user"] == slack.uid("p001")
    maya = await slack.mint(user="p018")
    assert await slack.err("conversations.info", maya, channel=im["id"]) == "channel_not_found"


# -- history / replies -------------------------------------------------------------------------


async def test_history_paging_and_ordering(slack: Slack) -> None:
    channel = slack.channel_id("general")
    page1 = await slack.ok("conversations.history", channel=channel, limit=7)
    assert len(page1["messages"]) == 7 and page1["has_more"] is True
    cursor = page1["response_metadata"]["next_cursor"]
    assert base64.b64decode(cursor).decode().startswith("next_ts:")
    seen = [m["ts"] for m in page1["messages"]]
    while cursor:
        body = await slack.ok("conversations.history", channel=channel, limit=7, cursor=cursor)
        seen += [m["ts"] for m in body["messages"]]
        cursor = body["response_metadata"]["next_cursor"]
        assert body["has_more"] is bool(cursor)
    everything = await slack.history(channel, limit=999)
    assert seen == [m["ts"] for m in everything] and seen == sorted(seen, reverse=True)
    assert len(set(seen)) == len(seen)
    assert page1["pin_count"] == 1


async def test_history_time_window(slack: Slack) -> None:
    channel = slack.channel_id("general")
    everything = await slack.history(channel, limit=999)
    newest, oldest = everything[0]["ts"], everything[-1]["ts"]
    middle = everything[len(everything) // 2]["ts"]
    window = await slack.history(channel, oldest=oldest, latest=newest)
    assert [m["ts"] for m in window] == [m["ts"] for m in everything[1:-1]]
    inclusive = await slack.history(channel, oldest=oldest, latest=newest, inclusive=True, limit=999)
    assert len(inclusive) == len(everything)
    after = await slack.history(channel, oldest=middle)
    assert all(m["ts"] > middle for m in after)
    assert await slack.err("conversations.history", channel=channel, oldest="abc") == "invalid_ts_oldest"
    assert await slack.err("conversations.history", channel=channel, latest="abc") == "invalid_ts_latest"


async def test_history_hides_thread_replies_but_shows_broadcasts(slack: Slack) -> None:
    channel = slack.channel_id("general")
    history = await slack.history(channel, limit=999)
    parent = next(m for m in history if m.get("reply_count"))
    token = await slack.mint(user="p001")
    plain = await slack.ok(
        "chat.postMessage", token, channel=channel, text="plain reply", thread_ts=parent["ts"]
    )
    broadcast = await slack.ok(
        "chat.postMessage",
        token,
        channel=channel,
        text="broadcast reply",
        thread_ts=parent["ts"],
        reply_broadcast=True,
    )
    after = await slack.history(channel, limit=999)
    stamps = {m["ts"] for m in after}
    assert plain["ts"] not in stamps and broadcast["ts"] in stamps
    shown = next(m for m in after if m["ts"] == broadcast["ts"])
    assert shown["subtype"] == "thread_broadcast" and shown["root"]["ts"] == parent["ts"]
    refreshed = next(m for m in after if m["ts"] == parent["ts"])
    assert refreshed["reply_count"] == parent["reply_count"] + 2
    assert refreshed["latest_reply"] == broadcast["ts"]
    assert slack.uid("p001") in refreshed["reply_users"]


async def test_history_membership_rules(slack: Slack) -> None:
    social = slack.channel_id("social")
    assert await slack.err("conversations.history", None, channel=social) == "not_in_channel"
    eleanor = await slack.user_token("p001")
    assert (await slack.ok("conversations.history", eleanor, channel=slack.channel_id("bd-general")))[
        "messages"
    ]
    assert (
        await slack.err("conversations.history", None, channel=slack.channel_id("partners"))
        == "channel_not_found"
    )
    assert (await slack.ok("conversations.history", eleanor, channel=slack.channel_id("partners")))[
        "messages"
    ]
    readonly = (await slack.tokens())["bot_readonly"]
    body = await slack.api("conversations.history", readonly, channel=slack.channel_id("general"))
    assert body["error"] == "missing_scope" and body["needed"] == "channels:history"
    assert await slack.err("conversations.history", eleanor, channel="D0NOTAREALDM") == "channel_not_found"


async def test_replies_returns_parent_then_replies(slack: Slack) -> None:
    channel = slack.channel_id("general")
    parent = next(m for m in await slack.history(channel, limit=999) if m.get("reply_count"))
    body = await slack.ok("conversations.replies", channel=channel, ts=parent["ts"])
    assert (
        body["messages"][0]["ts"] == parent["ts"]
        and body["messages"][0]["reply_count"] == parent["reply_count"]
    )
    stamps = [m["ts"] for m in body["messages"][1:]]
    assert stamps == sorted(stamps) and body["has_more"] is False
    reply_ts = stamps[0]
    via_reply = await slack.ok("conversations.replies", channel=channel, ts=reply_ts)
    assert [m["ts"] for m in via_reply["messages"]] == [m["ts"] for m in body["messages"]]
    one = await slack.ok("conversations.replies", channel=channel, ts=parent["ts"], limit=1)
    assert len(one["messages"]) == 1 and one["has_more"] is True
    assert await slack.err("conversations.replies", channel=channel, ts="1.000001") == "thread_not_found"
    assert await slack.err("conversations.replies", channel=channel, ts="zzz") == "invalid_ts"


async def test_unthreaded_message_replies_returns_itself(slack: Slack) -> None:
    channel = slack.channel_id("general")
    single = next(m for m in await slack.history(channel, limit=999) if not m.get("reply_count"))
    body = await slack.ok("conversations.replies", channel=channel, ts=single["ts"])
    assert [m["ts"] for m in body["messages"]] == [single["ts"]]


# -- members -----------------------------------------------------------------------------------


async def test_members_paging_and_privacy(slack: Slack) -> None:
    channel = slack.channel_id("general")
    members = await all_pages(slack, "conversations.members", "members", channel=channel, limit=10)
    assert len(members) == len(set(members)) > 40
    assert slack.uid("p001") in members and slack.uid("p046") not in members
    private = slack.channel_id("partners")
    assert await slack.err("conversations.members", None, channel=private) == "channel_not_found"
    assert (
        slack.uid("p001")
        in (await slack.ok("conversations.members", await slack.user_token("p001"), channel=private))[
            "members"
        ]
    )


# -- join / leave / create ---------------------------------------------------------------------


async def test_join_and_leave_public_channel(slack: Slack) -> None:
    social = slack.channel_id("social")
    joined = await slack.ok("conversations.join", channel=social)
    assert joined["channel"]["is_member"] is True and "warning" not in joined
    again = await slack.ok("conversations.join", channel=social)
    assert again["warning"] == "already_in_channel"
    history = await slack.history(social, limit=1)
    assert (
        history[0]["subtype"] == "channel_join"
        and history[0]["user"] == slack.ctx.store.collection("app_config").get("app")["bot_user_id"]
    )
    assert (await slack.ok("conversations.leave", channel=social)) == {"ok": True}
    assert (await slack.ok("conversations.leave", channel=social))["not_in_channel"] is True


async def test_join_restrictions(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    assert (
        await slack.err("conversations.join", eleanor, channel=slack.channel_id("partners"))
        == "method_not_supported_for_channel_type"
    )
    assert (
        await slack.err("conversations.join", None, channel=slack.channel_id("partners"))
        == "channel_not_found"
    )
    assert await slack.err("conversations.join", None, channel="C0NOTREALXX") == "channel_not_found"
    assert (
        await slack.err("conversations.leave", eleanor, channel=slack.channel_id("general"))
        == "cant_leave_general"
    )
    await slack.ok("conversations.archive", eleanor, channel=slack.channel_id("social"))
    assert await slack.err("conversations.join", None, channel=slack.channel_id("social")) == "is_archived"


async def test_create_validates_names(slack: Slack) -> None:
    created = (await slack.ok("conversations.create", name="Project-Atlas_1"))["channel"]
    assert created["name"] == "project-atlas_1" and created["is_member"] is True
    assert (
        created["id"].startswith("C")
        and created["creator"] == slack.ctx.store.collection("app_config").get("app")["bot_user_id"]
    )
    assert await slack.err("conversations.create", name="project-atlas_1") == "name_taken"
    assert await slack.err("conversations.create", name="has space") == "invalid_name_specials"
    assert await slack.err("conversations.create", name="x" * 81) == "invalid_name_maxlength"
    assert await slack.err("conversations.create", name="") == "invalid_name_required"
    private = (await slack.ok("conversations.create", name="secret-deal", is_private=True))["channel"]
    assert private["is_private"] and private["id"].startswith("G")
    guest = await slack.user_token("c002-1")
    assert await slack.err("conversations.create", guest, name="guest-made") == "restricted_action"
    listed = {c["name"] for c in (await slack.ok("conversations.list", limit=1000))["channels"]}
    assert "project-atlas_1" in listed and "secret-deal" not in listed


# -- invite / kick -----------------------------------------------------------------------------


async def test_invite_and_kick(slack: Slack) -> None:
    channel = (await slack.ok("conversations.create", name="invite-test"))["channel"]["id"]
    maya, ben = slack.uid("p018"), slack.uid("p019")
    body = await slack.ok("conversations.invite", channel=channel, users=f"{maya},{ben}")
    assert body["channel"]["id"] == channel
    members = (await slack.ok("conversations.members", channel=channel))["members"]
    assert maya in members and ben in members
    joins = [m for m in await slack.history(channel) if m.get("subtype") == "channel_join"]
    assert {m["user"] for m in joins} >= {maya, ben}
    assert all(m.get("inviter") for m in joins if m["user"] in (maya, ben))
    assert await slack.err("conversations.invite", channel=channel, users=maya) == "already_in_channel"
    assert await slack.err("conversations.invite", channel=channel, users="UNOSUCHUSER") == "user_not_found"
    assert (
        await slack.err("conversations.invite", channel=channel, users=slack.uid("p046")) == "user_disabled"
    )
    assert await slack.err("conversations.invite", channel=channel, users="") == "no_user"
    bot = slack.ctx.store.collection("app_config").get("app")["bot_user_id"]
    assert await slack.err("conversations.invite", channel=channel, users=bot) == "cant_invite_self"
    assert await slack.err("conversations.kick", channel=channel, user=bot) == "cant_kick_self"
    assert await slack.ok("conversations.kick", channel=channel, user=maya) == {"ok": True}
    assert maya not in (await slack.ok("conversations.members", channel=channel))["members"]
    assert await slack.err("conversations.kick", channel=channel, user=maya) == "not_in_channel"
    assert (
        await slack.err("conversations.kick", channel=slack.channel_id("general"), user=ben)
        == "cant_kick_from_general"
    )
    outsider = await slack.mint(user="p020")
    other = slack.channel_without("p020")
    assert await slack.err("conversations.invite", outsider, channel=other, users=maya) == "not_in_channel"


# -- archive / rename / topic ------------------------------------------------------------------


async def test_archive_unarchive_and_posting_rules(slack: Slack) -> None:
    channel = (await slack.ok("conversations.create", name="archive-me"))["channel"]["id"]
    assert await slack.ok("conversations.archive", channel=channel) == {"ok": True}
    assert await slack.err("conversations.archive", channel=channel) == "already_archived"
    assert await slack.err("chat.postMessage", channel=channel, text="x") == "is_archived"
    assert await slack.err("conversations.setTopic", channel=channel, topic="x") == "is_archived"
    assert await slack.ok("conversations.unarchive", channel=channel) == {"ok": True}
    assert await slack.err("conversations.unarchive", channel=channel) == "not_archived"
    assert (await slack.ok("chat.postMessage", channel=channel, text="back"))["ok"]
    assert (
        await slack.err(
            "conversations.archive", await slack.user_token("p001"), channel=slack.channel_id("general")
        )
        == "cant_archive_general"
    )


async def test_rename_topic_and_purpose(slack: Slack) -> None:
    channel = (await slack.ok("conversations.create", name="old-name"))["channel"]["id"]
    renamed = (await slack.ok("conversations.rename", channel=channel, name="new-name"))["channel"]
    assert renamed["name"] == "new-name" and renamed["previous_names"] == ["old-name"]
    assert await slack.err("conversations.rename", channel=channel, name="general") == "name_taken"
    assert (
        await slack.err("conversations.rename", channel=channel, name="Bad Name") == "invalid_name_specials"
    )
    topic = await slack.ok("conversations.setTopic", channel=channel, topic="Sprint goals")
    assert topic["channel"]["topic"]["value"] == "Sprint goals" and topic["channel"]["topic"]["creator"]
    purpose = await slack.ok("conversations.setPurpose", channel=channel, purpose="Planning")
    assert purpose["channel"]["purpose"]["value"] == "Planning" and purpose["purpose"] == "Planning"
    assert await slack.err("conversations.setTopic", channel=channel, topic="x" * 251) == "too_long"
    subtypes = [m["subtype"] for m in await slack.history(channel) if "subtype" in m]
    assert {"channel_name", "channel_topic", "channel_purpose"} <= set(subtypes)
    outsider = await slack.mint(user="p020")
    other = slack.channel_without("p020")
    assert await slack.err("conversations.setTopic", outsider, channel=other, topic="x") == "not_in_channel"


# -- open / close / mark -----------------------------------------------------------------------


async def test_open_im_and_group_dm(slack: Slack) -> None:
    maya = slack.uid("p018")
    first = await slack.ok("conversations.open", users=maya)
    assert first["channel"]["id"].startswith("D") and "no_op" not in first
    again = await slack.ok("conversations.open", users=maya, return_im=True)
    assert again["no_op"] is True and again["already_open"] is True
    assert again["channel"]["is_im"] and again["channel"]["user"] == maya
    group = await slack.ok("conversations.open", users=f"{maya},{slack.uid('p019')}", return_im=True)
    assert group["channel"]["is_mpim"] and group["channel"]["id"].startswith("G")
    assert group["channel"]["name"].startswith("mpdm-")
    assert await slack.err("conversations.open") == "users_list_not_supplied"
    assert await slack.err("conversations.open", users="UNOSUCHUSER") == "user_not_found"
    assert await slack.err("conversations.open", users=slack.uid("p046")) == "user_disabled"
    too_many = ",".join(slack.uid(p.id) for p in slack.ctx.world.active_employees[:12])
    assert await slack.err("conversations.open", users=too_many) == "too_many_users"


async def test_close_and_reopen_im(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    im = (await slack.ok("conversations.open", eleanor, users=slack.uid("p002")))["channel"]["id"]
    assert await slack.ok("conversations.close", eleanor, channel=im) == {"ok": True}
    assert (await slack.ok("conversations.close", eleanor, channel=im))["already_closed"] is True
    listed = (await slack.ok("conversations.list", eleanor, types="im", limit=1000))["channels"]
    assert im not in {c["id"] for c in listed}
    reopened = await slack.ok("conversations.open", eleanor, channel=im)
    assert "already_open" not in reopened
    assert (
        await slack.err("conversations.close", eleanor, channel=slack.channel_id("general"))
        == "channel_not_found"
    )


async def test_mark_updates_last_read(slack: Slack) -> None:
    eleanor = await slack.user_token("p001")
    channel = slack.channel_id("general")
    newest = (await slack.history(channel, eleanor))[0]["ts"]
    assert await slack.ok("conversations.mark", eleanor, channel=channel, ts=newest) == {"ok": True}
    assert (await slack.ok("conversations.info", eleanor, channel=channel))["channel"]["last_read"] == newest
    assert await slack.err("conversations.mark", eleanor, channel=channel, ts="nope") == "invalid_timestamp"
    assert (
        await slack.err("conversations.mark", None, channel=slack.channel_id("social"), ts=newest)
        == "not_in_channel"
    )
