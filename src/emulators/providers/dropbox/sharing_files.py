"""``sharing/*`` for individual files: file members, received files, shared-file metadata."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from emulators.core.errors import EmulatorError
from emulators.core.provider import url_for_host
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import Args, Pager, api_error, tag, ts, union_tag
from emulators.providers.dropbox.dispatch import route
from emulators.providers.dropbox.sharing_common import (
    access_tag,
    group_info,
    initials,
    policy_obj,
    resolve_selector,
)
from emulators.providers.dropbox.sharing_folders import ACCESS_LEVELS, EMAIL_RE

FILE_PATTERN = r"((/|id:).*|nspath:[0-9]+:.*)|ns:[0-9]+(/.*)?"
FILE_ACTIONS = (
    "disable_viewer_info",
    "edit_contents",
    "enable_viewer_info",
    "invite_viewer",
    "invite_viewer_no_comment",
    "invite_editor",
    "unshare",
    "relinquish_membership",
    "share_link",
    "create_link",
    "create_view_link",
    "create_edit_link",
)


def file_access_error(kind: str) -> EmulatorError:
    return api_error({".tag": "access_error", "access_error": {".tag": kind}})


def locate(c: m.Call, ref: str) -> tuple[dict[str, Any], m.Loc]:
    if not re.fullmatch(FILE_PATTERN, ref, re.DOTALL):
        raise EmulatorError(
            400,
            f"request body: file: '{ref}' did not match pattern '{FILE_PATTERN}'",
            media_type="text/plain",
        )
    loc = m.resolve(c, ref, allow_root=False)
    if loc is None or loc.rec is None:
        raise file_access_error("invalid_file")
    if loc.rec["type"] == "folder":
        raise file_access_error("is_folder")
    return loc.rec, loc


def file_permissions(role: str | None, requested: list[Any]) -> list[dict[str, Any]]:
    is_owner = role == "owner"
    is_editor = role in ("owner", "editor")
    out: list[dict[str, Any]] = []
    for raw in requested:
        name, _ = union_tag(raw)
        allow = True
        if name in ("edit_contents", "invite_editor", "invite_viewer", "invite_viewer_no_comment"):
            allow = is_editor
        elif name in ("unshare", "disable_viewer_info", "enable_viewer_info"):
            allow = is_owner
        elif name == "relinquish_membership":
            allow = not is_owner
        entry: dict[str, Any] = {"action": tag(name or "other"), "allow": allow}
        if not allow:
            entry["reason"] = tag("permission_denied")
        out.append(entry)
    return out


def shared_file_metadata(
    c: m.Call, rec: dict[str, Any], loc: m.Loc, actions: list[Any] | None
) -> dict[str, Any]:
    role = m.entry_role(c, rec)
    ns = c.tree.namespace(rec["ns"])
    d: dict[str, Any] = {"id": rec["id"], "name": rec["name"]}
    if role:
        d["access_type"] = access_tag(role)
    owner = m.account(c.ctx, rec.get("owner"))
    if owner is not None:
        d["owner_display_names"] = [owner["display_name"]]
        if owner.get("is_team"):
            team = m.team_info(c.ctx)
            d["owner_team"] = {"id": team["id"], "name": team["name"]}
    if ns is not None and ns["type"] in ("team_folder", "shared_folder"):
        d["parent_shared_folder_id"] = ns["id"]
    if loc.reachable:
        d["path_display"] = loc.path_display
        d["path_lower"] = loc.path_lower
    if actions is not None:
        d["permissions"] = file_permissions(role, actions)
    d["policy"] = policy_obj(ns or {})
    d["preview_url"] = url_for_host(
        c.request,
        c.ctx,
        "www.dropbox.com",
        "/home" + quote(loc.path_display if loc.reachable else "/" + rec["name"]),
    )
    for share in rec.get("members", ()):
        if share.get("account_id") == c.actor.id:
            d["time_invited"] = share["created"]
    return d


@route("sharing/get_file_metadata", scope="sharing.read")
def get_file_metadata(c: m.Call) -> Any:
    rec, loc = locate(c, c.args.str("file"))
    return shared_file_metadata(c, rec, loc, c.args.raw("actions", None))


@route("sharing/get_file_metadata/batch", scope="sharing.read")
def get_file_metadata_batch(c: m.Call) -> Any:
    out = []
    for ref in c.args.list("files"):
        try:
            rec, loc = locate(c, ref)
            out.append(
                {
                    "file": ref,
                    "result": tag(
                        "metadata", **shared_file_metadata(c, rec, loc, c.args.raw("actions", None))
                    ),
                }
            )
        except EmulatorError as exc:
            err = (
                exc.body["error"]["access_error"]
                if isinstance(exc.body, dict) and "error" in exc.body
                else tag("other")
            )
            out.append({"file": ref, "result": tag("access_error", access_error=err)})
    return out


# ---------------------------------------------------------------------------------------
# Members of a file
# ---------------------------------------------------------------------------------------
def explicit_members(c: m.Call, rec: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in rec.get("members", ()) if not m.expired(s, c.ctx)]


def file_membership(
    c: m.Call, rec: dict[str, Any], include_inherited: bool, actions: list[Any] | None
) -> dict[str, list[dict[str, Any]]]:
    users: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    invitees: list[dict[str, Any]] = []

    def perms(level: str) -> dict[str, Any]:
        return {"permissions": file_permissions(m.entry_role(c, rec), actions)} if actions is not None else {}

    owner = m.account(c.ctx, rec.get("owner"))
    ns = c.tree.namespace(rec["ns"])
    if owner is not None and ns is not None and ns["type"] in ("team_member_folder", "personal"):
        users.append(
            {
                "access_type": access_tag("owner"),
                **perms("owner"),
                "initials": initials(owner["display_name"]),
                "is_inherited": False,
                "user": m.user_info(c, owner),
            }
        )
    for share in explicit_members(c, rec):
        if share.get("account_id"):
            acct = m.account(c.ctx, share["account_id"])
            if acct is None:
                continue
            users.append(
                {
                    "access_type": access_tag(share["access_type"]),
                    **perms(share["access_type"]),
                    "initials": initials(acct["display_name"]),
                    "is_inherited": False,
                    "user": m.user_info(c, acct),
                }
            )
        else:
            invitees.append(
                {
                    "access_type": access_tag(share["access_type"]),
                    **perms(share["access_type"]),
                    "initials": initials(share["email"].split("@")[0].replace(".", " ")),
                    "is_inherited": False,
                    "invitee": tag("email", email=share["email"]),
                }
            )
    if (
        include_inherited
        and not rec.get("no_inherit")
        and ns is not None
        and ns["type"] in ("team_folder", "shared_folder")
    ):
        for member in ns.get("members", ()):
            if member["kind"] == "user":
                acct = m.account(c.ctx, member["account_id"])
                if acct is not None and not any(u["user"]["account_id"] == acct["id"] for u in users):
                    users.append(
                        {
                            "access_type": access_tag(member["access_type"]),
                            **perms(member["access_type"]),
                            "initials": initials(acct["display_name"]),
                            "is_inherited": True,
                            "user": m.user_info(c, acct),
                        }
                    )
            elif member["kind"] == "group":
                group = c.ctx.store.collection(m.GROUPS).get(member["group_id"])
                if group is not None:
                    groups.append(
                        {
                            "access_type": access_tag(member["access_type"]),
                            **perms(member["access_type"]),
                            "is_inherited": True,
                            "group": group_info(c, group),
                        }
                    )
    return {"users": users, "groups": groups, "invitees": invitees}


def members_page(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    rec, _ = locate(c, state["file"])
    parts = file_membership(c, rec, state["inh"], state.get("actions"))
    flat = [
        *(("users", x) for x in parts["users"]),
        *(("groups", x) for x in parts["groups"]),
        *(("invitees", x) for x in parts["invitees"]),
    ]
    offset, limit = state["offset"], state["limit"]
    out: dict[str, Any] = {"users": [], "groups": [], "invitees": []}
    for kind, item in flat[offset : offset + limit]:
        out[kind].append(item)
    if offset + limit < len(flat):
        out["cursor"] = Pager.encode({**state, "offset": offset + limit})
    return out


@route("sharing/list_file_members", scope="sharing.read")
def list_file_members(c: m.Call) -> Any:
    a = c.args
    rec, _ = locate(c, a.str("file"))
    state = {
        "t": "fmem",
        "file": rec["id"],
        "inh": a.bool("include_inherited", True),
        "limit": a.int("limit", 100, lo=1, hi=300),
        "offset": 0,
        "actions": a.raw("actions", None),
    }
    return members_page(c, state)


@route("sharing/list_file_members/continue", scope="sharing.read")
def list_file_members_continue(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"))
    if not state or state.get("t") != "fmem":
        raise api_error(tag("invalid_cursor"))
    return members_page(c, state)


@route("sharing/list_file_members/batch", scope="sharing.read")
def list_file_members_batch(c: m.Call) -> Any:
    files = c.args.list("files")
    if len(files) > 100:
        raise EmulatorError(400, "request body: files: list is too long", media_type="text/plain")
    limit = c.args.int("limit", 10, lo=0, hi=1000)
    out = []
    for ref in files:
        try:
            rec, _ = locate(c, ref)
            parts = file_membership(c, rec, True, None)
            total = sum(len(v) for v in parts.values())
            shown = {"users": [], "groups": [], "invitees": []}
            budget = limit
            for kind in ("users", "groups", "invitees"):
                take = parts[kind][:budget]
                shown[kind] = take
                budget -= len(take)
            members = dict(shown)
            if total > limit:
                members["cursor"] = Pager.encode(
                    {
                        "t": "fmem",
                        "file": rec["id"],
                        "inh": True,
                        "limit": 100,
                        "offset": limit,
                        "actions": None,
                    }
                )
            out.append({"file": ref, "result": tag("result", members=members, member_count=total)})
        except EmulatorError as exc:
            err = (
                exc.body["error"]["access_error"]
                if isinstance(exc.body, dict) and "error" in exc.body
                else tag("other")
            )
            out.append({"file": ref, "result": tag("access_error", access_error=err)})
    return out


def need_edit(c: m.Call, rec: dict[str, Any]) -> None:
    if not m.is_writer(m.entry_role(c, rec)):
        raise file_access_error("no_permission")


@route("sharing/add_file_member", scope="sharing.write")
def add_file_member(c: m.Call) -> Any:
    a = c.args
    rec, _ = locate(c, a.str("file"))
    need_edit(c, rec)
    level, _ = a.union("access_level", ACCESS_LEVELS, default="viewer")
    if level in ("owner", "traverse", "no_access", "other"):
        level = "viewer"
    ns = c.tree.namespace(rec["ns"])
    team_only = bool(ns and ns.get("policy", {}).get("member_policy") == "team")
    out = []
    for raw in a.list("members"):
        kind, record, value = resolve_selector(c, raw)
        outcome: dict[str, Any]
        if (
            (kind == "user" and record is None)
            or (kind == "group" and record is None)
            or kind == "group"
            or (kind == "email" and not EMAIL_RE.match(value))
        ):
            outcome = tag("member_error", member_error=tag("invalid_member"))
        elif kind == "email" and team_only:
            outcome = tag("member_error", member_error=tag("no_permission"))
        else:
            existing = next(
                (
                    s
                    for s in rec["members"]
                    if (record and s.get("account_id") == record["id"])
                    or (not record and s.get("email", "").lower() == value.lower())
                ),
                None,
            )
            if existing is not None:
                existing["access_type"] = level
            elif record is not None:
                rec["members"].append(
                    {
                        "account_id": record["id"],
                        "access_type": level,
                        "created": ts(c.now()),
                        "expires": None,
                        "invited_by": c.actor.id,
                    }
                )
            else:
                rec["members"].append(
                    {
                        "email": value,
                        "access_type": level,
                        "created": ts(c.now()),
                        "expires": None,
                        "invited_by": c.actor.id,
                    }
                )
            outcome = tag("success", success=access_tag(level))
        out.append({"member": raw, "result": outcome})
    return out


def find_explicit(
    c: m.Call, rec: dict[str, Any], raw: Any
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    _kind, record, value = resolve_selector(c, raw)
    for share in rec["members"]:
        if record and share.get("account_id") == record["id"]:
            return share, record
        if not record and share.get("email", "").lower() == value.lower():
            return share, None
    return None, record


@route("sharing/remove_file_member_2", scope="sharing.write")
def remove_file_member_2(c: m.Call) -> Any:
    a = c.args
    rec, _ = locate(c, a.str("file"))
    need_edit(c, rec)
    share, _ = find_explicit(c, rec, a.raw("member"))
    if share is None:
        return tag("member_error", member_error=tag("no_explicit_access"))
    rec["members"] = [s for s in rec["members"] if s is not share]
    return tag("success")


@route("sharing/update_file_member", scope="sharing.write")
def update_file_member(c: m.Call) -> Any:
    a = c.args
    rec, _ = locate(c, a.str("file"))
    need_edit(c, rec)
    level, _ = a.union("access_level", ACCESS_LEVELS)
    share, _ = find_explicit(c, rec, a.raw("member"))
    if share is None:
        raise api_error(tag("no_explicit_access"))
    share["access_type"] = level
    return {"access_level": access_tag(level)}


@route("sharing/relinquish_file_membership", scope="sharing.write")
def relinquish_file_membership(c: m.Call) -> Any:
    rec, _ = locate(c, c.args.str("file"))
    mine = [s for s in rec["members"] if s.get("account_id") == c.actor.id]
    if not mine:
        raise api_error(tag("no_permission"))
    rec["members"] = [s for s in rec["members"] if s not in mine]
    return None


@route("sharing/unshare_file", scope="sharing.write")
def unshare_file(c: m.Call) -> Any:
    rec, _ = locate(c, c.args.str("file"))
    need_edit(c, rec)
    rec["members"] = []
    return None


# ---------------------------------------------------------------------------------------
# Received files
# ---------------------------------------------------------------------------------------
def received(c: m.Call) -> list[dict[str, Any]]:
    out = [
        r
        for r in c.tree.entries.values()
        if r["type"] == "file"
        and not r.get("deleted")
        and any(s.get("account_id") == c.actor.id and not m.expired(s, c.ctx) for s in r.get("members", ()))
    ]
    out.sort(key=lambda r: (r["name"].lower(), r["id"]))
    return out


def received_page(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    items = received(c)
    offset, limit = state["offset"], state["limit"]
    entries = []
    for rec in items[offset : offset + limit]:
        loc = m.loc_for_entry(c, rec)
        entries.append(shared_file_metadata(c, rec, loc, state.get("actions")))
    out: dict[str, Any] = {"entries": entries}
    if offset + limit < len(items):
        out["cursor"] = Pager.encode({**state, "offset": offset + limit})
    return out


@route("sharing/list_received_files", scope="sharing.read")
def list_received_files(c: m.Call) -> Any:
    a = c.args
    return received_page(
        c,
        {
            "t": "recv",
            "offset": 0,
            "limit": a.int("limit", 100, lo=1, hi=300),
            "actions": a.raw("actions", None),
        },
    )


@route("sharing/list_received_files/continue", scope="sharing.read")
def list_received_files_continue(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"))
    if not state or state.get("t") != "recv":
        raise api_error(tag("invalid_cursor"))
    return received_page(c, state)


__all__ = ["FILE_ACTIONS", "Args"]
