"""Project world identities, meeting occurrences and team channels into Webex."""

from __future__ import annotations

from datetime import timedelta
from math import ceil
from typing import Any

from emulators.core.clock import parse_instant
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.world.model import Meeting

from .common import PERSONAL_SCOPES, SERVICE_SCOPES, SITE, opaque, stamp


def org_id(ctx: ProviderContext) -> str:
    return opaque("ORGANIZATION", ctx.world.org.domain)


def meeting_shape(
    ctx: ProviderContext,
    key: str,
    host: dict[str, Any],
    title: str,
    start: str,
    end: str,
    timezone: str,
    **fields: Any,
) -> dict[str, Any]:
    number = str(25520000000 + int(stable_hash(key, length=7), 16) % 10000000)
    return {
        "id": key,
        "meetingNumber": number,
        "title": title,
        "agenda": "",
        "password": "Halcyon2026!",
        "phoneAndVideoSystemPassword": "42692626",
        "meetingType": "scheduledMeeting",
        "state": "ready",
        "timezone": timezone,
        "start": start,
        "end": end,
        "hostUserId": host["id"],
        "hostDisplayName": host["displayName"],
        "hostEmail": host["emails"][0],
        "siteUrl": SITE,
        "webLink": f"https://{SITE}/halcyonadvisory/j.php?MTID=m{key.split('_I_')[0]}",
        "sipAddress": f"{number}@halcyonadvisory.webex.com",
        "dialInIpAddress": "192.0.2.10",
        "enabledAutoRecordMeeting": True,
        "telephony": {
            "accessCode": number,
            "callInNumbers": [{"label": "Canada Toll", "callInNumber": "+1-416-555-0100", "type": "toll"}],
            "links": [{"rel": "globalCallinNumbers", "href": f"https://{SITE}/globalcallin.php"}],
        },
        "_people": [host["id"]],
        **fields,
    }


def add_artifacts(ctx: ProviderContext, meeting: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Artifact ids follow the occurrence so ending twice cannot duplicate an import."""
    key = meeting["id"]
    recording_id = stable_hash("webex", "RECORDING", key, length=32)
    transcript_id = stable_hash("webex", "TRANSCRIPT", key, length=32)
    recording = ctx.store.collection("recordings").get(recording_id)
    transcript = ctx.store.collection("transcripts").get(transcript_id)
    start, end = parse_instant(meeting["start"]), parse_instant(meeting["end"])
    if recording is None:
        recording = ctx.store.collection("recordings").put(
            {
                "id": recording_id,
                "meetingId": key,
                "scheduledMeetingId": meeting.get("_scheduledId", key.split("_I_")[0]),
                "meetingSeriesId": meeting.get("_seriesId", key.split("_I_")[0]),
                "topic": meeting["title"],
                "createTime": stamp(end + timedelta(minutes=5)),
                "timeRecorded": stamp(start),
                "siteUrl": SITE,
                "password": "Halcyon2026!",
                "format": "MP4",
                "serviceType": "MeetingCenter",
                "durationSeconds": max(1, int((end - start).total_seconds())),
                # The shared ISO-BMFF writer uses 4-byte AAC frames and a fixed envelope.
                "sizeBytes": 640
                + (85 + len(meeting["title"].encode()) if meeting["title"] else 0)
                + 4 * ceil(max(1, int((end - start).total_seconds())) * 16000 / 1024),
                "shareToMe": False,
                "integrationTags": [],
                "status": "available",
            }
        )
    if transcript is None:
        transcript = ctx.store.collection("transcripts").put(
            {
                "id": transcript_id,
                "siteUrl": SITE,
                "startTime": meeting["start"],
                "meetingTopic": meeting["title"],
                "meetingId": key,
                "scheduledMeetingId": recording["scheduledMeetingId"],
                "meetingSeriesId": recording["meetingSeriesId"],
                "hostUserId": meeting["hostUserId"],
                "status": "available",
            }
        )
    return recording, transcript


def seed_meeting(ctx: ProviderContext, world: Meeting) -> None:
    people = ctx.store.collection("people")
    host = people.get(opaque("PEOPLE", world.organizer_id))
    assert host is not None
    series_id = stable_hash("webex", world.series_id or world.id, length=32)
    scheduled_id = f"{series_id}_{world.start.strftime('%Y%m%dT%H%M%SZ')}"
    shape = meeting_shape(
        ctx,
        scheduled_id,
        host,
        world.title,
        stamp(world.start),
        stamp(world.end),
        world.timezone,
        agenda=world.description,
        state="ended" if world.happened else "scheduled",
        meetingSeriesId=series_id,
        _people=[opaque("PEOPLE", p) for p in world.all_attendee_ids()],
        _worldId=world.id,
        _scheduledId=scheduled_id,
        _seriesId=series_id,
        _turns=[
            {
                "speaker": opaque("PEOPLE", t.speaker_id),
                "offset": t.offset_s,
                "duration": t.duration_s,
                "text": t.text,
            }
            for t in world.transcript
        ],
    )
    # Even a non-recurring meeting has a series and a dated scheduled occurrence in Webex.
    series = ctx.store.collection("meetings").get(series_id)
    if series is None:
        series = ctx.store.collection("meetings").put(
            {**shape, "id": series_id, "meetingType": "meetingSeries", "state": "expired"}
        )
        series.pop("meetingSeriesId", None)
        if world.series_id:
            series["recurrence"] = "FREQ=WEEKLY;INTERVAL=2"
    series["_people"] = sorted(set(series["_people"]) | set(shape["_people"]))
    if not world.happened:
        series.update(state="active", start=shape["start"], end=shape["end"])
    ctx.store.collection("meetings").put(shape)
    if world.happened:
        shape["_endedInstance"] = f"{series_id}_I_{int(world.start.timestamp() * 1000)}"
        shape = {
            **shape,
            "id": shape["_endedInstance"],
            "meetingType": "meeting",
            "state": "ended",
            "scheduledMeetingId": scheduled_id,
        }
        ctx.store.collection("meetings").put(shape)
    for pid in world.attendee_ids + world.optional_ids:
        p = people.get(opaque("PEOPLE", pid))
        assert p is not None
        ctx.store.collection("invitees").put(
            {
                "id": opaque("MEETING_INVITEE", f"{scheduled_id}/{pid}"),
                "meetingId": scheduled_id,
                "email": p["emails"][0],
                "displayName": p["displayName"],
                "coHost": False,
            }
        )
    for participation in world.participations:
        p = people.get(opaque("PEOPLE", participation.person_id))
        assert p is not None
        ctx.store.collection("participants").put(
            {
                "id": opaque("MEETING_PARTICIPANT", f"{shape['id']}/{participation.person_id}"),
                "meetingId": shape["id"],
                "personId": p["id"],
                "orgId": p["orgId"],
                "host": participation.person_id == world.organizer_id,
                "coHost": participation.role == "presenter",
                "email": p["emails"][0],
                "displayName": p["displayName"],
                "invitee": True,
                "video": participation.device != "phone",
                "muted": False,
                "state": "end",
                "joinedTime": stamp(participation.joined),
                "leftTime": stamp(participation.left),
                "devices": [
                    {
                        "correlationId": opaque("DEVICE", f"{world.id}/{p['id']}"),
                        "deviceType": participation.device,
                        "joinedTime": stamp(participation.joined),
                        "leftTime": stamp(participation.left),
                        "durationSecond": int((participation.left - participation.joined).total_seconds()),
                        "audioType": "pstn" if participation.device == "phone" else "voip",
                    }
                ],
            }
        )
    if world.happened and world.recorded:
        add_artifacts(ctx, shape)


def seed(ctx: ProviderContext) -> None:
    ctx.tokens.register_client("webex-emulator", "webex-emulator-secret", name="Halcyon Notes")
    org = org_id(ctx)
    created = stamp(ctx.world.anchor - timedelta(days=365))
    role_id = opaque("ROLE", "full-admin")
    license_id = opaque("LICENSE", "meetings")
    ctx.store.collection("organizations").put(
        {"id": org, "displayName": ctx.world.org.name, "created": created}
    )
    ctx.store.collection("roles").put({"id": role_id, "name": "Full administrator"})
    ctx.store.collection("licenses").put(
        {
            "id": license_id,
            "name": "Webex Meetings",
            "totalUnits": 100,
            "consumedUnits": len(ctx.world.active_employees),
        }
    )
    for p in ctx.world.people:
        record = {
            "id": opaque("PEOPLE", p.id),
            "emails": [p.email],
            "phoneNumbers": [
                {"type": kind, "value": phone}
                for kind, phone in (("work", p.phone_business), ("mobile", p.phone_mobile))
                if phone
            ],
            "displayName": p.display_name,
            "nickName": p.first_name,
            "firstName": p.first_name,
            "lastName": p.last_name,
            "orgId": opaque("ORGANIZATION", p.company_id) if p.is_external else org,
            "roles": [role_id] if p.is_admin else [],
            "licenses": [] if p.is_external or not p.active else [license_id],
            "created": created,
            "lastActivity": stamp(ctx.world.anchor - timedelta(hours=2)),
            "status": "inactive",
            "type": "person",
            "siteUrls": [] if p.is_external else [f"{SITE}#attendee"],
            "timezone": p.timezone,
            "department": ctx.world.department(p.department_id).name if p.department_id else "",
            "title": p.job_title,
            "_worldId": p.id,
            "_admin": p.is_admin,
            "_external": p.is_external,
            "_loginEnabled": p.active,
        }
        if p.manager_id:
            record.update(
                manager=ctx.world.person(p.manager_id).display_name, managerId=opaque("PEOPLE", p.manager_id)
            )
        ctx.store.collection("people").put(record)
    bot = {
        "id": opaque("PEOPLE", "notes-bot"),
        "emails": ["halcyon-notes-bot@halcyonadvisory.example"],
        "displayName": "Halcyon Notes Bot",
        "type": "bot",
        "orgId": org,
        "status": "active",
        "created": created,
        "roles": [],
        "licenses": [],
        "_worldId": "notes-bot",
        "_admin": False,
    }
    ctx.store.collection("people").put(bot)
    for token, user, scopes in [
        ("webex-host-token", "p005", PERSONAL_SCOPES),
        ("webex-admin-token", "p035", PERSONAL_SCOPES + SERVICE_SCOPES),
        ("webex-bot-token", "notes-bot", ["spark:all"]),
    ]:
        ctx.tokens.issue_access(
            client_id="webex-emulator", user_id=opaque("PEOPLE", user), scopes=scopes, ttl=None, token=token
        )
    clients = {
        c.id
        for c in ctx.world.clients
        if c.name in {"Northwind Logistics", "Atlas Freight Partners", "Meridian Legal"}
    }
    for meeting in ctx.world.meetings:
        if (
            meeting.client_id in clients
            and not meeting.cancelled
            and meeting.start >= ctx.world.anchor - timedelta(weeks=6)
        ):
            seed_meeting(ctx, meeting)
    for series in ctx.store.collection("meetings"):
        if series["meetingType"] != "meetingSeries":
            continue
        upcoming = [
            m
            for m in ctx.store.collection("meetings")
            if m["meetingType"] == "scheduledMeeting"
            and m["_seriesId"] == series["id"]
            and m["state"] == "scheduled"
        ]
        if upcoming:
            first = min(upcoming, key=lambda m: m["start"])
            first["state"] = "ready"
            series.update(start=first["start"], end=first["end"], _scheduledId=first["id"])
    for team in ctx.world.teams:
        channel = next(c for c in team.channels if c.name.lower() == "general")
        room = {
            "id": opaque("ROOM", channel.id),
            "title": f"{team.name} - General",
            "type": "group",
            "isLocked": team.visibility == "Private",
            "teamId": opaque("TEAM", team.id),
            "lastActivity": stamp(max((m.created for m in channel.messages), default=ctx.world.anchor)),
            "creatorId": opaque("PEOPLE", team.owner_ids[0]),
            "created": created,
        }
        ctx.store.collection("rooms").put(room)
        for pid in team.member_ids:
            p = ctx.store.collection("people").get(opaque("PEOPLE", pid))
            assert p is not None
            ctx.store.collection("memberships").put(
                {
                    "id": opaque("MEMBERSHIP", f"{room['id']}/{p['id']}"),
                    "roomId": room["id"],
                    "personId": p["id"],
                    "personEmail": p["emails"][0],
                    "personDisplayName": p["displayName"],
                    "isModerator": pid in team.owner_ids,
                    "isMonitor": False,
                    "created": created,
                }
            )
        for message in channel.messages:
            p = ctx.store.collection("people").get(opaque("PEOPLE", message.author_id))
            assert p is not None
            record = {
                "id": opaque("MESSAGE", message.id),
                "roomId": room["id"],
                "roomType": "group",
                "personId": p["id"],
                "personEmail": p["emails"][0],
                "text": message.text,
                "created": stamp(message.created),
            }
            if message.mentions:
                record["mentionedPeople"] = [opaque("PEOPLE", pid) for pid in message.mentions]
            if message.reply_to_id:
                record["parentId"] = opaque("MESSAGE", message.reply_to_id)
            ctx.store.collection("messages").put(record)
