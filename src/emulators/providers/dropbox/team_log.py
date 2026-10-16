"""``team_log/get_events`` -- a compact audit log.

Events are recorded by the file and sharing routes through :func:`audit` and seeded from recent
document activity in the world. Only a core set of event types is generated (file_add, file_edit,
file_delete, file_move, file_copy, file_rename, login_success, shared_folder_create,
shared_link_create, group_create, member_change_status); the wire shapes follow ``team_log``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import Pager, api_error, parse_ts, tag, ts, union_tag
from emulators.providers.dropbox.dispatch import route

DESCRIPTIONS = {
    "file_add": ("file_operations", "Added files and/or folders"),
    "file_edit": ("file_operations", "Edited files"),
    "file_delete": ("file_operations", "Deleted files and/or folders"),
    "file_move": ("file_operations", "Moved files and/or folders"),
    "file_copy": ("file_operations", "Copied files and/or folders"),
    "file_rename": ("file_operations", "Renamed files and/or folders"),
    "file_download": ("file_operations", "Downloaded files and/or folders"),
    "login_success": ("logins", "Signed in"),
    "shared_folder_create": ("sharing", "Created shared folders"),
    "shared_link_create": ("sharing", "Created shared links"),
    "group_create": ("groups", "Created groups"),
    "member_change_status": ("members", "Changed member status (invited, joined, suspended, etc.)"),
}


def audit(
    c: m.Call,
    kind: str,
    rec: dict[str, Any] | None = None,
    *,
    dest: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    """Append an audit event for the acting account."""
    record: dict[str, Any] = {
        "id": f"ev{m.counter(c.ctx, 'audit'):08d}",
        "ts": ts(c.now()),
        "actor": c.actor.id if c.actor.account.get("id") else None,
        "kind": kind,
        "assets": [_asset(rec)] if rec is not None else [],
        **extra,
    }
    if dest is not None:
        record["assets"].append(_asset(dest))
    c.ctx.store.collection(m.AUDIT).put(record)


def _asset(rec: dict[str, Any]) -> dict[str, Any]:
    return {"type": rec["type"], "ns": rec["ns"], "path": rec["dpath"], "name": rec["name"], "id": rec["id"]}


def seed_events(ctx: ProviderContext) -> None:
    """Recent edits (last 30 days) become ``file_edit`` events; each active member signed in once."""
    store = ctx.store.collection(m.AUDIT)
    anchor = ctx.world.anchor
    n = 0
    for rec in ctx.store.collection(m.ENTRIES):
        if rec["type"] != "file":
            continue
        latest = rec["revisions"][-1]
        when = parse_ts(latest["server_modified"])
        if anchor - when <= timedelta(days=30) and latest.get("modified_by"):
            n += 1
            store.put(
                {
                    "id": f"ev{n:08d}",
                    "ts": latest["server_modified"],
                    "actor": latest["modified_by"],
                    "kind": "file_edit" if len(rec["revisions"]) > 1 else "file_add",
                    "assets": [_asset(rec)],
                }
            )
    for acct in ctx.store.collection(m.ACCOUNTS):
        if acct.get("is_team") and acct["status"] == "active":
            n += 1
            store.put(
                {
                    "id": f"ev{n:08d}",
                    "ts": ts(anchor - timedelta(hours=1 + n % 20)),
                    "actor": acct["id"],
                    "kind": "login_success",
                    "assets": [],
                }
            )
    ctx.store.meta.setdefault("counters", {})["audit"] = n


# ---------------------------------------------------------------------------------------
# Wire rendering
# ---------------------------------------------------------------------------------------
def log_user(ctx: ProviderContext, acct: dict[str, Any]) -> dict[str, Any]:
    if acct.get("is_team"):
        return {
            ".tag": "team_member",
            "account_id": acct["id"],
            "display_name": acct["display_name"],
            "email": acct["email"],
            "team_member_id": acct["team_member_id"],
        }
    return {
        ".tag": "non_team_member",
        "account_id": acct["id"],
        "display_name": acct["display_name"],
        "email": acct["email"],
    }


def asset_obj(rec: dict[str, Any]) -> dict[str, Any]:
    path = {"namespace_relative": {"ns_id": rec["ns"], "relative_path": rec["path"]}}
    if rec["type"] == "folder":
        return {".tag": "folder", "path": path, "display_name": rec["name"]}
    return {".tag": "file", "path": path, "display_name": rec["name"], "file_id": rec["id"]}


def event_details(kind: str, record: dict[str, Any]) -> dict[str, Any]:
    if kind in ("file_move", "file_copy", "file_rename"):
        return {
            ".tag": f"{kind}_details",
            "relocate_action_details": [{"src_asset_index": 0, "dest_asset_index": 1}],
        }
    if kind == "login_success":
        return {".tag": "login_success_details", "is_emm_managed": False, "login_method": tag("password")}
    if kind == "shared_folder_create":
        return {".tag": "shared_folder_create_details", "target_ns_id": record.get("ns_id")}
    if kind == "shared_link_create":
        return {
            ".tag": "shared_link_create_details",
            "shared_link_access_level": tag(record.get("access", "viewer")),
        }
    if kind == "group_create":
        return {".tag": "group_create_details", "is_company_managed": bool(record.get("company_managed"))}
    if kind == "member_change_status":
        return {
            ".tag": "member_change_status_details",
            "previous_value": tag(record.get("previous", "active")),
            "new_value": tag(record.get("new", "suspended")),
        }
    return {".tag": f"{kind}_details"}


def render_event(ctx: ProviderContext, record: dict[str, Any]) -> dict[str, Any]:
    category, description = DESCRIPTIONS[record["kind"]]
    acct = m.account(ctx, record.get("actor"))
    d: dict[str, Any] = {"timestamp": record["ts"], "event_category": tag(category)}
    if acct is not None:
        user = log_user(ctx, acct)
        d["actor"] = tag("user", user=user)
        d["context"] = user
        d["involve_non_team_member"] = not acct.get("is_team")
    d["origin"] = {"access_method": tag("api", request_id=record["id"])}
    ns_assets = []
    for asset in record.get("assets", ()):
        ns_assets.append(
            asset_obj(
                {
                    "type": asset["type"],
                    "ns": asset["ns"],
                    "path": asset["path"],
                    "name": asset["name"],
                    "id": asset["id"],
                }
            )
        )
    if ns_assets:
        d["assets"] = ns_assets
    d["event_type"] = tag(record["kind"], description=description)
    d["details"] = event_details(record["kind"], record)
    return d


def events_page(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    items = sorted(c.ctx.store.collection(m.AUDIT).values(), key=lambda r: (r["ts"], r["id"]))
    if state.get("account"):
        items = [r for r in items if r.get("actor") == state["account"]]
    if state.get("start"):
        items = [r for r in items if r["ts"] >= state["start"]]
    if state.get("end"):
        items = [r for r in items if r["ts"] < state["end"]]
    if state.get("category"):
        items = [r for r in items if DESCRIPTIONS[r["kind"]][0] == state["category"]]
    if state.get("etype"):
        items = [r for r in items if r["kind"] == state["etype"]]
    offset, limit = state["offset"], state["limit"]
    chunk = items[offset : offset + limit]
    more = offset + limit < len(items)
    nxt = {**state, "offset": offset + len(chunk)}
    return {
        "events": [render_event(c.ctx, r) for r in chunk],
        "cursor": Pager.encode(nxt, c),
        "has_more": more,
    }


@route("team_log/get_events", auth="team", scope="events.read")
def get_events(c: m.Call) -> Any:
    a = c.args
    window = a.obj("time", None)
    start = window.ts("start_time") if window else None
    end = window.ts("end_time") if window else None
    if start and end and start > end:
        raise api_error(tag("invalid_time_range"))
    account_id = a.str("account_id", None)
    if account_id and m.account(c.ctx, account_id) is None:
        raise api_error(tag("account_id_not_found"))
    category = None
    if a.has("category"):
        category = a.union("category")[0]
    etype = None
    if a.has("event_type"):
        etype = union_tag(a.raw("event_type"))[0]
    state = {
        "t": "events",
        "offset": 0,
        "limit": a.int("limit", 1000, lo=1, hi=1000),
        "account": account_id,
        "start": ts(start) if start else None,
        "end": ts(end) if end else None,
        "category": category,
        "etype": etype,
    }
    return events_page(c, state)


@route("team_log/get_events/continue", auth="team", scope="events.read")
def get_events_continue(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"), c)
    if not state or state.get("t") != "events":
        raise api_error(tag("bad_cursor"))
    return events_page(c, state)
