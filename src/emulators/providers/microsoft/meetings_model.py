"""Online meetings, attendance and artifact metadata derived from the common world."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

from fastapi import Request

from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.ids import stable_guid
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common
from emulators.providers.microsoft import drive_model as dm
from emulators.world import media

MEETINGS = "ms_online_meetings"
TRANSCRIPTS = "ms_meeting_transcripts"
RECORDINGS = "ms_meeting_recordings"
REPORTS = "ms_meeting_attendance"
CALLS = "ms_call_records"


def meeting_shape(
    ctx: ProviderContext, key: str, organizer: str, subject: str, start: str, end: str, attendees: list[str]
) -> dict[str, Any]:
    mid = common.online_meeting_id(ctx.world, key, organizer)
    person = ctx.world.person(organizer)
    source = SimpleNamespace(id=key, organizer_id=organizer)
    return {
        "id": mid,
        "creationDateTime": start,
        "startDateTime": start,
        "endDateTime": end,
        "subject": subject,
        "joinWebUrl": common.join_web_url(ctx.world, source),
        "joinMeetingIdSettings": {
            "isPasscodeRequired": True,
            "joinMeetingId": str(int(stable_guid(key).replace("-", "")[:12], 16))[:12],
            "passcode": stable_guid(key)[-6:],
        },
        "audioConferencing": None,
        "chatInfo": {
            "threadId": common.meeting_thread_id(ctx.world, key),
            "messageId": "0",
            "replyChainMessageId": None,
        },
        "participants": {
            "organizer": {
                "identity": common.identity(ctx.world, person),
                "upn": common.upn(ctx.world, person),
                "role": "presenter",
            },
            "attendees": [
                {
                    "identity": common.identity(ctx.world, ctx.world.person(pid)),
                    "upn": common.upn(ctx.world, ctx.world.person(pid)),
                    "role": "attendee",
                }
                for pid in attendees
                if pid != organizer
            ],
        },
        "isEntryExitAnnounced": True,
        "allowedPresenters": "everyone",
        "allowAttendeeToEnableMic": True,
        "allowAttendeeToEnableCamera": True,
        "allowMeetingChat": "enabled",
        "allowTeamworkReactions": True,
        "allowRecording": True,
        "allowTranscription": True,
        "recordAutomatically": False,
        "lobbyBypassSettings": {"scope": "organization", "isDialInBypassEnabled": False},
        "videoTeleconferenceId": None,
        "isBroadcast": False,
        "broadcastSettings": None,
        "capabilities": [],
        "watermarkProtection": None,
        "_key": key,
        "_organizer": organizer,
        "_attendees": list(dict.fromkeys([organizer, *attendees])),
        "_ended": False,
    }


def artifact_id(ctx: ProviderContext, mid: str, kind: str, index: int = 0) -> str:
    return (
        base64.b64encode(f"{index}##{kind}##{stable_guid(ctx.world.seed, mid, kind, index)}".encode())
        .decode()
        .rstrip("=")
    )


def artifact(ctx: ProviderContext, meeting: dict[str, Any], kind: str, index: int = 0) -> dict[str, Any]:
    return {
        "id": artifact_id(ctx, meeting["id"], kind, index),
        "meetingId": meeting["id"],
        "callId": stable_guid(ctx.world.seed, "teams-call", meeting["_key"]),
        "createdDateTime": meeting["startDateTime"],
        "endDateTime": meeting["endDateTime"],
        "contentCorrelationId": stable_guid(meeting["id"], "content", index),
        "meetingOrganizer": {
            "user": {
                "id": common.user_id(ctx.world, meeting["_organizer"]),
                "tenantId": common.tenant_id(ctx.world),
            }
        },
        "_meeting": meeting["id"],
    }


def artifact_wire(
    request: Request, ctx: ProviderContext, record: dict[str, Any], kind: str
) -> dict[str, Any]:
    result = dm.public(record)
    meeting = ctx.store[MEETINGS].get(record["_meeting"])
    path = f"/{common.version_of(request)}/users/{common.user_id(ctx.world, meeting['_organizer'])}/onlineMeetings/{dm.quote(meeting['id'], safe='')}/{kind}/{dm.quote(record['id'], safe='')}/content"
    result["transcriptContentUrl" if kind == "transcripts" else "recordingContentUrl"] = dm.graph_url(
        request, ctx, path
    )
    return result


def attendance_record(ctx: ProviderContext, pid: str, joined: str, left: str, role: str) -> dict[str, Any]:
    seconds = max(0, int((parse_instant(left) - parse_instant(joined)).total_seconds()))
    person = ctx.world.person(pid)
    return {
        "id": common.user_id(ctx.world, pid),
        "emailAddress": person.email,
        "totalAttendanceInSeconds": seconds,
        "role": role.capitalize(),
        "identity": {
            "id": common.user_id(ctx.world, pid),
            "displayName": person.display_name,
            "tenantId": common.tenant_id(ctx.world),
        },
        "attendanceIntervals": [
            {"joinDateTime": joined, "leaveDateTime": left, "durationInSeconds": seconds}
        ],
    }


def create_attendance(
    ctx: ProviderContext, meeting: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    report = ctx.store[REPORTS].put(
        {
            "id": stable_guid(meeting["id"], "attendance"),
            "meetingStartDateTime": meeting["startDateTime"],
            "meetingEndDateTime": meeting["endDateTime"],
            "totalParticipantCount": len(records),
            "_meeting": meeting["id"],
            "_records": records,
        }
    )
    call_id = stable_guid(ctx.world.seed, "teams-call", meeting["_key"])
    sessions = []
    for record in records:
        session_id = stable_guid(call_id, record["id"])
        identity = {"user": record["identity"]}
        endpoint = {
            "@odata.type": "#microsoft.graph.callRecords.participantEndpoint",
            "identity": identity,
            "userAgent": {
                "applicationVersion": "1.0",
                "headerValue": "Teams",
                "platform": "windows",
                "productFamily": "teams",
            },
        }
        sessions.append(
            {
                "id": session_id,
                "startDateTime": record["attendanceIntervals"][0]["joinDateTime"],
                "endDateTime": record["attendanceIntervals"][0]["leaveDateTime"],
                "modalities": ["audio", "video"],
                "caller": endpoint,
                "callee": {"@odata.type": "#microsoft.graph.callRecords.serviceEndpoint"},
                "failureInfo": None,
                "segments": [
                    {
                        "id": stable_guid(session_id, "segment"),
                        "startDateTime": record["attendanceIntervals"][0]["joinDateTime"],
                        "endDateTime": record["attendanceIntervals"][0]["leaveDateTime"],
                        "caller": endpoint,
                        "callee": {"@odata.type": "#microsoft.graph.callRecords.serviceEndpoint"},
                        "media": [],
                        "failureInfo": None,
                    }
                ],
            }
        )
    ctx.store[CALLS].put(
        {
            "id": call_id,
            "version": 1,
            "type": "groupCall",
            "modalities": ["audio", "video"],
            "startDateTime": meeting["startDateTime"],
            "endDateTime": meeting["endDateTime"],
            "lastModifiedDateTime": meeting["endDateTime"],
            "joinWebUrl": meeting["joinWebUrl"],
            "organizer": common.identity(ctx.world, ctx.world.person(meeting["_organizer"])),
            "participants_v2": [
                {"id": stable_guid(call_id, r["id"], "participant"), "identity": {"user": r["identity"]}}
                for r in records
            ],
            "_meeting": meeting["id"],
            "_sessions": sessions,
        }
    )
    return report


def seed(ctx: ProviderContext) -> None:
    for source in ctx.world.meetings:
        if source.platform != "teams" or source.cancelled:
            continue
        meeting = meeting_shape(
            ctx,
            source.id,
            source.organizer_id,
            source.title,
            isoformat_z(source.start),
            isoformat_z(source.end),
            [*source.attendee_ids, *source.optional_ids],
        )
        meeting["_ended"] = source.happened
        ctx.store[MEETINGS].put(meeting)
        if source.transcribed and source.happened:
            transcript = artifact(ctx, meeting, "transcript")
            transcript["_cues"] = [
                {
                    "start": turn.offset_s,
                    "end": turn.offset_s + turn.duration_s,
                    "speaker": ctx.world.person(turn.speaker_id).display_name,
                    "speaker_id": turn.speaker_id,
                    "text": turn.text,
                }
                for turn in source.transcript
            ]
            ctx.store[TRANSCRIPTS].put(transcript)
        if source.recorded and source.happened:
            recording = artifact(ctx, meeting, "recording")
            drive_item = dm.add_recording(
                ctx, source.id, source.organizer_id, source.title, source.start, source.end
            )
            recording["_drive_item"] = drive_item["id"]
            ctx.store[RECORDINGS].put(recording)
        if source.happened:
            records = [
                attendance_record(
                    ctx,
                    participation.person_id,
                    isoformat_z(participation.joined),
                    isoformat_z(participation.left),
                    participation.role,
                )
                for participation in source.participations
            ]
            create_attendance(ctx, meeting, records)


def require_meeting(
    ctx: ProviderContext, mid: str, p: common.Principal, uid: str | None = None, *, write: bool = False
) -> dict[str, Any]:
    meeting = ctx.store[MEETINGS].get(mid)
    if meeting is None or (p.delegated and p.person_id not in meeting["_attendees"]):
        raise common.item_not_found()
    if uid and uid not in meeting["_attendees"]:
        raise common.item_not_found()
    if write and p.delegated and p.person_id != meeting["_organizer"]:
        raise common.access_denied("Only the organizer can modify this meeting.")
    return meeting


def cues(record: dict[str, Any]) -> list[media.Cue]:
    return [media.Cue(c["start"], c["end"], c["speaker"], c["text"]) for c in record["_cues"]]
