"""sharing/*: shared folders and members, file members, shared links, restricted items."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

RYAN = "ryan.murphy@halcyonadvisory.example"
OUTSIDER = "someone@outside.example"


async def make_shared_folder(dbx: Any, token: str, path: str = "/Projects/Alpha") -> dict[str, Any]:
    await dbx.put(token, f"{path}/spec.txt", b"spec")
    await dbx.ok(token, "files/create_folder_v2", {"path": f"{path}/Inner"})
    data = await dbx.ok(token, "sharing/share_folder", {"path": path})
    assert data[".tag"] == "complete"
    return data


async def test_share_folder_converts_the_folder_into_a_namespace(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    meta = await make_shared_folder(dbx, maya)
    assert meta["access_type"] == {".tag": "owner"}
    assert meta["is_team_folder"] is False and meta["path_lower"] == "/projects/alpha"
    assert re.fullmatch(r"\d+", meta["shared_folder_id"])
    listing = await dbx.list_all(maya, "/Projects/Alpha")
    assert {e["name"] for e in listing} == {"spec.txt", "Inner"}
    folder = await dbx.ok(maya, "files/get_metadata", {"path": "/Projects/Alpha"})
    assert folder["sharing_info"]["shared_folder_id"] == meta["shared_folder_id"]
    assert folder["sharing_info"]["read_only"] is False
    inner = await dbx.ok(maya, "files/get_metadata", {"path": "/Projects/Alpha/spec.txt"})
    assert inner["sharing_info"]["parent_shared_folder_id"] == meta["shared_folder_id"]
    again = await dbx.err(maya, "sharing/share_folder", {"path": "/Projects/Alpha"})
    assert again["error"]["bad_path"][".tag"] == "already_shared"
    nested = await dbx.err(maya, "sharing/share_folder", {"path": "/Projects/Alpha/Inner"})
    assert nested["error"]["bad_path"][".tag"] == "inside_shared_folder"
    file_err = await dbx.err(maya, "sharing/share_folder", {"path": "/Projects/Alpha/spec.txt"})
    assert file_err["error"]["bad_path"][".tag"] == "is_file"
    missing = await dbx.err(maya, "sharing/share_folder", {"path": "/no/such"})
    assert missing["error"]["bad_path"][".tag"] == "invalid_path"
    got = await dbx.ok(
        maya,
        "sharing/get_folder_metadata",
        {
            "shared_folder_id": meta["shared_folder_id"],
            "actions": [{".tag": "unshare"}, {".tag": "relinquish_membership"}],
        },
    )
    perms = {p["action"][".tag"]: p["allow"] for p in got["permissions"]}
    assert perms == {"unshare": True, "relinquish_membership": False}
    folders = await dbx.ok(maya, "sharing/list_folders", {})
    assert meta["shared_folder_id"] in {f["shared_folder_id"] for f in folders["entries"]}


async def test_members_mounting_and_enforcement(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    ryan = await dbx.token(RYAN)
    christopher = await dbx.token(dbx.CHRISTOPHER)
    meta = await make_shared_folder(dbx, maya)
    sfid = meta["shared_folder_id"]
    ryan_id = await dbx.account_id(RYAN)
    await dbx.ok(
        maya,
        "sharing/add_folder_member",
        {
            "shared_folder_id": sfid,
            "members": [
                {"member": {".tag": "dropbox_id", "dropbox_id": ryan_id}, "access_level": {".tag": "editor"}},
                {"member": {".tag": "email", "email": dbx.CHRISTOPHER}, "access_level": {".tag": "viewer"}},
                {
                    "member": {".tag": "email", "email": OUTSIDER},
                    "access_level": {".tag": "viewer_no_comment"},
                },
            ],
        },
    )
    members = await dbx.ok(maya, "sharing/list_folder_members", {"shared_folder_id": sfid})
    by_email = {u["user"]["email"]: u for u in members["users"]}
    assert by_email[dbx.MAYA]["access_type"] == {".tag": "owner"}
    assert by_email[RYAN]["access_type"] == {".tag": "editor"} and by_email[RYAN]["user"]["same_team"] is True
    assert by_email[dbx.CHRISTOPHER]["user"]["same_team"] is False
    assert members["invitees"][0]["invitee"] == {".tag": "email", "email": OUTSIDER}

    # not mounted yet: visible as a mountable folder, absent from the home tree
    mountable = await dbx.ok(ryan, "sharing/list_mountable_folders", {})
    assert sfid in {f["shared_folder_id"] for f in mountable["entries"]}
    assert "path_lower" not in next(f for f in mountable["entries"] if f["shared_folder_id"] == sfid)
    assert all(e["name"] != "Alpha" for e in await dbx.list_all(ryan, ""))
    mounted = await dbx.ok(ryan, "sharing/mount_folder", {"shared_folder_id": sfid})
    assert mounted["path_lower"] == "/alpha"
    again = await dbx.err(ryan, "sharing/mount_folder", {"shared_folder_id": sfid})
    assert again["error"][".tag"] == "already_mounted"
    # editors write, viewers cannot
    await dbx.put(ryan, "/Alpha/by-ryan.txt", b"hi")
    await dbx.ok(christopher, "sharing/mount_folder", {"shared_folder_id": sfid})
    read = await dbx.list_all(christopher, "/Alpha")
    assert {"by-ryan.txt", "spec.txt"} <= {e["name"] for e in read}
    assert all(e.get("sharing_info", {}).get("read_only") is not False for e in read if e[".tag"] == "file")
    denied = await dbx.upload(christopher, "files/upload", {"path": "/Alpha/by-chris.txt"}, b"x")
    assert denied.status_code == 409 and denied.json()["error_summary"].startswith("path/no_write_permission")
    denied_delete = await dbx.err(christopher, "files/delete_v2", {"path": "/Alpha/spec.txt"})
    assert denied_delete["error"]["path_write"][".tag"] == "no_write_permission"
    # the owner sees the editor's file at the original location
    assert "by-ryan.txt" in {e["name"] for e in await dbx.list_all(maya, "/Projects/Alpha")}

    # promote the viewer, then remove them
    upd = await dbx.ok(
        maya,
        "sharing/update_folder_member",
        {
            "shared_folder_id": sfid,
            "member": {".tag": "email", "email": dbx.CHRISTOPHER},
            "access_level": {".tag": "editor"},
        },
    )
    assert upd["access_level"] == {".tag": "editor"}
    await dbx.put(christopher, "/Alpha/by-chris.txt", b"now allowed")
    removed = await dbx.ok(
        maya,
        "sharing/remove_folder_member",
        {
            "shared_folder_id": sfid,
            "member": {".tag": "email", "email": dbx.CHRISTOPHER},
            "leave_a_copy": False,
        },
    )
    assert removed[".tag"] == "async_job_id"
    status = await dbx.ok(
        maya, "sharing/check_remove_member_job_status", {"async_job_id": removed["async_job_id"]}
    )
    assert status[".tag"] == "complete"
    gone = await dbx.err(christopher, "files/list_folder", {"path": "/Alpha"})
    assert gone["error"]["path"][".tag"] == "not_found"
    owner_removal = await dbx.err(
        maya,
        "sharing/remove_folder_member",
        {"shared_folder_id": sfid, "member": {".tag": "email", "email": dbx.MAYA}, "leave_a_copy": False},
    )
    assert owner_removal["error"][".tag"] == "folder_owner"
    stranger = await dbx.token("tyler.brooks@halcyonadvisory.example")
    no = await dbx.err(stranger, "sharing/list_folder_members", {"shared_folder_id": sfid})
    assert no["error"][".tag"] == "not_a_member"
    # a non-owner editor cannot invite unless the policy allows it
    cant = await dbx.err(
        ryan,
        "sharing/add_folder_member",
        {"shared_folder_id": sfid, "members": [{"member": {".tag": "email", "email": OUTSIDER}}]},
    )
    assert cant["error"][".tag"] == "no_permission"


async def test_unmount_and_unshare(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    ryan = await dbx.token(RYAN)
    meta = await make_shared_folder(dbx, maya)
    sfid = meta["shared_folder_id"]
    await dbx.ok(
        maya,
        "sharing/add_folder_member",
        {
            "shared_folder_id": sfid,
            "members": [{"member": {".tag": "email", "email": RYAN}, "access_level": {".tag": "viewer"}}],
        },
    )
    await dbx.ok(ryan, "sharing/mount_folder", {"shared_folder_id": sfid})
    await dbx.ok(ryan, "sharing/unmount_folder", {"shared_folder_id": sfid})
    gone = await dbx.err(ryan, "files/get_metadata", {"path": "/Alpha"})
    assert gone["error"]["path"][".tag"] == "not_found"
    still = await dbx.ok(ryan, "sharing/list_folders", {})
    assert sfid in {f["shared_folder_id"] for f in still["entries"]}
    await dbx.ok(ryan, "sharing/mount_folder", {"shared_folder_id": sfid})
    not_owner = await dbx.err(ryan, "sharing/unshare_folder", {"shared_folder_id": sfid})
    assert not_owner["error"][".tag"] == "no_permission"
    done = await dbx.ok(maya, "sharing/unshare_folder", {"shared_folder_id": sfid})
    assert done[".tag"] == "complete"
    # the folder is an ordinary folder again for the owner and gone for the member
    assert {e["name"] for e in await dbx.list_all(maya, "/Projects/Alpha")} == {"spec.txt", "Inner"}
    plain = await dbx.ok(maya, "files/get_metadata", {"path": "/Projects/Alpha"})
    assert "sharing_info" not in plain
    assert (await dbx.err(ryan, "files/get_metadata", {"path": "/Alpha"}))["error"]["path"][
        ".tag"
    ] == "not_found"
    assert (await dbx.err(maya, "sharing/get_folder_metadata", {"shared_folder_id": sfid}))["error"][
        ".tag"
    ] == "invalid_id"


async def test_member_policy_team_blocks_outsiders(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    await dbx.ok(maya, "files/create_folder_v2", {"path": "/Internal"})
    meta = await dbx.ok(
        maya, "sharing/share_folder", {"path": "/Internal", "member_policy": {".tag": "team"}}
    )
    err = await dbx.err(
        maya,
        "sharing/add_folder_member",
        {
            "shared_folder_id": meta["shared_folder_id"],
            "members": [{"member": {".tag": "email", "email": OUTSIDER}}],
        },
    )
    assert err["error"][".tag"] == "cant_share_outside_team"
    bad = await dbx.err(
        maya,
        "sharing/add_folder_member",
        {
            "shared_folder_id": meta["shared_folder_id"],
            "members": [{"member": {".tag": "dropbox_id", "dropbox_id": "dbid:nope"}}],
        },
    )
    assert bad["error"]["bad_member"][".tag"] == "invalid_dropbox_id"
    changed = await dbx.ok(
        maya,
        "sharing/update_folder_policy",
        {"shared_folder_id": meta["shared_folder_id"], "acl_update_policy": {".tag": "editors"}},
    )
    assert changed["policy"]["acl_update_policy"] == {".tag": "editors"}


async def test_team_folder_membership_is_group_based(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    root = await dbx.root(maya)
    folder = next(
        e for e in await dbx.list_all(maya, "", path_root=root) if e["name"] == "Client Engagements"
    )
    sfid = folder["sharing_info"]["shared_folder_id"]
    meta = await dbx.ok(maya, "sharing/get_folder_metadata", {"shared_folder_id": sfid})
    assert meta["is_team_folder"] is True and meta["owner_team"]["name"] == "Halcyon Advisory Group"
    members = await dbx.ok(maya, "sharing/list_folder_members", {"shared_folder_id": sfid})
    groups = {g["group"]["group_name"]: g for g in members["groups"]}
    assert groups["Partners"]["access_type"] == {".tag": "editor"}
    assert groups["Finance & Operations"]["access_type"] == {".tag": "viewer"}
    assert groups["Partners"]["group"]["is_member"] is True and groups["Tax"]["group"]["is_member"] is False
    err = await dbx.err(
        maya,
        "sharing/add_folder_member",
        {"shared_folder_id": sfid, "members": [{"member": {".tag": "email", "email": OUTSIDER}}]},
    )
    assert err["error"][".tag"] == "team_folder"
    listed = await dbx.ok(maya, "sharing/list_folders", {})
    assert sfid in {f["shared_folder_id"] for f in listed["entries"]}
    no = await dbx.err(maya, "sharing/mount_folder", {"shared_folder_id": sfid})
    assert no["error"][".tag"] == "must_automount"
    # an admin selecting themselves as admin may add a group to a team folder
    arjun = await dbx.team_token()
    arjun_id = await dbx.member_id(dbx.ARJUN)
    groups_list = await dbx.ok(arjun, "team/groups/list", {})
    it = next(g for g in groups_list["groups"] if g["group_name"] == "Technology")
    await dbx.ok(
        arjun,
        "sharing/add_folder_member",
        {
            "shared_folder_id": sfid,
            "members": [
                {
                    "member": {".tag": "dropbox_id", "dropbox_id": it["group_id"]},
                    "access_level": {".tag": "viewer"},
                }
            ],
        },
        select_admin=arjun_id,
    )
    after = await dbx.ok(maya, "sharing/list_folder_members", {"shared_folder_id": sfid})
    assert "Technology" in {g["group"]["group_name"] for g in after["groups"]}


# -- file members -------------------------------------------------------------------------
async def pbc_sheet(dbx: Any, token: str, root: dict[str, str]) -> dict[str, Any]:
    entries = await dbx.list_all(
        token, "/Client Engagements/Northwind Logistics/FY2025 Review/Requests", path_root=root
    )
    return next(e for e in entries if e["name"] == "PBC Request List.xlsx")


async def test_seeded_file_members_and_received_files(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    root = await dbx.root(maya)
    sheet = await pbc_sheet(dbx, maya, root)
    data = await dbx.ok(maya, "sharing/list_file_members", {"file": sheet["id"]}, path_root=root)
    users = {u["user"]["email"]: u for u in data["users"]}
    assert users[dbx.CHRISTOPHER]["access_type"] == {".tag": "editor"}
    assert (
        users[dbx.CHRISTOPHER]["is_inherited"] is False
        and users[dbx.CHRISTOPHER]["user"]["same_team"] is False
    )
    assert any(g["is_inherited"] for g in data["groups"])  # team folder groups are inherited
    only_direct = await dbx.ok(
        maya, "sharing/list_file_members", {"file": sheet["id"], "include_inherited": False}, path_root=root
    )
    assert only_direct["groups"] == []
    flagged = await dbx.ok(
        maya,
        "files/get_metadata",
        {"path": sheet["id"], "include_has_explicit_shared_members": True},
        path_root=root,
    )
    assert flagged["has_explicit_shared_members"] is True
    batch = await dbx.ok(
        maya,
        "sharing/list_file_members/batch",
        {"files": [sheet["id"], "id:nonexistent"], "limit": 1},
        path_root=root,
    )
    assert batch[0]["result"][".tag"] == "result" and batch[0]["result"]["member_count"] >= 2
    assert batch[1]["result"][".tag"] == "access_error"
    # the external client sees the file as received, can download it by id but cannot browse
    chris = await dbx.token(dbx.CHRISTOPHER)
    received = await dbx.ok(chris, "sharing/list_received_files", {"actions": [{".tag": "edit_contents"}]})
    assert sheet["id"] in {f["id"] for f in received["entries"]}
    entry = next(f for f in received["entries"] if f["id"] == sheet["id"])
    assert entry["access_type"] == {".tag": "editor"} and entry["permissions"][0]["allow"] is True
    r, meta = await dbx.download(chris, sheet["id"])
    assert r.status_code == 200 and meta["id"] == sheet["id"]
    assert "path_lower" not in meta
    nope = await dbx.err(chris, "files/list_folder", {"path": "/Client Engagements"})
    assert nope["error"]["path"][".tag"] == "not_found"
    shared = await dbx.ok(chris, "sharing/get_file_metadata", {"file": sheet["id"]})
    assert shared["name"] == "PBC Request List.xlsx" and shared["policy"]["acl_update_policy"]
    other = await dbx.token("fatima.noor@halcyonadvisory.example")
    assert (await dbx.err(chris, "files/download", {"path": "/x"}, status=400)) is not None
    assert other


async def test_add_update_remove_file_members(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    chris = await dbx.token(dbx.CHRISTOPHER)
    meta = await dbx.put(maya, "/share-me.txt", b"secret")
    out = await dbx.ok(
        maya,
        "sharing/add_file_member",
        {
            "file": meta["id"],
            "members": [
                {".tag": "email", "email": dbx.CHRISTOPHER},
                {".tag": "email", "email": OUTSIDER},
                {".tag": "dropbox_id", "dropbox_id": "dbid:nobody"},
            ],
            "access_level": {".tag": "viewer"},
        },
    )
    assert out[0]["result"] == {".tag": "success", "success": {".tag": "viewer"}}
    assert out[2]["result"][".tag"] == "member_error"
    malformed = await dbx.rpc(
        maya,
        "sharing/add_file_member",
        {"file": meta["id"], "members": [{".tag": "email", "email": "not-an-email"}]},
    )
    assert malformed.status_code == 400 and "did not match pattern" in malformed.text
    members = await dbx.ok(maya, "sharing/list_file_members", {"file": meta["id"]})
    assert {u["user"]["email"] for u in members["users"]} == {dbx.MAYA, dbx.CHRISTOPHER}
    assert members["invitees"][0]["invitee"]["email"] == OUTSIDER
    r, _ = await dbx.download(chris, meta["id"])
    assert r.content == b"secret"
    denied = await dbx.upload(chris, "files/upload", {"path": meta["id"], "mode": "overwrite"}, b"x")
    assert denied.status_code == 409
    await dbx.ok(
        maya,
        "sharing/update_file_member",
        {
            "file": meta["id"],
            "member": {".tag": "email", "email": dbx.CHRISTOPHER},
            "access_level": {".tag": "editor"},
        },
    )
    edit = await dbx.upload(chris, "files/upload", {"path": meta["id"], "mode": "overwrite"}, b"edited")
    assert edit.status_code == 200, edit.text
    removed = await dbx.ok(
        maya,
        "sharing/remove_file_member_2",
        {"file": meta["id"], "member": {".tag": "email", "email": dbx.CHRISTOPHER}},
    )
    assert removed[".tag"] == "success"
    again = await dbx.ok(
        maya,
        "sharing/remove_file_member_2",
        {"file": meta["id"], "member": {".tag": "email", "email": dbx.CHRISTOPHER}},
    )
    assert again[".tag"] == "member_error"
    gone, _ = await dbx.download(chris, meta["id"])
    assert gone.status_code == 409
    await dbx.ok(maya, "sharing/unshare_file", {"file": meta["id"]})
    after = await dbx.ok(maya, "sharing/list_file_members", {"file": meta["id"]})
    assert after["invitees"] == []
    is_folder = await dbx.err(maya, "sharing/list_file_members", {"file": "/"})
    assert is_folder["error"]["access_error"][".tag"] in ("is_folder", "invalid_file")


async def test_restricted_items_hide_from_inherited_access(dbx: Any, hub: Any) -> None:
    world = hub.world
    restricted = world.document("d0252")  # Payroll Calendar 2026.xlsx in the finance space
    owner = world.person(restricted.owner_id)
    shared_with = world.person(restricted.shares[0].principal_id)
    finance = world.group("g-finance")
    others = [world.person(pid) for pid in finance.member_ids if pid not in (owner.id, shared_with.id)]
    path = "/Finance & Operations/Payroll"

    async def names(email: str) -> set[str]:
        token = await dbx.token(email)
        return {e["name"] for e in await dbx.list_all(token, path, path_root=await dbx.root(token))}

    assert "Payroll Calendar 2026.xlsx" in await names(owner.email)
    assert "Payroll Calendar 2026.xlsx" in await names(shared_with.email)
    assert others, "need a finance member with inherited access only"
    assert "Payroll Calendar 2026.xlsx" not in await names(others[0].email)
    token = await dbx.token(others[0].email)
    err = await dbx.err(
        token,
        "files/get_metadata",
        {"path": f"{path}/Payroll Calendar 2026.xlsx"},
        path_root=await dbx.root(token),
    )
    assert err["error"]["path"][".tag"] == "not_found"
    admin = await dbx.team_token()
    admin_id = await dbx.member_id(dbx.ARJUN)
    ns = await dbx.root_ns(admin, select_user=admin_id)
    seen = await dbx.list_all(
        admin, "/Finance & Operations/Payroll", select_admin=admin_id, path_root={".tag": "root", "root": ns}
    )
    assert "Payroll Calendar 2026.xlsx" in {e["name"] for e in seen}  # admins reach cut-inheritance items
    # inheritance is cut: only explicit members (and the owner) are listed, no inherited groups
    sheet = next(e for e in seen if e["name"] == "Payroll Calendar 2026.xlsx")
    members = await dbx.ok(
        admin,
        "sharing/list_file_members",
        {"file": sheet["id"]},
        select_admin=admin_id,
        path_root={".tag": "root", "root": ns},
    )
    assert members["groups"] == []
    assert {u["user"]["email"] for u in members["users"]} == {shared_with.email}
    assert all(not u["is_inherited"] for u in members["users"])


# -- shared links --------------------------------------------------------------------------
async def test_shared_link_lifecycle(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    meta = await dbx.put(maya, "/links/report.txt", b"link me")
    link = await dbx.ok(maya, "sharing/create_shared_link_with_settings", {"path": "/links/report.txt"})
    assert link[".tag"] == "file" and link["name"] == "report.txt"
    assert re.fullmatch(
        r"https://www\.dropbox\.com/scl/fi/[a-z0-9]{25}/report\.txt\?rlkey=[a-z0-9]{25}&dl=0", link["url"]
    )
    assert link["link_permissions"]["resolved_visibility"] == {".tag": "public"}
    assert link["id"] == meta["id"] and link["size"] == 7
    dup = await dbx.err(maya, "sharing/create_shared_link_with_settings", {"path": "/links/report.txt"})
    assert dup["error"][".tag"] == "shared_link_already_exists"
    assert dup["error"]["shared_link_already_exists"]["metadata"]["url"] == link["url"]
    listed = await dbx.ok(maya, "sharing/list_shared_links", {"path": "/links/report.txt"})
    assert [lk["url"] for lk in listed["links"]] == [link["url"]]
    everything = await dbx.ok(maya, "sharing/list_shared_links", {})
    assert link["url"] in {lk["url"] for lk in everything["links"]}
    info = await dbx.ok(maya, "sharing/get_shared_link_metadata", {"url": link["url"]})
    assert info["id"] == link["id"]
    # anyone can download through the public URL (no auth): dl=1 redirects to the CDN host
    page = await dbx.client.get(link["url"])
    assert page.status_code == 200 and "report.txt" in page.text
    redirect = await dbx.client.get(link["url"].replace("dl=0", "dl=1"))
    assert (
        redirect.status_code == 302 and ".dl.dropboxusercontent.com/cd/0/get/" in redirect.headers["location"]
    )
    content = await dbx.client.get(redirect.headers["location"])
    assert content.content == b"link me"
    headers = dbx.headers(await dbx.token(dbx.CHRISTOPHER))
    headers["Dropbox-API-Arg"] = json.dumps({"url": link["url"]})
    file = await dbx.client.post(f"{dbx.CONTENT}/2/sharing/get_shared_link_file", headers=headers)
    assert file.status_code == 200 and file.content == b"link me"
    dbx.check("sharing/get_shared_link_file", "result", json.loads(file.headers["dropbox-api-result"]))
    # modify then revoke
    modified = await dbx.ok(
        maya,
        "sharing/modify_shared_link_settings",
        {
            "url": link["url"],
            "settings": {"requested_visibility": {".tag": "team_only"}, "allow_download": False},
        },
    )
    assert modified["link_permissions"]["resolved_visibility"] == {".tag": "team_only"}
    assert modified["link_permissions"]["allow_download"] is False
    blocked = await dbx.client.get(link["url"])
    assert blocked.status_code == 403
    await dbx.ok(maya, "sharing/revoke_shared_link", {"url": link["url"]})
    gone = await dbx.err(maya, "sharing/get_shared_link_metadata", {"url": link["url"]})
    assert gone["error"][".tag"] == "shared_link_not_found"
    bad = await dbx.err(maya, "sharing/revoke_shared_link", {"url": "https://example.org/nope"})
    assert bad["error"][".tag"] == "shared_link_malformed"
    assert (await dbx.client.get(link["url"])).status_code == 404


async def test_shared_link_password_expiry_and_ownership(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    ryan = await dbx.token(RYAN)
    await dbx.put(maya, "/links/private.txt", b"pw")
    link = await dbx.ok(
        maya,
        "sharing/create_shared_link_with_settings",
        {
            "path": "/links/private.txt",
            "settings": {
                "requested_visibility": {".tag": "password"},
                "link_password": "s3cret",
                "expires": "2030-01-01T00:00:00Z",
            },
        },
    )
    assert link["expires"] == "2030-01-01T00:00:00Z"
    assert link["link_permissions"]["require_password"] is True
    denied = await dbx.err(ryan, "sharing/get_shared_link_metadata", {"url": link["url"]})
    assert denied["error"][".tag"] == "shared_link_access_denied"
    ok = await dbx.ok(
        ryan, "sharing/get_shared_link_metadata", {"url": link["url"], "link_password": "s3cret"}
    )
    assert ok["name"] == "private.txt"
    web = await dbx.client.get(link["url"])
    assert web.status_code == 401 and "password" in web.text.lower()
    unlocked = await dbx.client.get(link["url"] + "&password=s3cret")
    assert unlocked.status_code == 200
    not_mine = await dbx.err(ryan, "sharing/revoke_shared_link", {"url": link["url"]})
    assert not_mine["error"][".tag"] == "shared_link_access_denied"
    past = await dbx.err(
        maya,
        "sharing/modify_shared_link_settings",
        {"url": link["url"], "settings": {"expires": "2001-01-01T00:00:00Z"}},
    )
    assert past["error"]["settings_error"][".tag"] == "invalid_settings"
    removed = await dbx.ok(
        maya,
        "sharing/modify_shared_link_settings",
        {"url": link["url"], "settings": {}, "remove_expiration": True},
    )
    assert "expires" not in removed
    no_pw = await dbx.err(
        maya,
        "sharing/create_shared_link_with_settings",
        {"path": "/links", "settings": {"requested_visibility": {".tag": "password"}}},
    )
    assert no_pw["error"]["settings_error"][".tag"] == "invalid_settings"


async def test_shared_folder_link_lists_children(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    await dbx.put(maya, "/linked/one.txt", b"1")
    await dbx.put(maya, "/linked/two.txt", b"22")
    link = await dbx.ok(maya, "sharing/create_shared_link_with_settings", {"path": "/linked"})
    assert link[".tag"] == "folder" and "/scl/fo/" in link["url"]
    listing = await dbx.ok(maya, "files/list_folder", {"path": "", "shared_link": {"url": link["url"]}})
    assert {e["name"] for e in listing["entries"]} == {"one.txt", "two.txt"}
    assert all("path_lower" not in e for e in listing["entries"])
    sub = await dbx.ok(maya, "sharing/get_shared_link_metadata", {"url": link["url"], "path": "/two.txt"})
    assert sub[".tag"] == "file" and sub["name"] == "two.txt"
    headers = dbx.headers(maya)
    headers["Dropbox-API-Arg"] = json.dumps({"url": link["url"], "path": "/two.txt"})
    f = await dbx.client.post(f"{dbx.CONTENT}/2/sharing/get_shared_link_file", headers=headers)
    assert f.content == b"22"
    is_dir = await dbx.client.post(
        f"{dbx.CONTENT}/2/sharing/get_shared_link_file",
        headers={**dbx.headers(maya), "Dropbox-API-Arg": json.dumps({"url": link["url"]})},
    )
    assert is_dir.json()["error"][".tag"] == "shared_link_is_directory"
    zipped = await dbx.client.get(link["url"].replace("dl=0", "dl=1"))
    assert zipped.headers["content-type"] == "application/zip"
    ancestors = await dbx.ok(maya, "sharing/list_shared_links", {"path": "/linked/one.txt"})
    assert [lk[".tag"] for lk in ancestors["links"]] == ["folder"]
    direct = await dbx.ok(maya, "sharing/list_shared_links", {"path": "/linked/one.txt", "direct_only": True})
    assert direct["links"] == []


async def test_seeded_public_link_resolves_for_anyone(dbx: Any, hub: Any) -> None:
    doc = hub.world.document("d0241")  # proposal with an 'anyone' link share
    owner = hub.world.person(doc.owner_id)
    token = await dbx.token(owner.email)
    root = await dbx.root(token)
    links = await dbx.ok(token, "sharing/list_shared_links", {}, path_root=root)
    link = next(lk for lk in links["links"] if lk["name"] == doc.name)
    assert link["link_permissions"]["resolved_visibility"] == {".tag": "public"}
    outsider = await dbx.token(dbx.CHRISTOPHER)
    meta = await dbx.ok(outsider, "sharing/get_shared_link_metadata", {"url": link["url"]})
    assert meta["name"] == doc.name
    assert (await dbx.client.get(link["url"])).status_code == 200


@pytest.mark.parametrize("path", ["/links/missing.txt"])
async def test_shared_link_for_missing_path(dbx: Any, path: str) -> None:
    maya = await dbx.token(dbx.MAYA)
    err = await dbx.err(maya, "sharing/create_shared_link_with_settings", {"path": path})
    assert err["error"]["path"][".tag"] == "not_found"


async def test_link_errors_use_the_calling_routes_own_error_unions(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    bogus = "https://www.dropbox.com/scl/fo/nothere/AAA?rlkey=x&dl=0"
    err = await dbx.err(maya, "files/list_folder", {"path": "", "shared_link": {"url": bogus}})
    assert err["error"]["path"][".tag"] == "not_found"
    headers = {
        **dbx.headers(maya),
        "Dropbox-API-Arg": json.dumps({"resource": {".tag": "link", "url": bogus}}),
    }
    r = await dbx.client.post(f"{dbx.CONTENT}/2/files/get_thumbnail_v2", headers=headers)
    assert r.status_code == 409 and r.json()["error"] == {".tag": "not_found"}
    dbx.check("files/get_thumbnail_v2", "error", r.json()["error"])
