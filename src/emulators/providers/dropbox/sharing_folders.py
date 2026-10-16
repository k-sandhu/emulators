"""``sharing/*`` for folders: share, list, members, mount/unmount, unshare, policies."""

from __future__ import annotations

from typing import Any

from emulators.core.errors import EmulatorError
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import (
    WRITE_OR_ID,
    Args,
    Pager,
    api_error,
    autorename,
    numeric_id,
    tag,
    ts,
)
from emulators.providers.dropbox.dispatch import route
from emulators.providers.dropbox.files_batch import launch, poll
from emulators.providers.dropbox.ops import Ops
from emulators.providers.dropbox.sharing_common import (
    EMAIL_PATTERN,
    access_tag,
    folder_metadata,
    membership,
    mounted_for,
    resolve_selector,
    shared_ns,
)
from emulators.providers.dropbox.team_log import audit

ACCESS_LEVELS = ("owner", "editor", "viewer", "viewer_no_comment", "traverse", "no_access", "other")
EMAIL_RE = EMAIL_PATTERN


def access_error(kind: str) -> EmulatorError:
    return api_error({".tag": "access_error", "access_error": {".tag": kind}})


def can_manage_members(c: m.Call, ns: dict[str, Any], role: str | None) -> bool:
    if ns["type"] == "team_folder":
        return c.actor.admin  # team folders are managed by admins (Dropbox-API-Select-Admin)
    if role == "owner":
        return True
    return role == "editor" and ns.get("policy", {}).get("acl_update_policy") == "editors"


# ---------------------------------------------------------------------------------------
# share_folder
# ---------------------------------------------------------------------------------------
def new_shared_ns(c: m.Call, rec: dict[str, Any], a: Args) -> dict[str, Any]:
    ns_id = numeric_id(f"shared/{c.ctx.world.seed}/{m.counter(c.ctx, 'shared_ns')}")
    team = c.actor.account.get("is_team")
    policy = {
        "member_policy": a.union(
            "member_policy", ("team", "anyone", "team_and_approved", "other"), default="anyone"
        )[0],
        "acl_update_policy": a.union("acl_update_policy", ("owner", "editors", "other"), default="owner")[0],
        "shared_link_policy": a.union(
            "shared_link_policy", ("anyone", "team", "members", "other"), default="anyone"
        )[0],
        "viewer_info_policy": a.union(
            "viewer_info_policy", ("enabled", "disabled", "other"), default="enabled"
        )[0],
    }
    if (
        team
        and m.team_info(c.ctx)["policies"]["shared_folder_member_policy"] == "team"
        and policy["member_policy"] == "anyone"
    ):
        policy["member_policy"] = "team"
    ns = {
        "id": ns_id,
        "type": "shared_folder",
        "name": rec["name"],
        "owner": c.actor.id,
        "team_id": m.team_info(c.ctx)["id"] if team else None,
        "members": [
            {
                "kind": "user",
                "account_id": c.actor.id,
                "access_type": "owner",
                "inherited": False,
                "invited": ts(c.now()),
            }
        ],
        "status": "active",
        "created": ts(c.now()),
        "policy": policy,
        "access_inheritance": a.union(
            "access_inheritance", ("inherit", "no_inherit", "other"), default="inherit"
        )[0],
        "n": 0,
        "seq": 0,
    }
    c.ctx.store.collection(m.NAMESPACES).put(ns)
    return ns


def share_folder_impl(c: m.Call, a: Args) -> dict[str, Any]:
    path = a.str("path", pattern=WRITE_OR_ID)
    loc = m.resolve(c, path, allow_root=False)
    if loc is None or loc.rec is None:
        raise api_error({".tag": "bad_path", "bad_path": {".tag": "invalid_path"}})
    rec = loc.rec
    if rec["type"] == "file":
        raise api_error({".tag": "bad_path", "bad_path": {".tag": "is_file"}})
    if rec.get("mount_ns"):
        target = c.tree.namespace(rec["mount_ns"])
        if target is not None and target["type"] in ("shared_folder", "team_folder"):
            meta = folder_metadata(c, target)
            raise api_error({".tag": "bad_path", "bad_path": {".tag": "already_shared", **meta}})
        raise api_error({".tag": "bad_path", "bad_path": {".tag": "invalid_path"}})
    holder = c.tree.namespace(rec["ns"])
    assert holder is not None
    if holder["type"] == "shared_folder":
        raise api_error({".tag": "bad_path", "bad_path": {".tag": "inside_shared_folder"}})
    if holder["type"] == "team_root" or not m.is_writer(m.ns_role(c.actor, holder)):
        raise api_error(tag("no_permission"))
    for item in c.tree.subtree(rec["ns"], rec["path"]):
        if item.get("mount_ns"):
            target = c.tree.namespace(item["mount_ns"])
            kind = (
                "contains_team_folder"
                if target and target["type"] == "team_folder"
                else "contains_shared_folder"
            )
            raise api_error({".tag": "bad_path", "bad_path": {".tag": kind}})
    team_policy = m.team_info(c.ctx)["policies"]
    if (
        a.has("member_policy")
        and a.union("member_policy")[0] == "anyone"
        and team_policy["shared_folder_member_policy"] == "team"
    ):
        raise api_error(tag("team_policy_disallows_member_policy"))
    ns = new_shared_ns(c, rec, a)
    Ops(c.ctx, c.tree, c.touched).convert_to_shared(rec, ns["id"])
    audit(c, "shared_folder_create", rec, ns_id=ns["id"])
    c.invalidate()
    ns = c.tree.namespace(ns["id"]) or ns
    actions = a.raw("actions", None)
    return folder_metadata(c, ns, actions=actions)


@route("sharing/share_folder", scope="sharing.write")
def share_folder(c: m.Call) -> Any:
    meta = share_folder_impl(c, c.args)
    if c.args.bool("force_async"):
        return launch(c, "share", meta)
    return tag("complete", **meta)


@route("sharing/check_share_job_status", scope="sharing.write")
def check_share_job_status(c: m.Call) -> Any:
    return poll(c, "share")


@route("sharing/check_job_status", scope="sharing.write")
def check_job_status(c: m.Call) -> Any:
    job = c.ctx.store.collection(m.JOBS).get(c.args.str("async_job_id"))
    if job is None:
        raise api_error(tag("invalid_async_job_id"))
    return tag("complete")


@route("sharing/check_remove_member_job_status", scope="sharing.write")
def check_remove_member_job_status(c: m.Call) -> Any:
    return poll(c, "remove_member")


# ---------------------------------------------------------------------------------------
# list / metadata
# ---------------------------------------------------------------------------------------
def visible_shared(c: m.Call) -> list[dict[str, Any]]:
    out = [
        ns
        for ns in c.tree.ns.values()
        if ns["type"] in ("shared_folder", "team_folder") and m.ns_role(c.actor, ns) is not None
    ]
    out.sort(key=lambda ns: (ns["name"].lower(), ns["id"]))
    return out


def list_page(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    items = visible_shared(c)
    offset, limit = state["offset"], state["limit"]
    chunk = items[offset : offset + limit]
    out: dict[str, Any] = {"entries": [folder_metadata(c, ns, actions=state.get("actions")) for ns in chunk]}
    if offset + limit < len(items):
        out["cursor"] = Pager.encode({**state, "offset": offset + limit}, c)
    return out


def list_folders_args(c: m.Call) -> dict[str, Any]:
    return {
        "t": "folders",
        "a": c.actor.id,
        "offset": 0,
        "limit": c.args.int("limit", 1000, lo=1, hi=1000),
        "actions": c.args.raw("actions", None),
    }


@route("sharing/list_folders", scope="sharing.read")
def list_folders(c: m.Call) -> Any:
    return list_page(c, list_folders_args(c))


@route("sharing/list_folders/continue", scope="sharing.read")
def list_folders_continue(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"), c)
    if not state or state.get("t") != "folders":
        raise api_error(tag("invalid_cursor"))
    return list_page(c, state)


@route("sharing/list_mountable_folders", scope="sharing.read")
def list_mountable_folders(c: m.Call) -> Any:
    return list_page(c, list_folders_args(c))


@route("sharing/list_mountable_folders/continue", scope="sharing.read")
def list_mountable_folders_continue(c: m.Call) -> Any:
    return list_folders_continue(c)


@route("sharing/get_folder_metadata", scope="sharing.read")
def get_folder_metadata(c: m.Call) -> Any:
    ns = shared_ns(c, c.args.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"), wrap=False)
    return folder_metadata(c, ns, actions=c.args.raw("actions", None))


# ---------------------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------------------
@route("sharing/list_folder_members", scope="sharing.read")
def list_folder_members(c: m.Call) -> Any:
    ns = shared_ns(c, c.args.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"), wrap=False)
    limit = c.args.int("limit", 1000, lo=1, hi=1000)
    actions = c.args.raw("actions", None)
    return members_page(c, {"t": "fmembers", "ns": ns["id"], "offset": 0, "limit": limit, "actions": actions})


def members_page(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    ns = shared_ns(c, state["ns"], wrap=state.get("wrap", False))
    parts = membership(c, ns, state.get("actions"))
    flat: list[tuple[str, dict[str, Any]]] = [
        *(("users", u) for u in parts["users"]),
        *(("groups", g) for g in parts["groups"]),
        *(("invitees", i) for i in parts["invitees"]),
    ]
    offset, limit = state["offset"], state["limit"]
    chunk = flat[offset : offset + limit]
    out: dict[str, Any] = {"users": [], "groups": [], "invitees": []}
    for kind, item in chunk:
        out[kind].append(item)
    if offset + limit < len(flat):
        out["cursor"] = Pager.encode({**state, "offset": offset + limit}, c)
    return out


@route("sharing/list_folder_members/continue", scope="sharing.read")
def list_folder_members_continue(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"), c)
    if not state or state.get("t") != "fmembers":
        raise api_error(tag("invalid_cursor"))
    return members_page(c, {**state, "wrap": True})


def member_ref(
    c: m.Call, ns: dict[str, Any], raw: Any
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """``(kind, member entry in ns or None, account/group record)`` for a MemberSelector."""
    kind, record, value = resolve_selector(c, raw)
    for member in ns["members"]:
        if kind == "user" and record and member["kind"] == "user" and member["account_id"] == record["id"]:
            return kind, member, record
        if kind == "group" and record and member["kind"] == "group" and member["group_id"] == record["id"]:
            return kind, member, record
        if kind == "email" and member["kind"] == "invitee" and member["email"].lower() == value.lower():
            return kind, member, None
        if (
            kind == "user"
            and record
            and member["kind"] == "invitee"
            and member["email"].lower() == record["email"].lower()
        ):
            return kind, member, record
    return kind, None, record


@route("sharing/add_folder_member", scope="sharing.write")
def add_folder_member(c: m.Call) -> Any:
    a = c.args
    ns = shared_ns(c, a.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"))
    role = m.ns_role(c.actor, ns)
    if ns["type"] == "team_folder" and not c.actor.admin:
        raise api_error(tag("team_folder"))
    if not can_manage_members(c, ns, role):
        raise api_error(tag("no_permission"))
    entries = []
    for raw in a.list("members"):
        item = Args(raw, "request body: members")
        entries.append((item.raw("member"), item.union("access_level", ACCESS_LEVELS, default="viewer")[0]))
    team_only = ns.get("policy", {}).get("member_policy") == "team"
    staged: list[dict[str, Any]] = []
    for selector, level in entries:
        if level in ("owner", "no_access", "traverse", "other"):
            level = "viewer"
        kind, record, value = resolve_selector(c, selector)
        existing = next(
            (mem for mem in staged + ns["members"] if _same_member(mem, kind, record, value)), None
        )
        if existing is not None:
            continue
        if kind == "user":
            if record is None:
                raise api_error(
                    {".tag": "bad_member", "bad_member": tag("invalid_dropbox_id", invalid_dropbox_id=value)}
                )
            if team_only and not record.get("is_team"):
                raise api_error(tag("cant_share_outside_team"))
            staged.append(
                {
                    "kind": "user",
                    "account_id": record["id"],
                    "access_type": level,
                    "inherited": False,
                    "invited": ts(c.now()),
                    "inviter": c.actor.id,
                }
            )
        elif kind == "group":
            if record is None:
                raise api_error(
                    {".tag": "bad_member", "bad_member": tag("invalid_dropbox_id", invalid_dropbox_id=value)}
                )
            staged.append(
                {
                    "kind": "group",
                    "group_id": record["id"],
                    "access_type": level,
                    "inherited": False,
                    "invited": ts(c.now()),
                    "inviter": c.actor.id,
                }
            )
        else:
            if not EMAIL_RE.match(value):
                raise api_error(
                    {".tag": "bad_member", "bad_member": tag("invalid_email", invalid_email=value)}
                )
            if team_only:
                raise api_error(tag("cant_share_outside_team"))
            staged.append(
                {
                    "kind": "invitee",
                    "email": value,
                    "access_type": level,
                    "inherited": False,
                    "invited": ts(c.now()),
                    "inviter": c.actor.id,
                }
            )
    ns["members"].extend(staged)
    c.touch(ns["id"])
    return None


def _same_member(member: dict[str, Any], kind: str, record: dict[str, Any] | None, value: str) -> bool:
    if kind == "user" and record:
        return member["kind"] == "user" and member["account_id"] == record["id"]
    if kind == "group" and record:
        return member["kind"] == "group" and member["group_id"] == record["id"]
    return member["kind"] == "invitee" and member.get("email", "").lower() == value.lower()


def remove_mounts_of(c: m.Call, ns_id: str, acct: dict[str, Any]) -> None:
    ops = Ops(c.ctx, c.tree, c.touched)
    for mount in list(c.tree.mounts_to.get(ns_id, ())):
        if mount["ns"] == acct["home_ns"]:
            ops.delete(mount, when=c.now(), by=acct["id"])


@route("sharing/remove_folder_member", scope="sharing.write")
def remove_folder_member(c: m.Call) -> Any:
    a = c.args
    ns = shared_ns(c, a.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"))
    if ns["type"] == "team_folder" and not c.actor.admin:
        raise api_error(tag("team_folder"))
    role = m.ns_role(c.actor, ns)
    if not can_manage_members(c, ns, role):
        raise api_error(tag("no_permission"))
    kind, member, record = member_ref(c, ns, a.raw("member"))
    if member is None:
        raise api_error({".tag": "member_error", "member_error": tag("not_a_member")})
    if member["access_type"] == "owner":
        raise api_error(tag("folder_owner"))
    ns["members"] = [mem for mem in ns["members"] if mem is not member]
    if record is not None and kind == "user":
        remove_mounts_of(c, ns["id"], record)
    c.invalidate()
    c.touch(ns["id"])
    return launch(c, "remove_member", {})


@route("sharing/update_folder_member", scope="sharing.write")
def update_folder_member(c: m.Call) -> Any:
    a = c.args
    ns = shared_ns(c, a.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"))
    role = m.ns_role(c.actor, ns)
    if not can_manage_members(c, ns, role):
        raise api_error(tag("no_permission"))
    level, _ = a.union("access_level", ACCESS_LEVELS)
    _kind, member, _record = member_ref(c, ns, a.raw("member"))
    if member is None:
        raise api_error({".tag": "member_error", "member_error": tag("not_a_member")})
    if member["access_type"] == "owner" or level in ("owner", "no_access", "traverse", "other"):
        raise api_error(tag("no_permission"))
    member["access_type"] = level
    c.touch(ns["id"])
    return {"access_level": access_tag(level)}


@route("sharing/mount_folder", scope="sharing.write")
def mount_folder(c: m.Call) -> Any:
    ns = shared_ns(c, c.args.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"))
    if ns["type"] == "team_folder":
        raise api_error(tag("must_automount"))
    acct = c.actor.account
    if mounted_for(c, ns["id"], acct):
        raise api_error(tag("already_mounted"))
    taken = {r["name"].lower() for r in c.tree.children(acct["home_ns"], "")}
    name = ns["name"] if ns["name"].lower() not in taken else autorename(ns["name"], taken)
    Ops(c.ctx, c.tree, c.touched).create_folder(
        acct["home_ns"], "", name, when=c.now(), owner=acct["id"], mount_ns=ns["id"]
    )
    c.invalidate()
    return folder_metadata(c, c.tree.namespace(ns["id"]) or ns)


@route("sharing/unmount_folder", scope="sharing.write")
def unmount_folder(c: m.Call) -> Any:
    ns = shared_ns(c, c.args.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"))
    if ns["type"] == "team_folder":
        raise api_error(tag("not_unmountable"))
    acct = c.actor.account
    mounts = [mt for mt in c.tree.mounts_to.get(ns["id"], ()) if mt["ns"] == acct["home_ns"]]
    if not mounts:
        raise api_error(tag("not_unmountable"))
    ops = Ops(c.ctx, c.tree, c.touched)
    for mount in mounts:
        ops.delete(mount, when=c.now(), by=acct["id"])
    return None


@route("sharing/unshare_folder", scope="sharing.write")
def unshare_folder(c: m.Call) -> Any:
    ns = shared_ns(c, c.args.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"))
    if ns["type"] == "team_folder":
        raise api_error(tag("team_folder"))
    if m.ns_role(c.actor, ns) != "owner":
        raise api_error(tag("no_permission"))
    owner = m.account(c.ctx, ns["owner"])
    ops = Ops(c.ctx, c.tree, c.touched)
    mounts = list(c.tree.mounts_to.get(ns["id"], ()))
    owner_mount = next((mt for mt in mounts if owner is not None and mt["ns"] == owner["home_ns"]), None)
    if owner_mount is None:  # shared from inside a team folder
        owner_mount = next(
            (mt for mt in mounts if (c.tree.namespace(mt["ns"]) or {}).get("type") != "team_member_folder"),
            None,
        )
    for mount in mounts:
        if mount is not owner_mount:
            ops.delete(mount, when=c.now(), by=c.actor.id)
    if owner_mount is not None:
        ops.convert_from_shared(owner_mount)
    c.touched.add(ns["id"])
    c.ctx.store.collection(m.NAMESPACES).delete(ns["id"])
    c.invalidate()
    return tag("complete")


@route("sharing/update_folder_policy", scope="sharing.write")
def update_folder_policy(c: m.Call) -> Any:
    a = c.args
    ns = shared_ns(c, a.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"))
    if m.ns_role(c.actor, ns) != "owner" or ns["type"] == "team_folder":
        raise api_error(tag("no_permission"))
    policy = ns.setdefault("policy", {})
    for key, choices in (
        ("member_policy", ("team", "anyone", "team_and_approved", "other")),
        ("acl_update_policy", ("owner", "editors", "other")),
        ("shared_link_policy", ("anyone", "team", "members", "other")),
        ("viewer_info_policy", ("enabled", "disabled", "other")),
    ):
        if a.has(key):
            policy[key] = a.union(key, choices)[0]
    return folder_metadata(c, ns, actions=a.raw("actions", None))


@route("sharing/set_access_inheritance", scope="sharing.write")
def set_access_inheritance(c: m.Call) -> Any:
    a = c.args
    ns = shared_ns(c, a.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"))
    if m.ns_role(c.actor, ns) != "owner":
        raise api_error(tag("no_permission"))
    ns["access_inheritance"] = a.union(
        "access_inheritance", ("inherit", "no_inherit", "other"), default="inherit"
    )[0]
    return tag("complete", **folder_metadata(c, ns))


@route("sharing/relinquish_folder_membership", scope="sharing.write")
def relinquish_folder_membership(c: m.Call) -> Any:
    a = c.args
    ns = shared_ns(c, a.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"))
    if ns["type"] == "team_folder":
        raise api_error(tag("team_folder"))
    me = c.actor.id
    member = next((mem for mem in ns["members"] if mem["kind"] == "user" and mem["account_id"] == me), None)
    if member is None:
        raise api_error(tag("no_explicit_access"))
    if member["access_type"] == "owner":
        raise api_error(tag("folder_owner"))
    ns["members"] = [mem for mem in ns["members"] if mem is not member]
    remove_mounts_of(c, ns["id"], c.actor.account)
    c.touch(ns["id"])
    c.invalidate()
    return tag("complete")


@route("sharing/transfer_folder", scope="sharing.write")
def transfer_folder(c: m.Call) -> Any:
    a = c.args
    ns = shared_ns(c, a.str("shared_folder_id", pattern=r"[-_0-9a-zA-Z:]+"))
    if m.ns_role(c.actor, ns) != "owner" or ns["type"] == "team_folder":
        raise api_error(tag("no_permission"))
    target_id = a.str("to_dropbox_id")
    target = next(
        (mem for mem in ns["members"] if mem["kind"] == "user" and mem["account_id"] == target_id), None
    )
    if target is None:
        raise api_error({".tag": "new_owner_not_a_member"})
    for mem in ns["members"]:
        if mem["kind"] == "user" and mem["access_type"] == "owner":
            mem["access_type"] = "editor"
    target["access_type"] = "owner"
    ns["owner"] = target_id
    return None
