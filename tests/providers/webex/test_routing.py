from __future__ import annotations

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint


@pytest.mark.parametrize(
    "host", ["webexapis.com", "api.ciscospark.com", "halcyonadvisory.webex.com", "binaries.webex.com"]
)
async def test_vendor_hosts_and_tracking(client: httpx.AsyncClient, host: str) -> None:
    response = await client.get(f"https://{host}/v1/people/me")
    assert response.status_code == 200
    assert response.headers["TrackingId"].startswith("ROUTER_")
    assert response.json()["emails"] == ["sofia.alvarez@halcyonadvisory.example"]


@pytest.mark.parametrize("mode", ["prefix", "port"])
async def test_all_absolute_urls_work_in_prefix_and_port_modes(hub: EmulatorHub, mode: str) -> None:
    prefix = "/webex" if mode == "prefix" else ""
    transport = httpx.ASGITransport(app=hub.gateway if mode == "prefix" else hub.apps["webex"])
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost:44114", headers=bearer("webex-admin-token")
    ) as client:
        first = await client.get(
            prefix + "/v1/meetings",
            params={"meetingType": "meeting", "max": 1, "from": "2026-09-01", "to": "2026-10-16"},
        )
        link = first.links["next"]["url"]
        assert link.startswith(f"http://localhost:44114{prefix}/v1/meetings?")
        second = await client.get(link)
        assert second.status_code == 200 and first.json()["items"][0]["id"] != second.json()["items"][0]["id"]
        row = first.json()["items"][0]
        assert row["webLink"].startswith(f"http://localhost:44114{prefix}/halcyonadvisory/")
        recordings = (await client.get(prefix + "/v1/recordings", params={"meetingId": row["id"]})).json()[
            "items"
        ]
        detail = (await client.get(prefix + f"/v1/recordings/{recordings[0]['id']}")).json()
        for name in ("downloadUrl", "playbackUrl"):
            assert detail[name].startswith(f"http://localhost:44114{prefix}/recording/")
            assert (await client.get(detail[name])).status_code == 200
        for key, url in detail["temporaryDirectDownloadLinks"].items():
            if key != "expiration":
                assert url.startswith(f"http://localhost:44114{prefix}/recordings/")
                assert (await client.get(url, headers={"Authorization": ""})).status_code == 200
        transcript = (
            await client.get(prefix + "/v1/meetingTranscripts", params={"meetingId": row["id"]})
        ).json()["items"][0]
        assert transcript["vttDownloadLink"].startswith(f"http://localhost:44114{prefix}/v1/")
        assert (await client.get(transcript["vttDownloadLink"])).status_code == 200


async def test_error_envelopes_faults_journal_and_reset(client: httpx.AsyncClient) -> None:
    for path, headers in (
        ("/v1/people/me", {"Authorization": ""}),
        ("/v1/people/missing", {}),
        ("/v1/unknown", {}),
    ):
        response = await client.get(path, headers=headers)
        assert response.status_code in (401, 404)
        assert set(response.json()) == {"message", "errors", "trackingId"}
        assert response.json()["trackingId"] == response.headers["TrackingId"]
    await client.post("/_emulator/faults", json={"path": "/v1/people/me", "status": 429, "times": 1})
    throttled = await client.get("/v1/people/me")
    assert throttled.status_code == 429 and throttled.headers["Retry-After"] == "2"
    assert throttled.headers["TrackingId"] == throttled.json()["trackingId"]
    assert (await client.get("/v1/people/me")).status_code == 200
    before = (await client.get("/v1/rooms")).json()["items"]
    await client.post("/v1/rooms", json={"title": "Reset me"})
    token = await mint(client, "https://webexapis.com", user="p005")
    journal = (await client.get("/_emulator/requests")).json()["requests"]
    assert any(r["status"] == 429 and r["fault"] for r in journal)
    assert all("webex-host-token" not in r["headers"].get("authorization", "") for r in journal)
    await client.post("/_emulator/reset")
    assert (await client.get("/v1/people/me", headers=bearer(token))).status_code == 401
    assert (await client.get("/v1/rooms")).json()["items"] == before


async def test_snapshots_replay_and_lazy_media(client: httpx.AsyncClient) -> None:
    await client.post("/_emulator/snapshots", json={"name": "before"})
    created = (await client.post("/v1/rooms", json={"title": "Temporary"})).json()
    await client.post("/_emulator/snapshots/before/restore")
    assert (await client.get(f"/v1/rooms/{created['id']}")).status_code == 404
    response = await client.post("/v1/rooms", content="{", headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["trackingId"] == response.headers["trackingid"]
