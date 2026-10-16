from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint
from emulators.world.media import parse_vtt

ADMIN = bearer("webex-admin-token")


async def test_recording_import_binary_links_and_speaker_identity(
    client: httpx.AsyncClient, hub: EmulatorHub
) -> None:
    meetings = (await client.get("/v1/meetings", params={"meetingType": "meeting"})).json()["items"]
    meeting = meetings[0]
    assert len(hub.context("webex").store.blobs) == 0
    listing = await client.get(
        "/v1/recordings", params={"meetingId": meeting["id"], "format": "MP4", "max": 1}
    )
    assert listing.status_code == 200, listing.text
    recording = listing.json()["items"][0]
    assert (
        recording["format"] == "MP4"
        and recording["status"] == "available"
        and recording["durationSeconds"] == 2700
    )
    assert "temporaryDirectDownloadLinks" not in recording
    detail = (await client.get(f"/v1/recordings/{recording['id']}")).json()
    links = detail["temporaryDirectDownloadLinks"]
    assert len(hub.context("webex").store.blobs) == 0
    assert "binaries.webex.com" in links["recordingDownloadLink"]
    assert (await client.get(detail["downloadUrl"])).headers["content-type"].startswith("text/html")
    downloads = {}
    for key, mime in (
        ("recordingDownloadLink", "video/mp4"),
        ("audioDownloadLink", "audio/mp4"),
        ("transcriptDownloadLink", "text/vtt"),
    ):
        response = await client.get(links[key], headers={"Authorization": ""})
        assert response.status_code == 200 and response.headers["content-type"].startswith(mime)
        downloads[key] = response.content
    assert downloads["recordingDownloadLink"][4:8] == b"ftyp"
    assert downloads["audioDownloadLink"][8:12] == b"M4A "
    assert len(downloads["recordingDownloadLink"]) == recording["sizeBytes"]
    vtt = downloads["transcriptDownloadLink"].decode()
    assert vtt.startswith("WEBVTT\n\n1\n") and "Sofia Alvarez:" in vtt and "<v " not in vtt
    transcript = (await client.get("/v1/meetingTranscripts", params={"meetingId": meeting["id"]})).json()[
        "items"
    ][0]
    for path in ("recordings", "meetingTranscripts"):
        assert (
            await client.get(f"/v1/{path}", params={"meetingId": recording["scheduledMeetingId"]})
        ).json() == {"items": []}
    assert (await client.get(transcript["vttDownloadLink"])).text == vtt
    assert "Sofia Alvarez:" in (await client.get(transcript["txtDownloadLink"])).text
    response = await client.get(f"/v1/meetingTranscripts/{transcript['id']}/snippets", params={"max": 1})
    assert "next" in response.links
    snippet = response.json()["items"][0]
    cues = parse_vtt(vtt.encode())
    assert cues[0].speaker == snippet["personName"] and cues[0].text == snippet["text"]
    assert round(cues[0].start_s * 1000) == snippet["offsetMillisecond"]
    p = (await client.get("/v1/people", params={"email": snippet["personEmail"]})).json()["items"][0]
    assert p["displayName"] == snippet["personName"]
    count = len(hub.context("webex").store.blobs)
    assert (await client.get(links["recordingDownloadLink"])).content == downloads["recordingDownloadLink"]
    assert len(hub.context("webex").store.blobs) == count
    hub.clock.advance(timedelta(hours=2))
    assert (await client.get(links["recordingDownloadLink"])).status_code == 403


async def test_recording_acl_ranges_and_recycle_bin(client: httpx.AsyncClient) -> None:
    rows = (await client.get("/v1/recordings", headers=ADMIN)).json()["items"]
    row = next(r for r in rows if r["topic"].startswith("Meridian"))
    outsider = bearer(await mint(client, "https://webexapis.com", user="p018"))
    assert (await client.get(f"/v1/recordings/{row['id']}", headers=outsider)).status_code == 404
    assert (await client.get("/v1/recordings", headers=outsider)).json()["items"] == []
    transcript = (
        await client.get("/v1/meetingTranscripts", params={"meetingId": row["meetingId"]}, headers=ADMIN)
    ).json()["items"][0]
    assert (await client.get(transcript["vttDownloadLink"], headers=outsider)).status_code == 404
    assert (await client.get(transcript["vttDownloadLink"], headers={"Authorization": ""})).status_code == 401
    detail = (await client.get(f"/v1/recordings/{row['id']}", headers=ADMIN)).json()
    link = detail["temporaryDirectDownloadLinks"]["recordingDownloadLink"]
    full = (await client.get(link)).content
    for value, expected in (("bytes=0-11", full[:12]), ("bytes=-12", full[-12:]), ("bytes=12-", full[12:])):
        r = await client.get(link, headers={"Range": value})
        assert r.status_code == 206 and r.content == expected
        assert r.headers["content-range"].endswith(f"/{len(full)}")
    head = await client.head(link)
    assert head.status_code == 200 and not head.content and int(head.headers["content-length"]) == len(full)
    assert (await client.get(link, headers={"Range": "bytes=999999999-"})).status_code == 416
    assert (await client.get(link.split("?")[0])).status_code == 403
    assert (await client.delete(f"/v1/recordings/{row['id']}", headers=ADMIN)).status_code == 204
    assert (await client.get(link)).status_code == 404
    assert (await client.get(f"/v1/recordings/{row['id']}", headers=ADMIN)).json()["status"] == "deleted"
    deleted = (await client.get("/v1/recordings", params={"status": "deleted"}, headers=ADMIN)).json()[
        "items"
    ]
    assert [r["id"] for r in deleted] == [row["id"]]


async def test_artifact_list_filters_pagination_and_admin_scope(client: httpx.AsyncClient) -> None:
    q = {"from": "2026-09-16", "to": "2026-10-16", "max": 1}
    r = await client.get("/v1/recordings", params=q, headers=ADMIN)
    ids = []
    while True:
        ids.extend(row["id"] for row in r.json()["items"])
        if "next" not in r.links:
            break
        r = await client.get(r.links["next"]["url"], headers=ADMIN)
    assert len(set(ids)) == 3
    assert (
        await client.get("/v1/recordings", params={**q, "siteUrl": "other.webex.com"}, headers=ADMIN)
    ).json()["items"] == []
    assert (
        len(
            (
                await client.get(
                    "/v1/recordings",
                    params={**q, "hostEmail": "daniel.kowalski@halcyonadvisory.example"},
                    headers=ADMIN,
                )
            ).json()["items"]
        )
        == 1
    )
    assert (
        await client.get("/v1/recordings", params={"hostEmail": "daniel.kowalski@halcyonadvisory.example"})
    ).status_code == 403
    q = {"from": "2026-10-01", "to": "2026-10-16"}
    assert len((await client.get("/v1/meetingTranscripts", params=q, headers=ADMIN)).json()["items"]) == 2
    service = bearer(await mint(client, "https://webexapis.com", app_only=True))
    assert len((await client.get("/v1/recordings", headers=service)).json()["items"]) == 3
    weak = bearer(
        await mint(client, "https://webexapis.com", app_only=True, scopes=["meeting:recordings_read"])
    )
    assert (await client.get("/v1/recordings", headers=weak)).json() == {"items": []}


@pytest.mark.parametrize(
    "query",
    [{"from": "2026-01-01", "to": "2026-10-16"}, {"format": "ARF"}, {"status": "bad"}, {"from": "bad"}],
)
async def test_recording_bad_filters(client: httpx.AsyncClient, query: dict[str, str]) -> None:
    assert (await client.get("/v1/recordings", params=query)).status_code == 400
