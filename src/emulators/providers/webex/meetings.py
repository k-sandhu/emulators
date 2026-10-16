"""Meeting schedules, instances, invitees and attendance."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext, url_for_host

from .common import (
    SITE,
    admin_scope,
    authenticate,
    body,
    date_range,
    fail,
    get_meeting,
    in_range,
    instant,
    is_admin,
    meeting_access,
    opaque,
    page,
    person,
    require_scope,
    required,
    stamp,
    strings,
    subject,
    time_wire,
)
from .hooks import emit
from .seed import meeting_shape


def wire(request: Request, ctx: ProviderContext, row: dict[str, Any]) -> dict[str, Any]:
    result = time_wire(request, row)
    result["webLink"] = url_for_host(
        request, ctx, SITE, f"/halcyonadvisory/j.php?MTID=m{row.get('_seriesId', row['id'])}"
    )
    result["telephony"] = {
        **row["telephony"],
        "links": [
            {"rel": "globalCallinNumbers", "href": url_for_host(request, ctx, SITE, "/globalcallin.php")}
        ],
    }
    if row["meetingType"] == "meeting" and row["state"] == "ended":
        for key in (
            "meetingNumber",
            "password",
            "phoneAndVideoSystemPassword",
            "sipAddress",
            "dialInIpAddress",
            "enabledAutoRecordMeeting",
            "telephony",
            "recurrence",
        ):
            result.pop(key, None)
    return result


def check_scope(grant: Grant, area: str, *, write: bool = False) -> None:
    require_scope(
        grant,
        f"meeting:{area}_{'write' if write else 'read'}",
        admin_scope(area, write),
        *([] if write else ["spark-compliance:meetings_read"]),
    )


def invitees(ctx: ProviderContext, meeting: dict[str, Any], payload: list[dict[str, Any]]) -> None:
    if not isinstance(payload, list) or len(payload) > 1000 or not all(isinstance(i, dict) for i in payload):
        fail(400, "invitees must be an array of objects.")
    prepared = []
    for item in payload:
        required(item, "email")
        strings(item, "displayName", maximum=128)
        if "coHost" in item and not isinstance(item["coHost"], bool):
            fail(400, "coHost must be a boolean.")
        p = person(ctx, item["email"])
        if not p:
            fail(400, "Invitees must identify people in the synthetic world.")
        if not any(existing[0]["id"] == p["id"] for existing in prepared):
            prepared.append((p, item))
    key = meeting.get("_scheduledId", meeting["id"])
    for row in ctx.store.collection("invitees").where(meetingId=key):
        ctx.store.collection("invitees").delete(row["id"])
    meeting["_people"] = [meeting["hostUserId"]]
    for p, item in prepared:
        meeting["_people"].append(p["id"])
        ctx.store.collection("invitees").put(
            {
                "id": opaque("MEETING_INVITEE", f"{key}/{p['id']}"),
                "meetingId": key,
                "email": p["emails"][0],
                "displayName": item.get("displayName", p["displayName"]),
                "coHost": bool(item.get("coHost", False)),
            }
        )


def validate_schedule(
    ctx: ProviderContext,
    payload: dict[str, Any],
    existing: dict[str, Any] | None = None,
    *,
    patch: bool = False,
) -> dict[str, Any]:
    fields = {
        k: payload[k]
        for k in (
            "title",
            "agenda",
            "start",
            "end",
            "timezone",
            "password",
            "enabledAutoRecordMeeting",
            "invitees",
        )
        if k in payload
    }
    candidate = {**(existing or {}), **fields}
    required(candidate, "title", "start", "end")
    strings(fields, "title", maximum=128)
    if fields.get("agenda") is not None:
        strings(fields, "agenda", maximum=1300)
    for key in ("agenda", "password", "timezone"):
        if (
            key in fields
            and not isinstance(fields[key], str)
            and not (key == "agenda" and patch and fields[key] is None)
        ):
            fail(400, f"{key} must be a string.")
    start, end = instant(candidate["start"], "start"), instant(candidate["end"], "end")
    if existing is None or "start" in fields or "end" in fields:
        now = ctx.clock.now()
        if start < now:
            fail(400, "start cannot be before the current time.")
        if now.year <= 9989:
            try:
                furthest = now.replace(year=now.year + 10)
            except ValueError:
                furthest = now.replace(year=now.year + 10, day=28)
            if start >= furthest:
                fail(400, "start must be less than ten years in the future.")
        start = start.replace(second=0, microsecond=0)
        if start <= now:
            start += timedelta(minutes=1)
        end = end.replace(second=0, microsecond=0)
        if not timedelta(minutes=10) <= end - start <= timedelta(hours=23, minutes=59):
            fail(400, "Meeting duration must be between 10 minutes and 23 hours 59 minutes.")

    try:
        ZoneInfo(candidate.get("timezone", "UTC"))
    except (TypeError, ValueError, OSError, ZoneInfoNotFoundError):
        fail(400, "Invalid timezone.")
    if "enabledAutoRecordMeeting" in fields and not isinstance(fields["enabledAutoRecordMeeting"], bool):
        fail(400, "enabledAutoRecordMeeting must be a boolean.")
    if existing is None or "start" in fields or "end" in fields:
        fields.update(start=stamp(start), end=stamp(end))
    return fields


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/v1/meetings")
    async def listing(request: Request) -> JSONResponse:
        grant = authenticate(request, ctx)
        check_scope(grant, "schedules")
        q = request.query_params
        kind = q.get("meetingType", None if q.get("meetingSeriesId") else "meetingSeries")
        if kind not in (None, "meetingSeries", "scheduledMeeting", "meeting"):
            fail(400, "Invalid meetingType.")
        bounds = date_range(request, ctx, defaults=not q.get("meetingSeriesId"), future=kind != "meeting")
        rows = [
            m
            for m in ctx.store.collection("meetings")
            if (m["meetingType"] == kind if kind else m["meetingType"] != "meetingSeries")
            and meeting_access(ctx, grant, m, "schedules")
            and (
                in_range(m["start"], bounds)
                or (
                    m["meetingType"] == "meetingSeries"
                    and any(
                        child.get("_seriesId") == m["id"]
                        and child["meetingType"] == "scheduledMeeting"
                        and in_range(child["start"], bounds)
                        for child in ctx.store.collection("meetings")
                    )
                )
            )
        ]
        if q.get("state"):
            if q["state"] not in (
                "active",
                "scheduled",
                "ready",
                "lobby",
                "ended",
                "inProgress",
                "expired",
                "missed",
            ):
                fail(400, "Invalid state.")
            rows = [m for m in rows if m["state"] == q["state"]]
        if q.get("hostEmail"):
            user = person(ctx, grant.user_id)
            if (
                not user or q["hostEmail"].lower() not in [e.lower() for e in user["emails"]]
            ) and not is_admin(ctx, grant, "meeting:admin_schedule_read"):
                fail(403, "Filtering another host requires meeting:admin_schedule_read.")
            rows = [m for m in rows if m["hostEmail"].lower() == q["hostEmail"].lower()]
        if q.get("meetingSeriesId"):
            rows = [m for m in rows if m.get("_seriesId") == q["meetingSeriesId"]]
        if q.get("siteUrl"):
            rows = [m for m in rows if m["siteUrl"] == q["siteUrl"]]
        rows.sort(key=lambda m: (m["start"], m["id"]))
        return page(request, [wire(request, ctx, m) for m in rows], default=10, maximum=100)

    @app.post("/v1/meetings")
    async def create(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        check_scope(grant, "schedules", write=True)
        if not is_admin(ctx, grant, "meeting:admin_schedule_write"):
            require_scope(grant, "meeting:schedules_write")
        payload = await body(request)
        strings(payload, "hostEmail")
        fields = validate_schedule(ctx, payload)
        if payload.get("hostEmail"):
            host = person(ctx, payload["hostEmail"])
            if not host or host.get("_external") or host["type"] == "bot":
                fail(400, "Unknown meeting host.")
            if (grant.app_only or subject(ctx, grant)["id"] != host["id"]) and not is_admin(
                ctx, grant, "meeting:admin_schedule_write"
            ):
                fail(403, "Scheduling for another host requires an administrator scope.")
        else:
            host = subject(ctx, grant)
        if host["type"] == "bot" or (
            grant.app_only and not is_admin(ctx, grant, "meeting:admin_schedule_write")
        ):
            fail(403, "This token cannot schedule meetings.")
        key = ctx.ids.hex(32)
        scheduled_key = f"{key}_{instant(fields['start'], 'start').strftime('%Y%m%dT%H%M%SZ')}"
        row = meeting_shape(
            ctx,
            key,
            host,
            fields.pop("title"),
            fields.pop("start"),
            fields.pop("end"),
            fields.pop("timezone", host.get("timezone", "UTC")),
            meetingType="meetingSeries",
            state="active",
            _scheduledId=scheduled_key,
            _seriesId=key,
        )
        invites = fields.pop("invitees", [])
        row.update(fields)
        invitees(ctx, row, invites)
        ctx.store.collection("meetings").put(row)
        ctx.store.collection("meetings").put(
            {
                **row,
                "id": scheduled_key,
                "meetingType": "scheduledMeeting",
                "state": "ready",
                "meetingSeriesId": key,
            }
        )
        await emit(ctx, "meetings", "created", row, actor=grant.user_id or host["id"])
        return wire(request, ctx, row)

    @app.get("/v1/meetings/{meeting_id}")
    async def detail(meeting_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        check_scope(grant, "schedules")
        return wire(request, ctx, get_meeting(ctx, meeting_id, grant))

    @app.put("/v1/meetings/{meeting_id}")
    @app.patch("/v1/meetings/{meeting_id}")
    async def update(meeting_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        check_scope(grant, "schedules", write=True)
        row = get_meeting(ctx, meeting_id, grant, write=True)
        if row["meetingType"] == "meeting":
            fail(400, "An ended meeting cannot be updated.")
        payload = await body(request)
        fields = validate_schedule(ctx, payload, row, patch=request.method == "PATCH")
        if row.get("recurrence") and any(k in fields for k in ("start", "end")):
            fail(400, "Rescheduling a recurring series is not supported by this emulator.")
        if "invitees" in fields:
            invitees(ctx, row, fields.pop("invitees"))
        row.update(fields)
        if row.get("agenda") is None:
            row.pop("agenda", None)
        if row["meetingType"] == "meetingSeries":
            for child in ctx.store.collection("meetings"):
                if child.get("_seriesId") == row["id"] and child["meetingType"] == "scheduledMeeting":
                    child.update(fields)
                    if child.get("agenda") is None:
                        child.pop("agenda", None)
                    if "invitees" in payload:
                        invitees(ctx, child, payload["invitees"])
        await emit(ctx, "meetings", "updated", row, actor=grant.user_id or row["hostUserId"])
        return wire(request, ctx, row)

    @app.delete("/v1/meetings/{meeting_id}")
    async def delete(meeting_id: str, request: Request) -> Response:
        grant = authenticate(request, ctx)
        check_scope(grant, "schedules", write=True)
        row = get_meeting(ctx, meeting_id, grant, write=True)
        if row["meetingType"] == "meeting":
            fail(400, "Meeting instances cannot be deleted.")
        schedules = {row["id"]}
        if row["meetingType"] == "meetingSeries":
            schedules.update(
                m["id"]
                for m in ctx.store.collection("meetings")
                if m.get("_seriesId") == row["id"] and m["meetingType"] == "scheduledMeeting"
            )
        for key in schedules:
            ctx.store.collection("meetings").delete(key)
            for invite in ctx.store.collection("invitees").where(meetingId=key):
                ctx.store.collection("invitees").delete(invite["id"])
        await emit(ctx, "meetings", "deleted", row, actor=grant.user_id or row["hostUserId"])
        return Response(status_code=204)

    @app.get("/v1/meetingInvitees")
    async def meeting_invitees(request: Request) -> JSONResponse:
        grant = authenticate(request, ctx)
        check_scope(grant, "schedules")
        meeting_id = request.query_params.get("meetingId")
        if not meeting_id:
            fail(400, "meetingId is required.")
        row = get_meeting(ctx, meeting_id, grant)
        return page(
            request,
            ctx.store.collection("invitees").where(meetingId=row.get("_scheduledId", row["id"])),
            maximum=100,
        )

    @app.get("/v1/meetingParticipants")
    async def participants(request: Request) -> JSONResponse:
        grant = authenticate(request, ctx)
        check_scope(grant, "participants")
        meeting_id = request.query_params.get("meetingId")
        if not meeting_id:
            fail(400, "meetingId is required.")
        row = get_meeting(ctx, meeting_id, grant, "participants")
        if row["meetingType"] != "meeting":
            instances = [
                m
                for m in ctx.store.collection("meetings")
                if m["meetingType"] == "meeting" and meeting_id in (m.get("_scheduledId"), m.get("_seriesId"))
            ]
            row = max(instances, key=lambda m: m["start"]) if instances else row
        return page(
            request,
            [time_wire(request, p) for p in ctx.store.collection("participants").where(meetingId=row["id"])],
            default=10,
            maximum=100,
        )
