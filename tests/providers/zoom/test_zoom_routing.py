"""Routing modes, control plane, reset and fault interplay for the Zoom provider."""

from __future__ import annotations

import httpx

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer
from tests.providers.zoom.conftest import API, HOST, OAUTH, ORCHID_ID, V2, double_encode


async def test_prefix_mode_rewrites_every_absolute_url() -> None:
    hub = EmulatorHub(providers=["zoom"], anchor="2026-10-16", webhook_mode="sync")
    transport = httpx.ASGITransport(app=hub.gateway)
    base = "http://localhost:44080/zoom"
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080") as client:
        token = await client.post(
            "/zoom/oauth/token",
            params={"grant_type": "account_credentials", "account_id": hub.world.org.zoom_account_id},
            auth=("id", "secret"),
        )
        body = token.json()
        assert body["api_url"] == base  # clients that follow api_url stay on the emulator
        headers = bearer(body["access_token"])
        recs = await client.get(
            f"/zoom/v2/users/{HOST}/recordings",
            headers=headers,
            params={"from": "2026-09-01", "to": "2026-09-30"},
        )
        meeting = recs.json()["meetings"][0]
        assert meeting["share_url"].startswith(f"{base}/rec/share/")
        assert meeting["recording_files"][0]["download_url"].startswith(f"{base}/rec/download/")
        info = (await client.get(f"/zoom/v2/meetings/{ORCHID_ID}", headers=headers)).json()
        assert info["join_url"].startswith(f"{base}/j/{ORCHID_ID}?pwd=") and info["start_url"].startswith(
            f"{base}/s/"
        )
        hop = await client.get(meeting["recording_files"][0]["download_url"], headers=headers)
        assert hop.status_code == 302 and hop.headers["location"].startswith(f"{base}/file/")
        final = await client.get(hop.headers["location"])
        assert final.status_code == 200 and final.headers["content-type"] == "video/mp4"
        authorize = await client.get(
            "/zoom/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "c",
                "redirect_uri": "http://app.test/cb",
                "login_hint": HOST,
            },
        )
        assert authorize.status_code == 302 and authorize.headers["location"].startswith(
            "http://app.test/cb?code="
        )


async def test_control_plane_info_state_and_reset(zoom: httpx.AsyncClient, s2s: dict[str, str]) -> None:
    info = (await zoom.get(f"{API}/_emulator/info")).json()
    assert info["name"] == "zoom" and info["default_port"] == 44106
    assert info["account_id"] == zoom.hub.world.org.zoom_account_id  # type: ignore[attr-defined]
    assert info["account_owner"] == "eleanor.whitfield@halcyonadvisory.example"
    assert {
        "configure_webhook",
        "start_meeting",
        "end_meeting",
        "participant_join",
        "participant_leave",
    } <= set(info["actions"])
    assert info["collections"] == {
        "groups": 13,
        "instances": 26,
        "meetings": 14,
        "recordings": 20,
        "users": 47,
    }
    for host in (
        "zoom.us",
        "api.zoom.us",
        "us06web.zoom.us",
        "marketplace.zoom.us",
        "file.zoom.us",
        "ssrweb.zoom.us",
    ):
        assert zoom.hub.provider_for_host(host) == "zoom"  # type: ignore[attr-defined]

    await zoom.post(
        f"{V2}/users",
        headers=s2s,
        json={"action": "create", "user_info": {"email": "temp@halcyonadvisory.example", "type": 1}},
    )
    await zoom.delete(f"{V2}/meetings/{ORCHID_ID}", headers=s2s)
    assert (await zoom.get(f"{API}/_emulator/state")).json()["users"] == 48
    assert (await zoom.post(f"{API}/_emulator/reset")).json()["status"] == "reset"
    assert (await zoom.get(f"{API}/_emulator/state")).json()["users"] == 47
    fresh = (await zoom.post(f"{API}/_emulator/tokens", json={"app_only": True})).json()["access_token"]
    assert (await zoom.get(f"{V2}/meetings/{ORCHID_ID}", headers=bearer(fresh))).status_code == 200
    assert (
        await zoom.get(f"{V2}/users/temp@halcyonadvisory.example", headers=bearer(fresh))
    ).status_code == 404


async def test_journal_records_requests_without_secrets(
    zoom: httpx.AsyncClient, host: dict[str, str]
) -> None:
    await zoom.get(f"{V2}/users/me", headers=host)
    entries = (await zoom.get(f"{API}/_emulator/requests", params={"path": "/v2/users/me"})).json()[
        "requests"
    ]
    assert entries and entries[-1]["status"] == 200
    assert entries[-1]["headers"]["authorization"].startswith("Bearer …")
    await zoom.post(f"{OAUTH}/oauth/token", params={"grant_type": "client_credentials"}, auth=("a", "b"))
    token_calls = (await zoom.get(f"{API}/_emulator/requests", params={"path": "/oauth/token"})).json()[
        "requests"
    ]
    assert token_calls[-1]["headers"]["authorization"].startswith("Basic …")


async def test_state_dump_hides_nothing_but_wire_responses_hide_internals(
    zoom: httpx.AsyncClient, s2s: dict[str, str]
) -> None:
    dump = (await zoom.get(f"{API}/_emulator/state/meetings", params={"id": str(ORCHID_ID)})).json()
    assert dump["_world_series"] and "occurrences" in dump
    wire = (await zoom.get(f"{V2}/meetings/{ORCHID_ID}", headers=s2s)).json()
    assert not [k for k in wire if k.startswith("_")]
    assert all(not [k for k in o if k.startswith("_")] for o in wire["occurrences"])
    listing = (
        await zoom.get(
            f"{V2}/users/{HOST}/recordings", headers=s2s, params={"from": "2026-09-01", "to": "2026-09-30"}
        )
    ).json()
    assert listing["meetings"]

    def private_keys(value: object) -> list[str]:
        if isinstance(value, dict):
            return [k for k in value if k.startswith("_")] + [
                x for v in value.values() for x in private_keys(v)
            ]
        if isinstance(value, list):
            return [x for v in value for x in private_keys(v)]
        return []

    assert private_keys(listing) == []
    uuid = double_encode(listing["meetings"][0]["uuid"])
    assert (await zoom.get(f"{V2}/meetings/{uuid}/recordings", headers=s2s)).status_code == 200


async def test_seed_is_deterministic_and_fast() -> None:
    import time

    started = time.perf_counter()
    first = EmulatorHub(providers=["zoom"], anchor="2026-10-16")
    elapsed = time.perf_counter() - started
    second = EmulatorHub(providers=["zoom"], anchor="2026-10-16")
    assert elapsed < 2.5  # world generation plus the whole Zoom seed
    one, two = first.context("zoom").store.dump(), second.context("zoom").store.dump()
    assert one == two and one["recordings"] and one["users"]
    other = EmulatorHub(providers=["zoom"], anchor="2026-10-16", seed="another-seed")
    assert other.context("zoom").store.dump()["users"] != one["users"]
