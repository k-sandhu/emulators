"""Calendar projections, sharing, recurrence expansion and sync cursors."""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request

from emulators.core.clock import isoformat_z
from emulators.core.ids import b64url, b64url_decode, stable_hash
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.google.common import google_error, meet_code, meet_uri, principal, user_id
from emulators.world.model import Meeting

PREFIX = "/calendar/v3"
REMINDERS = [{"method": "popup", "minutes": 10}]


def public(record: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy({k: v for k, v in record.items() if not k.startswith("_")})


def identity(request: Request, ctx: ProviderContext) -> str:
    actor = principal(request, ctx)
    person = next((p for p in ctx.world.employees if p.id == actor.person_id and p.active), None)
    if not person or actor.app_only or not ctx.store["calendar_calendars"].get(person.email):
        raise google_error(403, "Calendar requires a user credential.", reason="forbidden")
    if actor.scopes:
        path = request.url.path
        reading = request.method == "GET" or path.endswith("/watch")
        allowed = {"calendar"}
        if "/settings" in path:
            allowed |= {"calendar.readonly", "calendar.settings.readonly"}
        elif "/calendarList" in path:
            allowed.add("calendar.calendarlist")
            if reading:
                allowed |= {"calendar.calendarlist.readonly", "calendar.readonly"}
        elif "/acl" in path:
            allowed |= {"calendar.acls", "calendar.acls.readonly"}
        elif "/events" in path:
            allowed |= {"calendar.app.created", "calendar.events", "calendar.events.owned"}
            if reading:
                allowed |= {"calendar.events.readonly", "calendar.events.owned.readonly", "calendar.readonly"}
        elif path.endswith("/freeBusy"):
            allowed |= {"calendar.freebusy", "calendar.events.freebusy", "calendar.readonly"}
        elif path.endswith(("/colors", "/channels/stop")):
            allowed |= {
                scope.removeprefix("https://www.googleapis.com/auth/")
                for scope in actor.scopes
                if scope.startswith("https://www.googleapis.com/auth/calendar")
            }
        else:
            allowed |= {"calendar.app.created", "calendar.calendars"}
            if reading:
                allowed |= {"calendar.calendars.readonly", "calendar.readonly"}
        if not {"https://www.googleapis.com/auth/" + scope for scope in allowed}.intersection(actor.scopes):
            raise google_error(
                403, "Request had insufficient authentication scopes.", reason="insufficientPermissions"
            )
    return actor.person_id


def calendar_access(
    request: Request, ctx: ProviderContext, calendar_id: str, *, write: bool = False, busy: bool = False
) -> tuple[dict[str, Any], str, str]:
    actor = identity(request, ctx)
    email = ctx.world.person(actor).email
    calendar_id = email if calendar_id == "primary" else calendar_id
    cal = ctx.store["calendar_calendars"].get(calendar_id)
    role = cal.get("_acl", {}).get(actor) if cal else None
    if not role and cal and busy:
        role = cal.get("_domainRole")
    if not cal or not role or (role == "freeBusyReader" and not busy):
        raise google_error(404, "Not Found", reason="notFound")
    if write and role not in ("owner", "writer"):
        raise google_error(403, "You need to have writer access to this calendar.", reason="forbidden")
    return cal, role, actor


def conference_data(ctx: ProviderContext, key: str) -> dict[str, Any]:
    code = meet_code(ctx.world, key)
    return {
        "entryPoints": [
            {"entryPointType": "video", "uri": meet_uri(ctx.world, key), "label": "meet.google.com/" + code},
            {"entryPointType": "more", "uri": f"https://tel.meet/{code}"},
        ],
        "conferenceSolution": {
            "key": {"type": "hangoutsMeet"},
            "name": "Google Meet",
            "iconUri": "https://fonts.gstatic.com/s/i/productlogos/meet_2020q4/v6/web-512dp/logo_meet_2020q4_color_2x_web_512dp.png",
        },
        "conferenceId": code,
    }


def _event(ctx: ProviderContext, meeting: Meeting, calendar_id: str, key: str) -> dict[str, Any]:
    organizer = ctx.world.person(meeting.organizer_id)
    responses = dict(meeting.responses)
    response_map = {"tentativelyAccepted": "tentative", "none": "needsAction"}
    event_id = stable_hash(ctx.world.seed, "calendar", key, length=32)
    record: dict[str, Any] = {
        "kind": "calendar#event",
        "id": event_id,
        "etag": '"' + stable_hash(event_id, meeting.start) + '"',
        "status": "cancelled" if meeting.cancelled else "confirmed",
        "htmlLink": f"https://www.google.com/calendar/event?eid={b64url((event_id + ' ' + calendar_id).encode())}",
        "created": isoformat_z(meeting.start - timedelta(days=14)),
        "updated": isoformat_z(meeting.start - timedelta(days=1)),
        "summary": meeting.title,
        "description": meeting.description,
        "creator": {"email": organizer.email, "displayName": organizer.display_name},
        "organizer": {"email": organizer.email, "displayName": organizer.display_name},
        "start": {
            "dateTime": meeting.start.astimezone(ZoneInfo(meeting.timezone)).isoformat(),
            "timeZone": meeting.timezone,
        },
        "end": {
            "dateTime": meeting.end.astimezone(ZoneInfo(meeting.timezone)).isoformat(),
            "timeZone": meeting.timezone,
        },
        "iCalUID": event_id + "@google.com",
        "sequence": 0,
        "attendees": [
            {
                "id": user_id(ctx.world, pid),
                "email": ctx.world.person(pid).email,
                "displayName": ctx.world.person(pid).display_name,
                "responseStatus": "accepted"
                if pid == meeting.organizer_id
                else response_map.get(responses.get(pid, "none"), responses.get(pid, "needsAction")),
                **({"organizer": True} if pid == meeting.organizer_id else {}),
                **({"optional": True} if pid in meeting.optional_ids else {}),
            }
            for pid in meeting.all_attendee_ids()
        ],
        "reminders": {"useDefault": True},
        "eventType": "default",
        "guestsCanModify": False,
        "guestsCanInviteOthers": True,
        "guestsCanSeeOtherGuests": True,
        "_calendar": calendar_id,
        "_rev": 0,
        "_meeting": meeting.id,
    }
    if meeting.location:
        record["location"] = meeting.location
    if meeting.platform == "meet":
        record["hangoutLink"] = meet_uri(ctx.world, meeting.id)
        record["conferenceData"] = conference_data(ctx, meeting.id)
    elif meeting.platform == "zoom":
        link = "https://zoom.us/j/" + str(int(stable_hash(ctx.world.seed, "zoom", meeting.id), 16))[:11]
        record["location"] = link
        record["description"] += "\nJoin Zoom: " + link
    else:
        record["description"] += (
            "\nJoin Microsoft Teams: https://teams.microsoft.com/l/meetup-join/"
            + stable_hash(ctx.world.seed, meeting.id)
        )
    return record


def seed(ctx: ProviderContext) -> None:
    ctx.store.meta["calendar_revision"] = 0
    for person in ctx.world.employees:
        if not person.active:
            continue
        acl = {person.id: "owner"}
        for colleague in ctx.world.employees:
            if (
                colleague.active
                and colleague.department_id == person.department_id
                and colleague.id != person.id
            ):
                acl[colleague.id] = "reader"
            if "Executive Assistant" in colleague.job_title and colleague.manager_id == person.id:
                acl[colleague.id] = "writer"
        cal = {
            "kind": "calendar#calendar",
            "id": person.email,
            "etag": '"' + stable_hash(person.email) + '"',
            "summary": person.display_name,
            "timeZone": person.timezone,
            "_owner": person.id,
            "_acl": acl,
            "_domainRole": "freeBusyReader",
        }
        ctx.store["calendar_calendars"].put(cal)
        for pid, role in acl.items():
            ctx.store["calendar_list"].put_as(
                pid + ":" + person.email, list_entry(cal, role, pid == person.id)
            )
    groups: dict[str, list[Meeting]] = {}
    for meeting in ctx.world.meetings:
        groups.setdefault(meeting.series_id or meeting.id, []).append(meeting)
    for key, meetings in groups.items():
        meetings.sort(key=lambda m: m.start)
        first = meetings[0]
        members = dict.fromkeys(
            pid for m in meetings for pid in m.all_attendee_ids() if not ctx.world.person(pid).is_external
        )
        for pid in members:
            email = ctx.world.person(pid).email
            if not ctx.store["calendar_calendars"].get(email):
                continue
            own = [m for m in meetings if pid in m.all_attendee_ids()]
            if not own:
                continue
            master = _event(ctx, first, email, key)
            if first.recurrence:
                freq = "MONTHLY" if first.recurrence == "monthly" else "WEEKLY"
                interval = ";INTERVAL=2" if first.recurrence == "biweekly" else ""
                until = meetings[-1].start.strftime("%Y%m%dT%H%M%SZ")
                master["recurrence"] = [f"RRULE:FREQ={freq}{interval};UNTIL={until}"]
                for meeting in own:
                    instance = _event(ctx, meeting, email, key)
                    instance["id"] = master["id"] + "_" + meeting.start.strftime("%Y%m%dT%H%M%SZ")
                    instance["recurringEventId"] = master["id"]
                    instance["originalStartTime"] = dict(instance["start"])
                    instance["_seedInstance"] = True
                    ctx.store["calendar_instances"].put_as(email + ":" + instance["id"], instance)
                master["_seedSeries"] = True
            ctx.store["calendar_events"].put_as(email + ":" + master["id"], master)


def list_entry(cal: dict[str, Any], role: str, primary: bool = False) -> dict[str, Any]:
    return {
        "kind": "calendar#calendarListEntry",
        "id": cal["id"],
        "etag": cal["etag"],
        "summary": cal["summary"],
        "timeZone": cal["timeZone"],
        "accessRole": role,
        "colorId": "9",
        "backgroundColor": "#7bd148",
        "foregroundColor": "#000000",
        "selected": True,
        "defaultReminders": copy.deepcopy(REMINDERS),
        "notificationSettings": {"notifications": [{"type": "eventCreation", "method": "email"}]},
        **({"primary": True} if primary else {}),
    }


def instant(value: dict[str, Any]) -> datetime:
    try:
        stamp = value.get("dateTime") or value["date"]
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(value.get("timeZone", "UTC")))
        return parsed.astimezone(UTC)
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise google_error(400, "Invalid event time", reason="badRequest") from exc


def validate_event(record: dict[str, Any]) -> None:
    if not record.get("start") or not record.get("end"):
        raise google_error(400, "Missing end time.", reason="required")
    if ("date" in record["start"]) != ("date" in record["end"]) or instant(record["end"]) <= instant(
        record["start"]
    ):
        raise google_error(400, "The specified time range is empty.", reason="timeRangeEmpty")
    for rule in record.get("recurrence", []):
        if (
            not isinstance(rule, str)
            or not rule.startswith("RRULE:")
            or not re.search(r"FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(;|$)", rule)
        ):
            raise google_error(400, "Invalid recurrence rule", reason="badRequest")
        try:
            parts = dict(item.split("=", 1) for item in rule[6:].split(";"))
            if set(parts) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "WKST"}:
                raise ValueError
            if int(parts.get("INTERVAL", "1")) < 1 or int(parts.get("COUNT", "1")) < 1:
                raise ValueError
            if "UNTIL" in parts:
                datetime.strptime(parts["UNTIL"], "%Y%m%dT%H%M%SZ")
            if "BYDAY" in parts and (
                parts["FREQ"] != "WEEKLY"
                or not re.fullmatch(r"(?:MO|TU|WE|TH|FR|SA|SU)(?:,(?:MO|TU|WE|TH|FR|SA|SU))*", parts["BYDAY"])
            ):
                raise ValueError
            if "BYMONTHDAY" in parts and (
                parts["FREQ"] != "MONTHLY"
                or any(not 1 <= int(day) <= 31 for day in parts["BYMONTHDAY"].split(","))
            ):
                raise ValueError
            if "WKST" in parts and parts["WKST"] != "MO":
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise google_error(400, "Invalid or unsupported recurrence rule", reason="badRequest") from exc
    if len(record.get("recurrence", [])) > 1:
        raise google_error(400, "Multiple recurrence rules are not supported", reason="badRequest")


def event_output(
    request: Request, ctx: ProviderContext, event: dict[str, Any], cal: dict[str, Any], actor: str, role: str
) -> dict[str, Any]:
    result = public(event)
    email = ctx.world.person(actor).email
    for field in ("creator", "organizer"):
        if field in result:
            result[field].pop("self", None)
            if result[field].get("email") == cal["id"]:
                result[field]["self"] = True
    for attendee in result.get("attendees", []):
        attendee.pop("self", None)
        if attendee.get("email") == cal["id"]:
            attendee["self"] = True
    if result.get("visibility") == "private" and role == "reader" and cal["id"] != email:
        result = {
            key: value
            for key, value in result.items()
            if key
            in ("kind", "id", "etag", "status", "start", "end", "recurringEventId", "originalStartTime")
        }
        result["summary"] = "Busy"
    if "hangoutLink" in result:
        code = result.get("conferenceData", {}).get("conferenceId")
        if code:
            result["hangoutLink"] = url_for_host(request, ctx, "meet.google.com", "/" + code)
            for entry in result["conferenceData"].get("entryPoints", []):
                if entry.get("entryPointType") == "video":
                    entry["uri"] = result["hangoutLink"]
    if "htmlLink" in result:
        result["htmlLink"] = url_for_host(
            request,
            ctx,
            "www.google.com",
            "/calendar/event?eid=" + b64url((event["id"] + " " + cal["id"]).encode()),
        )
    return result


def expand(
    ctx: ProviderContext, master: dict[str, Any], low: datetime | None = None, high: datetime | None = None
) -> list[dict[str, Any]]:
    if "recurrence" not in master:
        return [master]
    calendar_id = master["_calendar"]
    overrides = {
        r["id"]: r
        for r in ctx.store["calendar_instances"].where(_calendar=calendar_id, recurringEventId=master["id"])
    }
    if master.get("_seedSeries"):
        rows = list(overrides.values())
        if master["status"] == "cancelled":
            rows = [
                dict(r, status="cancelled", _rev=max(r.get("_rev", 0), master.get("_rev", 0))) for r in rows
            ]
        return [
            r
            for r in rows
            if (not low or instant(r["end"]) > low) and (not high or instant(r["start"]) < high)
        ]
    rule = dict(part.split("=", 1) for part in master["recurrence"][0][6:].split(";") if "=" in part)
    tz = ZoneInfo(master["start"].get("timeZone", "UTC"))
    start = instant(master["start"]).astimezone(tz)
    duration = instant(master["end"]) - instant(master["start"])
    until = (
        datetime.strptime(rule["UNTIL"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) if "UNTIL" in rule else None
    )
    count = int(rule.get("COUNT", 10000))
    interval = int(rule.get("INTERVAL", 1))
    if count < 1 or interval < 1:
        raise google_error(400, "Invalid recurrence rule", reason="badRequest")
    boundary = high or until or (start + timedelta(days=366))
    days = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
    results, emitted = [], 0
    for step in range(36600):
        current = start + timedelta(days=step)
        if current > boundary or (until and current > until) or emitted >= count:
            break
        freq = rule.get("FREQ")
        if freq == "DAILY":
            selected = step % interval == 0
        elif freq == "WEEKLY":
            selected = ((step + start.weekday()) // 7) % interval == 0 and days[
                current.weekday()
            ] in rule.get("BYDAY", days[start.weekday()]).split(",")
        elif freq == "MONTHLY":
            selected = (
                (current.year - start.year) * 12 + current.month - start.month
            ) % interval == 0 and current.day in [
                int(day) for day in rule.get("BYMONTHDAY", str(start.day)).split(",")
            ]
        else:
            selected = (current.year - start.year) % interval == 0 and (current.month, current.day) == (
                start.month,
                start.day,
            )
        if not selected:
            continue
        emitted += 1
        if low and current + duration <= low:
            continue
        event = copy.deepcopy(master)
        event.pop("recurrence", None)
        event["id"] = master["id"] + "_" + current.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        for field, stamp in (("start", current), ("end", current + duration)):
            event[field] = (
                {"date": stamp.date().isoformat()}
                if "date" in master[field]
                else {"dateTime": stamp.isoformat(), "timeZone": str(tz)}
            )
        event["recurringEventId"] = master["id"]
        event["originalStartTime"] = dict(event["start"])
        results.append(overrides.get(event["id"], event))
    return results


def revision(ctx: ProviderContext, record: dict[str, Any]) -> None:
    value = ctx.store.meta["calendar_revision"] + 1
    ctx.store.meta["calendar_revision"] = value
    record["_rev"] = value
    record["updated"] = isoformat_z(ctx.clock.now(), timespec="microseconds")
    record["sequence"] = record.get("sequence", 0) + 1
    record["etag"] = '"' + stable_hash(record["id"], value, record["updated"]) + '"'


def sync_token(ctx: ProviderContext, calendar_id: str, actor: str) -> str:
    token = "sync_" + ctx.ids.urlsafe(32)
    ctx.store["calendar_sync"].put(
        {
            "id": token,
            "calendar": calendar_id,
            "actor": actor,
            "revision": ctx.store.meta["calendar_revision"],
            "expires": ctx.clock.timestamp() + 7 * 86400,
        }
    )
    return token


def page(
    request: Request,
    rows: list[dict[str, Any]],
    *,
    default: int = 250,
    maximum: int = 2500,
    binding: str = "",
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        size = int(request.query_params.get("maxResults", default))
        if size <= 0:
            raise ValueError
        size = min(size, maximum)
        binding = stable_hash(
            request.url.path,
            binding,
            sorted(
                (k, v) for k, v in request.query_params.multi_items() if k not in ("pageToken", "maxResults")
            ),
        )
        token = request.query_params.get("pageToken")
        decoded = json.loads(b64url_decode(token)) if token else [binding, 0]
        if decoded[0] != binding or not isinstance(decoded[1], int) or decoded[1] < 0:
            raise ValueError
        offset = decoded[1]
    except (ValueError, TypeError, IndexError, UnicodeError) as exc:
        raise google_error(400, "Invalid page token or page size", reason="invalid") from exc
    next_token = b64url(json.dumps([binding, offset + size]).encode()) if offset + size < len(rows) else None
    return rows[offset : offset + size], next_token
