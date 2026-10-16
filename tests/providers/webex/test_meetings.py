from __future__ import annotations

import re

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint

ADMIN = bearer("webex-admin-token")


async def test_seeded_series_occurrences_attendance_and_acl(
    client: httpx.AsyncClient, hub: EmulatorHub
) -> None:
    r = await client.get("/v1/meetings", params={"meetingType": "meeting", "max": 1}, headers=ADMIN)
    meetings = []
    while True:
        assert r.status_code == 200
        meetings.extend(r.json()["items"])
        if "next" not in r.links:
            break
        r = await client.get(r.links["next"]["url"], headers=ADMIN)
    assert len(meetings) == 4
    assert all(re.fullmatch(r"[a-f0-9]{32}_I_\d+", m["id"]) and m["state"] == "ended" for m in meetings)
    assert all(not k.startswith("_") for m in meetings for k in m)
    first = meetings[0]
    original = next(
        m
        for m in hub.world.meetings
        if m.start.isoformat().startswith("2026-09-09") and m.client_id == "c019"
    )
    assert first["title"] == original.title
    assert first["hostEmail"] == hub.world.person(original.organizer_id).email
    assert re.fullmatch(r"\d{4} \d{3} \d{4}", first["meetingNumber"])
    for key in (
        "webLink",
        "sipAddress",
        "dialInIpAddress",
        "telephony",
        "password",
        "enabledAutoRecordMeeting",
    ):
        assert key in first
    detail = await client.get(f"/v1/meetings/{first['id']}")
    assert detail.json()["id"] == first["id"]
    participants = (await client.get("/v1/meetingParticipants", params={"meetingId": first["id"]})).json()[
        "items"
    ]
    assert len(participants) == len(original.participations)
    assert sum(p["host"] for p in participants) == 1
    assert participants[0]["devices"][0]["durationSecond"] > 0
    invites = (await client.get("/v1/meetingInvitees", params={"meetingId": first["id"]})).json()["items"]
    assert {p["email"] for p in invites} == {hub.world.person(pid).email for pid in original.attendee_ids}
    outsider = bearer(await mint(client, "https://webexapis.com", user="p018"))
    assert (await client.get(f"/v1/meetings/{first['id']}", headers=outsider)).status_code == 404
    assert (
        await client.get("/v1/meetingParticipants", params={"meetingId": first["id"]}, headers=outsider)
    ).status_code == 404
    assert (await client.get("/v1/meetings", params={"meetingType": "meeting"}, headers=outsider)).json() == {
        "items": []
    }
    series = (await client.get("/v1/meetings", params={"meetingType": "meetingSeries"})).json()["items"]
    assert len(series) == 1 and re.fullmatch(r"[a-f0-9]{32}", series[0]["id"])


async def test_meeting_crud_and_host_administration(client: httpx.AsyncClient) -> None:
    payload = {
        "title": "Meridian review",
        "start": "2026-10-20T14:00:00Z",
        "end": "2026-10-20T15:00:00Z",
        "timezone": "America/Toronto",
        "invitees": [{"email": "maya.chen@halcyonadvisory.example", "coHost": True}],
        "enabledAutoRecordMeeting": True,
        "password": "TestMeeting1!",
    }
    r = await client.post("/v1/meetings", json=payload)
    assert r.status_code == 200, r.text
    meeting = r.json()
    assert re.fullmatch(r"[a-f0-9]{32}", meeting["id"])
    assert meeting["start"] == "2026-10-20T14:00:00.000Z"
    maya = bearer(await mint(client, "https://webexapis.com", user="p018"))
    url = f"/v1/meetings/{meeting['id']}"
    assert (await client.get(url, headers=maya)).status_code == 200
    assert (await client.patch(url, headers=maya, json={"title": "Forbidden"})).status_code == 404
    assert (await client.put(url, json={"title": "Updated review"})).json()["title"] == "Updated review"
    patched = await client.patch(url, json={"end": "2026-10-20T15:30:00Z", "invitees": []})
    assert patched.status_code == 200
    assert (await client.get(url, headers=maya)).status_code == 404
    assert (await client.delete(url)).status_code == 204
    assert (await client.get(url)).status_code == 404
    service = bearer(await mint(client, "https://webexapis.com", app_only=True))
    r = await client.post(
        "/v1/meetings", json={**payload, "hostEmail": "maya.chen@halcyonadvisory.example"}, headers=service
    )
    assert r.status_code == 200 and r.json()["hostEmail"] == "maya.chen@halcyonadvisory.example"
    assert (
        await client.post("/v1/meetings", json={**payload, "hostEmail": "maya.chen@halcyonadvisory.example"})
    ).status_code == 403


async def test_meeting_filters_and_service_scopes(client: httpx.AsyncClient) -> None:
    params = {"meetingType": "meeting", "state": "ended", "from": "2026-10-01", "to": "2026-10-16"}
    items = (await client.get("/v1/meetings", params=params, headers=ADMIN)).json()["items"]
    assert len(items) == 2
    assert (
        await client.get(
            "/v1/meetings", params={**params, "hostEmail": "daniel.kowalski@halcyonadvisory.example"}
        )
    ).status_code == 403
    assert (
        len(
            (
                await client.get(
                    "/v1/meetings",
                    params={**params, "hostEmail": "daniel.kowalski@halcyonadvisory.example"},
                    headers=ADMIN,
                )
            ).json()["items"]
        )
        == 1
    )
    weak = bearer(
        await mint(client, "https://webexapis.com", app_only=True, scopes=["meeting:schedules_read"])
    )
    assert (await client.get("/v1/meetings", headers=weak)).json()["items"] == []
    service = bearer(await mint(client, "https://webexapis.com", app_only=True))
    assert (
        len(
            (await client.get("/v1/meetings", params={"meetingType": "meeting"}, headers=service)).json()[
                "items"
            ]
        )
        == 4
    )


@pytest.mark.parametrize(
    "query",
    [
        {"meetingType": "bad"},
        {"state": "bad"},
        {"max": "bad"},
        {"max": 101},
        {"from": "bad"},
        {"from": "2026-11-01", "to": "2026-10-01"},
        {"cursor": "garbage"},
    ],
)
async def test_bad_meeting_queries(client: httpx.AsyncClient, query: dict[str, str | int]) -> None:
    assert (await client.get("/v1/meetings", params=query)).status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": "x", "start": "bad", "end": "bad"},
        {"title": "x", "start": "2026-10-20", "end": "2026-10-19"},
        {"title": "x", "start": "2026-10-20", "end": "2026-10-21", "timezone": "Wrong/Timezone"},
        {
            "title": "x",
            "start": "2026-10-20",
            "end": "2026-10-21",
            "invitees": [{"email": "unknown@unknown.example"}],
        },
    ],
)
async def test_bad_meeting_bodies(client: httpx.AsyncClient, payload: dict[str, object]) -> None:
    assert (await client.post("/v1/meetings", json=payload)).status_code == 400


async def test_required_meeting_id(client: httpx.AsyncClient) -> None:
    for path in ("meetingInvitees", "meetingParticipants"):
        assert (await client.get(f"/v1/{path}")).status_code == 400
