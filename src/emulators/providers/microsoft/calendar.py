"""Graph calendars, expanded views, event actions and scheduling."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import calendar_helpers as h
from emulators.providers.microsoft import calendar_recurrence as recurrence
from emulators.providers.microsoft import common as c
from emulators.providers.microsoft import directory_support as s
from emulators.providers.microsoft.calendar_helpers import seed  # noqa: F401


def register(app: FastAPI, ctx: ProviderContext) -> None:
    router = APIRouter()

    async def calendars(
        request: Request, identifier: str = "me", cid: str | None = None, gid: str | None = None
    ) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        record = h.calendar(ctx, owner)
        if cid and cid != record["id"]:
            raise c.not_found(cid)
        path = s.local_path(request)
        if path.endswith("calendarGroups"):
            return s.collection(
                request,
                [
                    {
                        "id": c.object_id(ctx.world, "calendarGroup", owner["id"]),
                        "name": "My Calendars",
                        "classId": "0006f0b7-0000-0000-c000-000000000046",
                        "changeKey": record["changeKey"],
                    }
                ],
                "calendarGroups",
            )
        if path.endswith("calendars"):
            return s.collection(request, [record], "calendars")
        return s.single(request, record, "calendars/$entity")

    async def events(request: Request, identifier: str = "me", cid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        if cid and cid != h.calendar_id(ctx, owner["id"]):
            raise c.not_found(cid)
        rows = ctx.store["calendar_events"].where(_owner=owner["id"])
        view = "calendarView" in s.local_path(request)
        if view:
            low, high = h.bounds(request)
            rows = [r for r in rows if r["type"] != "seriesMaster" and h.in_range(r, low, high)]
            for master in ctx.store["calendar_events"].where(
                _owner=owner["id"], type="seriesMaster", _runtime_series=True
            ):
                for instance in recurrence.instances(master, low, high):
                    ctx.store["calendar_events"].put(instance)
                    rows = [r for r in rows if r["id"] != instance["id"]]
                    rows.append(instance)
        else:
            rows = [r for r in rows if r["type"] in {"seriesMaster", "singleInstance"}]
        if request.url.path.endswith("/delta"):
            result = s.delta(request, ctx, rows, f"users/{owner['id']}/events", "events")
            result["value"] = [h.shape(request, r) if "start" in r else r for r in result["value"]]
            return result
        return s.collection(
            request,
            [h.shape(request, r) for r in rows],
            "events",
            size=10,
            maximum=1000,
            search=("subject", "bodyPreview"),
        )

    async def get_event(request: Request, eid: str, identifier: str = "me", cid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        record = h.get_event(ctx, owner["id"], eid)
        if cid and cid != h.calendar_id(ctx, owner["id"]):
            raise c.not_found(cid)
        if request.url.path.endswith("/instances"):
            low, high = h.bounds(request)
            if record.get("_runtime_series") and record["type"] == "seriesMaster":
                for instance in recurrence.instances(record, low, high):
                    ctx.store["calendar_events"].put(instance)
            rows = [
                r
                for r in ctx.store["calendar_events"].where(_owner=owner["id"], seriesMasterId=eid)
                if h.in_range(r, low, high)
            ]
            return s.collection(request, [h.shape(request, r) for r in rows], "events")
        return s.single(request, h.shape(request, record), "events/$entity")

    async def create(request: Request, identifier: str = "me", cid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        if cid and cid != h.calendar_id(ctx, owner["id"]):
            raise c.not_found(cid)
        payload = await request.json()
        if not all(k in payload for k in ("start", "end")):
            raise c.graph_error(400, "ErrorInvalidParameter", "Start and end are required.")
        if payload.get("transactionId"):
            existing = ctx.store["calendar_events"].first(
                _owner=owner["id"], transactionId=payload["transactionId"]
            )
            if existing:
                return JSONResponse(
                    s.single(request, h.shape(request, existing), "events/$entity"), status_code=201
                )
        organizer = {"emailAddress": {"name": owner["displayName"], "address": owner["mail"]}}
        record = h.event(ctx, owner["id"], s.outlook_id(ctx), payload, organizer)
        ctx.store["calendar_events"].put(record)
        await h.changed(ctx, record, "created")
        await invite(record)
        return JSONResponse(s.single(request, h.shape(request, record), "events/$entity"), status_code=201)

    async def invite(record: dict[str, Any]) -> None:
        for attendee in record["attendees"]:
            address = attendee["emailAddress"]["address"].lower()
            target = next(
                (
                    u
                    for u in ctx.store["users"]
                    if str(u.get("mail", "")).lower() == address and u.get("userType") == "Member"
                ),
                None,
            )
            if target and target["id"] != record["_owner"]:
                copied = copy.deepcopy(record)
                copied.update(
                    {
                        "id": s.outlook_id(ctx),
                        "_owner": target["id"],
                        "isOrganizer": False,
                        "responseStatus": {"response": "notResponded", "time": "0001-01-01T00:00:00Z"},
                        "_organizer_event": record["id"],
                    }
                )
                ctx.store["calendar_events"].put(copied)
                await h.changed(ctx, copied, "created")

    async def patch(request: Request, eid: str, identifier: str = "me", cid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        record = h.get_event(ctx, owner["id"], eid)
        payload = await request.json()
        merged = {**record, **payload}
        # Validate and normalize dates while preserving the original event identity.
        validated = h.event(ctx, owner["id"], eid, merged, record["organizer"])
        if record["type"] == "seriesMaster" and record.get("_runtime_series"):
            for instance in ctx.store["calendar_events"].where(_owner=owner["id"], seriesMasterId=eid):
                ctx.store["calendar_events"].delete(instance["id"])
        for key in payload:
            if key in validated and key not in {"id", "organizer", "isOrganizer", "createdDateTime"}:
                record[key] = validated[key]
        if "isOnlineMeeting" in payload:
            for key in ("onlineMeeting", "onlineMeetingProvider", "body"):
                record[key] = validated[key]
        record.update({"lastModifiedDateTime": s.now(ctx), "changeKey": ctx.ids.urlsafe(24)})
        record["@odata.etag"] = f'W/"{record["changeKey"]}"'
        if record.get("_runtime_series"):
            record["type"] = "seriesMaster" if record["recurrence"] else "singleInstance"
        await h.changed(ctx, record, "updated")
        await synchronize(record)
        return s.single(request, h.shape(request, record), "events/$entity")

    async def synchronize(record: dict[str, Any]) -> None:
        if not record["isOrganizer"]:
            return
        keys = (
            "subject",
            "body",
            "bodyPreview",
            "start",
            "end",
            "location",
            "locations",
            "attendees",
            "isCancelled",
            "isOnlineMeeting",
            "onlineMeetingProvider",
            "onlineMeeting",
            "lastModifiedDateTime",
        )
        for sibling in ctx.store["calendar_events"]:
            if sibling["id"] != record["id"] and sibling["iCalUId"] == record["iCalUId"]:
                sibling.update({key: copy.deepcopy(record[key]) for key in keys})
                await h.changed(ctx, sibling, "updated")

    async def delete(request: Request, eid: str, identifier: str = "me", cid: str | None = None) -> Response:
        owner = s.mailbox(request, ctx, identifier)
        record = h.get_event(ctx, owner["id"], eid)
        rows = [record, *ctx.store["calendar_events"].where(_owner=owner["id"], seriesMasterId=eid)]
        for row in rows:
            ctx.store["calendar_events"].delete(row["id"])
            await h.changed(ctx, row, "deleted")
        return Response(status_code=204)

    async def action(request: Request, eid: str, identifier: str = "me", cid: str | None = None) -> Response:
        owner = s.mailbox(request, ctx, identifier)
        record = h.get_event(ctx, owner["id"], eid)
        name = request.url.path.rsplit("/", 1)[-1]
        payload = await request.json() if await request.body() else {}
        if name == "cancel":
            if not record["isOrganizer"]:
                raise c.graph_error(400, "ErrorInvalidOperation", "Only the organizer can cancel a meeting.")
            record["isCancelled"] = True
        elif name == "forward":
            forwarded = copy.deepcopy(record)
            forwarded["attendees"] = [{**r, "type": "required"} for r in payload.get("toRecipients", [])]
            await invite(forwarded)
        else:
            if record["isOrganizer"]:
                raise c.graph_error(
                    400, "ErrorInvalidOperation", "The meeting organizer cannot respond to the meeting."
                )
            response = {
                "accept": "accepted",
                "decline": "declined",
                "tentativelyAccept": "tentativelyAccepted",
            }[name]
            record["responseStatus"] = {"response": response, "time": s.now(ctx)}
            for sibling in ctx.store["calendar_events"]:
                if sibling["iCalUId"] == record["iCalUId"]:
                    for attendee in sibling["attendees"]:
                        if attendee["emailAddress"]["address"] == owner["mail"]:
                            attendee["status"] = copy.deepcopy(record["responseStatus"])
        await h.changed(ctx, record, "updated")
        await synchronize(record)
        return Response(status_code=202)

    async def schedule(request: Request, identifier: str = "me") -> dict[str, Any]:
        s.mailbox(request, ctx, identifier)
        caller = c.principal(request, ctx)
        body = await request.json()
        start, end = body.get("startTime", {}), body.get("endTime", {})
        low = h.parse(start.get("dateTime"), start.get("timeZone", "UTC"))
        high = h.parse(end.get("dateTime"), end.get("timeZone", "UTC"))
        interval = body.get("availabilityViewInterval", 30)
        if (
            not isinstance(interval, int)
            or not 5 <= interval <= 1440
            or high <= low
            or high - low > timedelta(days=62)
        ):
            raise c.graph_error(
                400, "ErrorInvalidParameter", "Invalid schedule range or availabilityViewInterval."
            )
        values = []
        for address in body.get("schedules", []):
            owner = next(
                (u for u in ctx.store["users"] if str(u.get("mail", "")).lower() == address.lower()), None
            )
            if not owner:
                values.append(
                    {
                        "scheduleId": address,
                        "error": {"responseCode": "5000", "message": "The recipient was not found."},
                    }
                )
                continue
            rows = [
                r
                for r in ctx.store["calendar_events"].where(_owner=owner["id"])
                if r["type"] != "seriesMaster" and not r["isCancelled"] and h.in_range(r, low, high)
            ]
            cursor, slots = low, []
            while cursor < high:
                slots.append(
                    "2"
                    if any(h.in_range(r, cursor, cursor + timedelta(minutes=interval)) for r in rows)
                    else "0"
                )
                cursor += timedelta(minutes=interval)
            values.append(
                {
                    "scheduleId": address,
                    "availabilityView": "".join(slots),
                    "scheduleItems": [
                        {
                            "status": r["showAs"],
                            "subject": r["subject"]
                            if caller.app_only or caller.user_id == owner["id"]
                            else "",
                            "location": r["location"]["displayName"]
                            if caller.app_only or caller.user_id == owner["id"]
                            else "",
                            "isPrivate": r["sensitivity"] == "private",
                            "start": r["start"],
                            "end": r["end"],
                        }
                        for r in rows
                    ],
                    "workingHours": (ctx.store["mailbox_settings"].get(owner["id"]) or {}).get(
                        "workingHours", {}
                    ),
                }
            )
        return {
            "@odata.context": c.context_url(request, "Collection(microsoft.graph.scheduleInformation)"),
            "value": values,
        }

    async def meeting_times(request: Request, identifier: str = "me") -> dict[str, Any]:
        s.mailbox(request, ctx, identifier)
        payload = await request.json()
        slots = payload.get("timeConstraint", {}).get("timeSlots", [])
        start = (
            slots[0]["start"]
            if slots
            else {"dateTime": isoformat_z(ctx.clock.now() + timedelta(days=1)), "timeZone": "UTC"}
        )
        duration = payload.get("meetingDuration", "PT30M")
        import re

        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration)
        if not match:
            raise c.graph_error(400, "ErrorInvalidParameter", "Invalid meetingDuration.")
        minutes = int(match[1] or 0) * 60 + int(match[2] or 0)
        finish = h.parse(start["dateTime"], start["timeZone"]) + timedelta(minutes=minutes)
        return {
            "@odata.context": c.context_url(request, "microsoft.graph.meetingTimeSuggestionsResult"),
            "emptySuggestionsReason": "",
            "meetingTimeSuggestions": [
                {
                    "confidence": 100,
                    "organizerAvailability": "free",
                    "attendeeAvailability": [
                        {"attendee": a, "availability": "free"} for a in payload.get("attendees", [])
                    ],
                    "locations": [],
                    "meetingTimeSlot": {
                        "start": start,
                        "end": h.datetime_zone(isoformat_z(finish), start["timeZone"]),
                    },
                    "suggestionReason": "Suggested time within the requested interval.",
                }
            ],
        }

    for base in ("/me", "/users/{identifier}"):
        for suffix in (
            "calendars",
            "calendar",
            "calendarGroups",
            "calendars/{cid}",
            "calendarGroups/{gid}/calendars",
        ):
            router.add_api_route(f"{base}/{suffix}", calendars, methods=["GET"])
        for suffix in (
            "events",
            "calendar/events",
            "calendars/{cid}/events",
            "calendarView",
            "calendar/calendarView",
            "calendars/{cid}/calendarView",
            "events/delta",
            "calendarView/delta",
        ):
            router.add_api_route(f"{base}/{suffix}", events, methods=["GET"])
        for prefix in ("events", "calendar/events", "calendars/{cid}/events"):
            router.add_api_route(f"{base}/{prefix}", create, methods=["POST"])
            for suffix in ("{eid}", "{eid}/instances"):
                router.add_api_route(f"{base}/{prefix}/{suffix}", get_event, methods=["GET"])
            router.add_api_route(f"{base}/{prefix}/{{eid}}", patch, methods=["PATCH"])
            router.add_api_route(f"{base}/{prefix}/{{eid}}", delete, methods=["DELETE"])
            for suffix in ("accept", "decline", "tentativelyAccept", "cancel", "forward"):
                router.add_api_route(f"{base}/{prefix}/{{eid}}/{suffix}", action, methods=["POST"])
        router.add_api_route(f"{base}/calendar/getSchedule", schedule, methods=["POST"])
        router.add_api_route(f"{base}/findMeetingTimes", meeting_times, methods=["POST"])
    for version in c.GRAPH_VERSIONS:
        app.include_router(router, prefix=f"/{version}")
