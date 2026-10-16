"""Teams online meetings, transcripts, recordings, attendance and call records."""

from __future__ import annotations

import copy
import html
import json
import math
import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common
from emulators.providers.microsoft import drive_model as dm
from emulators.providers.microsoft import meetings_model as mm
from emulators.world import documents, media

seed = mm.seed


def times(start: Any, end: Any) -> tuple[str, str]:
    try:
        a, b = parse_instant(start), parse_instant(end)
        if a >= b:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise common.graph_error(
            400, "BadRequest", "Valid startDateTime and endDateTime, with end after start, are required."
        ) from None
    return isoformat_z(a), isoformat_z(b)


def attendee_ids(ctx: ProviderContext, payload: dict[str, Any]) -> list[str]:
    result = []
    participants = payload.get("participants", {})
    if not isinstance(participants, dict) or not isinstance(participants.get("attendees", []), list):
        raise common.graph_error(400, "BadRequest", "participants.attendees must be an array.")
    for attendee in participants.get("attendees", []):
        if (
            not isinstance(attendee, dict)
            or not isinstance(attendee.get("identity", {}), dict)
            or not isinstance(attendee.get("identity", {}).get("user", {}), dict)
        ):
            raise common.graph_error(400, "BadRequest", "Each attendee must contain a user identity or upn.")
        identifier = attendee.get("identity", {}).get("user", {}).get("id") or attendee.get("upn")
        if not identifier:
            raise common.graph_error(400, "BadRequest", "Each attendee requires a user identity or upn.")
        result.append(dm.person_id(ctx, identifier))
    return result


async def create_meeting(
    request: Request, ctx: ProviderContext, pid: str, *, external: bool = False
) -> Response:
    payload = await dm.json_body(request)
    external_id = payload.get("externalId")
    if external and (not isinstance(external_id, str) or not external_id):
        raise common.graph_error(400, "BadRequest", "externalId is required.")
    if external:
        existing = ctx.store[mm.MEETINGS].first(_organizer=pid, externalId=external_id)
        if existing:
            return JSONResponse(dm.public(existing), status_code=200)
    start, end = times(payload.get("startDateTime"), payload.get("endDateTime"))
    if not isinstance(payload.get("subject"), str) or not payload["subject"].strip():
        raise common.graph_error(400, "BadRequest", "subject is required.")
    record = mm.meeting_shape(
        ctx, ctx.ids.guid(), pid, payload["subject"], start, end, attendee_ids(ctx, payload)
    )
    record["creationDateTime"] = isoformat_z(ctx.clock.now())
    if external:
        record["externalId"] = external_id
    for key in (
        "lobbyBypassSettings",
        "allowedPresenters",
        "isEntryExitAnnounced",
        "allowAttendeeToEnableMic",
        "allowAttendeeToEnableCamera",
        "recordAutomatically",
        "allowMeetingChat",
        "allowRecording",
        "allowTranscription",
    ):
        if key in payload:
            record[key] = payload[key]
    ctx.store[mm.MEETINGS].put(record)
    return JSONResponse(dm.public(record), status_code=201)


def transcript_content(
    request: Request, ctx: ProviderContext, record: dict[str, Any], metadata: bool = False
) -> Response:
    fmt = request.query_params.get("$format") or request.headers.get("accept", "text/vtt")
    if fmt in ("*/*", "application/json"):
        fmt = "text/vtt"
    if metadata:
        cache = "_metadata_blob"
        if cache not in record:
            metadata_cues = [
                media.Cue(
                    c["start"],
                    c["end"],
                    "",
                    json.dumps(
                        {
                            "startDateTime": isoformat_z(
                                parse_instant(record["createdDateTime"]) + timedelta(seconds=c["start"])
                            ),
                            "endDateTime": isoformat_z(
                                parse_instant(record["createdDateTime"]) + timedelta(seconds=c["end"])
                            ),
                            "speakerName": c["speaker"],
                            "spokenText": c["text"],
                            "spokenLanguage": "en-us",
                        },
                        ensure_ascii=False,
                    ),
                )
                for c in record["_cues"]
            ]
            record[cache] = ctx.store.blobs.put(
                html.unescape(
                    media.make_vtt(metadata_cues, with_voice_tags=False, cue_ids="none").decode()
                ).encode()
            )
        return Response(ctx.store.blobs.get(record[cache]), media_type="text/vtt")
    if fmt == documents.MIME_DOCX:
        cache = "_docx_blob"
        if cache not in record:
            meeting = ctx.store[mm.MEETINGS].get(record["_meeting"])
            data, _ = documents.render(
                "docx",
                meeting["subject"],
                [f"{c['speaker']}: {c['text']}" for c in record["_cues"]],
                created=parse_instant(record["createdDateTime"]),
            )
            record[cache] = ctx.store.blobs.put(data)
    elif fmt in ("text/vtt", "application/vnd.microsoft.graph.transcript+text"):
        if fmt != "text/vtt" and request.query_params.get("$format"):
            raise common.graph_error(400, "BadRequest", "Use Accept to request an unattributed transcript.")
        cache = "_vtt_blob" if fmt == "text/vtt" else "_plain_blob"
        if cache not in record:
            source = mm.cues(record)
            if fmt != "text/vtt":
                source = [media.Cue(c.start_s, c.end_s, "", c.text) for c in source]
            record[cache] = ctx.store.blobs.put(media.make_vtt(source, cue_ids="uuid"))
    else:
        raise common.graph_error(400, "BadRequest", "Unsupported transcript format.")
    return Response(ctx.store.blobs.get(record[cache]), media_type=fmt)


async def artifacts_dispatch(
    request: Request, ctx: ProviderContext, meeting: dict[str, Any], tail: str
) -> Any:
    if request.method != "GET":
        raise common.graph_error(405, "methodNotAllowed", "Meeting artifacts are read only.")
    parts = tail.split("/")
    kind = parts[0]
    if kind == "attendanceReports":
        reports = ctx.store[mm.REPORTS].where(_meeting=meeting["id"])
        if len(parts) == 1:
            return dm.collection(request, reports, context="Collection(meetingAttendanceReport)")
        report = next((r for r in reports if r["id"] == parts[1]), None)
        if not report:
            raise common.item_not_found()
        if len(parts) == 2:
            wire = dm.public(report)
            if "attendanceRecords" in request.query_params.get("$expand", ""):
                wire["attendanceRecords"] = copy.deepcopy(report["_records"])
            return dm.single(request, wire, "meetingAttendanceReport")
        if parts[2] == "attendanceRecords":
            if len(parts) == 3:
                return dm.collection(
                    request,
                    report["_records"],
                    context="Collection(attendanceRecord)",
                    search_fields=("emailAddress",),
                )
            record = next((r for r in report["_records"] if r["id"] == parts[3]), None)
            if record:
                return dm.single(request, record, "attendanceRecord")
        raise common.item_not_found()
    store = ctx.store[mm.TRANSCRIPTS if kind == "transcripts" else mm.RECORDINGS]
    records = store.where(_meeting=meeting["id"])
    if len(parts) == 1:
        return dm.collection(
            request,
            [mm.artifact_wire(request, ctx, r, kind) for r in records],
            context="Collection(callTranscript)" if kind == "transcripts" else "Collection(callRecording)",
        )
    record = next((r for r in records if r["id"] == parts[1]), None)
    if record is None:
        raise common.item_not_found()
    if len(parts) == 2:
        return dm.single(
            request,
            mm.artifact_wire(request, ctx, record, kind),
            "callTranscript" if kind == "transcripts" else "callRecording",
        )
    if parts[2] in ("content", "metadataContent"):
        if kind == "transcripts":
            return transcript_content(request, ctx, record, parts[2] == "metadataContent")
        if parts[2] == "content":
            item = ctx.store[dm.ITEMS].get(record["_drive_item"])
            data, mime = dm.content(ctx, item)
            return dm.binary_response(request, data, mime, f'"{record["id"]}"')
    raise common.item_not_found()


def get_all_artifacts(
    request: Request, ctx: ProviderContext, pid: str, function: str, p: common.Principal
) -> dict[str, Any]:
    if not p.app_only:
        raise common.access_denied("This API requires application permissions.")
    match = re.fullmatch(r"getAll(Transcripts|Recordings)\((.*)\)", function)
    if not match:
        raise common.item_not_found()
    args = dict(re.findall(r"(\w+)\s*=\s*('(?:[^']|'')*'|[^,]+)", match.group(2)))
    organizer = args.get("meetingOrganizerUserId", "").strip("'")
    if not organizer or dm.person_id(ctx, organizer) != pid:
        raise common.graph_error(
            400, "BadRequest", "meetingOrganizerUserId must match the user in the request path."
        )
    try:
        start = parse_instant(args["startDateTime"].strip("'"))
        end = parse_instant(args["endDateTime"].strip("'"))
        if start >= end:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise common.graph_error(
            400, "BadRequest", "A valid startDateTime/endDateTime interval is required."
        ) from None
    transcripts = match.group(1) == "Transcripts"
    source = ctx.store[mm.TRANSCRIPTS if transcripts else mm.RECORDINGS]
    kind = "transcripts" if transcripts else "recordings"
    records = [
        r
        for r in source
        if ctx.store[mm.MEETINGS].get(r["_meeting"])["_organizer"] == pid
        and start <= parse_instant(r["createdDateTime"]) < end
    ]
    return dm.collection(
        request,
        [mm.artifact_wire(request, ctx, r, kind) for r in records],
        context="Collection(callTranscript)" if transcripts else "Collection(callRecording)",
    )


async def meeting_dispatch(
    request: Request, ctx: ProviderContext, pid: str, tail: str, p: common.Principal
) -> Any:
    if tail.startswith(("getAllTranscripts(", "getAllRecordings(")):
        return get_all_artifacts(request, ctx, pid, tail, p)
    if tail == "createOrGet" and request.method == "POST":
        return await create_meeting(request, ctx, pid, external=True)
    if not tail:
        if request.method == "POST":
            return await create_meeting(request, ctx, pid)
        if request.method == "GET":
            filter_text = request.query_params.get("$filter", "")
            match = re.fullmatch(r"JoinWebUrl\s+eq\s+'((?:[^']|'')*)'", filter_text, re.IGNORECASE)
            if not match:
                raise common.graph_error(
                    400,
                    "BadRequest",
                    "Online meetings must be retrieved by id or an exact JoinWebUrl filter.",
                )
            join_url = match.group(1).replace("''", "'")
            records = [
                m
                for m in ctx.store[mm.MEETINGS]
                if m["joinWebUrl"] == join_url
                and pid in m["_attendees"]
                and (p.app_only or p.person_id in m["_attendees"])
            ]
            return dm.collection(request, records, context="Collection(onlineMeeting)")
    mid, _, operation = tail.partition("/")
    meeting = mm.require_meeting(ctx, mid, p, pid, write=request.method in ("PATCH", "DELETE"))
    if not operation:
        if request.method == "GET":
            return dm.single(request, meeting, "onlineMeeting")
        if request.method == "PATCH":
            payload = await dm.json_body(request)
            allowed = {
                "subject",
                "startDateTime",
                "endDateTime",
                "participants",
                "lobbyBypassSettings",
                "allowedPresenters",
                "isEntryExitAnnounced",
                "allowAttendeeToEnableMic",
                "allowAttendeeToEnableCamera",
                "recordAutomatically",
                "allowMeetingChat",
                "allowRecording",
                "allowTranscription",
            }
            if set(payload) - allowed:
                raise common.graph_error(400, "BadRequest", "One or more properties cannot be updated.")
            start, end = times(
                payload.get("startDateTime", meeting["startDateTime"]),
                payload.get("endDateTime", meeting["endDateTime"]),
            )
            if "subject" in payload and (
                not isinstance(payload["subject"], str) or not payload["subject"].strip()
            ):
                raise common.graph_error(400, "BadRequest", "subject cannot be empty.")
            if "participants" in payload:
                ids = attendee_ids(ctx, payload)
                shape = mm.meeting_shape(
                    ctx, meeting["_key"], meeting["_organizer"], meeting["subject"], start, end, ids
                )
                payload["participants"] = shape["participants"]
                meeting["_attendees"] = shape["_attendees"]
            meeting.update(payload, startDateTime=start, endDateTime=end)
            return dm.public(meeting)
        if request.method == "DELETE":
            ctx.store[mm.MEETINGS].delete(mid)
            for name in (mm.TRANSCRIPTS, mm.RECORDINGS, mm.REPORTS, mm.CALLS):
                for record in ctx.store[name].where(_meeting=mid):
                    ctx.store[name].delete(record["id"])
            return Response(status_code=204)
    if operation.split("/", 1)[0] in ("transcripts", "recordings", "attendanceReports"):
        return await artifacts_dispatch(request, ctx, meeting, operation)
    raise common.item_not_found()


async def add_transcript(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Add a transcript to a Teams meeting using turns with world person speaker ids."""
    meeting = action_meeting(ctx, payload)
    turns = payload.get("turns")
    if turns is None:
        text = payload.get(
            "text", "The meeting has concluded. Follow-up actions are recorded in the engagement tracker."
        )
        if not isinstance(text, str):
            raise common.graph_error(400, "BadRequest", "text must be a string.")
        turns = [
            {
                "speaker_id": meeting["_organizer"],
                "text": text,
                "offset_s": 0,
                "duration_s": max(1, len(text.split()) * 0.4),
            }
        ]
    if not isinstance(turns, list) or not turns:
        raise common.graph_error(400, "BadRequest", "turns must be a nonempty array.")
    cues = []
    for turn in turns:
        if not isinstance(turn, dict):
            raise common.graph_error(400, "BadRequest", "Each transcript turn must be an object.")
        pid = dm.person_id(ctx, turn.get("speaker_id", meeting["_organizer"]))
        if pid not in meeting["_attendees"]:
            raise common.graph_error(
                400, "BadRequest", "Transcript speakers must participate in the meeting."
            )
        try:
            start = float(turn.get("offset_s", turn.get("start", 0)))
            duration = float(turn.get("duration_s", 4))
            if (
                not math.isfinite(start)
                or not math.isfinite(duration)
                or start < 0
                or duration <= 0
                or not isinstance(turn["text"], str)
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise common.graph_error(400, "BadRequest", "Invalid transcript turn.") from None
        cues.append(
            {
                "start": start,
                "end": start + duration,
                "speaker": ctx.world.person(pid).display_name,
                "speaker_id": pid,
                "text": turn["text"],
            }
        )
    index = len(ctx.store[mm.TRANSCRIPTS].where(_meeting=meeting["id"]))
    transcript = mm.artifact(ctx, meeting, "transcript", index)
    transcript["_cues"] = cues
    ctx.store[mm.TRANSCRIPTS].put(transcript)
    uid = common.user_id(ctx.world, meeting["_organizer"])
    await common.publish_change(
        ctx,
        f"users/{uid}/onlineMeetings/{meeting['id']}/transcripts/{transcript['id']}",
        "created",
        dm.public(transcript),
    )
    await common.publish_change(
        ctx,
        f"communications/onlineMeetings/{meeting['id']}/transcripts/{transcript['id']}",
        "created",
        dm.public(transcript),
    )
    return dm.public(transcript)


def action_meeting(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    identifier = payload.get("meeting_id")
    meeting = ctx.store[mm.MEETINGS].get(identifier) or ctx.store[mm.MEETINGS].first(_key=identifier)
    if meeting is None:
        raise common.item_not_found()
    return meeting


async def end_meeting(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """End a Teams meeting and generate its transcript, recording and attendance report."""
    meeting = action_meeting(ctx, payload)
    if meeting["_ended"]:
        return {"meetingId": meeting["id"], "status": "ended"}
    if not ctx.store[mm.TRANSCRIPTS].where(_meeting=meeting["id"]):
        await add_transcript(ctx, {**payload, "meeting_id": meeting["id"]})
    meeting["_ended"] = True
    if not ctx.store[mm.RECORDINGS].where(_meeting=meeting["id"]):
        recording = mm.artifact(ctx, meeting, "recording")
        drive_item = dm.add_recording(
            ctx,
            meeting["_key"],
            meeting["_organizer"],
            meeting["subject"],
            parse_instant(meeting["startDateTime"]),
            parse_instant(meeting["endDateTime"]),
        )
        recording["_drive_item"] = drive_item["id"]
        ctx.store[mm.RECORDINGS].put(recording)
        await dm.changed(ctx, drive_item, "created")
        uid = common.user_id(ctx.world, meeting["_organizer"])
        await common.publish_change(
            ctx,
            f"users/{uid}/onlineMeetings/{meeting['id']}/recordings/{recording['id']}",
            "created",
            dm.public(recording),
        )
        await common.publish_change(
            ctx,
            f"communications/onlineMeetings/{meeting['id']}/recordings/{recording['id']}",
            "created",
            dm.public(recording),
        )
    if not ctx.store[mm.REPORTS].where(_meeting=meeting["id"]):
        records = [
            mm.attendance_record(
                ctx,
                pid,
                meeting["startDateTime"],
                meeting["endDateTime"],
                "organizer" if pid == meeting["_organizer"] else "attendee",
            )
            for pid in meeting["_attendees"]
        ]
        mm.create_attendance(ctx, meeting, records)
        call_id = mm.artifact(ctx, meeting, "recording")["callId"]
        await common.publish_change(
            ctx,
            f"communications/callRecords/{call_id}",
            "created",
            dm.public(ctx.store[mm.CALLS].get(call_id)),
        )
    return {"meetingId": meeting["id"], "status": "ended"}


def register(app: FastAPI, ctx: ProviderContext) -> None:
    ctx.provider.actions["end_meeting"] = end_meeting
    ctx.provider.actions["add_transcript"] = add_transcript

    def router() -> APIRouter:
        routes = APIRouter()

        @routes.api_route("/me/onlineMeetings", methods=dm.METHODS)
        @routes.api_route("/me/onlineMeetings/{tail:path}", methods=dm.METHODS)
        async def my_meetings(request: Request, tail: str = "") -> Any:
            p = common.principal(request, ctx)
            common.me_user_id(p)
            return await meeting_dispatch(request, ctx, p.person_id, tail, p)

        @routes.api_route("/users/{uid}/onlineMeetings", methods=dm.METHODS)
        @routes.api_route("/users/{uid}/onlineMeetings/{tail:path}", methods=dm.METHODS)
        async def user_meetings(request: Request, uid: str, tail: str = "") -> Any:
            p = common.principal(request, ctx)
            pid = dm.person_id(ctx, uid)
            if p.delegated and pid != p.person_id and (not tail or request.method == "POST"):
                raise common.access_denied("Delegated callers must address their own user context.")
            return await meeting_dispatch(request, ctx, pid, tail, p)

        @routes.get("/communications/callRecords/{cid}")
        @routes.get("/communications/callRecords/{cid}/{tail:path}")
        async def call_records(request: Request, cid: str, tail: str = "") -> Any:
            p = common.principal(request, ctx)
            if not p.app_only:
                raise common.access_denied("Call records require application permissions.")
            call = ctx.store[mm.CALLS].get(cid)
            if call is None:
                raise common.item_not_found()
            sessions = copy.deepcopy(call["_sessions"])
            expand = request.query_params.get("$expand", "")
            if tail.startswith("sessions"):
                if "segments" not in expand:
                    sessions = [{k: v for k, v in s.items() if k != "segments"} for s in sessions]
                if tail == "sessions":
                    return dm.collection(
                        request, sessions, context="Collection(microsoft.graph.callRecords.session)"
                    )
                sid = tail.split("/")[1]
                session = next((s for s in sessions if s["id"] == sid), None)
                if session:
                    return dm.single(request, session, "microsoft.graph.callRecords.session")
                raise common.item_not_found()
            if tail:
                raise common.item_not_found()
            record = dm.public(call)
            if "sessions" in expand:
                record["sessions"] = (
                    sessions
                    if "segments" in expand
                    else [{k: v for k, v in s.items() if k != "segments"} for s in sessions]
                )
            return dm.single(request, record, "microsoft.graph.callRecords.callRecord")

        return routes

    for version in common.GRAPH_VERSIONS:
        app.include_router(router(), prefix="/" + version, tags=["Online meetings / recordings"])
