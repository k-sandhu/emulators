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
from emulators.providers.microsoft import outlook_attachments as attachments_api
from emulators.providers.microsoft import outlook_payloads as payloads
from emulators.providers.microsoft.calendar_helpers import seed  # noqa: F401


def register(app: FastAPI, ctx: ProviderContext) -> None:
    router = APIRouter()

    async def calendars(
        request: Request, identifier: str = "me", cid: str | None = None, gid: str | None = None
    ) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        record = h.get_calendar(ctx, owner, cid)
        if request.method == "POST":
            body = await c.json_body(request)
            if not isinstance(body.get("name"), str) or not body["name"].strip():
                raise c.graph_error(400, "ErrorInvalidParameter", "name is required.")
            record = {
                **h.calendar(ctx, owner),
                "id": s.outlook_id(ctx),
                "name": body["name"],
                "color": body.get("color", "auto"),
                "isDefaultCalendar": False,
                "isRemovable": True,
                "changeKey": ctx.ids.urlsafe(24),
            }
            ctx.store["calendars"].put(record)
            return JSONResponse(s.single(request, record, "calendars/$entity"), status_code=201)
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
            return s.collection(request, ctx.store["calendars"].where(_owner=owner["id"]), "calendars")
        return s.single(request, record, "calendars/$entity")

    async def events(request: Request, identifier: str = "me", cid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        calendar = h.get_calendar(ctx, owner, cid)
        rows = ctx.store["calendar_events"].where(_owner=owner["id"], _calendar=calendar["id"])
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
        if request.url.path.endswith("/$count"):
            return c.count_response(request, rows)
        if request.url.path.endswith("/delta"):
            resource = f"users/{owner['id']}/calendars/{cid}/events" if cid else f"users/{owner['id']}/events"
            result = s.delta(request, ctx, rows, resource, "events")
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
        if cid and record["_calendar"] != h.get_calendar(ctx, owner, cid)["id"]:
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
        result = s.single(request, h.shape(request, record), "events/$entity")
        if "attachments" in request.query_params.get("$expand", ""):
            result["attachments"] = [
                attachments_api.wire(ctx, item) for item in record.get("_attachments", [])
            ]
        return result

    async def create(request: Request, identifier: str = "me", cid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        calendar = h.get_calendar(ctx, owner, cid)
        payload = await c.json_body(request)
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
        record["_calendar"] = calendar["id"]
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
                        "_calendar": h.calendar_id(ctx, target["id"]),
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
        if cid and record["_calendar"] != h.get_calendar(ctx, owner, cid)["id"]:
            raise c.not_found(eid)
        payload = await c.json_body(request)
        merged = {**record, **payload}
        # Validate and normalize dates while preserving the original event identity.
        validated = h.event(ctx, owner["id"], eid, merged, record["organizer"])
        if record["type"] == "seriesMaster" and record.get("_runtime_series"):
            for instance in ctx.store["calendar_events"].where(_owner=owner["id"], seriesMasterId=eid):
                ctx.store["calendar_events"].delete(instance["id"])
        for key in payload:
            if (
                key in validated
                and key not in {"id", "organizer", "isOrganizer", "createdDateTime"}
                and not key.startswith(("_", "@"))
            ):
                record[key] = validated[key]
        if "isOnlineMeeting" in payload:
            for key in ("onlineMeeting", "onlineMeetingProvider", "body"):
                record[key] = validated[key]
        record.update({"lastModifiedDateTime": s.now(ctx), "changeKey": ctx.ids.urlsafe(24)})
        if "body" in payload:
            record["bodyPreview"] = validated["bodyPreview"]
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
        if cid and record["_calendar"] != h.get_calendar(ctx, owner, cid)["id"]:
            raise c.not_found(eid)
        rows = [record, *ctx.store["calendar_events"].where(_owner=owner["id"], seriesMasterId=eid)]
        for row in rows:
            ctx.store["calendar_events"].delete(row["id"])
            await h.changed(ctx, row, "deleted")
        return Response(status_code=204)

    async def action(request: Request, eid: str, identifier: str = "me", cid: str | None = None) -> Response:
        owner = s.mailbox(request, ctx, identifier)
        record = h.get_event(ctx, owner["id"], eid)
        if cid and record["_calendar"] != h.get_calendar(ctx, owner, cid)["id"]:
            raise c.not_found(eid)
        name = request.url.path.rsplit("/", 1)[-1]
        payload = await c.json_body(request, optional=True)
        if name == "snoozeReminder":
            value = payloads.datetime_zone(payload.get("newReminderTime"))
            instant = h.parse(value["dateTime"], value["timeZone"])
            record["_reminder_time"] = isoformat_z(instant)
            await h.changed(ctx, record, "updated")
            return Response(status_code=204)
        if name == "cancel":
            if not record["isOrganizer"]:
                raise c.graph_error(400, "ErrorInvalidOperation", "Only the organizer can cancel a meeting.")
            record["isCancelled"] = True
        elif name == "forward":
            forwarded = copy.deepcopy(record)
            forwarded["attendees"] = [
                {**r, "type": "required"} for r in payloads.recipients(payload.get("toRecipients", []))
            ]
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
        body = await c.json_body(request)
        start, end = (
            payloads.datetime_zone(body.get("startTime")),
            payloads.datetime_zone(body.get("endTime")),
        )
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
        addresses = body.get("schedules")
        if (
            not isinstance(addresses, list)
            or not 1 <= len(addresses) <= 20
            or not all(isinstance(address, str) for address in addresses)
        ):
            raise c.invalid_json()
        values = []
        for address in addresses:
            owner = next(
                (u for u in ctx.store["users"] if str(u.get("mail", "")).lower() == address.lower()), None
            )
            if not owner:
                values.append(
                    {
                        "scheduleId": address,
                        "error": {
                            "responseCode": "ErrorMailRecipientNotFound",
                            "message": "The recipient was not found.",
                        },
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
                # Graph folds workingElsewhere into free for Outlook compatibility.
                codes = {"free": 0, "tentative": 1, "busy": 2, "oof": 3, "workingElsewhere": 0, "unknown": 0}
                slots.append(
                    str(
                        max(
                            (
                                codes.get(r["showAs"], 0)
                                for r in rows
                                if h.in_range(r, cursor, cursor + timedelta(minutes=interval))
                            ),
                            default=0,
                        )
                    )
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
        payload = await c.json_body(request)
        slots = c.json_object(payload.get("timeConstraint", {})).get("timeSlots", [])
        if not isinstance(slots, list):
            raise c.invalid_json()
        for slot in slots:
            for key in ("start", "end"):
                value = payloads.datetime_zone(c.json_object(slot).get(key))
                h.parse(value["dateTime"], value["timeZone"])
        start = (
            slots[0]["start"]
            if slots
            else {"dateTime": isoformat_z(ctx.clock.now() + timedelta(days=1)), "timeZone": "UTC"}
        )
        duration = payload.get("meetingDuration", "PT30M")
        import re

        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration) if isinstance(duration, str) else None
        if not match:
            raise c.graph_error(400, "ErrorInvalidParameter", "Invalid meetingDuration.")
        minutes = int(match[1] or 0) * 60 + int(match[2] or 0)
        if minutes <= 0:
            raise c.graph_error(400, "ErrorInvalidParameter", "Invalid meetingDuration.")
        finish = h.parse(start["dateTime"], start["timeZone"]) + timedelta(minutes=minutes)
        normalized_start = h.datetime_zone(
            isoformat_z(h.parse(start["dateTime"], start["timeZone"])), start["timeZone"]
        )
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
                        "start": normalized_start,
                        "end": h.datetime_zone(isoformat_z(finish), start["timeZone"]),
                    },
                    "suggestionReason": "Suggested time within the requested interval.",
                }
            ],
        }

    async def attachments(
        request: Request, eid: str, identifier: str = "me", cid: str | None = None, aid: str | None = None
    ) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        record = h.get_event(ctx, owner["id"], eid)
        if cid and record["_calendar"] != h.get_calendar(ctx, owner, cid)["id"]:
            raise c.not_found(eid)
        items = record.setdefault("_attachments", [])
        if request.method == "POST":
            item = attachments_api.create(ctx, await c.json_body(request))
            items.append(item)
            record["hasAttachments"] = any(not item["isInline"] for item in items)
            attachments_api.touch(ctx, record)
            await h.changed(ctx, record, "updated")
            return attachments_api.created(request, ctx, item)
        if request.method == "DELETE":
            item = next((item for item in items if item["id"] == aid), None)
            if item is None:
                raise c.not_found(str(aid))
            items.remove(item)
            record["hasAttachments"] = any(not item["isInline"] for item in items)
            attachments_api.touch(ctx, record)
            await h.changed(ctx, record, "updated")
            return Response(status_code=204)
        return attachments_api.read(request, ctx, [attachments_api.wire(ctx, item) for item in items], aid)

    for base in ("/me", "/users/{identifier}"):
        for suffix in (
            "calendars",
            "calendar",
            "calendarGroups",
            "calendars/{cid}",
            "calendarGroups/{gid}/calendars",
        ):
            router.add_api_route(f"{base}/{suffix}", calendars, methods=["GET"])
        router.add_api_route(f"{base}/calendars", calendars, methods=["POST"])
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
            router.add_api_route(f"{base}/{prefix}/$count", events, methods=["GET"])
            for suffix in ("{eid}", "{eid}/instances"):
                router.add_api_route(f"{base}/{prefix}/{suffix}", get_event, methods=["GET"])
            router.add_api_route(f"{base}/{prefix}/{{eid}}", patch, methods=["PATCH"])
            router.add_api_route(f"{base}/{prefix}/{{eid}}", delete, methods=["DELETE"])
            for suffix in ("accept", "decline", "tentativelyAccept", "cancel", "forward", "snoozeReminder"):
                router.add_api_route(f"{base}/{prefix}/{{eid}}/{suffix}", action, methods=["POST"])
            router.add_api_route(f"{base}/{prefix}/{{eid}}/attachments", attachments, methods=["GET", "POST"])
            router.add_api_route(
                f"{base}/{prefix}/{{eid}}/attachments/{{aid}}", attachments, methods=["GET", "DELETE"]
            )
            router.add_api_route(
                f"{base}/{prefix}/{{eid}}/attachments/{{aid}}/$value", attachments, methods=["GET"]
            )
        router.add_api_route(f"{base}/calendar/getSchedule", schedule, methods=["POST"])
        router.add_api_route(f"{base}/findMeetingTimes", meeting_times, methods=["POST"])
    for version in c.GRAPH_VERSIONS:
        app.include_router(router, prefix=f"/{version}")
