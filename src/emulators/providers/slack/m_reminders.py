"""reminders.* (user tokens only, as in Slack): Slackbot DMs the user when a reminder is due."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.slack import access, messages
from emulators.providers.slack.records import new_id
from emulators.providers.slack.runtime import BEFORE_CALL, USER, Call, SlackError, api

REMINDERS = "reminders"
RELATIVE = re.compile(r"^in (\d+|an?|one) (second|minute|hour|day|week)s?$")
CLOCK = re.compile(r"^(?:at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")
UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}
MAX_SECONDS_AHEAD = 5 * 365 * 86400


def parse_when(ctx: ProviderContext, raw: str) -> int:
    """Slack's reminder ``time``: unix timestamp, seconds from now, or simple natural language."""
    now = ctx.clock.now()
    text = raw.strip().lower()
    if text.isdigit():
        value = int(text)
        if value <= 86400 * 365:
            return int(now.timestamp()) + value
        return value
    if match := RELATIVE.match(text):
        amount = 1 if match.group(1) in ("a", "an", "one") else int(match.group(1))
        return int(now.timestamp()) + amount * UNIT_SECONDS[match.group(2)]
    if text == "tomorrow":
        return int((now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).timestamp())
    if match := CLOCK.match(text):
        hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), match.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            raise SlackError("cannot_parse")
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return int((target if target > now else target + timedelta(days=1)).timestamp())
    raise SlackError("cannot_parse")


def _out(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if not k.startswith("_")}


def _own(call: Call, ref: str | None) -> dict[str, Any]:
    record = access.col(call.ctx, REMINDERS).get(ref) if ref else None
    if record is None or record["user"] != call.me.uid:
        raise SlackError("not_found")
    return record


@api("reminders.add", scopes=("reminders:write",), kinds=(USER,))
async def reminders_add(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    text = p.need("text")
    when = parse_when(ctx, p.need("time"))
    now = int(ctx.clock.now().timestamp())
    if when <= now:
        raise SlackError("cannot_add_in_past" if p.str("time", "").isdigit() else "cannot_parse")
    if when > now + MAX_SECONDS_AHEAD:
        raise SlackError("cannot_parse")
    target = access.user_by_id(ctx, p.str("user") or call.me.uid)
    if target is None:
        raise SlackError("user_not_found")
    if target["id"] != call.me.uid:
        raise SlackError(
            "cannot_add_others" if not access.is_admin(access.actor_user(ctx, call.me)) else "cannot_add_bot"
        )
    record = {
        "id": new_id(ctx, "Rm", REMINDERS, 8),
        "creator": call.me.uid,
        "user": target["id"],
        "text": text,
        "recurring": False,
        "time": when,
        "complete_ts": 0,
    }
    access.col(ctx, REMINDERS).put(record)
    return {"reminder": _out(record)}


@api("reminders.list", scopes=("reminders:read",), kinds=(USER,))
async def reminders_list(call: Call) -> dict[str, Any]:
    found = [r for r in access.col(call.ctx, REMINDERS) if r["user"] == call.me.uid]
    found.sort(key=lambda r: (r["time"], r["id"]))
    return {"reminders": [_out(r) for r in found]}


@api("reminders.info", scopes=("reminders:read",), kinds=(USER,))
async def reminders_info(call: Call) -> dict[str, Any]:
    return {"reminder": _out(_own(call, call.params.str("reminder")))}


@api("reminders.complete", scopes=("reminders:write",), kinds=(USER,))
async def reminders_complete(call: Call) -> dict[str, Any]:
    record = _own(call, call.params.str("reminder"))
    record["complete_ts"] = int(call.ctx.clock.now().timestamp())
    return {}


@api("reminders.delete", scopes=("reminders:write",), kinds=(USER,))
async def reminders_delete(call: Call) -> dict[str, Any]:
    record = _own(call, call.params.str("reminder"))
    access.col(call.ctx, REMINDERS).delete(record["id"])
    return {}


async def fire_due(ctx: ProviderContext) -> None:
    """Slackbot reminds the user in a DM once ``time`` has passed (lazy, like scheduled messages)."""
    reminders = access.col(ctx, REMINDERS)
    if not len(reminders):
        return
    now = int(ctx.clock.now().timestamp())
    for record in reminders.values():
        if record["time"] > now or record.get("_fired") or record["complete_ts"]:
            continue
        record["_fired"] = True
        channel = messages.open_im(ctx, access.SLACKBOT, record["user"])
        await messages.create_message(
            ctx,
            channel,
            user=access.SLACKBOT,
            text=f"Reminder: {record['text']}.",
            extra={"reminder": record["id"]},
        )


BEFORE_CALL.append(fire_due)
