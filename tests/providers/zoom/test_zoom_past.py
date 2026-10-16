"""Past meetings, UUID path encoding, the report API and the dashboard (metrics) API."""

from __future__ import annotations

from typing import Any

import httpx

from emulators.core.hub import EmulatorHub
from tests.providers.zoom.conftest import (
    API,
    BLUEFIN_ID,
    HOST,
    V2,
    double_encode,
    instances_of,
    single_encode,
    special_uuids,
    token_for,
)

DETAIL_KEYS = [
    "uuid", "id", "host_id", "type", "topic", "user_name", "user_email", "start_time", "end_time", "duration",
    "total_minutes", "participants_count", "dept", "source",
]  # fmt: skip


async def test_past_meeting_details_match_the_world(
    zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any
) -> None:
    first = instances_of(ctx, BLUEFIN_ID)[0]
    r = await zoom.get(f"{V2}/past_meetings/{single_encode(first['id'])}", headers=host)
    # the first instance's UUID starts with "/": a single-encoded path is still accepted
    assert r.status_code == 200, r.text
    body = r.json()
    assert list(body) == DETAIL_KEYS
    assert body["uuid"] == first["id"] and body["id"] == BLUEFIN_ID and body["type"] == 8
    assert body["topic"] == "Bluefin Seafood — Status Update"
    assert body["user_name"] == "Benjamin Adler" and body["user_email"] == HOST and body["source"] == "Zoom"
    assert body["dept"] == "Advisory" and body["participants_count"] == 6
    assert body["start_time"].endswith("Z") and body["end_time"] > body["start_time"]
    assert 30 <= body["duration"] <= 50 and body["total_minutes"] >= body["duration"]


async def test_uuid_path_encoding_rules(
    zoom: httpx.AsyncClient, host: dict[str, str], maya: dict[str, str], ctx: Any
) -> None:
    leading, double = special_uuids(ctx)
    headers = {leading: host, double: host}
    for uuid in (leading, double):
        once = await zoom.get(f"{V2}/past_meetings/{single_encode(uuid)}", headers=headers[uuid])
        twice = await zoom.get(f"{V2}/past_meetings/{double_encode(uuid)}", headers=headers[uuid])
        assert once.status_code == twice.status_code == 200, (uuid, once.text, twice.text)
        assert once.json()["uuid"] == twice.json()["uuid"] == uuid
    plain = next(
        i
        for i in ctx.store.collection("instances")
        if "/" not in i["id"]
        and i["host_id"] == next(u["id"] for u in ctx.store.collection("users") if u["email"] == HOST)
    )
    for form in (plain["id"], single_encode(plain["id"]), double_encode(plain["id"])):
        assert (await zoom.get(f"{V2}/past_meetings/{form}", headers=host)).json()["uuid"] == plain["id"]
    # a meeting id works too (most recent instance)
    latest = (await zoom.get(f"{V2}/past_meetings/{BLUEFIN_ID}", headers=host)).json()
    assert latest["uuid"] == instances_of(ctx, BLUEFIN_ID)[-1]["id"]
    # participants are addressable through the double-encoded UUID as well
    parts = await zoom.get(f"{V2}/past_meetings/{double_encode(leading)}/participants", headers=host)
    assert parts.status_code == 200 and parts.json()["total_records"] >= 2
    _ = maya


async def test_unknown_unfinished_and_forbidden_past_meetings(
    zoom: httpx.AsyncClient,
    host: dict[str, str],
    maya: dict[str, str],
    staff: dict[str, str],
    admin: dict[str, str],
    s2s: dict[str, str],
    ctx: Any,
) -> None:
    first = instances_of(ctx, BLUEFIN_ID)[0]["id"]
    missing = await zoom.get(f"{V2}/past_meetings/AAAAAAAAAAAAAAAAAAAAAA==", headers=host)
    assert missing.status_code == 404
    assert missing.json() == {"code": 3001, "message": "Meeting does not exist: AAAAAAAAAAAAAAAAAAAAAA==."}
    meeting = ctx.store.collection("meetings").get(str(BLUEFIN_ID))
    not_yet = next(
        o["_uuid"] for o in meeting["occurrences"] if not o.get("_happened") and o["status"] == "available"
    )
    assert (await zoom.get(f"{V2}/past_meetings/{double_encode(not_yet)}", headers=host)).status_code == 404
    for who in (staff, maya):  # not the host, even though Maya is an alternative host
        denied = await zoom.get(f"{V2}/past_meetings/{double_encode(first)}", headers=who)
        assert denied.status_code == 403 and denied.json()["code"] == 200
    for who in (admin, s2s):
        assert (await zoom.get(f"{V2}/past_meetings/{double_encode(first)}", headers=who)).status_code == 200
    narrow = await token_for(zoom, None, scopes="user:read:admin")
    assert (await zoom.get(f"{V2}/past_meetings/{double_encode(first)}", headers=narrow)).status_code == 401


async def test_past_meeting_instances(
    zoom: httpx.AsyncClient, host: dict[str, str], staff: dict[str, str], ctx: Any
) -> None:
    body = (await zoom.get(f"{V2}/past_meetings/{BLUEFIN_ID}/instances", headers=host)).json()
    assert list(body) == ["meetings"] and len(body["meetings"]) == 3
    assert [m["uuid"] for m in body["meetings"]] == [i["id"] for i in instances_of(ctx, BLUEFIN_ID)]
    assert body["meetings"] == sorted(body["meetings"], key=lambda m: m["start_time"])
    assert all(set(m) == {"start_time", "uuid"} for m in body["meetings"])
    assert (await zoom.get(f"{V2}/past_meetings/{BLUEFIN_ID}/instances", headers=staff)).status_code == 403
    assert (await zoom.get(f"{V2}/past_meetings/12345/instances", headers=host)).status_code == 404
    upcoming_only = ctx.store.collection("meetings").get(
        "6048581623"
    )  # Brightwater findings review, not yet held
    assert upcoming_only is not None
    admin = await token_for(zoom)
    assert (await zoom.get(f"{V2}/past_meetings/6048581623/instances", headers=admin)).json() == {
        "meetings": []
    }


async def test_past_participants_shape_guests_and_paging(
    zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any
) -> None:
    uuid = instances_of(ctx, BLUEFIN_ID)[1]["id"]
    body = (await zoom.get(f"{V2}/past_meetings/{double_encode(uuid)}/participants", headers=host)).json()
    assert list(body) == ["page_count", "page_size", "total_records", "next_page_token", "participants"]
    assert body["total_records"] == 5 and body["page_size"] == 30
    keys = [
        "id",
        "name",
        "user_id",
        "registrant_id",
        "user_email",
        "join_time",
        "leave_time",
        "duration",
        "failover",
        "status",
        "internal_user",
    ]
    for p in body["participants"]:
        assert list(p) == keys and p["status"] == "in_meeting" and p["failover"] is False
        assert isinstance(p["duration"], int) and p["duration"] > 0 and p["join_time"] < p["leave_time"]
        assert len(p["id"]) == 22
    internal = [p for p in body["participants"] if p["internal_user"]]
    guests = [p for p in body["participants"] if not p["internal_user"]]
    assert internal and guests
    assert all(
        len(p["user_id"]) == 16
        and p["user_id"].isdigit()
        and p["user_email"].endswith("@halcyonadvisory.example")
        for p in internal
    )
    # guests of the host's account are anonymous in Zoom's data: no user id, no email
    assert all(p["user_id"] == "" and p["user_email"] == "" and p["name"] for p in guests)
    assert [p["join_time"] for p in body["participants"]] == sorted(
        p["join_time"] for p in body["participants"]
    )
    paged = (
        await zoom.get(
            f"{V2}/past_meetings/{double_encode(uuid)}/participants", headers=host, params={"page_size": 2}
        )
    ).json()
    assert len(paged["participants"]) == 2 and paged["page_count"] == 3 and paged["next_page_token"]


async def test_guest_emails_can_be_exposed_with_a_setting() -> None:
    hub = EmulatorHub(
        providers=["zoom"], anchor="2026-10-16", settings={"zoom": {"guest_emails": "1"}}, webhook_mode="sync"
    )
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        headers = await token_for(client, None)
        ctx = hub.context("zoom")
        uuid = instances_of(ctx, BLUEFIN_ID)[0]["id"]
        rows = (
            await client.get(f"{V2}/past_meetings/{double_encode(uuid)}/participants", headers=headers)
        ).json()["participants"]
    assert any(not p["internal_user"] and p["user_email"].endswith(".example") for p in rows)


async def test_report_api_meeting_and_participants(
    zoom: httpx.AsyncClient, s2s: dict[str, str], host: dict[str, str], ctx: Any
) -> None:
    uuid = instances_of(ctx, BLUEFIN_ID)[0]["id"]
    r = (await zoom.get(f"{V2}/report/meetings/{double_encode(uuid)}", headers=s2s)).json()
    assert set(DETAIL_KEYS) <= set(r) and r["uuid"] == uuid
    assert r["has_recording"] is True and r["has_voip"] is True and r["user_type"] == "Licensed"
    assert r["custom_keys"] == [] and r["tracking_fields"] == []
    parts = (
        await zoom.get(
            f"{V2}/report/meetings/{double_encode(uuid)}/participants",
            headers=s2s,
            params={"include_fields": "registrant_id"},
        )
    ).json()
    assert parts["total_records"] == 6
    p = parts["participants"][0]
    assert {
        "customer_key",
        "attentiveness_score",
        "participant_user_id",
        "registrant_id",
        "failover",
        "status",
    } <= set(p)
    assert p["attentiveness_score"] == "" and p["customer_key"] == ""
    no_reg = (await zoom.get(f"{V2}/report/meetings/{double_encode(uuid)}/participants", headers=s2s)).json()[
        "participants"
    ][0]
    assert "registrant_id" not in no_reg
    # the report API is account-admin territory
    assert (await zoom.get(f"{V2}/report/meetings/{double_encode(uuid)}", headers=host)).status_code == 403


async def test_report_user_meetings_daily_and_users(zoom: httpx.AsyncClient, s2s: dict[str, str]) -> None:
    body = (
        await zoom.get(
            f"{V2}/report/users/{HOST}/meetings",
            headers=s2s,
            params={"from": "2026-09-01", "to": "2026-09-30"},
        )
    ).json()
    assert body["from"] == "2026-09-01" and body["to"] == "2026-09-30"
    assert body["total_records"] == 10 and all(m["user_email"] == HOST for m in body["meetings"])
    required = await zoom.get(
        f"{V2}/report/users/{HOST}/meetings", headers=s2s, params={"from": "2026-09-01"}
    )
    assert required.status_code == 400 and required.json()["errors"][0]["field"] == "to"
    too_long = await zoom.get(
        f"{V2}/report/users/{HOST}/meetings", headers=s2s, params={"from": "2026-08-01", "to": "2026-10-01"}
    )
    assert too_long.status_code == 400 and too_long.json()["code"] == 300
    daily = (await zoom.get(f"{V2}/report/daily", headers=s2s, params={"year": 2026, "month": 9})).json()
    assert daily["year"] == 2026 and daily["month"] == 9 and len(daily["dates"]) == 30
    sep9 = next(d for d in daily["dates"] if d["date"] == "2026-09-09")
    assert sep9["meetings"] == 6 and sep9["participants"] >= 25 and sep9["meeting_minutes"] > sep9["meetings"]
    users = (
        await zoom.get(f"{V2}/report/users", headers=s2s, params={"from": "2026-09-01", "to": "2026-09-30"})
    ).json()
    assert {u["email"] for u in users["users"]} >= {HOST}
    ben = next(u for u in users["users"] if u["email"] == HOST)
    assert ben["meetings"] == 10 and ben["meeting_minutes"] > 0
    inactive = (
        await zoom.get(
            f"{V2}/report/users",
            headers=s2s,
            params={"from": "2026-09-01", "to": "2026-09-30", "type": "inactive"},
        )
    ).json()
    assert HOST not in {u["email"] for u in inactive["users"]}


async def test_dashboard_metrics(
    zoom: httpx.AsyncClient, s2s: dict[str, str], host: dict[str, str], ctx: Any
) -> None:
    past = (
        await zoom.get(
            f"{V2}/metrics/meetings",
            headers=s2s,
            params={"type": "past", "from": "2026-09-09", "to": "2026-09-09"},
        )
    ).json()
    assert past["total_records"] == 6
    item = past["meetings"][0]
    assert {"uuid", "id", "topic", "host", "email", "user_type", "start_time", "end_time", "duration", "participants",
            "has_voip", "has_video", "has_recording", "in_room_participants", "dept"} <= set(item)  # fmt: skip
    assert len(item["duration"]) == 5 and item["duration"][2] == ":"
    assert (await zoom.get(f"{V2}/metrics/meetings", headers=s2s)).json()["total_records"] == 0  # live
    uuid = instances_of(ctx, BLUEFIN_ID)[0]["id"]
    assert (await zoom.get(f"{V2}/metrics/meetings/{double_encode(uuid)}", headers=s2s)).json()[
        "uuid"
    ] == uuid
    parts = (await zoom.get(f"{V2}/metrics/meetings/{double_encode(uuid)}/participants", headers=s2s)).json()
    assert (
        parts["total_records"] == 6
        and "device" in parts["participants"][0]
        and "ip_address" in parts["participants"][0]
    )
    assert (await zoom.get(f"{V2}/metrics/meetings", headers=host)).status_code == 403


async def test_every_seeded_instance_mirrors_its_world_meeting(
    zoom: httpx.AsyncClient, s2s: dict[str, str], ctx: Any
) -> None:
    world = zoom.hub.world  # type: ignore[attr-defined]
    checked = 0
    for inst in ctx.store.collection("instances"):
        meeting = next(m for m in world.meetings if m.id == inst["_world_meeting_id"])
        assert meeting.platform == "zoom" and meeting.happened and not meeting.cancelled
        body = (
            await zoom.get(
                f"{V2}/past_meetings/{double_encode(inst['id'])}/participants",
                headers=s2s,
                params={"page_size": 300},
            )
        ).json()
        assert [p["name"] for p in body["participants"]] == [
            world.person(pt.person_id).display_name
            for pt in sorted(meeting.participations, key=lambda x: x.joined)
        ]
        assert body["total_records"] == len(meeting.participations)
        detail = (await zoom.get(f"{V2}/past_meetings/{double_encode(inst['id'])}", headers=s2s)).json()
        assert detail["user_email"] == world.person(meeting.organizer_id).email
        assert detail["topic"] == meeting.title and detail["participants_count"] == len(
            meeting.participations
        )
        assert bool(inst["recorded"]) == meeting.recorded and bool(inst["transcribed"]) == meeting.transcribed
        checked += 1
    assert checked == 26


async def test_live_meetings_are_not_past_meetings_yet(zoom: httpx.AsyncClient, s2s: dict[str, str]) -> None:
    started = (
        await zoom.post(f"{API}/_emulator/actions/start_meeting", json={"meeting_id": 3507027577})
    ).json()
    live = f"{V2}/past_meetings/{double_encode(started['uuid'])}"
    assert (await zoom.get(live, headers=s2s)).status_code == 404
    assert (await zoom.get(f"{live}/participants", headers=s2s)).status_code == 404
    assert (await zoom.get(f"{V2}/past_meetings/3507027577/instances", headers=s2s)).json()["meetings"][-1][
        "uuid"
    ] != started["uuid"]
    await zoom.post(f"{API}/_emulator/actions/end_meeting", json={"meeting_id": 3507027577, "record": False})
    assert (await zoom.get(live, headers=s2s)).status_code == 200
