"""Security tooling routes: ``team/devices/*`` and ``team/linked_apps/*``.

Linked apps are derived from live access tokens (one entry per OAuth ``client_id`` per member);
devices are a deterministic desktop client and phone per active member so admin tooling has
something realistic to list and revoke.
"""

from __future__ import annotations

from typing import Any

from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import Args, Pager, api_error, b64url, digest, tag, ts, union_tag
from emulators.providers.dropbox.dispatch import route
from emulators.providers.dropbox.team import require_admin, team_members

PAGE = 100


def linked_apps(c: m.Call, acct: dict[str, Any]) -> list[dict[str, Any]]:
    apps: dict[str, str] = {}
    revoked = set(acct.get("revoked_apps", ()))
    for record in c.ctx.store.collection("_grants"):
        if record["kind"] != "access" or record.get("revoked") or record.get("user_id") != acct["person"]:
            continue
        if record["client_id"] in revoked:
            continue
        issued = record["issued_at"]
        issued = ts(issued) if not isinstance(issued, str) else issued
        apps[record["client_id"]] = min(apps.get(record["client_id"], issued), issued)
    return [
        {"app_id": app_id, "app_name": app_id, "linked": linked, "is_app_folder": False}
        for app_id, linked in sorted(apps.items())
    ]


def sessions(acct: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    seed = b64url(digest("dbx-device", acct["id"]))
    country = acct.get("country") or "US"
    revoked = set(acct.get("revoked_sessions", ()))
    created = acct.get("joined_on") or "2026-01-01T00:00:00Z"
    ip = f"203.0.113.{digest('dbx-ip', acct['id'])[0] % 250 + 1}"
    desktop = {
        "session_id": "dbdsid:" + seed[:22],
        "ip_address": ip,
        "country": country,
        "created": created,
        "host_name": f"{acct['given_name'].upper()}-LAPTOP",
        "client_type": tag("windows" if digest("dbx-os", acct["id"])[0] % 2 else "mac"),
        "client_version": "198.4.7",
        "platform": "Windows 11" if digest("dbx-os", acct["id"])[0] % 2 else "macOS 15",
        "is_delete_on_unlink_supported": True,
    }
    mobile = {
        "session_id": "dbmsid:" + seed[:22],
        "ip_address": ip,
        "country": country,
        "created": created,
        "device_name": f"{acct['given_name']}'s phone",
        "client_type": tag("iphone" if digest("dbx-os", acct["id"])[1] % 2 else "android"),
        "client_version": "311.2.2",
        "os_version": "18.1",
    }
    return {
        "web_sessions": [],
        "desktop_clients": [s for s in (desktop,) if s["session_id"] not in revoked],
        "mobile_clients": [s for s in (mobile,) if s["session_id"] not in revoked],
    }


def member_devices(acct: dict[str, Any], a: Args) -> dict[str, Any]:
    s = sessions(acct)
    d: dict[str, Any] = {"team_member_id": acct["team_member_id"]}
    if a.bool("include_web_sessions", True):
        d["web_sessions"] = s["web_sessions"]
    if a.bool("include_desktop_clients", True):
        d["desktop_clients"] = s["desktop_clients"]
    if a.bool("include_mobile_clients", True):
        d["mobile_clients"] = s["mobile_clients"]
    return d


def page_members(c: m.Call, kind: str) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    cursor = c.args.str("cursor", None)
    offset = 0
    if cursor is not None:
        state = Pager.decode(cursor, c)
        if not state or state.get("t") != kind:
            raise api_error(tag("reset"))
        offset = state["offset"]
    members = [a for a in team_members(c) if a["status"] != "invited"]
    return members, offset, {"t": kind}


def paged(c: m.Call, kind: str, render: Any) -> dict[str, Any]:
    members, offset, state = page_members(c, kind)
    chunk = members[offset : offset + PAGE]
    more = offset + PAGE < len(members)
    out: dict[str, Any] = {kind_key(kind): [render(a) for a in chunk], "has_more": more}
    if more:
        out["cursor"] = Pager.encode({**state, "offset": offset + PAGE}, c)
    return out


def kind_key(kind: str) -> str:
    return "devices" if kind.startswith("devices") else "apps"


@route("team/devices/list_members_devices", auth="team", scope="sessions.list")
def list_members_devices(c: m.Call) -> Any:
    return paged(c, "devices_m", lambda a: member_devices(a, c.args))


@route("team/devices/list_team_devices", auth="team", scope="sessions.list")
def list_team_devices(c: m.Call) -> Any:
    return paged(c, "devices_t", lambda a: member_devices(a, c.args))


@route("team/devices/list_member_devices", auth="team", scope="sessions.list")
def list_member_devices(c: m.Call) -> Any:
    acct = m.account_by_member_id(c.ctx, c.args.str("team_member_id"))
    if acct is None or not acct.get("is_team"):
        raise api_error(tag("member_not_found"))
    s = sessions(acct)
    out: dict[str, Any] = {}
    if c.args.bool("include_web_sessions", True):
        out["active_web_sessions"] = s["web_sessions"]
    if c.args.bool("include_desktop_clients", True):
        out["desktop_client_sessions"] = s["desktop_clients"]
    if c.args.bool("include_mobile_clients", True):
        out["mobile_client_sessions"] = s["mobile_clients"]
    return out


def revoke_session(c: m.Call, raw: Any) -> None:
    kind, payload = union_tag(raw)
    arg = Args(payload if isinstance(payload, dict) else {}, f"request body: {kind}")
    acct = m.account_by_member_id(c.ctx, arg.str("team_member_id"))
    if acct is None or not acct.get("is_team"):
        raise api_error(tag("member_not_found"))
    sid = arg.str("session_id")
    s = sessions(acct)
    if sid not in {
        x["session_id"] for x in (*s["desktop_clients"], *s["mobile_clients"], *s["web_sessions"])
    }:
        raise api_error(tag("device_session_not_found"))
    acct.setdefault("revoked_sessions", []).append(sid)


@route("team/devices/revoke_device_session", auth="team", scope="sessions.modify")
def revoke_device_session(c: m.Call) -> Any:
    require_admin(c)
    revoke_session(c, c.args.data)
    return None


@route("team/devices/revoke_device_session_batch", auth="team", scope="sessions.modify")
def revoke_device_session_batch(c: m.Call) -> Any:
    require_admin(c)
    out = []
    for raw in c.args.list("revoke_devices"):
        try:
            revoke_session(c, raw)
            out.append({"success": True})
        except Exception as exc:
            body = getattr(exc, "body", None)
            err = body["error"] if isinstance(body, dict) and "error" in body else tag("other")
            out.append({"success": False, "error_type": err})
    return {"revoke_devices_status": out}


# -- linked apps -------------------------------------------------------------------------
def member_apps(c: m.Call, acct: dict[str, Any]) -> dict[str, Any]:
    return {"team_member_id": acct["team_member_id"], "linked_api_apps": linked_apps(c, acct)}


@route("team/linked_apps/list_members_linked_apps", auth="team", scope="sessions.list")
def list_members_linked_apps(c: m.Call) -> Any:
    return paged(c, "apps_m", lambda a: member_apps(c, a))


@route("team/linked_apps/list_team_linked_apps", auth="team", scope="sessions.list")
def list_team_linked_apps(c: m.Call) -> Any:
    return paged(c, "apps_t", lambda a: member_apps(c, a))


@route("team/linked_apps/list_member_linked_apps", auth="team", scope="sessions.list")
def list_member_linked_apps(c: m.Call) -> Any:
    acct = m.account_by_member_id(c.ctx, c.args.str("team_member_id"))
    if acct is None or not acct.get("is_team"):
        raise api_error(tag("member_not_found"))
    return {"linked_api_apps": linked_apps(c, acct)}


def revoke_app(c: m.Call, arg: Args) -> None:
    acct = m.account_by_member_id(c.ctx, arg.str("team_member_id"))
    if acct is None or not acct.get("is_team"):
        raise api_error(tag("member_not_found"))
    app_id = arg.str("app_id")
    if app_id not in {a["app_id"] for a in linked_apps(c, acct)}:
        raise api_error(tag("app_not_found"))
    for record in c.ctx.store.collection("_grants"):
        if record.get("user_id") == acct["person"] and record["client_id"] == app_id:
            record["revoked"] = True


@route("team/linked_apps/revoke_linked_app", auth="team", scope="sessions.modify")
def revoke_linked_app(c: m.Call) -> Any:
    require_admin(c)
    revoke_app(c, c.args)
    return None


@route("team/linked_apps/revoke_linked_app_batch", auth="team", scope="sessions.modify")
def revoke_linked_app_batch(c: m.Call) -> Any:
    require_admin(c)
    out = []
    for raw in c.args.list("revoke_linked_app"):
        try:
            revoke_app(c, Args(raw, "request body: revoke_linked_app"))
            out.append({"success": True})
        except Exception as exc:
            body = getattr(exc, "body", None)
            err = body["error"] if isinstance(body, dict) and "error" in body else tag("other")
            out.append({"success": False, "error_type": err})
    return {"revoke_linked_app_status": out}
