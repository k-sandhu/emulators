"""Exercise the recording-import contract with unmodified httpx vendor URLs."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from emulators.testing.server import EmulatorServer

pytestmark = pytest.mark.sdk
BASE = "https://webexapis.com/v1"


def test_webex_recording_import_over_live_gateway(emulator_intercept: EmulatorServer) -> None:
    with httpx.Client(headers={"Authorization": "Bearer webex-admin-token"}) as client:
        response = client.get(BASE + "/meetings", params={"meetingType": "meeting", "max": 1})
        response.raise_for_status()
        meeting = response.json()["items"][0]
        assert "_I_" in meeting["id"] and response.headers["TrackingId"]
        next_page = client.get(response.links["next"]["url"])
        next_page.raise_for_status()
        assert next_page.json()["items"][0]["id"] != meeting["id"]
        participants = client.get(BASE + "/meetingParticipants", params={"meetingId": meeting["id"]})
        participants.raise_for_status()
        assert any(p["host"] for p in participants.json()["items"])
        recordings = client.get(BASE + "/recordings", params={"meetingId": meeting["id"], "format": "MP4"})
        recordings.raise_for_status()
        row = recordings.json()["items"][0]
        detail = client.get(BASE + "/recordings/" + row["id"])
        detail.raise_for_status()
        links = detail.json()["temporaryDirectDownloadLinks"]
        with httpx.Client() as downloader:
            mp4 = downloader.get(links["recordingDownloadLink"])
            mp4.raise_for_status()
            assert mp4.content[4:8] == b"ftyp" and mp4.headers["content-type"] == "video/mp4"
            assert len(mp4.content) == row["sizeBytes"]
            audio = downloader.get(links["audioDownloadLink"])
            assert audio.status_code == 200 and audio.content[8:12] == b"M4A "
            vtt = downloader.get(links["transcriptDownloadLink"])
            assert vtt.status_code == 200 and vtt.text.startswith("WEBVTT")
        transcripts = client.get(BASE + "/meetingTranscripts", params={"meetingId": meeting["id"]})
        transcripts.raise_for_status()
        transcript = transcripts.json()["items"][0]
        downloaded = client.get(transcript["vttDownloadLink"])
        assert downloaded.text == vtt.text
        snippets = client.get(BASE + f"/meetingTranscripts/{transcript['id']}/snippets")
        snippets.raise_for_status()
        speaker = snippets.json()["items"][0]
        people = client.get(BASE + "/people", params={"email": speaker["personEmail"]})
        people.raise_for_status()
        assert people.json()["items"][0]["displayName"] == speaker["personName"]


def test_webex_oauth_recording_scope_over_live_gateway(emulator_intercept: EmulatorServer) -> None:
    with httpx.Client() as client:
        redirect = "https://consumer.example/callback"
        approved = client.get(
            BASE + "/authorize",
            params={
                "response_type": "code",
                "client_id": "recording-importer",
                "redirect_uri": redirect,
                "login_hint": "sofia.alvarez@halcyonadvisory.example",
                "scope": "spark:people_read meeting:schedules_read meeting:recordings_read meeting:transcripts_read",
            },
        )
        assert approved.status_code == 302
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        token = client.post(
            BASE + "/access_token",
            data={
                "grant_type": "authorization_code",
                "client_id": "recording-importer",
                "client_secret": "test-only",
                "code": code,
                "redirect_uri": redirect,
            },
        )
        token.raise_for_status()
        client.headers["Authorization"] = "Bearer " + token.json()["access_token"]
        me = client.get(BASE + "/people/me")
        assert me.status_code == 200 and me.json()["displayName"] == "Sofia Alvarez"
        meetings = client.get(BASE + "/meetings", params={"meetingType": "meeting"})
        meetings.raise_for_status()
        recording = client.get(BASE + "/recordings", params={"meetingId": meetings.json()["items"][0]["id"]})
        assert recording.status_code == 200 and recording.json()["items"]
        assert (
            client.post(
                BASE + "/messages",
                json={"toPersonEmail": "maya.chen@halcyonadvisory.example", "text": "No write scope"},
            ).status_code
            == 403
        )
