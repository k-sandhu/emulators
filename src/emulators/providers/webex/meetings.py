"""Meeting schedules, instances, invitees and attendance."""

from __future__ import annotations

from typing import Any

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
    public,
    require_scope,
    required,
    stamp,
    subject,
)
from .hooks import emit
from .seed import meeting_shape


def wire(request: Request, ctx: ProviderContext, row: dict[str, Any]) -> dict[str, Any]:
    result = public(row)
    result["webLink"] = url_for_host(
        request, ctx, SITE, f"/halcyonadvisory/j.php?MTID=m{row['id'].split('_I_')[0]}"
    )
    result["telephony"] = {
        **row["telephony"],
        "links": [
            {"rel": "globalCallinNumbers", "href": url_for_host(request, ctx, SITE, "/globalcallin.php")}
        ],
    }
    return result


def check_scope(grant: Grant, area: str, *, write: bool = False) -> None:
    require_scope(
        grant,
        f"meeting:{area}_{'write' if write else 'read'}",
        admin_scope(area, write),
        *([] if write else ["spark-compliance:meetings_read"]),
    )


def invitees(ctx: ProviderContext, meeting: dict[str, Any], payload: list[dict[str, Any]]) -> None:
    if not isinstance(payload, list) or not all(isinstance(i, dict) for i in payload):
        fail(400, "invitees must be an array of objects.")
    prepared = []
    for item in payload:
        required(item, "email")
        if "coHost" in item and not isinstance(item["coHost"], bool):
            fail(400, "coHost must be a boolean.")
        p = person(ctx, item["email"])
        if not p:
            fail(400, "Invitees must identify people in the synthetic world.")
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


def validate_schedule(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
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
    for key in ("agenda", "password", "timezone"):
        if key in fields and not isinstance(fields[key], str):
            fail(400, f"{key} must be a string.")
    start, end = instant(candidate["start"], "start"), instant(candidate["end"], "end")
    if end <= start:
        fail(400, "end must be after start.")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(candidate.get("timezone", "UTC"))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        fail(400, "Invalid timezone.")
    if "enabledAutoRecordMeeting" in fields and not isinstance(fields["enabledAutoRecordMeeting"], bool):
        fail(400, "enabledAutoRecordMeeting must be a boolean.")
    fields.update(start=stamp(start), end=stamp(end))
    return fields


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/v1/meetings")
    async def listing(request: Request) -> JSONResponse:
        grant = authenticate(request, ctx)
        check_scope(grant, "schedules")
        q = request.query_params
        kind = q.get("meetingType", "scheduledMeeting")
        if kind not in ("meetingSeries", "scheduledMeeting", "meeting"):
            fail(400, "Invalid meetingType.")
        bounds = date_range(request, ctx)
        rows = [
            m
            for m in ctx.store.collection("meetings")
            if m["meetingType"] == kind
            and meeting_access(ctx, grant, m, "schedules")
            and in_range(m["start"], bounds)
        ]
        if q.get("state"):
            if q["state"] not in ("ready", "ended", "inProgress", "expired", "missed"):
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
        rows.sort(key=lambda m: (m["start"], m["id"]))
        return page(request, [wire(request, ctx, m) for m in rows], default=10, maximum=100)

    @app.post("/v1/meetings")
    async def create(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        check_scope(grant, "schedules", write=True)
        if not is_admin(ctx, grant, "meeting:admin_schedule_write"):
            require_scope(grant, "meeting:schedules_write")
        payload = await body(request)
        fields = validate_schedule(payload)
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
        row = meeting_shape(
            ctx,
            key,
            host,
            fields.pop("title"),
            fields.pop("start"),
            fields.pop("end"),
            fields.pop("timezone", host.get("timezone", "UTC")),
            _scheduledId=key,
            _seriesId=key,
        )
        invites = fields.pop("invitees", [])
        row.update(fields)
        invitees(ctx, row, invites)
        ctx.store.collection("meetings").put(row)
        await emit(ctx, "meetings", "created", row, actor=host["id"])
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
        if row["meetingType"] == "meeting" or row["state"] == "ended":
            fail(400, "An ended meeting cannot be updated.")
        fields = validate_schedule(await body(request), row)
        if "invitees" in fields:
            invitees(ctx, row, fields.pop("invitees"))
        row.update(fields)
        await emit(ctx, "meetings", "updated", row, actor=row["hostUserId"])
        return wire(request, ctx, row)

    @app.delete("/v1/meetings/{meeting_id}")
    async def delete(meeting_id: str, request: Request) -> Response:
        grant = authenticate(request, ctx)
        check_scope(grant, "schedules", write=True)
        row = get_meeting(ctx, meeting_id, grant, write=True)
        ctx.store.collection("meetings").delete(meeting_id)
        await emit(ctx, "meetings", "deleted", row, actor=row["hostUserId"])
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
        get_meeting(ctx, meeting_id, grant, "participants")
        return page(request, ctx.store.collection("participants").where(meetingId=meeting_id), maximum=100)
