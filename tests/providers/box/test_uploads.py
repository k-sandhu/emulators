"""Simple, versioned and chunked uploads on upload.box.com."""

from __future__ import annotations

import base64
import hashlib
import json

UPLOAD = "https://upload.box.com/api/2.0"


def digest(data: bytes) -> str:
    return "sha=" + base64.b64encode(hashlib.sha1(data).digest()).decode()


async def test_multipart_upload_returns_entries_with_the_new_file(box):
    parent = await box.find("Technology")
    r = await box.upload(
        parent,
        "runbook.md",
        b"# Runbook\n",
        content_created_at="2026-01-02T03:04:05Z",
        content_modified_at="2026-01-03T03:04:05-05:00",
        description="Ops runbook",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total_count"] == 1
    file = body["entries"][0]
    assert file["type"] == "file" and file["name"] == "runbook.md" and file["size"] == 10
    assert file["parent"]["id"] == parent and file["etag"] == "0" and file["sequence_id"] == "0"
    assert file["sha1"] == hashlib.sha1(b"# Runbook\n").hexdigest()
    assert file["content_created_at"] == "2026-01-01T19:04:05-08:00"
    assert file["content_modified_at"] == "2026-01-03T00:04:05-08:00"
    assert file["description"] == "Ops runbook"
    assert file["created_by"]["name"] == "Arjun Mehta" and file["owned_by"]["name"] == "Arjun Mehta"
    # The uploaded bytes are what downloads return.
    location = (await box.get(f"/files/{file['id']}/content")).headers["location"]
    assert (await box.client.get(location)).content == b"# Runbook\n"


async def test_uploading_into_someone_elses_folder_makes_the_folder_owner_the_owner(box):
    folder = await box.find("Client Engagements/Northwind Logistics/Correspondence", "p004")
    r = await box.upload(folder, "note.txt", b"hi", "p004")
    assert r.status_code == 201
    file = r.json()["entries"][0]
    assert file["created_by"]["name"] == "Daniel Kowalski"
    assert file["owned_by"]["name"] == "Arjun Mehta"


async def test_upload_name_conflicts_return_the_existing_file(box):
    parent = await box.find("Technology")
    first = (await box.upload(parent, "dup.txt", b"1")).json()["entries"][0]
    r = await box.upload(parent, "DUP.txt", b"2")
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "item_name_in_use" and body["status"] == 409
    conflict = body["context_info"]["conflicts"]
    assert conflict["type"] == "file" and conflict["id"] == first["id"] and conflict["sha1"] == first["sha1"]


async def test_upload_validation_and_digest_checks(box):
    parent = await box.find("Technology")
    data = b"checksummed"
    good = await box.client.post(
        f"{UPLOAD}/files/content",
        headers={**box.auth(), "Content-MD5": hashlib.sha1(data).hexdigest()},
        files={
            "attributes": (None, json.dumps({"name": "sum.txt", "parent": {"id": parent}})),
            "file": ("sum.txt", data),
        },
    )
    assert good.status_code == 201
    bad = await box.client.post(
        f"{UPLOAD}/files/content",
        headers={**box.auth(), "Content-MD5": "0" * 40},
        files={
            "attributes": (None, json.dumps({"name": "sum2.txt", "parent": {"id": parent}})),
            "file": ("x", data),
        },
    )
    assert bad.status_code == 400 and bad.json()["code"] == "bad_digest"
    assert "sum2.txt" not in [e["name"] for e in await box.items(parent)]
    no_file = await box.client.post(
        f"{UPLOAD}/files/content",
        headers=box.auth(),
        files={"attributes": (None, json.dumps({"name": "x.txt", "parent": {"id": parent}}))},
    )
    assert no_file.status_code == 400
    no_parent = await box.upload("999", "x.txt", b"x")
    assert no_parent.status_code == 404
    no_name = await box.client.post(
        f"{UPLOAD}/files/content",
        headers=box.auth(),
        files={"attributes": (None, json.dumps({"parent": {"id": parent}})), "file": ("", b"x")},
    )
    assert no_name.status_code == 400
    bad_attrs = await box.client.post(
        f"{UPLOAD}/files/content",
        headers=box.auth(),
        files={"attributes": (None, "{not json"), "file": ("x", b"x")},
    )
    assert bad_attrs.status_code == 400
    assert (await box.client.post(f"{UPLOAD}/files/content", headers={})).status_code == 401


async def test_viewers_cannot_upload(box):
    folder = await box.find("Knowledge Base", "p044")  # everyone is a viewer on the knowledge base
    denied = await box.upload(folder, "x.txt", b"x", "p044")
    assert denied.status_code == 403
    assert denied.json()["code"] == "access_denied_insufficient_permissions"
    managers = await box.upload(folder, "guidance.txt", b"x", "p019")  # managers are editors there
    assert managers.status_code == 201


async def test_preflight_check(box):
    parent = await box.find("Technology")
    await box.upload(parent, "exists.txt", b"x")
    ok = await box.client.request(
        "OPTIONS",
        "https://api.box.com/2.0/files/content",
        headers=box.auth(),
        json={"name": "fresh.txt", "parent": {"id": parent}, "size": 10},
    )
    assert ok.status_code == 200
    assert ok.json()["upload_url"] == "https://upload.box.com/api/2.0/files/content"
    taken = await box.client.request(
        "OPTIONS",
        "https://api.box.com/2.0/files/content",
        headers=box.auth(),
        json={"name": "exists.txt", "parent": {"id": parent}},
    )
    assert taken.status_code == 409 and taken.json()["code"] == "item_name_in_use"
    huge = await box.client.request(
        "OPTIONS",
        "https://api.box.com/2.0/files/content",
        headers=box.auth(),
        json={"name": "huge.bin", "parent": {"id": parent}, "size": 60 * 1024**3},
    )
    assert huge.status_code == 413


async def test_new_version_upload_honours_if_match_and_rename(box):
    parent = await box.find("Technology")
    created = (await box.upload(parent, "doc.txt", b"one")).json()["entries"][0]
    stale = await box.client.post(
        f"{UPLOAD}/files/{created['id']}/content",
        headers={**box.auth(), "If-Match": "9"},
        files={"file": ("doc.txt", b"two")},
    )
    assert stale.status_code == 412
    ok = await box.client.post(
        f"{UPLOAD}/files/{created['id']}/content",
        headers={**box.auth(), "If-Match": "0"},
        files={
            "attributes": (
                None,
                json.dumps({"name": "doc v2.txt", "content_modified_at": "2026-02-03T04:05:06Z"}),
            ),
            "file": ("doc.txt", b"two"),
        },
    )
    assert ok.status_code == 201
    file = ok.json()["entries"][0]
    assert file["name"] == "doc v2.txt" and file["etag"] == "1" and file["size"] == 3
    assert file["file_version"]["id"] != created["file_version"]["id"]
    assert file["content_modified_at"] == "2026-02-02T20:05:06-08:00"
    versions = (await box.get(f"/files/{created['id']}/versions")).json()
    assert [v["id"] for v in versions["entries"]] == [created["file_version"]["id"]]


async def configure_parts(box, size: int) -> None:
    await box.action("configure_uploads", {"part_size": size})


async def test_chunked_upload_end_to_end(box):
    await configure_parts(box, 10)
    parent = await box.find("Technology")
    payload = b"0123456789abcdefghij-tail"  # 3 parts of 10, 10 and 5 bytes
    created = await box.post(
        f"{UPLOAD}/files/upload_sessions",
        json={"folder_id": parent, "file_size": len(payload), "file_name": "big.bin"},
    )
    assert created.status_code == 201, created.text
    session = created.json()
    assert session["type"] == "upload_session" and session["part_size"] == 10 and session["total_parts"] == 3
    assert session["num_parts_processed"] == 0 and len(session["id"]) == 32
    endpoints = session["session_endpoints"]
    assert endpoints["upload_part"].startswith("https://upload.box.com/api/2.0/files/upload_sessions/")
    assert set(endpoints) >= {"abort", "commit", "list_parts", "log_event", "status", "upload_part"}
    parts = []
    for offset in (20, 0, 10):  # parts may arrive in any order
        chunk = payload[offset : offset + 10]
        r = await box.client.put(
            endpoints["upload_part"],
            headers={
                **box.auth(),
                "Digest": digest(chunk),
                "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{len(payload)}",
                "Content-Type": "application/octet-stream",
            },
            content=chunk,
        )
        assert r.status_code == 200, r.text
        part = r.json()["part"]
        assert part["offset"] == offset and part["size"] == len(chunk)
        assert part["sha1"] == hashlib.sha1(chunk).hexdigest() and len(part["part_id"]) == 8
        parts.append(part)
    listed = (await box.client.get(endpoints["list_parts"], headers=box.auth())).json()
    assert listed["total_count"] == 3 and [p["offset"] for p in listed["entries"]] == [0, 10, 20]
    status = (await box.client.get(endpoints["status"], headers=box.auth())).json()
    assert status["num_parts_processed"] == 3
    wrong = await box.client.post(
        endpoints["commit"], headers={**box.auth(), "Digest": digest(b"other")}, json={"parts": parts}
    )
    assert wrong.status_code == 400 and wrong.json()["code"] == "bad_digest"
    done = await box.client.post(
        endpoints["commit"], headers={**box.auth(), "Digest": digest(payload)}, json={"parts": parts}
    )
    assert done.status_code == 201, done.text
    file = done.json()["entries"][0]
    assert file["name"] == "big.bin" and file["size"] == len(payload) and file["parent"]["id"] == parent
    assert file["sha1"] == hashlib.sha1(payload).hexdigest()
    location = (await box.get(f"/files/{file['id']}/content")).headers["location"]
    assert (await box.client.get(location)).content == payload
    # The session is gone once committed.
    assert (await box.client.get(endpoints["status"], headers=box.auth())).status_code == 404


async def test_chunked_upload_validation_and_abort(box):
    await configure_parts(box, 8)
    parent = await box.find("Technology")
    await box.upload(parent, "taken.bin", b"x")
    taken = await box.post(
        f"{UPLOAD}/files/upload_sessions",
        json={"folder_id": parent, "file_size": 16, "file_name": "taken.bin"},
    )
    assert taken.status_code == 409
    created = await box.post(
        f"{UPLOAD}/files/upload_sessions", json={"folder_id": parent, "file_size": 16, "file_name": "x.bin"}
    )
    session = created.json()
    url = session["session_endpoints"]["upload_part"]
    chunk = b"12345678"

    def headers(**extra):
        return {**box.auth(), "Digest": digest(chunk), "Content-Range": "bytes 0-7/16", **extra}

    assert (
        await box.client.put(url, headers={**headers(), "Digest": digest(b"nope")}, content=chunk)
    ).status_code == 400
    no_digest = {k: v for k, v in headers().items() if k != "Digest"}
    assert (await box.client.put(url, headers=no_digest, content=chunk)).status_code == 400
    assert (
        await box.client.put(url, headers=headers(**{"Content-Range": "bytes 3-10/16"}), content=chunk)
    ).status_code == 400
    assert (
        await box.client.put(url, headers=headers(**{"Content-Range": "bytes 0-7/99"}), content=chunk)
    ).status_code == 400
    assert (await box.client.put(url, headers=headers(), content=chunk)).status_code == 200
    missing_part = await box.client.post(
        session["session_endpoints"]["commit"], headers={**box.auth(), "Digest": digest(chunk)}, json={}
    )
    assert missing_part.status_code == 400
    # Another user cannot touch the session.
    assert (await box.client.get(url, headers=box.auth("p004"))).status_code == 404
    assert (await box.client.delete(url, headers=box.auth())).status_code == 204
    assert (await box.client.put(url, headers=headers(), content=chunk)).status_code == 404


async def test_chunked_upload_of_a_new_version(box):
    await configure_parts(box, 6)
    parent = await box.find("Technology")
    created = (await box.upload(parent, "versioned.bin", b"first")).json()["entries"][0]
    payload = b"second-version"
    session = (
        await box.post(f"{UPLOAD}/files/{created['id']}/upload_sessions", json={"file_size": len(payload)})
    ).json()
    parts = []
    for offset in range(0, len(payload), 6):
        chunk = payload[offset : offset + 6]
        part = await box.client.put(
            session["session_endpoints"]["upload_part"],
            headers={
                **box.auth(),
                "Digest": digest(chunk),
                "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{len(payload)}",
            },
            content=chunk,
        )
        parts.append(part.json()["part"])
    done = await box.client.post(
        session["session_endpoints"]["commit"],
        headers={**box.auth(), "Digest": digest(payload)},
        json={"parts": parts},
    )
    assert done.status_code == 201
    file = done.json()["entries"][0]
    assert file["id"] == created["id"] and file["etag"] == "1" and file["size"] == len(payload)
    assert len((await box.get(f"/files/{created['id']}/versions")).json()["entries"]) == 1


async def test_attributes_may_arrive_as_a_plain_form_field_or_a_file_part(box):
    parent = await box.find("Technology")
    as_field = await box.client.post(
        f"{UPLOAD}/files/content",
        headers=box.auth(),
        data={"attributes": json.dumps({"name": "field.txt", "parent": {"id": parent}})},
        files={"file": ("field.txt", b"form field style")},
    )
    assert as_field.status_code == 201, as_field.text
    as_part = await box.client.post(
        f"{UPLOAD}/files/content",
        headers=box.auth(),
        files={
            "attributes": (
                "attributes.json",
                json.dumps({"name": "part.txt", "parent": {"id": parent}}).encode(),
            ),
            "file": ("part.txt", b"file part style"),
        },
    )
    assert as_part.status_code == 201, as_part.text
    fallback_name = await box.client.post(
        f"{UPLOAD}/files/content",
        headers=box.auth(),
        data={"attributes": json.dumps({"parent": {"id": parent}})},
        files={"file": ("from-filename.txt", b"x")},
    )
    assert (
        fallback_name.status_code == 201 and fallback_name.json()["entries"][0]["name"] == "from-filename.txt"
    )
