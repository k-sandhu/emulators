from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.clock import isoformat_z, parse_instant
from emulators.providers.microsoft import common
from emulators.providers.microsoft import meetings_model as mm
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"
BODY = {
    "subject": "Audit meeting",
    "startDateTime": "2026-10-16T12:00:00Z",
    "endDateTime": "2026-10-16T13:00:00Z",
}


@pytest.mark.parametrize(
    "payload",
    [
        {"allowRecording": 0},
        {"recordAutomatically": "false"},
        {"isEntryExitAnnounced": []},
        {"allowAttendeeToEnableMic": None},
        {"allowAttendeeToEnableCamera": 1},
        {"allowTranscription": {}},
        {"allowedPresenters": []},
        {"allowedPresenters": "invalid"},
        {"allowMeetingChat": False},
        {"lobbyBypassSettings": []},
        {"lobbyBypassSettings": {"scope": []}},
        {"lobbyBypassSettings": {"isDialInBypassEnabled": 0}},
        {"participants": {"attendees": [{"upn": "maya.chen@halcyonadvisory.example", "role": []}]}},
        {"participants": {"attendees": [{"upn": ["wrong"]}]}},
    ],
)
async def test_meeting_settings_invalid_types_do_not_mutate(
    make_client: Callable[..., Any], payload: dict[str, Any]
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        meeting = next(m for m in ctx.store[mm.MEETINGS] if m["_organizer"] == "p018")
        original = (await client.get("/v1.0/me/onlineMeetings/" + meeting["id"])).json()
        count = len(ctx.store[mm.MEETINGS])
        created = await client.post("/v1.0/me/onlineMeetings", json={**BODY, **payload})
        assert created.status_code == 400, created.text
        patched = await client.patch("/v1.0/me/onlineMeetings/" + meeting["id"], json=payload)
        assert patched.status_code == 400, patched.text
        assert len(ctx.store[mm.MEETINGS]) == count
        assert (await client.get("/v1.0/me/onlineMeetings/" + meeting["id"])).json() == original


@pytest.mark.parametrize("version", ["v1.0", "beta"])
async def test_meeting_join_id_lookup_presenter_roles_and_optional_export_dates(
    make_client: Callable[..., Any], version: str
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        auth = bearer(await mint(client, BASE, user="p018"))
        uid = common.user_id(ctx.world, "p018")
        attendee = common.user_id(ctx.world, "p001")
        body = {
            **BODY,
            "allowedPresenters": "roleIsPresenter",
            "participants": {"attendees": [{"identity": {"user": {"id": attendee}}, "role": "presenter"}]},
        }
        created = await client.post(f"/{version}/me/onlineMeetings", headers=auth, json=body)
        assert created.status_code == 201
        meeting = created.json()
        assert meeting["participants"]["attendees"][0]["role"] == "presenter"
        path = f"/{version}/me/onlineMeetings"
        found = await client.get(
            path,
            headers=auth,
            params={
                "$filter": f"joinMeetingIdSettings/joinMeetingId eq '{meeting['joinMeetingIdSettings']['joinMeetingId']}'"
            },
        )
        assert found.status_code == 200 and found.json()["value"][0]["id"] == meeting["id"]
        changed = await client.patch(
            path + "/" + meeting["id"],
            headers=auth,
            json={
                "participants": {
                    "attendees": [{"upn": ctx.world.person("p001").email, "role": "coorganizer"}]
                }
            },
        )
        assert changed.json()["participants"]["attendees"][0]["role"] == "coorganizer"
        assert (
            await client.post("/_emulator/actions/end_meeting", json={"meeting_id": meeting["id"]})
        ).status_code == 200
        app = bearer(await mint(client, BASE, app_only=True))
        for kind in ("Transcripts", "Recordings"):
            for date_args in ("", ",startDateTime=2026-10-01T00:00:00Z", ",endDateTime=2026-11-01T00:00:00Z"):
                export = f"/{version}/users/{uid}/onlineMeetings/getAll{kind}(meetingOrganizerUserId='{uid}'{date_args})"
                response = await client.get(export, headers=app)
                assert response.status_code == 200, response.text
                assert meeting["id"] in [r["meetingId"] for r in response.json()["value"]]
                assert (await client.get(export, headers=auth)).status_code == 403


@pytest.mark.parametrize(
    "args",
    [
        "startDateTime=invalid",
        "unexpected=true",
        "startDateTime=2026-10-16T00:00:00Z,startDateTime=2026-10-16T00:00:00Z",
        "startDateTime=2026-10-17T00:00:00Z,endDateTime=2026-10-16T00:00:00Z",
    ],
)
async def test_artifact_export_rejects_invalid_function_arguments(
    make_client: Callable[..., Any], args: str
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        uid = common.user_id(client.hub.world, "p018")
        app = bearer(await mint(client, BASE, app_only=True))
        response = await client.get(
            f"/v1.0/users/{uid}/onlineMeetings/getAllTranscripts(meetingOrganizerUserId='{uid}',{args})",
            headers=app,
        )
        assert response.status_code == 400


async def test_current_call_record_relationships_and_segment_navigation(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        client.headers.update(bearer(await mint(client, BASE, app_only=True)))
        call = next(iter(ctx.store[mm.CALLS]))
        path = "/v1.0/communications/callRecords/" + call["id"]
        response = (await client.get(path)).json()
        assert "organizer_v2" in response and "organizer" not in response
        assert "participants_v2" not in response
        assert response["lastModifiedDateTime"] >= response["endDateTime"]
        organizer = (await client.get(path + "/organizer_v2")).json()
        assert organizer["identity"]["user"]["id"] == organizer["id"]
        participants = (await client.get(path + "/participants_v2?$top=2")).json()
        assert participants["value"]
        expanded = (await client.get(path + "?$expand=participants_v2,sessions($expand=segments)")).json()
        assert expanded["participants_v2"]
        session = expanded["sessions"][0]
        assert session["caller"]["associatedIdentity"]["id"] == session["caller"]["participantId"]
        segments_path = path + f"/sessions/{session['id']}/segments"
        segments = (await client.get(segments_path)).json()["value"]
        assert segments == session["segments"]
        assert (await client.get(segments_path + "/" + segments[0]["id"])).json()["id"] == segments[0]["id"]


async def test_bulk_artifact_organizer_filter_alternative(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        source = next(iter(ctx.store[mm.TRANSCRIPTS]))
        organizer = ctx.store[mm.MEETINGS].get(source["_meeting"])["_organizer"]
        uid = common.user_id(ctx.world, organizer)
        auth = bearer(await mint(client, BASE, app_only=True))
        path = f"/v1.0/users/{uid}/onlineMeetings/getAllTranscripts()"
        response = await client.get(
            path, headers=auth, params={"$filter": f"MeetingOrganizer/User/Id eq '{uid}'"}
        )
        assert response.status_code == 200, response.text
        assert source["id"] in [r["id"] for r in response.json()["value"]]


async def test_call_record_updated_subscription_receives_initial_record(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        receiver = FastAPI()
        received: list[dict[str, Any]] = []

        @receiver.post("/hook")
        async def hook(request: Request) -> Response:
            if validation := request.query_params.get("validationToken"):
                return Response(validation, media_type="text/plain")
            received.append(await request.json())
            return Response(status_code=202)

        client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
        app = bearer(await mint(client, BASE, app_only=True))
        subscription = await client.post(
            "/v1.0/subscriptions",
            headers=app,
            json={
                "resource": "communications/callRecords",
                "changeType": "updated",
                "notificationUrl": "https://receiver.example/hook",
                "expirationDateTime": isoformat_z(client.hub.clock.now() + timedelta(hours=1)),
                "clientState": "audit-call-state",
            },
        )
        assert subscription.status_code == 201
        auth = bearer(await mint(client, BASE, user="p018"))
        meeting = (await client.post("/v1.0/me/onlineMeetings", headers=auth, json=BODY)).json()
        ended = await client.post("/_emulator/actions/end_meeting", json={"meeting_id": meeting["id"]})
        assert ended.status_code == 200
        assert len(received) == 1
        notice = received[0]["value"][0]
        assert notice["changeType"] == "updated" and notice["clientState"] == "audit-call-state"
        assert notice["resourceData"]["@odata.type"] == "#Microsoft.Graph.callRecord"
        assert "@odata.etag" not in notice["resourceData"]
        assert (await client.get("/v1.0/" + notice["resource"], headers=app)).status_code == 200


@pytest.mark.parametrize(
    "offset,duration", [(1e308, 1e308), (0, 1e308), pytest.param(0, 10**400, id="integer-overflow")]
)
async def test_transcript_action_rejects_unrenderable_timing(
    make_client: Callable[..., Any], offset: float, duration: float
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        response = await client.post(
            "/_emulator/actions/add_transcript",
            json={
                "meeting_id": "m0002",
                "turns": [
                    {"speaker_id": "p001", "text": "Timing", "offset_s": offset, "duration_s": duration}
                ],
            },
        )
        assert response.status_code == 400


async def test_subsecond_meeting_preserves_times_and_end_action(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        response = await client.post(
            "/v1.0/me/onlineMeetings",
            headers=auth,
            json={
                **BODY,
                "startDateTime": "2026-10-16T12:00:00.100000Z",
                "endDateTime": "2026-10-16T12:00:00.200000Z",
            },
        )
        assert response.status_code == 201
        meeting = response.json()
        assert meeting["startDateTime"] != meeting["endDateTime"]
        assert (
            await client.post("/_emulator/actions/end_meeting", json={"meeting_id": meeting["id"]})
        ).status_code == 200


async def test_future_seeded_meeting_was_created_before_anchor(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        meeting = next(
            m for m in ctx.store[mm.MEETINGS] if parse_instant(m["startDateTime"]) > ctx.world.anchor
        )
        auth = bearer(await mint(client, BASE, user=meeting["_organizer"]))
        response = await client.get("/v1.0/me/onlineMeetings/" + meeting["id"], headers=auth)
        assert parse_instant(response.json()["creationDateTime"]) <= ctx.world.anchor
