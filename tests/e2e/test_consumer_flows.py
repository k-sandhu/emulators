"""Replays of the exact call sequences our products make against the real vendors.

These are the acceptance tests for the emulators: if parlance's meeting import,
lumen-copilot's Drive sync or saas_backbone's notifications would break against an
emulator, one of these fails. Each flow uses only the vendor's real URLs and an httpx
client whose transport is swapped, exactly like the products' own tests do.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest

from emulators.testing.helpers import bearer, mint

pytestmark = pytest.mark.e2e

REDIRECT = "https://app.example/api/connections/callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def authorize(client: httpx.AsyncClient, url: str, **params: str) -> dict[str, list[str]]:
    """Follow an authorize endpoint with ``login_hint`` and return the redirect's query."""
    r = await client.get(url, params=params, follow_redirects=False)
    assert r.status_code in (302, 303), (r.status_code, r.text[:500])
    return parse_qs(urlparse(r.headers["location"]).query)


def requires(module: str) -> None:
    pytest.importorskip(module, reason=f"{module} not implemented yet")


# --------------------------------------------------------------------------------------
# parlance: Microsoft Teams meeting import
# --------------------------------------------------------------------------------------


async def test_parlance_teams_import(make_client) -> None:
    requires("emulators.providers.microsoft.meetings")
    requires("emulators.providers.microsoft.calendar")
    requires("emulators.providers.microsoft.identity")
    async with make_client("microsoft") as client:
        verifier, challenge = pkce()
        query = await authorize(
            client,
            "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize",
            client_id="parlance-app",
            response_type="code",
            redirect_uri=REDIRECT,
            scope="offline_access OnlineMeetings.Read OnlineMeetingTranscript.Read.All Calendars.Read",
            state="xyz",
            code_challenge=challenge,
            code_challenge_method="S256",
            login_hint="maya.chen@halcyonadvisory.example",
        )
        assert query["state"] == ["xyz"]
        token = await client.post(
            "https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "redirect_uri": REDIRECT,
                "client_id": "parlance-app",
                "client_secret": "secret",
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        auth = bearer(token.json()["access_token"])
        assert token.json().get("refresh_token")

        start = datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime(2026, 10, 17, tzinfo=UTC)
        url = "https://graph.microsoft.com/v1.0/me/calendarView"
        params: dict[str, str] | None = {
            "startDateTime": start.isoformat().replace("+00:00", "Z"),
            "endDateTime": end.isoformat().replace("+00:00", "Z"),
            "$select": "subject,start,end,onlineMeeting,isOnlineMeeting,onlineMeetingProvider",
            "$top": "15",
            "$orderby": "start/dateTime",
        }
        teams_events = []
        pages = 0
        while url:
            r = await client.get(url, params=params, headers=auth)
            assert r.status_code == 200, r.text
            body = r.json()
            teams_events += [e for e in body["value"] if e.get("onlineMeetingProvider") == "teamsForBusiness"]
            url = body.get("@odata.nextLink")
            params = None
            if url:
                assert urlparse(url).netloc == "graph.microsoft.com"
                assert urlparse(url).path == "/v1.0/me/calendarView"
            pages += 1
        assert pages > 1, "expected pagination with $top=15"
        assert teams_events

        found_transcript = False
        for event in teams_events:
            join = event["onlineMeeting"]["joinUrl"]
            r = await client.get(
                "https://graph.microsoft.com/v1.0/me/onlineMeetings",
                params={"$filter": f"JoinWebUrl eq '{join}'"},
                headers=auth,
            )
            assert r.status_code == 200, r.text
            meetings = r.json()["value"]
            assert len(meetings) == 1
            mid = meetings[0]["id"]
            transcripts = (
                await client.get(f"https://graph.microsoft.com/v1.0/me/onlineMeetings/{mid}/transcripts", headers=auth)
            ).json()["value"]
            recordings = (
                await client.get(f"https://graph.microsoft.com/v1.0/me/onlineMeetings/{mid}/recordings", headers=auth)
            ).json()["value"]
            if transcripts:
                vtt = await client.get(
                    f"https://graph.microsoft.com/v1.0/me/onlineMeetings/{mid}/transcripts/{transcripts[0]['id']}/content",
                    params={"$format": "text/vtt"},
                    headers=auth,
                )
                assert vtt.status_code == 200
                assert vtt.text.startswith("WEBVTT")
                assert "<v " in vtt.text
                found_transcript = True
            if recordings:
                mp4 = await client.get(
                    f"https://graph.microsoft.com/v1.0/me/onlineMeetings/{mid}/recordings/{recordings[0]['id']}/content",
                    headers=auth,
                    follow_redirects=True,
                )
                assert mp4.status_code == 200
                assert mp4.content[4:8] == b"ftyp"
            if found_transcript:
                break
        assert found_transcript


# --------------------------------------------------------------------------------------
# parlance: Zoom cloud recordings import
# --------------------------------------------------------------------------------------


def zoom_uuid_path(uuid: str) -> str:
    if uuid.startswith("/") or "//" in uuid:
        return quote(quote(uuid, safe=""), safe="")
    return quote(uuid, safe="")


async def test_parlance_zoom_import(make_client) -> None:
    requires("emulators.providers.zoom")
    async with make_client("zoom") as client:
        verifier, challenge = pkce()
        query = await authorize(
            client,
            "https://zoom.us/oauth/authorize",
            response_type="code",
            client_id="parlance-zoom",
            redirect_uri=REDIRECT,
            code_challenge=challenge,
            code_challenge_method="S256",
            login_hint="maya.chen@halcyonadvisory.example",
        )
        token = await client.post(
            "https://zoom.us/oauth/token",
            data={"grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
                  "code_verifier": verifier},
            auth=("parlance-zoom", "secret"),
        )
        assert token.status_code == 200, token.text
        auth = bearer(token.json()["access_token"])
        today = datetime(2026, 10, 16, tzinfo=UTC).date()
        meetings = []
        next_token = ""
        while True:
            r = await client.get(
                "https://api.zoom.us/v2/users/me/recordings",
                params={"from": str(today - timedelta(days=29)), "to": str(today), "page_size": 30,
                        "next_page_token": next_token},
                headers=auth,
            )
            assert r.status_code == 200, r.text
            meetings += r.json()["meetings"]
            next_token = r.json().get("next_page_token") or ""
            if not next_token:
                break
        assert meetings, "Maya hosts recorded Zoom meetings in the last 30 days"
        downloaded = 0
        for meeting in meetings:
            r = await client.get(f"https://api.zoom.us/v2/meetings/{zoom_uuid_path(meeting['uuid'])}/recordings",
                                 headers=auth)
            assert r.status_code == 200, r.text
            files = [f for f in r.json()["recording_files"] if f.get("status") == "completed"]
            best = next((f for t in ("VTT", "M4A", "MP4") for f in files if f.get("file_extension") == t), None)
            assert best is not None
            content = await client.get(best["download_url"], headers=auth, follow_redirects=True)
            assert content.status_code == 200
            assert content.content
            downloaded += 1
            if downloaded >= 3:
                break


# --------------------------------------------------------------------------------------
# parlance: Google Meet import (Meet REST v2 + Drive download)
# --------------------------------------------------------------------------------------


async def test_parlance_google_meet_import(make_client) -> None:
    requires("emulators.providers.google.meet")
    requires("emulators.providers.google.drive")
    async with make_client("google") as client:
        token = await mint(client, "https://meet.googleapis.com", user="p040")
        auth = bearer(token)
        r = await client.get(
            "https://meet.googleapis.com/v2/conferenceRecords",
            params={"filter": 'start_time>="2026-09-01T00:00:00Z" AND end_time<="2026-10-17T00:00:00Z"',
                    "pageSize": 15},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        records = r.json().get("conferenceRecords", [])
        assert records
        got_recording = False
        for record in records:
            name = record["name"]
            assert (await client.get(f"https://meet.googleapis.com/v2/{name}", headers=auth)).status_code == 200
            participants = (await client.get(f"https://meet.googleapis.com/v2/{name}/participants",
                                             headers=auth)).json().get("participants", [])
            assert participants
            transcripts = (await client.get(f"https://meet.googleapis.com/v2/{name}/transcripts",
                                            headers=auth)).json().get("transcripts", [])
            for transcript in transcripts:
                if transcript.get("state") == "FILE_GENERATED":
                    entries = (await client.get(f"https://meet.googleapis.com/v2/{transcript['name']}/entries",
                                                headers=auth)).json().get("transcriptEntries", [])
                    assert entries and entries[0]["text"]
            recordings = (await client.get(f"https://meet.googleapis.com/v2/{name}/recordings",
                                           headers=auth)).json().get("recordings", [])
            for recording in recordings:
                if recording.get("state") == "FILE_GENERATED":
                    file_id = recording["driveDestination"]["file"]
                    media = await client.get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                                             params={"alt": "media"}, headers=auth)
                    assert media.status_code == 200, media.text[:300]
                    assert media.content[4:8] == b"ftyp"
                    got_recording = True
            if got_recording:
                break
        assert got_recording


# --------------------------------------------------------------------------------------
# lumen-copilot: Google Drive incremental sync
# --------------------------------------------------------------------------------------


async def test_lumen_copilot_drive_sync(make_client) -> None:
    requires("emulators.providers.google.drive")
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018",
                                 scopes=["https://www.googleapis.com/auth/drive.readonly"]))
        base = "https://www.googleapis.com/drive/v3"
        about = await client.get(f"{base}/about", params={"fields": "user(emailAddress)"}, headers=auth)
        assert about.json() == {"user": {"emailAddress": "maya.chen@halcyonadvisory.example"}}
        drives = (await client.get(f"{base}/drives", headers=auth)).json()["drives"]
        assert drives
        drive_id = drives[0]["id"]
        start = (await client.get(f"{base}/changes/startPageToken", params={"driveId": drive_id,
                                  "supportsAllDrives": "true"}, headers=auth)).json()["startPageToken"]
        files, page_token, queue = [], None, [drive_id]
        fields = "nextPageToken,files(id,name,mimeType,modifiedTime,size,md5Checksum,parents,webViewLink)"
        while queue:
            parent = queue.pop()
            while True:
                params = {"q": f"'{parent}' in parents and trashed = false", "pageSize": "100", "fields": fields,
                          "supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "corpora": "drive",
                          "driveId": drive_id}
                if page_token:
                    params["pageToken"] = page_token
                r = await client.get(f"{base}/files", params=params, headers=auth)
                assert r.status_code == 200, r.text
                body = r.json()
                assert set(body) <= {"nextPageToken", "files"}
                for f in body["files"]:
                    assert set(f) <= {"id", "name", "mimeType", "modifiedTime", "size", "md5Checksum", "parents",
                                      "webViewLink"}
                    if f["mimeType"] == "application/vnd.google-apps.folder":
                        queue.append(f["id"])
                    else:
                        files.append(f)
                page_token = body.get("nextPageToken")
                if not page_token:
                    break
        assert files
        native = next((f for f in files if f["mimeType"].startswith("application/vnd.google-apps.")), None)
        binary = next((f for f in files if not f["mimeType"].startswith("application/vnd.google-apps.")), None)
        if native:
            text = await client.get(f"{base}/files/{native['id']}/export",
                                    params={"mimeType": "text/plain", "supportsAllDrives": "true"}, headers=auth)
            assert text.status_code == 200 and text.text.strip()
        if binary:
            # Shared-drive items are invisible without supportsAllDrives=true, exactly like Drive.
            hidden = await client.get(f"{base}/files/{binary['id']}", params={"alt": "media"}, headers=auth)
            assert hidden.status_code == 404
            raw = await client.get(f"{base}/files/{binary['id']}", params={"alt": "media", "supportsAllDrives": "true"},
                                   headers=auth)
            assert raw.status_code == 200 and raw.content
            perms = await client.get(f"{base}/files/{binary['id']}/permissions", params={"supportsAllDrives": "true"},
                                     headers=auth)
            assert perms.status_code == 200
        changes = await client.get(f"{base}/changes", params={"pageToken": start, "includeRemoved": "true",
                                   "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                                   "driveId": drive_id}, headers=auth)
        assert changes.status_code == 200, changes.text
        assert "newStartPageToken" in changes.json() or "nextPageToken" in changes.json()


# --------------------------------------------------------------------------------------
# parlance: storage connectors (OneDrive, Dropbox, Box)
# --------------------------------------------------------------------------------------


async def test_parlance_onedrive_listing(make_client) -> None:
    requires("emulators.providers.microsoft.drive")
    async with make_client("microsoft") as client:
        auth = bearer(await mint(client, "https://graph.microsoft.com", user="p018"))
        r = await client.get("https://graph.microsoft.com/v1.0/me/drive/root/children", headers=auth)
        assert r.status_code == 200, r.text
        items = r.json()["value"]
        assert items
        file_item = None
        stack = [i for i in items]
        while stack and file_item is None:
            item = stack.pop()
            if "file" in item:
                file_item = item
            elif "folder" in item:
                children = await client.get(
                    f"https://graph.microsoft.com/v1.0/me/drive/items/{item['id']}/children", headers=auth)
                stack += children.json()["value"]
        assert file_item is not None
        meta = await client.get(f"https://graph.microsoft.com/v1.0/me/drive/items/{file_item['id']}", headers=auth)
        assert meta.status_code == 200
        content = await client.get(f"https://graph.microsoft.com/v1.0/me/drive/items/{file_item['id']}/content",
                                   headers=auth, follow_redirects=True)
        assert content.status_code == 200 and content.content


async def test_parlance_dropbox_listing(make_client) -> None:
    requires("emulators.providers.dropbox")
    async with make_client("dropbox") as client:
        auth = bearer(await mint(client, "https://api.dropboxapi.com", user="p018"))
        r = await client.post("https://api.dropboxapi.com/2/files/list_folder", json={"path": "", "recursive": True},
                              headers=auth)
        assert r.status_code == 200, r.text
        entries = r.json()["entries"]
        while r.json()["has_more"]:
            r = await client.post("https://api.dropboxapi.com/2/files/list_folder/continue",
                                  json={"cursor": r.json()["cursor"]}, headers=auth)
            entries += r.json()["entries"]
        f = next(e for e in entries if e[".tag"] == "file")
        meta = await client.post("https://api.dropboxapi.com/2/files/get_metadata", json={"path": f["id"]},
                                 headers=auth)
        assert meta.json()["path_lower"] == f["path_lower"]
        import json as _json

        dl = await client.post("https://content.dropboxapi.com/2/files/download",
                               headers={**auth, "Dropbox-API-Arg": _json.dumps({"path": f["path_lower"]})})
        assert dl.status_code == 200 and dl.content
        assert _json.loads(dl.headers["Dropbox-API-Result"])["id"] == f["id"]


async def test_parlance_box_listing(make_client) -> None:
    requires("emulators.providers.box")
    async with make_client("box") as client:
        auth = bearer(await mint(client, "https://api.box.com", user="p018"))
        r = await client.get("https://api.box.com/2.0/folders/0/items", headers=auth)
        assert r.status_code == 200, r.text
        stack = r.json()["entries"]
        file_entry = None
        while stack and file_entry is None:
            entry = stack.pop()
            if entry["type"] == "file":
                file_entry = entry
            elif entry["type"] == "folder":
                stack += (await client.get(f"https://api.box.com/2.0/folders/{entry['id']}/items",
                                           headers=auth)).json()["entries"]
        assert file_entry is not None
        info = await client.get(f"https://api.box.com/2.0/files/{file_entry['id']}", headers=auth)
        assert info.status_code == 200
        version = info.json()["file_version"]["id"]
        content = await client.get(f"https://api.box.com/2.0/files/{file_entry['id']}/content",
                                   params={"version": version}, headers=auth, follow_redirects=True)
        assert content.status_code == 200 and content.content


# --------------------------------------------------------------------------------------
# saas_backbone: Twilio SMS notifications, Stripe webhook verification
# --------------------------------------------------------------------------------------


async def test_backbone_twilio_sms(make_client) -> None:
    requires("emulators.providers.twilio")
    async with make_client("twilio") as client:
        hub = client.hub  # type: ignore[attr-defined]
        sid = hub.world.org.twilio_account_sid
        r = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"To": "+14165550142", "From": "+14165550188", "Body": "Your verification code is 482913"},
            auth=(sid, "any-token"),
        )
        assert r.status_code == 201, r.text
        msg = r.json()
        assert msg["sid"].startswith("SM") and len(msg["sid"]) == 34
        assert msg["status"] in ("queued", "sent", "delivered")


async def test_backbone_stripe_webhook_signature(make_client) -> None:
    requires("emulators.providers.stripe")
    import stripe as stripe_sdk
    from fastapi import FastAPI, Request

    received: list[tuple[bytes, str]] = []
    receiver = FastAPI()

    @receiver.post("/stripe/webhook")
    async def hook(request: Request) -> dict:
        received.append((await request.body(), request.headers["stripe-signature"]))
        return {}

    async with make_client("stripe") as client:
        hub = client.hub  # type: ignore[attr-defined]
        hub.webhook_transport = httpx.ASGITransport(app=receiver)
        key = {"Authorization": "Bearer sk_test_emulator"}
        endpoint = await client.post("https://api.stripe.com/v1/webhook_endpoints", headers=key,
                                     data={"url": "http://consumer.test/stripe/webhook", "enabled_events[]": "*"})
        assert endpoint.status_code == 200, endpoint.text
        secret = endpoint.json()["secret"]
        created = await client.post("https://api.stripe.com/v1/customers", headers=key,
                                    data={"email": "new.client@example.com", "name": "New Client"})
        assert created.status_code == 200
        await hub.drain_webhooks()
    assert received
    payload, signature = received[-1]
    event = stripe_sdk.Webhook.construct_event(payload, signature, secret)
    assert event["type"].startswith("customer.")


# --------------------------------------------------------------------------------------
# Cross-system consistency
# --------------------------------------------------------------------------------------


async def test_same_person_everywhere(make_client) -> None:
    requires("emulators.providers.microsoft.directory")
    requires("emulators.providers.google.directory")
    async with make_client("microsoft", "google") as client:
        ms = bearer(await mint(client, "https://graph.microsoft.com", user="p018"))
        me = (await client.get("https://graph.microsoft.com/v1.0/me", headers=ms)).json()
        gg = bearer(await mint(client, "https://admin.googleapis.com", user="p035"))
        user = (await client.get("https://admin.googleapis.com/admin/directory/v1/users/maya.chen@halcyonadvisory.example",
                                 headers=gg)).json()
        assert me["mail"] == user["primaryEmail"] == "maya.chen@halcyonadvisory.example"
        assert me["displayName"] == user["name"]["fullName"] == "Maya Chen"
