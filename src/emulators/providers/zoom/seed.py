"""Project the shared world into Zoom: users, groups, meetings, past instances, recordings.

Everything is derived from ``ctx.world`` and stable hashes, so ids are identical on every run:
user ids, meeting ids/UUIDs, participant ids and recording tokens can be hard-coded in tests.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.providers.zoom import files as rec_files
from emulators.providers.zoom import shapes
from emulators.providers.zoom.common import (
    b64_22,
    digits_str,
    meeting_uuid,
    number,
    owner_person,
    zoom_user_id,
)
from emulators.world.model import Meeting, Participation, World

BASIC_USER_MODULUS = 9
ZOOM_GROUP_KINDS = ("department", "role", "all_staff")


def seed(ctx: ProviderContext) -> None:
    world = ctx.world
    seed_groups(ctx)
    seed_users(ctx)
    seed_meetings(ctx)
    ctx.store.meta["owner_zoom_id"] = zoom_user_id(world, owner_person(world).id)


# -- users and groups ---------------------------------------------------------------------


def seed_groups(ctx: ProviderContext) -> None:
    world = ctx.world
    groups = ctx.store.collection("groups")
    for group in world.groups:
        if group.kind not in ZOOM_GROUP_KINDS:
            continue
        member_ids = [zoom_user_id(world, pid) for pid in group.member_ids if world.person(pid).active]
        groups.put({"id": b64_22(world, "group", group.id), "name": group.name, "_member_ids": member_ids, "_world_id": group.id})


def seed_users(ctx: ProviderContext) -> None:
    world = ctx.world
    owner = owner_person(world)
    hosts = {m.organizer_id for m in world.meetings if m.platform == "zoom"}
    users = ctx.store.collection("users")
    memberships: dict[str, list[str]] = defaultdict(list)
    for group in ctx.store.collection("groups"):
        for zid in group["_member_ids"]:
            memberships[zid].append(group["id"])
    for person in world.employees:
        role_id = "0" if person.id == owner.id else ("1" if person.is_admin else "2")
        basic = (
            role_id == "2" and person.id not in hosts and number(world, "basic", person.id, digits=6) % BASIC_USER_MODULUS == 0
        )
        zid = zoom_user_id(world, person.id)
        users.put(
            shapes.user_record(
                world, person, role_id=role_id, user_type=1 if basic else 2, group_ids=memberships.get(zid, [])
            )
        )


# -- meetings -----------------------------------------------------------------------------


def _meeting_number(world: World, taken: set[int], key: str) -> int:
    n = number(world, "meeting", key, digits=11 if number(world, "mlen", key, digits=2) % 2 else 10)
    while n in taken:
        n += 1
    taken.add(n)
    return n


def _settings_for(world: World, first: Meeting, alt_host: Person | None, recorded: bool) -> dict[str, Any]:
    client_facing = first.kind in ("client", "external")
    return shapes.default_settings(
        host_video=client_facing,
        participant_video=not client_facing,
        join_before_host=not client_facing,
        jbh_time=5 if not client_facing else 0,
        mute_upon_entry=first.kind in ("all_hands", "internal") and len(first.attendee_ids) > 6,
        waiting_room=client_facing,
        auto_recording="cloud" if recorded else "none",
        alternative_hosts=alt_host.email if alt_host else "",
        meeting_authentication=False,
        show_share_button=True,
        email_notification=True,
        contact_name=world.person(first.organizer_id).display_name,
        contact_email=world.person(first.organizer_id).email,
    )


def seed_meetings(ctx: ProviderContext) -> None:
    world = ctx.world
    zoom_meetings = sorted((m for m in world.meetings if m.platform == "zoom"), key=lambda m: (m.start, m.id))
    series: dict[str, list[Meeting]] = {}
    for m in zoom_meetings:
        series.setdefault(m.series_id or m.id, []).append(m)

    taken: set[int] = set()
    meetings = ctx.store.collection("meetings")
    forced = iter(("leading_slash", "double_slash", "leading_slash", "double_slash"))
    for key, group in series.items():
        first = group[0]
        host = world.person(first.organizer_id)
        host_zid = zoom_user_id(world, host.id)
        mid = _meeting_number(world, taken, key)
        recurring = first.recurrence is not None
        any_recorded = any(m.recorded for m in group)
        alt = next(
            (world.person(p) for p in first.attendee_ids if not world.person(p).is_external and p != host.id),
            None,
        )
        pwd = shapes.numeric_password(world, mid)
        enc = shapes.encrypted_password(world, mid)
        occurrences: list[dict[str, Any]] = []
        instances: list[tuple[Meeting, str]] = []
        for m in group:
            variant = next(forced, None) if (m.happened and m.recorded) else None
            uuid = meeting_uuid(world, mid, m.start.isoformat(), variant=variant)
            occurrences.append(
                {
                    "occurrence_id": str(int(m.start.timestamp() * 1000)),
                    "start_time": isoformat_z(m.start),
                    "duration": m.duration_minutes,
                    "status": "deleted" if m.cancelled else "available",
                    "_uuid": uuid,
                    "_happened": m.happened,
                }
            )
            if m.happened:
                instances.append((m, uuid))
        latest_uuid = instances[-1][1] if instances else occurrences[0]["_uuid"]
        record: dict[str, Any] = {
            "id": mid,
            "uuid": latest_uuid,
            "host_id": host_zid,
            "host_email": host.email,
            "topic": first.title,
            "type": 8 if recurring else 2,
            "status": "waiting",
            "start_time": isoformat_z(first.start),
            "duration": first.duration_minutes,
            "timezone": first.timezone,
            "agenda": first.description,
            "created_at": isoformat_z(first.start - timedelta(days=14, hours=3)),
            "start_url": shapes.start_url(world, mid, host_zid),
            "join_url": shapes.join_url(mid, enc),
            "password": pwd,
            "h323_password": pwd,
            "pstn_password": pwd,
            "encrypted_password": enc,
            "settings": _settings_for(world, first, alt, any_recorded),
            "pre_schedule": False,
            "_world_series": key,
            "_world_meeting_ids": [m.id for m in group],
        }
        if recurring:
            record["recurrence"] = _recurrence_for(first, group)
            record["occurrences"] = occurrences
        else:
            record["occurrences"] = occurrences
        meetings.put(record)
        for m, uuid in instances:
            seed_instance(ctx, record, m, uuid)


def _recurrence_for(first: Meeting, group: list[Meeting]) -> dict[str, Any]:
    zone = ZoneInfo(first.timezone)
    local = first.start.astimezone(zone)
    last = group[-1].start
    zdow = shapes.zoom_weekday(local.date())
    if first.recurrence == "monthly":
        return {"type": 3, "repeat_interval": 1, "monthly_day": local.day, "end_date_time": isoformat_z(last + timedelta(hours=1))}
    interval = 2 if first.recurrence == "biweekly" else 1
    return {
        "type": 2,
        "repeat_interval": interval,
        "weekly_days": str(zdow),
        "end_date_time": isoformat_z(last + timedelta(hours=1)),
    }


def participant_rows(ctx: ProviderContext, uuid: str, parts: tuple[Participation, ...]) -> list[dict[str, Any]]:
    world = ctx.world
    guest_emails = (ctx.setting("guest_emails", "") or "").lower() in ("1", "true", "yes")
    rows = []
    for pt in sorted(parts, key=lambda p: p.joined):
        person = world.person(pt.person_id)
        internal = not person.is_external
        rows.append(
            {
                "id": b64_22(world, "participant", uuid, person.id),
                "name": person.display_name,
                "user_id": digits_str(world, "uid", person.id, digits=16) if internal else "",
                "registrant_id": "",
                "user_email": person.email if (internal or guest_emails) else "",
                "join_time": isoformat_z(pt.joined),
                "leave_time": isoformat_z(pt.left),
                "duration": int((pt.left - pt.joined).total_seconds()),
                "failover": False,
                "status": "in_meeting",
                "internal_user": internal,
                "_person_id": person.id,
                "_device": pt.device,
                "_role": pt.role,
            }
        )
    return rows


def seed_instance(ctx: ProviderContext, meeting: dict[str, Any], m: Meeting, uuid: str) -> None:
    world = ctx.world
    host = world.person(m.organizer_id)
    parts = m.participations
    start = min((p.joined for p in parts), default=m.start)
    end = max((p.left for p in parts), default=m.end)
    rows = participant_rows(ctx, uuid, parts)
    seconds = (end - start).total_seconds()
    emails = {r["name"]: r["user_email"] for r in rows}
    offset = 5
    rec_start = start + timedelta(seconds=offset)
    turns = []
    for turn in m.transcript:
        speaker = world.person(turn.speaker_id)
        abs_start = (m.start + timedelta(seconds=turn.offset_s) - rec_start).total_seconds()
        turns.append((max(0.0, abs_start), speaker.display_name, turn.text))
    cues = rec_files.cues_to_records(rec_files.make_cues(turns), emails) if m.transcribed else []
    chat = [
        {
            "offset_s": max(0.0, (c.created - rec_start).total_seconds()),
            "name": world.person(c.author_id).display_name,
            "email": world.person(c.author_id).email,
            "time": isoformat_z(c.created),
            "text": c.text,
        }
        for c in m.chat
    ]
    instance = {
        "id": uuid,
        "meeting_id": meeting["id"],
        "host_id": meeting["host_id"],
        "host_email": host.email,
        "topic": meeting["topic"],
        "type": meeting["type"],
        "start_time": isoformat_z(start),
        "end_time": isoformat_z(end),
        "duration": max(1, math.ceil(seconds / 60)),
        "total_minutes": sum(max(1, math.ceil(r["duration"] / 60)) for r in rows),
        "participants_count": len(rows),
        "timezone": meeting["timezone"],
        "user_name": host.display_name,
        "user_email": host.email,
        "dept": world.department(host.department_id).name if host.department_id else "",
        "source": "Zoom",
        "state": "ended",
        "occurrence_id": str(int(m.start.timestamp() * 1000)),
        "participants": rows,
        "chat": chat,
        "transcript": cues,
        "recorded": m.recorded,
        "transcribed": m.transcribed,
        "recording_offset_s": offset,
        "_world_meeting_id": m.id,
    }
    ctx.store.collection("instances").put(instance)
    if m.recorded:
        ctx.store.collection("recordings").put(rec_files.build_recording(ctx, instance))
