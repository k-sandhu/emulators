"""Enterprise users, groups and memberships."""

from __future__ import annotations

import re

ENTERPRISE = {"type": "enterprise", "id": "336533359", "name": "Halcyon Advisory Group"}


async def test_users_me_has_the_full_user_shape(box):
    me = (await box.get("/users/me", "p035")).json()
    assert me["type"] == "user"
    assert re.fullmatch(r"\d+", me["id"])
    assert me["name"] == "Arjun Mehta"
    assert me["login"] == "arjun.mehta@halcyonadvisory.example"
    assert me["role"] == "admin"
    assert me["status"] == "active"
    assert me["enterprise"] == ENTERPRISE
    assert me["job_title"] == "Director of Technology"
    assert me["avatar_url"].startswith("https://app.box.com/api/avatar/large/")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-0[78]:00", me["created_at"])
    for key in (
        "space_amount",
        "space_used",
        "max_upload_size",
        "language",
        "timezone",
        "notification_email",
    ):
        assert key in me
    assert me["space_used"] > 0


async def test_user_list_is_admin_only_with_filters_and_pagination(box):
    denied = await box.get("/users", "p018")
    assert denied.status_code == 403
    everyone = (await box.get("/users", params={"limit": 1000})).json()
    assert everyone["total_count"] == len(everyone["entries"]) > 45
    assert everyone["order"] == [{"by": "name", "direction": "ASC"}]
    managed = (await box.get("/users", params={"user_type": "managed", "limit": 1000})).json()
    external = (await box.get("/users", params={"user_type": "external", "limit": 1000})).json()
    assert all(e["login"].endswith("@halcyonadvisory.example") for e in managed["entries"])
    assert external["total_count"] == 22
    assert all("@halcyonadvisory.example" not in e["login"] for e in external["entries"])
    found = (await box.get("/users", params={"filter_term": "maya"})).json()
    assert [e["name"] for e in found["entries"]] == ["Maya Chen"]
    first = (await box.get("/users", params={"limit": 5, "usemarker": "true"})).json()
    assert first["next_marker"] and len(first["entries"]) == 5 and "total_count" not in first
    second = (
        await box.get("/users", params={"limit": 5, "usemarker": "true", "marker": first["next_marker"]})
    ).json()
    assert {e["id"] for e in second["entries"]}.isdisjoint({e["id"] for e in first["entries"]})
    paged = (await box.get("/users", params={"limit": 3, "offset": 3})).json()
    assert paged["offset"] == 3 and paged["limit"] == 3
    assert (await box.get("/users", params={"limit": 5000})).status_code == 400


async def test_departed_employees_are_inactive_and_cannot_authenticate(box):
    everyone = (await box.get("/users", params={"limit": 1000})).json()["entries"]
    inactive = [u for u in everyone if u["status"] == "inactive"]
    assert {u["name"] for u in inactive} == {"Paul Henderson", "Yuki Tanaka"}
    assert (await box.get("/users/me", "p046")).status_code == 401


async def test_get_user_visibility(box):
    maya = (await box.get("/users/me", "p018")).json()
    other = (await box.get("/users/me", "p019")).json()
    assert (await box.get(f"/users/{maya['id']}", "p019")).status_code == 200
    externals = (await box.get("/users", params={"user_type": "external"})).json()["entries"]
    seen_by_p019 = {(await box.get(f"/users/{e['id']}", "p019")).status_code for e in externals}
    assert 403 in seen_by_p019  # outsiders are visible only to the people who invited them
    assert {(await box.get(f"/users/{e['id']}", "p035")).status_code for e in externals} == {200}
    assert (await box.get(f"/users/{other['id']}", "p035", params={"fields": "name,role"})).json() == {
        "type": "user",
        "id": other["id"],
        "name": other["name"],
        "login": other["login"],
        "role": "user",
    }
    assert (await box.get("/users/1", "p035")).status_code == 404


async def test_admin_creates_updates_and_deletes_users(box):
    created = await box.post(
        "/users", json={"name": "Temp Contractor", "login": "temp.contractor@halcyonadvisory.example"}
    )
    assert created.status_code == 201
    user = created.json()
    assert user["status"] == "active" and user["role"] == "user"
    assert (await box.post("/users", json={"name": "Dup", "login": user["login"]})).status_code == 409
    assert (
        await box.post("/users", "p018", json={"name": "X", "login": "x@example.test"})
    ).status_code == 403
    assert (await box.post("/users", json={"login": "y@example.test"})).json()["code"] == "bad_request"
    updated = await box.put(f"/users/{user['id']}", json={"job_title": "Contractor", "role": "coadmin"})
    assert updated.status_code == 200
    assert updated.json()["job_title"] == "Contractor" and updated.json()["role"] == "coadmin"
    assert (await box.put(f"/users/{user['id']}", "p036", json={"role": "admin"})).status_code == 403
    maya = (await box.get("/users/me", "p018")).json()
    own = await box.put(f"/users/{maya['id']}", "p018", json={"phone": "+1 555 0100"})
    assert own.status_code == 200 and own.json()["phone"] == "+1 555 0100"
    escalate = await box.put(f"/users/{maya['id']}", "p018", json={"role": "admin"})
    assert escalate.status_code == 403
    assert (await box.delete(f"/users/{user['id']}")).status_code == 204
    assert (await box.get(f"/users/{user['id']}")).status_code == 404
    # A user who owns content cannot be removed without force.
    await box.upload(await box.find("Notes", "p018"), "keep.txt", b"x", "p018")
    assert (await box.delete(f"/users/{maya['id']}")).status_code == 400
    assert (await box.delete(f"/users/{maya['id']}", params={"force": "true"})).status_code == 204


async def test_deactivating_a_user_revokes_tokens(box):
    maya = (await box.get("/users/me", "p018")).json()
    await box.put(f"/users/{maya['id']}", json={"status": "inactive"})
    assert (await box.get("/users/me", "p018")).status_code == 401


async def test_user_avatar_is_a_png(box):
    me = (await box.get("/users/me", "p018")).json()
    avatar = await box.get(f"/users/{me['id']}/avatar", "p018")
    assert avatar.status_code == 200
    assert avatar.headers["content-type"] == "image/png"
    assert avatar.content.startswith(b"\x89PNG")
    public = await box.client.get(me["avatar_url"])
    assert public.status_code == 200 and public.content.startswith(b"\x89PNG")


async def test_groups_follow_the_org_structure(box):
    groups = (await box.get("/groups", params={"limit": 1000})).json()
    names = {g["name"] for g in groups["entries"]}
    assert {"All Staff", "Partners", "Audit & Assurance", "Tax", "Finance & Operations"} <= names
    assert all(g["group_type"] == "managed_group" for g in groups["entries"])
    assert (await box.get("/groups", "p018")).status_code == 403
    partners = next(g for g in groups["entries"] if g["name"] == "Partners")
    detail = (await box.get(f"/groups/{partners['id']}")).json()
    assert detail["invitability_level"] in ("admins_only", "admins_and_members", "all_managed_users")
    assert detail["member_viewability_level"]
    members = (await box.get(f"/groups/{partners['id']}/memberships")).json()
    assert members["total_count"] == 4
    entry = members["entries"][0]
    assert entry["type"] == "group_membership" and entry["group"]["id"] == partners["id"]
    assert entry["user"]["login"].endswith("@halcyonadvisory.example")
    assert entry["role"] in ("member", "admin")
    # Members may read their own group; outsiders may not.
    assert (await box.get(f"/groups/{partners['id']}", "p001")).status_code == 200
    assert (await box.get(f"/groups/{partners['id']}", "p019")).status_code == 403
    maya = (await box.get("/users/me", "p018")).json()
    memberships = (await box.get(f"/users/{maya['id']}/memberships")).json()
    assert {m["group"]["name"] for m in memberships["entries"]} >= {"All Staff", "Partners", "Advisory"}


async def test_group_crud_and_membership_changes(box):
    created = await box.post("/groups", json={"name": "Outside Counsel", "description": "External lawyers"})
    assert created.status_code == 201
    group = created.json()
    assert group["description"] == "External lawyers"
    assert (await box.post("/groups", json={"name": "outside counsel"})).status_code == 409
    assert (await box.post("/groups", json={"name": "Z", "invitability_level": "nobody"})).status_code == 400
    maya = (await box.get("/users/me", "p018")).json()
    added = await box.post(
        "/group_memberships", json={"user": {"id": maya["id"]}, "group": {"id": group["id"]}, "role": "admin"}
    )
    assert added.status_code == 201
    membership = added.json()
    assert membership["role"] == "admin" and membership["user"]["name"] == "Maya Chen"
    duplicate = await box.post(
        "/group_memberships", json={"user": {"id": maya["id"]}, "group": {"id": group["id"]}}
    )
    assert duplicate.status_code == 409
    renamed = await box.put(f"/groups/{group['id']}", json={"name": "Outside Legal"})
    assert renamed.json()["name"] == "Outside Legal"
    changed = await box.put(f"/group_memberships/{membership['id']}", json={"role": "member"})
    assert changed.json()["role"] == "member"
    assert (await box.get(f"/group_memberships/{membership['id']}", "p018")).status_code == 200
    assert (await box.delete(f"/group_memberships/{membership['id']}")).status_code == 204
    assert (await box.get(f"/groups/{group['id']}/memberships")).json()["total_count"] == 0
    assert (await box.delete(f"/groups/{group['id']}")).status_code == 204
    assert (await box.get(f"/groups/{group['id']}")).status_code == 404


async def test_group_collaboration_listing(box):
    groups = (await box.get("/groups", params={"limit": 1000})).json()["entries"]
    audit = next(g for g in groups if g["name"] == "Audit & Assurance")
    collabs = (await box.get(f"/groups/{audit['id']}/collaborations")).json()
    assert collabs["total_count"] >= 1
    assert collabs["entries"][0]["accessible_by"]["type"] == "group"
    assert collabs["entries"][0]["item"]["name"] == "Client Engagements"
