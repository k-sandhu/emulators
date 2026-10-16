"""Comments, tasks, web links, collections (favourites), recent items and zip downloads."""

from __future__ import annotations

import io
import zipfile
from datetime import timedelta

DELIVERABLES = "Client Engagements/Northwind Logistics/FY2025 Review/Deliverables"


async def new_file(box, name="note.txt", data=b"hello", who="p035", folder="Technology") -> str:
    parent = await box.find(folder, who)
    return (await box.upload(parent, name, data, who)).json()["entries"][0]["id"]


# -- comments ------------------------------------------------------------------------------------


async def test_comment_lifecycle_and_replies(box):
    fid = await new_file(box)
    created = await box.post("/comments", json={"message": "First!", "item": {"type": "file", "id": fid}})
    assert created.status_code == 201
    comment = created.json()
    assert (
        comment["type"] == "comment"
        and comment["message"] == "First!"
        and comment["is_reply_comment"] is False
    )
    assert comment["item"] == {"id": fid, "type": "file"} and comment["created_by"]["name"] == "Arjun Mehta"
    reply = await box.post(
        "/comments", json={"message": "Reply", "item": {"type": "comment", "id": comment["id"]}}
    )
    assert reply.status_code == 201 and reply.json()["is_reply_comment"] is True
    listing = (await box.get(f"/files/{fid}/comments")).json()
    assert listing["total_count"] == 2 and [c["message"] for c in listing["entries"]] == ["First!", "Reply"]
    assert (await box.get(f"/files/{fid}", params={"fields": "comment_count"})).json()["comment_count"] == 2
    one = await box.get(f"/comments/{comment['id']}")
    assert one.status_code == 200 and one.json()["id"] == comment["id"]
    edited = await box.put(f"/comments/{comment['id']}", json={"message": "Edited"})
    assert edited.json()["message"] == "Edited"
    assert (
        await box.put(f"/comments/{comment['id']}", "p036", json={"message": "hijack"})
    ).status_code == 403
    assert (await box.delete(f"/comments/{comment['id']}")).status_code == 204
    assert (await box.get(f"/comments/{comment['id']}")).status_code == 404
    assert (await box.get(f"/comments/{reply.json()['id']}")).status_code == 404  # replies go with the parent
    assert (await box.post("/comments", json={"item": {"type": "file", "id": fid}})).status_code == 400
    assert (
        await box.post("/comments", json={"message": "x", "item": {"type": "file", "id": "1"}})
    ).status_code == 404


async def test_comment_permissions_and_mentions(box):
    folder = await box.find(DELIVERABLES, "p030")  # finance staff are viewers of client files
    entry = (await box.items(folder, "p030"))[0]
    commented = await box.post(
        "/comments", "p030", json={"message": "Viewer note", "item": {"type": "file", "id": entry["id"]}}
    )
    assert commented.status_code == 201  # viewers may comment
    outsider = await box.post(
        "/comments", "p044", json={"message": "x", "item": {"type": "file", "id": entry["id"]}}
    )
    assert outsider.status_code == 404
    user = (await box.get("/users/me", "p004")).json()
    tagged = await box.post(
        "/comments",
        "p030",
        json={
            "tagged_message": f"See this @[{user['id']}:Daniel Kowalski]",
            "item": {"type": "file", "id": entry["id"]},
        },
    )
    assert tagged.json()["message"] == "See this @Daniel Kowalski"
    assert tagged.json()["tagged_message"].startswith("See this @[")
    # The author of a comment on someone else's file can delete it; others cannot.
    cid = commented.json()["id"]
    assert (await box.delete(f"/comments/{cid}", "p029")).status_code == 403  # another finance viewer
    assert (await box.delete(f"/comments/{cid}", "p030")).status_code == 204


async def test_seeded_review_comments_exist_on_deliverables(box):
    folder = await box.find(DELIVERABLES)
    comments = 0
    for entry in await box.items(folder):
        comments += (await box.get(f"/files/{entry['id']}/comments")).json()["total_count"]
    assert comments >= 1


# -- tasks -----------------------------------------------------------------------------------------


async def test_tasks_and_assignments(box):
    fid = await new_file(box)
    reviewer = (await box.get("/users/me", "p036")).json()
    task = await box.post(
        "/tasks",
        json={
            "item": {"type": "file", "id": fid},
            "action": "review",
            "message": "Please review",
            "due_at": "2026-12-01T00:00:00Z",
        },
    )
    assert task.status_code == 201, task.text
    body = task.json()
    assert body["type"] == "task" and body["action"] == "review" and body["is_completed"] is False
    assert body["item"]["id"] == fid and body["task_assignment_collection"] == {
        "total_count": 0,
        "entries": [],
    }
    assert body["completion_rule"] == "all_assignees" and body["due_at"].startswith("2026-11-30")
    assignment = await box.post(
        "/task_assignments",
        json={"task": {"id": body["id"], "type": "task"}, "assign_to": {"id": reviewer["id"]}},
    )
    assert assignment.status_code == 201
    a = assignment.json()
    assert a["assigned_to"]["name"] == "Sarah Johansson" and a["resolution_state"] == "incomplete"
    assert a["assigned_by"]["name"] == "Arjun Mehta" and a["item"]["id"] == fid
    dup = await box.post(
        "/task_assignments",
        json={"task": {"id": body["id"], "type": "task"}, "assign_to": {"id": reviewer["id"]}},
    )
    assert dup.status_code == 409
    assert (await box.get(f"/files/{fid}/tasks")).json()["total_count"] == 1
    assert (await box.get(f"/tasks/{body['id']}/assignments")).json()["total_count"] == 1
    assert (
        await box.put(f"/task_assignments/{a['id']}", json={"resolution_state": "approved"})
    ).status_code == 403
    done = await box.put(
        f"/task_assignments/{a['id']}", "p036", json={"resolution_state": "approved", "message": "LGTM"}
    )
    assert (
        done.status_code == 200
        and done.json()["resolution_state"] == "approved"
        and done.json()["completed_at"]
    )
    assert (await box.get(f"/tasks/{body['id']}")).json()["is_completed"] is True
    assert (
        await box.put(f"/task_assignments/{a['id']}", "p036", json={"resolution_state": "maybe"})
    ).status_code == 400
    updated = await box.put(
        f"/tasks/{body['id']}", json={"message": "Reviewed", "completion_rule": "any_assignee"}
    )
    assert updated.json()["message"] == "Reviewed"
    assert (await box.put(f"/tasks/{body['id']}", "p036", json={"message": "x"})).status_code == 403
    assert (await box.delete(f"/task_assignments/{a['id']}", "p036")).status_code == 204
    assert (await box.delete(f"/tasks/{body['id']}")).status_code == 204
    assert (await box.get(f"/tasks/{body['id']}")).status_code == 404
    for bad in ({"item": {"type": "file", "id": fid}, "action": "dance"}, {"action": "review"}):
        assert (await box.post("/tasks", json=bad)).status_code == 400


# -- web links -------------------------------------------------------------------------------------


async def test_web_links(box):
    parent = await box.find("Technology")
    created = await box.post(
        "/web_links",
        json={
            "url": "https://status.example.test",
            "parent": {"id": parent},
            "name": "Status",
            "description": "Uptime",
        },
    )
    assert created.status_code == 201, created.text
    link = created.json()
    assert (
        link["type"] == "web_link"
        and link["url"] == "https://status.example.test"
        and link["name"] == "Status"
    )
    assert link["parent"]["id"] == parent and link["item_status"] == "active"
    listed = [e for e in await box.items(parent, fields="url") if e["type"] == "web_link"]
    assert listed == [
        {
            "type": "web_link",
            "id": link["id"],
            "sequence_id": "0",
            "etag": "0",
            "name": "Status",
            "url": "https://status.example.test",
        }
    ]
    assert (
        await box.post("/web_links", json={"url": "ftp://x", "parent": {"id": parent}})
    ).status_code == 400
    assert (
        await box.post(
            "/web_links",
            json={"url": "https://status.example.test", "parent": {"id": parent}, "name": "Status"},
        )
    ).status_code == 409
    renamed = await box.put(
        f"/web_links/{link['id']}", json={"name": "Status page", "url": "https://status.example.test/v2"}
    )
    assert renamed.json()["name"] == "Status page" and renamed.json()["url"].endswith("/v2")
    assert (await box.delete(f"/web_links/{link['id']}")).status_code == 204
    assert (await box.get(f"/web_links/{link['id']}")).status_code == 404
    assert (await box.get(f"/web_links/{link['id']}/trash")).json()["item_status"] == "trashed"
    assert link["id"] in [e["id"] for e in (await box.get("/folders/trash/items")).json()["entries"]]
    assert (await box.post(f"/web_links/{link['id']}", json={})).status_code == 201
    assert (await box.get(f"/web_links/{link['id']}")).status_code == 200
    await box.delete(f"/web_links/{link['id']}")
    assert (await box.delete(f"/web_links/{link['id']}/trash")).status_code == 204


# -- collections -----------------------------------------------------------------------------------


async def test_favorites_collection(box):
    collections = (await box.get("/collections", "p018")).json()
    assert collections["total_count"] == 1
    favorites = collections["entries"][0]
    assert (
        favorites["type"] == "collection"
        and favorites["name"] == "Favorites"
        and favorites["collection_type"] == "favorites"
    )
    assert (await box.get(f"/collections/{favorites['id']}", "p018")).json() == favorites
    assert (await box.get(f"/collections/{favorites['id']}", "p019")).status_code == 404
    seeded = (await box.get(f"/collections/{favorites['id']}/items", "p018")).json()
    assert seeded["total_count"] >= 1  # starred documents of Maya Chen
    fid = await new_file(box, "star.txt", who="p018", folder="Notes")
    assert (await box.get(f"/files/{fid}", "p018", params={"fields": "collections"})).json()[
        "collections"
    ] == []
    starred = await box.put(
        f"/files/{fid}",
        "p018",
        params={"fields": "collections"},
        json={"collections": [{"id": favorites["id"]}]},
    )
    assert starred.json()["collections"] == [favorites]
    items = (await box.get(f"/collections/{favorites['id']}/items", "p018", params={"fields": "name"})).json()
    assert fid in [e["id"] for e in items["entries"]]
    assert items["total_count"] == seeded["total_count"] + 1
    unstarred = await box.put(
        f"/files/{fid}", "p018", params={"fields": "collections"}, json={"collections": []}
    )
    assert unstarred.json()["collections"] == []
    assert (await box.put(f"/files/{fid}", "p018", json={"collections": [{"id": "1"}]})).status_code == 404
    folder = await box.find("Notes", "p018")
    await box.put(f"/folders/{folder}", "p018", json={"collections": [{"id": favorites["id"]}]})
    again = (await box.get(f"/collections/{favorites['id']}/items", "p018")).json()
    assert folder in [e["id"] for e in again["entries"]]
    # Favourites are personal: another user does not see them.
    assert (await box.get(f"/files/{fid}", "p018", params={"fields": "collections"})).status_code == 200


# -- recent items ----------------------------------------------------------------------------------


async def test_recent_items(box):
    box.hub.clock.advance(timedelta(days=2))  # seeded interactions are dated up to the world anchor
    recent = (await box.get("/recent_items", "p004")).json()
    assert recent["limit"] == 100 and "entries" in recent
    assert recent["entries"], "p004 has recent seeded uploads"
    entry = recent["entries"][0]
    assert entry["type"] == "recent_item" and entry["item"]["type"] == "file"
    assert entry["interaction_type"] in ("item_preview", "item_upload", "item_modify")
    assert entry["interacted_at"] and entry["interaction_shared_link"] is None
    fid = await new_file(
        box, "fresh.txt", who="p004", folder="Client Engagements/Northwind Logistics/Correspondence"
    )
    after = (await box.get("/recent_items", "p004", params={"fields": "name"})).json()
    assert (
        after["entries"][0]["item"]["id"] == fid and after["entries"][0]["interaction_type"] == "item_upload"
    )
    box.hub.clock.advance(timedelta(minutes=1))
    await box.get(f"/files/{fid}/content", "p004")
    previewed = (await box.get("/recent_items", "p004")).json()["entries"]
    assert previewed[0]["interaction_type"] == "item_preview" and previewed[0]["item"]["id"] == fid
    assert [e["item"]["id"] for e in previewed].count(fid) == 1  # one row per item
    page = (await box.get("/recent_items", "p004", params={"limit": 2})).json()
    assert len(page["entries"]) == 2 and page["next_marker"]
    nxt = (await box.get("/recent_items", "p004", params={"limit": 2, "marker": page["next_marker"]})).json()
    assert {e["item"]["id"] for e in nxt["entries"]}.isdisjoint({e["item"]["id"] for e in page["entries"]})
    # Recents only show what the user can currently see.
    hidden = await box.get("/recent_items", "p044")
    assert fid not in [e["item"]["id"] for e in hidden.json()["entries"]]


# -- zip downloads ---------------------------------------------------------------------------------


async def test_zip_download_of_files_and_folders(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Bundle")
    inner = await box.mkdir(folder, "Inner")
    other = await box.mkdir(parent, "Other")
    await box.upload(folder, "a.txt", b"alpha")
    await box.upload(inner, "b.txt", b"bravo")
    loose = (await box.upload(parent, "a.txt", b"loose alpha")).json()["entries"][0]["id"]
    twin = (await box.upload(other, "a.txt", b"twin alpha")).json()["entries"][0]["id"]
    r = await box.post(
        "/zip_downloads",
        json={
            "items": [
                {"type": "folder", "id": folder},
                {"type": "file", "id": loose},
                {"type": "file", "id": twin},
            ],
            "download_file_name": "bundle",
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["download_url"].startswith("https://dl.boxcloud.com/2.0/zip_downloads/")
    assert body["status_url"].startswith("https://api.box.com/2.0/zip_downloads/") and body["expires_at"]
    # Box renames *both* items that share a name and reports them together (a multiple of two entries).
    (conflict,) = body["name_conflicts"]
    assert [(c["id"], c["original_name"], c["download_name"]) for c in conflict] == [
        (loose, "a.txt", "a (1).txt"),
        (twin, "a.txt", "a (2).txt"),
    ]
    status = (await box.client.get(body["status_url"], headers=box.auth())).json()
    assert status["state"] == "succeeded" and status["total_file_count"] == 4
    archive = await box.client.get(body["download_url"])
    assert archive.status_code == 200 and archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        assert sorted(z.namelist()) == ["Bundle/Inner/b.txt", "Bundle/a.txt", "a (1).txt", "a (2).txt"]
        assert z.read("Bundle/a.txt") == b"alpha" and z.read("a (1).txt") == b"loose alpha"
        assert z.read("a (2).txt") == b"twin alpha"
    assert (await box.post("/zip_downloads", json={"items": []})).status_code == 400
    denied = await box.post("/zip_downloads", "p044", json={"items": [{"type": "file", "id": loose}]})
    assert denied.status_code == 404
    assert (await box.client.get("https://dl.boxcloud.com/2.0/zip_downloads/nope/content")).status_code == 404


# -- trash ---------------------------------------------------------------------------------------


async def test_trash_listing_is_per_owner_and_paginated(box):
    parent = await box.find("Technology")
    ids = []
    for n in range(3):
        fid = (await box.upload(parent, f"t{n}.txt", b"x")).json()["entries"][0]["id"]
        ids.append(fid)
        await box.delete(f"/files/{fid}")
    mine = (
        await box.get("/folders/trash/items", params={"fields": "name", "sort": "name", "direction": "ASC"})
    ).json()
    assert [e["name"] for e in mine["entries"]] == ["t0.txt", "t1.txt", "t2.txt"]
    assert mine["total_count"] == 3 and mine["order"] == [{"by": "name", "direction": "ASC"}]
    page = (await box.get("/folders/trash/items", params={"limit": 2, "usemarker": "true"})).json()
    assert len(page["entries"]) == 2 and page["next_marker"]
    assert (await box.get("/folders/trash/items", "p044")).json()["entries"] == []
    # An editor who deletes someone else's file sends it to the owner's trash, not their own.
    nw = await box.find("Client Engagements/Northwind Logistics/Correspondence", "p004")
    fid = (await box.upload(nw, "edited.txt", b"x", "p004")).json()["entries"][0]["id"]
    await box.delete(f"/files/{fid}", "p004")
    assert fid not in [e["id"] for e in (await box.get("/folders/trash/items", "p004")).json()["entries"]]
    assert fid in [e["id"] for e in (await box.get("/folders/trash/items", "p035")).json()["entries"]]
