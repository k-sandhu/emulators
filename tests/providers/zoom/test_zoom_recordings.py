"""Cloud recordings: listing, lookup by id/UUID, trash/recover/delete, settings, summaries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from emulators.core.clock import Clock
from emulators.core.hub import EmulatorHub
from tests.providers.zoom.conftest import (
    BLUEFIN_ID,
    HOST,
    MAYA,
    ORCHID_ID,
    V2,
    double_encode,
    single_encode,
    special_uuids,
    token_for,
)

SEPT = {"from": "2026-09-01", "to": "2026-09-30"}


def recorded_of(ctx: Any, meeting_id: int) -> list[dict[str, Any]]:
    return sorted(
        (r for r in ctx.store.collection("recordings") if r["meeting_id"] == meeting_id),
        key=lambda r: r["start_time"],
    )


FILE_KEYS = [
    "id", "meeting_id", "recording_start", "recording_end", "file_type", "file_extension", "file_size", "play_url",
    "download_url", "status", "recording_type",
]  # fmt: skip
MEETING_KEYS = [
    "uuid", "id", "account_id", "host_id", "topic", "type", "start_time", "timezone", "host_email", "duration",
    "total_size", "recording_count", "share_url", "recording_files", "password", "recording_play_passcode",
]  # fmt: skip


async def test_list_user_recordings_shape_and_files(zoom: httpx.AsyncClient, host: dict[str, str]) -> None:
    body = (
        await zoom.get(f"{V2}/users/me/recordings", headers=host, params={**SEPT, "page_size": 30})
    ).json()
    assert list(body)[:6] == ["from", "to", "page_count", "page_size", "total_records", "next_page_token"]
    assert body["from"] == "2026-09-01" and body["to"] == "2026-09-30"
    assert body["total_records"] == 8 and body["next_page_token"] == ""
    starts = [m["start_time"] for m in body["meetings"]]
    assert starts == sorted(starts, reverse=True)
    meeting = body["meetings"][0]
    assert list(meeting) == MEETING_KEYS
    assert meeting["account_id"] == zoom.hub.world.org.zoom_account_id  # type: ignore[attr-defined]
    assert meeting["recording_count"] == len(meeting["recording_files"])
    assert meeting["total_size"] == sum(f["file_size"] for f in meeting["recording_files"])
    assert meeting["share_url"].startswith("https://us06web.zoom.us/rec/share/")
    for f in meeting["recording_files"]:
        assert f["status"] == "completed" and f["meeting_id"] == meeting["uuid"] and f["file_size"] > 0
        assert f["download_url"].startswith("https://us06web.zoom.us/rec/download/")
        assert f["recording_start"] <= f["recording_end"]
    by_type = {f["file_type"]: f for f in meeting["recording_files"]}
    assert {"MP4", "M4A", "TRANSCRIPT", "TIMELINE"} <= set(by_type)
    assert (by_type["MP4"]["file_extension"], by_type["MP4"]["recording_type"]) == (
        "MP4",
        "shared_screen_with_speaker_view",
    )
    assert (by_type["M4A"]["file_extension"], by_type["M4A"]["recording_type"]) == ("M4A", "audio_only")
    assert (by_type["TRANSCRIPT"]["file_extension"], by_type["TRANSCRIPT"]["recording_type"]) == (
        "VTT",
        "audio_transcript",
    )
    assert (by_type["TIMELINE"]["file_extension"], by_type["TIMELINE"]["recording_type"]) == (
        "JSON",
        "timeline",
    )
    assert "play_url" in by_type["MP4"] and "play_url" not in by_type["TRANSCRIPT"]


async def test_chat_files_exist_only_for_meetings_with_chat(
    zoom: httpx.AsyncClient, host: dict[str, str]
) -> None:
    body = (
        await zoom.get(f"{V2}/users/me/recordings", headers=host, params={**SEPT, "page_size": 300})
    ).json()
    with_chat = [m for m in body["meetings"] if any(f["file_type"] == "CHAT" for f in m["recording_files"])]
    assert len(with_chat) == 2
    chat = next(f for f in with_chat[0]["recording_files"] if f["file_type"] == "CHAT")
    assert (chat["file_extension"], chat["recording_type"]) == ("TXT", "chat_file")


async def test_range_rules_defaults_and_paging(zoom: httpx.AsyncClient, host: dict[str, str]) -> None:
    too_long = await zoom.get(
        f"{V2}/users/me/recordings", headers=host, params={"from": "2026-08-01", "to": "2026-10-01"}
    )
    assert too_long.status_code == 400 and too_long.json()["code"] == 300
    assert (
        await zoom.get(
            f"{V2}/users/me/recordings", headers=host, params={"from": "2026-10-10", "to": "2026-10-01"}
        )
    ).status_code == 400
    assert (
        await zoom.get(f"{V2}/users/me/recordings", headers=host, params={"from": "soon"})
    ).status_code == 400
    exactly = await zoom.get(
        f"{V2}/users/me/recordings", headers=host, params={"from": "2026-09-01", "to": "2026-10-01"}
    )
    assert exactly.status_code == 200 and exactly.json()["total_records"] == 8
    # empty parameters (as a client template renders them) mean "not provided"
    blank = await zoom.get(
        f"{V2}/users/me/recordings",
        headers=host,
        params={"from": "", "to": "", "page_size": 30, "next_page_token": ""},
    )
    assert blank.status_code == 200
    first = (
        await zoom.get(f"{V2}/users/me/recordings", headers=host, params={**SEPT, "page_size": 3})
    ).json()
    assert len(first["meetings"]) == 3 and first["next_page_token"] and first["page_count"] == 3
    seen = [m["uuid"] for m in first["meetings"]]
    token = first["next_page_token"]
    while token:
        page = (
            await zoom.get(
                f"{V2}/users/me/recordings",
                headers=host,
                params={**SEPT, "page_size": 3, "next_page_token": token},
            )
        ).json()
        seen += [m["uuid"] for m in page["meetings"]]
        token = page["next_page_token"]
    assert len(seen) == len(set(seen)) == 8
    changed = await zoom.get(
        f"{V2}/users/me/recordings",
        headers=host,
        params={
            "from": "2026-09-01",
            "to": "2026-09-15",
            "page_size": 3,
            "next_page_token": first["next_page_token"],
        },
    )
    assert changed.status_code == 400  # a token only works for the query that produced it


async def test_default_window_is_today_to_tomorrow() -> None:
    hub = EmulatorHub(
        providers=["zoom"],
        anchor="2026-10-16",
        clock=Clock(datetime(2026, 10, 16, 12, tzinfo=UTC)),
        webhook_mode="sync",
    )
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        maya = await token_for(client, MAYA)
        body = (await client.get(f"{V2}/users/me/recordings", headers=maya)).json()
    assert (body["from"], body["to"]) == ("2026-10-16", "2026-10-17")
    assert body["meetings"] == []  # the most recent recording was held 15 Oct


async def test_visibility_of_user_recordings(
    zoom: httpx.AsyncClient,
    host: dict[str, str],
    maya: dict[str, str],
    staff: dict[str, str],
    admin: dict[str, str],
    s2s: dict[str, str],
) -> None:
    mine = (await zoom.get(f"{V2}/users/me/recordings", headers=maya, params=SEPT)).json()
    assert {m["host_email"] for m in mine["meetings"]} == {MAYA}
    other = await zoom.get(f"{V2}/users/{HOST}/recordings", headers=maya, params=SEPT)
    assert other.status_code == 403
    assert (await zoom.get(f"{V2}/users/me/recordings", headers=staff, params=SEPT)).json()[
        "total_records"
    ] == 0
    assert (await zoom.get(f"{V2}/users/{HOST}/recordings", headers=admin, params=SEPT)).json()[
        "total_records"
    ] == 8
    assert (await zoom.get(f"{V2}/users/{HOST}/recordings", headers=s2s, params=SEPT)).json()[
        "total_records"
    ] == 8
    narrow = await token_for(zoom, HOST, scopes="meeting:read")
    r = await zoom.get(f"{V2}/users/me/recordings", headers=narrow, params=SEPT)
    assert r.status_code == 401 and r.json()["code"] == 4700
    assert r.json()["message"] == "Invalid access token, does not contain scopes:[recording:read]."
    account = (await zoom.get(f"{V2}/accounts/me/recordings", headers=s2s, params=SEPT)).json()
    assert account["total_records"] == 13  # everyone's recordings in September
    assert (await zoom.get(f"{V2}/accounts/me/recordings", headers=host, params=SEPT)).status_code == 403
    assert (await zoom.get(f"{V2}/accounts/nope/recordings", headers=s2s, params=SEPT)).status_code == 404


async def test_get_meeting_recordings_by_uuid_in_every_encoding(
    zoom: httpx.AsyncClient, host: dict[str, str], maya: dict[str, str], ctx: Any
) -> None:
    leading, double = special_uuids(ctx)
    plain = recorded_of(ctx, ORCHID_ID)[0]["id"]
    assert "/" not in plain
    for uuid, who in ((leading, host), (double, host), (plain, host)):
        for form in (double_encode(uuid), single_encode(uuid)) + ((uuid,) if uuid == plain else ()):
            r = await zoom.get(f"{V2}/meetings/{form}/recordings", headers=who)
            assert r.status_code == 200, (form, r.text)
            assert r.json()["uuid"] == uuid and list(r.json()) == MEETING_KEYS
    by_id = (await zoom.get(f"{V2}/meetings/{BLUEFIN_ID}/recordings", headers=host)).json()
    assert by_id["uuid"] == recorded_of(ctx, BLUEFIN_ID)[-1]["id"]  # latest recorded instance
    assert by_id["id"] == BLUEFIN_ID and by_id["type"] == "8"
    assert (
        await zoom.get(f"{V2}/meetings/{double_encode(leading)}/recordings", headers=maya)
    ).status_code == 403


async def test_recordings_404_and_download_access_token(
    zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any
) -> None:
    missing = await zoom.get(
        f"{V2}/meetings/{double_encode('AAAAAAAAAAAAAAAAAAAAAA==')}/recordings", headers=host
    )
    assert missing.status_code == 404
    assert missing.json() == {"code": 3301, "message": "This recording does not exist."}
    assert (
        await zoom.get(f"{V2}/meetings/6048581623/recordings", headers=await token_for(zoom))
    ).status_code == 404
    uuid = recorded_of(ctx, BLUEFIN_ID)[0]["id"]
    plain = (await zoom.get(f"{V2}/meetings/{double_encode(uuid)}/recordings", headers=host)).json()
    assert "download_access_token" not in plain
    with_token = (
        await zoom.get(
            f"{V2}/meetings/{double_encode(uuid)}/recordings",
            headers=host,
            params={"include_fields": "download_access_token", "ttl": 600},
        )
    ).json()
    assert with_token["download_access_token"].count(".") == 2
    too_long = await zoom.get(
        f"{V2}/meetings/{double_encode(uuid)}/recordings",
        headers=host,
        params={"include_fields": "download_access_token", "ttl": 700000},
    )
    assert too_long.status_code == 400


async def test_trash_recover_and_permanent_delete(
    zoom: httpx.AsyncClient, host: dict[str, str], staff: dict[str, str], ctx: Any
) -> None:
    uuid = recorded_of(ctx, ORCHID_ID)[1]["id"]  # 23 Sep
    path = f"{V2}/meetings/{double_encode(uuid)}/recordings"
    assert (await zoom.delete(path, headers=staff)).status_code == 403
    assert (await zoom.delete(path, headers=host, params={"action": "burn"})).status_code == 400
    assert (await zoom.delete(path, headers=host)).status_code == 204  # default action=trash
    assert (await zoom.get(path, headers=host)).status_code == 404
    listed = (await zoom.get(f"{V2}/users/me/recordings", headers=host, params=SEPT)).json()
    assert uuid not in {m["uuid"] for m in listed["meetings"]} and listed["total_records"] == 7
    trash = (
        await zoom.get(f"{V2}/users/me/recordings", headers=host, params={**SEPT, "trash": "true"})
    ).json()
    assert [m["uuid"] for m in trash["meetings"]] == [uuid]
    assert all("deleted_time" in f for f in trash["meetings"][0]["recording_files"])
    assert (await zoom.put(f"{path}/status", headers=host, json={"action": "recover"})).status_code == 204
    assert (await zoom.get(path, headers=host)).json()["uuid"] == uuid
    assert (await zoom.put(f"{path}/status", headers=host, json={"action": "explode"})).status_code == 400
    assert (await zoom.delete(path, headers=host, params={"action": "delete"})).status_code == 204
    assert (await zoom.put(f"{path}/status", headers=host, json={"action": "recover"})).status_code == 404
    assert (
        await zoom.get(f"{V2}/users/me/recordings", headers=host, params={**SEPT, "trash": "true"})
    ).json()["total_records"] == 0


async def test_delete_and_recover_a_single_recording_file(
    zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any
) -> None:
    uuid = recorded_of(ctx, BLUEFIN_ID)[1]["id"]
    path = f"{V2}/meetings/{double_encode(uuid)}/recordings"
    files = (await zoom.get(path, headers=host)).json()["recording_files"]
    vtt = next(f for f in files if f["file_type"] == "TRANSCRIPT")
    assert (await zoom.delete(f"{path}/{vtt['id']}", headers=host)).status_code == 204
    after = (await zoom.get(path, headers=host)).json()
    assert vtt["id"] not in {f["id"] for f in after["recording_files"]}
    assert after["recording_count"] == len(files) - 1 and after["total_size"] == sum(
        f["file_size"] for f in after["recording_files"]
    )
    assert (await zoom.delete(f"{path}/{vtt['id']}", headers=host)).status_code == 404
    assert (
        await zoom.put(f"{path}/{vtt['id']}/status", headers=host, json={"action": "recover"})
    ).status_code == 204
    assert (await zoom.get(path, headers=host)).json()["recording_count"] == len(files)
    assert (
        await zoom.delete(f"{path}/{vtt['id']}", headers=host, params={"action": "delete"})
    ).status_code == 204
    assert (await zoom.get(path, headers=host)).json()["recording_count"] == len(files) - 1


async def test_recording_settings(
    zoom: httpx.AsyncClient, host: dict[str, str], staff: dict[str, str], ctx: Any
) -> None:
    uuid = recorded_of(ctx, BLUEFIN_ID)[0]["id"]
    path = f"{V2}/meetings/{double_encode(uuid)}/recordings/settings"
    settings = (await zoom.get(path, headers=host)).json()
    assert {
        "share_recording",
        "recording_authentication",
        "viewer_download",
        "password",
        "on_demand",
        "approval_type",
        "send_email_to_host",
        "show_social_share_buttons",
    } <= set(settings)
    assert settings["share_recording"] == "internally"
    assert (
        await zoom.patch(path, headers=host, json={"share_recording": "publicly", "viewer_download": False})
    ).status_code == 204
    changed = (await zoom.get(path, headers=host)).json()
    assert changed["share_recording"] == "publicly" and changed["viewer_download"] is False
    assert (await zoom.patch(path, headers=host, json={"share_recording": "everyone"})).status_code == 400
    assert (await zoom.get(path, headers=staff)).status_code == 403


@pytest.fixture
def summary_uuid(ctx: Any) -> str:
    return next(
        i["id"]
        for i in ctx.store.collection("instances")
        if i["transcribed"] and i["meeting_id"] == BLUEFIN_ID
    )


async def test_meeting_summary_from_the_transcript(
    zoom: httpx.AsyncClient, host: dict[str, str], staff: dict[str, str], summary_uuid: str
) -> None:
    r = await zoom.get(f"{V2}/meetings/{double_encode(summary_uuid)}/meeting_summary", headers=host)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["meeting_uuid"] == summary_uuid and body["meeting_id"] == BLUEFIN_ID
    assert body["meeting_host_email"] == HOST and body["meeting_topic"] == "Bluefin Seafood — Status Update"
    assert body["summary_title"] and body["summary_overview"]
    assert body["summary_details"] and all(set(d) == {"label", "summary"} for d in body["summary_details"])
    assert body["next_steps"] and all(isinstance(s, str) for s in body["next_steps"])
    for key in (
        "summary_start_time",
        "summary_end_time",
        "summary_created_time",
        "summary_last_modified_time",
        "meeting_start_time",
        "meeting_end_time",
    ):
        assert body[key].endswith("Z")
    assert (
        await zoom.get(f"{V2}/meetings/{double_encode(summary_uuid)}/meeting_summary", headers=staff)
    ).status_code == 403
    untranscribed = next(
        i["id"] for i in zoom.hub.context("zoom").store.collection("instances") if not i["transcribed"]
    )  # type: ignore[attr-defined]
    missing = await zoom.get(f"{V2}/meetings/{double_encode(untranscribed)}/meeting_summary", headers=host)
    assert missing.status_code == 404


async def test_meeting_summaries_list_is_admin_only(
    zoom: httpx.AsyncClient, s2s: dict[str, str], host: dict[str, str]
) -> None:
    body = (await zoom.get(f"{V2}/meetings/meeting_summaries", headers=s2s, params=SEPT)).json()
    assert body["total_records"] == 12 and body["from"] == "2026-09-01"
    assert all("summary_title" not in s and s["meeting_topic"] for s in body["summaries"])
    assert (await zoom.get(f"{V2}/meetings/meeting_summaries", headers=host, params=SEPT)).status_code == 403
