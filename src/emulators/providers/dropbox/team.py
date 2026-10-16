"""Dropbox Business ``team/*`` routes: team info, members, groups, team folders, namespaces."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from emulators.core.errors import EmulatorError
from emulators.providers.dropbox import catalog
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import (
    Args,
    BadInput,
    Pager,
    account_id_for,
    api_error,
    disallowed_name,
    group_id_for,
    member_id_for,
    numeric_id,
    parse_ts,
    tag,
    ts,
    union_tag,
)
from emulators.providers.dropbox.dispatch import route
from emulators.providers.dropbox.files_batch import launch, poll
from emulators.providers.dropbox.ops import Ops
from emulators.providers.dropbox.team_log import audit

ADMIN_ROLES = {"Team admin", "User management admin"}


def require_admin(c: m.Call, *, any_admin: bool = False) -> None:
    roles = {r["name"] for r in c.actor.account.get("roles", ())}
    allowed = {"Team admin", "User management admin", "Support admin"} if any_admin else ADMIN_ROLES
    if not roles & allowed:
        raise EmulatorError(
            401,
            {"error_summary": "route_access_denied/...", "error": {".tag": "route_access_denied"}},
        )


def member_roles(acct: dict[str, Any]) -> list[dict[str, Any]]:
    return acct["roles"] or [catalog.role_obj("member_only")]


def admin_tier(acct: dict[str, Any]) -> dict[str, Any]:
    order = {
        "Team admin": "team_admin",
        "User management admin": "user_management_admin",
        "Support admin": "support_admin",
    }
    for role in acct["roles"]:
        if role["name"] in order:
            return tag(order[role["name"]])
    return tag("member_only")


def status_obj(acct: dict[str, Any]) -> dict[str, Any]:
    if acct["status"] == "removed":
        return tag("removed", is_recoverable=True, is_disconnected=False)
    return tag(acct["status"])


def member_profile(c: m.Call, acct: dict[str, Any]) -> dict[str, Any]:
    d: dict[str, Any] = {
        "team_member_id": acct["team_member_id"],
        "external_id": acct.get("external_id"),
        "account_id": acct["id"],
        "email": acct["email"],
        "email_verified": acct["email_verified"],
        "status": status_obj(acct),
        "name": m.name_obj(acct),
        "membership_type": tag("full"),
        "invited_on": acct.get("invited_on"),
        "joined_on": acct.get("joined_on") if acct["status"] != "invited" else None,
        "suspended_on": acct.get("suspended_on") if acct["status"] == "suspended" else None,
        "groups": list(acct["groups"]),
        "member_folder_id": acct["home_ns"],
        "root_folder_id": acct["root_ns"],
    }
    return d


def member_info_v2(c: m.Call, acct: dict[str, Any]) -> dict[str, Any]:
    return {"profile": member_profile(c, acct), "roles": member_roles(acct)}


def member_info_v1(c: m.Call, acct: dict[str, Any]) -> dict[str, Any]:
    return {"profile": member_profile(c, acct), "role": admin_tier(acct)}


def team_members(c: m.Call, include_removed: bool = False) -> list[dict[str, Any]]:
    out = [
        a
        for a in c.ctx.store.collection(m.ACCOUNTS)
        if a.get("is_team") and (include_removed or a["status"] != "removed")
    ]
    out.sort(key=lambda a: (a["display_name"].lower(), a["id"]))
    return out


def select_member(c: m.Call, raw: Any) -> dict[str, Any] | None:
    name, value = union_tag(raw)
    for acct in team_members(c, include_removed=True):
        if name == "team_member_id" and acct["team_member_id"] == value:
            return acct
        if name == "external_id" and acct.get("external_id") == value:
            return acct
        if name == "email" and acct["email"].lower() == str(value).lower():
            return acct
    return None


# ---------------------------------------------------------------------------------------
# Team info
# ---------------------------------------------------------------------------------------
@route("team/get_info", auth="team", scope="team_info.read", void_arg=True)
def get_info(c: m.Call) -> Any:
    team = m.team_info(c.ctx)
    members = team_members(c)
    return {
        "name": team["name"],
        "team_id": team["id"],
        "num_licensed_users": team["num_licensed_users"],
        "num_provisioned_users": len(members),
        "num_used_licenses": sum(1 for a in members if a["status"] == "active"),
        "policies": {
            "sharing": catalog.sharing_policies(team["policies"]),
            "emm_state": tag(team["emm_state"]),
            "office_addin": tag(team["office_addin"]),
            "suggest_members_policy": tag(team["suggest_members_policy"]),
            "top_level_content_policy": tag(team["top_level_content_policy"]),
        },
    }


@route("team/token/get_authenticated_admin", auth="team", scope="team_info.read", void_arg=True)
def get_authenticated_admin(c: m.Call) -> Any:
    acct = c.actor.account
    if not acct.get("is_team") or not acct.get("roles"):
        raise api_error(tag("mapping_not_found"))
    if acct["status"] != "active":
        raise api_error(tag("admin_not_active"))
    return {"admin_profile": member_profile(c, acct)}


@route("team/features/get_values", auth="team", scope="team_info.read")
def team_features_get_values(c: m.Call) -> Any:
    features = c.args.list("features")
    if not features:
        raise api_error(tag("empty_features_list"))
    values = []
    for raw in features:
        name, _ = union_tag(raw)
        if name == "upload_api_rate_limit":
            values.append(tag(name, **{name: tag("unlimited")}))
        elif name == "has_team_shared_dropbox":
            values.append(tag(name, **{name: tag(name, **{name: False})}))
        elif name == "has_team_file_events":
            values.append(tag(name, **{name: tag("enabled", enabled=True)}))
        elif name == "has_team_selective_sync" or name == "has_distinct_member_homes":
            values.append(tag(name, **{name: tag(name, **{name: True})}))
        else:
            values.append(tag("other"))
    return {"values": values}


# ---------------------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------------------
def members_page(c: m.Call, state: dict[str, Any], v2: bool) -> dict[str, Any]:
    items = team_members(c, state["rm"])
    offset, limit = state["offset"], state["limit"]
    chunk = items[offset : offset + limit]
    more = offset + limit < len(items)
    build = member_info_v2 if v2 else member_info_v1
    return {
        "members": [build(c, a) for a in chunk],
        "cursor": Pager.encode({**state, "offset": offset + limit})
        if more
        else Pager.encode({**state, "offset": len(items)}),
        "has_more": more,
    }


def list_members(c: m.Call, v2: bool) -> Any:
    state = {
        "t": "tmembers",
        "offset": 0,
        "limit": c.args.int("limit", 1000, lo=1, hi=1000),
        "rm": c.args.bool("include_removed"),
    }
    return members_page(c, state, v2)


def continue_members(c: m.Call, v2: bool) -> Any:
    state = Pager.decode(c.args.str("cursor"))
    if not state or state.get("t") != "tmembers":
        raise api_error(tag("invalid_cursor"))
    return members_page(c, state, v2)


@route("team/members/list_v2", auth="team", scope="members.read")
def members_list_v2(c: m.Call) -> Any:
    return list_members(c, True)


@route("team/members/list/continue_v2", auth="team", scope="members.read")
def members_list_continue_v2(c: m.Call) -> Any:
    return continue_members(c, True)


@route("team/members/list", auth="team", scope="members.read")
def members_list_v1(c: m.Call) -> Any:
    return list_members(c, False)


@route("team/members/list/continue", auth="team", scope="members.read")
def members_list_continue_v1(c: m.Call) -> Any:
    return continue_members(c, False)


@route("team/members/get_info_v2", auth="team", scope="members.read")
def members_get_info_v2(c: m.Call) -> Any:
    out = []
    for raw in c.args.list("members"):
        acct = select_member(c, raw)
        if acct is None:
            out.append(tag("id_not_found", id_not_found=str(union_tag(raw)[1])))
        else:
            out.append(tag("member_info", **member_info_v2(c, acct)))
    return {"members_info": out}


@route("team/members/get_info", auth="team", scope="members.read")
def members_get_info_v1(c: m.Call) -> Any:
    out = []
    for raw in c.args.list("members"):
        acct = select_member(c, raw)
        if acct is None:
            out.append(tag("id_not_found", id_not_found=str(union_tag(raw)[1])))
        else:
            out.append(tag("member_info", **member_info_v1(c, acct)))
    return out


@route("team/members/get_available_team_member_roles", auth="team", scope="members.read", void_arg=True)
def members_available_roles(c: m.Call) -> Any:
    return {"roles": [catalog.role_obj(r["key"]) for r in catalog.ROLE_DEFS]}


def new_member_account(c: m.Call, a: Args) -> dict[str, Any]:
    email = a.str("member_email")
    given = a.str("member_given_name", None) or email.split("@")[0].split(".")[0].title()
    surname = a.str("member_surname", None) or (
        email.split("@")[0].split(".")[1].title() if "." in email.split("@")[0] else ""
    )
    person = "new-" + member_id_for(email)[-12:]
    ns_id = numeric_id(f"home/{person}")
    display = f"{given} {surname}".strip()
    now = ts(c.now())
    roles = [r for r in catalog.ROLE_DEFS if r["key"] != "member_only"]
    role_ids = {catalog.role_id(r["key"]): r["key"] for r in roles}
    acct = {
        "id": account_id_for(person),
        "person": person,
        "uid": 10_000_000 + int.from_bytes(member_id_for(person).encode()[-4:], "big") % 89_999_999,
        "email": email,
        "email_verified": False,
        "given_name": given,
        "surname": surname,
        "familiar_name": given,
        "display_name": display,
        "abbreviated_name": (given[:1] + surname[:1]).upper(),
        "country": "US",
        "locale": "en",
        "disabled": False,
        "is_team": True,
        "team_member_id": member_id_for(person),
        "status": "invited",
        "joined_on": None,
        "invited_on": now,
        "suspended_on": None,
        "groups": [],
        "roles": [catalog.role_obj(role_ids[rid]) for rid in a.list("role_ids", []) if rid in role_ids],
        "home_ns": ns_id,
        "root_ns": next(ns["id"] for ns in c.tree.ns.values() if ns["type"] == "team_root"),
        "team_id": m.team_info(c.ctx)["id"],
        "external_id": a.str("member_external_id", None),
        "referral_link": f"https://www.dropbox.com/referrals/AAB{ns_id}",
        "account_type": "business",
        "space_allocated": 2 * 1024**4,
    }
    c.ctx.store.collection(m.ACCOUNTS).put(acct)
    c.ctx.store.collection(m.NAMESPACES).put(
        {
            "id": ns_id,
            "type": "team_member_folder",
            "name": display,
            "owner": acct["id"],
            "team_id": acct["team_id"],
            "members": [],
            "status": "active",
            "created": now,
            "n": 0,
            "seq": 0,
        }
    )
    c.invalidate()
    ops = Ops(c.ctx, c.tree, c.touched)
    taken = {r["name"].lower() for r in c.tree.children(acct["root_ns"], "")}
    name = display
    if name.lower() in taken:
        from emulators.providers.dropbox.common import autorename

        name = autorename(name, taken)
    ops.create_folder(acct["root_ns"], "", name, when=c.now(), mount_ns=ns_id)
    return acct


@route("team/members/add_v2", auth="team", scope="members.write")
def members_add_v2(c: m.Call) -> Any:
    require_admin(c)
    results: list[dict[str, Any]] = []
    for raw in c.args.list("new_members"):
        a = Args(raw, "request body: new_members")
        email = a.str("member_email")
        existing = m.account_by_email(c.ctx, email)
        if existing is not None and existing.get("is_team"):
            results.append(tag("user_already_on_team", user_already_on_team=email))
            continue
        if existing is not None:
            results.append(tag("user_on_another_team", user_on_another_team=email))
            continue
        ext = a.str("member_external_id", None)
        if ext and any(x.get("external_id") == ext for x in team_members(c, True)):
            results.append(tag("duplicate_external_member_id", duplicate_external_member_id=ext))
            continue
        used = sum(1 for x in team_members(c) if x["status"] != "removed")
        if used >= m.team_info(c.ctx)["num_licensed_users"]:
            results.append(tag("team_license_limit", team_license_limit=email))
            continue
        acct = new_member_account(c, a)
        results.append(tag("success", **member_info_v2(c, acct)))
    if c.args.bool("force_async"):
        return launch(c, "members_add", {"results": results})
    return tag("complete", complete=results)


def selected(c: m.Call, raw: Any, *, active_required: bool = False) -> dict[str, Any]:
    acct = select_member(c, raw)
    if acct is None:
        raise api_error(tag("user_not_found"))
    if acct["status"] == "removed":
        raise api_error(tag("user_not_in_team"))
    return acct


@route("team/members/suspend", auth="team", scope="members.write")
def members_suspend(c: m.Call) -> Any:
    require_admin(c)
    acct = selected(c, c.args.raw("user"))
    if acct["status"] == "suspended":
        raise api_error(tag("suspend_inactive_user"))
    if (
        acct.get("roles")
        and sum(1 for a in team_members(c) if a.get("roles") and a["status"] == "active") <= 1
    ):
        raise api_error(tag("suspend_last_admin"))
    acct["status"] = "suspended"
    acct["suspended_on"] = ts(c.now())
    audit(c, "member_change_status", previous="active", new="suspended")
    return None


@route("team/members/unsuspend", auth="team", scope="members.write")
def members_unsuspend(c: m.Call) -> Any:
    require_admin(c)
    acct = selected(c, c.args.raw("user"))
    if acct["status"] != "suspended":
        raise api_error(tag("unsuspend_non_suspended_member"))
    acct["status"] = "active"
    acct["suspended_on"] = None
    return None


@route("team/members/remove", auth="team", scope="members.delete")
def members_remove(c: m.Call) -> Any:
    require_admin(c)
    acct = selected(c, c.args.raw("user"))
    if (
        acct.get("roles")
        and sum(1 for a in team_members(c) if a.get("roles") and a["status"] == "active") <= 1
    ):
        raise api_error(tag("remove_last_admin"))
    acct["status"] = "removed"
    for group in c.ctx.store.collection(m.GROUPS):
        group["member_ids"] = [x for x in group["member_ids"] if x != acct["team_member_id"]]
    acct["groups"] = []
    c.ctx.tokens.revoke_user(acct["person"])
    return launch(c, "members_remove", {})


@route("team/members/remove/job_status/get", auth="team", scope="members.delete")
def members_remove_status(c: m.Call) -> Any:
    return poll(c, "members_remove")


@route("team/members/recover", auth="team", scope="members.delete")
def members_recover(c: m.Call) -> Any:
    require_admin(c)
    acct = select_member(c, c.args.raw("user"))
    if acct is None:
        raise api_error(tag("user_not_found"))
    if acct["status"] != "removed":
        raise api_error(tag("user_unrecoverable"))
    acct["status"] = "active"
    return None


@route("team/members/send_welcome_email", auth="team", scope="members.write")
def members_send_welcome_email(c: m.Call) -> Any:
    selected(c, c.args.data)
    return None


@route("team/members/set_admin_permissions_v2", auth="team", scope="members.write")
def members_set_admin_permissions_v2(c: m.Call) -> Any:
    require_admin(c)
    acct = selected(c, c.args.raw("user"))
    ids = c.args.raw("new_roles", None)
    by_id = {catalog.role_id(r["key"]): r["key"] for r in catalog.ROLE_DEFS}
    roles = []
    for rid in ids or []:
        if rid not in by_id:
            raise api_error(tag("role_not_found"))
        if by_id[rid] != "member_only":
            roles.append(catalog.role_obj(by_id[rid]))
    if (
        not roles
        and acct["roles"]
        and sum(1 for a in team_members(c) if a.get("roles") and a["status"] == "active") <= 1
    ):
        raise api_error(tag("last_admin"))
    acct["roles"] = roles
    return {"team_member_id": acct["team_member_id"], "roles": member_roles(acct)}


@route("team/members/set_profile_v2", auth="team", scope="members.write")
def members_set_profile_v2(c: m.Call) -> Any:
    require_admin(c)
    a = c.args
    acct = selected(c, a.raw("user"))
    fields = (
        "new_email",
        "new_external_id",
        "new_given_name",
        "new_surname",
        "new_persistent_id",
        "new_is_directory_restricted",
    )
    if not any(a.has(f) for f in fields):
        raise api_error(tag("no_new_data_specified"))
    if a.has("new_email"):
        other = m.account_by_email(c.ctx, a.str("new_email"))
        if other is not None and other is not acct:
            raise api_error(tag("email_reserved_for_other_user"))
        acct["email"] = a.str("new_email")
    if a.has("new_external_id"):
        ext = a.str("new_external_id")
        if any(x.get("external_id") == ext and x is not acct for x in team_members(c, True)):
            raise api_error(tag("external_id_used_by_other_user"))
        acct["external_id"] = ext
    if a.has("new_given_name"):
        acct["given_name"] = a.str("new_given_name", nonempty=True)
        acct["familiar_name"] = acct["given_name"]
    if a.has("new_surname"):
        acct["surname"] = a.str("new_surname", nonempty=True)
    acct["display_name"] = f"{acct['given_name']} {acct['surname']}".strip()
    acct["abbreviated_name"] = (acct["given_name"][:1] + acct["surname"][:1]).upper()
    return {"member_info": member_info_v2(c, acct)}


# ---------------------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------------------
def group_summary(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_name": group["name"],
        "group_id": group["id"],
        "group_external_id": group.get("external_id"),
        "member_count": len(group["member_ids"]),
        "group_management_type": tag(group["management_type"]),
    }


def group_member_info(c: m.Call, acct: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
    profile = member_profile(c, acct)
    for key in ("groups", "member_folder_id", "root_folder_id"):
        profile.pop(key, None)
    access = "owner" if acct["team_member_id"] in group.get("owners", ()) else "member"
    return {"profile": profile, "access_type": tag(access)}


def group_full(c: m.Call, group: dict[str, Any], *, members: bool = True) -> dict[str, Any]:
    d = group_summary(group)
    if members:
        d["members"] = [
            group_member_info(c, acct, group)
            for mid in group["member_ids"]
            if (acct := m.account_by_member_id(c.ctx, mid)) is not None
        ]
    created = (
        parse_ts(group["created"]) if isinstance(group["created"], str) else datetime.fromtimestamp(0, UTC)
    )
    d["created"] = int(created.timestamp() * 1000)
    return d


def select_group(c: m.Call, raw: Any) -> dict[str, Any]:
    name, value = union_tag(raw)
    groups = c.ctx.store.collection(m.GROUPS)
    for group in groups:
        if (name == "group_id" and group["id"] == value) or (
            name == "group_external_id" and group.get("external_id") == value
        ):
            return group
    raise api_error(tag("group_not_found"))


@route("team/groups/list", auth="team", scope="groups.read")
def groups_list(c: m.Call) -> Any:
    state = {"t": "groups", "offset": 0, "limit": c.args.int("limit", 1000, lo=1, hi=1000)}
    return groups_page(c, state)


def groups_page(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    items = sorted(c.ctx.store.collection(m.GROUPS).values(), key=lambda g: (g["name"].lower(), g["id"]))
    offset, limit = state["offset"], state["limit"]
    more = offset + limit < len(items)
    return {
        "groups": [group_summary(g) for g in items[offset : offset + limit]],
        "cursor": Pager.encode({**state, "offset": offset + limit})
        if more
        else Pager.encode({**state, "offset": len(items)}),
        "has_more": more,
    }


@route("team/groups/list/continue", auth="team", scope="groups.read")
def groups_list_continue(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"))
    if not state or state.get("t") != "groups":
        raise api_error(tag("invalid_cursor"))
    return groups_page(c, state)


@route("team/groups/get_info", auth="team", scope="groups.read")
def groups_get_info(c: m.Call) -> Any:
    name, payload = union_tag(c.args.data)
    if name not in ("group_ids", "group_external_ids") or not isinstance(payload, list):
        raise BadInput("request body: expected a GroupsSelector")
    selector_tag = "group_id" if name == "group_ids" else "group_external_id"
    out = []
    for value in payload:
        try:
            group = select_group(c, {".tag": selector_tag, selector_tag: value})
            out.append(tag("group_info", **group_full(c, group)))
        except EmulatorError:
            out.append(tag("id_not_found", id_not_found=value))
    return out


def members_listing(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    group = select_group(c, state["group"])
    members = [a for mid in group["member_ids"] if (a := m.account_by_member_id(c.ctx, mid)) is not None]
    offset, limit = state["offset"], state["limit"]
    more = offset + limit < len(members)
    return {
        "members": [group_member_info(c, a, group) for a in members[offset : offset + limit]],
        "cursor": Pager.encode({**state, "offset": offset + limit})
        if more
        else Pager.encode({**state, "offset": len(members)}),
        "has_more": more,
    }


@route("team/groups/members/list", auth="team", scope="groups.read")
def groups_members_list(c: m.Call) -> Any:
    state = {
        "t": "gmembers",
        "group": c.args.raw("group"),
        "offset": 0,
        "limit": c.args.int("limit", 1000, lo=1, hi=1000),
    }
    return members_listing(c, state)


@route("team/groups/members/list/continue", auth="team", scope="groups.read")
def groups_members_list_continue(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"))
    if not state or state.get("t") != "gmembers":
        raise api_error(tag("invalid_cursor"))
    return members_listing(c, state)


@route("team/groups/create", auth="team", scope="groups.write")
def groups_create(c: m.Call) -> Any:
    require_admin(c)
    a = c.args
    name = a.str("group_name", nonempty=True)
    store = c.ctx.store.collection(m.GROUPS)
    if any(g["name"].lower() == name.lower() for g in store):
        raise api_error(tag("group_name_already_used"))
    ext = a.str("group_external_id", None)
    if ext and any(g.get("external_id") == ext for g in store):
        raise api_error(tag("external_id_already_in_use"))
    kind, _ = a.union(
        "group_management_type",
        ("user_managed", "company_managed", "system_managed", "other"),
        default="user_managed",
    )
    group = {
        "id": group_id_for(f"{c.ctx.world.seed}/{name}/{m.counter(c.ctx, 'group')}"),
        "name": name,
        "external_id": ext,
        "management_type": kind,
        "member_ids": [],
        "owners": [],
        "created": ts(c.now()),
    }
    if a.bool("add_creator_as_owner") and c.actor.account.get("team_member_id"):
        group["member_ids"].append(c.actor.account["team_member_id"])
        group["owners"].append(c.actor.account["team_member_id"])
        c.actor.account["groups"].append(group["id"])
    store.put(group)
    audit(c, "group_create", company_managed=kind == "company_managed")
    return group_full(c, group)


@route("team/groups/delete", auth="team", scope="groups.write")
def groups_delete(c: m.Call) -> Any:
    require_admin(c)
    group = select_group(c, c.args.data)
    for acct in c.ctx.store.collection(m.ACCOUNTS):
        if group["id"] in acct["groups"]:
            acct["groups"].remove(group["id"])
    for ns in c.ctx.store.collection(m.NAMESPACES):
        ns["members"] = [
            mem
            for mem in ns.get("members", ())
            if not (mem["kind"] == "group" and mem["group_id"] == group["id"])
        ]
    c.ctx.store.collection(m.GROUPS).delete(group["id"])
    return tag("complete")


@route("team/groups/update", auth="team", scope="groups.write")
def groups_update(c: m.Call) -> Any:
    require_admin(c)
    a = c.args
    group = select_group(c, a.raw("group"))
    store = c.ctx.store.collection(m.GROUPS)
    if a.has("new_group_name"):
        new_name = a.str("new_group_name", nonempty=True)
        if any(g["name"].lower() == new_name.lower() and g is not group for g in store):
            raise api_error(tag("group_name_already_used"))
        group["name"] = new_name
    if a.has("new_group_external_id"):
        ext = a.str("new_group_external_id")
        if any(g.get("external_id") == ext and g is not group for g in store):
            raise api_error(tag("external_id_already_in_use"))
        group["external_id"] = ext
    if a.has("new_group_management_type"):
        group["management_type"] = a.union(
            "new_group_management_type", ("user_managed", "company_managed", "system_managed", "other")
        )[0]
    return group_full(c, group, members=a.bool("return_members", True))


def change_members(c: m.Call, add: bool) -> Any:
    require_admin(c)
    a = c.args
    group = select_group(c, a.raw("group"))
    if group["management_type"] == "system_managed":
        raise api_error(tag("system_managed_group_disallowed"))
    selectors = (
        [Args(x, "request body: members").raw("user") for x in a.list("members")] if add else a.list("users")
    )
    accounts = []
    missing = []
    for raw in selectors:
        acct = select_member(c, raw)
        if acct is None or acct["status"] == "removed":
            missing.append(str(union_tag(raw)[1]))
        else:
            accounts.append(acct)
    if missing:
        raise api_error(tag("users_not_found", users_not_found=missing))
    if add:
        for acct in accounts:
            if acct["team_member_id"] in group["member_ids"]:
                raise api_error(tag("duplicate_user"))
        for acct in accounts:
            group["member_ids"].append(acct["team_member_id"])
            acct["groups"].append(group["id"])
        for item in a.list("members"):
            access = Args(item).union("access_type", ("member", "owner"), default="member")[0]
            if access == "owner":
                acct = select_member(c, Args(item).raw("user"))
                if acct:
                    group["owners"].append(acct["team_member_id"])
    else:
        for acct in accounts:
            if acct["team_member_id"] not in group["member_ids"]:
                raise api_error(tag("member_not_in_group"))
        for acct in accounts:
            group["member_ids"].remove(acct["team_member_id"])
            acct["groups"] = [g for g in acct["groups"] if g != group["id"]]
    # group membership changes alter who can reach team folders -> notify them
    for ns in c.ctx.store.collection(m.NAMESPACES):
        if any(mem["kind"] == "group" and mem["group_id"] == group["id"] for mem in ns.get("members", ())):
            c.touch(ns["id"])
    job = launch(c, "group_members", {})
    return {
        "group_info": group_full(c, group, members=a.bool("return_members", True)),
        "async_job_id": job["async_job_id"],
    }


@route("team/groups/members/add", auth="team", scope="groups.write")
def groups_members_add(c: m.Call) -> Any:
    return change_members(c, True)


@route("team/groups/members/remove", auth="team", scope="groups.write")
def groups_members_remove(c: m.Call) -> Any:
    return change_members(c, False)


@route("team/groups/job_status/get", auth="team", scope="groups.read")
def groups_job_status(c: m.Call) -> Any:
    job = c.ctx.store.collection(m.JOBS).get(c.args.str("async_job_id"))
    if job is None:
        raise api_error(tag("invalid_async_job_id"))
    return tag("complete")


# ---------------------------------------------------------------------------------------
# Team folders and namespaces
# ---------------------------------------------------------------------------------------
def team_folder_meta(ns: dict[str, Any]) -> dict[str, Any]:
    return {
        "team_folder_id": ns["id"],
        "name": ns["name"],
        "status": tag(ns.get("status", "active")),
        "is_team_shared_dropbox": False,
        "sync_setting": tag(ns.get("sync", "default_sync").replace("default_sync", "default")),
        "content_sync_settings": [],
    }


def team_folders(c: m.Call) -> list[dict[str, Any]]:
    out = [ns for ns in c.ctx.store.collection(m.NAMESPACES) if ns["type"] == "team_folder"]
    out.sort(key=lambda ns: (ns["name"].lower(), ns["id"]))
    return out


def team_folder_page(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    items = team_folders(c)
    offset, limit = state["offset"], state["limit"]
    more = offset + limit < len(items)
    return {
        "team_folders": [team_folder_meta(ns) for ns in items[offset : offset + limit]],
        "cursor": Pager.encode({**state, "offset": offset + limit})
        if more
        else Pager.encode({**state, "offset": len(items)}),
        "has_more": more,
    }


@route("team/team_folder/list", auth="team", scope="team_data.team_space")
def team_folder_list(c: m.Call) -> Any:
    return team_folder_page(
        c, {"t": "tfolders", "offset": 0, "limit": c.args.int("limit", 1000, lo=1, hi=1000)}
    )


@route("team/team_folder/list/continue", auth="team", scope="team_data.team_space")
def team_folder_list_continue(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"))
    if not state or state.get("t") != "tfolders":
        raise api_error(tag("invalid_cursor"))
    return team_folder_page(c, state)


@route("team/team_folder/get_info", auth="team", scope="team_data.team_space")
def team_folder_get_info(c: m.Call) -> Any:
    out = []
    for fid in c.args.list("team_folder_ids"):
        ns = c.ctx.store.collection(m.NAMESPACES).get(fid)
        if ns is None or ns["type"] != "team_folder":
            out.append(tag("id_not_found", id_not_found=fid))
        else:
            out.append(tag("team_folder_metadata", **team_folder_meta(ns)))
    return out


def folder_by_id(c: m.Call, fid: str) -> dict[str, Any]:
    ns = c.ctx.store.collection(m.NAMESPACES).get(fid)
    if ns is None or ns["type"] != "team_folder":
        raise api_error({".tag": "access_error", "access_error": tag("invalid_team_folder_id")})
    return ns


def root_mount(c: m.Call, ns: dict[str, Any]) -> dict[str, Any] | None:
    for rec in c.tree.mounts_to.get(ns["id"], ()):
        return rec
    return None


@route("team/team_folder/create", auth="team", scope="team_data.team_space")
def team_folder_create(c: m.Call) -> Any:
    require_admin(c)
    name = c.args.str("name", nonempty=True)
    if disallowed_name(name):
        raise api_error(tag("invalid_folder_name"))
    root = next(ns for ns in c.tree.ns.values() if ns["type"] == "team_root")
    if (root["id"], "/" + name.lower()) in c.tree.live:
        raise api_error(tag("folder_name_already_used"))
    ns_id = numeric_id(f"team-folder/{c.ctx.world.seed}/{m.counter(c.ctx, 'team_folder')}")
    sync = c.args.union("sync_setting", ("default", "not_synced", "other"), default="default")[0]
    ns = {
        "id": ns_id,
        "type": "team_folder",
        "name": name,
        "owner": None,
        "team_id": m.team_info(c.ctx)["id"],
        "members": [],
        "status": "active",
        "sync": "default_sync" if sync == "default" else sync,
        "created": ts(c.now()),
        "policy": {
            "acl_update_policy": "editors",
            "member_policy": "team",
            "shared_link_policy": "anyone",
            "viewer_info_policy": "enabled",
        },
        "access_inheritance": "inherit",
        "n": 0,
        "seq": 0,
    }
    c.ctx.store.collection(m.NAMESPACES).put(ns)
    Ops(c.ctx, c.tree, c.touched).create_folder(root["id"], "", name, when=c.now(), mount_ns=ns_id)
    return team_folder_meta(ns)


@route("team/team_folder/archive", auth="team", scope="team_data.team_space")
def team_folder_archive(c: m.Call) -> Any:
    require_admin(c)
    ns = folder_by_id(c, c.args.str("team_folder_id"))
    if ns.get("status") != "active":
        raise api_error({".tag": "status_error", "status_error": tag(ns["status"])})
    ns["status"] = "archived"
    c.touch(ns["id"])
    meta = team_folder_meta(ns)
    if c.args.bool("force_async_off"):
        return tag("complete", **meta)
    return launch(c, "team_folder_archive", meta)


@route("team/team_folder/archive/check", auth="team", scope="team_data.team_space")
def team_folder_archive_check(c: m.Call) -> Any:
    return poll(c, "team_folder_archive")


@route("team/team_folder/activate", auth="team", scope="team_data.team_space")
def team_folder_activate(c: m.Call) -> Any:
    require_admin(c)
    ns = folder_by_id(c, c.args.str("team_folder_id"))
    if ns.get("status") == "active":
        raise api_error({".tag": "status_error", "status_error": tag("active")})
    ns["status"] = "active"
    c.touch(ns["id"])
    return team_folder_meta(ns)


@route("team/team_folder/rename", auth="team", scope="team_data.team_space")
def team_folder_rename(c: m.Call) -> Any:
    require_admin(c)
    ns = folder_by_id(c, c.args.str("team_folder_id"))
    name = c.args.str("name", nonempty=True)
    if disallowed_name(name):
        raise api_error(tag("invalid_folder_name"))
    mount = root_mount(c, ns)
    if mount is not None:
        if (mount["ns"], "/" + name.lower()) in c.tree.live and name.lower() != mount["name"].lower():
            raise api_error(tag("folder_name_already_used"))
        Ops(c.ctx, c.tree, c.touched).relocate(mount, mount["ns"], "", name)
    ns["name"] = name
    return team_folder_meta(ns)


@route("team/team_folder/permanently_delete", auth="team", scope="team_data.team_space")
def team_folder_permanently_delete(c: m.Call) -> Any:
    require_admin(c)
    ns = folder_by_id(c, c.args.str("team_folder_id"))
    if ns.get("status") == "active":
        raise api_error({".tag": "status_error", "status_error": tag("active")})
    ops = Ops(c.ctx, c.tree, c.touched)
    mount = root_mount(c, ns)
    for rec in [r for r in c.ctx.store.collection(m.ENTRIES).values() if r["ns"] == ns["id"]]:
        c.ctx.store.collection(m.ENTRIES).delete(rec["id"])
    if mount is not None:
        ops.delete(mount, when=c.now())
        ops.purge(mount)
    c.ctx.store.collection(m.NAMESPACES).delete(ns["id"])
    return None


@route("team/team_folder/update_sync_settings", auth="team", scope="team_data.team_space")
def team_folder_update_sync_settings(c: m.Call) -> Any:
    require_admin(c)
    ns = folder_by_id(c, c.args.str("team_folder_id"))
    if c.args.has("sync_setting"):
        sync = c.args.union("sync_setting", ("default", "not_synced", "other"))[0]
        ns["sync"] = "default_sync" if sync == "default" else sync
    return team_folder_meta(ns)


NS_TYPES = {
    "team_member_folder": "team_member_folder",
    "team_folder": "team_folder",
    "shared_folder": "shared_folder",
}


def namespaces_page(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    items = [
        ns for ns in c.ctx.store.collection(m.NAMESPACES) if ns["type"] in NS_TYPES and ns.get("team_id")
    ]
    items.sort(key=lambda ns: (ns["type"], ns["name"].lower(), ns["id"]))
    offset, limit = state["offset"], state["limit"]
    more = offset + limit < len(items)
    out = []
    for ns in items[offset : offset + limit]:
        d: dict[str, Any] = {
            "name": ns["name"],
            "namespace_id": ns["id"],
            "namespace_type": tag(NS_TYPES[ns["type"]]),
        }
        if ns["type"] == "team_member_folder":
            owner = m.account(c.ctx, ns["owner"])
            if owner is not None:
                d["team_member_id"] = owner["team_member_id"]
        out.append(d)
    return {
        "namespaces": out,
        "cursor": Pager.encode({**state, "offset": offset + limit})
        if more
        else Pager.encode({**state, "offset": len(items)}),
        "has_more": more,
    }


@route("team/namespaces/list", auth="team", scope="team_data.member")
def namespaces_list(c: m.Call) -> Any:
    return namespaces_page(
        c, {"t": "namespaces", "offset": 0, "limit": c.args.int("limit", 1000, lo=1, hi=1000)}
    )


@route("team/namespaces/list/continue", auth="team", scope="team_data.member")
def namespaces_list_continue(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"))
    if not state or state.get("t") != "namespaces":
        raise api_error(tag("invalid_cursor"))
    return namespaces_page(c, state)
