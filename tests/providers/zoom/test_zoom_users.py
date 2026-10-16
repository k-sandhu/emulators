"""Users, groups and settings: shapes, paging, role/visibility rules, admin mutations."""

from __future__ import annotations

import httpx

from emulators.testing.helpers import bearer
from tests.providers.zoom.conftest import ADMIN, HOST, OWNER, STAFF, V2, token_for

LIST_KEYS = [
    "id first_name last_name display_name email type pmi timezone verified dept created_at last_login_time "
    "last_client_version pic_url language phone_number status role_id employee_unique_id user_created_at group_ids"
][0].split()


async def test_list_users_envelope_paging_and_item_shape(
    zoom: httpx.AsyncClient, s2s: dict[str, str]
) -> None:
    first = (await zoom.get(f"{V2}/users", headers=s2s)).json()
    assert list(first)[:5] == ["page_count", "page_number", "page_size", "total_records", "next_page_token"]
    assert (first["page_count"], first["page_number"], first["page_size"], first["total_records"]) == (
        2,
        1,
        30,
        45,
    )
    assert first["next_page_token"] and len(first["users"]) == 30
    user = first["users"][0]
    assert list(user) == LIST_KEYS
    assert len(user["id"]) == 22 and len(str(user["pmi"])) == 10 and user["verified"] == 1
    assert user["status"] == "active" and user["type"] in (1, 2) and user["role_id"] in ("0", "1", "2")

    second = (
        await zoom.get(f"{V2}/users", headers=s2s, params={"next_page_token": first["next_page_token"]})
    ).json()
    assert second["page_number"] == 2 and len(second["users"]) == 15 and second["next_page_token"] == ""
    ids = [u["id"] for u in first["users"] + second["users"]]
    assert len(set(ids)) == 45


async def test_list_users_filters_and_limits(zoom: httpx.AsyncClient, s2s: dict[str, str]) -> None:
    inactive = (await zoom.get(f"{V2}/users", headers=s2s, params={"status": "inactive"})).json()
    assert {u["email"] for u in inactive["users"]} == {
        "paul.henderson@halcyonadvisory.example",
        "yuki.tanaka@halcyonadvisory.example",
    }
    assert (await zoom.get(f"{V2}/users", headers=s2s, params={"status": "pending"})).json()[
        "total_records"
    ] == 0
    owner = (await zoom.get(f"{V2}/users", headers=s2s, params={"role_id": "0"})).json()
    assert [u["email"] for u in owner["users"]] == [OWNER]
    admins = (await zoom.get(f"{V2}/users", headers=s2s, params={"role_id": "1"})).json()
    assert {u["email"] for u in admins["users"]} == {ADMIN, "sarah.johansson@halcyonadvisory.example"}
    capped = (await zoom.get(f"{V2}/users", headers=s2s, params={"page_size": 5000})).json()
    assert capped["page_size"] == 300 and len(capped["users"]) == 45
    with_key = (
        await zoom.get(f"{V2}/users", headers=s2s, params={"include_fields": "host_key", "page_size": 1})
    ).json()
    assert len(with_key["users"][0]["host_key"]) == 6
    bad = await zoom.get(f"{V2}/users", headers=s2s, params={"page_size": "many"})
    assert bad.status_code == 400 and bad.json()["code"] == 300
    stale = await zoom.get(f"{V2}/users", headers=s2s, params={"next_page_token": "bogus"})
    assert stale.status_code == 400
    assert stale.json() == {"code": 300, "message": "The next page token is invalid or has expired."}
    bad_status = await zoom.get(f"{V2}/users", headers=s2s, params={"status": "weird"})
    assert bad_status.status_code == 400 and bad_status.json()["errors"][0]["field"] == "status"


async def test_basic_and_licensed_types_and_group_ids(zoom: httpx.AsyncClient, s2s: dict[str, str]) -> None:
    users = (await zoom.get(f"{V2}/users", headers=s2s, params={"page_size": 300})).json()["users"]
    assert {u["type"] for u in users} == {1, 2}
    licensed = sum(1 for u in users if u["type"] == 2)
    assert licensed > 35  # "type 2 Licensed for most, 1 Basic for a few staff"
    assert all(u["group_ids"] for u in users)


async def test_get_user_by_me_id_email_and_full_record(zoom: httpx.AsyncClient, host: dict[str, str]) -> None:
    me = (await zoom.get(f"{V2}/users/me", headers=host)).json()
    assert me["email"] == HOST and me["role_name"] == "Member" and me["role_id"] == "2"
    for key in (
        "personal_meeting_url",
        "cms_user_id",
        "jid",
        "account_id",
        "account_number",
        "host_key",
        "cluster",
        "job_title",
        "company",
        "location",
        "language",
        "phone_country",
        "login_types",
    ):
        assert key in me, key
    assert me["cluster"] == "us06" and me["account_id"] == zoom.hub.world.org.zoom_account_id  # type: ignore[attr-defined]
    assert me["jid"].endswith("@xmpp.zoom.us") and me["personal_meeting_url"].startswith(
        "https://us06web.zoom.us/j/"
    )
    assert (await zoom.get(f"{V2}/users/{me['id']}", headers=host)).json()["email"] == HOST
    assert (await zoom.get(f"{V2}/users/{HOST}", headers=host)).json()["id"] == me["id"]


async def test_delegated_user_cannot_read_colleagues_but_admins_can(
    zoom: httpx.AsyncClient, host: dict[str, str], admin: dict[str, str]
) -> None:
    denied = await zoom.get(f"{V2}/users/{STAFF}", headers=host)
    assert denied.status_code == 403 and denied.json() == {"code": 200, "message": "No permission."}
    assert (await zoom.get(f"{V2}/users", headers=host)).status_code == 403
    assert (await zoom.get(f"{V2}/users/{STAFF}", headers=admin)).json()["email"] == STAFF
    assert (await zoom.get(f"{V2}/users", headers=admin)).json()["total_records"] == 45
    missing = await zoom.get(f"{V2}/users/ghost@halcyonadvisory.example", headers=admin)
    assert missing.status_code == 404
    assert missing.json() == {"code": 1001, "message": "User does not exist: ghost@halcyonadvisory.example."}


async def test_scopes_gate_endpoints_for_every_token_kind(zoom: httpx.AsyncClient) -> None:
    narrow = await token_for(zoom, None, scopes="meeting:read:admin")
    r = await zoom.get(f"{V2}/users", headers=narrow)
    assert r.status_code == 401 and r.json()["code"] == 4700
    assert r.json()["message"] == "Invalid access token, does not contain scopes:[user:read:admin]."
    granular_narrow = await token_for(zoom, None, scopes="meeting:read:list_meetings:admin")
    g = await zoom.get(f"{V2}/users", headers=granular_narrow)
    assert (
        g.json()["message"] == "Invalid access token, does not contain scopes:[user:read:list_users:admin]."
    )
    wide = await token_for(zoom, None, scopes="user:read:admin")
    assert (await zoom.get(f"{V2}/users", headers=wide)).status_code == 200
    granular = await token_for(zoom, None, scopes="user:read:list_users:admin")
    assert (await zoom.get(f"{V2}/users", headers=granular)).status_code == 200
    # a non-admin scope on an account-level app gives no reach over other users
    user_level = await token_for(zoom, None, scopes="user:read")
    assert (await zoom.get(f"{V2}/users/{STAFF}", headers=user_level)).status_code == 403
    # a non-admin user holding the :admin scope still only sees themselves
    staff_admin_scope = await token_for(zoom, STAFF, scopes="user:read:admin")
    assert (await zoom.get(f"{V2}/users/me", headers=staff_admin_scope)).json()["email"] == STAFF
    assert (await zoom.get(f"{V2}/users/{HOST}", headers=staff_admin_scope)).status_code == 403
    assert (await zoom.get(f"{V2}/users", headers=staff_admin_scope)).status_code == 403


async def test_patch_user_own_profile_vs_admin_only_fields(
    zoom: httpx.AsyncClient, staff: dict[str, str], admin: dict[str, str]
) -> None:
    ok = await zoom.patch(
        f"{V2}/users/me", headers=staff, json={"job_title": "Lead Associate", "language": "fr-FR"}
    )
    assert ok.status_code == 204 and ok.content == b""
    me = (await zoom.get(f"{V2}/users/me", headers=staff)).json()
    assert me["job_title"] == "Lead Associate" and me["language"] == "fr-FR"
    assert (await zoom.patch(f"{V2}/users/me", headers=staff, json={"type": 1})).status_code == 403
    assert (await zoom.patch(f"{V2}/users/{HOST}", headers=staff, json={"job_title": "x"})).status_code == 403
    changed = await zoom.patch(f"{V2}/users/{STAFF}", headers=admin, json={"type": 1, "first_name": "Ry"})
    assert changed.status_code == 204
    after = (await zoom.get(f"{V2}/users/{STAFF}", headers=admin)).json()
    assert after["type"] == 1 and after["first_name"] == "Ry" and after["display_name"].startswith("Ry ")
    invalid = await zoom.patch(f"{V2}/users/{STAFF}", headers=admin, json={"type": 7})
    assert invalid.status_code == 400 and invalid.json()["errors"][0]["field"] == "type"
    assert (await zoom.patch(f"{V2}/users/{STAFF}", headers=admin, content=b"[1,2]")).status_code == 400


async def test_create_delete_and_deactivate_users(
    zoom: httpx.AsyncClient, admin: dict[str, str], staff: dict[str, str]
) -> None:
    body = {
        "action": "create",
        "user_info": {
            "email": "new.hire@halcyonadvisory.example",
            "type": 1,
            "first_name": "New",
            "last_name": "Hire",
        },
    }
    assert (await zoom.post(f"{V2}/users", headers=staff, json=body)).status_code == 403
    created = await zoom.post(f"{V2}/users", headers=admin, json=body)
    assert created.status_code == 201
    assert created.json()["email"] == "new.hire@halcyonadvisory.example" and len(created.json()["id"]) == 22
    again = await zoom.post(f"{V2}/users", headers=admin, json=body)
    assert again.status_code == 409 and again.json()["code"] == 1005
    pending = (await zoom.get(f"{V2}/users", headers=admin, params={"status": "pending"})).json()
    assert [u["email"] for u in pending["users"]] == ["new.hire@halcyonadvisory.example"]
    exists = await zoom.get(
        f"{V2}/users/email", headers=admin, params={"email": "new.hire@halcyonadvisory.example"}
    )
    assert exists.json() == {"existed_email": True}
    assert (await zoom.get(f"{V2}/users/email", headers=admin, params={"email": "x@y.example"})).json() == {
        "existed_email": False
    }

    assert (await zoom.delete(f"{V2}/users/{created.json()['id']}", headers=admin)).status_code == 204
    assert (await zoom.get(f"{V2}/users/{created.json()['id']}", headers=admin)).status_code == 404
    assert (await zoom.delete(f"{V2}/users/{OWNER}", headers=admin)).status_code == 400
    assert (await zoom.delete(f"{V2}/users/{STAFF}", headers=staff)).status_code == 403

    off = await zoom.put(f"{V2}/users/{STAFF}/status", headers=admin, json={"action": "deactivate"})
    assert off.status_code == 204
    assert (await zoom.get(f"{V2}/users/me", headers=staff)).status_code == 401  # their tokens died
    inactive = (await zoom.get(f"{V2}/users", headers=admin, params={"status": "inactive"})).json()
    assert STAFF in {u["email"] for u in inactive["users"]}
    on = await zoom.put(f"{V2}/users/{STAFF}/status", headers=admin, json={"action": "activate"})
    assert on.status_code == 204


async def test_user_settings_token_and_permissions(
    zoom: httpx.AsyncClient, host: dict[str, str], admin: dict[str, str]
) -> None:
    settings = (await zoom.get(f"{V2}/users/me/settings", headers=host)).json()
    assert {
        "schedule_meeting",
        "in_meeting",
        "email_notification",
        "recording",
        "telephony",
        "feature",
        "tsp",
    } <= set(settings)
    assert settings["recording"]["auto_recording"] == "none"
    patch = await zoom.patch(
        f"{V2}/users/me/settings", headers=host, json={"recording": {"auto_recording": "cloud"}}
    )
    assert patch.status_code == 204
    assert (await zoom.get(f"{V2}/users/me/settings", headers=host)).json()["recording"][
        "auto_recording"
    ] == "cloud"
    zak = (await zoom.get(f"{V2}/users/me/token", headers=host, params={"type": "zak"})).json()["token"]
    assert zak.count(".") == 2
    perms = (await zoom.get(f"{V2}/users/me/permissions", headers=host)).json()["permissions"]
    assert "Meeting:Read" in perms and "User:Edit" not in perms
    assert "User:Edit" in (await zoom.get(f"{V2}/users/me/permissions", headers=admin)).json()["permissions"]


async def test_groups_list_members_and_membership_changes(
    zoom: httpx.AsyncClient, admin: dict[str, str], host: dict[str, str]
) -> None:
    groups = (await zoom.get(f"{V2}/groups", headers=admin)).json()
    assert set(groups) == {"groups", "next_page_token", "page_size", "total_records"}
    by_name = {g["name"]: g for g in groups["groups"]}
    assert {"Audit & Assurance", "Technology", "All Staff", "Partners"} <= set(by_name)
    assert "Toronto Office" not in by_name  # office distribution lists are not Zoom groups
    tech = by_name["Technology"]
    assert (await zoom.get(f"{V2}/groups/{tech['id']}", headers=admin)).json() == tech
    members = (await zoom.get(f"{V2}/groups/{tech['id']}/members", headers=admin)).json()
    assert members["total_records"] == tech["total_members"] == 5
    assert set(members["members"][0]) == {"id", "email", "first_name", "last_name", "type", "primary_group"}

    assert (await zoom.get(f"{V2}/groups", headers=host)).status_code == 403
    created = (await zoom.post(f"{V2}/groups", headers=admin, json={"name": "Clients Team"})).json()
    assert created["total_members"] == 0
    dup = await zoom.post(f"{V2}/groups", headers=admin, json={"name": "clients team"})
    assert dup.status_code == 409
    host_id = (await zoom.get(f"{V2}/users/{HOST}", headers=admin)).json()["id"]
    added = await zoom.post(
        f"{V2}/groups/{created['id']}/members",
        headers=admin,
        json={"members": [{"id": host_id}, {"email": STAFF}]},
    )
    assert (
        added.status_code == 201 and len(added.json()["ids"].split(",")) == 2 and "added_at" in added.json()
    )
    assert created["id"] in (await zoom.get(f"{V2}/users/{HOST}", headers=admin)).json()["group_ids"]
    assert (await zoom.get(f"{V2}/groups/{created['id']}", headers=admin)).json()["total_members"] == 2
    assert (
        await zoom.delete(f"{V2}/groups/{created['id']}/members/{host_id}", headers=admin)
    ).status_code == 204
    assert (
        await zoom.patch(f"{V2}/groups/{created['id']}", headers=admin, json={"name": "Client Team"})
    ).status_code == 204
    assert (await zoom.delete(f"{V2}/groups/{created['id']}", headers=admin)).status_code == 204
    gone = await zoom.get(f"{V2}/groups/{created['id']}", headers=admin)
    assert gone.status_code == 404 and "group" in gone.json()["message"].lower()
    assert created["id"] not in (await zoom.get(f"{V2}/users/{STAFF}", headers=admin)).json()["group_ids"]


async def test_inactive_user_lookup_and_tokens(zoom: httpx.AsyncClient, admin: dict[str, str]) -> None:
    departed = (await zoom.get(f"{V2}/users/paul.henderson@halcyonadvisory.example", headers=admin)).json()
    assert departed["status"] == "inactive"
    denied = await zoom.post(
        "https://api.zoom.us/_emulator/tokens", json={"user": "paul.henderson@halcyonadvisory.example"}
    )
    token = denied.json()["access_token"]
    assert (await zoom.get(f"{V2}/users/me", headers=bearer(token))).status_code == 401
