from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from emulators.providers.microsoft import common as c
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"
RANGE = {"startDateTime": "2026-09-01T00:00:00Z", "endDateTime": "2026-11-15T00:00:00Z"}


async def test_parlance_calendar_view_pages_and_online_meetings(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        result = await client.get(
            "/v1.0/me/calendarView",
            params={
                **RANGE,
                "$select": "subject,start,end,onlineMeeting,isOnlineMeeting,onlineMeetingProvider",
                "$top": 15,
                "$orderby": "start/dateTime",
            },
        )
        assert result.status_code == 200, result.text
        rows = result.json()["value"]
        assert len(rows) == 15
        assert rows == sorted(rows, key=lambda row: row["start"]["dateTime"])
        while "@odata.nextLink" in result.json():
            link = result.json()["@odata.nextLink"]
            assert urlsplit(link).path == "/v1.0/me/calendarView"
            result = await client.get(link)
            rows.extend(result.json()["value"])
        assert len(rows) > 15 and len({r["id"] for r in rows}) == len(rows)
        teams = [r for r in rows if r["isOnlineMeeting"]]
        assert teams and all(
            r["onlineMeeting"]["joinUrl"].startswith("https://teams.microsoft.com/l/meetup-join/")
            for r in teams
        )
        assert all(r["start"]["dateTime"].endswith("0000000") for r in rows)


async def test_calendar_series_timezones_settings_and_range_errors(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        for path in ("calendar", "calendars", "calendarGroups"):
            assert (await client.get("/v1.0/me/" + path)).status_code == 200
        cid = (await client.get("/v1.0/me/calendar")).json()["id"]
        for path in ("events", "calendar/events", f"calendars/{cid}/events"):
            result = await client.get("/v1.0/me/" + path, params={"$top": 1000})
            assert all(r["type"] in {"singleInstance", "seriesMaster"} for r in result.json()["value"])
        master = next(r for r in result.json()["value"] if r["type"] == "seriesMaster")
        instances = await client.get(f"/v1.0/me/events/{master['id']}/instances", params=RANGE)
        assert instances.json()["value"]
        first = instances.json()["value"][0]
        assert first["seriesMasterId"] == master["id"] and first["type"] == "occurrence"
        result = await client.get(
            f"/beta/me/events/{first['id']}", headers={"Prefer": 'outlook.timezone="Pacific Standard Time"'}
        )
        assert result.json()["start"]["timeZone"] == "Pacific Standard Time"
        assert result.json()["start"]["dateTime"] != first["start"]["dateTime"]
        for params in (
            {},
            {"startDateTime": RANGE["startDateTime"]},
            {"startDateTime": "bad", "endDateTime": "bad"},
        ):
            assert (await client.get("/v1.0/me/calendarView", params=params)).status_code == 400
        assert (await client.get("/v1.0/me/calendar/calendarView", params=RANGE)).status_code == 200
        uid = c.user_id(client.hub.world, "p035")
        assert (await client.get(f"/v1.0/users/{uid}/events")).status_code == 403
        assert (
            await client.get(
                f"/v1.0/users/{uid}/events", headers=bearer(await mint(client, BASE, app_only=True))
            )
        ).status_code == 200


async def test_event_crud_invites_responses_delta_schedule_and_suggestions(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        delta = (await client.get("/v1.0/me/events/delta", params={"$top": 1000})).json()["@odata.deltaLink"]
        payload = {
            "subject": "Synthetic scheduling contract",
            "start": {"dateTime": "2026-10-20T09:00:00", "timeZone": "Eastern Standard Time"},
            "end": {"dateTime": "2026-10-20T10:00:00", "timeZone": "Eastern Standard Time"},
            "isOnlineMeeting": True,
            "transactionId": "contract-transaction",
            "attendees": [
                {
                    "emailAddress": {"address": "arjun.mehta@halcyonadvisory.example", "name": "Arjun Mehta"},
                    "type": "required",
                }
            ],
        }
        result = await client.post("/v1.0/me/events", json=payload)
        assert result.status_code == 201, result.text
        record = result.json()
        eid = record["id"]
        assert record["onlineMeeting"]["joinUrl"] in record["body"]["content"]
        assert record["start"]["dateTime"].startswith("2026-10-20T13:00")
        assert (await client.post("/v1.0/me/events", json=payload)).json()["id"] == eid
        assert (await client.get(delta)).json()["value"][0]["id"] == eid
        patched = await client.patch(
            f"/v1.0/me/events/{eid}", json={"subject": "Updated scheduling contract"}
        )
        assert patched.json()["subject"] == "Updated scheduling contract"
        attendee = bearer(await mint(client, BASE, user="p035"))
        invited = (
            await client.get(
                "/v1.0/me/events",
                headers=attendee,
                params={"$filter": "subject eq 'Updated scheduling contract'"},
            )
        ).json()["value"][0]
        for operation in ("accept", "tentativelyAccept", "decline"):
            response = await client.post(
                f"/v1.0/me/events/{invited['id']}/{operation}", headers=attendee, json={"sendResponse": True}
            )
            assert response.status_code == 202
        assert (
            await client.post(f"/v1.0/me/events/{eid}/forward", json={"toRecipients": payload["attendees"]})
        ).status_code == 202
        assert (
            await client.post(f"/v1.0/me/events/{eid}/cancel", json={"comment": "Cancelled"})
        ).status_code == 202
        assert (await client.get(f"/v1.0/me/events/{eid}")).json()["isCancelled"]
        assert (await client.delete(f"/v1.0/me/events/{eid}")).status_code == 204
        schedule = await client.post(
            "/v1.0/me/calendar/getSchedule",
            json={
                "schedules": ["maya.chen@halcyonadvisory.example"],
                "startTime": payload["start"],
                "endTime": payload["end"],
                "availabilityViewInterval": 30,
            },
        )
        assert len(schedule.json()["value"][0]["availabilityView"]) == 2
        suggestions = await client.post(
            "/v1.0/me/findMeetingTimes",
            json={
                "meetingDuration": "PT30M",
                "timeConstraint": {"timeSlots": [{"start": payload["start"], "end": payload["end"]}]},
            },
        )
        assert suggestions.json()["meetingTimeSuggestions"][0]["meetingTimeSlot"]["start"] == payload["start"]
        view_delta = await client.get("/v1.0/me/calendarView/delta", params={**RANGE, "$top": 1000})
        assert "@odata.deltaLink" in view_delta.json()


async def test_prefix_paging_keeps_single_provider_prefix(make_hub: Callable[..., Any]) -> None:
    hub = make_hub("microsoft")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hub.gateway), base_url="http://localhost:44080"
    ) as client:
        client.headers.update(bearer(await mint(client, "/microsoft", user="p018")))
        result = await client.get("/microsoft/v1.0/me/calendarView", params={**RANGE, "$top": 1})
        link = result.json()["@odata.nextLink"]
        assert urlsplit(link).path == "/microsoft/v1.0/me/calendarView"
        assert (await client.get(link)).status_code == 200


async def test_runtime_recurring_series_expands_across_dst(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        payload = {
            "subject": "Recurring DST contract",
            "start": {"dateTime": "2026-10-26T09:00:00", "timeZone": "Eastern Standard Time"},
            "end": {"dateTime": "2026-10-26T09:30:00", "timeZone": "Eastern Standard Time"},
            "recurrence": {
                "pattern": {
                    "type": "weekly",
                    "interval": 1,
                    "daysOfWeek": ["monday"],
                    "firstDayOfWeek": "sunday",
                },
                "range": {"type": "numbered", "startDate": "2026-10-26", "numberOfOccurrences": 3},
            },
        }
        master = (await client.post("/v1.0/me/events", json=payload)).json()
        assert master["type"] == "seriesMaster"
        result = await client.get(f"/v1.0/me/events/{master['id']}/instances", params=RANGE)
        instances = result.json()["value"]
        assert len(instances) == 3, result.text
        assert instances[0]["start"]["dateTime"].startswith("2026-10-26T13:00")
        assert instances[1]["start"]["dateTime"].startswith("2026-11-02T14:00")
        assert (await client.get(f"/v1.0/me/events/{instances[1]['id']}")).status_code == 200
        view = await client.get(
            "/v1.0/me/calendarView", params={**RANGE, "$filter": "subject eq 'Recurring DST contract'"}
        )
        assert len(view.json()["value"]) == 3
        assert (await client.delete(f"/v1.0/me/events/{master['id']}")).status_code == 204
