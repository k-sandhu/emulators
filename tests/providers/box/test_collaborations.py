"""Collaborations, invitations and shared links."""

from __future__ import annotations

import re
from datetime import timedelta

NW = "Client Engagements/Northwind Logistics"


async def sandbox(box, name: str = "Sandbox") -> str:
    return await box.mkdir(await box.find("Technology"), name)


async def invite(box, folder: str, who: str, role: str, **kw):
    user = (await box.get("/users/me", who)).json()
    return await box.post(
        "/collaborations",
        json={
            "item": {"type": "folder", "id": folder},
            "accessible_by": {"type": "user", "id": user["id"]},
            "role": role,
            **kw,
        },
    )


async def test_create_collaboration_shape_and_effect(box):
    folder = await sandbox(box)
    r = await invite(box, folder, "p044", "editor")
    assert r.status_code == 201, r.text
    collab = r.json()
    assert collab["type"] == "collaboration" and collab["role"] == "editor" and collab["status"] == "accepted"
    assert collab["accessible_by"]["login"] == "rebecca.stein@halcyonadvisory.example"
    assert collab["item"] == {
        "type": "folder",
        "id": folder,
        "sequence_id": "0",
        "etag": "0",
        "name": "Sandbox",
    }
    assert collab["created_by"]["name"] == "Arjun Mehta"
    assert collab["expires_at"] is None and collab["invite_email"] is None
    assert re.match(r"\d{4}-\d{2}-\d{2}T", collab["created_at"]) and collab["acknowledged_at"]
    assert "Sandbox" in {e["name"] for e in await box.items("0", "p044")}
    # The new collaborator now writes into the folder.
    assert (await box.upload(folder, "from-rebecca.txt", b"x", "p044")).status_code == 201
    listed = (await box.get(f"/folders/{folder}/collaborations")).json()
    assert listed["total_count"] == 1 and listed["entries"][0]["id"] == collab["id"]
    assert listed["limit"] == 100 and listed["next_marker"] is None
    dup = await invite(box, folder, "p044", "viewer")
    assert dup.status_code == 400 and dup.json()["code"] == "user_already_collaborator"


async def test_each_role_grants_its_documented_rights(box):
    folder = await sandbox(box)
    fid = (await box.upload(folder, "doc.txt", b"data")).json()["entries"][0]["id"]
    roles = {
        "p019": "editor",
        "p020": "viewer",
        "p021": "previewer",
        "p022": "uploader",
        "p023": "previewer uploader",
        "p024": "viewer uploader",
        "p001": "co-owner",
    }
    for who, role in roles.items():
        assert (await invite(box, folder, who, role)).status_code == 201, role
    can_read = {"p019", "p020", "p021", "p023", "p024", "p001"}
    can_download = {"p019", "p020", "p024", "p001"}
    can_upload = {"p019", "p022", "p023", "p024", "p001"}
    can_delete = {"p019", "p001"}
    for who in roles:
        assert ((await box.get(f"/files/{fid}", who)).status_code == 200) == (who in can_read), who
        assert ((await box.get(f"/files/{fid}/content", who)).status_code == 302) == (who in can_download), (
            who
        )
        upload = await box.upload(folder, f"{who}.txt", b"x", who)
        assert (upload.status_code == 201) == (who in can_upload), who
        assert ((await box.get(f"/folders/{folder}/items", who)).status_code == 200) == (who in can_read), who
    for who in roles:
        r = await box.put(f"/files/{fid}", who, json={"description": f"edited by {who}"})
        assert (r.status_code == 200) == (who in can_delete), who
    assert (await box.delete(f"/files/{fid}", "p020")).status_code == 403
    assert (await box.delete(f"/files/{fid}", "p019")).status_code == 204


async def test_only_those_who_may_invite_can_invite_and_editors_cannot_make_co_owners(box):
    folder = await sandbox(box)
    await invite(box, folder, "p019", "editor")
    await invite(box, folder, "p020", "viewer")
    ok = await box.post(
        "/collaborations",
        "p019",
        json={
            "item": {"type": "folder", "id": folder},
            "accessible_by": {"type": "user", "id": (await box.get("/users/me", "p021")).json()["id"]},
            "role": "viewer",
        },
    )
    assert ok.status_code == 201
    co_owner = await box.post(
        "/collaborations",
        "p019",
        json={
            "item": {"type": "folder", "id": folder},
            "accessible_by": {"type": "user", "id": (await box.get("/users/me", "p022")).json()["id"]},
            "role": "co-owner",
        },
    )
    assert co_owner.status_code == 403
    viewer = await box.post(
        "/collaborations",
        "p020",
        json={
            "item": {"type": "folder", "id": folder},
            "accessible_by": {"type": "user", "id": (await box.get("/users/me", "p022")).json()["id"]},
            "role": "viewer",
        },
    )
    assert viewer.status_code == 403
    stranger = await box.post(
        "/collaborations",
        "p044",
        json={
            "item": {"type": "folder", "id": folder},
            "accessible_by": {"type": "user", "id": (await box.get("/users/me", "p022")).json()["id"]},
            "role": "viewer",
        },
    )
    assert stranger.status_code == 404


async def test_collaboration_validation(box):
    folder = await sandbox(box)
    user = (await box.get("/users/me", "p044")).json()
    base = {
        "item": {"type": "folder", "id": folder},
        "accessible_by": {"type": "user", "id": user["id"]},
        "role": "editor",
    }
    cases = (
        ({**base, "role": "owner"}, 400),
        ({**base, "role": "wizard"}, 400),
        ({**base, "item": {"type": "folder", "id": "0"}}, 400),
        ({**base, "accessible_by": {"type": "user", "id": "123"}}, 404),
        ({**base, "accessible_by": {"type": "group", "id": "123"}}, 404),
        ({**base, "item": {"type": "file", "id": "123"}}, 404),
        ({**base, "expires_at": "2020-01-01T00:00:00Z"}, 400),
    )
    for payload, expected in cases:
        r = await box.post("/collaborations", json=payload)
        assert r.status_code == expected, (payload, r.text)
    for missing in ("item", "accessible_by", "role"):
        r = await box.post("/collaborations", json={k: v for k, v in base.items() if k != missing})
        assert r.status_code == 400, missing
    owner = (await box.get("/users/me", "p035")).json()
    to_owner = await box.post(
        "/collaborations", json={**base, "accessible_by": {"type": "user", "id": owner["id"]}}
    )
    assert to_owner.status_code == 400
    file_id = (await box.upload(folder, "f.txt", b"x")).json()["entries"][0]["id"]
    uploader_on_file = await box.post(
        "/collaborations", json={**base, "item": {"type": "file", "id": file_id}, "role": "uploader"}
    )
    assert uploader_on_file.status_code == 400
    on_file = await box.post(
        "/collaborations", json={**base, "item": {"type": "file", "id": file_id}, "role": "viewer"}
    )
    assert on_file.status_code == 201 and on_file.json()["item"]["type"] == "file"
    assert (await box.get(f"/files/{file_id}/collaborations")).json()["total_count"] == 1


async def test_invite_by_login_creates_pending_external_invitations(box):
    folder = await sandbox(box)
    r = await box.post(
        "/collaborations",
        json={
            "item": {"type": "folder", "id": folder},
            "accessible_by": {"type": "user", "login": "new.advisor@outside.example"},
            "role": "viewer",
            "expires_at": (box.hub.clock.now() + timedelta(days=30)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    collab = r.json()
    assert collab["status"] == "pending" and collab["invite_email"] == "new.advisor@outside.example"
    assert collab["accessible_by"]["login"] == "new.advisor@outside.example"
    assert collab["expires_at"]
    # The invitee exists as an external Box user and sees the invite until it is accepted.
    token = (await box.action("mint_token", {"user": "new.advisor@outside.example"}))["access_token"]
    assert (await box.get(f"/folders/{folder}", token)).status_code == 404
    pending = (await box.get("/collaborations", token, params={"status": "pending"})).json()
    assert [e["id"] for e in pending["entries"]] == [collab["id"]]
    await box.put(f"/collaborations/{collab['id']}", token, json={"status": "accepted"})
    assert (await box.get(f"/folders/{folder}", token)).status_code == 200
    # An existing managed colleague invited by e-mail is accepted straight away.
    known = await box.post(
        "/collaborations",
        json={
            "item": {"type": "folder", "id": folder},
            "accessible_by": {"type": "user", "login": "maya.chen@halcyonadvisory.example"},
            "role": "editor",
        },
    )
    assert known.json()["status"] == "accepted"
    assert (await box.get("/collaborations", params={})).status_code == 400
    assert (await box.get("/collaborations", params={"status": "accepted"})).status_code == 400


async def test_update_collaboration_role_status_and_removal(box):
    folder = await sandbox(box)
    collab = (await invite(box, folder, "p044", "viewer")).json()
    assert (await box.upload(folder, "x.txt", b"x", "p044")).status_code == 403
    r = await box.put(f"/collaborations/{collab['id']}", json={"role": "editor"})
    assert r.status_code == 200 and r.json()["role"] == "editor"
    assert (await box.upload(folder, "x.txt", b"x", "p044")).status_code == 201
    fetched = await box.get(f"/collaborations/{collab['id']}", "p044")
    assert fetched.status_code == 200 and fetched.json()["role"] == "editor"
    assert (
        await box.put(f"/collaborations/{collab['id']}", "p044", json={"role": "co-owner"})
    ).status_code == 403
    assert (await box.put(f"/collaborations/{collab['id']}", json={"role": "owner"})).status_code == 400
    assert (await box.put(f"/collaborations/{collab['id']}", json={"role": "bogus"})).status_code == 400
    # Collaborators can walk away; everyone else needs rights on the folder.
    assert (await box.delete(f"/collaborations/{collab['id']}", "p020")).status_code == 404
    assert (await box.delete(f"/collaborations/{collab['id']}", "p044")).status_code == 204
    assert (await box.get(f"/folders/{folder}", "p044")).status_code == 404
    assert (await box.get(f"/collaborations/{collab['id']}")).status_code == 404
    other = (await invite(box, folder, "p045", "viewer")).json()
    rejected = await box.put(f"/collaborations/{other['id']}", "p045", json={"status": "rejected"})
    assert rejected.status_code == 204
    assert (await box.get(f"/collaborations/{other['id']}")).status_code == 404


async def test_group_collaboration_gives_every_member_access(box):
    groups = (await box.get("/groups", params={"limit": 1000})).json()["entries"]
    growth = next(g for g in groups if g["name"].startswith("Business Development"))
    folder = await sandbox(box, "For growth")
    r = await box.post(
        "/collaborations",
        json={
            "item": {"type": "folder", "id": folder},
            "accessible_by": {"type": "group", "id": growth["id"]},
            "role": "viewer",
        },
    )
    assert r.status_code == 201
    assert r.json()["accessible_by"] == {
        "type": "group", "id": growth["id"], "name": growth["name"], "group_type": "managed_group",
    }  # fmt: skip
    for who in ("p040", "p044", "p045"):
        assert (await box.get(f"/folders/{folder}", who)).status_code == 200, who
    assert (await box.get(f"/folders/{folder}", "p019")).status_code == 404


async def test_seeded_team_folder_collaborations(box):
    clients = await box.find("Client Engagements")
    collabs = (await box.get(f"/folders/{clients}/collaborations")).json()
    by_group = {
        e["accessible_by"]["name"]: e["role"]
        for e in collabs["entries"]
        if e["accessible_by"]["type"] == "group"
    }
    assert by_group == {
        "Partners": "co-owner",
        "Audit & Assurance": "editor",
        "Tax": "editor",
        "Advisory": "editor",
        "Finance & Operations": "viewer",
    }
    assert all(e["status"] == "accepted" for e in collabs["entries"])
    users = [e for e in collabs["entries"] if e["accessible_by"]["type"] == "user"]
    assert any(e["accessible_by"]["name"] == "Eleanor Whitfield" and e["role"] == "co-owner" for e in users)


async def test_client_contact_shares_become_file_collaborations(box):
    clients = await box.find(NW + "/FY2025 Review/Requests")
    entry = next(e for e in await box.items(clients) if e["name"] == "PBC Request List.xlsx")
    collabs = (await box.get(f"/files/{entry['id']}/collaborations")).json()["entries"]
    assert len(collabs) == 1
    assert collabs[0]["accessible_by"]["login"] == "christopher.ingram@northwindlogistics.example"
    assert collabs[0]["role"] == "editor" and collabs[0]["item"]["type"] == "file"
    assert collabs[0]["status"] == "accepted"
    # Expiring shares keep their expiry.
    letters = [
        c
        for e in await box.items(await box.find(NW + "/FY2025 Review/Deliverables"))
        for c in (await box.get(f"/files/{e['id']}/collaborations")).json()["entries"]
    ]
    assert any(c["expires_at"] for c in letters)


async def test_shared_link_lifecycle(box):
    folder = await sandbox(box)
    fid = (await box.upload(folder, "report.pdf", b"%PDF-1.4 demo")).json()["entries"][0]["id"]
    assert (await box.get(f"/files/{fid}", params={"fields": "shared_link"})).json()["shared_link"] is None
    created = await box.put(
        f"/files/{fid}",
        params={"fields": "shared_link"},
        json={
            "shared_link": {"access": "open", "password": "s3cret-pw", "permissions": {"can_download": True}}
        },
    )
    link = created.json()["shared_link"]
    assert re.fullmatch(r"https://app\.box\.com/s/[0-9a-f]{32}", link["url"])
    assert link["download_url"].startswith("https://app.box.com/shared/static/") and link[
        "download_url"
    ].endswith(".pdf")
    assert link["access"] == "open" and link["effective_access"] == "open"
    assert link["is_password_enabled"] is True and link["permissions"]["can_download"] is True
    assert link["effective_permission"] == "can_download" and link["download_count"] == 0
    # Anyone with the link and the password can resolve it; no token needed for an open link.
    header = {"BoxApi": f"shared_link={link['url']}&shared_link_password=s3cret-pw"}
    shared = await box.client.get("https://api.box.com/2.0/shared_items", headers=header)
    assert shared.status_code == 200 and shared.json()["id"] == fid
    wrong = await box.client.get(
        "https://api.box.com/2.0/shared_items",
        headers={"BoxApi": f"shared_link={link['url']}&shared_link_password=nope"},
    )
    assert wrong.status_code == 403
    missing_pw = await box.client.get(
        "https://api.box.com/2.0/shared_items", headers={"BoxApi": f"shared_link={link['url']}"}
    )
    assert missing_pw.status_code == 403
    assert (await box.client.get("https://api.box.com/2.0/shared_items")).status_code == 401
    # With a token, the same header grants read access to the item and its content.
    viaoutsider = await box.get(f"/files/{fid}", "p044", headers=header)
    assert viaoutsider.status_code == 200
    content = await box.get(f"/files/{fid}/content", "p044", headers=header)
    assert content.status_code == 302
    assert (await box.get(f"/files/{fid}", "p044")).status_code == 404
    updated = (await box.get(f"/files/{fid}", params={"fields": "shared_link"})).json()["shared_link"]
    assert updated["download_count"] == 1
    # Updating keeps the URL; removing makes it dead.
    changed = await box.put(
        f"/files/{fid}",
        params={"fields": "shared_link"},
        json={"shared_link": {"password": None, "access": "company"}},
    )
    assert (
        changed.json()["shared_link"]["url"] == link["url"]
        and changed.json()["shared_link"]["access"] == "company"
    )
    assert changed.json()["shared_link"]["is_password_enabled"] is False
    removed = await box.put(f"/files/{fid}", params={"fields": "shared_link"}, json={"shared_link": None})
    assert removed.json()["shared_link"] is None
    gone = await box.client.get(
        "https://api.box.com/2.0/shared_items", headers={"BoxApi": f"shared_link={link['url']}"}
    )
    assert gone.status_code == 404


async def test_shared_link_access_levels_and_expiry(box):
    folder = await sandbox(box)
    fid = (await box.upload(folder, "memo.txt", b"memo")).json()["entries"][0]["id"]
    company = (
        await box.put(
            f"/files/{fid}", params={"fields": "shared_link"}, json={"shared_link": {"access": "company"}}
        )
    ).json()["shared_link"]
    header = {"BoxApi": f"shared_link={company['url']}"}
    assert (await box.get("/shared_items", "p044", headers=header)).status_code == 200  # a colleague
    external = (await box.externals())[0]
    ext_token = (await box.action("mint_token", {"user": external["id"]}))["access_token"]
    assert (await box.get("/shared_items", ext_token, headers=header)).status_code == 404
    assert (await box.client.get("https://api.box.com/2.0/shared_items", headers=header)).status_code == 401
    collab_only = (
        await box.put(
            f"/files/{fid}",
            params={"fields": "shared_link"},
            json={"shared_link": {"access": "collaborators"}},
        )
    ).json()["shared_link"]
    assert (
        await box.get("/shared_items", "p044", headers={"BoxApi": f"shared_link={collab_only['url']}"})
    ).status_code == 404
    assert (
        await box.get("/shared_items", "p035", headers={"BoxApi": f"shared_link={collab_only['url']}"})
    ).status_code == 200
    expiring = (
        await box.put(
            f"/files/{fid}",
            params={"fields": "shared_link"},
            json={
                "shared_link": {
                    "access": "open",
                    "unshared_at": (box.hub.clock.now() + timedelta(days=1)).isoformat(),
                }
            },
        )
    ).json()["shared_link"]
    assert expiring["unshared_at"]
    open_header = {"BoxApi": f"shared_link={expiring['url']}"}
    assert (
        await box.client.get("https://api.box.com/2.0/shared_items", headers=open_header)
    ).status_code == 200
    box.hub.clock.advance(timedelta(days=2))
    assert (
        await box.client.get("https://api.box.com/2.0/shared_items", headers=open_header)
    ).status_code == 404


async def test_shared_link_rules(box):
    folder = await sandbox(box)
    fid = (await box.upload(folder, "r.txt", b"r")).json()["entries"][0]["id"]
    for bad in (
        {"access": "everyone"},
        {"access": "company", "password": "longenough1"},
        {"password": "short"},
        {"vanity_name": "tiny"},
        {"permissions": "yes"},
    ):
        r = await box.put(f"/files/{fid}", json={"shared_link": bad})
        assert r.status_code == 400, bad
    # Viewers may not create links of 'open' access; folder links cannot allow editing.
    await invite(box, folder, "p044", "viewer")
    assert (
        await box.put(f"/files/{fid}", "p044", json={"shared_link": {"access": "open"}})
    ).status_code == 403
    r = await box.put(
        f"/folders/{folder}", json={"shared_link": {"access": "open", "permissions": {"can_edit": True}}}
    )
    assert r.status_code == 400
    folder_link = await box.put(
        f"/folders/{folder}", params={"fields": "shared_link"}, json={"shared_link": {"access": "open"}}
    )
    assert folder_link.json()["shared_link"]["download_url"] is None
    items = await box.client.get(
        f"https://api.box.com/2.0/folders/{folder}/items",
        headers={"BoxApi": f"shared_link={folder_link.json()['shared_link']['url']}"},
    )
    assert items.status_code == 200 and [e["name"] for e in items.json()["entries"]] == ["r.txt"]


async def test_static_download_and_landing_page_for_open_links(box):
    folder = await sandbox(box)
    fid = (await box.upload(folder, "public.txt", b"public text")).json()["entries"][0]["id"]
    link = (
        await box.put(
            f"/files/{fid}", params={"fields": "shared_link"}, json={"shared_link": {"access": "open"}}
        )
    ).json()["shared_link"]
    landing = await box.client.get(link["url"])
    assert landing.status_code == 200 and "public.txt" in landing.text
    download = await box.client.get(link["download_url"])
    assert download.status_code == 200 and download.content == b"public text"
    await box.put(f"/files/{fid}", json={"shared_link": {"permissions": {"can_download": False}}})
    assert (await box.client.get(link["download_url"])).status_code == 404
    assert (await box.client.get("https://app.box.com/s/" + "0" * 32)).status_code == 404


async def test_departed_users_cannot_be_invited_and_the_invite_endpoint(box):
    folder = await sandbox(box, "No departed")
    departed = next(
        u
        for u in (await box.get("/users", params={"limit": 1000})).json()["entries"]
        if u["status"] == "inactive"
    )
    r = await box.post(
        "/collaborations",
        json={
            "item": {"type": "folder", "id": folder},
            "accessible_by": {"type": "user", "id": departed["id"]},
            "role": "viewer",
        },
    )
    assert r.status_code == 400
    invite = await box.post(
        "/invites",
        json={"enterprise": {"id": "336533359"}, "actionable_by": {"login": "new.hire@example.test"}},
    )
    assert invite.status_code == 201, invite.text
    body = invite.json()
    assert body["type"] == "invite" and body["status"] == "pending"
    assert (
        body["invited_to"]["id"] == "336533359" and body["actionable_by"]["login"] == "new.hire@example.test"
    )
    assert (await box.get(f"/invites/{body['id']}")).json()["id"] == body["id"]
    assert (
        await box.post(
            "/invites",
            "p018",
            json={"enterprise": {"id": "336533359"}, "actionable_by": {"login": "x@y.test"}},
        )
    ).status_code == 403
    maya = {"login": "maya.chen@halcyonadvisory.example"}
    assert (
        await box.post("/invites", json={"enterprise": {"id": "336533359"}, "actionable_by": maya})
    ).status_code == 409
    assert (
        await box.post("/invites", json={"enterprise": {"id": "1"}, "actionable_by": maya})
    ).status_code == 404
