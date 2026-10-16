"""Calendar sharing, events, recurrence, sync and push wire contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from emulators.core.clock import parse_instant
from emulators.providers.google.common import meet_uri
from emulators.testing.helpers import bearer, mint

BASE = "https://www.googleapis.com"
ROOT = BASE + "/calendar/v3"
EVENTS = ROOT + "/calendars/primary/events"
CEO = "eleanor.whitfield@halcyonadvisory.example"


@pytest.fixture
async def calendar_client(make_client: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client("google") as client:
        client.headers.update(bearer(await mint(client, BASE, user="p001")))
        yield client


def event_body(**fields: Any) -> dict[str, Any]:
    return {
        "summary": "Integration planning",
        "description": "Review the integration contract",
        "start": {"dateTime": "2026-10-19T09:00:00-04:00", "timeZone": "America/Toronto"},
        "end": {"dateTime": "2026-10-19T10:00:00-04:00", "timeZone": "America/Toronto"},
        **fields,
    }


async def test_calendar_list_roles_primary_and_denied(calendar_client: httpx.AsyncClient) -> None:
    client = calendar_client
    rows = (await client.get(ROOT + "/users/me/calendarList")).json()["items"]
    primary = next(r for r in rows if r.get("primary"))
    assert primary["id"] == CEO and primary["accessRole"] == "owner"
    assert primary["defaultReminders"] and primary["selected"]
    ea = bearer(await mint(client, BASE, user="p003"))
    shared = (await client.get(ROOT + "/users/me/calendarList/" + CEO, headers=ea)).json()
    assert shared["accessRole"] == "writer"
    assert (
        await client.post(ROOT + "/calendars/" + CEO + "/events", headers=ea, json=event_body())
    ).status_code == 200
    stranger = bearer(await mint(client, BASE, user="p035"))
    denied = await client.get(EVENTS, headers=stranger)
    assert denied.status_code == 200  # primary resolves to this caller's own mailbox
    assert (await client.get(ROOT + "/calendars/" + CEO + "/events", headers=stranger)).status_code == 404
    reader = bearer(await mint(client, BASE, user="p002"))
    assert (
        await client.post(ROOT + "/calendars/" + CEO + "/events", headers=reader, json=event_body())
    ).status_code == 403
    assert (
        await client.get(EVENTS, headers=bearer(await mint(client, BASE, app_only=True)))
    ).status_code == 403
    assert (await client.get(EVENTS, headers={"Authorization": "Bearer invalid"})).status_code == 401


async def test_seed_projection_meet_cross_ids_and_paging(calendar_client: httpx.AsyncClient) -> None:
    client = calendar_client
    first = (
        await client.get(EVENTS, params={"singleEvents": "true", "orderBy": "startTime", "maxResults": 2})
    ).json()
    assert len(first["items"]) == 2 and "nextSyncToken" not in first
    second = (
        await client.get(
            EVENTS,
            params={
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 2,
                "pageToken": first["nextPageToken"],
            },
        )
    ).json()
    assert {r["id"] for r in first["items"]}.isdisjoint(r["id"] for r in second["items"])
    all_events = (await client.get(EVENTS, params={"singleEvents": "true"})).json()
    assert "nextSyncToken" in all_events
    ctx = client.hub.context("google")  # type: ignore[attr-defined]
    meeting = next(m for m in ctx.world.meetings if m.platform == "meet" and "p001" in m.all_attendee_ids())
    event = next(
        r
        for r in all_events["items"]
        if r["summary"] == meeting.title and parse_instant(r["start"]["dateTime"]) == meeting.start
    )
    assert event["hangoutLink"] == meet_uri(ctx.world, meeting.id)
    assert event["conferenceData"]["conferenceSolution"]["key"]["type"] == "hangoutsMeet"
    assert any(a.get("self") and a["email"] == CEO for a in event["attendees"])
    assert (await client.get(EVENTS, params={"orderBy": "startTime"})).status_code == 400
    assert (await client.get(EVENTS, params={"pageToken": "bad"})).status_code == 400


async def test_event_crud_etag_sync_cancel_and_private_visibility(calendar_client: httpx.AsyncClient) -> None:
    client = calendar_client
    sync = (await client.get(EVENTS)).json()["nextSyncToken"]
    created = await client.post(EVENTS, json=event_body(visibility="private"))
    assert created.status_code == 200, created.text
    event = created.json()
    assert event["kind"] == "calendar#event" and len(event["id"]) >= 5
    reader = bearer(await mint(client, BASE, user="p002"))
    other_view = (
        await client.get(ROOT + "/calendars/" + CEO + "/events/" + event["id"], headers=reader)
    ).json()
    assert other_view["summary"] == "Busy" and "description" not in other_view
    assert (
        await client.get(EVENTS + "/" + event["id"], headers={"If-None-Match": event["etag"]})
    ).status_code == 304
    stale = await client.patch(
        EVENTS + "/" + event["id"], json={"summary": "No"}, headers={"If-Match": '"stale"'}
    )
    assert stale.status_code == 412
    updated = (
        await client.patch(
            EVENTS + "/" + event["id"], json={"summary": "Changed"}, headers={"If-Match": event["etag"]}
        )
    ).json()
    assert updated["etag"] != event["etag"] and updated["sequence"] > event["sequence"]
    delta = (await client.get(EVENTS, params={"syncToken": sync})).json()
    assert [r["id"] for r in delta["items"]] == [event["id"]]
    assert delta["items"][0]["summary"] == "Changed"
    assert (await client.delete(EVENTS + "/" + event["id"])).status_code == 204
    cancelled = (await client.get(EVENTS, params={"syncToken": delta["nextSyncToken"]})).json()["items"]
    assert len(cancelled) == 1 and cancelled[0]["status"] == "cancelled"
    assert (await client.get(EVENTS + "/" + event["id"])).status_code == 410
    invalid = await client.get(EVENTS, params={"syncToken": "invalid"})
    assert invalid.status_code == 410 and invalid.json()["error"]["errors"][0]["reason"] == "fullSyncRequired"
    assert (await client.get(EVENTS, params={"syncToken": sync, "q": "x"})).status_code == 400


async def test_recurring_instances_dst_all_day_and_override(calendar_client: httpx.AsyncClient) -> None:
    client = calendar_client
    event = (
        await client.post(EVENTS, json=event_body(recurrence=["RRULE:FREQ=WEEKLY;COUNT=4;BYDAY=MO"]))
    ).json()
    instances = (await client.get(EVENTS + "/" + event["id"] + "/instances")).json()["items"]
    assert len(instances) == 4
    assert all(r["recurringEventId"] == event["id"] and "recurrence" not in r for r in instances)
    assert instances[-1]["start"]["dateTime"].endswith("-05:00")
    assert instances[0]["id"].endswith("_20261019T130000Z")
    assert (
        await client.patch(EVENTS + "/" + instances[0]["id"], json={"summary": "Exception"})
    ).status_code == 200
    assert (await client.get(EVENTS + "/" + event["id"] + "/instances")).json()["items"][0][
        "summary"
    ] == "Exception"
    assert (await client.delete(EVENTS + "/" + instances[1]["id"])).status_code == 204
    assert len((await client.get(EVENTS + "/" + event["id"] + "/instances")).json()["items"]) == 3
    all_day = await client.post(
        EVENTS, json={"summary": "Offsite", "start": {"date": "2026-10-21"}, "end": {"date": "2026-10-22"}}
    )
    assert all_day.status_code == 200 and all_day.json()["start"] == {"date": "2026-10-21"}


async def test_calendar_crud_list_preferences_acl_and_clear(calendar_client: httpx.AsyncClient) -> None:
    client = calendar_client
    cal = (
        await client.post(
            ROOT + "/calendars", json={"summary": "Project calendar", "timeZone": "America/Toronto"}
        )
    ).json()
    calendar_id = cal["id"]
    assert (await client.get(ROOT + "/calendars/" + calendar_id)).json()["summary"] == "Project calendar"
    for method in ("PATCH", "PUT"):
        assert (
            await client.request(
                method, ROOT + "/calendars/" + calendar_id, json={"summary": "Updated calendar"}
            )
        ).status_code == 200
        assert (
            await client.request(
                method, ROOT + "/users/me/calendarList/" + calendar_id, json={"selected": False}
            )
        ).json()["selected"] is False
    assert (await client.delete(ROOT + "/users/me/calendarList/" + calendar_id)).status_code == 204
    assert (await client.post(ROOT + "/users/me/calendarList", json={"id": calendar_id})).status_code == 200
    acl = (await client.get(ROOT + "/calendars/primary/acl")).json()
    assert any(r["scope"]["type"] == "domain" and r["role"] == "freeBusyReader" for r in acl["items"])
    response = await client.post(ROOT + "/calendars/" + calendar_id + "/events", json=event_body())
    assert response.status_code == 200
    assert (await client.post(ROOT + "/calendars/" + calendar_id + "/clear")).status_code == 204
    assert (await client.get(ROOT + "/calendars/" + calendar_id + "/events")).json()["items"] == []
    assert (await client.delete(ROOT + "/calendars/" + calendar_id)).status_code == 204
    assert (await client.get(ROOT + "/calendars/" + calendar_id)).status_code == 404


async def test_conference_create_invitation_replication_move_import_quick_add(
    calendar_client: httpx.AsyncClient,
) -> None:
    client = calendar_client
    created = await client.post(
        EVENTS,
        params={"conferenceDataVersion": 1, "sendUpdates": "all"},
        json=event_body(
            attendees=[{"email": "maya.chen@halcyonadvisory.example", "responseStatus": "needsAction"}],
            conferenceData={"createRequest": {"requestId": "integration-conference"}},
        ),
    )
    assert created.status_code == 200, created.text
    event = created.json()
    assert event["conferenceData"]["createRequest"]["status"]["statusCode"] == "success"
    owner_token = await mint(client, BASE, user="p018")
    attendee_copy = (await client.get(EVENTS + "/" + event["id"], headers=bearer(owner_token))).json()
    assert attendee_copy["iCalUID"] == event["iCalUID"]
    assert (
        await client.get("https://meet.googleapis.com/v2/spaces/" + event["conferenceData"]["conferenceId"])
    ).status_code == 200
    imported = await client.post(
        EVENTS + "/import", json=event_body(iCalUID="vendor-event@halcyonadvisory.example")
    )
    assert (
        imported.status_code == 200 and imported.json()["iCalUID"] == "vendor-event@halcyonadvisory.example"
    )
    quick = await client.post(EVENTS + "/quickAdd", params={"text": "Planning 2026-10-22 at 14:00"})
    assert quick.status_code == 200 and quick.json()["summary"] == "Planning"
    target = (await client.post(ROOT + "/calendars", json={"summary": "Destination"})).json()["id"]
    moved = await client.post(EVENTS + "/" + imported.json()["id"] + "/move", params={"destination": target})
    assert moved.status_code == 200 and moved.json()["organizer"]["email"] == target
    replaced = await client.put(
        ROOT + "/calendars/" + target + "/events/" + moved.json()["id"], json=event_body(summary="Replaced")
    )
    assert replaced.status_code == 200 and replaced.json()["summary"] == "Replaced"


async def test_free_busy_colors_settings_and_filters(calendar_client: httpx.AsyncClient) -> None:
    client = calendar_client
    await client.post(EVENTS, json=event_body())
    await client.post(
        EVENTS,
        json=event_body(
            start={"dateTime": "2026-10-19T09:30:00-04:00"}, end={"dateTime": "2026-10-19T10:30:00-04:00"}
        ),
    )
    response = await client.post(
        ROOT + "/freeBusy",
        json={
            "timeMin": "2026-10-19T13:00:00Z",
            "timeMax": "2026-10-19T15:00:00Z",
            "items": [{"id": "primary"}, {"id": "missing@halcyonadvisory.example"}],
        },
    )
    assert response.status_code == 200
    busy = response.json()["calendars"]["primary"]["busy"]
    assert any(r["start"] <= "2026-10-19T13:00:00Z" and r["end"] >= "2026-10-19T14:30:00Z" for r in busy)
    assert (
        response.json()["calendars"]["missing@halcyonadvisory.example"]["errors"][0]["reason"] == "notFound"
    )
    assert len((await client.get(ROOT + "/colors")).json()["event"]) == 11
    assert (await client.get(ROOT + "/users/me/settings/timezone")).json()["value"] == "America/Toronto"
    assert (await client.get(ROOT + "/users/me/settings")).json()["items"]
    queried = (
        await client.get(
            EVENTS,
            params={
                "q": "Integration planning",
                "timeMin": "2026-10-19T00:00:00Z",
                "timeMax": "2026-10-20T00:00:00Z",
                "singleEvents": "true",
                "orderBy": "updated",
            },
        )
    ).json()
    assert len(queried["items"]) == 2


async def test_push_channel_headers_stop_and_permissions(calendar_client: httpx.AsyncClient) -> None:
    client = calendar_client
    received: list[dict[str, str]] = []
    receiver = FastAPI()

    @receiver.post("/calendar")
    async def hook(request: Request) -> dict[str, Any]:
        received.append(dict(request.headers))
        assert await request.body() == b""
        return {}

    client.hub.webhook_transport = httpx.ASGITransport(app=receiver)  # type: ignore[attr-defined]
    channel = (
        await client.post(
            EVENTS + "/watch",
            json={
                "id": "calendar-test",
                "type": "web_hook",
                "address": "https://receiver.example/calendar",
                "token": "test-verification",
            },
        )
    ).json()
    assert received[0]["x-goog-resource-state"] == "sync"
    assert received[0]["x-goog-channel-token"] == "test-verification"
    await client.post(EVENTS, json=event_body())
    assert received[-1]["x-goog-resource-state"] == "exists"
    assert int(received[-1]["x-goog-message-number"]) > int(received[0]["x-goog-message-number"])
    assert received[-1]["x-goog-resource-id"] == channel["resourceId"]
    assert (
        await client.post(
            ROOT + "/channels/stop", json={"id": channel["id"], "resourceId": channel["resourceId"]}
        )
    ).status_code == 204
    count = len(received)
    await client.post(EVENTS, json=event_body())
    assert len(received) == count


@pytest.mark.parametrize(
    "body",
    [
        event_body(id="invalid-characters"),
        {"summary": "Missing times"},
        event_body(end={"dateTime": "2026-10-19T01:00:00Z"}),
        event_body(recurrence=["RRULE:FREQ=UNKNOWN"]),
    ],
)
async def test_invalid_event_is_google_400(calendar_client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    response = await calendar_client.post(EVENTS, json=body)
    assert response.status_code == 400 and response.json()["error"]["errors"]


async def test_seed_series_update_delete_and_sync_expiry(calendar_client: httpx.AsyncClient) -> None:
    client = calendar_client
    recurring = next(r for r in (await client.get(EVENTS)).json()["items"] if "recurrence" in r)
    event_id = recurring["id"]
    response = await client.patch(EVENTS + "/" + event_id, json={"summary": "Updated recurring review"})
    assert response.status_code == 200
    instances = (await client.get(EVENTS + "/" + event_id + "/instances")).json()["items"]
    assert instances and all(r["summary"] == "Updated recurring review" for r in instances)
    assert (await client.delete(EVENTS + "/" + event_id)).status_code == 204
    expanded = (await client.get(EVENTS, params={"singleEvents": "true"})).json()["items"]
    assert all(r.get("recurringEventId") != event_id for r in expanded)
    token = (await client.get(EVENTS)).json()["nextSyncToken"]
    client.hub.context("google").store["calendar_sync"].get(token)["expires"] = 0  # type: ignore[attr-defined]
    assert (await client.get(EVENTS, params={"syncToken": token})).status_code == 410


async def test_explicit_readonly_scope_and_malformed_json(calendar_client: httpx.AsyncClient) -> None:
    client = calendar_client
    readonly = bearer(
        await mint(client, BASE, user="p001", scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    )
    assert (await client.get(EVENTS, headers=readonly)).status_code == 200
    assert (await client.post(EVENTS, headers=readonly, json=event_body())).status_code == 403
    assert (
        await client.post(EVENTS, content="{", headers={"Content-Type": "application/json"})
    ).status_code == 400


@pytest.mark.parametrize(
    "rule",
    [
        "RRULE:FREQ=WEEKLY;COUNT=bad",
        "RRULE:FREQ=WEEKLY;INTERVAL=0",
        "RRULE:FREQ=WEEKLY;UNTIL=bad",
        "RRULE:FREQ=WEEKLY;BYDAY=NO",
    ],
)
async def test_bad_recurrence_never_persists(calendar_client: httpx.AsyncClient, rule: str) -> None:
    response = await calendar_client.post(EVENTS, json=event_body(recurrence=[rule]))
    assert response.status_code == 400
