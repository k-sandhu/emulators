"""users.*, team.*, usergroups.*, dnd.*, bots.info and emoji.list."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .conftest import Slack


async def test_users_list_shape_and_paging(slack: Slack) -> None:
    body = await slack.ok("users.list", limit=1000)
    members = body["members"]
    assert body["cache_ts"] > 0 and body["response_metadata"]["next_cursor"] == ""
    eleanor = next(m for m in members if m["id"] == slack.uid("p001"))
    for key in (
        "id", "team_id", "name", "deleted", "color", "real_name", "tz", "tz_label", "tz_offset", "profile",
        "is_admin", "is_owner", "is_primary_owner", "is_restricted", "is_ultra_restricted", "is_bot",
        "is_app_user", "updated", "is_email_confirmed",
    ):  # fmt: skip
        assert key in eleanor, key
    profile = eleanor["profile"]
    assert profile["real_name"] == "Eleanor Whitfield" and profile["first_name"] == "Eleanor"
    assert profile["email"] == "eleanor.whitfield@halcyonadvisory.example"
    assert profile["title"] == "Managing Partner & CEO" and profile["phone"] == "+14165550101"
    for size in (24, 32, 48, 72, 192, 512):
        assert profile[f"image_{size}"].startswith("https://")
    assert eleanor["tz"] == "America/Toronto" and eleanor["tz_offset"] in (-14400, -18000)
    assert eleanor["tz_label"] in ("Eastern Daylight Time", "Eastern Standard Time")
    london_person = next(p for p in slack.ctx.world.employees if p.timezone == "Europe/London")
    london = next(m for m in members if m["id"] == slack.uid(london_person.id))
    assert london["tz"] == "Europe/London" and london["tz_label"] in (
        "British Summer Time",
        "Greenwich Mean Time",
    )
    paged: list = []
    cursor = ""
    while True:
        page = await slack.ok("users.list", limit=10, cursor=cursor or None)
        paged += page["members"]
        cursor = page["response_metadata"]["next_cursor"]
        if not cursor:
            break
    assert [m["id"] for m in paged] == [m["id"] for m in members]
    assert await slack.err("users.list", cursor="Zm9v") == "invalid_cursor"


async def test_email_requires_its_own_scope(slack: Slack) -> None:
    narrow = await slack.mint(scopes=["users:read"])
    member = (await slack.ok("users.info", narrow, user=slack.uid("p018")))["user"]
    assert "email" not in member["profile"] and member["profile"]["real_name"] == "Maya Chen"
    wide = (await slack.ok("users.info", user=slack.uid("p018")))["user"]
    assert wide["profile"]["email"] == slack.person_email("p018")
    body = await slack.api("users.lookupByEmail", narrow, email=slack.person_email("p018"))
    assert body["error"] == "missing_scope" and body["needed"] == "users:read.email"


async def test_users_info_and_lookup(slack: Slack) -> None:
    departed = (await slack.ok("users.info", user=slack.uid("p046")))["user"]
    assert departed["deleted"] is True
    shown = (await slack.ok("users.info", user=slack.uid("p018"), include_locale=True))["user"]
    assert shown["locale"] == slack.ctx.world.person("p018").locale
    assert "locale" not in (await slack.ok("users.info", user=slack.uid("p018")))["user"]
    assert await slack.err("users.info", user="UNOSUCHUSER") == "user_not_found"
    assert await slack.err("users.info") == "user_not_found"
    found = await slack.ok("users.lookupByEmail", email=slack.person_email("p018").upper())
    assert found["user"]["id"] == slack.uid("p018")
    assert await slack.err("users.lookupByEmail", email="ghost@halcyonadvisory.example") == "users_not_found"
    assert await slack.err("users.lookupByEmail", email=slack.person_email("c004-1")) == "users_not_found"


async def test_bots_and_slackbot(slack: Slack) -> None:
    members = (await slack.ok("users.list", limit=1000))["members"]
    bot = next(m for m in members if m["is_bot"])
    assert bot["profile"]["bot_id"].startswith("B") and bot["profile"]["api_app_id"].startswith("A")
    info = (await slack.ok("bots.info", bot=bot["profile"]["bot_id"]))["bot"]
    assert (
        info["name"] == "Halcyon Bot"
        and info["user_id"] == bot["id"]
        and info["app_id"] == bot["profile"]["api_app_id"]
    )
    assert set(info["icons"]) == {"image_36", "image_48", "image_72"}
    assert await slack.err("bots.info", bot="BNOSUCHBOT") == "bot_not_found"
    slackbot = next(m for m in members if m["id"] == "USLACKBOT")
    assert slackbot["name"] == "slackbot" and slackbot["profile"]["always_active"] is True


async def test_profile_get_and_set(slack: Slack) -> None:
    maya = await slack.user_token("p018")
    mine = (await slack.ok("users.profile.get", maya))["profile"]
    assert mine["real_name"] == "Maya Chen" and mine["email"] == slack.person_email("p018")
    other = (await slack.ok("users.profile.get", maya, user=slack.uid("p001")))["profile"]
    assert other["real_name"] == "Eleanor Whitfield"
    updated = await slack.ok(
        "users.profile.set",
        maya,
        profile={"status_text": "In a meeting", "status_emoji": ":spiral_calendar_pad:", "title": "Lead"},
    )
    assert updated["profile"]["status_text"] == "In a meeting" and updated["profile"]["title"] == "Lead"
    assert updated["username"] == "maya.chen"
    assert (await slack.ok("users.info", user=slack.uid("p018")))["user"]["profile"][
        "status_emoji"
    ] == ":spiral_calendar_pad:"
    via_pair = await slack.ok("users.profile.set", maya, name="display_name", value="mayac")
    assert via_pair["profile"]["display_name"] == "mayac"
    assert (
        await slack.err("users.profile.set", maya, profile={"status_emoji": "smile"})
        == "profile_status_set_failed_not_valid_emoji"
    )
    assert await slack.err("users.profile.set", maya, profile={"unknown_field": 1}) == "invalid_profile"
    assert await slack.err("users.profile.set", maya) == "invalid_profile"
    assert (
        await slack.err("users.profile.set", maya, user=slack.uid("p001"), profile={"title": "x"})
        == "not_admin"
    )
    admin = await slack.user_token("p035")
    assert (await slack.ok("users.profile.set", admin, user=slack.uid("p018"), profile={"title": "Senior"}))[
        "profile"
    ]["title"] == "Senior"


async def test_users_conversations(slack: Slack) -> None:
    maya = await slack.user_token("p018")
    own = (
        await slack.ok("users.conversations", maya, types="public_channel,private_channel,im,mpim", limit=999)
    )["channels"]
    names = {c.get("name") for c in own}
    assert "general" in names and "eng-bluefin-seafood-fees" in names
    assert all("is_member" not in c for c in own)
    public = (await slack.ok("users.conversations", maya))["channels"]
    assert all(c["is_channel"] for c in public) and "social" in {c["name"] for c in public}
    junior = await slack.mint(user="p020")
    others = (await slack.ok("users.conversations", junior, user=slack.uid("p001"), types="private_channel"))[
        "channels"
    ]
    assert others == []
    partner_view = (
        await slack.ok("users.conversations", maya, user=slack.uid("p001"), types="private_channel")
    )["channels"]
    assert "partners" in {c["name"] for c in partner_view}
    bot = (await slack.ok("users.conversations"))["channels"]
    assert "general" in {c["name"] for c in bot} and "social" not in {c["name"] for c in bot}
    assert await slack.err("users.conversations", user="UNOSUCHUSER") == "user_not_found"
    first = await slack.ok("users.conversations", maya, limit=3)
    assert len(first["channels"]) == 3 and first["response_metadata"]["next_cursor"]


async def test_presence(slack: Slack) -> None:
    maya = await slack.user_token("p018")
    mine = await slack.ok("users.getPresence", maya)
    assert mine["presence"] in ("active", "away") and "connection_count" in mine
    assert (await slack.ok("users.getPresence", maya, user=slack.uid("p001")))["presence"] in (
        "active",
        "away",
    )
    await slack.ok("users.setPresence", maya, presence="away")
    assert (await slack.ok("users.getPresence", maya))["presence"] == "away"
    assert (await slack.ok("users.getPresence", maya))["manual_away"] is True
    await slack.ok("users.setPresence", maya, presence="auto")
    assert (await slack.ok("users.getPresence", maya))["presence"] == "active"
    assert await slack.err("users.setPresence", maya, presence="busy") == "invalid_presence"
    assert await slack.err("users.setPresence", None, presence="away") == "not_allowed_token_type"
    assert await slack.err("users.getPresence", maya, user="UNOSUCHUSER") == "user_not_found"


async def test_team_info_and_profile_fields(slack: Slack) -> None:
    team = (await slack.ok("team.info"))["team"]
    assert team["email_domain"] == "halcyonadvisory.example" and team["icon"]["image_default"] is True
    assert (await slack.ok("team.info", domain="halcyonadvisory"))["team"]["id"] == team["id"]
    assert await slack.err("team.info", domain="other") == "team_not_found"
    fields = (await slack.ok("team.profile.get"))["profile"]["fields"]
    assert {f["label"] for f in fields} == {"Employee ID", "Office", "Department"}
    assert next(f for f in fields if f["label"] == "Office")["possible_values"]


async def test_usergroups_crud(slack: Slack) -> None:
    groups = (await slack.ok("usergroups.list"))["usergroups"]
    assert all("users" not in g and "user_count" not in g for g in groups)
    counted = (await slack.ok("usergroups.list", include_count=True))["usergroups"]
    assert all(g["user_count"] >= 0 for g in counted)
    admin = await slack.user_token("p035")
    created = (
        await slack.ok(
            "usergroups.create", admin, name="Busy Season Crew", handle="busy-crew", description="Extra hands"
        )
    )["usergroup"]
    assert created["handle"] == "busy-crew" and created["id"].startswith("S") and created["date_delete"] == 0
    assert await slack.err("usergroups.create", admin, name="Busy Season Crew") == "name_already_exists"
    assert (
        await slack.err("usergroups.create", admin, name="Other", handle="busy-crew")
        == "handle_already_exists"
    )
    assert (
        await slack.err("usergroups.create", await slack.user_token("p018"), name="Nope")
        == "permission_denied"
    )
    users = f"{slack.uid('p018')},{slack.uid('p019')}"
    updated = (
        await slack.ok(
            "usergroups.users.update", admin, usergroup=created["id"], users=users, include_count=True
        )
    )["usergroup"]
    assert updated["user_count"] == 2
    assert (await slack.ok("usergroups.users.list", usergroup=created["id"]))["users"] == users.split(",")
    assert (
        await slack.err("usergroups.users.update", admin, usergroup=created["id"], users="")
        == "no_users_provided"
    )
    assert (
        await slack.err("usergroups.users.update", admin, usergroup=created["id"], users=slack.uid("p046"))
        == "invalid_users"
    )
    renamed = (
        await slack.ok(
            "usergroups.update",
            admin,
            usergroup=created["id"],
            name="Busy Crew",
            channels=slack.channel_id("general"),
        )
    )["usergroup"]
    assert renamed["name"] == "Busy Crew" and renamed["prefs"]["channels"] == [slack.channel_id("general")]
    assert await slack.err("usergroups.update", admin, usergroup="SNOSUCH") == "no_such_subteam"
    disabled = (await slack.ok("usergroups.disable", admin, usergroup=created["id"]))["usergroup"]
    assert disabled["date_delete"] > 0
    assert created["id"] not in {g["id"] for g in (await slack.ok("usergroups.list"))["usergroups"]}
    assert created["id"] in {
        g["id"] for g in (await slack.ok("usergroups.list", include_disabled=True))["usergroups"]
    }
    assert await slack.err("usergroups.users.list", usergroup=created["id"]) == "no_such_subteam"
    assert await slack.err("usergroups.disable", admin, usergroup=created["id"]) == "already_disabled"
    assert (await slack.ok("usergroups.enable", admin, usergroup=created["id"]))["usergroup"][
        "date_delete"
    ] == 0


async def test_dnd(slack: Slack) -> None:
    maya = await slack.user_token("p018")
    info = await slack.ok("dnd.info", maya)
    assert info["dnd_enabled"] is False and info["snooze_enabled"] is False
    assert await slack.err("dnd.endSnooze", maya) == "snooze_not_active"
    snooze = await slack.ok("dnd.setSnooze", maya, num_minutes=30)
    assert snooze["snooze_enabled"] is True and 1700 < snooze["snooze_remaining"] <= 1800
    assert (await slack.ok("dnd.info", maya, user=slack.uid("p018")))["snooze_enabled"] is True
    team = (await slack.ok("dnd.teamInfo", maya, users=f"{slack.uid('p018')},{slack.uid('p001')}"))["users"]
    assert set(team) == {slack.uid("p018"), slack.uid("p001")}
    ended = await slack.ok("dnd.endSnooze", maya)
    assert ended["snooze_enabled"] is False
    assert await slack.err("dnd.setSnooze", maya) == "missing_duration"
    assert (await slack.ok("dnd.endDnd", maya)) == {"ok": True}
    assert await slack.err("dnd.info", maya, user="UNOSUCHUSER") == "user_not_found"


async def test_emoji_list(slack: Slack) -> None:
    emoji = (await slack.ok("emoji.list"))["emoji"]
    assert emoji["halcyon"].startswith("https://emoji.slack-edge.com/T")
    assert emoji["ship-it"] == "alias:rocket"
