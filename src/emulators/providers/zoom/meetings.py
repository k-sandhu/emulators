"""Scheduled meetings: list/create/get/update/delete, status, invitation, registrants."""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request, Response

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.providers.zoom import lookup, shapes, simulate
from emulators.providers.zoom import webhooks as hooks
from emulators.providers.zoom.common import (
    E_MEETING_MISSING,
    LiveCollection,
    Need,
    Principal,
    authenticate,
    invalid_field,
    json_body,
    no_permission,
    paginate,
    query_bool,
    respond,
    runtime_b64_22,
    runtime_uuid,
    user_missing,
    zoom_error,
)
from emulators.providers.zoom.timeutil import parse_instant, valid_timezone
from emulators.world.model import Person

LIST_MEETINGS = Need("meeting:read", "meeting:read:list_meetings")
LIST_UPCOMING = Need("meeting:read", "meeting:read:list_upcoming_meetings")
READ_MEETING = Need("meeting:read", "meeting:read:meeting")
CREATE_MEETING = Need("meeting:write", "meeting:write:meeting")
UPDATE_MEETING = Need("meeting:write", "meeting:update:meeting")
DELETE_MEETING = Need("meeting:write", "meeting:delete:meeting")
MEETING_STATUS = Need("meeting:write", "meeting:update:status")
INVITATION = Need("meeting:read", "meeting:read:invitation")
LIST_REGISTRANTS = Need("meeting:read", "meeting:read:list_registrants")
ADD_REGISTRANT = Need("meeting:write", "meeting:write:registrant")
UPDATE_REGISTRANT = Need("meeting:write", "meeting:update:registrant_status")
DELETE_REGISTRANT = Need("meeting:write", "meeting:delete:registrant")
MEETING_TYPES = (1, 2, 3, 8)
LIST_TYPES = ("scheduled", "live", "upcoming", "upcoming_meetings", "previous_meetings")
AUDIO_OPTIONS = ("both", "telephony", "voip", "thirdParty")
PASSWORD_RE = re.compile(r"^[A-Za-z0-9@\-_*]{1,10}$")
EDITABLE = (
    "topic",
    "agenda",
    "duration",
    "timezone",
    "start_time",
    "recurrence",
    "settings",
    "password",
    "type",
)


def operator_of(principal: Principal) -> Person | None:
    return principal.person


def validate_settings(data: dict[str, Any]) -> dict[str, Any]:
    settings = data.get("settings")
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise invalid_field("settings")
    if settings.get("auto_recording") not in (None, "local", "cloud", "none"):
        raise invalid_field("settings.auto_recording")
    if settings.get("approval_type") not in (None, 0, 1, 2):
        raise invalid_field("settings.approval_type")
    if settings.get("audio") not in (None, *AUDIO_OPTIONS):
        raise invalid_field("settings.audio")
    return settings


def merge_settings(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def check_common(data: dict[str, Any], *, creating: bool) -> None:
    if "type" in data and data["type"] not in MEETING_TYPES:
        raise invalid_field("type")
    if "topic" in data and (not isinstance(data["topic"], str) or len(data["topic"]) > 200):
        raise invalid_field("topic")
    if "agenda" in data and (not isinstance(data["agenda"], str) or len(data["agenda"]) > 2000):
        raise invalid_field("agenda")
    if "duration" in data and (not isinstance(data["duration"], int) or data["duration"] < 0):
        raise invalid_field("duration")
    if "timezone" in data and not valid_timezone(str(data["timezone"])):
        raise invalid_field("timezone")
    if (
        "password" in data
        and data["password"]
        and not (
            re.fullmatch(r"[\x21-\x7e]{1,10}", data["password"])
            if creating
            else PASSWORD_RE.fullmatch(data["password"])
        )
    ):
        raise invalid_field("password", "Passcode may only contain letters, digits and @ - _ *.")
    if "recurrence" in data and not isinstance(data["recurrence"], dict):
        raise invalid_field("recurrence")


def parse_start(data: dict[str, Any], tz_name: str) -> Any:
    try:
        return parse_instant(data["start_time"], tz_name)
    except (ValueError, TypeError, OverflowError):
        raise invalid_field("start_time") from None


def regenerate_occurrences(meeting: dict[str, Any], uuid_factory: Any) -> None:
    """Rebuild a recurring meeting's future occurrences, keeping those that already happened."""
    happened = [o for o in meeting.get("occurrences", []) if o.get("_happened")]
    start = parse_instant(meeting["start_time"], meeting["timezone"])
    fresh = shapes.expand_occurrences(start, meeting["timezone"], meeting["recurrence"], meeting["duration"])
    seen = {o["occurrence_id"] for o in happened}
    for occ in fresh:
        if occ["occurrence_id"] not in seen:
            occ["_uuid"] = uuid_factory()
            happened.append(occ)
    happened.sort(key=lambda o: o["start_time"])
    meeting["occurrences"] = happened


def upcoming_occurrences(meeting: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    return [
        o
        for o in meeting.get("occurrences", [])
        if not o.get("_happened")
        and o["status"] != "deleted"
        and (now is None or parse_instant(o["start_time"]) >= now)
    ]


def listing_start(meeting: dict[str, Any], list_type: str, now: datetime) -> str | None:
    occs = meeting.get("occurrences", [])
    if list_type in ("upcoming", "upcoming_meetings"):
        up = upcoming_occurrences(meeting, now)
        return up[0]["start_time"] if up else meeting.get("start_time")
    if list_type == "previous_meetings":
        past = [
            o
            for o in occs
            if o["status"] != "deleted"
            and parse_instant(o["start_time"]) + timedelta(minutes=o["duration"]) < now
        ]
        return past[-1]["start_time"] if past else meeting.get("start_time")
    return meeting.get("start_time")


def matches_type(meeting: dict[str, Any], list_type: str, now: datetime) -> bool:
    if meeting["type"] == 1:
        return False
    if list_type == "live":
        return meeting.get("status") == "started"
    if list_type in ("upcoming", "upcoming_meetings"):
        return (
            meeting.get("status") == "started"
            or bool(upcoming_occurrences(meeting, now))
            or meeting["type"] == 3
        )
    if list_type == "previous_meetings":
        return any(
            o["status"] != "deleted"
            and parse_instant(o["start_time"]) + timedelta(minutes=o["duration"]) < now
            for o in meeting.get("occurrences", [])
        )
    return True


def register(app: FastAPI, ctx: ProviderContext) -> None:
    meetings = LiveCollection(ctx, "meetings")
    world = ctx.world

    def new_meeting_id() -> int:
        while True:
            mid = int(ctx.ids.digits(11 if int(ctx.ids.digits(2)) % 2 else 10))
            if str(mid) not in meetings:
                return mid

    def host_view(request: Request, meeting: dict[str, Any], **kwargs: Any) -> Response:
        return respond(request, ctx, shapes.meeting_detail(meeting, **kwargs))

    # -- list -----------------------------------------------------------------------------

    @app.get("/v2/users/{user_id}/meetings")
    async def list_meetings(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(LIST_MEETINGS)
        host = lookup.acting_user(ctx, principal, user_id, admin_level=admin)
        q = request.query_params
        list_type = q.get("type", "scheduled")
        if list_type not in LIST_TYPES:
            raise invalid_field("type")
        lo = hi = None
        tz_name = q.get("timezone", "UTC")
        if not valid_timezone(tz_name):
            raise invalid_field("timezone")
        try:
            if q.get("from"):
                lo = parse_instant(q["from"], tz_name)
            if q.get("to"):
                hi = parse_instant(q["to"], tz_name) + (
                    timedelta(days=1) if len(q["to"]) == 10 else timedelta()
                )
        except (ValueError, OverflowError):
            raise invalid_field("from") from None
        if lo and hi and lo >= hi:
            raise invalid_field("to")
        now = ctx.clock.now()
        rows = []
        for m in meetings:
            if m["host_id"] != host["id"] or not matches_type(m, list_type, now):
                continue
            start = listing_start(m, list_type, now)
            if (lo or hi) and start:
                moment = parse_instant(start)
                if (lo and moment < lo) or (hi and moment >= hi):
                    continue
            rows.append((start or "", m))
        rows.sort(key=lambda pair: (pair[0], pair[1]["id"]))
        page, meta = paginate(request, rows)
        body = {
            "page_size": meta["page_size"],
            "total_records": meta["total_records"],
            "next_page_token": meta["next_page_token"],
            "meetings": [shapes.meeting_listing(m, start_time=s or None) for s, m in page],
        }
        return respond(request, ctx, body)

    @app.get("/v2/users/{user_id}/upcoming_meetings")
    async def upcoming_meetings(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(LIST_UPCOMING)
        host = lookup.acting_user(ctx, principal, user_id, admin_level=admin)
        rows = []
        for m in meetings:
            if m["host_id"] != host["id"]:
                continue
            if m["type"] == 1:
                continue
            for occ in upcoming_occurrences(m, ctx.clock.now()):
                rows.append(
                    {
                        "id": m["id"],
                        "topic": m["topic"],
                        "type": m["type"],
                        "start_time": occ["start_time"],
                        "duration": occ["duration"],
                        "timezone": m["timezone"],
                        "agenda": m.get("agenda", ""),
                        "created_at": m["created_at"],
                        "join_url": m["join_url"],
                        "host_id": m["host_id"],
                    }
                )
        rows.sort(key=lambda r: (r["start_time"], r["id"]))
        return respond(request, ctx, {"total_records": len(rows), "meetings": rows})

    # -- create ---------------------------------------------------------------------------

    @app.post("/v2/users/{user_id}/meetings")
    async def create_meeting(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(CREATE_MEETING)
        host = lookup.acting_user(ctx, principal, user_id, admin_level=admin)
        data = await json_body(request)
        if data.get("schedule_for"):
            target = lookup.find_user(ctx, str(data["schedule_for"]))
            if target is None:
                raise user_missing(str(data["schedule_for"]))
            if target["id"] != host["id"] and not admin:
                raise no_permission()
            host = target
        if host["status"] != "active":
            raise zoom_error(
                400, 3161, "Your user account is not allowed meeting hosting and scheduling capabilities."
            )
        check_common(data, creating=True)
        incoming = validate_settings(data)
        kind = data.get("type", 2)
        tz_name = data.get("timezone") or host["timezone"] or "UTC"
        now = ctx.clock.now()
        if kind in (2, 8) and data.get("start_time"):
            start = parse_start(data, tz_name)
        else:
            start = now.replace(microsecond=0)
        duration = data.get("duration", 60)
        recurrence = data.get("recurrence")
        if kind == 8 and not recurrence:
            raise invalid_field(
                "recurrence", "Recurrence is required for a recurring meeting with fixed time."
            )
        mid = new_meeting_id()
        password = data.get("password") or (
            shapes.numeric_password(world, mid, ctx.ids.hex(8)) if data.get("default_password", True) else ""
        )
        enc = shapes.encrypted_password(world, mid, password)
        uuid = runtime_uuid(ctx)
        record: dict[str, Any] = {
            "id": mid,
            "uuid": uuid,
            "host_id": host["id"],
            "host_email": host["email"],
            "topic": data.get("topic") or f"{host['display_name']}'s Zoom Meeting",
            "type": kind,
            "status": "waiting",
            "timezone": tz_name,
            "agenda": data.get("agenda", ""),
            "created_at": isoformat_z(now),
            "start_url": shapes.start_url(world, mid, host["id"]),
            "join_url": shapes.join_url(mid, enc),
            "password": password,
            "h323_password": password,
            "pstn_password": password,
            "encrypted_password": enc,
            "settings": merge_settings(shapes.default_settings(), incoming),
            "pre_schedule": bool(data.get("pre_schedule", False)),
        }
        if kind != 3:
            record["start_time"] = isoformat_z(start)
            record["duration"] = duration
        if data.get("tracking_fields"):
            record["tracking_fields"] = data["tracking_fields"]
        if kind == 8:
            record["recurrence"] = recurrence
            try:
                record["occurrences"] = shapes.expand_occurrences(start, tz_name, recurrence, duration)
            except (ValueError, OverflowError):
                raise invalid_field("recurrence") from None
            if record["occurrences"]:  # the series starts at its first real occurrence
                record["start_time"] = record["occurrences"][0]["start_time"]
            for occ in record["occurrences"]:
                occ["_uuid"] = uuid if occ is record["occurrences"][0] else runtime_uuid(ctx)
        elif kind == 3:
            record["occurrences"] = []
        else:
            record["occurrences"] = [
                {
                    "occurrence_id": str(int(start.timestamp() * 1000)),
                    "start_time": record["start_time"],
                    "duration": duration,
                    "status": "available",
                    "_uuid": uuid,
                }
            ]
        meetings.put(record)
        await hooks.emit(
            ctx,
            "meeting.created",
            {
                **shapes.meeting_payload(record),
                "join_url": record["join_url"],
                "password": password,
                "settings": record["settings"],
            },
            operator=operator_of(principal),
        )
        return respond(request, ctx, shapes.meeting_detail(record), status=201)

    # -- read -----------------------------------------------------------------------------

    @app.get("/v2/meetings/{meeting_id}")
    async def get_meeting(request: Request, meeting_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(READ_MEETING)
        meeting = lookup.find_meeting(ctx, meeting_id)
        lookup.ensure_meeting_access(ctx, principal, admin, meeting)
        occurrence_id = request.query_params.get("occurrence_id")
        if occurrence_id and not any(
            o["occurrence_id"] == occurrence_id for o in meeting.get("occurrences", [])
        ):
            raise zoom_error(
                404, E_MEETING_MISSING, f"Meeting ID {meeting_id} occurrence {occurrence_id} does not exist."
            )
        return host_view(
            request,
            meeting,
            show_previous=query_bool(request, "show_previous_occurrences"),
            occurrence_id=occurrence_id,
        )

    @app.get("/v2/meetings/{meeting_id}/invitation")
    async def invitation(request: Request, meeting_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(INVITATION)
        meeting = lookup.find_meeting(ctx, meeting_id)
        lookup.ensure_meeting_access(ctx, principal, admin, meeting)
        host = ctx.store.collection("users").get(meeting["host_id"])
        text = shapes.invitation_text(meeting, host["display_name"] if host else "Zoom")
        meeting_no = str(meeting["id"])
        links = [f"sip:{meeting_no}.{meeting['password']}@zoomcrc.com"]
        return respond(request, ctx, {"invitation": text, "sip_links": links})

    # -- update ---------------------------------------------------------------------------

    @app.patch("/v2/meetings/{meeting_id}")
    async def update_meeting(request: Request, meeting_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(UPDATE_MEETING)
        meeting = copy.deepcopy(lookup.find_meeting(ctx, meeting_id))
        lookup.ensure_meeting_access(ctx, principal, admin, meeting, write=True)
        data = await json_body(request)
        check_common(data, creating=False)
        incoming = validate_settings(data)
        occurrence_id = request.query_params.get("occurrence_id")
        old: dict[str, Any] = {}
        new: dict[str, Any] = {}
        if occurrence_id:
            occ = next(
                (o for o in meeting.get("occurrences", []) if o["occurrence_id"] == occurrence_id), None
            )
            if occ is None:
                raise zoom_error(
                    404,
                    E_MEETING_MISSING,
                    f"Meeting ID {meeting_id} occurrence {occurrence_id} does not exist.",
                )
            if "start_time" in data:
                old["start_time"] = occ["start_time"]
                occ["start_time"] = isoformat_z(parse_start(data, meeting["timezone"]))
                new["start_time"] = occ["start_time"]
            if "duration" in data:
                old["duration"] = occ["duration"]
                occ["duration"] = new["duration"] = data["duration"]
            if "agenda" in data:
                old["agenda"] = occ.get("_agenda", meeting.get("agenda", ""))
                occ["_agenda"] = new["agenda"] = data["agenda"]
            if incoming:
                old["settings"] = copy.deepcopy(occ.get("_settings", meeting["settings"]))
                occ["_settings"] = merge_settings(old["settings"], incoming)
                new["settings"] = incoming
        else:
            if "type" in data:
                old["type"], new["type"] = meeting["type"], data["type"]
                meeting["type"] = data["type"]
                if meeting["type"] == 8 and not (data.get("recurrence") or meeting.get("recurrence")):
                    raise invalid_field("recurrence")
                if meeting["type"] != 8:
                    meeting.pop("recurrence", None)
                    if meeting["type"] != old["type"]:
                        meeting["occurrences"] = []
                if meeting["type"] == 3:
                    meeting.pop("start_time", None)
                    meeting.pop("duration", None)
                    meeting["occurrences"] = []
                else:
                    meeting.setdefault("start_time", isoformat_z(ctx.clock.now()))
                    meeting.setdefault("duration", 60)
            for key in ("topic", "agenda", "duration", "timezone"):
                if key in data:
                    old[key], meeting[key] = meeting.get(key), data[key]
                    new[key] = data[key]
            if "start_time" in data and meeting["type"] != 3:
                old["start_time"] = meeting.get("start_time")
                meeting["start_time"] = isoformat_z(parse_start(data, meeting["timezone"]))
                new["start_time"] = meeting["start_time"]
            if "recurrence" in data:
                if meeting["type"] != 8:
                    raise invalid_field("recurrence")
                old["recurrence"], meeting["recurrence"] = meeting.get("recurrence"), data["recurrence"]
                new["recurrence"] = data["recurrence"]
            if "password" in data:
                old["password"] = meeting["password"]
                new["password"] = data["password"]
                meeting["password"] = meeting["h323_password"] = meeting["pstn_password"] = str(
                    data["password"]
                )
                meeting["encrypted_password"] = shapes.encrypted_password(
                    world, meeting["id"], data["password"]
                )
                meeting["join_url"] = shapes.join_url(meeting["id"], meeting["encrypted_password"])
            if incoming:
                old["settings"] = copy.deepcopy(meeting["settings"])
                meeting["settings"] = merge_settings(meeting["settings"], incoming)
                new["settings"] = incoming
            if "tracking_fields" in data:
                meeting["tracking_fields"] = data["tracking_fields"]
            retimed = {"start_time", "duration", "timezone", "recurrence", "type"} & set(new)
            if retimed:
                if meeting["type"] == 8 and meeting.get("recurrence"):
                    try:
                        regenerate_occurrences(meeting, lambda: runtime_uuid(ctx))
                    except (ValueError, OverflowError):
                        raise invalid_field("recurrence") from None
                else:
                    if meeting["type"] != 3 and not meeting.get("occurrences"):
                        start = parse_instant(meeting["start_time"])
                        meeting["occurrences"] = [
                            {
                                "occurrence_id": str(int(start.timestamp() * 1000)),
                                "start_time": meeting["start_time"],
                                "duration": meeting["duration"],
                                "status": "available",
                                "_uuid": meeting["uuid"],
                            }
                        ]
                    for occ in meeting.get("occurrences", []):
                        if not occ.get("_happened"):
                            occ["start_time"] = meeting.get("start_time", occ["start_time"])
                            occ["duration"] = meeting.get("duration", occ["duration"])
                            occ["occurrence_id"] = str(
                                int(parse_instant(occ["start_time"]).timestamp() * 1000)
                            )
        meetings.put(meeting)
        if new:
            await hooks.emit(
                ctx,
                "meeting.updated",
                {"id": meeting["id"], "uuid": meeting["uuid"], "host_id": meeting["host_id"], **new},
                operator=operator_of(principal),
                payload_extra={
                    "old_object": {
                        "id": meeting["id"],
                        "uuid": meeting["uuid"],
                        "host_id": meeting["host_id"],
                        **old,
                    },
                    "time_stamp": ctx.clock.timestamp() * 1000,
                    **({"scope": "single"} if occurrence_id else {}),
                },
            )
        return Response(status_code=204)

    # -- delete / status ------------------------------------------------------------------

    @app.delete("/v2/meetings/{meeting_id}")
    async def delete_meeting(request: Request, meeting_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(DELETE_MEETING)
        meeting = lookup.find_meeting(ctx, meeting_id)
        lookup.ensure_meeting_access(ctx, principal, admin, meeting, write=True)
        if meeting.get("status") == "started":
            raise zoom_error(400, 3000, "Cannot delete a meeting that is in progress.")
        occurrence_id = request.query_params.get("occurrence_id")
        if occurrence_id:
            occ = next(
                (o for o in meeting.get("occurrences", []) if o["occurrence_id"] == occurrence_id), None
            )
            if occ is None:
                raise zoom_error(
                    404,
                    E_MEETING_MISSING,
                    f"Meeting ID {meeting_id} occurrence {occurrence_id} does not exist.",
                )
            occ["status"] = "deleted"
            extra = {"scope": "single", "time_stamp": ctx.clock.timestamp() * 1000}
            await hooks.emit(
                ctx,
                "meeting.deleted",
                {
                    **shapes.meeting_payload(meeting),
                    "start_time": occ["start_time"],
                    "occurrence_id": occurrence_id,
                },
                operator=operator_of(principal),
                payload_extra=extra,
            )
            return Response(status_code=204)
        ctx.store.collection("deleted_meetings").put(copy.deepcopy(meeting))
        meetings.delete(str(meeting["id"]))
        await hooks.emit(
            ctx,
            "meeting.deleted",
            shapes.meeting_payload(meeting),
            operator=operator_of(principal),
            payload_extra={"scope": "all", "time_stamp": ctx.clock.timestamp() * 1000},
        )
        return Response(status_code=204)

    @app.put("/v2/meetings/{meeting_id}/status")
    async def meeting_status(request: Request, meeting_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(MEETING_STATUS)
        data = await json_body(request)
        action = data.get("action")
        if action not in ("end", "recover"):
            raise invalid_field("action")
        if action == "recover":
            gone = ctx.store.collection("deleted_meetings").get(str(meeting_id))
            if gone is None:
                raise zoom_error(404, E_MEETING_MISSING, f"Meeting does not exist: {meeting_id}.")
            lookup.ensure_meeting_access(ctx, principal, admin, gone, write=True)
            ctx.store.collection("deleted_meetings").delete(str(meeting_id))
            meetings.put(gone)
            return Response(status_code=204)
        meeting = lookup.find_meeting(ctx, meeting_id)
        lookup.ensure_meeting_access(ctx, principal, admin, meeting, write=True)
        if meeting.get("status") != "started":
            raise zoom_error(400, 3002, f"Meeting {meeting_id} is not in progress.")
        await simulate.end_meeting(
            ctx,
            meeting,
            record=meeting["settings"].get("auto_recording") == "cloud",
            operator=operator_of(principal),
        )
        return Response(status_code=204)

    # -- registrants ----------------------------------------------------------------------

    def registrants_of(meeting: dict[str, Any]) -> list[dict[str, Any]]:
        return [r for r in ctx.store.collection("registrants") if r["_meeting_id"] == meeting["id"]]

    def require_registration(meeting: dict[str, Any]) -> None:
        if meeting["settings"].get("approval_type", 2) == 2:
            raise zoom_error(
                400, 3000, f"Registration has not been enabled for this meeting: {meeting['id']}."
            )

    @app.get("/v2/meetings/{meeting_id}/registrants")
    async def list_registrants(request: Request, meeting_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(LIST_REGISTRANTS)
        meeting = lookup.find_meeting(ctx, meeting_id)
        lookup.ensure_meeting_access(ctx, principal, admin, meeting)
        require_registration(meeting)
        status = request.query_params.get("status", "approved")
        if status not in ("pending", "approved", "denied"):
            raise invalid_field("status")
        rows = [r for r in registrants_of(meeting) if r["status"] == status]
        page, meta = paginate(request, rows)
        return respond(request, ctx, {**meta, "registrants": page})

    @app.post("/v2/meetings/{meeting_id}/registrants")
    async def add_registrant(request: Request, meeting_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(ADD_REGISTRANT)
        meeting = lookup.find_meeting(ctx, meeting_id)
        lookup.ensure_meeting_access(ctx, principal, admin, meeting, write=True)
        require_registration(meeting)
        data = await json_body(request)
        email = str(data.get("email", "")).strip()
        if "@" not in email:
            raise invalid_field("email")
        if not data.get("first_name"):
            raise invalid_field("first_name")
        existing = next((r for r in registrants_of(meeting) if r["email"].lower() == email.lower()), None)
        if existing is None:
            rid = runtime_b64_22(ctx)
            token = ctx.ids.urlsafe(40)
            auto = meeting["settings"].get("approval_type") == 0 or bool(data.get("auto_approve"))
            existing = {
                "id": rid,
                "email": email,
                "first_name": data["first_name"],
                "last_name": data.get("last_name", ""),
                **{
                    k: data.get(k, "")
                    for k in (
                        "address",
                        "city",
                        "country",
                        "zip",
                        "state",
                        "phone",
                        "industry",
                        "org",
                        "job_title",
                        "purchasing_time_frame",
                        "role_in_purchase_process",
                        "no_of_employees",
                        "comments",
                    )
                },
                "custom_questions": data.get("custom_questions", []),
                "status": "approved" if auto else "pending",
                "create_time": isoformat_z(ctx.clock.now()),
                "join_url": f"https://us06web.zoom.us/w/{meeting['id']}?tk={token}&pwd={meeting['encrypted_password']}",
                "participant_pin_code": int(ctx.ids.digits(6)),
                "_meeting_id": meeting["id"],
            }
            ctx.store.collection("registrants").put(existing)
        body = {
            "id": meeting["id"],
            "join_url": existing["join_url"],
            "registrant_id": existing["id"],
            "start_time": meeting.get("start_time"),
            "topic": meeting["topic"],
            "participant_pin_code": existing["participant_pin_code"],
        }
        return respond(request, ctx, body, status=201)

    @app.put("/v2/meetings/{meeting_id}/registrants/status")
    async def registrant_status(request: Request, meeting_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(UPDATE_REGISTRANT)
        meeting = lookup.find_meeting(ctx, meeting_id)
        lookup.ensure_meeting_access(ctx, principal, admin, meeting, write=True)
        data = await json_body(request)
        action = data.get("action")
        if action not in ("approve", "cancel", "deny"):
            raise invalid_field("action")
        mapping = {"approve": "approved", "cancel": "cancelled", "deny": "denied"}
        known = {r["id"]: r for r in registrants_of(meeting)}
        selected = []
        for entry in data.get("registrants") or []:
            reg = known.get(entry.get("id")) or next(
                (r for r in known.values() if r["email"].lower() == str(entry.get("email", "")).lower()), None
            )
            if reg is None:
                raise zoom_error(
                    404, 3043, f"Registrant does not exist: {entry.get('id') or entry.get('email')}."
                )
            selected.append(reg)
        for reg in selected:
            reg["status"] = mapping[action]
        return Response(status_code=204)

    @app.delete("/v2/meetings/{meeting_id}/registrants/{registrant_id}")
    async def delete_registrant(request: Request, meeting_id: str, registrant_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(DELETE_REGISTRANT)
        meeting = lookup.find_meeting(ctx, meeting_id)
        lookup.ensure_meeting_access(ctx, principal, admin, meeting, write=True)
        registrant = ctx.store.collection("registrants").get(registrant_id)
        if registrant is None or registrant["_meeting_id"] != meeting["id"]:
            raise zoom_error(404, 3043, f"Registrant does not exist: {registrant_id}.")
        ctx.store.collection("registrants").delete(registrant_id)
        return Response(status_code=204)
