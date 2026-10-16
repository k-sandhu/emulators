"""list_folder options: mounted folders, limits and delta-visible deletions inside shared folders."""

from __future__ import annotations

from typing import Any


async def test_include_mounted_folders_controls_descent(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    root = await dbx.root(maya)
    shallow = await dbx.list_all(maya, "", recursive=True, include_mounted_folders=False, path_root=root)
    deep = await dbx.list_all(maya, "", recursive=True, path_root=root)
    assert {e["name"] for e in shallow} >= {"Client Engagements", "Maya Chen"}
    assert all(e["path_lower"].count("/") == 1 for e in shallow)  # mount points only, nothing below them
    assert len(deep) > 300 and len(shallow) < 12
    # a shared folder mounted in the home namespace behaves the same way
    await dbx.put(maya, "/Shared Thing/inner/file.txt", b"x")
    await dbx.ok(maya, "sharing/share_folder", {"path": "/Shared Thing"})
    with_mounts = await dbx.list_all(maya, "", recursive=True)
    without = await dbx.list_all(maya, "", recursive=True, include_mounted_folders=False)
    assert "/shared thing/inner/file.txt" in {e["path_lower"] for e in with_mounts}
    assert "/shared thing/inner/file.txt" not in {e["path_lower"] for e in without}
    assert "/shared thing" in {e["path_lower"] for e in without}


async def test_limit_bounds_and_single_item_pages(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    for bad in (0, 2001):
        r = await dbx.rpc(maya, "files/list_folder", {"path": "", "limit": bad})
        assert r.status_code == 400, bad
    seen: list[str] = []
    page = await dbx.ok(maya, "files/list_folder", {"path": "", "limit": 1, "recursive": True})
    seen += [e["path_lower"] for e in page["entries"]]
    pages = 1
    while page["has_more"]:
        page = await dbx.ok(maya, "files/list_folder/continue", {"cursor": page["cursor"]})
        seen += [e["path_lower"] for e in page["entries"]]
        pages += 1
    assert pages == len(seen) and len(seen) == len(set(seen)) >= 4


async def test_changes_inside_a_shared_folder_reach_other_members_cursors(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    ryan = await dbx.token("ryan.murphy@halcyonadvisory.example")
    await dbx.put(maya, "/Collab/readme.txt", b"hi")
    meta = await dbx.ok(maya, "sharing/share_folder", {"path": "/Collab"})
    await dbx.ok(
        maya,
        "sharing/add_folder_member",
        {
            "shared_folder_id": meta["shared_folder_id"],
            "members": [
                {
                    "member": {".tag": "email", "email": "ryan.murphy@halcyonadvisory.example"},
                    "access_level": {".tag": "editor"},
                }
            ],
        },
    )
    await dbx.ok(ryan, "sharing/mount_folder", {"shared_folder_id": meta["shared_folder_id"]})
    cursor = (
        await dbx.ok(ryan, "files/list_folder/get_latest_cursor", {"path": "/Collab", "recursive": True})
    )["cursor"]
    await dbx.put(maya, "/Collab/from-maya.txt", b"1")
    await dbx.ok(maya, "files/delete_v2", {"path": "/Collab/readme.txt"})
    delta = await dbx.ok(ryan, "files/list_folder/continue", {"cursor": cursor})
    summary = {(e[".tag"], e["path_lower"]) for e in delta["entries"]}
    assert summary == {("file", "/collab/from-maya.txt"), ("deleted", "/collab/readme.txt")}
    # the folder-level cursor on the owner's side sees the mount's content changes too (include_mounted_folders)
    owner_cursor = (
        await dbx.ok(maya, "files/list_folder/get_latest_cursor", {"path": "", "recursive": True})
    )["cursor"]
    await dbx.put(ryan, "/Collab/from-ryan.txt", b"2")
    owner_delta = await dbx.ok(maya, "files/list_folder/continue", {"cursor": owner_cursor})
    assert [e["path_lower"] for e in owner_delta["entries"]] == ["/collab/from-ryan.txt"]
