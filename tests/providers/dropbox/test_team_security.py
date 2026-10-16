"""team/devices and team/linked_apps: derived from live tokens and deterministic devices."""

from __future__ import annotations

from typing import Any


async def test_linked_apps_follow_tokens_and_can_be_revoked(dbx: Any) -> None:
    arjun = await dbx.team_token()
    maya = await dbx.token(dbx.MAYA, client_id="expense-sync")
    await dbx.token(dbx.MAYA, client_id="doc-indexer", scopes=["files.metadata.read"])
    member = await dbx.member_id(dbx.MAYA)
    apps = await dbx.ok(arjun, "team/linked_apps/list_member_linked_apps", {"team_member_id": member})
    assert [a["app_id"] for a in apps["linked_api_apps"]] == ["doc-indexer", "expense-sync"]
    everyone = await dbx.ok(arjun, "team/linked_apps/list_members_linked_apps", {})
    mine = next(x for x in everyone["apps"] if x["team_member_id"] == member)
    assert len(mine["linked_api_apps"]) == 2 and everyone["has_more"] is False
    team_wide = await dbx.ok(arjun, "team/linked_apps/list_team_linked_apps", {})
    assert team_wide["apps"]
    await dbx.ok(
        arjun, "team/linked_apps/revoke_linked_app", {"app_id": "expense-sync", "team_member_id": member}
    )
    err = await dbx.err(maya, "users/get_current_account", status=401)
    assert err["error"][".tag"] == "invalid_access_token"
    again = await dbx.err(
        arjun, "team/linked_apps/revoke_linked_app", {"app_id": "expense-sync", "team_member_id": member}
    )
    assert again["error"][".tag"] == "app_not_found"
    batch = await dbx.ok(
        arjun,
        "team/linked_apps/revoke_linked_app_batch",
        {
            "revoke_linked_app": [
                {"app_id": "doc-indexer", "team_member_id": member},
                {"app_id": "x", "team_member_id": "dbmid:no"},
            ]
        },
    )
    assert [s["success"] for s in batch["revoke_linked_app_status"]] == [True, False]
    assert batch["revoke_linked_app_status"][1]["error_type"] == {".tag": "member_not_found"}


async def test_devices_listing_and_revocation(dbx: Any) -> None:
    arjun = await dbx.team_token()
    member = await dbx.member_id(dbx.MAYA)
    page = await dbx.ok(arjun, "team/devices/list_members_devices", {"include_web_sessions": False})
    entry = next(d for d in page["devices"] if d["team_member_id"] == member)
    assert (
        "web_sessions" not in entry
        and len(entry["desktop_clients"]) == 1
        and len(entry["mobile_clients"]) == 1
    )
    single = await dbx.ok(arjun, "team/devices/list_member_devices", {"team_member_id": member})
    desktop = single["desktop_client_sessions"][0]
    revoked = await dbx.ok(
        arjun,
        "team/devices/revoke_device_session",
        {".tag": "desktop_client", "session_id": desktop["session_id"], "team_member_id": member},
    )
    assert revoked is None
    after = await dbx.ok(arjun, "team/devices/list_member_devices", {"team_member_id": member})
    assert after["desktop_client_sessions"] == [] and len(after["mobile_client_sessions"]) == 1
    missing = await dbx.err(
        arjun,
        "team/devices/revoke_device_session",
        {".tag": "web_session", "session_id": "nope", "team_member_id": member},
    )
    assert missing["error"][".tag"] == "device_session_not_found"
    batch = await dbx.ok(
        arjun,
        "team/devices/revoke_device_session_batch",
        {
            "revoke_devices": [
                {
                    ".tag": "mobile_client",
                    "session_id": single["mobile_client_sessions"][0]["session_id"],
                    "team_member_id": member,
                }
            ]
        },
    )
    assert batch["revoke_devices_status"] == [{"success": True}]
    assert (await dbx.ok(arjun, "team/devices/list_team_devices", {}))["devices"]
    bad = await dbx.err(arjun, "team/devices/list_members_devices", {"cursor": "nope"})
    assert bad["error"][".tag"] == "reset"
