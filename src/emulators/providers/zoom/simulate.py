"""Live-meeting simulation: start, join, leave, end, and webhook replays.

``/_emulator/actions/start_meeting`` .. ``end_meeting`` drive the same state machine real Zoom
goes through, emitting ``meeting.started``, ``meeting.participant_joined``/``_left``,
``meeting.ended`` and, after "processing", ``recording.completed`` and
``recording.transcript_completed`` with generated files (valid MP4/M4A/VTT) that download from
the usual ``download_url``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from emulators.core.clock import isoformat_z
from emulators.core.errors import EmulatorError
from emulators.core.provider import Provider, ProviderContext
from emulators.providers.zoom import dialogue, files, lookup
from emulators.providers.zoom import webhooks as hooks
from emulators.providers.zoom.common import (
    decode_path_key,
    digits_str,
    is_meeting_id,
    runtime_b64_22,
    runtime_uuid,
)
from emulators.providers.zoom.timeutil import parse_instant
from emulators.world.model import Person

Record = dict[str, Any]


def fail(status: int, message: str, **extra: Any) -> EmulatorError:
    return EmulatorError(status, {"error": message, **extra})


def resolve_meeting(ctx: ProviderContext, payload: Record) -> Record:
    key = payload.get("meeting_id") or payload.get("uuid") or payload.get("id")
    if key is None:
        raise fail(400, "meeting_id (or uuid) is required")
    text = decode_path_key(str(key))
    meeting = ctx.store.collection("meetings").get(text.replace(" ", "")) if is_meeting_id(text) else None
    if meeting is None:
        meeting = lookup.meeting_by_uuid(ctx, text)
    if meeting is None:
        raise fail(404, f"Meeting does not exist: {key}.")
    return meeting


def live_instance(ctx: ProviderContext, meeting: Record) -> Record | None:
    inst = ctx.store.collection("instances").get(meeting["uuid"])
    return inst if inst and inst.get("state") == "live" else None


def find_person(ctx: ProviderContext, key: str | None) -> Person | None:
    if not key:
        return None
    needle = key.strip().lower()
    world = ctx.world
    for person in world.people:
        if needle in (person.id.lower(), person.email.lower(), person.display_name.lower()):
            return person
    return None


def pick_occurrence(meeting: Record, occurrence_id: str | None) -> Record | None:
    occs = meeting.get("occurrences") or []
    if occurrence_id:
        found = next((o for o in occs if o["occurrence_id"] == str(occurrence_id)), None)
        if found is None:
            raise fail(404, f"occurrence {occurrence_id} does not exist")
        return found
    return next((o for o in occs if not o.get("_happened") and o["status"] != "deleted"), None)


def participant_row(ctx: ProviderContext, instance: Record, spec: Any, join: datetime) -> Record:
    if isinstance(spec, str):
        spec = {"user": spec}
    key = spec.get("user") or spec.get("email") or spec.get("person_id")
    person = find_person(ctx, key) or find_person(ctx, spec.get("name"))
    internal = person is not None and not person.is_external
    name = spec.get("name") or (person.display_name if person else str(key or "Guest"))
    email = person.email if person else str(spec.get("email") or "")
    guest_emails = (ctx.setting("guest_emails", "") or "").lower() in ("1", "true", "yes")
    return {
        "id": runtime_b64_22(ctx),
        "name": name,
        "user_id": digits_str(ctx.world, "uid", person.id, digits=16) if internal and person else "",
        "registrant_id": "",
        "user_email": email if (internal or guest_emails or spec.get("show_email")) else "",
        "join_time": isoformat_z(join),
        "leave_time": "",
        "duration": 0,
        "failover": False,
        "status": "in_meeting",
        "internal_user": internal,
        "_person_id": person.id if person else None,
        "_email": email,
        "_open": True,
        "_stay_s": int(float(spec["stay_minutes"]) * 60) if spec.get("stay_minutes") is not None else None,
    }


def instance_object(instance: Record, meeting: Record, **extra: Any) -> Record:
    return {
        "id": str(meeting["id"]),
        "uuid": instance["id"],
        "host_id": instance["host_id"],
        "topic": instance["topic"],
        "type": instance["type"],
        "start_time": instance["start_time"],
        "timezone": instance["timezone"],
        "duration": meeting.get("duration") or 0,
        **extra,
    }


def participant_object(instance: Record, meeting: Record, row: Record, *, leave: str | None = None) -> Record:
    who: Record = {
        "public_ip": "198.51.100.23",
        "user_id": row["user_id"],
        "user_name": row["name"],
        "participant_user_id": row["user_id"],
        "id": row["id"],
        "participant_uuid": b64_to_hex(row["id"]),
        "join_time": row["join_time"],
        "email": row["user_email"],
        "registrant_id": row["registrant_id"],
        "private_ip": "10.0.0.12",
    }
    if leave is not None:
        who["leave_time"] = leave
        who["leave_reason"] = "left the meeting. Reason : left the meeting."
    return {**instance_object(instance, meeting), "participant": who}


def b64_to_hex(value: str) -> str:
    import base64

    return base64.urlsafe_b64decode(value + "==").hex()


def deliveries(sent: list[Record]) -> list[Record]:
    return [{"event": d["event_type"], "status": d["status"], "id": d["id"]} for d in sent]


# -- state machine ------------------------------------------------------------------------


async def start_meeting(
    ctx: ProviderContext,
    meeting: Record,
    *,
    participants: list[Any] | None = None,
    occurrence_id: str | None = None,
    operator: Person | None = None,
) -> tuple[Record, list[Record]]:
    if meeting.get("status") == "started":
        raise fail(400, "meeting is already in progress", uuid=meeting["uuid"])
    occ = pick_occurrence(meeting, occurrence_id)
    uuid = occ["_uuid"] if occ and occ.get("_uuid") else runtime_uuid(ctx)
    if ctx.store.collection("instances").get(uuid) is not None:
        uuid = runtime_uuid(ctx)
        if occ is not None:
            occ["_uuid"] = uuid
    host = ctx.store.collection("users").get(meeting["host_id"]) or {}
    now = ctx.clock.now().replace(microsecond=0)
    instance: Record = {
        "id": uuid,
        "meeting_id": meeting["id"],
        "host_id": meeting["host_id"],
        "host_email": meeting["host_email"],
        "topic": meeting["topic"],
        "type": meeting["type"],
        "start_time": isoformat_z(now),
        "end_time": "",
        "duration": 0,
        "total_minutes": 0,
        "participants_count": 0,
        "timezone": meeting["timezone"],
        "user_name": host.get("display_name", ""),
        "user_email": meeting["host_email"],
        "dept": host.get("dept", ""),
        "source": "Zoom",
        "state": "live",
        "occurrence_id": occ["occurrence_id"] if occ else None,
        "participants": [],
        "chat": [],
        "transcript": [],
        "recorded": False,
        "transcribed": False,
        "recording_offset_s": 5,
    }
    ctx.store.collection("instances").put(instance)
    meeting["status"] = "started"
    meeting["uuid"] = uuid
    sent = await hooks.emit(ctx, "meeting.started", instance_object(instance, meeting), operator=operator)
    host_person = find_person(ctx, meeting["host_email"])
    host_spec = {
        "user": host_person.id if host_person else meeting["host_email"],
        "name": host.get("display_name"),
    }
    sent += await join(ctx, instance, meeting, host_spec, now)
    for spec in participants or []:
        sent += await join(ctx, instance, meeting, spec, now)
    return instance, sent


async def join(
    ctx: ProviderContext, instance: Record, meeting: Record, spec: Any, at: datetime | None = None
) -> list[Record]:
    offset = float(spec.get("join_offset_minutes", 0)) if isinstance(spec, dict) else 0.0
    moment = (at or ctx.clock.now()).replace(microsecond=0) + timedelta(minutes=offset)
    row = participant_row(ctx, instance, spec, moment)
    instance["participants"].append(row)
    instance["participants_count"] = len(instance["participants"])
    return await hooks.emit(ctx, "meeting.participant_joined", participant_object(instance, meeting, row))


async def leave(
    ctx: ProviderContext, instance: Record, meeting: Record, row: Record, at: datetime
) -> list[Record]:
    joined = parse_instant(row["join_time"])
    at = max(at, joined).replace(microsecond=0)
    row["leave_time"] = isoformat_z(at)
    row["duration"] = int((at - joined).total_seconds())
    row["_open"] = False
    return await hooks.emit(
        ctx, "meeting.participant_left", participant_object(instance, meeting, row, leave=row["leave_time"])
    )


def open_rows(instance: Record) -> list[Record]:
    return [r for r in instance["participants"] if r.get("_open")]


def find_row(instance: Record, payload: Record) -> Record | None:
    wanted = {str(payload[k]).lower() for k in ("participant_id", "user", "email", "name") if payload.get(k)}
    for row in open_rows(instance):
        if wanted & {
            row["id"].lower(),
            row["name"].lower(),
            row.get("_email", "").lower(),
            str(row.get("_person_id")).lower(),
        }:
            return row
    return None


async def end_meeting(
    ctx: ProviderContext,
    meeting: Record,
    *,
    record: bool = True,
    transcribe: bool = True,
    duration_minutes: float | None = None,
    chat: list[Record] | None = None,
    operator: Person | None = None,
) -> tuple[Record, list[Record]]:
    instance = live_instance(ctx, meeting)
    if instance is None:
        raise fail(400, "meeting is not in progress", uuid=meeting["uuid"])
    start = parse_instant(instance["start_time"])
    scheduled = None
    occ = next(
        (o for o in meeting.get("occurrences", []) if o["occurrence_id"] == instance["occurrence_id"]), None
    )
    if occ:
        scheduled = occ["duration"]
    minutes = (
        float(duration_minutes)
        if duration_minutes is not None
        else float(scheduled or meeting.get("duration") or 30)
    )
    end = start + timedelta(minutes=max(minutes, 1 / 60))
    sent: list[Record] = []
    for row in open_rows(instance):
        stay = row.get("_stay_s")
        join_dt = parse_instant(row["join_time"])
        left = min(end, join_dt + timedelta(seconds=stay)) if stay is not None else end
        sent += await leave(ctx, instance, meeting, row, left)
    seconds = (end - start).total_seconds()
    rows = instance["participants"]
    instance.update(
        {
            "state": "ended",
            "end_time": isoformat_z(end),
            "duration": max(1, math.ceil(seconds / 60)),
            "total_minutes": sum(max(1, math.ceil(r["duration"] / 60)) for r in rows),
            "participants_count": len({r["name"] for r in rows}),
            "recorded": record,
            "transcribed": bool(record and transcribe),
        }
    )
    if chat:
        instance["chat"] = [
            {
                "offset_s": float(c.get("offset_s", 30 * (i + 1))),
                "name": c.get("from") or c.get("name") or "Guest",
                "email": c.get("email", ""),
                "time": isoformat_z(start + timedelta(seconds=float(c.get("offset_s", 30 * (i + 1))))),
                "text": c["text"],
            }
            for i, c in enumerate(chat)
        ]
    meeting["status"] = "waiting"
    if occ is not None:
        occ["_happened"] = True
    sent += await hooks.emit(
        ctx,
        "meeting.ended",
        instance_object(instance, meeting, end_time=instance["end_time"], duration=instance["duration"]),
        operator=operator,
    )
    if record:
        sent += await process_recording(ctx, instance, meeting)
    return instance, sent


async def process_recording(ctx: ProviderContext, instance: Record, meeting: Record) -> list[Record]:
    names = [r["name"] for r in instance["participants"]]
    host = instance["user_name"] or (names[0] if names else "Host")
    if instance["transcribed"] and not instance["transcript"]:
        lines = dialogue.script(instance["topic"], host, [n for n in names if n != host], instance["id"])
        seconds = (
            parse_instant(instance["end_time"]) - parse_instant(instance["start_time"])
        ).total_seconds() - instance["recording_offset_s"]
        turns = dialogue.timed(lines, seconds, instance["id"])
        emails = {r["name"]: r.get("user_email", "") for r in instance["participants"]}
        instance["transcript"] = files.cues_to_records(files.make_cues(turns), emails)
    recording = files.build_recording(ctx, instance)
    ctx.store.collection("recordings").put(recording)
    sent = await hooks.emit(
        ctx,
        "recording.completed",
        recording_object(recording),
        top_extra={"download_token": hooks.issue_download_token(ctx, recording["id"])},
    )
    if instance["transcribed"]:
        sent += await hooks.emit(
            ctx,
            "recording.transcript_completed",
            recording_object(recording, only=("TRANSCRIPT",)),
            top_extra={"download_token": hooks.issue_download_token(ctx, recording["id"])},
        )
    return sent


def recording_object(recording: Record, *, only: tuple[str, ...] | None = None) -> Record:
    wire = files.recording_wire(recording)
    if only is not None:
        kept = [f for f in wire["recording_files"] if f["file_type"] in only]
        wire["recording_files"] = kept
        wire["recording_count"] = len(kept)
        wire["total_size"] = sum(f["file_size"] for f in kept)
    wire["participant_audio_files"] = []
    return wire


# -- actions ------------------------------------------------------------------------------


def actions(provider: Provider) -> None:
    @provider.action("start_meeting")
    async def start_action(ctx: ProviderContext, payload: Record) -> Record:
        """Start a meeting: ``{meeting_id, participants?, occurrence_id?}``.

        Creates the live instance, emits ``meeting.started`` and ``meeting.participant_joined`` for the
        host and every entry of ``participants`` (person id / email / name, or ``{user, name,
        join_offset_minutes, stay_minutes}``; ``"all"`` joins the invited attendees)."""
        meeting = resolve_meeting(ctx, payload)
        specs = payload.get("participants") or []
        if specs == "all":
            world_ids = meeting.get("_world_meeting_ids") or []
            invited = next((m for m in ctx.world.meetings if m.id in world_ids), None)
            specs = [p for p in invited.all_attendee_ids() if p != invited.organizer_id] if invited else []
        instance, sent = await start_meeting(
            ctx, meeting, participants=specs, occurrence_id=payload.get("occurrence_id")
        )
        return {
            "meeting_id": meeting["id"],
            "uuid": instance["id"],
            "status": "started",
            "deliveries": deliveries(sent),
        }

    @provider.action("participant_join")
    async def join_action(ctx: ProviderContext, payload: Record) -> Record:
        """A participant joins a live meeting: ``{meeting_id, user|email|name, join_offset_minutes?}``."""
        meeting = resolve_meeting(ctx, payload)
        instance = live_instance(ctx, meeting)
        if instance is None:
            raise fail(400, "meeting is not in progress")
        spec = {k: v for k, v in payload.items() if k != "meeting_id"}
        if not any(spec.get(k) for k in ("user", "email", "name", "person_id")):
            raise fail(400, "user, email or name is required")
        sent = await join(ctx, instance, meeting, spec)
        row = instance["participants"][-1]
        return {"participant_id": row["id"], "name": row["name"], "deliveries": deliveries(sent)}

    @provider.action("participant_leave")
    async def leave_action(ctx: ProviderContext, payload: Record) -> Record:
        """A participant leaves: ``{meeting_id, participant_id | user | email | name}``."""
        meeting = resolve_meeting(ctx, payload)
        instance = live_instance(ctx, meeting)
        if instance is None:
            raise fail(400, "meeting is not in progress")
        row = find_row(instance, payload)
        if row is None:
            raise fail(404, "no such participant in the meeting")
        moment = ctx.clock.now() + timedelta(minutes=float(payload.get("after_minutes", 0)))
        sent = await leave(ctx, instance, meeting, row, moment)
        return {"participant_id": row["id"], "duration": row["duration"], "deliveries": deliveries(sent)}

    @provider.action("end_meeting")
    async def end_action(ctx: ProviderContext, payload: Record) -> Record:
        """End a live meeting: ``{meeting_id, record?=true, transcribe?=true, duration_minutes?, chat?}``.

        Emits ``participant_left`` for everyone still in, then ``meeting.ended``; with ``record`` it then
        generates the cloud recording (MP4, M4A, TRANSCRIPT VTT, TIMELINE, CHAT when ``chat`` lines are
        given) and emits ``recording.completed`` and ``recording.transcript_completed``.
        ``duration_minutes`` defaults to the scheduled duration (simulated, so tests need not wait)."""
        meeting = resolve_meeting(ctx, payload)
        instance, sent = await end_meeting(
            ctx,
            meeting,
            record=bool(payload.get("record", True)),
            transcribe=bool(payload.get("transcribe", True)),
            duration_minutes=payload.get("duration_minutes"),
            chat=payload.get("chat"),
        )
        return {
            "meeting_id": meeting["id"],
            "uuid": instance["id"],
            "status": "ended",
            "duration": instance["duration"],
            "recording": bool(ctx.store.collection("recordings").get(instance["id"])),
            "deliveries": deliveries(sent),
        }

    @provider.action("replay_event")
    async def replay_action(ctx: ProviderContext, payload: Record) -> Record:
        """Re-send an event for an existing meeting instance: ``{event, meeting_id | uuid}``.

        Works for ``meeting.started``/``meeting.ended``, ``meeting.participant_joined``/``_left`` (one per
        participant), ``recording.completed``, ``recording.transcript_completed``, ``recording.trashed``
        and ``recording.recovered`` -- handy to test a consumer against the seeded recordings."""
        event = payload.get("event")
        key = payload.get("uuid") or payload.get("meeting_id")
        if not event or key is None:
            raise fail(400, "event and meeting_id (or uuid) are required")
        instance = lookup.find_instance(ctx, str(key))
        meeting = ctx.store.collection("meetings").get(str(instance["meeting_id"])) or {
            "id": instance["meeting_id"],
            "duration": instance["duration"],
        }
        sent: list[Record] = []
        if event in ("meeting.started", "meeting.ended"):
            extra = {"end_time": instance["end_time"]} if event == "meeting.ended" else {}
            sent = await hooks.emit(ctx, event, instance_object(instance, meeting, **extra))
        elif event in ("meeting.participant_joined", "meeting.participant_left"):
            for row in instance["participants"]:
                leave_time = row["leave_time"] if event.endswith("left") else None
                sent += await hooks.emit(
                    ctx, event, participant_object(instance, meeting, row, leave=leave_time)
                )
        elif event in (
            "recording.completed",
            "recording.transcript_completed",
            "recording.trashed",
            "recording.recovered",
        ):
            recording = ctx.store.collection("recordings").get(instance["id"])
            if recording is None:
                raise fail(404, "that meeting instance has no cloud recording")
            only = ("TRANSCRIPT",) if event == "recording.transcript_completed" else None
            top = (
                {"download_token": hooks.issue_download_token(ctx, recording["id"])}
                if event.startswith("recording.")
                and event in ("recording.completed", "recording.transcript_completed")
                else None
            )
            sent = await hooks.emit(ctx, event, recording_object(recording, only=only), top_extra=top)
        else:
            raise fail(400, f"cannot replay '{event}'", events=list(hooks.EVENT_NAMES))
        return {"uuid": instance["id"], "deliveries": deliveries(sent)}
