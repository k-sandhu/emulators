"""Regressions for scope, pagination, state transitions and REST/webhook wire differences."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.providers.zoom.conftest import API, HOST, V2, double_encode, token_for
from tests.providers.zoom.test_zoom_meetings import create
from tests.providers.zoom.test_zoom_webhooks import SECRET, URL, Receiver, action


async def test_cursor_is_reusable_but_cannot_cross_users_or_expire_silently(
    zoom: httpx.AsyncClient, host: dict[str, str], staff: dict[str, str], ctx: Any
) -> None:
    url = V2 + "/users/me/meetings"
    page = (await zoom.get(url, headers=host, params={"page_size": 1})).json()
    params = {"page_size": 1, "next_page_token": page["next_page_token"]}
    first = await zoom.get(url, headers=host, params=params)
    again = await zoom.get(url, headers=host, params=params)
    assert first.status_code == again.status_code == 200
    assert first.json()["meetings"] == again.json()["meetings"]
    wrong_user = await zoom.get(url, headers=staff, params=params)
    assert wrong_user.status_code == 400 and wrong_user.json()["code"] == 300
    ctx.clock.advance(timedelta(seconds=901))
    expired = await zoom.get(url, headers=host, params=params)
    assert expired.status_code == 400 and expired.json()["code"] == 300


async def test_cursor_cannot_be_forged_or_survive_reset(zoom: httpx.AsyncClient, s2s: dict[str, str]) -> None:
    response = await zoom.get(V2 + "/users?page_size=1", headers=s2s)
    cursor = response.json()["next_page_token"]
    invalid = await zoom.get(V2 + "/users", headers=s2s, params={"next_page_token": cursor + "tampered"})
    assert invalid.status_code == 400
    await zoom.post(API + "/_emulator/reset")
    after = await zoom.get(V2 + "/users", headers=await token_for(zoom), params={"next_page_token": cursor})
    assert after.status_code == 400


async def test_meeting_filters_follow_scheduled_times_and_exclude_instant_meetings(
    zoom: httpx.AsyncClient, staff: dict[str, str]
) -> None:
    past = await create(zoom, staff, start_time="2026-10-15T10:00:00Z", duration=30)
    future = await create(zoom, staff, start_time="2026-11-15T10:00:00Z")
    instant = await create(zoom, staff, type=1)
    previous = (await zoom.get(V2 + "/users/me/meetings?type=previous_meetings", headers=staff)).json()
    upcoming = (await zoom.get(V2 + "/users/me/meetings?type=upcoming", headers=staff)).json()
    assert {m["id"] for m in previous["meetings"]} == {past["id"]}
    assert {m["id"] for m in upcoming["meetings"]} == {future["id"]}
    scheduled = (await zoom.get(V2 + "/users/me/meetings", headers=staff)).json()
    assert instant["id"] not in {m["id"] for m in scheduled["meetings"]}


async def test_meeting_date_window_honors_timezone(zoom: httpx.AsyncClient, staff: dict[str, str]) -> None:
    meeting = await create(zoom, staff, start_time="2026-11-02T01:00:00Z")
    response = await zoom.get(
        V2 + "/users/me/meetings",
        headers=staff,
        params={
            "from": "2026-11-01",
            "to": "2026-11-01",
            "timezone": "America/Los_Angeles",
        },
    )
    assert [m["id"] for m in response.json()["meetings"]] == [meeting["id"]]


async def test_type_update_and_single_occurrence_settings_are_persisted(
    zoom: httpx.AsyncClient, staff: dict[str, str], ctx: Any
) -> None:
    meeting = await create(zoom, staff, type=3)
    url = f"{V2}/meetings/{meeting['id']}"
    changed = await zoom.patch(
        url,
        headers=staff,
        json={
            "type": 8,
            "start_time": "2026-11-01T12:00:00Z",
            "recurrence": {"type": 1, "end_times": 60},
        },
    )
    assert changed.status_code == 204
    recurring = (await zoom.get(url, headers=staff)).json()
    assert recurring["type"] == 8 and len(recurring["occurrences"]) == 60
    oid = recurring["occurrences"][0]["occurrence_id"]
    response = await zoom.patch(
        url,
        headers=staff,
        params={"occurrence_id": oid},
        json={
            "agenda": "Only this occurrence",
            "settings": {"waiting_room": True},
        },
    )
    assert response.status_code == 204
    occurrence = (await zoom.get(url, headers=staff, params={"occurrence_id": oid})).json()
    assert occurrence["agenda"] == "Only this occurrence" and occurrence["settings"]["waiting_room"] is True
    other = (
        await zoom.get(
            url, headers=staff, params={"occurrence_id": recurring["occurrences"][1]["occurrence_id"]}
        )
    ).json()
    assert other["agenda"] == "" and other["settings"]["waiting_room"] is False
    assert (await zoom.patch(url, headers=staff, json={"type": 2})).status_code == 204
    assert len(ctx.store.collection("meetings").get(str(meeting["id"]))["occurrences"]) == 1


async def test_create_passcode_generation_and_supported_special_characters(
    zoom: httpx.AsyncClient, staff: dict[str, str]
) -> None:
    response = await create(zoom, staff, password="A!#123")
    assert response["password"] == "A!#123"
    no_default = await create(zoom, staff, default_password=False)
    assert no_default["password"] == ""


async def test_registrants_cannot_be_deleted_via_another_hosts_meeting(
    zoom: httpx.AsyncClient, host: dict[str, str], staff: dict[str, str]
) -> None:
    first = await create(zoom, host, settings={"approval_type": 0})
    second = await create(zoom, staff, settings={"approval_type": 0})
    reg = (
        await zoom.post(
            f"{V2}/meetings/{first['id']}/registrants",
            headers=host,
            json={"email": "guest@client.example", "first_name": "Guest"},
        )
    ).json()["registrant_id"]
    denied = await zoom.delete(f"{V2}/meetings/{second['id']}/registrants/{reg}", headers=staff)
    assert denied.status_code == 404
    original = (await zoom.get(f"{V2}/meetings/{first['id']}/registrants", headers=host)).json()
    assert original["registrants"][0]["id"] == reg


async def test_registrant_status_batch_does_not_partially_apply_on_failure(
    zoom: httpx.AsyncClient, staff: dict[str, str]
) -> None:
    meeting = await create(zoom, staff, settings={"approval_type": 1})
    base = f"{V2}/meetings/{meeting['id']}/registrants"
    reg = (
        await zoom.post(base, headers=staff, json={"email": "guest@client.example", "first_name": "Guest"})
    ).json()["registrant_id"]
    response = await zoom.put(
        base + "/status",
        headers=staff,
        json={
            "action": "approve",
            "registrants": [{"id": reg}, {"id": "missing"}],
        },
    )
    assert response.status_code == 404
    pending = (await zoom.get(base + "?status=pending", headers=staff)).json()
    assert pending["total_records"] == 1


async def test_group_granular_scopes_match_each_operation(zoom: httpx.AsyncClient, ctx: Any) -> None:
    async def auth(scope: str) -> dict[str, str]:
        return await token_for(zoom, scopes=[scope + ":admin"])

    created = await zoom.post(V2 + "/groups", json={"name": "Audit"}, headers=await auth("group:write:group"))
    assert created.status_code == 201
    gid = created.json()["id"]
    base = V2 + "/groups/" + gid
    response = await zoom.patch(base, json={"name": "Renamed"}, headers=await auth("group:update:group"))
    assert response.status_code == 204
    added = await zoom.post(
        base + "/members", json={"members": [{"email": HOST}]}, headers=await auth("group:write:member")
    )
    assert added.status_code == 201
    members = await zoom.get(base + "/members", headers=await auth("group:read:list_members"))
    assert members.status_code == 200 and members.json()["total_records"] == 1
    zid = members.json()["members"][0]["id"]
    denied = await zoom.post(
        base + "/members", json={"members": [{"email": HOST}]}, headers=await auth("group:read:list_members")
    )
    assert denied.status_code == 401 and denied.json()["code"] == 4700
    removed = await zoom.delete(base + "/members/" + zid, headers=await auth("group:delete:member"))
    assert removed.status_code == 204
    assert gid not in ctx.store.collection("users").get(zid)["group_ids"]
    assert (await zoom.delete(base, headers=await auth("group:delete:group"))).status_code == 204


async def test_group_batch_and_delegated_group_assignment_cannot_change_membership_on_failure(
    zoom: httpx.AsyncClient, s2s: dict[str, str], staff: dict[str, str], ctx: Any
) -> None:
    group = (await zoom.post(V2 + "/groups", headers=s2s, json={"name": "Audit"})).json()
    url = f"{V2}/groups/{group['id']}/members"
    response = await zoom.post(url, headers=s2s, json={"members": [{"email": HOST}, {"id": "missing"}]})
    assert response.status_code == 404
    assert (await zoom.get(url, headers=s2s)).json()["total_records"] == 0
    denied = await zoom.patch(V2 + "/users/me", headers=staff, json={"group_id": group["id"]})
    assert denied.status_code == 403
    assert ctx.store.collection("groups").get(group["id"])["_member_ids"] == []


async def test_recording_rest_type_trash_date_filter_and_permanent_deletion(
    zoom: httpx.AsyncClient, s2s: dict[str, str], ctx: Any
) -> None:
    rec = ctx.store.collection("recordings").values()[0]
    base = f"{V2}/meetings/{double_encode(rec['id'])}/recordings"
    response = await zoom.get(base, headers=s2s)
    assert response.json()["type"] == str(rec["type"])
    fid = response.json()["recording_files"][0]["id"]
    assert (await zoom.delete(base + "/" + fid, headers=s2s)).status_code == 204
    assert (await zoom.delete(base + "/" + fid + "?action=delete", headers=s2s)).status_code == 204
    assert all(f["id"] != fid for f in rec["recording_files"])
    assert (await zoom.delete(base, headers=s2s)).status_code == 204
    listed = await zoom.get(
        f"{V2}/users/{rec['host_id']}/recordings?trash=true&from=2026-12-01&to=2026-12-02", headers=s2s
    )
    assert rec["id"] in {r["uuid"] for r in listed.json()["meetings"]}
    assert (await zoom.delete(base + "?action=delete", headers=s2s)).status_code == 204
    assert (await zoom.get(base, headers=s2s)).status_code == 404


@pytest.mark.parametrize("status", [302, 400, 401, 403, 404, 429])
async def test_webhooks_never_retry_redirects_or_client_errors(
    zoom: httpx.AsyncClient, staff: dict[str, str], status: int
) -> None:
    receiver = Receiver(status=status)
    zoom.hub.webhook_transport = httpx.ASGITransport(app=receiver.app)  # type: ignore[attr-defined]
    await action(zoom, "configure_webhook", url=URL, secret_token=SECRET)
    await create(zoom, staff, topic="Retry audit")
    deliveries = (await zoom.get(API + "/_emulator/webhooks")).json()["deliveries"]
    assert len(receiver.events) == 1 and all(receiver.signatures_ok)
    assert deliveries[0]["attempts"] == 1 and deliveries[0]["status"] == "failed"


async def test_recording_and_report_granular_scopes(zoom: httpx.AsyncClient, ctx: Any) -> None:
    rec = ctx.store.collection("recordings").values()[0]
    uuid = double_encode(rec["id"])
    base = f"{V2}/meetings/{uuid}/recordings"
    for scope, path in [
        ("cloud_recording:read:recording_settings:admin", base + "/settings"),
        (
            "report:read:user:admin",
            f"{V2}/report/users/{rec['host_id']}/meetings?from=2026-09-01&to=2026-09-30",
        ),
        ("report:read:list_users:admin", f"{V2}/report/users?from=2026-09-01&to=2026-09-30"),
        ("meeting:read:list_upcoming_meetings:admin", f"{V2}/users/{rec['host_id']}/upcoming_meetings"),
    ]:
        response = await zoom.get(path, headers=await token_for(zoom, scopes=[scope]))
        assert response.status_code == 200, (scope, response.text)
    updated = await zoom.patch(
        base + "/settings",
        json={"viewer_download": False},
        headers=await token_for(zoom, scopes=["cloud_recording:update:recording_settings:admin"]),
    )
    assert updated.status_code == 204
    s2s = await token_for(zoom)
    assert (await zoom.delete(base, headers=s2s)).status_code == 204
    recovered = await zoom.put(
        base + "/status",
        json={"action": "recover"},
        headers=await token_for(zoom, scopes=["cloud_recording:update:recover_meeting_recordings:admin"]),
    )
    assert recovered.status_code == 204
    fid = rec["recording_files"][0]["id"]
    assert (await zoom.delete(base + "/" + fid, headers=s2s)).status_code == 204
    recovered_file = await zoom.put(
        base + "/" + fid + "/status",
        json={"action": "recover"},
        headers=await token_for(zoom, scopes=["cloud_recording:update:recover_single_recording:admin"]),
    )
    assert recovered_file.status_code == 204


async def test_recording_dates_require_exact_iso_dates(zoom: httpx.AsyncClient, s2s: dict[str, str]) -> None:
    response = await zoom.get(V2 + "/users/me/recordings?from=2026-10-15junk&to=2026-10-16", headers=s2s)
    assert response.status_code == 400 and response.json()["code"] == 300


async def test_one_second_meeting_recording_has_consistent_times(
    zoom: httpx.AsyncClient, staff: dict[str, str]
) -> None:
    meeting = await create(zoom, staff)
    await action(zoom, "start_meeting", meeting_id=meeting["id"])
    await action(zoom, "end_meeting", meeting_id=meeting["id"], duration_minutes=0)
    recordings = (await zoom.get(f"{V2}/meetings/{meeting['id']}/recordings", headers=staff)).json()
    assert all(f["recording_start"] < f["recording_end"] for f in recordings["recording_files"])


async def test_dashboard_granular_scopes_live_past_selectors_and_duration(
    zoom: httpx.AsyncClient, s2s: dict[str, str], ctx: Any
) -> None:
    rec = ctx.store.collection("recordings").values()[0]
    uuid = double_encode(rec["id"])
    base = f"{V2}/metrics/meetings/{uuid}"
    for scope, suffix in [
        ("dashboard:read:meeting:admin", ""),
        ("dashboard:read:list_meeting_participants:admin", "/participants"),
    ]:
        auth = await token_for(zoom, scopes=[scope])
        assert (await zoom.get(base + suffix, headers=auth)).status_code == 404
        response = await zoom.get(base + suffix + "?type=past", headers=auth)
        assert response.status_code == 200
    detail = (await zoom.get(base + "?type=past", headers=s2s)).json()
    inst = ctx.store.collection("instances").get(rec["id"])
    seconds = int(
        (
            datetime.fromisoformat(inst["end_time"]) - datetime.fromisoformat(inst["start_time"])
        ).total_seconds()
    )
    assert detail["duration"] == f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    start = inst["start_time"].replace("T", " ").removesuffix("Z")
    end = inst["end_time"].replace("T", " ").removesuffix("Z")
    listed = await zoom.get(
        V2 + "/metrics/meetings",
        params={"type": "past", "from": start, "to": end},
        headers=await token_for(zoom, scopes=["dashboard:read:list_meetings:admin"]),
    )
    assert listed.status_code == 200 and rec["id"] in {r["uuid"] for r in listed.json()["meetings"]}
    single = (
        await zoom.get(V2 + "/metrics/meetings?type=pastOne&from=2026-09-01&to=2026-09-30", headers=s2s)
    ).json()
    assert single["meetings"] == []  # every seeded meeting has multiple participants


async def test_granular_delete_registrant_and_zero_ttl_download_token(
    zoom: httpx.AsyncClient, staff: dict[str, str], ctx: Any
) -> None:
    meeting = await create(zoom, staff, settings={"approval_type": 0})
    base = f"{V2}/meetings/{meeting['id']}/registrants"
    reg = (
        await zoom.post(base, headers=staff, json={"email": "guest@client.example", "first_name": "Guest"})
    ).json()["registrant_id"]
    deleted = await zoom.delete(
        base + "/" + reg, headers=await token_for(zoom, scopes=["meeting:delete:registrant:admin"])
    )
    assert deleted.status_code == 204
    rec = ctx.store.collection("recordings").values()[0]
    response = await zoom.get(
        f"{V2}/meetings/{double_encode(rec['id'])}/recordings?include_fields=download_access_token&ttl=0",
        headers=await token_for(zoom),
    )
    assert response.status_code == 200
    download = await zoom.get(
        rec["recording_files"][0]["download_url"],
        headers={"Authorization": "Bearer " + response.json()["download_access_token"]},
    )
    assert download.status_code == 401


async def test_invalid_boolean_query_does_not_silently_become_false(
    zoom: httpx.AsyncClient, s2s: dict[str, str]
) -> None:
    response = await zoom.get(V2 + "/users/me/recordings?trash=garbage", headers=s2s)
    assert response.status_code == 400 and response.json()["code"] == 300


async def test_recording_settings_password_and_topic_are_consistent(
    zoom: httpx.AsyncClient, s2s: dict[str, str], ctx: Any
) -> None:
    rec = ctx.store.collection("recordings").values()[0]
    base = f"{V2}/meetings/{double_encode(rec['id'])}/recordings"
    before = (await zoom.get(base, headers=s2s)).json()
    settings = (await zoom.get(base + "/settings", headers=s2s)).json()
    assert settings["password"] == before["password"]
    changed = await zoom.patch(
        base + "/settings",
        headers=s2s,
        json={"password": "testpass9", "topic": "Recording renamed", "auto_delete": True},
    )
    assert changed.status_code == 204
    after = (await zoom.get(base, headers=s2s)).json()
    assert after["topic"] == "Recording renamed" and after["password"] == "testpass9"
    assert after["recording_play_passcode"] != before["recording_play_passcode"]
    assert (await zoom.get(base + "/settings", headers=s2s)).json()["auto_delete"] is True


async def test_deleted_occurrences_cannot_start_and_blank_guest_names_cannot_crash_processing(
    zoom: httpx.AsyncClient, staff: dict[str, str], ctx: Any
) -> None:
    meeting = await create(
        zoom, staff, type=8, start_time="2026-11-01T12:00:00Z", recurrence={"type": 1, "end_times": 2}
    )
    oid = meeting["occurrences"][0]["occurrence_id"]
    assert (
        await zoom.delete(f"{V2}/meetings/{meeting['id']}", headers=staff, params={"occurrence_id": oid})
    ).status_code == 204
    cannot_start = await zoom.post(
        API + "/_emulator/actions/start_meeting", json={"meeting_id": meeting["id"], "occurrence_id": oid}
    )
    assert cannot_start.status_code == 404
    await action(zoom, "start_meeting", meeting_id=meeting["id"], participants=[{"name": " "}])
    ended = await action(zoom, "end_meeting", meeting_id=meeting["id"], duration_minutes=1)
    inst = ctx.store.collection("instances").get(ended["uuid"])
    assert all(0 <= cue["start_s"] < cue["end_s"] <= 55 for cue in inst["transcript"])


@pytest.mark.parametrize("transfer", [False, True])
async def test_deleting_user_removes_or_transfers_owned_assets(
    zoom: httpx.AsyncClient, s2s: dict[str, str], host: dict[str, str], ctx: Any, transfer: bool
) -> None:
    user = (await zoom.get(f"{V2}/users/{HOST}", headers=s2s)).json()
    meeting = await create(zoom, host, settings={"approval_type": 0})
    reg = (
        await zoom.post(
            f"{V2}/meetings/{meeting['id']}/registrants",
            headers=host,
            json={"email": "guest@client.example", "first_name": "Guest"},
        )
    ).json()["registrant_id"]
    rec = next(r for r in ctx.store.collection("recordings") if r["host_id"] == user["id"])
    mid, uuid = meeting["id"], rec["id"]
    params: dict[str, Any] = {"action": "delete"}
    if transfer:
        params.update(
            transfer_email="maya.chen@halcyonadvisory.example",
            transfer_meeting="true",
            transfer_recording="true",
        )
    deleted = await zoom.delete(f"{V2}/users/{user['id']}", headers=s2s, params=params)
    assert deleted.status_code == 204
    assert all(
        r["host_id"] != user["id"]
        for name in ("meetings", "instances", "recordings")
        for r in ctx.store.collection(name)
    )
    if transfer:
        moved = (await zoom.get(f"{V2}/meetings/{mid}", headers=s2s)).json()
        assert moved["host_email"] == "maya.chen@halcyonadvisory.example"
        assert (await zoom.get(f"{V2}/meetings/{double_encode(uuid)}/recordings", headers=s2s)).json()[
            "host_email"
        ] == moved["host_email"]
        assert ctx.store.collection("registrants").get(reg) is not None
    else:
        assert (await zoom.get(f"{V2}/meetings/{mid}", headers=s2s)).status_code == 404
        assert (
            await zoom.get(f"{V2}/meetings/{double_encode(uuid)}/recordings", headers=s2s)
        ).status_code == 404
        assert ctx.store.collection("registrants").get(reg) is None


async def test_invalid_user_asset_transfer_is_atomic(
    zoom: httpx.AsyncClient, s2s: dict[str, str], ctx: Any
) -> None:
    before = ctx.store.snapshot()
    response = await zoom.delete(f"{V2}/users/{HOST}?action=delete&transfer_meeting=true", headers=s2s)
    assert response.status_code == 400 and ctx.store.snapshot() == before


async def test_password_only_meeting_update_emits_updated_event(
    zoom: httpx.AsyncClient, staff: dict[str, str]
) -> None:
    meeting = await create(zoom, staff)
    receiver = Receiver()
    zoom.hub.webhook_transport = httpx.ASGITransport(app=receiver.app)  # type: ignore[attr-defined]
    await action(zoom, "configure_webhook", url=URL, secret_token=SECRET, events=["meeting.updated"])
    response = await zoom.patch(f"{V2}/meetings/{meeting['id']}", headers=staff, json={"password": "new123"})
    assert response.status_code == 204
    assert len(receiver.events) == 1
    assert receiver.events[0]["payload"]["object"]["password"] == "new123"
