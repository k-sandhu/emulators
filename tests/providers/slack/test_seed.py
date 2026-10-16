"""The seeded workspace mirrors the shared world."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from emulators.core.hub import EmulatorHub

if TYPE_CHECKING:
    from .conftest import Slack


async def test_seed_is_fast_and_deterministic() -> None:
    started = time.perf_counter()
    first = EmulatorHub(providers=["slack"], anchor="2026-10-16")
    assert time.perf_counter() - started < 2.5
    second = EmulatorHub(providers=["slack"], anchor="2026-10-16")
    a = first.context("slack").store.collection("channels").ids()
    b = second.context("slack").store.collection("channels").ids()
    assert a == b
    ma = [m["ts"] for m in first.context("slack").store.collection("messages")]
    mb = [m["ts"] for m in second.context("slack").store.collection("messages")]
    assert ma == mb


async def test_workspace_and_members(slack: Slack) -> None:
    team = (await slack.ok("team.info"))["team"]
    assert team["id"] == slack.ctx.world.org.slack_team_id
    assert team["name"] == "Halcyon Advisory Group"
    assert team["domain"] == "halcyonadvisory"
    assert team["url"] == "https://halcyonadvisory.slack.com/"
    members = (await slack.ok("users.list", limit=1000))["members"]
    by_id = {m["id"]: m for m in members}
    assert "USLACKBOT" in by_id
    bot = next(m for m in members if m["is_bot"])
    assert bot["real_name"] == "Halcyon Bot"
    employees = slack.ctx.world.employees
    for person in employees:
        member = by_id[slack.uid(person.id)]
        assert member["deleted"] is (not person.active)
        assert member["name"] == person.username
    owner = by_id[slack.uid("p001")]
    assert owner["is_owner"] and owner["is_primary_owner"] and owner["is_admin"]
    assert by_id[slack.uid("p035")]["is_admin"] and not by_id[slack.uid("p035")]["is_owner"]
    assert not by_id[slack.uid("p018")]["is_admin"]
    # Slack Connect strangers are not workspace members
    assert slack.uid("c004-1") not in by_id


async def test_channel_names_from_world_teams(slack: Slack) -> None:
    public = (await slack.ok("conversations.list", types="public_channel", limit=1000))["channels"]
    names = {c["name"] for c in public}
    for expected in (
        "general",
        "announcements",
        "it-help",
        "social",
        "audit-general",
        "audit-fieldwork",
        "tax-updates",
        "advisory-deals",
        "bd-pipeline",
        "eng-bluefin-seafood",
        "ext-pinnacle-robotics",
    ):
        assert expected in names
    general = next(c for c in public if c["name"] == "general")
    assert general["is_general"] and general["is_channel"] and not general["is_private"]
    assert general["num_members"] >= 45
    private = await slack.ok(
        "conversations.list", await slack.user_token("p001"), types="private_channel", limit=1000
    )
    private_names = {c["name"] for c in private["channels"]}
    assert {"partners"} <= private_names
    assert all(c["is_private"] and c["is_group"] for c in private["channels"])


async def test_slack_connect_channels_have_external_members(slack: Slack) -> None:
    channel = (await slack.ok("conversations.info", channel=slack.channel_id("ext-pinnacle-robotics")))[
        "channel"
    ]
    assert channel["is_ext_shared"] and channel["is_shared"]
    assert len(channel["shared_team_ids"]) == 2
    members = (await slack.ok("conversations.members", channel=channel["id"]))["members"]
    assert slack.uid("c004-1") in members
    stranger = (await slack.ok("users.info", user=slack.uid("c004-1")))["user"]
    assert stranger["is_stranger"] and stranger["team_id"] != slack.ctx.world.org.slack_team_id


async def test_guests_are_flagged(slack: Slack) -> None:
    multi = (await slack.ok("users.info", user=slack.uid("c002-1")))["user"]
    single = (await slack.ok("users.info", user=slack.uid("c002-2")))["user"]
    assert multi["is_restricted"] and not multi["is_ultra_restricted"]
    assert single["is_restricted"] and single["is_ultra_restricted"]


async def test_messages_threads_reactions_and_mentions(slack: Slack) -> None:
    history = await slack.history(slack.channel_id("general"), limit=200)
    assert len(history) > 10
    stamps = [m["ts"] for m in history]
    assert stamps == sorted(stamps, reverse=True) and len(set(stamps)) == len(stamps)
    parents = [m for m in history if m.get("reply_count")]
    assert parents and all(m["thread_ts"] == m["ts"] for m in parents)
    assert all("thread_ts" not in m or m["thread_ts"] == m["ts"] for m in history)
    assert any(m.get("reactions") for m in history)
    mention = next(m for m in history if "<@U" in m["text"])
    assert mention["blocks"][0]["elements"][0]["elements"][0]["type"] in ("user", "text")
    parent = parents[0]
    thread = await slack.ok("conversations.replies", channel=slack.channel_id("general"), ts=parent["ts"])
    assert thread["messages"][0]["ts"] == parent["ts"]
    assert len(thread["messages"]) == parent["reply_count"] + 1
    assert all(m["thread_ts"] == parent["ts"] for m in thread["messages"])


async def test_dms_and_group_dms_from_world_chats(slack: Slack) -> None:
    token = await slack.user_token("p001")
    ims = (await slack.ok("conversations.list", token, types="im", limit=1000))["channels"]
    others = {c["user"] for c in ims}
    assert slack.uid("p002") in others and slack.uid("p003") in others
    mpims = (await slack.ok("conversations.list", await slack.mint(user="p013"), types="mpim"))["channels"]
    assert mpims and all(c["is_mpim"] and c["name"].startswith("mpdm-") for c in mpims)


async def test_usergroups_from_departments(slack: Slack) -> None:
    groups = (await slack.ok("usergroups.list", include_users=True, include_count=True))["usergroups"]
    handles = {g["handle"]: g for g in groups}
    assert {"audit", "tax", "advisory", "it"} <= set(handles)
    audit = handles["audit"]
    assert audit["user_count"] == len(audit["users"]) > 0
    listed = (await slack.ok("usergroups.users.list", usergroup=audit["id"]))["users"]
    assert listed == audit["users"]
    assert slack.uid("p004") in listed


async def test_seeded_files_come_from_world_documents(slack: Slack) -> None:
    files = (await slack.ok("files.list", await slack.user_token("p001"), count=100))["files"]
    assert files
    assert {f["filetype"] for f in files} & {"docx", "xlsx", "pdf", "pptx"}
    assert all(f["url_private"].startswith("https://files.slack.com/files-pri/T") for f in files)


async def test_seeded_file_references_preserve_every_share(slack: Slack) -> None:
    for message in slack.ctx.store.collection("messages"):
        for file_id in message.get("_files", []):
            file = slack.ctx.store.collection("files").get(file_id)
            assert file is not None
            assert {"channel": message["_channel"], "ts": message["ts"]} in file["_shares"]
