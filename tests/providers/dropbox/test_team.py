"""Dropbox Business: team info, members, groups, team folders, namespaces and acting as members."""

from __future__ import annotations

from typing import Any


async def test_team_info_and_authenticated_admin(dbx: Any, hub: Any) -> None:
    arjun = await dbx.team_token()
    info = await dbx.ok(arjun, "team/get_info")
    assert info["name"] == "Halcyon Advisory Group" and info["team_id"] == hub.world.org.dropbox_team_id
    assert info["num_provisioned_users"] == len(hub.world.employees)
    assert info["num_used_licenses"] == len(hub.world.active_employees)
    admin = await dbx.ok(arjun, "team/token/get_authenticated_admin")
    assert admin["admin_profile"]["email"] == dbx.ARJUN
    # a user token cannot call team routes; a team token cannot call user routes without selecting a member
    user = await dbx.token(dbx.MAYA)
    text = await dbx.err(user, "team/get_info", status=400)
    assert "single user" in text
    text = await dbx.err(arjun, "users/get_current_account", status=400)
    assert "Select-User" in text


async def test_members_list_paging_roles_and_statuses(dbx: Any, hub: Any) -> None:
    arjun = await dbx.team_token()
    first = await dbx.ok(arjun, "team/members/list_v2", {"limit": 20})
    assert len(first["members"]) == 20 and first["has_more"] is True
    members = list(first["members"])
    page = first
    while page["has_more"]:
        page = await dbx.ok(arjun, "team/members/list/continue_v2", {"cursor": page["cursor"]})
        members.extend(page["members"])
    assert len(members) == len(hub.world.employees)
    by_email = {m["profile"]["email"]: m for m in members}
    assert [r["name"] for r in by_email[dbx.ARJUN]["roles"]] == ["Team admin"]
    assert [r["name"] for r in by_email[dbx.SARAH]["roles"]] == ["User management admin"]
    assert by_email[dbx.MAYA]["profile"]["status"] == {".tag": "active"}
    suspended = [m for m in members if m["profile"]["status"][".tag"] == "suspended"]
    assert {m["profile"]["email"] for m in suspended} == {
        p.email for p in hub.world.employees if not p.active
    }
    assert all(m["profile"]["suspended_on"] for m in suspended)
    maya = by_email[dbx.MAYA]["profile"]
    assert maya["team_member_id"].startswith("dbmid:") and maya["account_id"].startswith("dbid:")
    assert maya["member_folder_id"] and maya["root_folder_id"]
    assert set(maya["groups"]) <= {
        g["group_id"] for g in (await dbx.ok(arjun, "team/groups/list", {}))["groups"]
    }
    v1 = await dbx.ok(arjun, "team/members/list", {"limit": 5})
    assert v1["members"][0]["role"][".tag"] in ("member_only", "team_admin", "user_management_admin")
    got = await dbx.ok(
        arjun,
        "team/members/get_info_v2",
        {
            "members": [
                {".tag": "email", "email": dbx.MAYA},
                {".tag": "team_member_id", "team_member_id": "dbmid:missing"},
            ]
        },
    )
    assert got["members_info"][0][".tag"] == "member_info" and got["members_info"][1] == {
        ".tag": "id_not_found",
        "id_not_found": "dbmid:missing",
    }
    v1_info = await dbx.ok(
        arjun, "team/members/get_info", {"members": [{".tag": "email", "email": dbx.ARJUN}]}
    )
    assert v1_info[0]["role"] == {".tag": "team_admin"}
    roles = await dbx.ok(arjun, "team/members/get_available_team_member_roles")
    assert {r["name"] for r in roles["roles"]} >= {"Team admin", "User management admin", "Support admin"}


async def test_groups(dbx: Any, hub: Any) -> None:
    arjun = await dbx.team_token()
    groups = await dbx.ok(arjun, "team/groups/list", {"limit": 5})
    assert groups["has_more"] is True
    more = await dbx.ok(arjun, "team/groups/list/continue", {"cursor": groups["cursor"]})
    assert len(groups["groups"]) + len(more["groups"]) <= len(hub.world.groups)
    allg = (await dbx.ok(arjun, "team/groups/list", {}))["groups"]
    assert len(allg) == len(hub.world.groups)
    partners = next(g for g in allg if g["group_name"] == "Partners")
    assert partners["group_external_id"] == "g-partners" and partners["member_count"] == 4
    info = await dbx.ok(
        arjun, "team/groups/get_info", {".tag": "group_ids", "group_ids": [partners["group_id"], "g:missing"]}
    )
    assert info[0][".tag"] == "group_info" and len(info[0]["members"]) == 4
    assert info[1] == {".tag": "id_not_found", "id_not_found": "g:missing"}
    ext = await dbx.ok(
        arjun, "team/groups/get_info", {".tag": "group_external_ids", "group_external_ids": ["g-tax"]}
    )
    assert ext[0]["group_name"] == "Tax"
    members = await dbx.ok(
        arjun,
        "team/groups/members/list",
        {"group": {".tag": "group_id", "group_id": partners["group_id"]}, "limit": 2},
    )
    assert len(members["members"]) == 2 and members["has_more"] is True
    rest = await dbx.ok(arjun, "team/groups/members/list/continue", {"cursor": members["cursor"]})
    assert len(rest["members"]) == 2
    missing = await dbx.err(
        arjun, "team/groups/members/list", {"group": {".tag": "group_id", "group_id": "g:none"}}
    )
    assert missing["error"][".tag"] == "group_not_found"
    # create / add / remove / update / delete
    created = await dbx.ok(
        arjun,
        "team/groups/create",
        {
            "group_name": "Task Force",
            "group_external_id": "tf-1",
            "group_management_type": {".tag": "company_managed"},
        },
    )
    gid = created["group_id"]
    dup = await dbx.err(arjun, "team/groups/create", {"group_name": "task force"})
    assert dup["error"][".tag"] == "group_name_already_used"
    maya_id = await dbx.member_id(dbx.MAYA)
    added = await dbx.ok(
        arjun,
        "team/groups/members/add",
        {
            "group": {".tag": "group_id", "group_id": gid},
            "members": [
                {
                    "user": {".tag": "team_member_id", "team_member_id": maya_id},
                    "access_type": {".tag": "member"},
                }
            ],
        },
    )
    assert added["group_info"]["member_count"] == 1
    twice = await dbx.err(
        arjun,
        "team/groups/members/add",
        {
            "group": {".tag": "group_id", "group_id": gid},
            "members": [
                {
                    "user": {".tag": "team_member_id", "team_member_id": maya_id},
                    "access_type": {".tag": "member"},
                }
            ],
        },
    )
    assert twice["error"][".tag"] == "duplicate_user"
    profile = await dbx.ok(
        arjun,
        "team/members/get_info_v2",
        {"members": [{".tag": "team_member_id", "team_member_id": maya_id}]},
    )
    assert gid in profile["members_info"][0]["profile"]["groups"]
    renamed = await dbx.ok(
        arjun,
        "team/groups/update",
        {"group": {".tag": "group_id", "group_id": gid}, "new_group_name": "Task Force 2"},
    )
    assert renamed["group_name"] == "Task Force 2"
    removed = await dbx.ok(
        arjun,
        "team/groups/members/remove",
        {
            "group": {".tag": "group_id", "group_id": gid},
            "users": [{".tag": "team_member_id", "team_member_id": maya_id}],
        },
    )
    assert removed["group_info"]["member_count"] == 0
    status = await dbx.ok(arjun, "team/groups/job_status/get", {"async_job_id": removed["async_job_id"]})
    assert status[".tag"] == "complete"
    deleted = await dbx.ok(arjun, "team/groups/delete", {".tag": "group_id", "group_id": gid})
    assert deleted[".tag"] == "complete"
    nobody = await dbx.token(dbx.MAYA, team=True)
    forbidden = await dbx.err(nobody, "team/groups/create", {"group_name": "Rogue"}, status=401)
    assert forbidden["error"][".tag"] == "route_access_denied"


async def test_group_membership_controls_team_folder_access(dbx: Any) -> None:
    arjun = await dbx.team_token()
    ryan_member = await dbx.member_id("ryan.murphy@halcyonadvisory.example")
    ryan = await dbx.token("ryan.murphy@halcyonadvisory.example")
    root = await dbx.root(ryan)
    before = {e["name"] for e in await dbx.list_all(ryan, "", path_root=root)}
    assert "Finance & Operations" not in before
    groups = (await dbx.ok(arjun, "team/groups/list", {}))["groups"]
    finance = next(g for g in groups if g["group_name"] == "Finance & Operations")
    await dbx.ok(
        arjun,
        "team/groups/members/add",
        {
            "group": {".tag": "group_id", "group_id": finance["group_id"]},
            "members": [
                {
                    "user": {".tag": "team_member_id", "team_member_id": ryan_member},
                    "access_type": {".tag": "member"},
                }
            ],
        },
    )
    after = {e["name"] for e in await dbx.list_all(ryan, "", path_root=root)}
    assert "Finance & Operations" in after


async def test_team_folders(dbx: Any, hub: Any) -> None:
    arjun = await dbx.team_token()
    listing = await dbx.ok(arjun, "team/team_folder/list", {"limit": 3})
    assert len(listing["team_folders"]) == 3 and listing["has_more"] is True
    folders = list(listing["team_folders"])
    page = listing
    while page["has_more"]:
        page = await dbx.ok(arjun, "team/team_folder/list/continue", {"cursor": page["cursor"]})
        folders += page["team_folders"]
    names = {f["name"] for f in folders}
    assert names == {s.name for s in hub.world.spaces if s.kind == "team"}
    ids = [listing["team_folders"][0]["team_folder_id"], "999"]
    info = await dbx.ok(arjun, "team/team_folder/get_info", {"team_folder_ids": ids})
    assert info[0][".tag"] == "team_folder_metadata" and info[1] == {
        ".tag": "id_not_found",
        "id_not_found": "999",
    }
    created = await dbx.ok(arjun, "team/team_folder/create", {"name": "Audit Archive"})
    assert created["status"] == {".tag": "active"}
    dup = await dbx.err(arjun, "team/team_folder/create", {"name": "audit archive"})
    assert dup["error"][".tag"] == "folder_name_already_used"
    tid = created["team_folder_id"]
    renamed = await dbx.ok(arjun, "team/team_folder/rename", {"team_folder_id": tid, "name": "Audit Vault"})
    assert renamed["name"] == "Audit Vault"
    arjun_id = await dbx.member_id(dbx.ARJUN)
    root = await dbx.root_ns(arjun, select_user=arjun_id)
    # admin adds a group, which makes the folder reachable for its members
    groups = (await dbx.ok(arjun, "team/groups/list", {}))["groups"]
    tax = next(g for g in groups if g["group_name"] == "Tax")
    await dbx.ok(
        arjun,
        "sharing/add_folder_member",
        {
            "shared_folder_id": tid,
            "members": [
                {
                    "member": {".tag": "dropbox_id", "dropbox_id": tax["group_id"]},
                    "access_level": {".tag": "editor"},
                }
            ],
        },
        select_admin=arjun_id,
    )
    tax_member = hub.world.group_members("g-tax")[0]
    token = await dbx.token(tax_member.email)
    tax_root = await dbx.root(token)
    await dbx.put(token, "/Audit Vault/hello.txt", b"hi", path_root=tax_root)
    archived = await dbx.ok(
        arjun, "team/team_folder/archive", {"team_folder_id": tid, "force_async_off": True}
    )
    assert archived[".tag"] == "complete" and archived["status"] == {".tag": "archived"}
    again = await dbx.err(arjun, "team/team_folder/archive", {"team_folder_id": tid})
    assert again["error"][".tag"] == "status_error"
    hidden = await dbx.err(
        token, "files/get_metadata", {"path": "/Audit Vault/hello.txt"}, path_root=tax_root
    )
    assert hidden["error"]["path"][".tag"] == "not_found"
    activated = await dbx.ok(arjun, "team/team_folder/activate", {"team_folder_id": tid})
    assert activated["status"] == {".tag": "active"}
    still = await dbx.ok(token, "files/get_metadata", {"path": "/Audit Vault/hello.txt"}, path_root=tax_root)
    assert still["size"] == 2
    await dbx.ok(arjun, "team/team_folder/archive", {"team_folder_id": tid, "force_async_off": True})
    await dbx.ok(arjun, "team/team_folder/permanently_delete", {"team_folder_id": tid})
    gone = await dbx.ok(arjun, "team/team_folder/get_info", {"team_folder_ids": [tid]})
    assert gone[0][".tag"] == "id_not_found"
    assert root


async def test_namespaces_and_admin_access_to_all_content(dbx: Any, hub: Any) -> None:
    arjun = await dbx.team_token()
    arjun_id = await dbx.member_id(dbx.ARJUN)
    ns_page = await dbx.ok(arjun, "team/namespaces/list", {"limit": 10})
    namespaces = list(ns_page["namespaces"])
    while ns_page["has_more"]:
        ns_page = await dbx.ok(arjun, "team/namespaces/list/continue", {"cursor": ns_page["cursor"]})
        namespaces.extend(ns_page["namespaces"])
    kinds = {n["namespace_type"][".tag"] for n in namespaces}
    assert kinds == {"team_folder", "team_member_folder"}
    assert sum(1 for n in namespaces if n["namespace_type"][".tag"] == "team_member_folder") == len(
        hub.world.employees
    )
    clients = next(n for n in namespaces if n["name"] == "Client Engagements")
    # As admin, crawl the whole Client Engagements namespace without being a member of its groups
    entries = await dbx.list_all(
        arjun,
        "",
        recursive=True,
        select_admin=arjun_id,
        path_root={".tag": "namespace_id", "namespace_id": clients["namespace_id"]},
    )
    assert len([e for e in entries if e[".tag"] == "file"]) == len(hub.world.documents_in("clients"))
    # member folders are reachable the same way
    maya_ns = next(n for n in namespaces if n["name"] == "Maya Chen")
    maya_files = await dbx.list_all(
        arjun,
        "",
        recursive=True,
        select_admin=arjun_id,
        path_root={".tag": "namespace_id", "namespace_id": maya_ns["namespace_id"]},
    )
    assert {e["name"] for e in maya_files if e[".tag"] == "folder"} == {"Notes", "Expenses"}
    # plain Select-User is *not* an admin: another member's namespace is invisible
    denied = await dbx.rpc(
        arjun,
        "files/list_folder",
        {"path": ""},
        select_user=arjun_id,
        path_root={".tag": "namespace_id", "namespace_id": maya_ns["namespace_id"]},
    )
    assert denied.status_code == 422 and denied.json()["error"] == {".tag": "no_permission"}


async def test_select_user_acts_as_member_and_errors(dbx: Any) -> None:
    arjun = await dbx.team_token()
    maya_id = await dbx.member_id(dbx.MAYA)
    acct = await dbx.ok(arjun, "users/get_current_account", select_user=maya_id)
    assert acct["email"] == dbx.MAYA and acct["team_member_id"] == maya_id
    home = await dbx.list_all(arjun, "", select_user=maya_id)
    assert {e["name"] for e in home} == {"Notes", "Expenses"}
    bad = await dbx.err(arjun, "users/get_current_account", select_user="dbmid:nobody", status=401)
    assert bad["error"][".tag"] == "invalid_select_user"
    not_admin = await dbx.err(arjun, "users/get_current_account", select_admin=maya_id, status=401)
    assert not_admin["error"][".tag"] == "invalid_select_admin"
    ok_admin = await dbx.ok(arjun, "users/get_current_account", select_admin=await dbx.member_id(dbx.ARJUN))
    assert ok_admin["email"] == dbx.ARJUN
    # suspended members cannot be selected
    suspended = next(
        m["profile"]["team_member_id"]
        for m in (await dbx.ok(arjun, "team/members/list_v2", {}))["members"]
        if m["profile"]["status"][".tag"] == "suspended"
    )
    gone = await dbx.err(arjun, "users/get_current_account", select_user=suspended, status=401)
    assert gone["error"][".tag"] == "user_suspended"


async def test_path_root_errors(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    err = await dbx.err(
        maya, "files/list_folder", {"path": ""}, status=422, path_root={".tag": "root", "root": "1234"}
    )
    assert err["error"][".tag"] == "invalid_root"
    assert err["error"]["invalid_root"][".tag"] == "team"
    assert err["error"]["invalid_root"]["root_namespace_id"] == await dbx.root_ns(maya)
    dbx.check("files/list_folder", "result", {"entries": [], "cursor": "x", "has_more": False})
    bad = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder",
        headers={
            **dbx.headers(maya),
            "Dropbox-API-Path-Root": "not json",
            "Content-Type": "application/json",
        },
        content='{"path": ""}',
    )
    assert bad.status_code == 400
    home = await dbx.ok(maya, "files/list_folder", {"path": ""}, path_root={".tag": "home"})
    assert home["entries"]


async def test_member_lifecycle_add_suspend_remove(dbx: Any) -> None:
    arjun = await dbx.team_token()
    added = await dbx.ok(
        arjun,
        "team/members/add_v2",
        {
            "new_members": [
                {
                    "member_email": "new.hire@halcyonadvisory.example",
                    "member_given_name": "New",
                    "member_surname": "Hire",
                },
                {"member_email": dbx.MAYA},
            ]
        },
    )
    assert added[".tag"] == "complete"
    assert added["complete"][0][".tag"] == "success" and added["complete"][0]["profile"]["status"] == {
        ".tag": "invited"
    }
    assert added["complete"][1][".tag"] == "user_already_on_team"
    sel = {".tag": "email", "email": "new.hire@halcyonadvisory.example"}
    await dbx.ok(arjun, "team/members/set_admin_permissions_v2", {"user": sel, "new_roles": []}, check=True)
    profile = await dbx.ok(
        arjun,
        "team/members/set_profile_v2",
        {"user": sel, "new_given_name": "Newer", "new_external_id": "EXT-1"},
    )
    assert profile["member_info"]["profile"]["name"]["given_name"] == "Newer"
    dupe = await dbx.err(
        arjun, "team/members/set_profile_v2", {"user": sel, "new_external_id": "EXT-1", "new_email": dbx.MAYA}
    )
    assert dupe["error"][".tag"] == "email_reserved_for_other_user"
    await dbx.ok(arjun, "team/members/send_welcome_email", sel, check=True)
    await dbx.ok(arjun, "team/members/suspend", {"user": sel})
    twice = await dbx.err(arjun, "team/members/suspend", {"user": sel})
    assert twice["error"][".tag"] == "suspend_inactive_user"
    await dbx.ok(arjun, "team/members/unsuspend", {"user": sel})
    nope = await dbx.err(arjun, "team/members/unsuspend", {"user": sel})
    assert nope["error"][".tag"] == "unsuspend_non_suspended_member"
    unknown = await dbx.err(
        arjun, "team/members/suspend", {"user": {".tag": "email", "email": "ghost@x.example"}}
    )
    assert unknown["error"][".tag"] == "user_not_found"
    removed = await dbx.ok(arjun, "team/members/remove", {"user": sel})
    assert removed[".tag"] == "async_job_id"
    job = await dbx.ok(arjun, "team/members/remove/job_status/get", {"async_job_id": removed["async_job_id"]})
    assert job[".tag"] == "complete"
    listing = await dbx.ok(arjun, "team/members/list_v2", {"include_removed": True})
    status = {m["profile"]["email"]: m["profile"]["status"] for m in listing["members"]}
    assert status["new.hire@halcyonadvisory.example"][".tag"] == "removed"
    default = await dbx.ok(arjun, "team/members/list_v2", {})
    assert "new.hire@halcyonadvisory.example" not in {m["profile"]["email"] for m in default["members"]}
    await dbx.ok(arjun, "team/members/remove", {"user": {".tag": "email", "email": dbx.SARAH}})
    last_admin = await dbx.err(arjun, "team/members/remove", {"user": {".tag": "email", "email": dbx.ARJUN}})
    assert last_admin["error"][".tag"] == "remove_last_admin"


async def test_suspended_member_tokens_stop_working(dbx: Any) -> None:
    arjun = await dbx.team_token()
    ryan = await dbx.token("ryan.murphy@halcyonadvisory.example")
    await dbx.ok(ryan, "users/get_current_account")
    await dbx.ok(
        arjun,
        "team/members/suspend",
        {"user": {".tag": "email", "email": "ryan.murphy@halcyonadvisory.example"}},
    )
    err = await dbx.err(ryan, "users/get_current_account", status=401)
    assert err["error"][".tag"] == "user_suspended"
    await dbx.ok(
        arjun,
        "team/members/unsuspend",
        {"user": {".tag": "email", "email": "ryan.murphy@halcyonadvisory.example"}},
    )
    await dbx.ok(ryan, "users/get_current_account")


async def test_team_log_events(dbx: Any) -> None:
    arjun = await dbx.team_token()
    maya = await dbx.token(dbx.MAYA)
    await dbx.put(maya, "/audit/log.txt", b"x")
    page = await dbx.ok(arjun, "team_log/get_events", {"limit": 5})
    assert page["events"] and "cursor" in page
    cats = await dbx.ok(arjun, "team_log/get_events", {"category": {".tag": "file_operations"}})
    assert all(e["event_category"][".tag"] == "file_operations" for e in cats["events"])
    mine = await dbx.ok(arjun, "team_log/get_events", {"account_id": await dbx.account_id(dbx.MAYA)})
    assert any(
        e["event_type"][".tag"] == "file_add" and e["assets"][0]["display_name"] == "log.txt"
        for e in mine["events"]
    )
    paged = await dbx.ok(arjun, "team_log/get_events", {"limit": 3})
    cont = await dbx.ok(arjun, "team_log/get_events/continue", {"cursor": paged["cursor"]})
    assert cont["events"] and cont["events"][0] != paged["events"][0]
    bad = await dbx.err(arjun, "team_log/get_events", {"account_id": "dbid:" + "A" * 35})
    assert bad["error"][".tag"] == "account_id_not_found"
    bad_cursor = await dbx.err(arjun, "team_log/get_events/continue", {"cursor": "nope"})
    assert bad_cursor["error"][".tag"] == "bad_cursor"
