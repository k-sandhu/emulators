"""Per-user visibility and role enforcement: ownership, group collaborations, restricted files."""

from __future__ import annotations

from datetime import timedelta

NORTHWIND_DELIVERABLES = "Client Engagements/Northwind Logistics/FY2025 Review/Deliverables"


async def root_names(box, who: str) -> set[str]:
    return {e["name"] for e in await box.items("0", who)}


async def test_root_listing_is_the_users_own_tree_plus_shared_folders(box):
    # Growth staff: own folders, their team space, and the firm-wide viewable spaces - nothing else.
    names = await root_names(box, "p044")
    assert {"Marketing & BD", "Knowledge Base", "People & Culture"} <= names
    for hidden in ("Client Engagements", "Finance & Operations", "Partner Group", "Technology"):
        assert hidden not in names
    # Partners additionally see the confidential partner space and the client files.
    partner = await root_names(box, "p004")
    assert {"Partner Group", "Client Engagements", "Marketing & BD", "Knowledge Base"} <= partner
    assert {"Technology", "Finance & Operations"} <= partner  # the leadership group may read IT and Finance
    assert "People & Culture" in partner
    # The admin owns every team space but does not see other people's personal folders.
    admin = await root_names(box, "p035")
    assert "Client Engagements" in admin and "Technology" in admin


async def test_items_without_access_are_hidden_as_not_found(box):
    partner_group = await box.find("Partner Group", "p004")
    for path in (
        f"/folders/{partner_group}",
        f"/folders/{partner_group}/items",
        f"/folders/{partner_group}/collaborations",
    ):
        r = await box.get(path, "p044")
        assert r.status_code == 404, path
        assert r.json()["code"] == "not_found"
    clients = await box.find("Client Engagements", "p004")
    nw = await box.find("Client Engagements/Northwind Logistics/FY2025 Review/Deliverables", "p004")
    file_id = (await box.items(nw, "p004"))[0]["id"]
    assert (await box.get(f"/files/{file_id}", "p044")).status_code == 404
    assert (await box.get(f"/files/{file_id}/content", "p044")).status_code == 404
    assert (await box.get(f"/files/{file_id}/versions", "p044")).status_code == 404
    assert (await box.get(f"/folders/{clients}", "p044")).status_code == 404
    # ... and writes against hidden items do not reveal them either.
    assert (await box.put(f"/files/{file_id}", "p044", json={"name": "x"})).status_code == 404
    assert (await box.delete(f"/files/{file_id}", "p044")).status_code == 404


async def test_viewers_can_read_but_not_write(box):
    # Finance staff are viewers on the client files (space member g-finance -> viewer).
    folder = await box.find(NORTHWIND_DELIVERABLES, "p030")
    entry = (await box.items(folder, "p030"))[0]
    assert (await box.get(f"/files/{entry['id']}", "p030")).status_code == 200
    assert (await box.get(f"/files/{entry['id']}/content", "p030")).status_code == 302
    perms = (await box.get(f"/files/{entry['id']}", "p030", params={"fields": "permissions"})).json()[
        "permissions"
    ]
    assert perms["can_download"] is True and perms["can_preview"] is True
    assert perms["can_rename"] is False and perms["can_delete"] is False and perms["can_upload"] is False
    forbidden = await box.put(f"/files/{entry['id']}", "p030", json={"description": "nope"})
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "access_denied_insufficient_permissions"
    assert (await box.delete(f"/files/{entry['id']}", "p030")).status_code == 403
    assert (
        await box.post("/folders", "p030", json={"name": "N", "parent": {"id": folder}})
    ).status_code == 403
    assert (await box.upload(folder, "n.txt", b"x", "p030")).status_code == 403
    assert (
        await box.post(f"/files/{entry['id']}/copy", "p030", json={"parent": {"id": folder}, "name": "c"})
    ).status_code == 403
    dest = await box.find(
        "Finance & Operations", "p030"
    )  # editor there: copying out of the viewer tree is fine
    copied = await box.post(
        f"/files/{entry['id']}/copy", "p030", json={"parent": {"id": dest}, "name": "Copy.docx"}
    )
    assert copied.status_code == 201
    assert (
        await box.put(f"/files/{copied.json()['id']}", "p030", json={"name": "Mine.docx"})
    ).status_code == 200
    # An editor on the same folder (audit staff) can write.
    assert (
        await box.put(f"/files/{entry['id']}", "p005", json={"description": "reviewed"})
    ).status_code == 200


async def test_trash_and_move_need_edit_rights_on_both_sides(box):
    nw = await box.find("Client Engagements/Northwind Logistics/Correspondence", "p008")
    fid = (await box.upload(nw, "m.txt", b"x", "p008")).json()["entries"][0]["id"]
    kb = await box.find("Knowledge Base", "p008")  # an auditor is only a viewer on the knowledge base
    assert (await box.put(f"/files/{fid}", "p008", json={"parent": {"id": kb}})).status_code == 403
    other = await box.mkdir(nw, "Elsewhere", "p008")
    assert (await box.put(f"/files/{fid}", "p008", json={"parent": {"id": other}})).status_code == 200


async def test_group_membership_changes_take_effect_immediately(box):
    groups = (await box.get("/groups", params={"limit": 1000})).json()["entries"]
    advisory = next(
        g for g in groups if g["name"] == "Growth" or g["name"].startswith("Business Development")
    )
    clients_group = next(g for g in groups if g["name"] == "Audit & Assurance")
    rebecca = (await box.get("/users/me", "p044")).json()["id"]
    assert "Client Engagements" not in await root_names(box, "p044")
    added = await box.post(
        "/group_memberships", json={"user": {"id": rebecca}, "group": {"id": clients_group["id"]}}
    )
    assert added.status_code == 201
    assert "Client Engagements" in await root_names(box, "p044")
    folder = await box.find(NORTHWIND_DELIVERABLES, "p044")
    assert (await box.upload(folder, "n.txt", b"x", "p044")).status_code == 201  # audit staff are editors
    await box.delete(f"/group_memberships/{added.json()['id']}")
    assert "Client Engagements" not in await root_names(box, "p044")
    assert advisory["id"]


async def test_restricted_documents_live_in_a_private_folder_of_their_owner(box):
    # "Salary Bands 2026.xlsx" is restricted: owned by People's lead, shared only with two partners.
    owner_view = await box.find("Restricted", "p031")
    stack, found = [owner_view], None
    while stack and not found:
        for entry in await box.items(stack.pop(), "p031"):
            if entry["type"] == "folder":
                stack.append(entry["id"])
            elif entry["name"].startswith("Salary Bands"):
                found = entry
    assert found is not None
    # Other people-team members, and even the admin, cannot see it ...
    for who in ("p032", "p035", "p044"):
        assert (await box.get(f"/files/{found['id']}", who)).status_code == 404, who
    assert "Restricted" not in await root_names(box, "p032")
    # ... but the two named people can, with exactly the role they were given.
    for who in ("p001", "p025"):
        r = await box.get(f"/files/{found['id']}", who, params={"fields": "permissions,name"})
        assert r.status_code == 200, who
        assert r.json()["permissions"]["can_rename"] is False
    assert "Restricted" not in await root_names(
        box, "p001"
    )  # shared file surfaces at the root, not the folder
    assert found["name"] in await root_names(box, "p001")


async def test_clients_see_only_files_shared_with_them(box):
    clients = await box.externals()
    ingram = next(u for u in clients if u["login"] == "christopher.ingram@northwindlogistics.example")
    token = (await box.action("mint_token", {"user": ingram["id"]}))["access_token"]
    root = await box.items("0", token)
    assert root and all(e["type"] == "file" for e in root)  # shared files appear at the top level
    assert {e["name"] for e in root} >= {"PBC Request List.xlsx"}
    first = root[0]
    meta = (
        await box.get(f"/files/{first['id']}", token, params={"fields": "path_collection,parent,permissions"})
    ).json()
    assert meta["parent"] is None
    assert [e["name"] for e in meta["path_collection"]["entries"]] == ["All Files"]
    assert (await box.get("/users/me", token)).json()["enterprise"] is None
    siblings = await box.items(await box.find(NORTHWIND_DELIVERABLES), fields="name")
    unshared = next(e for e in siblings if e["id"] not in {r["id"] for r in root})
    assert (await box.get(f"/files/{unshared['id']}", token)).status_code == 404
    assert (await box.get("/users", token)).status_code == 403
    assert (await box.get("/folders/0/items", token)).status_code == 200


async def test_pending_invitations_grant_nothing_until_accepted_and_expiry_revokes_access(box):
    clients = await box.externals()
    pending = None
    for user in clients:
        token = (await box.action("mint_token", {"user": user["id"]}))["access_token"]
        mine = (await box.get("/collaborations", token, params={"status": "pending"})).json()
        if mine["total_count"]:
            pending = (user, token, mine["entries"][0])
            break
    assert pending is not None, "the seed should contain at least one pending client invitation"
    user, token, invite = pending
    item = invite["item"]
    assert invite["status"] == "pending" and invite["invite_email"] == user["login"]
    assert (await box.get(f"/files/{item['id']}", token)).status_code == 404
    accepted = await box.put(f"/collaborations/{invite['id']}", token, json={"status": "accepted"})
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    assert (await box.get(f"/files/{item['id']}", token)).status_code == 200
    # Collaborations can carry an expiry; once passed the access disappears.
    expiry = (box.hub.clock.now() + timedelta(days=2)).isoformat()
    assert (
        await box.put(f"/collaborations/{invite['id']}", "p035", json={"expires_at": expiry})
    ).status_code == 200
    assert (await box.get(f"/files/{item['id']}", token)).status_code == 200
    box.hub.clock.advance(timedelta(days=3))
    token = (await box.action("mint_token", {"user": user["id"]}))["access_token"]
    assert (await box.get(f"/files/{item['id']}", token)).status_code == 404
    assert (await box.get("/folders/0/items", token)).json()["entries"] == [] or item["id"] not in {
        e["id"] for e in await box.items("0", token)
    }


async def test_as_user_applies_the_target_users_permissions(box):
    partner_group = await box.find("Partner Group", "p004")
    growth = (await box.get("/users/me", "p044")).json()["id"]
    r = await box.get(f"/folders/{partner_group}", "p035", headers={"As-User": growth})
    assert r.status_code == 404  # the admin acting as Rebecca cannot see it either
    as_partner = (await box.get("/users/me", "p004")).json()["id"]
    ok = await box.get(f"/folders/{partner_group}", "enterprise", headers={"As-User": as_partner})
    assert ok.status_code == 200
    # Admin-only endpoints follow the *target* user too.
    assert (await box.get("/users", "p035", headers={"As-User": growth})).status_code == 403


async def test_admins_have_no_blanket_access_to_other_peoples_content(box):
    notes = await box.find("Notes", "p018")
    assert (await box.get(f"/folders/{notes}", "p035")).status_code == 404
    assert (await box.get(f"/folders/{notes}", "p036")).status_code == 404
    assert (await box.get(f"/folders/{notes}", "p018")).status_code == 200
    # The enterprise search scope is the one place admins may look across accounts.
    scoped = await box.get(
        "/search",
        "p035",
        params={"query": "notes", "scope": "enterprise_content", "type": "folder", "limit": 200},
    )
    assert scoped.status_code == 200 and notes in [e["id"] for e in scoped.json()["entries"]]
    assert (
        await box.get("/search", "p018", params={"query": "notes", "scope": "enterprise_content"})
    ).status_code == 403
