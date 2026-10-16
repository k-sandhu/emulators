from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from emulators.providers.google.common import drive_id, meet_recording_file_id, meet_transcript_doc_id
from emulators.providers.google.drive_seed import FOLDER, root_id, shared_drive_id
from emulators.testing.helpers import bearer, mint

BASE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"


async def test_lumen_folder_crawl_download_export_permissions_and_changes(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        ctx = client.hub.context("google")
        about = await client.get(BASE + "/about", params={"fields": "user(emailAddress)"}, headers=auth)
        assert about.json() == {"user": {"emailAddress": "maya.chen@halcyonadvisory.example"}}
        did = shared_drive_id(ctx, "clients")
        params = {
            "q": f"'{did}' in parents and trashed = false",
            "pageSize": 2,
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size,md5Checksum,parents,webViewLink)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "corpora": "drive",
            "driveId": did,
        }
        all_ids = []
        while True:
            response = await client.get(BASE + "/files", params=params, headers=auth)
            assert response.status_code == 200, response.text
            result = response.json()
            assert "kind" not in result
            assert all(
                set(r)
                <= {"id", "name", "mimeType", "modifiedTime", "size", "md5Checksum", "parents", "webViewLink"}
                for r in result["files"]
            )
            assert all(did in r["parents"] for r in result["files"])
            all_ids.extend(r["id"] for r in result["files"])
            if "nextPageToken" not in result:
                break
            params["pageToken"] = result["nextPageToken"]
        assert len(all_ids) == len(set(all_ids)) and len(all_ids) >= 20
        binary_doc = next(d for d in ctx.world.documents if d.space_id == "clients" and not d.restricted)
        fid = drive_id(ctx.world, "file", binary_doc.id)
        metadata = await client.get(
            BASE + "/files/" + fid,
            params={"supportsAllDrives": "true", "fields": "id,size,md5Checksum,mimeType"},
            headers=auth,
        )
        content = await client.get(
            BASE + "/files/" + fid, params={"supportsAllDrives": "true", "alt": "media"}, headers=auth
        )
        assert content.status_code == 200
        assert int(metadata.json()["size"]) == len(content.content)
        assert hashlib.md5(content.content).hexdigest() == metadata.json()["md5Checksum"]
        assert content.content.startswith(b"PK")
        perms = await client.get(
            BASE + "/files/" + fid + "/permissions",
            params={
                "supportsAllDrives": "true",
                "fields": "permissions(id,type,role,emailAddress,permissionDetails)",
            },
            headers=auth,
        )
        assert perms.status_code == 200 and perms.json()["permissions"]
        assert any(p.get("permissionDetails", [{}])[0].get("inherited") for p in perms.json()["permissions"])
        native_doc = next(
            d
            for d in ctx.world.documents
            if d.space_id == "knowledge" and d.kind == "docx" and not d.restricted
        )
        native_id = drive_id(ctx.world, "file", native_doc.id)
        export = await client.get(
            BASE + "/files/" + native_id + "/export", params={"mimeType": "text/plain"}, headers=auth
        )
        assert export.status_code == 200 and native_doc.paragraphs[0] in export.text
        native_media = await client.get(
            BASE + "/files/" + native_id, params={"alt": "media", "supportsAllDrives": "true"}, headers=auth
        )
        assert native_media.status_code == 403
        assert native_media.json()["error"]["errors"][0]["reason"] == "fileNotDownloadable"
        token = (
            await client.get(BASE + "/changes/startPageToken", params={"driveId": did}, headers=auth)
        ).json()["startPageToken"]
        changes = await client.get(
            BASE + "/changes", params={"pageToken": token, "includeRemoved": "true"}, headers=auth
        )
        assert changes.json()["changes"] == [] and changes.json()["newStartPageToken"] == token


async def test_acl_visibility_restricted_items_and_app_impersonation(make_client: Any) -> None:
    async with make_client("google") as client:
        ctx = client.hub.context("google")
        maya = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        hr = bearer(await mint(client, "https://www.googleapis.com", user="p031"))
        app = bearer(await mint(client, "https://www.googleapis.com", app_only=True))
        delegated = bearer(
            await mint(
                client,
                "https://www.googleapis.com",
                app_only=True,
                subject="maya.chen@halcyonadvisory.example",
            )
        )
        leadership = next(d for d in ctx.world.documents if d.space_id == "leadership" and not d.restricted)
        fid = drive_id(ctx.world, "file", leadership.id)
        assert (
            await client.get(BASE + "/files/" + fid, params={"supportsAllDrives": "true"}, headers=hr)
        ).status_code == 404
        assert (
            await client.get(BASE + "/files/" + fid, params={"supportsAllDrives": "true"}, headers=maya)
        ).status_code == 200
        assert (
            await client.get(BASE + "/files/" + fid, params={"supportsAllDrives": "true"}, headers=delegated)
        ).status_code == 200
        assert (
            await client.get(BASE + "/files/" + fid, params={"supportsAllDrives": "true"}, headers=app)
        ).status_code == 404
        assert (await client.get(BASE + "/files", headers=app)).json()["files"] == []
        restricted = next(d for d in ctx.world.documents if d.restricted and d.space_id == "people")
        restricted_id = drive_id(ctx.world, "file", restricted.id)
        assert (
            await client.get(
                BASE + "/files/" + restricted_id, params={"supportsAllDrives": "true"}, headers=maya
            )
        ).status_code == 404
        assert (
            await client.get(
                BASE + "/files/" + restricted_id, params={"supportsAllDrives": "true"}, headers=hr
            )
        ).status_code == 200
        knowledge = next(d for d in ctx.world.documents if d.space_id == "knowledge" and not d.restricted)
        junior = bearer(await mint(client, "https://www.googleapis.com", user="p009"))
        denied = await client.patch(
            BASE + "/files/" + drive_id(ctx.world, "file", knowledge.id),
            params={"supportsAllDrives": "true"},
            json={"name": "unauthorized"},
            headers=junior,
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["errors"][0]["reason"] == "insufficientFilePermissions"


async def test_mutations_sharing_ownership_changes_range_and_revisions(make_client: Any) -> None:
    async with make_client("google") as client:
        owner = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        reader = bearer(await mint(client, "https://www.googleapis.com", user="p031"))
        token = (await client.get(BASE + "/changes/startPageToken", headers=owner)).json()["startPageToken"]
        uploaded = await client.post(
            UPLOAD,
            params={"uploadType": "media", "fields": "*"},
            content=b"Hello world",
            headers={**owner, "Content-Type": "text/plain"},
        )
        assert uploaded.status_code == 200, uploaded.text
        fid = uploaded.json()["id"]
        assert uploaded.json()["size"] == "11"
        assert (await client.get(BASE + "/files/" + fid, headers=reader)).status_code == 404
        perm = await client.post(
            BASE + "/files/" + fid + "/permissions",
            params={"fields": "*"},
            headers=owner,
            json={"type": "user", "role": "reader", "emailAddress": "olivia.grant@halcyonadvisory.example"},
        )
        assert perm.status_code == 200
        pid = perm.json()["id"]
        assert pid.isdigit()
        assert (await client.get(BASE + "/files/" + fid, headers=reader)).status_code == 200
        assert (
            await client.patch(BASE + "/files/" + fid, headers=reader, json={"name": "denied"})
        ).status_code == 403
        shared = (
            await client.get(
                BASE + "/files", params={"q": "sharedWithMe", "fields": "files(id)"}, headers=reader
            )
        ).json()["files"]
        assert {"id": fid} in shared
        part = await client.get(
            BASE + "/files/" + fid, params={"alt": "media"}, headers={**reader, "Range": "bytes=1-4"}
        )
        assert part.status_code == 206 and part.content == b"ello"
        assert part.headers["content-range"] == "bytes 1-4/11" and part.headers["content-length"] == "4"
        suffix = await client.get(
            BASE + "/files/" + fid, params={"alt": "media"}, headers={**reader, "Range": "bytes=-5"}
        )
        assert suffix.content == b"world"
        assert (
            await client.get(
                BASE + "/files/" + fid, params={"alt": "media"}, headers={**reader, "Range": "bytes=100-200"}
            )
        ).status_code == 416
        assert (
            await client.patch(
                BASE + "/files/" + fid + "/permissions/" + pid, headers=owner, json={"role": "writer"}
            )
        ).status_code == 200
        assert (
            await client.patch(BASE + "/files/" + fid, headers=reader, json={"name": "changed.txt"})
        ).status_code == 200
        updated = await client.patch(
            UPLOAD + "/" + fid,
            params={"uploadType": "media"},
            headers={**owner, "Content-Type": "text/plain"},
            content=b"Updated content",
        )
        assert updated.status_code == 200
        revisions = (await client.get(BASE + "/files/" + fid + "/revisions", headers=owner)).json()[
            "revisions"
        ]
        assert len(revisions) == 3
        old = await client.get(
            BASE + "/files/" + fid + "/revisions/1", params={"alt": "media"}, headers=owner
        )
        assert old.content == b"Hello world"
        assert (await client.get(BASE + "/files/" + fid + "/revisions/3", headers=owner)).json()["id"] == "3"
        assert (
            await client.delete(BASE + "/files/" + fid + "/permissions/" + pid, headers=owner)
        ).status_code == 204
        assert (await client.get(BASE + "/files/" + fid, headers=reader)).status_code == 404
        assert (await client.delete(BASE + "/files/" + fid, headers=owner)).status_code == 204
        changes = (
            await client.get(
                BASE + "/changes",
                params={"pageToken": token, "includeRemoved": "true", "pageSize": 100},
                headers=owner,
            )
        ).json()
        assert changes["changes"][-1]["fileId"] == fid and changes["changes"][-1]["removed"] is True
        assert "file" not in changes["changes"][-1]
        assert int(changes["newStartPageToken"]) > int(token)


async def test_folders_move_copy_trash_generateids_and_labels(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        ids = (await client.get(BASE + "/files/generateIds", params={"count": 2}, headers=auth)).json()["ids"]
        assert len(set(ids)) == 2
        folder = (
            await client.post(
                BASE + "/files", headers=auth, json={"id": ids[0], "name": "New Folder", "mimeType": FOLDER}
            )
        ).json()
        assert folder["id"] == ids[0]
        uploaded = (
            await client.post(UPLOAD, params={"uploadType": "media"}, content=b"copy me", headers=auth)
        ).json()
        fid = uploaded["id"]
        root = root_id(client.hub.context("google"), "p018")
        moved = await client.patch(
            BASE + "/files/" + fid,
            params={"addParents": folder["id"], "removeParents": root, "fields": "parents"},
            json={},
            headers=auth,
        )
        assert moved.json() == {"parents": [folder["id"]]}
        copied = (
            await client.post(
                BASE + "/files/" + fid + "/copy",
                headers=auth,
                json={"name": "copy.txt", "parents": [folder["id"]]},
            )
        ).json()
        assert (
            await client.get(BASE + "/files/" + copied["id"], params={"alt": "media"}, headers=auth)
        ).content == b"copy me"
        assert (await client.get(BASE + "/files/" + fid + "/listLabels", headers=auth)).json() == {
            "kind": "drive#labelList",
            "labels": [],
        }
        trashed = await client.patch(
            BASE + "/files/" + fid,
            params={"fields": "trashed,explicitlyTrashed"},
            headers=auth,
            json={"trashed": True},
        )
        assert trashed.json() == {"trashed": True, "explicitlyTrashed": True}
        assert (await client.delete(BASE + "/files/trash", headers=auth)).status_code == 204
        assert (await client.get(BASE + "/files/" + fid, headers=auth)).status_code == 404
        assert (await client.delete(BASE + "/files/" + folder["id"], headers=auth)).status_code == 204
        assert (await client.get(BASE + "/files/" + copied["id"], headers=auth)).status_code == 404


async def test_multipart_and_resumable_upload_status_chunk_update_and_validation(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        body = b'--boundary\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{"name":"multipart.txt"}\r\n--boundary\r\nContent-Type: text/plain\r\n\r\nhello multipart\r\n--boundary--\r\n'
        response = await client.post(
            UPLOAD,
            params={"uploadType": "multipart", "fields": "id,name,size"},
            headers={**auth, "Content-Type": 'multipart/related; boundary="boundary"'},
            content=body,
        )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "multipart.txt" and response.json()["size"] == "15"
        start = await client.post(
            UPLOAD,
            params={"uploadType": "resumable", "fields": "id,name,size"},
            json={"name": "resume.txt"},
            headers={**auth, "X-Upload-Content-Type": "text/plain", "X-Upload-Content-Length": "10"},
        )
        location = start.headers["location"]
        first = await client.put(
            location, content=b"abcde", headers={**auth, "Content-Range": "bytes 0-4/10"}
        )
        assert first.status_code == 308 and first.headers["range"] == "bytes=0-4"
        status = await client.put(location, headers={**auth, "Content-Range": "bytes */10"})
        assert status.status_code == 308 and status.headers["range"] == "bytes=0-4"
        wrong = await client.put(
            location, content=b"fghij", headers={**auth, "Content-Range": "bytes 4-8/10"}
        )
        assert wrong.status_code == 400
        final = await client.put(
            location, content=b"fghij", headers={**auth, "Content-Range": "bytes 5-9/10"}
        )
        assert final.status_code == 200 and final.json()["size"] == "10"
        fid = final.json()["id"]
        assert (
            await client.get(BASE + "/files/" + fid, params={"alt": "media"}, headers=auth)
        ).content == b"abcdefghij"
        update = await client.patch(
            UPLOAD + "/" + fid,
            params={"uploadType": "resumable"},
            json={"name": "resumed-update.txt"},
            headers={**auth, "X-Upload-Content-Type": "text/plain"},
        )
        done = await client.put(update.headers["location"], headers=auth, content=b"replacement")
        assert done.status_code == 200 and done.json()["name"] == "resumed-update.txt"


async def test_shared_drive_crud_admin_access_hide_and_inherited_permissions(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        data = {"name": "Test Shared Drive"}
        new = await client.post(BASE + "/drives", params={"requestId": "idempotent"}, headers=auth, json=data)
        assert new.status_code == 200, new.text
        did = new.json()["id"]
        assert did.startswith("0A") and len(did) == 19
        duplicate = await client.post(
            BASE + "/drives", params={"requestId": "idempotent"}, headers=auth, json=data
        )
        assert duplicate.json()["id"] == did
        assert (await client.patch(BASE + "/drives/" + did, headers=auth, json={"name": "Renamed"})).json()[
            "name"
        ] == "Renamed"
        assert (await client.post(BASE + "/drives/" + did + "/hide", headers=auth)).json()["hidden"] is True
        assert (await client.post(BASE + "/drives/" + did + "/unhide", headers=auth)).json()[
            "hidden"
        ] is False
        assert (await client.get(BASE + "/drives/" + did, headers=auth)).json()["capabilities"][
            "canManageMembers"
        ] is True
        assert (await client.delete(BASE + "/drives/" + did, headers=auth)).status_code == 204
        assert (await client.get(BASE + "/drives/" + did, headers=auth)).status_code == 404
        admin = bearer(await mint(client, "https://www.googleapis.com", user="p035"))
        all_drives = (
            await client.get(
                BASE + "/drives",
                params={"useDomainAdminAccess": "true", "fields": "drives(id,name)"},
                headers=admin,
            )
        ).json()["drives"]
        assert len(all_drives) == 7


async def test_push_notifications_file_changes_stop_and_header_numbers(make_client: Any) -> None:
    async with make_client("google") as client:
        received: list[tuple[dict[str, str], bytes]] = []
        receiver = FastAPI()

        @receiver.post("/hook")
        async def hook(request: Request) -> dict[str, Any]:
            received.append((dict(request.headers), await request.body()))
            return {}

        client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        fid = (await client.post(BASE + "/files", headers=auth, json={"name": "Watched"})).json()["id"]
        token = (await client.get(BASE + "/changes/startPageToken", headers=auth)).json()["startPageToken"]
        channel = {
            "id": "file-channel",
            "type": "web_hook",
            "address": "http://consumer.example/hook",
            "token": "channel-secret",
        }
        response = await client.post(BASE + "/files/" + fid + "/watch", headers=auth, json=channel)
        assert response.status_code == 200
        changes_watch = await client.post(
            BASE + "/changes/watch",
            params={"pageToken": token},
            headers=auth,
            json={**channel, "id": "change-channel"},
        )
        assert changes_watch.status_code == 200
        assert [h["x-goog-resource-state"] for h, _ in received] == ["sync", "sync"]
        await client.patch(BASE + "/files/" + fid, headers=auth, json={"name": "Changed"})
        assert [h["x-goog-resource-state"] for h, _ in received][-2:] == ["update", "change"]
        assert received[-2][0]["x-goog-message-number"] == "2"
        assert received[-2][0]["x-goog-channel-token"] == "channel-secret"
        assert received[-2][0]["x-goog-changed"] == "properties"
        assert all(b == b"" for _, b in received)
        assert (
            await client.post(
                BASE + "/channels/stop",
                headers=auth,
                json={"id": response.json()["id"], "resourceId": response.json()["resourceId"]},
            )
        ).status_code == 204
        await client.patch(BASE + "/files/" + fid, headers=auth, json={"trashed": True})
        assert received[-1][0]["x-goog-channel-id"] == "change-channel"


async def test_meet_artifact_ids_export_and_download(make_client: Any) -> None:
    async with make_client("google") as client:
        ctx = client.hub.context("google")
        meeting = next(m for m in ctx.world.meetings if m.platform == "meet" and m.recorded and m.transcribed)
        auth = bearer(await mint(client, "https://www.googleapis.com", user=meeting.organizer_id))
        recording = meet_recording_file_id(ctx.world, meeting.id)
        transcript = meet_transcript_doc_id(ctx.world, meeting.id)
        rec = await client.get(
            BASE + "/files/" + recording,
            params={"fields": "id,mimeType,parents,videoMediaMetadata"},
            headers=auth,
        )
        assert rec.json()["mimeType"] == "video/mp4"
        data = await client.get(BASE + "/files/" + recording, params={"alt": "media"}, headers=auth)
        assert data.status_code == 200 and data.content[4:8] == b"ftyp"
        exported = await client.get(
            BASE + "/files/" + transcript + "/export", params={"mimeType": "text/plain"}, headers=auth
        )
        assert exported.status_code == 200 and meeting.transcript[0].text in exported.text
        attendee = bearer(await mint(client, "https://www.googleapis.com", user=meeting.attendee_ids[0]))
        assert (await client.get(BASE + "/files/" + transcript, headers=attendee)).status_code == 200


async def test_docs_create_atomic_batch_update_export_utf16_and_acl(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        docs = "https://docs.googleapis.com/v1/documents"
        created = await client.post(docs, headers=auth, json={"title": "Test Doc"})
        assert created.status_code == 200, created.text
        fid = created.json()["documentId"]
        edited = await client.post(
            docs + "/" + fid + ":batchUpdate",
            headers=auth,
            json={
                "requests": [
                    {"insertText": {"location": {"index": 1}, "text": "Hello 😀 world"}},
                    {
                        "replaceAllText": {
                            "containsText": {"text": "world", "matchCase": True},
                            "replaceText": "Google",
                        }
                    },
                ]
            },
        )
        assert edited.status_code == 200
        assert edited.json()["replies"][1]["replaceAllText"]["occurrencesChanged"] == 1
        result = (await client.get(docs + "/" + fid, headers=auth)).json()
        paragraph = result["body"]["content"][1]
        assert paragraph["paragraph"]["elements"][0]["textRun"]["content"] == "Hello 😀 Google\n"
        assert paragraph["endIndex"] == 17
        exported = await client.get(
            BASE + "/files/" + fid + "/export", params={"mimeType": "text/plain"}, headers=auth
        )
        assert exported.text == "Hello 😀 Google\n"
        failed = await client.post(
            docs + "/" + fid + ":batchUpdate",
            headers=auth,
            json={
                "requests": [
                    {"insertText": {"location": {"index": 1}, "text": "Do not commit"}},
                    {"unsupported": {}},
                ]
            },
        )
        assert failed.status_code == 400
        assert (
            await client.get(
                BASE + "/files/" + fid + "/export", params={"mimeType": "text/plain"}, headers=auth
            )
        ).text == exported.text
        reader = bearer(await mint(client, "https://www.googleapis.com", user="p031"))
        assert (await client.get(docs + "/" + fid, headers=reader)).status_code == 404
        pdf = await client.get(
            BASE + "/files/" + fid + "/export", params={"mimeType": "application/pdf"}, headers=auth
        )
        assert pdf.content.startswith(b"%PDF")
        assert (
            await client.get(
                BASE + "/files/" + fid + "/export", params={"mimeType": "video/mp4"}, headers=auth
            )
        ).status_code == 400


@pytest.mark.parametrize(
    "params",
    [
        {"q": "name ="},
        {"q": "unknown = 'test'"},
        {"q": "trashed = maybe"},
        {"q": "name contains 'unterminated"},
        {"q": "properties has { value='x' }"},
        {"fields": "files(id,,name)"},
        {"corpora": "drive"},
        {"pageToken": "garbage"},
        {"pageSize": 0},
        {"orderBy": "unknown"},
    ],
)
async def test_drive_invalid_queries_and_fields_are_vendor_errors(make_client: Any, params: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        response = await client.get(BASE + "/files", params=params, headers=auth)
        assert response.status_code == 400, response.text
        assert response.json()["error"]["errors"][0]["reason"] in ("invalid", "invalidParameter")


async def test_prefix_mode_links_and_reset(make_client: Any) -> None:
    async with make_client("google") as client:
        transport = httpx.ASGITransport(app=client.hub.gateway)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080/google") as prefix:
            auth = bearer(await mint(prefix, "", user="p018"))
            response = await prefix.get(
                "/drive/v3/files/root", params={"fields": "id,webViewLink"}, headers=auth
            )
            assert response.status_code == 200
            assert response.json()["webViewLink"].startswith("http://localhost:44080/google/")
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        fid = (await client.post(BASE + "/files", headers=auth, json={"name": "Reset me"})).json()["id"]
        await client.post("https://www.googleapis.com/_emulator/reset")
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        assert (await client.get(BASE + "/files/" + fid, headers=auth)).status_code == 404
