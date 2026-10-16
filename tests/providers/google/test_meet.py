"""Meet v2 discovery, artifact traversal, active conferences and access checks."""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest

from emulators.core.clock import isoformat_z
from emulators.providers.google.common import conference_record_name, meet_code, meet_space_name
from emulators.testing.helpers import bearer, mint

BASE = "https://meet.googleapis.com"


@pytest.fixture
async def meet_client(make_client: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client("google") as client:
        ctx = client.hub.context("google")  # type: ignore[attr-defined]
        meeting = next(m for m in ctx.world.meetings if m.platform == "meet" and m.recorded and m.transcribed)
        client.headers.update(bearer(await mint(client, BASE, user=meeting.organizer_id)))
        yield client


def selected(client: httpx.AsyncClient) -> Any:
    ctx = client.hub.context("google")  # type: ignore[attr-defined]
    return next(m for m in ctx.world.meetings if m.platform == "meet" and m.recorded and m.transcribed)


async def test_parlance_conference_artifact_traversal(meet_client: httpx.AsyncClient) -> None:
    client = meet_client
    meeting = selected(client)
    ctx = client.hub.context("google")  # type: ignore[attr-defined]
    query = f'start_time>="{isoformat_z(meeting.start)}" AND end_time<="{isoformat_z(meeting.end)}"'
    response = await client.get(BASE + "/v2/conferenceRecords", params={"filter": query, "pageSize": 15})
    assert response.status_code == 200, response.text
    conf = next(
        r
        for r in response.json()["conferenceRecords"]
        if r["name"] == conference_record_name(ctx.world, meeting.id)
    )
    assert (await client.get(BASE + "/v2/" + conf["name"])).json() == conf
    assert conf["expireTime"] == isoformat_z(meeting.start + timedelta(days=30))
    artifacts = {}
    for area in ("transcripts", "recordings", "participants"):
        result = await client.get(BASE + "/v2/" + conf["name"] + "/" + area)
        assert result.status_code == 200, result.text
        artifacts[area] = result.json()[area]
        item = artifacts[area][0]
        assert (await client.get(BASE + "/v2/" + item["name"])).json() == item
    assert all(
        r["state"] == "FILE_GENERATED" for area in ("transcripts", "recordings") for r in artifacts[area]
    )
    transcript = artifacts["transcripts"][0]
    entries = (
        await client.get(BASE + "/v2/" + transcript["name"] + "/entries", params={"pageSize": 2})
    ).json()
    assert len(entries["transcriptEntries"]) == 2
    entry_rows = entries["transcriptEntries"]
    entry = entry_rows[0]
    assert entry["text"] == meeting.transcript[0].text and entry["languageCode"] == "en-US"
    assert (await client.get(BASE + "/v2/" + entry["name"])).json() == entry
    participants = {r["name"] for r in artifacts["participants"]}
    assert entry["participant"] in participants
    participant = artifacts["participants"][0]
    sessions = (await client.get(BASE + "/v2/" + participant["name"] + "/participantSessions")).json()[
        "participantSessions"
    ]
    assert sessions and (await client.get(BASE + "/v2/" + sessions[0]["name"])).json() == sessions[0]
    assert "nextPageToken" in entries


async def test_recording_drive_download_matches_cross_module_id(meet_client: httpx.AsyncClient) -> None:
    if importlib.util.find_spec("emulators.providers.google.drive") is None:
        pytest.skip(
            "Google part A drive module has not landed yet; Meet artifact traversal is covered separately"
        )
    client = meet_client
    meeting = selected(client)
    ctx = client.hub.context("google")  # type: ignore[attr-defined]
    conf_name = conference_record_name(ctx.world, meeting.id)
    recording = (await client.get(BASE + "/v2/" + conf_name + "/recordings")).json()["recordings"][0]
    file_id = recording["driveDestination"]["file"]
    response = await client.get(
        "https://www.googleapis.com/drive/v3/files/" + file_id, params={"alt": "media"}
    )
    assert response.status_code == 200, response.text[:500]
    assert b"ftyp" in response.content[:32] and response.headers["content-type"].startswith("video/mp4")


async def test_meet_permissions_app_only_and_new_error_style(meet_client: httpx.AsyncClient) -> None:
    client = meet_client
    meeting = selected(client)
    ctx = client.hub.context("google")  # type: ignore[attr-defined]
    name = conference_record_name(ctx.world, meeting.id)
    unrelated = next(p for p in ctx.world.employees if p.active and p.id not in meeting.all_attendee_ids())
    denied_headers = bearer(await mint(client, BASE, user=unrelated.id))
    response = await client.get(BASE + "/v2/" + name, headers=denied_headers)
    assert response.status_code == 403
    assert set(response.json()["error"]) == {"code", "message", "status"}
    visible = (
        (await client.get(BASE + "/v2/conferenceRecords", headers=denied_headers))
        .json()
        .get("conferenceRecords", [])
    )
    assert name not in {r["name"] for r in visible}
    app_only = bearer(await mint(client, BASE, app_only=True))
    assert (await client.get(BASE + "/v2/conferenceRecords", headers=app_only)).status_code == 403
    invalid = await client.get(BASE + "/v2/conferenceRecords", headers={"Authorization": "Bearer invalid"})
    assert invalid.status_code == 401 and "errors" not in invalid.json()["error"]
    attendee = next(p for p in meeting.attendee_ids if not ctx.world.person(p).is_external)
    assert (
        await client.get(BASE + "/v2/" + name, headers=bearer(await mint(client, BASE, user=attendee)))
    ).status_code == 200


async def test_space_create_get_patch_and_seed_meeting_code(meet_client: httpx.AsyncClient) -> None:
    client = meet_client
    meeting = selected(client)
    ctx = client.hub.context("google")  # type: ignore[attr-defined]
    seed_space = (await client.get(BASE + "/v2/" + meet_space_name(ctx.world, meeting.id))).json()
    assert seed_space["meetingCode"] == meet_code(ctx.world, meeting.id)
    assert (await client.get(BASE + "/v2/spaces/" + seed_space["meetingCode"])).json() == seed_space
    created = (
        await client.post(
            BASE + "/v2/spaces",
            json={"config": {"accessType": "RESTRICTED", "entryPointAccess": "CREATOR_APP_ONLY"}},
        )
    ).json()
    assert created["meetingUri"] == "https://meet.google.com/" + created["meetingCode"]
    changed = await client.patch(
        BASE + "/v2/" + created["name"],
        params={"updateMask": "config.access_type"},
        json={"config": {"accessType": "OPEN"}},
    )
    assert changed.status_code == 200 and changed.json()["config"]["accessType"] == "OPEN"
    assert changed.json()["config"]["entryPointAccess"] == "CREATOR_APP_ONLY"
    assert (await client.patch(BASE + "/v2/" + created["name"], json={"config": {}})).status_code == 400
    assert (
        await client.post(BASE + "/v2/spaces", json={"config": {"accessType": "INVALID"}})
    ).status_code == 400


async def test_filter_grammar_paging_and_active_conference_actions(meet_client: httpx.AsyncClient) -> None:
    client = meet_client
    meeting = selected(client)
    ctx = client.hub.context("google")  # type: ignore[attr-defined]
    query = f'(space.name = "{meet_space_name(ctx.world, meeting.id)}" OR space.meeting_code = "missing-code") AND start_time >= "2020-01-01T00:00:00Z"'
    result = (await client.get(BASE + "/v2/conferenceRecords", params={"filter": query})).json()
    assert [r["name"] for r in result["conferenceRecords"]] == [conference_record_name(ctx.world, meeting.id)]
    first = (await client.get(BASE + "/v2/conferenceRecords", params={"pageSize": 1})).json()
    assert first["nextPageToken"]
    second = (
        await client.get(
            BASE + "/v2/conferenceRecords", params={"pageSize": 1, "pageToken": first["nextPageToken"]}
        )
    ).json()
    assert first["conferenceRecords"][0]["name"] != second["conferenceRecords"][0]["name"]
    started = await client.post(BASE + "/_emulator/actions/start_conference", json={"meeting_id": meeting.id})
    assert started.status_code == 200 and "endTime" not in started.json()
    active = (await client.get(BASE + "/v2/conferenceRecords", params={"filter": "end_time IS NULL"})).json()[
        "conferenceRecords"
    ]
    assert started.json()["name"] in {r["name"] for r in active}
    conf = BASE + "/v2/" + started.json()["name"]
    participants = (
        await client.get(conf + "/participants", params={"filter": "latest_end_time IS NULL"})
    ).json()["participants"]
    assert participants and all("latestEndTime" not in r for r in participants)
    recordings = (await client.get(conf + "/recordings")).json()["recordings"]
    assert recordings[0]["state"] == "STARTED" and "driveDestination" not in recordings[0]
    ended = await client.post(
        BASE + "/v2/" + meet_space_name(ctx.world, meeting.id) + ":endActiveConference", json={}
    )
    assert ended.status_code == 200 and ended.json() == {}
    assert (await client.get(conf + "/recordings")).json()["recordings"][0]["state"] == "FILE_GENERATED"
    assert (
        "activeConference"
        not in (await client.get(BASE + "/v2/" + meet_space_name(ctx.world, meeting.id))).json()
    )
    assert (
        await client.post(BASE + "/_emulator/actions/end_conference", json={"meeting_id": meeting.id})
    ).status_code == 400


@pytest.mark.parametrize(
    "query",
    [
        'unknown = "x"',
        'start_time >= "invalid"',
        "space.name = unquoted",
        "end_time IS NOT NULL",
        'start_time >= "2026-10-01T00:00:00Z" OR',
    ],
)
async def test_invalid_meet_filter_returns_new_style_400(meet_client: httpx.AsyncClient, query: str) -> None:
    response = await meet_client.get(BASE + "/v2/conferenceRecords", params={"filter": query})
    assert response.status_code == 400 and "errors" not in response.json()["error"]


async def test_google_prefix_links_use_local_gateway(make_hub: Any) -> None:
    hub = make_hub("google")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hub.gateway), base_url="http://localhost:44080"
    ) as client:
        token = await mint(client, "/google", user="p001")
        response = await client.post("/google/v2/spaces", headers=bearer(token), json={})
        assert response.json()["meetingUri"].startswith("http://localhost:44080/google/")


async def test_meet_explicit_scopes_and_framework_errors(meet_client: httpx.AsyncClient) -> None:
    client = meet_client
    meeting = selected(client)
    readonly = bearer(
        await mint(
            client,
            BASE,
            user=meeting.organizer_id,
            scopes=["https://www.googleapis.com/auth/meetings.space.readonly"],
        )
    )
    assert (await client.get(BASE + "/v2/conferenceRecords", headers=readonly)).status_code == 200
    assert (await client.post(BASE + "/v2/spaces", headers=readonly, json={})).status_code == 403
    malformed = await client.post(
        BASE + "/v2/spaces", content="{", headers={"Content-Type": "application/json"}
    )
    assert malformed.status_code == 400 and "errors" not in malformed.json()["error"]
    missing = await client.get(BASE + "/v2/missing/resource")
    assert missing.status_code == 404 and "errors" not in missing.json()["error"]


async def test_future_conference_generates_artifacts_and_active_sessions(
    meet_client: httpx.AsyncClient,
) -> None:
    client = meet_client
    ctx = client.hub.context("google")  # type: ignore[attr-defined]
    meeting = next(m for m in ctx.world.meetings if m.platform == "meet" and not m.happened)
    auth = bearer(await mint(client, BASE, user=meeting.organizer_id))
    started = (
        await client.post(BASE + "/_emulator/actions/start_conference", json={"meeting_id": meeting.id})
    ).json()
    conf = BASE + "/v2/" + started["name"]
    participant = (await client.get(conf + "/participants", headers=auth)).json()["participants"][0]
    sessions = await client.get(
        BASE + "/v2/" + participant["name"] + "/participantSessions",
        params={"filter": "end_time IS NULL"},
        headers=auth,
    )
    assert sessions.status_code == 200 and sessions.json()["participantSessions"]
    assert (
        await client.post(BASE + "/_emulator/actions/end_conference", json={"meeting_id": meeting.id})
    ).status_code == 200
    for area in ("recordings", "transcripts"):
        assert (await client.get(conf + "/" + area, headers=auth)).json()[area][0][
            "state"
        ] == "FILE_GENERATED"
