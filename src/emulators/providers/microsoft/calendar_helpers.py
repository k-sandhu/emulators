"""Calendar projection and Windows time zone conversion."""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common as c
from emulators.providers.microsoft import directory_support as s
from emulators.world.model import Meeting

ZONES = {
    "UTC": "UTC",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Pacific Standard Time": "America/Los_Angeles",
    "GMT Standard Time": "Europe/London",
    "Mountain Standard Time": "America/Denver",
    "India Standard Time": "Asia/Kolkata",
}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(ZONES.get(name, name))
    except (ZoneInfoNotFoundError, ValueError):
        raise c.graph_error(400, "ErrorInvalidTimeZone", f"The time zone '{name}' is invalid.") from None


def parse(value: str, timezone: str = "UTC") -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=zone(timezone))).astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        raise c.graph_error(400, "ErrorInvalidParameter", "The date/time value is invalid.") from None


def datetime_zone(value: str, timezone: str = "UTC") -> dict[str, str]:
    dt = parse(value).astimezone(zone(timezone))
    return {"dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0", "timeZone": timezone}


def preferred(request: Request) -> str:
    match = re.search(r'outlook.timezone="([^"]+)"', request.headers.get("prefer", ""), re.I)
    return match[1] if match else "UTC"


def shape(request: Request, record: dict[str, Any]) -> dict[str, Any]:
    result = s.clean(record)
    timezone = preferred(request)
    for key in ("start", "end"):
        value = record[key]
        result[key] = datetime_zone(isoformat_z(parse(value["dateTime"], value["timeZone"])), timezone)
    if 'outlook.body-content-type="text"' in request.headers.get("prefer", "").lower():
        result["body"] = {"contentType": "text", "content": s.text_body(result["body"]["content"])}
    return result


def calendar_id(ctx: ProviderContext, uid: str) -> str:
    return "AAMkA" + stable_hash(ctx.world.seed, "calendar", uid, length=64)


def calendar(ctx: ProviderContext, owner: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": calendar_id(ctx, owner["id"]),
        "name": "Calendar",
        "color": "auto",
        "hexColor": "",
        "isDefaultCalendar": True,
        "changeKey": stable_hash(owner["id"]),
        "canShare": True,
        "canViewPrivateItems": True,
        "canEdit": True,
        "isRemovable": False,
        "isTallyingResponses": True,
        "allowedOnlineMeetingProviders": ["teamsForBusiness"],
        "defaultOnlineMeetingProvider": "teamsForBusiness",
        "owner": {"name": owner["displayName"], "address": owner["mail"]},
    }


def get_event(ctx: ProviderContext, uid: str, eid: str) -> dict[str, Any]:
    record = ctx.store["calendar_events"].get(eid)
    if not record or record["_owner"] != uid:
        raise c.graph_error(404, "ErrorItemNotFound", "The specified object was not found in the store.")
    return record


def event(
    ctx: ProviderContext, uid: str, eid: str, payload: dict[str, Any], organizer: dict[str, Any]
) -> dict[str, Any]:
    stamp = s.now(ctx)
    start = payload.get("start", {"dateTime": stamp, "timeZone": "UTC"})
    end = payload.get(
        "end", {"dateTime": isoformat_z(ctx.clock.now() + timedelta(hours=1)), "timeZone": "UTC"}
    )
    start_dt, end_dt = (
        parse(start["dateTime"], start.get("timeZone", "UTC")),
        parse(end["dateTime"], end.get("timeZone", "UTC")),
    )
    if end_dt <= start_dt:
        raise c.graph_error(400, "ErrorInvalidParameter", "End time must be after start time.")
    body = payload.get("body", {"contentType": "html", "content": ""})
    ck = stable_hash(eid, stamp)
    result = {
        "id": eid,
        "createdDateTime": stamp,
        "lastModifiedDateTime": stamp,
        "changeKey": ck,
        "categories": [],
        "transactionId": payload.get("transactionId"),
        "originalStartTimeZone": start.get("timeZone", "UTC"),
        "originalEndTimeZone": end.get("timeZone", "UTC"),
        "iCalUId": stable_hash(eid, length=64),
        "reminderMinutesBeforeStart": 15,
        "isReminderOn": True,
        "hasAttachments": False,
        "subject": payload.get("subject", ""),
        "bodyPreview": s.text_body(body["content"])[:255],
        "body": body,
        "importance": "normal",
        "sensitivity": "normal",
        "isAllDay": False,
        "isCancelled": False,
        "isOrganizer": True,
        "responseRequested": True,
        "seriesMasterId": None,
        "showAs": "busy",
        "type": "singleInstance",
        "webLink": f"https://outlook.office365.com/owa/?itemid={eid}&exvsurl=1&path=/calendar/item",
        "onlineMeetingUrl": None,
        "isOnlineMeeting": False,
        "onlineMeetingProvider": "unknown",
        "onlineMeeting": None,
        "allowNewTimeProposals": True,
        "recurrence": None,
        "responseStatus": {"response": "organizer", "time": stamp},
        "start": datetime_zone(isoformat_z(start_dt)),
        "end": datetime_zone(isoformat_z(end_dt)),
        "location": {
            "displayName": "",
            "locationType": "default",
            "uniqueIdType": "unknown",
            "address": {},
            "coordinates": {},
        },
        "locations": [],
        "attendees": [],
        "organizer": organizer,
        "@odata.etag": f'W/"{ck}"',
        "_owner": uid,
    }
    for key in (
        "subject",
        "importance",
        "sensitivity",
        "isAllDay",
        "showAs",
        "categories",
        "location",
        "locations",
        "attendees",
        "responseRequested",
        "allowNewTimeProposals",
        "isReminderOn",
        "reminderMinutesBeforeStart",
        "recurrence",
    ):
        if key in payload:
            result[key] = copy.deepcopy(payload[key])
    for attendee in result["attendees"]:
        attendee.setdefault("type", "required")
        attendee.setdefault("status", {"response": "none", "time": "0001-01-01T00:00:00Z"})
    if result["location"]["displayName"] and not result["locations"]:
        result["locations"] = [copy.deepcopy(result["location"])]
    if payload.get("isOnlineMeeting"):
        join = payload.get("onlineMeeting", {}).get("joinUrl") or (
            f"https://teams.microsoft.com/l/meetup-join/19%3ameeting_{ctx.ids.urlsafe(32)}%40thread.v2/0"
        )
        result.update(
            {
                "isOnlineMeeting": True,
                "onlineMeetingProvider": "teamsForBusiness",
                "onlineMeeting": {"joinUrl": join},
            }
        )
        result["body"] = {
            "contentType": "html",
            "content": body["content"]
            + f'<br><div>Microsoft Teams meeting<br><a href="{join}">Join the meeting now</a></div>',
        }
    if result["recurrence"]:
        from emulators.providers.microsoft.calendar_recurrence import validate

        validate(result["recurrence"])
        result["type"] = "seriesMaster"
        result["_runtime_series"] = True
    return result


def from_meeting(ctx: ProviderContext, meeting: Meeting, pid: str) -> dict[str, Any]:
    world = ctx.world
    uid = c.user_id(world, pid)
    link = (
        c.join_web_url(world, meeting)
        if meeting.platform == "teams"
        else (
            f"https://zoom.us/j/{int(stable_hash(meeting.id, length=12), 16) % 10**11:011d}"
            if meeting.platform == "zoom"
            else f"https://meet.google.com/{stable_hash(meeting.id, length=10)[:3]}-{stable_hash(meeting.id, length=10)[3:7]}-{stable_hash(meeting.id, length=10)[7:]}"
        )
    )
    responses = dict(meeting.responses)
    payload = {
        "subject": meeting.title,
        "start": {"dateTime": isoformat_z(meeting.start), "timeZone": "UTC"},
        "end": {"dateTime": isoformat_z(meeting.end), "timeZone": "UTC"},
        "body": {
            "contentType": "html",
            "content": meeting.description
            + ("" if meeting.platform == "teams" else f'<br><a href="{link}">{link}</a>'),
        },
        "attendees": [
            {
                **c.email_address(world.person(a)),
                "type": "optional" if a in meeting.optional_ids else "required",
                "status": {
                    "response": responses.get(a, "none"),
                    "time": isoformat_z(meeting.start - timedelta(days=1)),
                },
            }
            for a in (*meeting.attendee_ids, *meeting.optional_ids)
        ],
        "isOnlineMeeting": meeting.platform == "teams",
        "onlineMeeting": {"joinUrl": link},
        "location": {
            "displayName": meeting.location
            or {"zoom": "Zoom Meeting", "meet": "Google Meet", "teams": "Microsoft Teams Meeting"}[
                meeting.platform
            ],
            "locationType": "default",
            "uniqueIdType": "unknown",
        },
    }
    record = event(
        ctx,
        uid,
        c.event_id(world, meeting.id, pid),
        payload,
        c.email_address(world.person(meeting.organizer_id)),
    )
    record.update(
        {
            "createdDateTime": isoformat_z(meeting.start - timedelta(days=7)),
            "lastModifiedDateTime": isoformat_z(meeting.start - timedelta(days=1)),
            "_meeting": meeting.id,
            "originalStartTimeZone": next(
                (office.windows_timezone for office in world.offices if office.timezone == meeting.timezone),
                next((k for k, v in ZONES.items() if v == meeting.timezone), "UTC"),
            ),
            "isCancelled": meeting.cancelled,
            "isOrganizer": pid == meeting.organizer_id,
            "iCalUId": stable_hash(world.seed, meeting.id, length=64),
            "responseStatus": {
                "response": "organizer" if pid == meeting.organizer_id else responses.get(pid, "none"),
                "time": isoformat_z(meeting.start - timedelta(days=1)),
            },
        }
    )
    record["changeKey"] = stable_hash(ctx.world.seed, meeting.id, pid, "event")
    record["@odata.etag"] = f'W/"{record["changeKey"]}"'
    record["originalEndTimeZone"] = record["originalStartTimeZone"]
    return record


def seed(ctx: ProviderContext) -> None:
    series: dict[tuple[str, str], list[tuple[Meeting, dict[str, Any]]]] = {}
    for meeting in ctx.world.meetings:
        for pid in meeting.all_attendee_ids():
            if ctx.world.person(pid).is_external:
                continue
            record = from_meeting(ctx, meeting, pid)
            ctx.store["calendar_events"].put(record)
            if meeting.series_id:
                series.setdefault((meeting.series_id, pid), []).append((meeting, record))
    for (sid, pid), entries in series.items():
        entries.sort(key=lambda e: e[0].start)
        first, last = entries[0][0], entries[-1][0]
        master = copy.deepcopy(entries[0][1])
        master_id = c.event_id(ctx.world, sid, pid)
        pattern: dict[str, Any] = {
            "type": "weekly",
            "interval": 2 if first.recurrence == "biweekly" else 1,
            "daysOfWeek": [DAYS[first.start.weekday()]],
            "firstDayOfWeek": "sunday",
        }
        if first.recurrence == "monthly":
            pattern = {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": first.start.day}
        master.update(
            {
                "id": master_id,
                "type": "seriesMaster",
                "recurrence": {
                    "pattern": pattern,
                    "range": {
                        "type": "endDate",
                        "startDate": first.start.date().isoformat(),
                        "endDate": last.start.date().isoformat(),
                        "recurrenceTimeZone": master["originalStartTimeZone"],
                        "numberOfOccurrences": 0,
                    },
                },
            }
        )
        ctx.store["calendar_events"].put(master)
        for meeting, record in entries:
            record.update(
                {
                    "type": "occurrence",
                    "seriesMasterId": master_id,
                    "originalStart": isoformat_z(meeting.start),
                }
            )


async def changed(ctx: ProviderContext, record: dict[str, Any], kind: str) -> None:
    await c.publish_change(ctx, f"users/{record['_owner']}/events/{record['id']}", kind, record)


def bounds(request: Request) -> tuple[datetime, datetime]:
    start, end = request.query_params.get("startDateTime"), request.query_params.get("endDateTime")
    if not start or not end:
        raise c.graph_error(400, "ErrorInvalidParameter", "Both startDateTime and endDateTime are required.")
    low, high = parse(start), parse(end)
    if high <= low:
        raise c.graph_error(400, "ErrorInvalidParameter", "End time must be after start time.")
    return low, high


def in_range(record: dict[str, Any], low: datetime, high: datetime) -> bool:
    return (
        parse(record["start"]["dateTime"], record["start"]["timeZone"]) < high
        and parse(record["end"]["dateTime"], record["end"]["timeZone"]) > low
    )
