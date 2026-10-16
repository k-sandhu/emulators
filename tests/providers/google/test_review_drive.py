"""Regression contracts for Drive review findings."""

from __future__ import annotations

import re
from typing import Any

import pytest

from emulators.testing.helpers import bearer, mint

BASE = "https://www.googleapis.com"
DRIVE = BASE + "/drive/v3"


async def test_resumable_session_is_self_authenticating(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        started = await client.post(
            BASE + "/upload/drive/v3/files?uploadType=resumable&fields=id,name,size",
            headers={**auth, "X-Upload-Content-Type": "text/plain", "X-Upload-Content-Length": "6"},
            json={"name": "session.txt"},
        )
        session = started.headers["location"]
        first = await client.put(session, content=b"abc", headers={"Content-Range": "bytes 0-2/6"})
        assert first.status_code == 308
        status = await client.put(session, headers={"Content-Range": "bytes */6"})
        assert status.headers["range"] == "bytes=0-2"
        # Even an expired/invalid bearer must not invalidate the session capability.
        done = await client.put(
            session,
            content=b"def",
            headers={"Content-Range": "bytes 3-5/6", "Authorization": "Bearer invalid"},
        )
        assert done.status_code == 200 and done.json()["size"] == "6"
        assert (
            await client.get(DRIVE + "/files/" + done.json()["id"], headers=auth, params={"alt": "media"})
        ).content == b"abcdef"
        assert (
            await client.put(session.replace("upload_id=", "upload_id=bad"), content=b"x")
        ).status_code == 404


async def test_query_word_prefix_mime_boolean_and_fulltext(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        created = (
            await client.post(
                DRIVE + "/files",
                headers=auth,
                json={
                    "name": "Annual ReportPhoto",
                    "mimeType": "image/png",
                    "description": "alpha gap beta hello_world",
                },
            )
        ).json()
        for query in (
            "name contains 'Report'",
            "mimeType contains 'image/'",
            "trashed != true",
            "fullText contains 'alpha beta'",
            "fullText contains '\"hello world\"'",
        ):
            rows = (await client.get(DRIVE + "/files", headers=auth, params={"q": query})).json()["files"]
            assert created["id"] in {r["id"] for r in rows}, query
        rows = (
            await client.get(DRIVE + "/files", headers=auth, params={"q": "name contains 'Photo'"})
        ).json()["files"]
        assert created["id"] not in {r["id"] for r in rows}


@pytest.mark.parametrize("discovery", [False, True])
async def test_domain_discovery_and_known_id(make_client: Any, discovery: bool) -> None:
    async with make_client("google") as client:
        owner = bearer(await mint(client, BASE, user="p018"))
        other = bearer(await mint(client, BASE, user="p035"))
        fid = (await client.post(DRIVE + "/files", headers=owner, json={"name": "Domain-only"})).json()["id"]
        assert (
            await client.post(
                DRIVE + f"/files/{fid}/permissions",
                headers=owner,
                json={
                    "type": "domain",
                    "domain": "halcyonadvisory.example",
                    "role": "reader",
                    "allowFileDiscovery": discovery,
                },
            )
        ).status_code == 200
        assert (await client.get(DRIVE + "/files/" + fid, headers=other)).status_code == 200
        assert (
            await client.patch(DRIVE + "/files/" + fid, headers=other, json={"name": "denied"})
        ).status_code == 403
        for corpus in ("user", "domain", "allDrives"):
            rows = (await client.get(DRIVE + "/files", headers=other, params={"corpora": corpus})).json()[
                "files"
            ]
            assert (fid in {r["id"] for r in rows}) is discovery


async def test_public_key_root_alias_trash_and_export(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        fid = (
            await client.post(
                BASE + "/upload/drive/v3/files?uploadType=media",
                headers={**auth, "Content-Type": "text/plain"},
                content=b"public bytes",
            )
        ).json()["id"]
        assert (await client.get(DRIVE + "/files/" + fid, params={"key": "test-key"})).status_code == 404
        await client.post(
            DRIVE + f"/files/{fid}/permissions", headers=auth, json={"type": "anyone", "role": "reader"}
        )
        assert (await client.get(DRIVE + "/files/" + fid, params={"key": "test-key"})).json()["id"] == fid
        assert (
            await client.get(DRIVE + "/files/" + fid, params={"key": "test-key", "alt": "media"})
        ).content == b"public bytes"
        folder = (
            await client.post(
                DRIVE + "/files",
                headers=auth,
                json={"name": "Folder", "mimeType": "application/vnd.google-apps.folder"},
            )
        ).json()["id"]
        moved = await client.patch(
            DRIVE + "/files/" + fid,
            headers=auth,
            params={"removeParents": "root", "addParents": folder, "fields": "parents"},
            json={},
        )
        assert moved.json()["parents"] == [folder]
        moved = await client.patch(
            DRIVE + "/files/" + fid,
            headers=auth,
            params={"removeParents": folder, "addParents": "root", "fields": "parents"},
            json={},
        )
        assert moved.json()["parents"] == [
            (await client.get(DRIVE + "/files/root", headers=auth)).json()["id"]
        ]
        trashed = (
            await client.patch(
                DRIVE + "/files/" + fid,
                headers=auth,
                params={"fields": "trashedTime,trashingUser(emailAddress),modifiedTime"},
                json={"trashed": True},
            )
        ).json()
        assert re.fullmatch(r".+\.\d{3}Z", trashed["trashedTime"])
        assert trashed["trashingUser"]["emailAddress"] == "maya.chen@halcyonadvisory.example"
        export = await client.get(
            DRIVE + f"/files/{fid}/export", headers=auth, params={"mimeType": "application/pdf"}
        )
        assert (
            export.status_code == 403 and export.json()["error"]["errors"][0]["reason"] == "fileNotExportable"
        )


async def test_documented_unmodelled_fields_and_millisecond_times(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        about = await client.get(
            DRIVE + "/about", headers=auth, params={"fields": "driveThemes,maxImportSizes,user(emailAddress)"}
        )
        assert about.status_code == 200 and set(about.json()) == {"user"}
        created = await client.post(
            DRIVE + "/files?fields=id,createdTime,modifiedTime,capabilities/*",
            headers=auth,
            json={"name": "Precision"},
        )
        assert created.status_code == 200
        for field in ("createdTime", "modifiedTime"):
            assert re.fullmatch(r".+\.\d{3}Z", created.json()[field])
        start = (await client.get(DRIVE + "/changes/startPageToken", headers=auth)).json()["startPageToken"]
        await client.patch(DRIVE + "/files/" + created.json()["id"], headers=auth, json={"name": "changed"})
        assert (
            await client.get(
                DRIVE + "/changes",
                headers=auth,
                params={"pageToken": start, "fields": "changes(type,fileId),newStartPageToken"},
            )
        ).status_code == 200


async def test_explicit_timestamps_normalize_and_bad_dates_do_not_mutate(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        created = await client.post(
            DRIVE + "/files?fields=id,createdTime,modifiedTime",
            headers=auth,
            json={
                "name": "Timestamp",
                "createdTime": "2026-01-01T00:00:00Z",
                "modifiedTime": "2026-01-02T00:00:00Z",
            },
        )
        assert created.status_code == 200
        assert created.json()["createdTime"] == "2026-01-01T00:00:00.000Z"
        path = DRIVE + "/files/" + created.json()["id"]
        revisions = (
            await client.get(path + "/revisions?fields=revisions(modifiedTime)", headers=auth)
        ).json()["revisions"]
        assert revisions[0]["modifiedTime"] == "2026-01-02T00:00:00.000Z"
        bad = await client.patch(
            path, headers=auth, json={"modifiedTime": "not a date", "name": "Must not persist"}
        )
        assert bad.status_code == 400
        assert (await client.get(path, headers=auth)).json()["name"] == "Timestamp"
        permission = await client.post(
            path + "/permissions?fields=id,expirationTime",
            headers=auth,
            json={
                "type": "user",
                "role": "reader",
                "emailAddress": "eleanor.whitfield@halcyonadvisory.example",
                "expirationTime": "2050-01-01T00:00:00Z",
            },
        )
        assert permission.status_code == 200
        assert permission.json()["expirationTime"] == "2050-01-01T00:00:00.000Z"
