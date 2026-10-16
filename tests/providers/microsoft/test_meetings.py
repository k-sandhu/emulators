from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.clock import isoformat_z
from emulators.providers.microsoft import common
from emulators.providers.microsoft import drive_model as dm
from emulators.providers.microsoft import meetings_model as mm
from emulators.testing.helpers import bearer, mint
from emulators.world import documents, media

BASE = "https://graph.microsoft.com"


@pytest.mark.parametrize("version", ["v1.0", "beta"])
async def test_parlance_lookup_transcripts_recordings_end_to_end(
    make_client: Callable[..., Any], version: str
) -> None:
    async with make_client("microsoft") as client:
        source = next(
            m
            for m in client.hub.world.meetings
            if m.platform == "teams" and m.recorded and m.transcribed and m.happened and not m.cancelled
        )
        auth = bearer(await mint(client, BASE, user=source.organizer_id))
        base = BASE + "/" + version
        lookup = await client.get(
            base + "/me/onlineMeetings",
            headers=auth,
            params={"$filter": "JoinWebUrl eq '" + common.join_web_url(client.hub.world, source) + "'"},
        )
        assert lookup.status_code == 200, lookup.text
        meeting = lookup.json()["value"][0]
        assert meeting["id"] == common.online_meeting_id(client.hub.world, source.id, source.organizer_id)
        assert meeting["joinWebUrl"] == common.join_web_url(client.hub.world, source)
        url = base + "/me/onlineMeetings/" + meeting["id"]
        assert (await client.get(url, headers=auth)).json()["subject"] == source.title
        transcripts = (await client.get(url + "/transcripts", headers=auth)).json()["value"]
        recordings = (await client.get(url + "/recordings", headers=auth)).json()["value"]
        assert len(transcripts) == len(recordings) == 1
        transcript, recording = transcripts[0], recordings[0]
        assert transcript["meetingId"] == recording["meetingId"] == meeting["id"]
        assert transcript["contentCorrelationId"] == recording["contentCorrelationId"]
        t_url = url + "/transcripts/" + transcript["id"]
        r_url = url + "/recordings/" + recording["id"]
        assert (await client.get(t_url, headers=auth)).json()["id"] == transcript["id"]
        assert (await client.get(r_url, headers=auth)).json()["id"] == recording["id"]
        default = await client.get(t_url + "/content", headers=auth)
        formatted = await client.get(t_url + "/content?$format=text/vtt", headers=auth)
        assert default.content == formatted.content
        assert default.headers["content-type"].startswith("text/vtt")
        assert default.content.startswith(b"WEBVTT") and b"<v " in default.content
        assert re.search(rb"[0-9a-f]{8}-[0-9a-f-]{27}/1-0", default.content)
        cues = media.parse_vtt(default.content)
        assert cues[0].speaker == client.hub.world.person(source.transcript[0].speaker_id).display_name
        assert cues[0].text == source.transcript[0].text
        docx = await client.get(t_url + "/content", headers=auth, params={"$format": documents.MIME_DOCX})
        with zipfile.ZipFile(io.BytesIO(docx.content)) as archive:
            assert "word/document.xml" in archive.namelist()
        metadata = await client.get(t_url + "/metadataContent", headers=auth)
        first_metadata = json.loads(metadata.text.split("\n\n")[1].splitlines()[1])
        assert first_metadata["speakerName"] == cues[0].speaker
        assert first_metadata["spokenText"] == cues[0].text
        plain = await client.get(
            t_url + "/content", headers={**auth, "Accept": "application/vnd.microsoft.graph.transcript+text"}
        )
        assert b"<v " not in plain.content
        video = await client.get(r_url + "/content", headers=auth)
        assert video.status_code == 200 and video.content[4:8] == b"ftyp"
        assert video.headers["content-type"] == "video/mp4"
        ranged = await client.get(r_url + "/content", headers={**auth, "Range": "bytes=0-31"})
        assert ranged.status_code == 206 and ranged.content == video.content[:32]
        assert (await client.get(transcript["transcriptContentUrl"], headers=auth)).content == default.content
        assert (await client.get(recording["recordingContentUrl"], headers=auth)).content == video.content
        drive = (await client.get(base + "/me/drive/root:/Recordings:/children", headers=auth)).json()[
            "value"
        ]
        filename = re.sub(r'["*:<>?/\\|]', "_", source.title) + source.start.strftime(
            "-%Y%m%d_%H%M%S-Meeting Recording.mp4"
        )
        drive_recording = next(i for i in drive if i["name"] == filename)
        assert (await client.get(drive_recording["@microsoft.graph.downloadUrl"])).content == video.content


async def test_meeting_access_attendance_call_records_and_bulk_exports(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft") as client:
        ctx = client.hub.context("microsoft")
        source = next(
            m
            for m in ctx.world.meetings
            if m.platform == "teams" and m.transcribed and m.recorded and m.happened and not m.cancelled
        )
        mid = common.online_meeting_id(ctx.world, source.id, source.organizer_id)
        auth = bearer(await mint(client, BASE, user=source.organizer_id))
        attendee = bearer(await mint(client, BASE, user=source.attendee_ids[0]))
        outsider = next(p for p in ctx.world.active_employees if p.id not in source.all_attendee_ids())
        other = bearer(await mint(client, BASE, user=outsider.id))
        url = BASE + "/v1.0/me/onlineMeetings/" + mid
        assert (await client.get(url, headers=attendee)).status_code == 200
        assert (await client.get(url, headers=other)).status_code == 404
        assert (await client.get(url + "/transcripts", headers=other)).status_code == 404
        assert (await client.patch(url, headers=attendee, json={"subject": "Denied"})).status_code == 403
        assert (await client.get(BASE + "/v1.0/me/onlineMeetings", headers=auth)).status_code == 400
        reports = (await client.get(url + "/attendanceReports", headers=auth)).json()["value"]
        report_url = url + "/attendanceReports/" + reports[0]["id"]
        expanded = (await client.get(report_url + "?$expand=attendanceRecords", headers=auth)).json()
        assert len(expanded["attendanceRecords"]) == reports[0]["totalParticipantCount"]
        records = (await client.get(report_url + "/attendanceRecords", headers=auth)).json()["value"]
        assert records[0]["totalAttendanceInSeconds"] == sum(
            i["durationInSeconds"] for i in records[0]["attendanceIntervals"]
        )
        assert records[0]["emailAddress"].endswith(".example")
        call_id = (await client.get(url + "/transcripts", headers=auth)).json()["value"][0]["callId"]
        call_url = BASE + "/v1.0/communications/callRecords/" + call_id
        assert (await client.get(call_url, headers=auth)).status_code == 403
        app = bearer(await mint(client, BASE, app_only=True))
        call = (await client.get(call_url + "?$expand=sessions($expand=segments)", headers=app)).json()
        assert call["type"] == "groupCall" and call["version"] == 1
        assert call["participants_v2"] and call["sessions"][0]["segments"]
        assert (await client.get(call_url + "/sessions", headers=app)).json()["value"]
        uid = common.user_id(ctx.world, source.organizer_id)
        for kind in ("Transcripts", "Recordings"):
            bulk = (
                BASE
                + f"/v1.0/users/{uid}/onlineMeetings/getAll{kind}(meetingOrganizerUserId='{uid}',startDateTime=2026-09-01T00:00:00Z,endDateTime=2026-11-30T00:00:00Z)"
            )
            assert (await client.get(bulk, headers=auth)).status_code == 403
            exported = await client.get(bulk, headers=app)
            assert exported.status_code == 200, exported.text
            assert mid in [r["meetingId"] for r in exported.json()["value"]]
        assert (
            await client.get(BASE + f"/v1.0/users/{uid}/onlineMeetings/{mid}", headers=app)
        ).status_code == 200
        assert (await client.get(url, headers=app)).status_code == 400


async def test_create_update_create_or_get_end_actions_and_delete(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        url = BASE + "/beta/me/onlineMeetings"
        payload = {
            "subject": "Contract meeting",
            "startDateTime": "2026-10-16T12:00:00Z",
            "endDateTime": "2026-10-16T12:30:00Z",
            "participants": {"attendees": [{"upn": client.hub.world.person("p001").email}]},
        }
        assert (await client.post(url, headers=auth, json={"subject": "Invalid"})).status_code == 400
        response = await client.post(url, headers=auth, json=payload)
        assert response.status_code == 201, response.text
        meeting = response.json()
        assert (
            await client.patch(
                url + "/" + meeting["id"], headers=auth, json={"subject": "Updated contract meeting"}
            )
        ).json()["subject"] == "Updated contract meeting"
        assert (
            await client.patch(
                url + "/" + meeting["id"], headers=auth, json={"endDateTime": "2026-10-16T11:00:00Z"}
            )
        ).status_code == 400
        assert (await client.get(url + "/" + meeting["id"] + "/transcripts", headers=auth)).json()[
            "value"
        ] == []
        ctx = client.hub.context("microsoft")
        drive = dm.personal_drive(ctx, "p018")
        app = bearer(await mint(client, BASE, app_only=True))
        baseline = (
            await client.get(BASE + f"/beta/drives/{drive['id']}/root/delta?token=latest", headers=app)
        ).json()
        ended = await client.post(
            BASE + "/_emulator/actions/end_meeting",
            json={
                "meeting_id": meeting["id"],
                "turns": [
                    {
                        "speaker_id": "p018",
                        "text": "The engagement is complete.",
                        "offset_s": 0,
                        "duration_s": 3,
                    }
                ],
            },
        )
        assert ended.status_code == 200, ended.text
        assert ended.json()["status"] == "ended"
        assert (
            await client.post(BASE + "/_emulator/actions/end_meeting", json={"meeting_id": meeting["id"]})
        ).status_code == 200
        transcripts = (await client.get(url + "/" + meeting["id"] + "/transcripts", headers=auth)).json()[
            "value"
        ]
        recordings = (await client.get(url + "/" + meeting["id"] + "/recordings", headers=auth)).json()[
            "value"
        ]
        assert len(transcripts) == len(recordings) == 1
        assert any(
            r["name"].endswith("Meeting Recording.mp4")
            for r in (await client.get(baseline["@odata.deltaLink"], headers=app)).json()["value"]
        )
        added = await client.post(
            BASE + "/_emulator/actions/add_transcript",
            json={"meeting_id": meeting["id"], "text": "Additional transcript segment."},
        )
        assert added.status_code == 200
        assert (
            len((await client.get(url + "/" + meeting["id"] + "/transcripts", headers=auth)).json()["value"])
            == 2
        )
        by_external = {**payload, "externalId": "integration-test-meeting"}
        one = await client.post(url + "/createOrGet", headers=auth, json=by_external)
        two = await client.post(url + "/createOrGet", headers=auth, json=by_external)
        assert one.status_code == 201 and two.status_code == 200
        assert one.json()["id"] == two.json()["id"]
        assert (await client.delete(url + "/" + meeting["id"], headers=auth)).status_code == 204
        assert (await client.get(url + "/" + meeting["id"], headers=auth)).status_code == 404


async def test_seeded_artifacts_are_lazy_and_stable(make_hub: Callable[..., Any]) -> None:
    hub = make_hub("microsoft")
    ctx = hub.context("microsoft")
    transcript = next(iter(ctx.store[mm.TRANSCRIPTS]))
    assert "_vtt_blob" not in transcript
    recording = next(iter(ctx.store[mm.RECORDINGS]))
    assert "_blob" not in ctx.store[dm.ITEMS].get(recording["_drive_item"])
    ids = ctx.store[mm.MEETINGS].ids()
    hub.reset()
    assert ctx.store[mm.MEETINGS].ids() == ids


async def test_end_meeting_artifact_and_call_notifications(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        received: list[dict[str, Any]] = []
        receiver = FastAPI()

        @receiver.post("/hook")
        async def hook(request: Request) -> Response:
            if token := request.query_params.get("validationToken"):
                return Response(token, media_type="text/plain")
            received.append(await request.json())
            return Response(status_code=202)

        client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
        delegated = bearer(await mint(client, BASE, user="p018"))
        app = bearer(await mint(client, BASE, app_only=True))
        created = await client.post(
            BASE + "/v1.0/me/onlineMeetings",
            headers=delegated,
            json={
                "subject": "Notification meeting",
                "startDateTime": "2026-10-16T12:00:00Z",
                "endDateTime": "2026-10-16T12:30:00Z",
            },
        )
        assert created.status_code == 201
        mid = created.json()["id"]
        for resource in (
            f"communications/onlineMeetings/{mid}/transcripts",
            f"communications/onlineMeetings/{mid}/recordings",
            "communications/callRecords",
        ):
            subscription = await client.post(
                BASE + "/v1.0/subscriptions",
                headers=app,
                json={
                    "resource": resource,
                    "changeType": "created",
                    "notificationUrl": "https://receiver.example/hook",
                    "expirationDateTime": isoformat_z(client.hub.clock.now() + timedelta(hours=1)),
                    "clientState": "meeting-contract",
                },
            )
            assert subscription.status_code == 201, subscription.text
        ended = await client.post(BASE + "/_emulator/actions/end_meeting", json={"meeting_id": mid})
        assert ended.status_code == 200
        notifications = [body["value"][0] for body in received]
        assert len(notifications) == 3
        assert {n["resourceData"]["@odata.type"] for n in notifications} == {
            "#microsoft.graph.callTranscript",
            "#microsoft.graph.callRecording",
            "#microsoft.graph.callRecord",
        }
        assert all(n["clientState"] == "meeting-contract" for n in notifications)
