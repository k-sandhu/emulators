"""The world projected into Box: users, groups, folder tree, sharing, determinism and cost."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from emulators.core.hub import EmulatorHub
from emulators.world.generator import build_world


def fresh_hub() -> EmulatorHub:
    return EmulatorHub(providers=["box"], anchor="2026-10-16", webhook_mode="sync")


def test_seeding_is_fast_and_deterministic():
    start = time.perf_counter()
    first = fresh_hub()
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"box seeding took {elapsed:.2f}s"
    second = fresh_hub()
    one, two = first.context("box").store, second.context("box").store
    for name in ("box_users", "box_folders", "box_files", "box_collaborations", "box_groups", "box_events"):
        left = {r["id"]: r for r in one.collection(name)}
        right = {r["id"]: r for r in two.collection(name)}
        assert left == right, name


async def test_every_employee_is_a_managed_user(box):
    world = build_world("emulators", datetime(2026, 10, 16, tzinfo=UTC))
    users = {u["login"]: u for u in (await box.get("/users", params={"limit": 1000})).json()["entries"]}
    for person in world.employees:
        assert person.email in users, person.email
        user = users[person.email]
        assert user["name"] == person.display_name and user["job_title"] == person.job_title
        assert user["status"] == ("active" if person.active else "inactive")
    roles = {
        u["login"]: (await box.get(f"/users/{u['id']}", params={"fields": "role"})).json()["role"]
        for u in users.values()
        if u["login"].endswith("@halcyonadvisory.example")
    }
    assert {login for login, role in roles.items() if role == "admin"} == {
        "arjun.mehta@halcyonadvisory.example"
    }
    assert {login for login, role in roles.items() if role == "coadmin"} == {
        "sarah.johansson@halcyonadvisory.example"
    }
    service = [u for u in users.values() if u["login"].startswith("AutomationUser_")]
    assert [u["name"] for u in service] == ["Halcyon Box Automation"]
    assert service[0]["login"].endswith("@boxdevedition.com")


async def test_groups_mirror_departments_and_roles(box):
    world = build_world("emulators", datetime(2026, 10, 16, tzinfo=UTC))
    groups = {g["name"]: g for g in (await box.get("/groups", params={"limit": 1000})).json()["entries"]}
    for dept in world.departments:
        assert dept.name in groups, dept.name
    for name in ("All Staff", "Partners", "Leadership Team", "People Managers", "IT Administrators"):
        assert name in groups
    for office in ("Toronto Office", "New York Office", "Chicago Office", "London Office"):
        assert office not in groups  # distribution lists stay out of Box
    all_staff = (
        await box.get(f"/groups/{groups['All Staff']['id']}/memberships", params={"limit": 1000})
    ).json()
    assert all_staff["total_count"] == 45
    owners = [m for m in all_staff["entries"] if m["role"] == "admin"]
    assert {m["user"]["name"] for m in owners} == {"Eleanor Whitfield", "Olivia Grant"}


async def test_team_spaces_are_top_level_admin_folders_with_the_world_acls(box):
    world = build_world("emulators", datetime(2026, 10, 16, tzinfo=UTC))
    root = {e["name"]: e["id"] for e in await box.items("0", "p035")}
    team = [s for s in world.spaces if s.kind == "team"]
    assert len(team) == 7
    for space in team:
        assert space.name in root, space.name
        folder = (await box.get(f"/folders/{root[space.name]}")).json()
        assert folder["owned_by"]["login"] == "arjun.mehta@halcyonadvisory.example"
        assert folder["description"] == space.description
        assert folder["parent"]["id"] == "0"
    finance = (await box.get(f"/folders/{root['Finance & Operations']}/collaborations")).json()["entries"]
    roles = {(c["accessible_by"]["name"], c["role"]) for c in finance}
    assert ("Finance & Operations", "editor") in roles and ("Leadership Team", "viewer") in roles
    assert ("Victoria Hale", "co-owner") in roles  # the space owner is a co-owner
    partner = (await box.get(f"/folders/{root['Partner Group']}/collaborations")).json()["entries"]
    assert {(c["accessible_by"]["name"], c["role"]) for c in partner} >= {
        ("Partners", "co-owner"),
        ("Marcus Okafor", "editor"),
        ("Victoria Hale", "editor"),
    }


async def test_documents_keep_their_folder_hierarchy(box):
    world = build_world("emulators", datetime(2026, 10, 16, tzinfo=UTC))
    document = next(
        d
        for d in world.documents
        if d.space_id == "clients" and d.folder.count("/") >= 2 and not d.restricted
    )
    folder = await box.find(f"Client Engagements/{document.folder}")
    names = [e["name"] for e in await box.items(folder)]
    assert document.name in names or any(n.startswith(document.name.rsplit(".", 1)[0]) for n in names)
    personal = next(d for d in world.documents if d.space_id == "personal-p018")
    owner_items = await box.items("0", "p018")
    assert personal.folder in {e["name"] for e in owner_items} or personal.name in {
        e["name"] for e in owner_items
    }
    # Duplicate names in the world are disambiguated so Box's unique-name rule holds.
    onboarding = await box.find("Client Engagements/Bluefin Seafood/Correspondence")
    names = [e["name"] for e in await box.items(onboarding)]
    assert len(names) == len(set(n.lower() for n in names))
    assert {"Client Onboarding Form.pdf", "Client Onboarding Form (2).pdf"} <= set(names)


async def test_file_sizes_and_hashes_match_the_bytes(box):
    import hashlib

    folder = await box.find("Client Engagements/Northwind Logistics/FY2025 Review/Deliverables")
    for entry in await box.items(folder, fields="sha1,size"):
        location = (await box.get(f"/files/{entry['id']}/content")).headers["location"]
        data = (await box.client.get(location)).content
        assert hashlib.sha1(data).hexdigest() == entry["sha1"] and len(data) == entry["size"]


async def test_shared_links_and_link_shares_from_the_world(box):
    world = build_world("emulators", datetime(2026, 10, 16, tzinfo=UTC))
    linked = [d for d in world.documents if any(s.link != "none" for s in d.shares)]
    assert linked
    document = linked[0]
    folder = await box.find(f"Client Engagements/{document.folder}")
    entry = next(e for e in await box.items(folder, fields="shared_link,name") if e["name"] == document.name)
    link = entry["shared_link"]
    assert link["access"] == "open" and link["url"].startswith("https://app.box.com/s/")
    assert link["permissions"] == {"can_download": True, "can_preview": True, "can_edit": False}
    shared_view = await box.client.get(
        "https://api.box.com/2.0/shared_items", headers={"BoxApi": f"shared_link={link['url']}"}
    )
    assert shared_view.status_code == 200 and shared_view.json()["id"] == entry["id"]


async def test_external_client_contacts(box):
    externals = await box.externals()
    assert len(externals) == 22
    ingram = next(e for e in externals if e["login"] == "christopher.ingram@northwindlogistics.example")
    full = (await box.get(f"/users/{ingram['id']}")).json()
    assert full["enterprise"] is None and full["name"] == "Christopher Ingram"
    pending = accepted = 0
    for user in externals:
        token = (await box.action("mint_token", {"user": user["id"]}))["access_token"]
        mine = (await box.get("/collaborations", token, params={"status": "pending"})).json()
        pending += mine["total_count"]
        accepted += len(await box.items("0", token))
    assert pending >= 1 and accepted >= 20


async def test_restricted_documents_are_isolated_from_their_space(box):
    world = build_world("emulators", datetime(2026, 10, 16, tzinfo=UTC))
    restricted = [d for d in world.documents if d.restricted]
    assert len(restricted) == 3
    payroll = next(d for d in restricted if d.name.startswith("Payroll"))
    # Finance staff (space editors) cannot see the payroll calendar; its owner and the named person can.
    finance = await box.find("Finance & Operations", "p027")
    stack, names = [finance], set()
    while stack:
        for e in await box.items(stack.pop(), "p027"):
            names.add(e["name"])
            if e["type"] == "folder":
                stack.append(e["id"])
    assert payroll.name not in names
    owner_root = {e["name"] for e in await box.items("0", "p025")}
    assert "Restricted" in owner_root
    shared_with = {e["name"] for e in await box.items("0", "p026")}
    assert payroll.name in shared_with


async def test_control_plane_state_and_reset(box):
    state = (await box.client.get("https://api.box.com/_emulator/state")).json()
    assert state["box_files"] == 359 and state["box_users"] == 70
    parent = await box.find("Technology")
    await box.upload(parent, "temp.txt", b"x")
    assert (await box.client.get("https://api.box.com/_emulator/state")).json()["box_files"] == 360
    await box.client.post("https://api.box.com/_emulator/reset")
    assert (await box.client.get("https://api.box.com/_emulator/state")).json()["box_files"] == 359
    assert (await box.get("/users/me", "p035")).status_code == 200  # dev tokens survive a reset
    info = (await box.client.get("https://api.box.com/_emulator/info")).json()
    assert info["name"] == "box" and "configure_webhook_keys" in info["actions"]
    assert "api.box.com" in info["hosts"] and info["default_port"] == 44103


async def test_fault_injection_uses_box_error_shapes(box):
    await box.client.post(
        "https://api.box.com/_emulator/faults", json={"path": "/2.0/users/me", "status": 429, "times": 1}
    )
    r = await box.get("/users/me")
    assert r.status_code == 429 and r.headers["retry-after"] == "2"
    assert r.json()["code"] == "rate_limit_exceeded" and r.json()["type"] == "error"
    await box.client.post(
        "https://api.box.com/_emulator/faults", json={"path": "/2.0/users/me", "status": 401, "times": 1}
    )
    unauthorized = await box.get("/users/me")
    assert unauthorized.status_code == 401 and "www-authenticate" in unauthorized.headers
    await box.client.post(
        "https://api.box.com/_emulator/faults", json={"path": "/oauth2/token", "status": 503, "times": 1}
    )
    token = await box.client.post(
        "https://api.box.com/oauth2/token", data={"grant_type": "client_credentials"}
    )
    assert token.status_code == 503 and token.json()["error"] == "server_error"
    assert (await box.get("/users/me")).status_code == 200
    missing_route = await box.get("/nonexistent")
    assert missing_route.status_code == 404 and missing_route.json()["code"] == "not_found"
    bad_method = await box.client.request("PATCH", "https://api.box.com/2.0/users/me", headers=box.auth())
    assert bad_method.status_code == 405 and bad_method.json()["code"] == "method_not_allowed"


async def test_the_openapi_document_and_docs_page_render_without_warnings(box):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        document = await box.client.get("https://api.box.com/_emulator/openapi.json")
    assert document.status_code == 200
    paths = document.json()["paths"]
    assert "/2.0/folders/{folder_id}/items" in paths and "/api/2.0/files/content" in paths
    assert len(paths) > 90
    assert (await box.client.get("https://api.box.com/_emulator/docs")).status_code == 200
