"""Per-user visibility and write permissions: viewer vs editor roles, restricted items, admins."""

from __future__ import annotations

from typing import Any

import pytest

RYAN = "ryan.murphy@halcyonadvisory.example"  # audit: editor on Client Engagements, no Finance access
FINANCE_PATH = "/Finance & Operations"


async def finance_member(dbx: Any, hub: Any, role: str) -> str:
    """Email of a person whose *only* route to Finance & Operations is the given role."""
    world = hub.world
    space = world.space("finance")
    groups = {pid: r for pid, r in space.members}
    people_by_role: dict[str, set[str]] = {"editor": set(), "viewer": set()}
    for principal, r in groups.items():
        mapped = "editor" if r in ("editor", "owner") else "viewer"
        people_by_role[mapped].update(world.expand_principal(principal))
    candidates = people_by_role[role] - (people_by_role["editor"] if role == "viewer" else set())
    for pid in sorted(candidates):
        person = world.person(pid)
        if person.active:
            return person.email
    raise AssertionError(f"no {role} candidate")


async def test_viewers_can_read_but_not_write_team_folders(dbx: Any, hub: Any) -> None:
    viewer = await finance_member(dbx, hub, "viewer")
    token = await dbx.token(viewer)
    root = await dbx.root(token)
    listing = await dbx.list_all(token, FINANCE_PATH, path_root=root)
    assert listing
    folder = await dbx.ok(token, "files/get_metadata", {"path": FINANCE_PATH}, path_root=root)
    assert folder["sharing_info"]["read_only"] is True
    file = next(
        e
        for e in await dbx.list_all(token, FINANCE_PATH, recursive=True, path_root=root)
        if e[".tag"] == "file"
    )
    assert file["sharing_info"]["read_only"] is True
    r, _ = await dbx.download(token, file["path_display"], path_root=root)
    assert r.status_code == 200
    denied_upload = await dbx.upload(
        token, "files/upload", {"path": f"{FINANCE_PATH}/new.txt"}, b"x", path_root=root
    )
    assert denied_upload.status_code == 409
    assert denied_upload.json()["error"]["reason"][".tag"] == "no_write_permission"
    for route, body in (
        ("files/create_folder_v2", {"path": f"{FINANCE_PATH}/NewFolder"}),
        ("files/delete_v2", {"path": file["path_display"]}),
        ("files/move_v2", {"from_path": file["path_display"], "to_path": "/Maya"}),
        ("files/copy_v2", {"from_path": file["path_display"], "to_path": f"{FINANCE_PATH}/copy.txt"}),
    ):
        err = await dbx.err(token, route, body, path_root=root)
        assert "no_write_permission" in err["error_summary"], (route, err)
    # copying out into one's own home is fine, and the copy is writable
    mine = hub.world.person_by_email(viewer).display_name
    copied = await dbx.ok(
        token,
        "files/copy_v2",
        {"from_path": file["path_display"], "to_path": f"/{mine}/copy-of-{file['name']}"},
        path_root=root,
    )
    assert "sharing_info" not in copied["metadata"]  # a copy in one's own member folder is not shared
    elsewhere = await dbx.err(
        token,
        "files/copy_v2",
        {"from_path": file["path_display"], "to_path": "/Maya Chen/not-allowed.txt"},
        path_root=root,
    )
    assert "no_write_permission" in elsewhere["error_summary"]


async def test_editors_write_and_cross_namespace_moves(dbx: Any, hub: Any) -> None:
    editor = await finance_member(dbx, hub, "editor")
    token = await dbx.token(editor)
    root = await dbx.root(token)
    meta = await dbx.put(token, f"{FINANCE_PATH}/Payroll/editor-note.txt", b"note", path_root=root)
    assert meta["sharing_info"]["read_only"] is False
    folder = await dbx.ok(
        token, "files/create_folder_v2", {"path": f"{FINANCE_PATH}/Editors"}, path_root=root
    )
    assert folder["metadata"]["sharing_info"]["parent_shared_folder_id"]
    # move from the team folder into the editor's own member folder and back
    person = hub.world.person_by_email(editor)
    mine = f"/{person.display_name}/moved-note.txt"
    out = await dbx.ok(
        token, "files/move_v2", {"from_path": meta["path_display"], "to_path": mine}, path_root=root
    )
    assert out["metadata"]["id"] == meta["id"] and out["metadata"]["path_display"] == mine
    assert "sharing_info" not in out["metadata"]
    back = await dbx.ok(
        token,
        "files/move_v2",
        {"from_path": mine, "to_path": f"{FINANCE_PATH}/Editors/back.txt"},
        path_root=root,
    )
    assert (
        back["metadata"]["sharing_info"]["parent_shared_folder_id"]
        == meta["sharing_info"]["parent_shared_folder_id"]
    )


async def test_member_folders_are_private_even_to_admins_without_select_admin(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    await dbx.put(maya, "/Notes/private-note.txt", b"mine")
    arjun = await dbx.token(dbx.ARJUN)  # a plain user token of an admin
    root = await dbx.root(arjun)
    names = {e["name"] for e in await dbx.list_all(arjun, "", path_root=root)}
    assert "Maya Chen" not in names
    err = await dbx.err(
        arjun, "files/get_metadata", {"path": "/Maya Chen/Notes/private-note.txt"}, path_root=root
    )
    assert err["error"]["path"][".tag"] == "not_found"
    # with Select-Admin the team admin can reach it through the member folder
    team = await dbx.team_token()
    admin_id = await dbx.member_id(dbx.ARJUN)
    admin_root = await dbx.root(team, select_user=admin_id)
    meta = await dbx.ok(
        team,
        "files/get_metadata",
        {"path": "/Maya Chen/Notes/private-note.txt"},
        select_admin=admin_id,
        path_root=admin_root,
    )
    assert meta["size"] == 4


async def test_restricted_file_by_id_is_hidden_from_other_members(dbx: Any, hub: Any) -> None:
    world = hub.world
    doc = world.document(
        "d0260"
    )  # Compensation/Salary Bands 2026.xlsx, restricted, shared with p001 and p025
    owner = world.person(doc.owner_id)
    sharer = world.person(doc.shares[0].principal_id)
    token = await dbx.token(owner.email)
    root = await dbx.root(token)
    entries = await dbx.list_all(token, "/People & Culture/Compensation", path_root=root)
    sheet = next(e for e in entries if e["name"] == doc.name)
    # a viewer of the People & Culture team folder (everyone via g-all) cannot see or fetch it
    bystander = await dbx.token("tyler.brooks@halcyonadvisory.example")
    by_root = await dbx.root(bystander)
    assert doc.name not in {
        e["name"] for e in await dbx.list_all(bystander, "/People & Culture/Compensation", path_root=by_root)
    }
    err = await dbx.err(bystander, "files/get_metadata", {"path": sheet["id"]}, path_root=by_root)
    assert err["error"]["path"][".tag"] == "not_found"
    r, _ = await dbx.download(bystander, sheet["id"], path_root=by_root)
    assert r.status_code == 409
    search = await dbx.ok(bystander, "files/search_v2", {"query": "salary bands"}, path_root=by_root)
    assert all(m["metadata"]["metadata"]["name"] != doc.name for m in search["matches"])
    # explicit members (p001 viewer) can read but not write
    reader = await dbx.token(world.person(sharer.id).email)
    r2, _ = await dbx.download(reader, sheet["id"], path_root=await dbx.root(reader))
    assert r2.status_code == 200
    write = await dbx.upload(
        reader,
        "files/upload",
        {"path": sheet["id"], "mode": "overwrite"},
        b"x",
        path_root=await dbx.root(reader),
    )
    assert write.status_code == 409


async def test_external_collaborators_cannot_browse_internal_content(dbx: Any) -> None:
    chris = await dbx.token(dbx.CHRISTOPHER)
    assert await dbx.list_all(chris, "") == []
    info = await dbx.ok(chris, "users/get_current_account")
    ns_root = info["root_info"]["root_namespace_id"]
    assert ns_root == info["root_info"]["home_namespace_id"]
    maya = await dbx.token(dbx.MAYA)
    team_ns = await dbx.root_ns(maya)
    denied = await dbx.rpc(
        chris, "files/list_folder", {"path": ""}, path_root={".tag": "namespace_id", "namespace_id": team_ns}
    )
    assert denied.status_code == 422
    root_err = await dbx.rpc(
        chris, "files/list_folder", {"path": ""}, path_root={".tag": "root", "root": team_ns}
    )
    assert root_err.status_code == 422 and root_err.json()["error"]["invalid_root"][".tag"] == "user"
    team_err = await dbx.err(chris, "team/get_info", status=400)
    assert "single user" in team_err


async def test_expired_file_share_loses_access(dbx: Any, hub: Any) -> None:
    from datetime import UTC, datetime, timedelta

    hub.clock.freeze(datetime(2026, 10, 16, 12, tzinfo=UTC))  # the world's anchor day, whatever today is
    try:
        maya = await dbx.token(dbx.MAYA)
        chris = await dbx.token(dbx.CHRISTOPHER)
        root = await dbx.root(maya)
        entries = await dbx.list_all(
            maya, "/Client Engagements/Northwind Logistics/FY2025 Review/Deliverables", path_root=root
        )
        draft = next(
            e for e in entries if e["name"] == "Draft Financial Statements.pdf"
        )  # share expires 2026-11-15
        r, _ = await dbx.download(chris, draft["id"])
        assert r.status_code == 200
        hub.clock.advance(timedelta(days=60))
        gone, _ = await dbx.download(await dbx.token(dbx.CHRISTOPHER, expires_in=86400), draft["id"])
        assert gone.status_code == 409
    finally:
        hub.clock.unfreeze()


@pytest.mark.parametrize("path", ["/", "/Notes/"])
async def test_root_and_trailing_slash_paths(dbx: Any, path: str) -> None:
    maya = await dbx.token(dbx.MAYA)
    r = await dbx.rpc(maya, "files/list_folder", {"path": path})
    if path == "/":
        assert r.status_code == 409 and r.json()["error"]["path"][".tag"] == "malformed_path"
    else:
        assert r.status_code in (200, 409)
