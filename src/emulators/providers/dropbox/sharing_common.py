"""Shared building blocks for ``sharing/*``: shared-folder lookup and wire shapes."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from emulators.core.errors import EmulatorError
from emulators.core.provider import url_for_host
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import BadInput, api_error, tag, union_tag

EMAIL_PATTERN = re.compile(r"^['#&A-Za-z0-9._%+-]+@[A-Za-z0-9-][A-Za-z0-9.-]*\.[A-Za-z]{2,15}$")
OWNER_ACTIONS = {
    "change_options",
    "set_access_inheritance",
    "unshare",
    "enable_viewer_info",
    "disable_viewer_info",
}


def access_tag(role: str) -> dict[str, Any]:
    return {".tag": role}


def selector_of(raw: Any) -> tuple[str, str]:
    """``("email", "x@y")`` or ``("dropbox_id", "dbid:...")`` from a MemberSelector."""
    name, payload = union_tag(raw)
    if name == "email" and isinstance(payload, str):
        if not EMAIL_PATTERN.match(payload):
            raise BadInput(
                f"request body: member: '{payload}' did not match pattern '{EMAIL_PATTERN.pattern}'"
            )
        return name, payload
    if name == "dropbox_id" and isinstance(payload, str):
        return name, payload
    raise BadInput("request body: member: expected a MemberSelector with tag 'email' or 'dropbox_id'")


def initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return "".join(p[0].upper() for p in parts[:2])


def resolve_selector(c: m.Call, raw: Any) -> tuple[str, dict[str, Any] | None, str]:
    """``(kind, account_or_group_record, raw_value)`` where kind is ``user``/``group``/``email``."""
    name, value = selector_of(raw)
    if name == "dropbox_id":
        if value.startswith("g:"):
            group = c.ctx.store.collection(m.GROUPS).get(value)
            return "group", group, value
        return "user", m.account(c.ctx, value), value
    acct = m.account_by_email(c.ctx, value)
    return ("user", acct, value) if acct is not None else ("email", None, value)


def access_error(kind: str, wrap: bool) -> EmulatorError:
    """``SharedFolderAccessError``: bare for get_folder_metadata / list_folder_members, otherwise
    wrapped as ``{"access_error": ...}`` by the route's own error union."""
    inner = {".tag": kind}
    return api_error({".tag": "access_error", "access_error": inner} if wrap else inner)


def shared_ns(c: m.Call, shared_folder_id: str, *, wrap: bool = True) -> dict[str, Any]:
    ns = c.tree.namespace(shared_folder_id)
    if ns is None or ns["type"] not in ("shared_folder", "team_folder"):
        raise access_error("invalid_id", wrap)
    if m.ns_role(c.actor, ns) is None:
        raise access_error("not_a_member", wrap)
    return ns


def policy_obj(ns: dict[str, Any]) -> dict[str, Any]:
    policy = ns.get("policy", {})
    d: dict[str, Any] = {
        "member_policy": tag(policy.get("member_policy", "anyone")),
        "resolved_member_policy": tag(policy.get("member_policy", "anyone")),
        "acl_update_policy": tag(policy.get("acl_update_policy", "owner")),
        "shared_link_policy": tag(policy.get("shared_link_policy", "anyone")),
        "viewer_info_policy": tag(policy.get("viewer_info_policy", "enabled")),
    }
    return d


def folder_actions(
    c: m.Call, ns: dict[str, Any], role: str | None, requested: list[Any]
) -> list[dict[str, Any]]:
    is_owner = role == "owner"
    is_editor = role in ("owner", "editor")
    acl_editors = ns.get("policy", {}).get("acl_update_policy") == "editors"
    can_invite = is_owner or (role == "editor" and acl_editors)
    is_team_folder = ns["type"] == "team_folder"
    out: list[dict[str, Any]] = []
    for raw in requested:
        name, _ = union_tag(raw)
        allow = True
        reason: dict[str, Any] | None = None
        if name in OWNER_ACTIONS:
            allow = is_owner and not (is_team_folder and name == "unshare")
        elif name == "edit_contents":
            allow = is_editor
        elif name in ("invite_editor", "invite_viewer", "invite_viewer_no_comment"):
            allow = can_invite and not is_team_folder
        elif name in ("relinquish_membership", "leave_a_copy"):
            allow = not is_owner and not is_team_folder
            reason = tag("target_is_owner") if is_owner else None
        elif name == "unmount":
            allow = not is_team_folder
        if not allow and reason is None:
            reason = tag("permission_denied")
        entry: dict[str, Any] = {"action": tag(name or "other"), "allow": allow}
        if not allow and reason is not None:
            entry["reason"] = reason
        out.append(entry)
    return out


def mounted_for(c: m.Call, ns_id: str, acct: dict[str, Any]) -> bool:
    for mount in c.tree.mounts_to.get(ns_id, ()):
        holder = c.tree.namespace(mount["ns"])
        if holder is None:
            continue
        if mount["ns"] == acct["home_ns"]:
            return True
        if holder["type"] in ("team_folder", "shared_folder") and any(
            u.get("account_id") == acct["id"] for u in holder.get("members", ())
        ):
            return True
    return False


def root_entry(c: m.Call, ns: dict[str, Any]) -> dict[str, Any] | None:
    mounts = c.tree.mounts_to.get(ns["id"], [])
    return sorted(mounts, key=lambda r: r["id"])[0] if mounts else None


def member_time(ns: dict[str, Any], acct_id: str) -> str:
    for member in ns.get("members", ()):
        if member["kind"] == "user" and member["account_id"] == acct_id and member.get("invited"):
            return member["invited"]
    return ns.get("created", "")


def folder_metadata(c: m.Call, ns: dict[str, Any], *, actions: list[Any] | None = None) -> dict[str, Any]:
    """Wire ``SharedFolderMetadata`` for ``ns`` as seen by the caller."""
    role = m.ns_role(c.actor, ns) or "viewer"
    root = root_entry(c, ns)
    parent_ns = c.tree.namespace(root["ns"]) if root else None
    inside_team = bool(parent_ns and parent_ns["type"] == "team_folder")
    d: dict[str, Any] = {
        "access_type": access_tag(role),
        "is_inside_team_folder": inside_team,
        "is_team_folder": ns["type"] == "team_folder",
    }
    owner = m.account(c.ctx, ns.get("owner"))
    if owner is not None:
        d["owner_display_names"] = [owner["display_name"]]
        if owner.get("is_team"):
            team = m.team_info(c.ctx)
            d["owner_team"] = {"id": team["id"], "name": team["name"]}
    elif ns["type"] == "team_folder":
        team = m.team_info(c.ctx)
        d["owner_team"] = {"id": team["id"], "name": team["name"]}
    if inside_team and parent_ns:
        d["parent_shared_folder_id"] = parent_ns["id"]
    prefix = m.locate_ns(c, ns["id"])
    if prefix is not None and prefix != ("", ""):
        d["path_display"] = prefix[1]
        d["path_lower"] = prefix[0]
    d["name"] = ns["name"]
    if actions is not None:
        d["permissions"] = folder_actions(c, ns, role, actions)
    d["policy"] = policy_obj(ns)
    d["preview_url"] = url_for_host(
        c.request, c.ctx, "www.dropbox.com", "/home" + quote(d.get("path_display", "/" + ns["name"]))
    )
    d["shared_folder_id"] = ns["id"]
    d["time_invited"] = member_time(ns, c.actor.id)
    d["access_inheritance"] = tag(ns.get("access_inheritance", "inherit"))
    if root is not None:
        d["folder_id"] = root["id"]
    return d


def group_info(c: m.Call, group: dict[str, Any]) -> dict[str, Any]:
    member_id = c.actor.account.get("team_member_id")
    return {
        "group_name": group["name"],
        "group_id": group["id"],
        "group_external_id": group.get("external_id"),
        "member_count": len(group["member_ids"]),
        "group_management_type": tag(group.get("management_type", "company_managed")),
        "group_type": tag("team"),
        "is_member": member_id in group["member_ids"] if member_id else False,
        "is_owner": False,
        "same_team": bool(c.actor.account.get("is_team")),
    }


def member_actions(
    c: m.Call, ns: dict[str, Any], target_role: str, requested: list[Any], *, is_group: bool = False
) -> list[dict[str, Any]]:
    role = m.ns_role(c.actor, ns)
    can_manage = role == "owner" or (
        role == "editor" and ns.get("policy", {}).get("acl_update_policy") == "editors"
    )
    out: list[dict[str, Any]] = []
    for raw in requested:
        name, _ = union_tag(raw)
        allow = can_manage and target_role != "owner" and ns["type"] != "team_folder"
        if name == "make_owner":
            allow = role == "owner" and target_role != "owner" and not is_group
        if name == "leave_a_copy":
            allow = False
        entry: dict[str, Any] = {"action": tag(name or "other"), "allow": allow}
        if not allow:
            entry["reason"] = tag("target_is_owner") if target_role == "owner" else tag("permission_denied")
        out.append(entry)
    return out


def membership(c: m.Call, ns: dict[str, Any], actions: list[Any] | None) -> dict[str, list[dict[str, Any]]]:
    """``users`` / ``groups`` / ``invitees`` lists for a shared folder or team folder."""
    users: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    invitees: list[dict[str, Any]] = []
    for member in ns.get("members", ()):
        role = member["access_type"]
        base: dict[str, Any] = {"access_type": access_tag(role)}
        if actions is not None:
            base["permissions"] = member_actions(c, ns, role, actions, is_group=member["kind"] == "group")
        if member["kind"] == "user":
            acct = m.account(c.ctx, member["account_id"])
            if acct is None:
                continue
            users.append(
                {
                    **base,
                    "initials": initials(acct["display_name"]),
                    "is_inherited": bool(member.get("inherited")),
                    "user": m.user_info(c, acct),
                }
            )
        elif member["kind"] == "group":
            group = c.ctx.store.collection(m.GROUPS).get(member["group_id"])
            if group is None:
                continue
            groups.append(
                {**base, "is_inherited": bool(member.get("inherited")), "group": group_info(c, group)}
            )
        else:
            invitees.append(
                {
                    **base,
                    "initials": initials(member["email"].split("@")[0].replace(".", " ")),
                    "is_inherited": False,
                    "invitee": tag("email", email=member["email"]),
                }
            )
    return {"users": users, "groups": groups, "invitees": invitees}
