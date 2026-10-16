"""Folders: shape, listing, pagination, create, update, move, copy, trash."""

from __future__ import annotations

import re
from datetime import timedelta

NORTHWIND = "Client Engagements/Northwind Logistics"
BOX_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-0[78]:00$")


async def test_root_folder_lists_team_spaces_and_personal_content(box):
    root = (await box.get("/folders/0", "p035")).json()
    assert root["type"] == "folder" and root["id"] == "0" and root["name"] == "All Files"
    assert root["etag"] is None and root["sequence_id"] is None and root["parent"] is None
    assert root["path_collection"] == {"total_count": 0, "entries": []}
    assert root["owned_by"]["login"] == "arjun.mehta@halcyonadvisory.example"
    names = [e["name"] for e in root["item_collection"]["entries"]]
    for space in (
        "Client Engagements",
        "Finance & Operations",
        "Partner Group",
        "Technology",
        "Knowledge Base",
    ):
        assert space in names
    assert root["item_collection"]["limit"] == 100
    assert root["item_collection"]["order"] == [
        {"by": "type", "direction": "ASC"},
        {"by": "name", "direction": "ASC"},
    ]


async def test_folder_standard_fields_and_path_collection(box):
    folder_id = await box.find(f"{NORTHWIND}/FY2025 Review/Deliverables")
    folder = (await box.get(f"/folders/{folder_id}")).json()
    assert list(folder)[:6] == ["type", "id", "sequence_id", "etag", "name", "created_at"]
    for key in (
        "description", "size", "path_collection", "created_by", "modified_by", "trashed_at", "purged_at",
        "content_created_at", "content_modified_at", "owned_by", "shared_link", "folder_upload_email",
        "parent", "item_status", "item_collection",
    ):  # fmt: skip
        assert key in folder, key
    assert folder["item_status"] == "active"
    assert folder["etag"] == "0" == folder["sequence_id"]
    assert BOX_TS.match(folder["created_at"]) and BOX_TS.match(folder["modified_at"])
    assert [e["name"] for e in folder["path_collection"]["entries"]] == [
        "All Files",
        "Client Engagements",
        "Northwind Logistics",
        "FY2025 Review",
    ]
    assert folder["path_collection"]["total_count"] == 4
    assert folder["parent"]["name"] == "FY2025 Review"
    assert folder["size"] == sum(e["size"] for e in await box.items(folder_id, fields="size"))
    entries = folder["item_collection"]["entries"]
    assert {e["type"] for e in entries} == {"file"}
    assert set(entries[0]) == {"type", "id", "file_version", "sequence_id", "etag", "sha1", "name"}


async def test_fields_parameter_returns_mini_representation_plus_requested(box):
    folder_id = await box.find(NORTHWIND)
    r = await box.get(f"/folders/{folder_id}", params={"fields": "name,permissions,has_collaborations"})
    body = r.json()
    assert set(body) == {"type", "id", "sequence_id", "etag", "name", "permissions", "has_collaborations"}
    assert body["permissions"]["can_download"] is True and body["permissions"]["can_upload"] is True
    assert body["has_collaborations"] is False
    shared = (
        await box.get(
            f"/folders/{await box.find('Client Engagements')}", params={"fields": "has_collaborations"}
        )
    ).json()
    assert shared["has_collaborations"] is True
    listed = await box.items(folder_id, fields="name,modified_at,size,item_status")
    assert all({"name", "modified_at", "size", "item_status", "etag"} <= set(e) for e in listed)


async def test_folder_items_pagination_and_sorting(box):
    clients = await box.find("Client Engagements")
    page1 = (await box.get(f"/folders/{clients}/items", params={"limit": 5})).json()
    assert page1["total_count"] == 23 and page1["offset"] == 0 and page1["limit"] == 5
    assert len(page1["entries"]) == 5
    page2 = (await box.get(f"/folders/{clients}/items", params={"limit": 5, "offset": 5})).json()
    assert page2["offset"] == 5
    assert {e["id"] for e in page1["entries"]}.isdisjoint({e["id"] for e in page2["entries"]})
    seen: list[str] = []
    marker = None
    while True:
        params = {"limit": 10, "usemarker": "true", **({"marker": marker} if marker else {})}
        body = (await box.get(f"/folders/{clients}/items", params=params)).json()
        assert "total_count" not in body and "offset" not in body
        seen += [e["name"] for e in body["entries"]]
        marker = body["next_marker"]
        if not marker:
            break
    assert len(seen) == 23 and seen == sorted(seen, key=str.lower)
    desc = (await box.get(f"/folders/{clients}/items", params={"sort": "name", "direction": "DESC"})).json()
    assert desc["order"] == [{"by": "name", "direction": "DESC"}]
    names = [e["name"] for e in desc["entries"]]
    assert names == sorted(names, key=str.lower, reverse=True)
    by_size = (await box.get(f"/folders/{clients}/items", params={"sort": "size", "fields": "size"})).json()
    sizes = [e["size"] for e in by_size["entries"]]
    assert sizes == sorted(sizes)
    by_id = (await box.get(f"/folders/{clients}/items", params={"sort": "id", "direction": "DESC"})).json()
    ids = [int(e["id"]) for e in by_id["entries"]]
    assert ids == sorted(ids, reverse=True)
    assert (await box.get(f"/folders/{clients}/items", params={"limit": 1001})).status_code == 400
    assert (await box.get(f"/folders/{clients}/items", params={"sort": "color"})).status_code == 400


async def test_missing_folder_is_a_box_404(box):
    r = await box.get("/folders/123456789")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "not_found" and body["message"] == "Not Found" and body["status"] == 404
    assert (await box.get("/folders/abc")).status_code == 404


async def test_create_folder_and_name_conflicts(box):
    parent = await box.find("Technology")
    r = await box.post("/folders", json={"name": "Runbooks 2026", "parent": {"id": parent}})
    assert r.status_code == 201
    folder = r.json()
    assert folder["name"] == "Runbooks 2026" and folder["parent"]["id"] == parent
    assert folder["item_collection"]["total_count"] == 0
    assert folder["created_by"]["name"] == "Arjun Mehta"
    dup = await box.post("/folders", json={"name": "runbooks 2026", "parent": {"id": parent}})
    assert dup.status_code == 409
    body = dup.json()
    assert body["code"] == "item_name_in_use"
    assert body["context_info"]["conflicts"][0]["id"] == folder["id"]
    assert body["context_info"]["conflicts"][0]["type"] == "folder"
    for bad in ("", "a/b", "a\\b", ".", "..", " lead", "x" * 256):
        assert (
            await box.post("/folders", json={"name": bad, "parent": {"id": parent}})
        ).status_code == 400, bad
    assert (await box.post("/folders", json={"parent": {"id": parent}})).status_code == 400
    assert (await box.post("/folders", json={"name": "Orphan", "parent": {"id": "999"}})).status_code == 404
    assert (await box.post("/folders", content=b"not json")).status_code == 400
    upload_email = await box.post(
        "/folders",
        json={"name": "Inbox", "parent": {"id": parent}, "folder_upload_email": {"access": "open"}},
    )
    assert upload_email.json()["folder_upload_email"]["access"] == "open"


async def test_update_rename_move_and_description(box):
    src = await box.find("Technology")
    a = await box.mkdir(src, "Alpha")
    b = await box.mkdir(src, "Beta")
    renamed = await box.put(
        f"/folders/{a}", json={"name": "Alpha 2", "description": "First folder", "tags": ["x", "y"]}
    )
    assert renamed.status_code == 200
    body = renamed.json()
    assert body["name"] == "Alpha 2" and body["description"] == "First folder"
    assert body["etag"] == "1" == body["sequence_id"]
    assert (await box.get(f"/folders/{a}", params={"fields": "tags"})).json()["tags"] == ["x", "y"]
    moved = await box.put(f"/folders/{a}", json={"parent": {"id": b}})
    assert moved.json()["parent"]["id"] == b
    assert [e["name"] for e in moved.json()["path_collection"]["entries"]][-2:] == ["Technology", "Beta"]
    clash = await box.mkdir(src, "Alpha 2")
    assert (await box.put(f"/folders/{clash}", json={"parent": {"id": b}})).status_code == 409
    assert (await box.put(f"/folders/{b}", json={"parent": {"id": a}})).status_code == 400
    assert (await box.put(f"/folders/{b}", json={"parent": {"id": b}})).status_code == 400
    assert (await box.put("/folders/0", json={"name": "x"})).status_code == 403


async def test_etags_conditional_requests_and_if_match(box):
    parent = await box.find("Technology")
    fid = await box.mkdir(parent, "Etag demo")
    assert (await box.get(f"/folders/{fid}", headers={"If-None-Match": "0"})).status_code == 304
    stale = await box.put(f"/folders/{fid}", headers={"If-Match": "7"}, json={"name": "Changed"})
    assert stale.status_code == 412
    assert stale.json()["code"] == "precondition_failed"
    ok = await box.put(f"/folders/{fid}", headers={"If-Match": "0"}, json={"name": "Changed"})
    assert ok.status_code == 200 and ok.json()["etag"] == "1"
    assert (await box.delete(f"/folders/{fid}", headers={"If-Match": "0"})).status_code == 412


async def test_delete_folder_trash_restore_and_purge(box):
    parent = await box.find("Technology")
    fid = await box.mkdir(parent, "Scratch")
    await box.upload(fid, "note.txt", b"keep me")
    not_empty = await box.delete(f"/folders/{fid}")
    assert not_empty.status_code == 400 and not_empty.json()["code"] == "folder_not_empty"
    assert (await box.delete(f"/folders/{fid}", params={"recursive": "true"})).status_code == 204
    gone = await box.get(f"/folders/{fid}")
    assert gone.status_code == 404 and gone.json()["code"] == "trashed"
    assert fid not in [e["id"] for e in await box.items(parent)]
    trash = (await box.get("/folders/trash/items")).json()
    assert [e["id"] for e in trash["entries"]] == [fid]
    trashed = (await box.get(f"/folders/{fid}/trash")).json()
    assert trashed["item_status"] == "trashed" and trashed["trashed_at"]
    assert [e["name"] for e in trashed["path_collection"]["entries"]] == ["All Files", "Trash"]
    # While the folder is in the trash its content is unreachable too.
    child = (await box.get(f"/folders/{fid}/items")).status_code
    assert child == 404
    await box.mkdir(parent, "Scratch")  # a new folder takes the name while the old one is trashed
    clash = await box.post(f"/folders/{fid}", json={})
    assert clash.status_code == 409
    restored = await box.post(f"/folders/{fid}", json={"name": "Scratch (restored)"})
    assert restored.status_code == 201
    assert restored.json()["name"] == "Scratch (restored)" and restored.json()["item_status"] == "active"
    assert [e["name"] for e in await box.items(fid)] == ["note.txt"]
    await box.delete(f"/folders/{fid}", params={"recursive": "true"})
    assert (await box.delete(f"/folders/{fid}/trash")).status_code == 204
    assert (await box.get(f"/folders/{fid}/trash")).status_code == 404
    assert (await box.get("/folders/trash/items")).json()["entries"] == []
    assert (await box.delete("/folders/0")).status_code == 403


async def test_copy_folder_is_deep_and_respects_name_rules(box):
    parent = await box.find("Technology")
    src = await box.mkdir(parent, "Original")
    inner = await box.mkdir(src, "Inner")
    await box.upload(inner, "deep.txt", b"deep")
    await box.upload(src, "top.txt", b"top")
    clash = await box.post(f"/folders/{src}/copy", json={"parent": {"id": parent}})
    assert clash.status_code == 409
    copied = await box.post(f"/folders/{src}/copy", json={"parent": {"id": parent}, "name": "Original copy"})
    assert copied.status_code == 201
    new_id = copied.json()["id"]
    assert new_id != src
    names = {e["name"] for e in await box.items(new_id)}
    assert names == {"Inner", "top.txt"}
    inner_copy = next(e for e in await box.items(new_id) if e["name"] == "Inner")
    assert [e["name"] for e in await box.items(inner_copy["id"])] == ["deep.txt"]
    assert inner_copy["id"] != inner
    assert (
        await box.post(f"/folders/{src}/copy", json={"parent": {"id": inner}, "name": "Loop"})
    ).status_code == 400
    assert (
        await box.post("/folders/0/copy", json={"parent": {"id": parent}, "name": "Root"})
    ).status_code == 403


async def test_folder_modified_at_follows_content_changes(box):
    parent = await box.find("Technology")
    fid = await box.mkdir(parent, "Touch")
    before = (await box.get(f"/folders/{fid}")).json()["modified_at"]
    box.hub.clock.advance(timedelta(minutes=5))
    await box.upload(fid, "a.txt", b"a")
    after = (await box.get(f"/folders/{fid}")).json()["modified_at"]
    assert after > before
