"""reminders.* (user tokens only)."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .conftest import Slack


async def test_add_list_info_complete_delete(slack: Slack) -> None:
    maya = await slack.user_token("p018")
    now = int(time.time())
    added = (await slack.ok("reminders.add", maya, text="File extension", time=now + 3600))["reminder"]
    assert added["id"].startswith("Rm") and added["time"] == now + 3600 and added["recurring"] is False
    assert added["user"] == added["creator"] == slack.uid("p018") and added["complete_ts"] == 0
    relative = (await slack.ok("reminders.add", maya, text="Soon", time="in 15 minutes"))["reminder"]
    assert abs(relative["time"] - (now + 900)) < 5
    seconds = (await slack.ok("reminders.add", maya, text="Seconds", time=120))["reminder"]
    assert abs(seconds["time"] - (now + 120)) < 5
    listed = (await slack.ok("reminders.list", maya))["reminders"]
    assert [r["text"] for r in listed] == ["Seconds", "Soon", "File extension"]
    assert (await slack.ok("reminders.info", maya, reminder=added["id"]))["reminder"][
        "text"
    ] == "File extension"
    assert await slack.ok("reminders.complete", maya, reminder=added["id"]) == {"ok": True}
    assert (await slack.ok("reminders.info", maya, reminder=added["id"]))["reminder"]["complete_ts"] > 0
    assert await slack.ok("reminders.delete", maya, reminder=added["id"]) == {"ok": True}
    assert await slack.err("reminders.info", maya, reminder=added["id"]) == "not_found"
    other = await slack.user_token("p001")
    assert await slack.err("reminders.info", other, reminder=relative["id"]) == "not_found"


async def test_reminder_errors(slack: Slack) -> None:
    maya = await slack.user_token("p018")
    assert (
        await slack.err("reminders.add", maya, text="x", time="whenever the moon is full") == "cannot_parse"
    )
    assert await slack.err("reminders.add", maya, text="x", time=1_000_000_000) == "cannot_add_in_past"
    assert await slack.err("reminders.add", maya, time="in 5 minutes") == "invalid_arguments"
    assert (
        await slack.err("reminders.add", maya, text="x", time="in 5 minutes", user=slack.uid("p001"))
        == "cannot_add_others"
    )
    assert await slack.err("reminders.add", None, text="x", time="in 5 minutes") == "not_allowed_token_type"


async def test_due_reminders_arrive_as_slackbot_direct_messages(slack: Slack) -> None:
    maya = await slack.user_token("p018")
    await slack.ok("reminders.add", maya, text="Call the client", time="in 10 minutes")
    slack.hub.clock.advance(timedelta(minutes=11))
    try:
        ims = (await slack.ok("conversations.list", maya, types="im", limit=1000))["channels"]
        slackbot_dm = next(c for c in ims if c["user"] == "USLACKBOT")
        message = (await slack.history(slackbot_dm["id"], maya))[0]
    finally:
        slack.hub.clock.advance(timedelta(minutes=-11))
    assert message["text"] == "Reminder: Call the client." and message["user"] == "USLACKBOT"
