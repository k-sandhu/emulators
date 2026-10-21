"""Scheduled meetings: listing, create/read/update/delete, recurrence, invitation, registrants."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import httpx

from emulators.core.clock import isoformat_z, parse_instant
from tests.providers.zoom.conftest import (
    ADVISORY_ID,
    BLUEFIN_ID,
    HOST,
    MAYA,
    STAFF,
    V2,
    special_uuids,
    token_for,
)

LIST_ITEM_KEYS = [
    "uuid", "id", "host_id", "topic", "type", "start_time", "duration", "timezone", "agenda", "created_at", "join_url",
]  # fmt: skip


async def create(client: httpx.AsyncClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    r = await client.post(f"{V2}/users/me/meetings", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_list_my_meetings_shape_ids_and_uuids(
    zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any
) -> None:
    body = (await zoom.get(f"{V2}/users/me/meetings", headers=host)).json()
    assert list(body) == ["page_size", "total_records", "next_page_token", "meetings"]
    assert body["total_records"] == 5 and body["next_page_token"] == "" and body["page_size"] == 30
    for meeting in body["meetings"]:
        assert list(meeting) == LIST_ITEM_KEYS
        assert 10 <= len(str(meeting["id"])) <= 11 and isinstance(meeting["id"], int)
        assert len(meeting["uuid"]) == 24 and meeting["uuid"].endswith("==")
        assert meeting["type"] == 8 and meeting["start_time"].endswith("Z")
        assert re.fullmatch(
            rf"https://us06web\.zoom\.us/j/{meeting['id']}\?pwd=[A-Za-z0-9+/=]+", meeting["join_url"]
        )
    # Zoom UUIDs may start with "/" or contain "//": the seed guarantees both shapes exist
    leading, double = special_uuids(ctx)
    assert leading.startswith("/") and "//" in double


async def test_list_types_and_date_window(zoom: httpx.AsyncClient, maya: dict[str, str]) -> None:
    def params(**kw: Any) -> dict[str, Any]:
        return kw

    previous = (
        await zoom.get(f"{V2}/users/me/meetings", headers=maya, params=params(type="previous_meetings"))
    ).json()
    assert previous["total_records"] == 3  # Advisory Deal Review + two findings reviews that ran
    upcoming = (
        await zoom.get(f"{V2}/users/me/meetings", headers=maya, params=params(type="upcoming"))
    ).json()
    assert {m["topic"] for m in upcoming["meetings"]} >= {"Advisory Deal Review"}
    advisory = next(m for m in upcoming["meetings"] if m["id"] == ADVISORY_ID)
    assert advisory["start_time"] == "2026-10-22T15:00:00Z"  # next occurrence, not the series start
    assert (await zoom.get(f"{V2}/users/me/meetings", headers=maya, params=params(type="live"))).json()[
        "total_records"
    ] == 0
    scheduled = (await zoom.get(f"{V2}/users/me/meetings", headers=maya)).json()
    assert scheduled["total_records"] == 6
    windowed = (
        await zoom.get(
            f"{V2}/users/me/meetings",
            headers=maya,
            params=params(**{"from": "2026-10-25", "to": "2026-10-27"}),
        )
    ).json()
    assert {m["topic"] for m in windowed["meetings"]} == {"Bluefin Seafood — Findings Review"}
    assert (
        await zoom.get(f"{V2}/users/me/meetings", headers=maya, params=params(type="bogus"))
    ).status_code == 400


async def test_meetings_paging(zoom: httpx.AsyncClient, host: dict[str, str]) -> None:
    first = (await zoom.get(f"{V2}/users/me/meetings", headers=host, params={"page_size": 4})).json()
    assert len(first["meetings"]) == 4 and first["next_page_token"]
    second = (
        await zoom.get(
            f"{V2}/users/me/meetings",
            headers=host,
            params={"page_size": 4, "next_page_token": first["next_page_token"]},
        )
    ).json()
    assert len(second["meetings"]) == 1 and second["next_page_token"] == ""
    assert not {m["id"] for m in first["meetings"]} & {m["id"] for m in second["meetings"]}


async def test_get_meeting_detail_occurrences_and_status(
    zoom: httpx.AsyncClient, host: dict[str, str]
) -> None:
    m = (await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}", headers=host)).json()
    for key in (
        "uuid",
        "id",
        "host_id",
        "host_email",
        "topic",
        "type",
        "status",
        "start_time",
        "duration",
        "timezone",
        "agenda",
        "created_at",
        "start_url",
        "join_url",
        "password",
        "h323_password",
        "pstn_password",
        "encrypted_password",
        "settings",
        "recurrence",
        "occurrences",
    ):
        assert key in m, key
    assert m["status"] == "waiting" and m["type"] == 8 and m["host_email"] == HOST
    assert "zak=" in m["start_url"] and m["start_url"].startswith(f"https://us06web.zoom.us/s/{BLUEFIN_ID}")
    assert m["join_url"] == f"https://us06web.zoom.us/j/{BLUEFIN_ID}?pwd={m['encrypted_password']}"
    assert (
        m["recurrence"]["type"] == 2
        and m["recurrence"]["repeat_interval"] == 2
        and m["recurrence"]["weekly_days"] == "4"
    )
    assert m["settings"]["auto_recording"] == "cloud" and m["settings"]["waiting_room"] is True
    # by default only occurrences still to come, one of them cancelled
    assert [o["status"] for o in m["occurrences"]] == ["available", "deleted"]
    assert all(set(o) == {"occurrence_id", "start_time", "duration", "status"} for o in m["occurrences"])
    full = (
        await zoom.get(
            f"{V2}/meetings/{BLUEFIN_ID}", headers=host, params={"show_previous_occurrences": "true"}
        )
    ).json()
    assert len(full["occurrences"]) == 5
    one = (
        await zoom.get(
            f"{V2}/meetings/{BLUEFIN_ID}",
            headers=host,
            params={"occurrence_id": full["occurrences"][1]["occurrence_id"]},
        )
    ).json()
    assert [o["start_time"] for o in one["occurrences"]] == [full["occurrences"][1]["start_time"]]
    assert one["start_time"] == full["occurrences"][1]["start_time"]
    missing = await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}", headers=host, params={"occurrence_id": "1"})
    assert missing.status_code == 404


async def test_meeting_visibility_host_alternative_host_admin_and_others(
    zoom: httpx.AsyncClient,
    host: dict[str, str],
    maya: dict[str, str],
    staff: dict[str, str],
    admin: dict[str, str],
    s2s: dict[str, str],
) -> None:
    assert (await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}", headers=staff)).status_code == 403
    denied = await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}", headers=staff)
    assert denied.json() == {"code": 3003, "message": "You are not the meeting host."}
    assert (
        await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}", headers=maya)
    ).status_code == 200  # alternative host
    assert (
        await zoom.patch(f"{V2}/meetings/{BLUEFIN_ID}", headers=maya, json={"topic": "x"})
    ).status_code == 403
    assert (await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}", headers=admin)).status_code == 200
    assert (await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}", headers=s2s)).status_code == 200
    gone = await zoom.get(f"{V2}/meetings/123", headers=host)
    assert gone.status_code == 404 and gone.json() == {
        "code": 3001,
        "message": "Meeting does not exist: 123.",
    }
    other = await zoom.get(f"{V2}/users/{MAYA}/meetings", headers=host)
    assert other.status_code == 403 and other.json()["code"] == 200
    assert (await zoom.get(f"{V2}/users/{MAYA}/meetings", headers=admin)).json()["total_records"] == 6
    assert (await zoom.get(f"{V2}/users/{STAFF}/meetings", headers=admin)).json()["total_records"] == 0
    assert (
        await zoom.get(f"{V2}/users/nobody@halcyonadvisory.example/meetings", headers=admin)
    ).status_code == 404


async def test_create_scheduled_meeting_full_response(zoom: httpx.AsyncClient, staff: dict[str, str]) -> None:
    m = await create(
        zoom, staff, topic="Planning", type=2, start_time="2026-11-10T15:30:00Z", duration=45,
        timezone="America/Toronto", agenda="Q4 plan", password="abc123",
        settings={"waiting_room": True, "auto_recording": "cloud", "join_before_host": False, "mute_upon_entry": True},
    )  # fmt: skip
    assert 10 <= len(str(m["id"])) <= 11 and len(m["uuid"]) == 24 and m["uuid"].endswith("==")
    assert (
        m["host_email"] == STAFF and m["topic"] == "Planning" and m["type"] == 2 and m["status"] == "waiting"
    )
    assert (
        m["start_time"] == "2026-11-10T15:30:00Z"
        and m["duration"] == 45
        and m["timezone"] == "America/Toronto"
    )
    assert m["password"] == m["h323_password"] == m["pstn_password"] == "abc123"
    assert m["join_url"] == f"https://us06web.zoom.us/j/{m['id']}?pwd={m['encrypted_password']}"
    assert m["start_url"].startswith(f"https://us06web.zoom.us/s/{m['id']}?zak=")
    s = m["settings"]
    assert s["waiting_room"] is True and s["auto_recording"] == "cloud" and s["mute_upon_entry"] is True
    assert s["approval_type"] == 2 and s["audio"] == "voip" and s["encryption_type"] == "enhanced_encryption"
    assert "occurrences" not in m  # only recurring meetings list occurrences
    again = (await zoom.get(f"{V2}/meetings/{m['id']}", headers=staff)).json()
    assert again["topic"] == "Planning" and again["settings"]["waiting_room"] is True
    listed = (await zoom.get(f"{V2}/users/me/meetings", headers=staff)).json()
    assert [x["id"] for x in listed["meetings"]] == [m["id"]]


async def test_create_defaults_local_time_and_instant_meeting(
    zoom: httpx.AsyncClient, staff: dict[str, str]
) -> None:
    defaulted = await create(zoom, staff)
    assert (
        defaulted["topic"] == "Ryan Murphy's Zoom Meeting"
        and defaulted["type"] == 2
        and defaulted["duration"] == 60
    )
    assert re.fullmatch(r"\d{6}", defaulted["password"])
    local = await create(
        zoom, staff, topic="Local", start_time="2026-11-10T09:00:00", timezone="America/New_York"
    )
    assert local["start_time"] == "2026-11-10T14:00:00Z"  # EST
    summer = await create(
        zoom, staff, topic="Summer", start_time="2026-07-10T09:00:00", timezone="America/New_York"
    )
    assert summer["start_time"] == "2026-07-10T13:00:00Z"  # EDT
    instant = await create(zoom, staff, topic="Now", type=1)
    assert instant["type"] == 1 and instant["start_time"].endswith("Z")


async def test_create_validation_and_permissions(
    zoom: httpx.AsyncClient, staff: dict[str, str], admin: dict[str, str], host: dict[str, str]
) -> None:
    async def field_error(**body: Any) -> dict[str, Any]:
        r = await zoom.post(f"{V2}/users/me/meetings", headers=staff, json=body)
        assert r.status_code == 400, r.text
        assert r.json()["code"] == 300 and r.json()["message"] == "Validation Failed."
        return r.json()["errors"][0]

    assert (await field_error(type=5))["field"] == "type"
    assert (await field_error(type=2, start_time="next tuesday"))["field"] == "start_time"
    assert (await field_error(timezone="Mars/Olympus"))["field"] == "timezone"
    assert (await field_error(settings={"auto_recording": "everything"}))[
        "field"
    ] == "settings.auto_recording"
    assert (await field_error(password="bad pass!"))["field"] == "password"
    assert (await field_error(type=8, start_time="2026-11-10T15:30:00Z"))["field"] == "recurrence"
    bad_json = await zoom.post(f"{V2}/users/me/meetings", headers=staff, content=b"{nope")
    assert bad_json.status_code == 400
    assert bad_json.json() == {"code": 300, "message": "Request Body should be a valid JSON object."}

    # scheduling on someone else's behalf needs account admin reach
    assert (
        await zoom.post(f"{V2}/users/{HOST}/meetings", headers=staff, json={"topic": "x"})
    ).status_code == 403
    on_behalf = await zoom.post(f"{V2}/users/{HOST}/meetings", headers=admin, json={"topic": "For Ben"})
    assert on_behalf.status_code == 201 and on_behalf.json()["host_email"] == HOST
    assert any(
        m["topic"] == "For Ben"
        for m in (await zoom.get(f"{V2}/users/me/meetings", headers=host)).json()["meetings"]
    )

    read_only = await token_for(zoom, STAFF, scopes="meeting:read")
    r = await zoom.post(f"{V2}/users/me/meetings", headers=read_only, json={"topic": "x"})
    assert r.status_code == 401 and r.json()["code"] == 4700 and "meeting:write" in r.json()["message"]


async def test_recurring_weekly_across_a_dst_change(zoom: httpx.AsyncClient, staff: dict[str, str]) -> None:
    m = await create(
        zoom, staff, topic="Standup", type=8, start_time="2026-10-27T09:00:00", timezone="America/New_York", duration=15,
        recurrence={"type": 2, "repeat_interval": 1, "weekly_days": "3", "end_times": 4},
    )  # fmt: skip
    times = [o["start_time"] for o in m["occurrences"]]
    # 9:00 Eastern: UTC-4 until Sunday 1 Nov 2026, UTC-5 after it
    assert times == [
        "2026-10-27T13:00:00Z",
        "2026-11-03T14:00:00Z",
        "2026-11-10T14:00:00Z",
        "2026-11-17T14:00:00Z",
    ]
    assert all(o["duration"] == 15 and o["status"] == "available" for o in m["occurrences"])
    assert re.fullmatch(r"\d{13}", m["occurrences"][0]["occurrence_id"])
    assert m["recurrence"]["weekly_days"] == "3" and m["start_time"] == times[0]


async def test_recurring_daily_monthly_and_end_date(zoom: httpx.AsyncClient, staff: dict[str, str]) -> None:
    daily = await create(
        zoom, staff, type=8, start_time="2026-11-02T10:00:00Z",
        recurrence={"type": 1, "repeat_interval": 2, "end_date_time": "2026-11-09T00:00:00Z"},
    )  # fmt: skip
    assert [o["start_time"][:10] for o in daily["occurrences"]] == [
        "2026-11-02",
        "2026-11-04",
        "2026-11-06",
        "2026-11-08",
    ]
    monthly = await create(
        zoom, staff, type=8, start_time="2026-11-15T10:00:00Z",
        recurrence={"type": 3, "repeat_interval": 1, "monthly_day": 15, "end_times": 3},
    )  # fmt: skip
    assert [o["start_time"][:10] for o in monthly["occurrences"]] == [
        "2026-11-15",
        "2026-12-15",
        "2027-01-15",
    ]
    nth = await create(
        zoom, staff, type=8, start_time="2026-11-01T10:00:00Z",
        recurrence={"type": 3, "monthly_week": 2, "monthly_week_day": 3, "end_times": 2},
    )  # fmt: skip
    assert [o["start_time"][:10] for o in nth["occurrences"]] == ["2026-11-10", "2026-12-08"]  # 2nd Tuesday
    capped = await create(
        zoom, staff, type=8, start_time="2026-11-02T10:00:00Z", recurrence={"type": 1, "end_times": 0}
    )
    assert len(capped["occurrences"]) == 60


async def test_update_meeting_fields_password_and_single_occurrence(
    zoom: httpx.AsyncClient, staff: dict[str, str]
) -> None:
    m = await create(
        zoom, staff, topic="Old", type=8, start_time="2026-11-02T10:00:00Z", duration=30,
        recurrence={"type": 1, "end_times": 3},
    )  # fmt: skip
    url = f"{V2}/meetings/{m['id']}"
    ok = await zoom.patch(
        url,
        headers=staff,
        json={"topic": "New", "agenda": "A", "password": "newpass1", "settings": {"waiting_room": True}},
    )
    assert ok.status_code == 204 and ok.content == b""
    after = (await zoom.get(url, headers=staff)).json()
    assert after["topic"] == "New" and after["agenda"] == "A" and after["password"] == "newpass1"
    assert (
        after["join_url"].endswith(f"pwd={after['encrypted_password']}")
        and after["encrypted_password"] != m["encrypted_password"]
    )
    assert after["settings"]["waiting_room"] is True and after["settings"]["host_video"] is False

    occ = m["occurrences"][1]
    single = await zoom.patch(
        url,
        headers=staff,
        params={"occurrence_id": occ["occurrence_id"]},
        json={"start_time": "2026-11-03T16:00:00Z", "duration": 90},
    )
    assert single.status_code == 204
    changed = (await zoom.get(url, headers=staff, params={"occurrence_id": occ["occurrence_id"]})).json()
    assert (
        changed["occurrences"][0]["start_time"] == "2026-11-03T16:00:00Z"
        and changed["occurrences"][0]["duration"] == 90
    )
    series = (await zoom.get(url, headers=staff)).json()
    assert series["occurrences"][0]["start_time"] == "2026-11-02T10:00:00Z"

    retimed = await zoom.patch(
        url,
        headers=staff,
        json={"start_time": "2026-11-09T12:00:00Z", "recurrence": {"type": 1, "end_times": 2}},
    )
    assert retimed.status_code == 204
    times = [o["start_time"] for o in (await zoom.get(url, headers=staff)).json()["occurrences"]]
    assert times == ["2026-11-09T12:00:00Z", "2026-11-10T12:00:00Z"]
    assert (await zoom.patch(url, headers=staff, json={"duration": -1})).status_code == 400


async def test_delete_meeting_occurrence_and_recover(zoom: httpx.AsyncClient, staff: dict[str, str]) -> None:
    m = await create(
        zoom,
        staff,
        topic="Delete me",
        type=8,
        start_time="2026-11-02T10:00:00Z",
        recurrence={"type": 1, "end_times": 3},
    )
    url = f"{V2}/meetings/{m['id']}"
    part = await zoom.delete(
        url, headers=staff, params={"occurrence_id": m["occurrences"][0]["occurrence_id"]}
    )
    assert part.status_code == 204
    statuses = [o["status"] for o in (await zoom.get(url, headers=staff)).json()["occurrences"]]
    assert statuses == ["deleted", "available", "available"]
    assert (await zoom.delete(url, headers=staff)).status_code == 204
    assert (await zoom.get(url, headers=staff)).status_code == 404
    assert (await zoom.delete(url, headers=staff)).status_code == 404
    assert (await zoom.put(f"{url}/status", headers=staff, json={"action": "recover"})).status_code == 204
    assert (await zoom.get(url, headers=staff)).json()["topic"] == "Delete me"


async def test_only_the_host_or_an_admin_can_delete(
    zoom: httpx.AsyncClient, staff: dict[str, str], host: dict[str, str]
) -> None:
    assert (await zoom.delete(f"{V2}/meetings/{BLUEFIN_ID}", headers=staff)).status_code == 403
    assert (await zoom.delete(f"{V2}/meetings/{BLUEFIN_ID}", headers=host)).status_code == 204
    assert (await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}", headers=host)).status_code == 404


async def test_invitation_text(zoom: httpx.AsyncClient, host: dict[str, str]) -> None:
    body = (await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}/invitation", headers=host)).json()
    text = body["invitation"]
    meeting = (await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}", headers=host)).json()
    assert text.startswith("Benjamin Adler is inviting you to a scheduled Zoom meeting.")
    assert "Topic: Bluefin Seafood — Status Update" in text
    assert "Eastern Time (US and Canada)" in text and "Every 2 weeks on Wed" in text
    assert meeting["join_url"] in text and f"Passcode: {meeting['password']}" in text
    assert "644 819 8817" in text  # meeting id in Zoom's grouped 3-4-3 form
    assert body["sip_links"] and body["sip_links"][0].startswith("sip:")


async def test_registrants_require_registration_to_be_enabled(
    zoom: httpx.AsyncClient, staff: dict[str, str]
) -> None:
    plain = await create(zoom, staff, topic="No registration")
    refused = await zoom.post(
        f"{V2}/meetings/{plain['id']}/registrants",
        headers=staff,
        json={"email": "a@b.example", "first_name": "A"},
    )
    assert refused.status_code == 400 and refused.json()["code"] == 3000
    m = await create(zoom, staff, topic="Webinar-ish", settings={"approval_type": 1})
    reg = await zoom.post(
        f"{V2}/meetings/{m['id']}/registrants", headers=staff,
        json={"email": "guest@client.example", "first_name": "Gus", "last_name": "Guest", "org": "Client"},
    )  # fmt: skip
    assert reg.status_code == 201
    created = reg.json()
    assert set(created) >= {"id", "join_url", "registrant_id", "topic"} and created["topic"] == "Webinar-ish"
    assert "/w/" in created["join_url"]
    dup = await zoom.post(
        f"{V2}/meetings/{m['id']}/registrants",
        headers=staff,
        json={"email": "guest@client.example", "first_name": "Gus"},
    )
    assert dup.json()["registrant_id"] == created["registrant_id"]
    pending = (
        await zoom.get(f"{V2}/meetings/{m['id']}/registrants", headers=staff, params={"status": "pending"})
    ).json()
    assert pending["total_records"] == 1 and pending["registrants"][0]["email"] == "guest@client.example"
    assert (
        pending["registrants"][0]["status"] == "pending" and pending["registrants"][0]["first_name"] == "Gus"
    )
    assert (await zoom.get(f"{V2}/meetings/{m['id']}/registrants", headers=staff)).json()[
        "total_records"
    ] == 0
    approve = await zoom.put(
        f"{V2}/meetings/{m['id']}/registrants/status", headers=staff,
        json={"action": "approve", "registrants": [{"id": created["registrant_id"]}]},
    )  # fmt: skip
    assert approve.status_code == 204
    assert (await zoom.get(f"{V2}/meetings/{m['id']}/registrants", headers=staff)).json()[
        "total_records"
    ] == 1
    assert (
        await zoom.delete(f"{V2}/meetings/{m['id']}/registrants/{created['registrant_id']}", headers=staff)
    ).status_code == 204


async def test_upcoming_meetings_endpoint(zoom: httpx.AsyncClient, staff: dict[str, str], ctx: Any) -> None:
    ctx.clock.freeze()
    now = ctx.clock.now()
    expected = await create(zoom, staff, start_time=isoformat_z(now + timedelta(hours=2)))
    later = await create(zoom, staff, start_time=isoformat_z(now + timedelta(hours=25)))
    body = (await zoom.get(f"{V2}/users/me/upcoming_meetings", headers=staff)).json()
    assert body["total_records"] == len(body["meetings"])
    ids = {meeting["id"] for meeting in body["meetings"]}
    assert expected["id"] in ids and later["id"] not in ids
    assert all(
        now <= parse_instant(row["start_time"]) < now + timedelta(hours=24) for row in body["meetings"]
    )
    assert {"id", "topic", "type", "start_time", "duration", "timezone", "join_url"} <= set(
        body["meetings"][0]
    )
    assert [m["start_time"] for m in body["meetings"]] == sorted(m["start_time"] for m in body["meetings"])


async def test_upcoming_meetings_includes_invited_user(
    zoom: httpx.AsyncClient, host: dict[str, str], staff: dict[str, str], ctx: Any
) -> None:
    meeting = await create(
        zoom,
        host,
        start_time=isoformat_z(ctx.clock.now() + timedelta(hours=2)),
        settings={"meeting_invitees": [{"email": STAFF}]},
    )
    body = (await zoom.get(f"{V2}/users/me/upcoming_meetings", headers=staff)).json()
    assert meeting["id"] in {row["id"] for row in body["meetings"]}
    assert all("start_url" not in row and "password" not in row for row in body["meetings"])


async def test_granular_scopes_work_like_classic_ones(zoom: httpx.AsyncClient) -> None:
    granular = await token_for(
        zoom,
        None,
        scopes="meeting:read:list_meetings:admin meeting:read:meeting:admin meeting:write:meeting:admin",
    )
    listing = await zoom.get(f"{V2}/users/{HOST}/meetings", headers=granular)
    assert listing.status_code == 200 and listing.json()["total_records"] == 5
    assert (await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}", headers=granular)).status_code == 200
    created = await zoom.post(f"{V2}/users/{HOST}/meetings", headers=granular, json={"topic": "Granular"})
    assert created.status_code == 201
    # but update/delete scopes were not granted
    denied = await zoom.patch(f"{V2}/meetings/{created.json()['id']}", headers=granular, json={"topic": "x"})
    assert denied.status_code == 401 and denied.json()["code"] == 4700
    assert "meeting:update:meeting" in denied.json()["message"]


async def test_schedule_for_another_user(
    zoom: httpx.AsyncClient, staff: dict[str, str], admin: dict[str, str], host: dict[str, str]
) -> None:
    refused = await zoom.post(
        f"{V2}/users/me/meetings", headers=staff, json={"topic": "x", "schedule_for": HOST}
    )
    assert refused.status_code == 403
    made = await zoom.post(
        f"{V2}/users/me/meetings", headers=admin, json={"topic": "On behalf", "schedule_for": HOST}
    )
    assert made.status_code == 201 and made.json()["host_email"] == HOST
    assert (await zoom.get(f"{V2}/meetings/{made.json()['id']}", headers=host)).status_code == 200
    ghost = await zoom.post(
        f"{V2}/users/me/meetings", headers=admin, json={"topic": "x", "schedule_for": "ghost@x.example"}
    )
    assert ghost.status_code == 404 and ghost.json()["code"] == 1001


async def test_inactive_hosts_cannot_schedule(zoom: httpx.AsyncClient, admin: dict[str, str]) -> None:
    refused = await zoom.post(
        f"{V2}/users/paul.henderson@halcyonadvisory.example/meetings", headers=admin, json={"topic": "ghost"}
    )
    assert refused.status_code == 400 and refused.json()["code"] == 3161


async def test_recurrence_patterns_multi_day_interval_and_last_weekday(
    zoom: httpx.AsyncClient, staff: dict[str, str]
) -> None:
    def days(meeting: dict[str, Any]) -> list[str]:
        return [o["start_time"][:10] for o in meeting["occurrences"]]

    multi = await create(
        zoom, staff, type=8, start_time="2026-11-02T10:00:00Z", timezone="UTC",
        recurrence={"type": 2, "weekly_days": "2,4", "end_times": 5},
    )  # fmt: skip
    assert days(multi) == ["2026-11-02", "2026-11-04", "2026-11-09", "2026-11-11", "2026-11-16"]
    biweekly = await create(
        zoom, staff, type=8, start_time="2026-11-02T10:00:00Z", timezone="UTC",
        recurrence={"type": 2, "repeat_interval": 2, "weekly_days": "2", "end_times": 3},
    )  # fmt: skip
    assert days(biweekly) == ["2026-11-02", "2026-11-16", "2026-11-30"]
    shifted = await create(
        zoom, staff, type=8, start_time="2026-11-03T10:00:00Z", timezone="UTC",
        recurrence={"type": 2, "weekly_days": "4", "end_times": 2},
    )  # fmt: skip
    assert days(shifted) == ["2026-11-04", "2026-11-11"]
    assert shifted["start_time"] == "2026-11-04T10:00:00Z"  # the series starts at its first real occurrence
    last_friday = await create(
        zoom, staff, type=8, start_time="2026-11-01T10:00:00Z", timezone="UTC",
        recurrence={"type": 3, "monthly_week": -1, "monthly_week_day": 6, "end_times": 2},
    )  # fmt: skip
    assert days(last_friday) == ["2026-11-27", "2026-12-25"]
    clipped = await create(
        zoom, staff, type=8, start_time="2026-01-31T10:00:00Z", timezone="UTC",
        recurrence={"type": 3, "monthly_day": 31, "end_times": 3},
    )  # fmt: skip
    assert days(clipped) == ["2026-01-31", "2026-02-28", "2026-03-31"]
