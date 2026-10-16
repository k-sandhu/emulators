"""Writes: upload modes, sessions, folders, delete/move/copy, batches, restore, tags, locks."""

from __future__ import annotations

import json
from typing import Any

import pytest


async def test_upload_add_conflict_autorename_overwrite_update(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    first = await dbx.put(token, "/Drafts/plan.txt", b"v1")
    assert first["name"] == "plan.txt" and first["size"] == 2
    # add conflicts
    r = await dbx.upload(token, "files/upload", {"path": "/drafts/PLAN.txt"}, b"v2")
    assert r.status_code == 409
    err = r.json()
    dbx.check("files/upload", "error", err["error"])
    assert err["error_summary"].startswith("path/conflict/file")
    assert err["error"]["upload_session_id"]
    # autorename
    renamed = await dbx.put(token, "/Drafts/plan.txt", b"v2", autorename=True)
    assert renamed["name"] == "plan (1).txt"
    # overwrite keeps the id, new rev
    over = await dbx.put(token, "/Drafts/plan.txt", b"version three", mode="overwrite")
    assert over["id"] == first["id"] and over["rev"] != first["rev"] and over["size"] == 13
    # update with a stale rev creates a conflicted copy; strict_conflict rejects
    stale = await dbx.put(
        token, "/Drafts/plan.txt", b"stale", mode={".tag": "update", "update": first["rev"]}
    )
    assert "conflicted copy" in stale["name"] and stale["id"] != first["id"]
    r = await dbx.upload(
        token,
        "files/upload",
        {
            "path": "/Drafts/plan.txt",
            "mode": {".tag": "update", "update": first["rev"]},
            "strict_conflict": True,
        },
        b"x",
    )
    assert r.status_code == 409
    fresh = await dbx.put(token, "/Drafts/plan.txt", b"fresh", mode={".tag": "update", "update": over["rev"]})
    assert fresh["id"] == first["id"] and fresh["size"] == 5
    # a folder in the way
    await dbx.ok(token, "files/create_folder_v2", {"path": "/Drafts/dir"})
    r = await dbx.upload(token, "files/upload", {"path": "/Drafts/dir"}, b"x")
    assert r.json()["error_summary"].startswith("path/conflict/folder")


async def test_upload_creates_parents_validates_names_and_content_hash(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    meta = await dbx.put(token, "/a/b/c/deep.txt", b"deep")
    assert meta["path_display"] == "/a/b/c/deep.txt"
    folders = await dbx.list_all(token, "/a", recursive=True)
    assert [e["name"] for e in folders if e[".tag"] == "folder"] == ["b", "c"]
    r = await dbx.upload(token, "files/upload", {"path": "/bad:name.txt"}, b"x")
    assert r.status_code == 409 and r.json()["error_summary"].startswith("path/disallowed_name")
    r = await dbx.upload(token, "files/upload", {"path": "/hash.txt", "content_hash": "0" * 64}, b"data")
    assert r.status_code == 409 and r.json()["error"][".tag"] == "content_hash_mismatch"
    r = await dbx.upload(
        token, "files/upload", {"path": "/hash.txt"}, b"x" * 10, extra={"Content-Type": "application/json"}
    )
    assert r.status_code == 400 and "Content-Type" in r.text
    # a file in the middle of a path
    r = await dbx.upload(token, "files/upload", {"path": "/a/b/c/deep.txt/x"}, b"x")
    assert r.json()["error_summary"].startswith("path/conflict/file_ancestor")


async def test_client_modified_is_honoured(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    meta = await dbx.put(token, "/dated.txt", b"x", client_modified="2020-02-02T02:02:02Z")
    assert meta["client_modified"] == "2020-02-02T02:02:02Z"
    assert meta["server_modified"] > meta["client_modified"]


async def test_upload_session_sequential(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    start = await dbx.upload(token, "files/upload_session/start", {"close": False}, b"hello ")
    sid = start.json()["session_id"]
    assert (
        await dbx.upload(
            token, "files/upload_session/append_v2", {"cursor": {"session_id": sid, "offset": 6}}, b"big "
        )
    ).status_code == 200
    # wrong offset -> incorrect_offset with the right one
    r = await dbx.upload(
        token, "files/upload_session/append_v2", {"cursor": {"session_id": sid, "offset": 3}}, b"zzz"
    )
    assert r.status_code == 409
    assert r.json()["error"] == {".tag": "incorrect_offset", "correct_offset": 10}
    dbx.check("files/upload_session/append_v2", "error", r.json()["error"])
    commit = {"path": "/sessions/out.txt", "mode": {".tag": "add"}}
    r = await dbx.upload(
        token,
        "files/upload_session/finish",
        {"cursor": {"session_id": sid, "offset": 10}, "commit": commit},
        b"world",
    )
    assert r.status_code == 200, r.text
    dbx.check("files/upload_session/finish", "result", r.json())
    r2, _ = await dbx.download(token, "/sessions/out.txt")
    assert r2.content == b"hello big world"
    # session is gone after finish
    r = await dbx.upload(
        token,
        "files/upload_session/finish",
        {"cursor": {"session_id": sid, "offset": 15}, "commit": commit},
        b"",
    )
    assert r.json()["error"]["lookup_failed"][".tag"] == "not_found"
    # closed sessions refuse more data
    sid2 = (await dbx.upload(token, "files/upload_session/start", {"close": True}, b"a")).json()["session_id"]
    r = await dbx.upload(
        token, "files/upload_session/append_v2", {"cursor": {"session_id": sid2, "offset": 1}}, b"b"
    )
    assert r.json()["error"][".tag"] == "closed"


async def test_upload_session_finish_batch(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    sids = []
    for body in (b"one", b"two!"):
        sids.append(
            (await dbx.upload(token, "files/upload_session/start", {"close": True}, body)).json()[
                "session_id"
            ]
        )
    entries = [
        {"cursor": {"session_id": sids[0], "offset": 3}, "commit": {"path": "/batch/one.txt"}},
        {"cursor": {"session_id": sids[1], "offset": 99}, "commit": {"path": "/batch/two.txt"}},
    ]
    data = await dbx.ok(token, "files/upload_session/finish_batch_v2", {"entries": entries})
    assert data["entries"][0][".tag"] == "success" and data["entries"][0]["name"] == "one.txt"
    assert data["entries"][1][".tag"] == "failure"
    assert data["entries"][1]["failure"]["lookup_failed"][".tag"] == "incorrect_offset"
    started = await dbx.ok(token, "files/upload_session/start_batch", {"num_sessions": 3})
    assert len(started["session_ids"]) == 3
    launch = await dbx.ok(token, "files/upload_session/finish_batch", {"entries": entries[:1]}, check=False)
    assert launch[".tag"] in ("complete", "async_job_id")


async def test_create_folder_delete_move_copy(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    made = await dbx.ok(token, "files/create_folder_v2", {"path": "/Projects/Alpha"})
    assert made["metadata"]["path_display"] == "/Projects/Alpha"
    err = await dbx.err(token, "files/create_folder_v2", {"path": "/projects/alpha"})
    assert err["error_summary"].startswith("path/conflict/folder")
    auto = await dbx.ok(token, "files/create_folder_v2", {"path": "/Projects/Alpha", "autorename": True})
    assert auto["metadata"]["name"] == "Alpha (1)"
    await dbx.put(token, "/Projects/Alpha/spec.txt", b"spec")
    # copy then move
    copied = await dbx.ok(
        token, "files/copy_v2", {"from_path": "/Projects/Alpha", "to_path": "/Projects/Beta"}
    )
    assert copied["metadata"][".tag"] == "folder"
    beta = await dbx.list_all(token, "/Projects/Beta")
    assert [e["name"] for e in beta] == ["spec.txt"]
    original = await dbx.ok(token, "files/get_metadata", {"path": "/Projects/Alpha/spec.txt"})
    assert beta[0]["id"] != original["id"]
    moved = await dbx.ok(
        token, "files/move_v2", {"from_path": "/Projects/Beta", "to_path": "/Projects/Gamma/Beta2"}
    )
    assert moved["metadata"]["path_display"] == "/Projects/Gamma/Beta2"
    gone = await dbx.err(token, "files/get_metadata", {"path": "/Projects/Beta/spec.txt"})
    assert gone["error"]["path"][".tag"] == "not_found"
    inside = await dbx.err(
        token, "files/move_v2", {"from_path": "/Projects/Gamma", "to_path": "/Projects/Gamma/Beta2/x"}
    )
    assert inside["error"][".tag"] == "cant_move_folder_into_itself"
    clash = await dbx.err(
        token,
        "files/copy_v2",
        {"from_path": "/Projects/Alpha/spec.txt", "to_path": "/Projects/Gamma/Beta2/spec.txt"},
    )
    assert clash["error"]["to"]["conflict"][".tag"] == "file"
    auto = await dbx.ok(
        token,
        "files/copy_v2",
        {
            "from_path": "/Projects/Alpha/spec.txt",
            "to_path": "/Projects/Gamma/Beta2/spec.txt",
            "autorename": True,
        },
    )
    assert auto["metadata"]["name"] == "spec (1).txt"
    missing = await dbx.err(token, "files/move_v2", {"from_path": "/nope", "to_path": "/x"})
    assert missing["error"]["from_lookup"][".tag"] == "not_found"
    deleted = await dbx.ok(token, "files/delete_v2", {"path": "/Projects/Gamma"})
    assert deleted["metadata"]["name"] == "Gamma"
    again = await dbx.err(token, "files/delete_v2", {"path": "/Projects/Gamma"})
    assert again["error"]["path_lookup"][".tag"] == "not_found"


async def test_delete_include_deleted_restore_and_revisions(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    v1 = await dbx.put(token, "/rev/doc.txt", b"one")
    v2 = await dbx.put(token, "/rev/doc.txt", b"two!!", mode="overwrite")
    revs = await dbx.ok(token, "files/list_revisions", {"path": "/rev/doc.txt"})
    assert [e["rev"] for e in revs["entries"]] == [v2["rev"], v1["rev"]] and revs["is_deleted"] is False
    await dbx.ok(token, "files/delete_v2", {"path": "/rev/doc.txt"})
    assert await dbx.list_all(token, "/rev") == []
    listed = await dbx.list_all(token, "/rev", include_deleted=True)
    assert [(e[".tag"], e["name"]) for e in listed] == [("deleted", "doc.txt")]
    meta = await dbx.ok(token, "files/get_metadata", {"path": "/rev/doc.txt", "include_deleted": True})
    assert meta[".tag"] == "deleted"
    revs = await dbx.ok(token, "files/list_revisions", {"path": "/rev/doc.txt"})
    assert revs["is_deleted"] is True and len(revs["entries"]) == 2
    restored = await dbx.ok(token, "files/restore", {"path": "/rev/doc.txt", "rev": v1["rev"]})
    assert restored["id"] == v1["id"] and restored["size"] == 3
    r, _ = await dbx.download(token, "/rev/doc.txt")
    assert r.content == b"one"
    bad = await dbx.err(token, "files/restore", {"path": "/rev/doc.txt", "rev": "deadbeef0"})
    assert bad["error"][".tag"] == "invalid_revision"
    # download an old revision explicitly
    r, _ = await dbx.download(token, "rev:" + v2["rev"])
    assert r.content == b"two!!"
    page = await dbx.ok(token, "files/list_revisions", {"path": "/rev/doc.txt", "limit": 1})
    assert page["has_more"] is True


async def test_batch_routes_and_async_jobs(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    created = await dbx.ok(token, "files/create_folder_batch", {"paths": ["/bat/a", "/bat/b", "/bat/a"]})
    assert created[".tag"] == "complete"
    assert [e[".tag"] for e in created["entries"]] == ["success", "success", "failure"]
    await dbx.put(token, "/bat/a/f.txt", b"f")
    launch = await dbx.ok(
        token,
        "files/copy_batch_v2",
        {
            "entries": [
                {"from_path": "/bat/a/f.txt", "to_path": "/bat/b/f.txt"},
                {"from_path": "/bat/zzz", "to_path": "/bat/y"},
            ]
        },
    )
    assert launch[".tag"] == "async_job_id"
    status = await dbx.ok(token, "files/copy_batch/check_v2", {"async_job_id": launch["async_job_id"]})
    assert status[".tag"] == "complete"
    assert status["entries"][0][".tag"] == "success" and status["entries"][0]["success"]["name"] == "f.txt"
    assert status["entries"][1]["failure"][".tag"] == "relocation_error"
    moved = await dbx.ok(
        token,
        "files/move_batch_v2",
        {"entries": [{"from_path": "/bat/b/f.txt", "to_path": "/bat/a/moved.txt"}]},
    )
    done = await dbx.ok(token, "files/move_batch/check_v2", {"async_job_id": moved["async_job_id"]})
    assert done["entries"][0]["success"]["path_display"] == "/bat/a/moved.txt"
    dele = await dbx.ok(
        token, "files/delete_batch", {"entries": [{"path": "/bat/a/moved.txt"}, {"path": "/bat/nothing"}]}
    )
    res = await dbx.ok(token, "files/delete_batch/check", {"async_job_id": dele["async_job_id"]})
    assert [e[".tag"] for e in res["entries"]] == ["success", "failure"]
    assert res["entries"][1]["failure"]["path_lookup"][".tag"] == "not_found"
    bad = await dbx.err(token, "files/delete_batch/check", {"async_job_id": "dbjid:nope"})
    assert bad["error"][".tag"] == "invalid_async_job_id"
    # v1 relocation batch: first failure fails the whole job
    v1 = await dbx.ok(token, "files/move_batch", {"entries": [{"from_path": "/missing", "to_path": "/x"}]})
    job = await dbx.ok(token, "files/move_batch/check", {"async_job_id": v1["async_job_id"]})
    assert job[".tag"] == "failed" and job["failed"][".tag"] == "from_lookup"


async def test_in_progress_polls_setting(dbx: Any, hub: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(hub.contexts["dropbox"].settings, "ASYNC_JOB_POLLS", "2")
    token = await dbx.token(dbx.MAYA)
    launch = await dbx.ok(token, "files/delete_batch", {"entries": [{"path": "/nothing"}]})
    seen = [
        (await dbx.ok(token, "files/delete_batch/check", {"async_job_id": launch["async_job_id"]}))[".tag"]
        for _ in range(3)
    ]
    assert seen == ["in_progress", "in_progress", "complete"]


async def test_tags_copy_reference_locks_and_permanent_delete(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    meta = await dbx.put(token, "/tags/one.txt", b"1")
    await dbx.ok(token, "files/tags/add", {"path": "/tags/one.txt", "tag_text": "important"})
    got = await dbx.ok(token, "files/tags/get", {"paths": ["/tags/one.txt"]})
    assert got["paths_to_tags"][0]["tags"] == [{".tag": "user_generated_tag", "tag_text": "important"}]
    gone = await dbx.err(token, "files/tags/remove", {"path": "/tags/one.txt", "tag_text": "other"})
    assert gone["error"][".tag"] == "tag_not_present"
    await dbx.ok(token, "files/tags/remove", {"path": "/tags/one.txt", "tag_text": "important"})
    ref = await dbx.ok(token, "files/copy_reference/get", {"path": "/tags/one.txt"})
    saved = await dbx.ok(
        token,
        "files/copy_reference/save",
        {"copy_reference": ref["copy_reference"], "path": "/tags/copy.txt"},
    )
    assert saved["metadata"]["id"] != meta["id"]
    bad = await dbx.err(token, "files/copy_reference/save", {"copy_reference": "nope", "path": "/tags/x.txt"})
    assert bad["error"][".tag"] == "invalid_copy_reference"
    # locks only work for shared content
    not_shared = await dbx.ok(token, "files/lock_file_batch", {"entries": [{"path": "/tags/one.txt"}]})
    assert not_shared["entries"][0]["failure"][".tag"] == "file_not_shared"
    await dbx.ok(token, "files/delete_v2", {"path": "/tags/copy.txt"})
    await dbx.ok(token, "files/permanently_delete", {"path": "/tags/copy.txt"})
    listed = await dbx.list_all(token, "/tags", include_deleted=True)
    assert [e["name"] for e in listed] == ["one.txt"]


async def test_file_locking_in_team_folder(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    root = await dbx.root(maya)
    entries = await dbx.list_all(
        maya, "/Client Engagements/Northwind Logistics/Correspondence", path_root=root
    )
    path = entries[0]["path_display"]
    locked = await dbx.ok(maya, "files/lock_file_batch", {"entries": [{"path": path}]}, path_root=root)
    assert locked["entries"][0][".tag"] == "success"
    assert locked["entries"][0]["lock"]["content"][".tag"] == "single_user"
    meta = await dbx.ok(maya, "files/get_metadata", {"path": path}, path_root=root)
    assert meta["file_lock_info"]["is_lockholder"] is True
    ryan = await dbx.token("ryan.murphy@halcyonadvisory.example")
    ryan_root = await dbx.root(ryan)
    conflict = await dbx.ok(ryan, "files/lock_file_batch", {"entries": [{"path": path}]}, path_root=ryan_root)
    assert conflict["entries"][0]["failure"][".tag"] == "lock_conflict"
    blocked = await dbx.upload(
        ryan, "files/upload", {"path": path, "mode": "overwrite"}, b"x", path_root=ryan_root
    )
    assert blocked.status_code == 409
    await dbx.ok(maya, "files/unlock_file_batch", {"entries": [{"path": path}]}, path_root=root)
    ok = await dbx.upload(
        ryan, "files/upload", {"path": path, "mode": "overwrite"}, b"x", path_root=ryan_root
    )
    assert ok.status_code == 200


async def test_save_url_only_fetches_emulator_hosted_content(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    data = await dbx.ok(
        token, "files/save_url", {"path": "/saved.txt", "url": "data:text/plain;base64,aGVsbG8="}
    )
    job = await dbx.ok(token, "files/save_url/check_job_status", {"async_job_id": data["async_job_id"]})
    assert job[".tag"] == "complete" and job["size"] == 5
    bad = await dbx.ok(token, "files/save_url", {"path": "/bad.txt", "url": "https://example.org/x"})
    status = await dbx.ok(token, "files/save_url/check_job_status", {"async_job_id": bad["async_job_id"]})
    assert status[".tag"] == "failed" and status["failed"][".tag"] == "download_failed"


async def test_temporary_upload_link(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    link = await dbx.ok(
        token, "files/get_temporary_upload_link", {"commit_info": {"path": "/viatemp.txt"}, "duration": 600}
    )
    assert link["link"].startswith("https://content.dropboxapi.com/apitul/1/")
    r = await dbx.client.post(
        link["link"], content=b"temp upload", headers={"Content-Type": "application/octet-stream"}
    )
    assert r.status_code == 200 and r.json()["size"] == 11
    again = await dbx.client.post(link["link"], content=b"x")
    assert again.status_code == 404
    out, _ = await dbx.download(token, "/viatemp.txt")
    assert out.content == b"temp upload"


@pytest.mark.parametrize("path", ["/", ""])
async def test_cannot_upload_to_root(dbx: Any, path: str) -> None:
    token = await dbx.token(dbx.MAYA)
    r = await dbx.upload(token, "files/upload", {"path": path}, b"x")
    assert r.status_code in (400, 409)


def test_header_json_helpers() -> None:
    from emulators.providers.dropbox.common import decode_header_json, header_json

    value = {"path": "/café/☃\x7f"}
    encoded = header_json(value)
    assert encoded.isascii() and "\x7f" not in encoded
    assert decode_header_json(encoded) == value
    assert json.loads(encoded) == value


async def test_storage_quota_blocks_uploads(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    used = (await dbx.ok(token, "users/get_space_usage"))["used"]
    r = await dbx.client.post(
        f"{dbx.API}/_emulator/actions/set_quota", json={"user": dbx.MAYA, "allocated": used + 100}
    )
    assert r.status_code == 200
    ok = await dbx.put(token, "/quota/small.bin", b"x" * 50)
    assert ok["size"] == 50
    r = await dbx.upload(token, "files/upload", {"path": "/quota/big.bin"}, b"x" * 500)
    assert r.status_code == 409 and r.json()["error_summary"].startswith("path/insufficient_space")
    dbx.check("files/upload", "error", r.json()["error"])
    # overwriting counts only the growth
    again = await dbx.put(token, "/quota/small.bin", b"y" * 100, mode="overwrite")
    assert again["size"] == 100


async def test_temporary_links_expire_after_four_hours(dbx: Any, hub: Any) -> None:
    from datetime import timedelta

    token = await dbx.token(dbx.MAYA)
    await dbx.put(token, "/expiring.txt", b"x")
    link = (await dbx.ok(token, "files/get_temporary_link", {"path": "/expiring.txt"}))["link"]
    assert (await dbx.client.get(link)).status_code == 200
    hub.clock.advance(timedelta(hours=4, minutes=1))
    try:
        assert (await dbx.client.get(link)).status_code == 404
    finally:
        hub.clock.advance(timedelta(hours=-4, minutes=-1))
