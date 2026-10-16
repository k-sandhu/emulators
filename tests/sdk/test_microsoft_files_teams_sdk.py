"""The installed Microsoft SDK is MSAL; Graph operations use its actual bearer tokens."""

from __future__ import annotations

from typing import Any

import msal
import pytest
import requests

from emulators.core.hub import EmulatorHub
from emulators.providers.microsoft import common

pytestmark = pytest.mark.sdk
BASE = "https://graph.microsoft.com/v1.0"
CLIENT_ID = "00000000-0000-0000-0000-00000000c0de"
AUTHORITY = "https://login.microsoftonline.com/halcyonadvisory.example"


def delegated(email: str) -> dict[str, str]:
    application = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY)
    token = application.acquire_token_by_username_password(
        email, "synthetic-password", scopes=["Files.ReadWrite", "OnlineMeetings.Read", "Chat.ReadWrite"]
    )
    assert "access_token" in token, token
    return {"Authorization": "Bearer " + token["access_token"]}


def test_msal_token_file_upload_session_and_sharepoint_download(emulator_intercept: Any) -> None:
    auth = delegated("maya.chen@halcyonadvisory.example")
    drive = requests.get(BASE + "/me/drive", headers=auth, timeout=10)
    assert drive.status_code == 200 and drive.json()["driveType"] == "business"
    response = requests.post(
        BASE + "/me/drive/root:/sdk-upload.txt:/createUploadSession",
        headers=auth,
        json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        timeout=10,
    )
    assert response.status_code == 200, response.text
    finished = requests.put(
        response.json()["uploadUrl"],
        data=b"official Microsoft auth",
        headers={"Content-Range": "bytes 0-22/23"},
        timeout=10,
    )
    assert finished.status_code == 201, finished.text
    file_url = BASE + "/me/drive/items/" + finished.json()["id"] + "/content"
    content = requests.get(file_url, headers=auth, timeout=10)
    assert content.status_code == 200 and content.content == b"official Microsoft auth"
    assert content.history[0].status_code == 302
    assert "sharepoint.com" in content.history[0].headers["Location"]


def test_msal_token_parlance_meeting_workflow(emulator_intercept: Any, emulator_hub: EmulatorHub) -> None:
    world = emulator_hub.world
    source = next(
        m
        for m in world.meetings
        if m.platform == "teams" and m.transcribed and m.recorded and m.happened and not m.cancelled
    )
    auth = delegated(world.person(source.organizer_id).email)
    lookup = requests.get(
        BASE + "/me/onlineMeetings",
        headers=auth,
        params={"$filter": "JoinWebUrl eq '" + common.join_web_url(world, source) + "'"},
        timeout=10,
    )
    assert lookup.status_code == 200, lookup.text
    meeting_url = BASE + "/me/onlineMeetings/" + lookup.json()["value"][0]["id"]
    transcript = requests.get(meeting_url + "/transcripts", headers=auth, timeout=10).json()["value"][0]
    recording = requests.get(meeting_url + "/recordings", headers=auth, timeout=10).json()["value"][0]
    vtt = requests.get(
        meeting_url + "/transcripts/" + transcript["id"] + "/content?$format=text/vtt",
        headers=auth,
        timeout=10,
    )
    mp4 = requests.get(meeting_url + "/recordings/" + recording["id"] + "/content", headers=auth, timeout=10)
    assert vtt.content.startswith(b"WEBVTT") and b"<v " in vtt.content
    assert mp4.content[4:8] == b"ftyp"


def test_msal_application_sites_and_channel_export(
    emulator_intercept: Any, emulator_hub: EmulatorHub
) -> None:
    application = msal.ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential="synthetic-secret"
    )
    token = application.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    assert "access_token" in token, token
    auth = {"Authorization": "Bearer " + token["access_token"]}
    sites = requests.get(BASE + "/sites?search=Client", headers=auth, timeout=10)
    assert sites.status_code == 200 and sites.json()["value"]
    team = emulator_hub.world.teams[0]
    tid = common.group_id(emulator_hub.world, team.group_id or f"team-{team.id}")
    messages = requests.get(BASE + f"/teams/{tid}/channels/getAllMessages?$top=1", headers=auth, timeout=10)
    assert messages.status_code == 200, messages.text
    assert messages.json()["value"][0]["from"]["user"]["userIdentityType"] == "aadUser"
