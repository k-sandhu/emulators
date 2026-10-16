"""v1 route variants, continue routes and the less common sharing / team routes."""

from __future__ import annotations

import base64
import json
from typing import Any

RYAN = "ryan.murphy@halcyonadvisory.example"


async def test_leaf_metadata_types_are_untagged_and_polymorphic_ones_tagged(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    up = await dbx.put(token, "/tags/leaf.txt", b"x")
    assert ".tag" not in up  # FileMetadata
    made = await dbx.ok(token, "files/create_folder_v2", {"path": "/tags/f"})
    assert ".tag" not in made["metadata"]  # FolderMetadata
    got = await dbx.ok(token, "files/get_metadata", {"path": "/tags/leaf.txt"})
    assert got[".tag"] == "file"  # Metadata
    link = await dbx.ok(token, "files/get_temporary_link", {"path": "/tags/leaf.txt"})
    assert ".tag" not in link["metadata"]
    moved = await dbx.ok(
        token, "files/move_v2", {"from_path": "/tags/leaf.txt", "to_path": "/tags/leaf2.txt"}
    )
    assert moved["metadata"][".tag"] == "file"
    revs = await dbx.ok(token, "files/list_revisions", {"path": "/tags/leaf2.txt"})
    assert ".tag" not in revs["entries"][0]
    _, header = await dbx.download(token, "/tags/leaf2.txt")
    assert ".tag" not in header


async def test_v1_file_routes(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    folder = await dbx.ok(token, "files/create_folder", {"path": "/v1/dir"})
    assert folder["name"] == "dir" and ".tag" not in folder
    await dbx.put(token, "/v1/dir/a.txt", b"a")
    copied = await dbx.ok(token, "files/copy", {"from_path": "/v1/dir/a.txt", "to_path": "/v1/dir/b.txt"})
    assert copied[".tag"] == "file" and copied["name"] == "b.txt"
    moved = await dbx.ok(token, "files/move", {"from_path": "/v1/dir/b.txt", "to_path": "/v1/dir/c.txt"})
    assert moved["name"] == "c.txt"
    deleted = await dbx.ok(token, "files/delete", {"path": "/v1/dir/c.txt"})
    assert deleted[".tag"] == "file"
    b = await dbx.ok(
        token, "files/copy_batch", {"entries": [{"from_path": "/v1/dir/a.txt", "to_path": "/v1/dir/d.txt"}]}
    )
    status = await dbx.ok(token, "files/copy_batch/check", {"async_job_id": b["async_job_id"]})
    assert status[".tag"] == "complete" and status["entries"][0]["metadata"]["name"] == "d.txt"
    launched = await dbx.ok(token, "files/create_folder_batch", {"paths": ["/v1/x"], "force_async": True})
    assert launched[".tag"] == "async_job_id"
    done = await dbx.ok(token, "files/create_folder_batch/check", {"async_job_id": launched["async_job_id"]})
    assert done[".tag"] == "complete" and done["entries"][0][".tag"] == "success"
    # deprecated upload_session/append and finish_batch v1
    start = await dbx.upload(token, "files/upload_session/start", {}, b"ab")
    sid = start.json()["session_id"]
    r = await dbx.upload(token, "files/upload_session/append", {"session_id": sid, "offset": 2}, b"cd")
    assert r.status_code == 200
    await dbx.upload(
        token, "files/upload_session/append_v2", {"cursor": {"session_id": sid, "offset": 4}, "close": True}
    )
    fb = await dbx.ok(
        token,
        "files/upload_session/finish_batch",
        {"entries": [{"cursor": {"session_id": sid, "offset": 4}, "commit": {"path": "/v1/fin.txt"}}]},
    )
    assert fb[".tag"] == "async_job_id"
    result = await dbx.ok(
        token, "files/upload_session/finish_batch/check", {"async_job_id": fb["async_job_id"]}
    )
    assert result[".tag"] == "complete" and result["entries"][0]["size"] == 4


async def test_thumbnail_batch_and_lock_queries(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    root = await dbx.root(token)
    photos = await dbx.list_all(token, "/Marketing & BD/Brand Assets/Photos", path_root=root)
    batch = await dbx.ok(
        token,
        "files/get_thumbnail_batch",
        {
            "entries": [
                {"path": photos[0]["path_display"], "format": {".tag": "png"}, "size": {".tag": "w32h32"}},
                {"path": "/missing.png"},
            ]
        },
        path_root=root,
        host=dbx.CONTENT,
    )
    assert (
        batch["entries"][0][".tag"] == "success"
        and base64.b64decode(batch["entries"][0]["thumbnail"])[:4] == b"\x89PNG"
    )
    assert batch["entries"][1][".tag"] == "failure"
    entries = await dbx.list_all(
        token, "/Client Engagements/Northwind Logistics/Correspondence", path_root=root
    )
    locks = await dbx.ok(
        token,
        "files/get_file_lock_batch",
        {"entries": [{"path": entries[0]["path_display"]}]},
        path_root=root,
    )
    assert locks["entries"][0]["lock"]["content"] == {".tag": "unlocked"}


async def test_sharing_continue_routes_and_job_checks(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    for name in ("Alpha1", "Alpha2", "Alpha3"):
        await dbx.put(maya, f"/multi/{name}/f.txt", b"x")
        await dbx.ok(maya, "sharing/share_folder", {"path": f"/multi/{name}"})
    page = await dbx.ok(maya, "sharing/list_folders", {"limit": 2})
    assert len(page["entries"]) == 2 and "cursor" in page
    # team folders are listed as well, so keep paging until the cursor runs out
    names = [e["name"] for e in page["entries"]]
    while "cursor" in page:
        page = await dbx.ok(maya, "sharing/list_folders/continue", {"cursor": page["cursor"]})
        names += [e["name"] for e in page["entries"]]
    assert {"Alpha1", "Alpha2", "Alpha3", "Client Engagements"} <= set(names)
    mountable = await dbx.ok(maya, "sharing/list_mountable_folders", {"limit": 1})
    assert "cursor" in mountable
    more = await dbx.ok(maya, "sharing/list_mountable_folders/continue", {"cursor": mountable["cursor"]})
    assert more["entries"]
    # force_async share_folder + check_share_job_status
    await dbx.put(maya, "/multi/Async/f.txt", b"x")
    launched = await dbx.ok(maya, "sharing/share_folder", {"path": "/multi/Async", "force_async": True})
    assert launched[".tag"] == "async_job_id"
    done = await dbx.ok(maya, "sharing/check_share_job_status", {"async_job_id": launched["async_job_id"]})
    assert done[".tag"] == "complete" and done["name"] == "Async"
    job = await dbx.ok(maya, "sharing/check_job_status", {"async_job_id": launched["async_job_id"]})
    assert job == {".tag": "complete"}
    bad = await dbx.err(maya, "sharing/check_share_job_status", {"async_job_id": "dbjid:none"})
    assert bad["error"][".tag"] == "invalid_async_job_id"
    # folder members paging
    meta = await dbx.ok(maya, "sharing/get_folder_metadata", {"shared_folder_id": done["shared_folder_id"]})
    for i in range(3):
        await dbx.ok(
            maya,
            "sharing/add_folder_member",
            {
                "shared_folder_id": meta["shared_folder_id"],
                "members": [{"member": {".tag": "email", "email": f"guest{i}@outside.example"}}],
            },
        )
    first = await dbx.ok(
        maya, "sharing/list_folder_members", {"shared_folder_id": meta["shared_folder_id"], "limit": 2}
    )
    assert first["cursor"]
    rest = await dbx.ok(maya, "sharing/list_folder_members/continue", {"cursor": first["cursor"]})
    total = len(first["users"]) + len(first["invitees"]) + len(rest["users"]) + len(rest["invitees"])
    assert total == 4


async def test_file_member_paging_batch_and_received_continue(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    meta = await dbx.put(maya, "/fm/shared.txt", b"x")
    members = [{".tag": "email", "email": f"guest{i}@outside.example"} for i in range(3)]
    await dbx.ok(maya, "sharing/add_file_member", {"file": meta["id"], "members": members})
    first = await dbx.ok(maya, "sharing/list_file_members", {"file": meta["id"], "limit": 2})
    assert first["cursor"]
    rest = await dbx.ok(maya, "sharing/list_file_members/continue", {"cursor": first["cursor"]})
    assert len(first["users"] + first["invitees"] + rest["users"] + rest["invitees"]) == 4
    batch = await dbx.ok(maya, "sharing/get_file_metadata/batch", {"files": [meta["id"], "id:missing"]})
    assert batch[0]["result"][".tag"] == "metadata" and batch[0]["result"]["name"] == "shared.txt"
    assert batch[1]["result"] == {".tag": "access_error", "access_error": {".tag": "invalid_file"}}
    # received files: share three files with the same person and page through them
    chris = await dbx.token(dbx.CHRISTOPHER)
    for i in range(3):
        f = await dbx.put(maya, f"/fm/for-chris-{i}.txt", b"x")
        await dbx.ok(
            maya,
            "sharing/add_file_member",
            {"file": f["id"], "members": [{".tag": "email", "email": dbx.CHRISTOPHER}]},
        )
    page = await dbx.ok(chris, "sharing/list_received_files", {"limit": 2})
    names = [e["name"] for e in page["entries"]]
    while "cursor" in page:
        page = await dbx.ok(chris, "sharing/list_received_files/continue", {"cursor": page["cursor"]})
        names += [e["name"] for e in page["entries"]]
    assert {f"for-chris-{i}.txt" for i in range(3)} <= set(names)
    # relinquish own file membership
    mine = await dbx.ok(chris, "sharing/list_received_files", {})
    target = next(e for e in mine["entries"] if e["name"] == "for-chris-0.txt")
    await dbx.ok(chris, "sharing/relinquish_file_membership", {"file": target["id"]})
    after = await dbx.ok(chris, "sharing/list_received_files", {})
    assert target["id"] not in {e["id"] for e in after["entries"]}


async def test_relinquish_transfer_and_inheritance(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    ryan = await dbx.token(RYAN)
    await dbx.put(maya, "/own/Shared/f.txt", b"x")
    meta = await dbx.ok(maya, "sharing/share_folder", {"path": "/own/Shared"})
    sfid = meta["shared_folder_id"]
    ryan_id = await dbx.account_id(RYAN)
    await dbx.ok(
        maya,
        "sharing/add_folder_member",
        {
            "shared_folder_id": sfid,
            "members": [
                {"member": {".tag": "dropbox_id", "dropbox_id": ryan_id}, "access_level": {".tag": "editor"}}
            ],
        },
    )
    await dbx.ok(ryan, "sharing/mount_folder", {"shared_folder_id": sfid})
    inh = await dbx.ok(
        maya,
        "sharing/set_access_inheritance",
        {"shared_folder_id": sfid, "access_inheritance": {".tag": "no_inherit"}},
    )
    assert inh["access_inheritance"] == {".tag": "no_inherit"}
    # only the owner may transfer; the new owner must be a member
    forbidden = await dbx.err(
        ryan, "sharing/transfer_folder", {"shared_folder_id": sfid, "to_dropbox_id": ryan_id}
    )
    assert forbidden["error"][".tag"] == "no_permission"
    await dbx.ok(maya, "sharing/transfer_folder", {"shared_folder_id": sfid, "to_dropbox_id": ryan_id})
    members = await dbx.ok(maya, "sharing/list_folder_members", {"shared_folder_id": sfid})
    roles = {u["user"]["email"]: u["access_type"][".tag"] for u in members["users"]}
    assert roles == {dbx.MAYA: "editor", RYAN: "owner"}
    # a non-owner can leave the folder; the owner cannot
    left = await dbx.ok(maya, "sharing/relinquish_folder_membership", {"shared_folder_id": sfid})
    assert left == {".tag": "complete"}
    owner = await dbx.err(ryan, "sharing/relinquish_folder_membership", {"shared_folder_id": sfid})
    assert owner["error"][".tag"] == "folder_owner"
    gone = await dbx.err(maya, "sharing/get_folder_metadata", {"shared_folder_id": sfid})
    assert gone["error"][".tag"] == "not_a_member"


async def test_members_continue_recover_and_team_features(dbx: Any, hub: Any) -> None:
    arjun = await dbx.team_token()
    first = await dbx.ok(arjun, "team/members/list", {"limit": 30})
    assert first["has_more"]
    rest = await dbx.ok(arjun, "team/members/list/continue", {"cursor": first["cursor"]})
    assert len(first["members"]) + len(rest["members"]) == len(hub.world.employees)
    feats = await dbx.ok(
        arjun,
        "team/features/get_values",
        {
            "features": [
                {".tag": "upload_api_rate_limit"},
                {".tag": "has_team_shared_dropbox"},
                {".tag": "has_team_file_events"},
            ]
        },
    )
    assert len(feats["values"]) == 3
    empty = await dbx.err(arjun, "team/features/get_values", {"features": []})
    assert empty["error"][".tag"] == "empty_features_list"
    sel = {".tag": "email", "email": RYAN}
    await dbx.ok(arjun, "team/members/remove", {"user": sel})
    await dbx.ok(arjun, "team/members/recover", {"user": sel})
    again = await dbx.err(arjun, "team/members/recover", {"user": sel})
    assert again["error"][".tag"] == "user_unrecoverable"
    folders = (await dbx.ok(arjun, "team/team_folder/list", {}))["team_folders"]
    updated = await dbx.ok(
        arjun,
        "team/team_folder/update_sync_settings",
        {"team_folder_id": folders[0]["team_folder_id"], "sync_setting": {".tag": "not_synced"}},
    )
    assert updated["sync_setting"] == {".tag": "not_synced"}
    created = await dbx.ok(arjun, "team/team_folder/create", {"name": "To Archive"})
    launch = await dbx.ok(arjun, "team/team_folder/archive", {"team_folder_id": created["team_folder_id"]})
    assert launch[".tag"] == "async_job_id"
    status = await dbx.ok(arjun, "team/team_folder/archive/check", {"async_job_id": launch["async_job_id"]})
    assert status[".tag"] == "complete" and status["status"] == {".tag": "archived"}


async def test_raw_utf8_in_arg_header_and_misc_headers(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    name = "/café ☃.txt"
    headers = dbx.headers(token)
    headers["Dropbox-API-Arg"] = json.dumps({"path": name}, ensure_ascii=False).encode("utf-8")  # type: ignore[assignment]
    headers["Content-Type"] = "application/octet-stream"
    r = await dbx.client.post(f"{dbx.CONTENT}/2/files/upload", headers=headers, content=b"bytes")
    assert r.status_code == 200 and r.json()["name"] == name[1:]
    r, result = await dbx.download(token, name)
    assert r.content == b"bytes" and result["path_display"] == name
    assert result["name"] == name[1:]
    assert r.headers["x-dropbox-request-id"] and len(r.headers["x-dropbox-request-id"]) == 32
