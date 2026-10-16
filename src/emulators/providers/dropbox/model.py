"""Dropbox data model on top of the emulator store: namespaces, entries, the per-request
:class:`Tree` index, access control and path resolution.

Everything the API serves comes from three record kinds, all plain dicts in the store:

``namespaces``  a root of files: a member's home (``team_member_folder`` / ``personal``), a team
                folder, a shared folder or the team-space root (``team_root``).
``entries``     files and folders. An entry belongs to one namespace and has a lowercase path
                relative to it. A folder with ``mount_ns`` set is a *mount point*: a shared
                folder / team folder / member folder that lives in another namespace. Walking a
                path crosses mount points exactly like real Dropbox crosses namespaces.
``accounts``    people (team members and external collaborators).

What a caller can see depends on a *path root* (home, team root or a namespace id), so display
paths are always computed relative to the caller's view; items outside it have no ``path_*``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext
from emulators.providers.dropbox.common import (
    Args,
    b64url,
    digest,
    path_error,
    revision_for,
    ts,
)

if TYPE_CHECKING:
    from starlette.requests import Request

ENTRIES = "entries"
NAMESPACES = "namespaces"
ACCOUNTS = "accounts"
GROUPS = "groups"
CHANGES = "changes"
LINKS = "links"
JOBS = "jobs"
SESSIONS = "upload_sessions"
TEXT = "search_text"
TEMP_LINKS = "temp_links"
WEBHOOKS = "webhooks"
AUDIT = "audit_events"
COPY_REFS = "copy_references"

RANK = {"traverse": 0, "viewer_no_comment": 1, "viewer": 2, "editor": 3, "owner": 4}
WRITE_ROLES = ("editor", "owner")


def rank(role: str | None) -> int:
    return -1 if role is None else RANK.get(role, -1)


def best(a: str | None, b: str | None) -> str | None:
    return a if rank(a) >= rank(b) else b


# ---------------------------------------------------------------------------------------
# Counters, clock and change log
# ---------------------------------------------------------------------------------------
def now(ctx: ProviderContext) -> datetime:
    return ctx.clock.now().astimezone(UTC).replace(microsecond=0)


def counter(ctx: ProviderContext, name: str) -> int:
    counters = ctx.store.meta.setdefault("counters", {})
    counters[name] = counters.get(name, 0) + 1
    return counters[name]


def current_seq(ctx: ProviderContext) -> int:
    return int(ctx.store.meta.get("seq", 0))


def next_seq(ctx: ProviderContext) -> int:
    ctx.store.meta["seq"] = current_seq(ctx) + 1
    return ctx.store.meta["seq"]


def parent_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def new_rev(ctx: ProviderContext) -> str:
    return revision_for(f"{ctx.world.seed}/{counter(ctx, 'rev')}")


def new_hex(ctx: ProviderContext, kind: str, n: int = 32) -> str:
    return digest("dbx-hex", ctx.world.seed, kind, counter(ctx, kind)).hex()[:n]


def new_token(ctx: ProviderContext, kind: str, n: int = 24) -> str:
    return b64url(digest("dbx-tok", ctx.world.seed, kind, counter(ctx, kind)))[:n]


# ---------------------------------------------------------------------------------------
# Tree: a per-request index over the entries
# ---------------------------------------------------------------------------------------
class Tree:
    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx
        store = ctx.store
        self.entries = store.collection(ENTRIES)
        self.ns = store.collection(NAMESPACES)
        self.live: dict[tuple[str, str], dict[str, Any]] = {}
        self.kids: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.mounts_to: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.dead: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for rec in self.entries.values():
            key = (rec["ns"], rec["path"])
            if rec.get("deleted"):
                self.dead[key].append(rec)
                continue
            self.live[key] = rec
            self.kids[(rec["ns"], parent_of(rec["path"]))].append(rec)
            if rec.get("mount_ns"):
                self.mounts_to[rec["mount_ns"]].append(rec)

    def namespace(self, ns_id: str) -> dict[str, Any] | None:
        return self.ns.get(ns_id)

    def add(self, rec: dict[str, Any]) -> None:
        """Index a (live) entry that was just created or moved."""
        key = (rec["ns"], rec["path"])
        self.live[key] = rec
        self.kids[(rec["ns"], parent_of(rec["path"]))].append(rec)
        if rec.get("mount_ns"):
            self.mounts_to[rec["mount_ns"]].append(rec)

    def drop(self, rec: dict[str, Any], *, dead: bool = False) -> None:
        """Remove an entry from the live index (it keeps its record; ``dead`` files stay findable)."""
        key = (rec["ns"], rec["path"])
        if self.live.get(key) is rec:
            del self.live[key]
        siblings = self.kids.get((rec["ns"], parent_of(rec["path"])))
        if siblings is not None and rec in siblings:
            siblings[:] = [r for r in siblings if r is not rec]
        if rec.get("mount_ns"):
            self.mounts_to[rec["mount_ns"]] = [
                r for r in self.mounts_to.get(rec["mount_ns"], []) if r is not rec
            ]
        if dead:
            self.dead[key].append(rec)

    def children(self, ns: str, rel: str) -> list[dict[str, Any]]:
        return sorted(self.kids.get((ns, rel), ()), key=lambda r: (r["name"].lower(), r["name"]))

    def dead_below(self, ns: str, rel: str, *, recursive: bool) -> list[dict[str, Any]]:
        """Deleted entries under ``rel`` that no live entry has replaced (latest deletion per path)."""
        out: dict[str, dict[str, Any]] = {}
        prefix = rel + "/"
        for (rns, rpath), recs in self.dead.items():
            if rns != ns or not recs or (rns, rpath) in self.live:
                continue
            if recursive:
                if not rpath.startswith(prefix):
                    continue
            elif parent_of(rpath) != rel:
                continue
            out[rpath] = max(recs, key=lambda r: (r.get("deleted_at") or "", r.get("seq", 0)))
        return list(out.values())

    def subtree(self, ns: str, rel: str) -> list[dict[str, Any]]:
        """Live entries strictly below ``rel`` in one namespace (parents before children)."""
        out: list[dict[str, Any]] = []
        stack = list(reversed(self.children(ns, rel)))
        while stack:
            rec = stack.pop()
            out.append(rec)
            if rec["type"] == "folder" and not rec.get("mount_ns"):
                stack.extend(reversed(self.children(ns, rec["path"])))
        return out

    def by_id(self, entry_id: str) -> dict[str, Any] | None:
        return self.entries.get(entry_id)

    def by_rev(self, rev: str) -> dict[str, Any] | None:
        for rec in self.entries.values():
            if rec["type"] == "file" and any(r["rev"] == rev for r in rec["revisions"]):
                return rec
        return None


# ---------------------------------------------------------------------------------------
# Actor and Call
# ---------------------------------------------------------------------------------------
@dataclass
class Actor:
    """Who a request acts as: the token's account, or a team member chosen with
    ``Dropbox-API-Select-User`` / ``Select-Admin``."""

    account: dict[str, Any]
    grant: Grant | None = None
    team_token: bool = False  # the token itself is a team-level token
    team_scope: bool = False  # acting at team level (no member selected)
    admin: bool = False  # Select-Admin: may reach any team-owned namespace
    root_mode: str = "home"
    root_ns: str = ""
    scopes: list[str] = field(default_factory=list)
    app_auth: bool = False

    @property
    def id(self) -> str:
        return self.account["id"]

    @property
    def is_member(self) -> bool:
        return bool(self.account.get("is_team")) and self.account.get("status") == "active"


@dataclass
class Call:
    """One Dropbox API call: parsed arguments, the actor and per-request caches."""

    ctx: ProviderContext
    request: Request
    route: str
    args: Args
    body: bytes
    actor: Actor
    request_id: str
    touched: set[str] = field(default_factory=set)
    prop_filter: list[str] | None = None  # template ids for ``include_property_groups``
    _tree: Tree | None = None
    _prefix: dict[str, tuple[str, str] | None] = field(default_factory=dict)

    @property
    def tree(self) -> Tree:
        if self._tree is None:
            self._tree = Tree(self.ctx)
        return self._tree

    def invalidate(self) -> None:
        self._tree = None
        self._prefix = {}

    @property
    def store(self):  # type: ignore[no-untyped-def]
        return self.ctx.store

    def now(self) -> datetime:
        return now(self.ctx)

    def touch(self, ns_id: str) -> None:
        self.touched.add(ns_id)


# ---------------------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------------------
def ns_role(actor: Actor, ns: dict[str, Any] | None) -> str | None:
    """The actor's access level on a whole namespace (``None`` = cannot see it)."""
    if ns is None:
        return None
    acct = actor.account
    if acct.get("status") not in ("active", None) and not actor.admin:
        return None
    kind = ns["type"]
    if kind in ("team_member_folder", "personal"):
        if ns.get("owner") == acct["id"]:
            return "owner"
        return "editor" if actor.admin and kind == "team_member_folder" else None
    if kind == "team_root":
        if actor.admin:
            return "editor"
        return "viewer" if acct.get("is_team") else None
    if ns.get("status", "active") != "active" and not actor.admin:
        return None
    role: str | None = None
    for member in ns.get("members", ()):
        if (member["kind"] == "user" and member["account_id"] == acct["id"]) or (
            member["kind"] == "group" and member["group_id"] in acct.get("groups", ())
        ):
            role = best(role, member["access_type"])
    if actor.admin and ns.get("team_id"):
        role = best(role, "editor")
    return role


def expired(share: dict[str, Any], ctx: ProviderContext) -> bool:
    exp = share.get("expires")
    return bool(exp) and exp <= ts(now(ctx))


def entry_role(c: Call, rec: dict[str, Any]) -> str | None:
    """Access to one entry: the namespace role unless inheritance is cut, plus explicit shares."""
    actor = c.actor
    role = ns_role(actor, c.tree.namespace(rec["ns"]))
    if rec.get("no_inherit"):
        role = "editor" if actor.admin and role else None
        if rec.get("owner") == actor.id:
            role = "owner"
    for share in rec.get("members", ()):
        if share.get("account_id") == actor.id and not expired(share, c.ctx):
            role = best(role, share["access_type"])
    return role


def can_see(c: Call, rec: dict[str, Any]) -> bool:
    if rec.get("mount_ns"):
        return ns_role(c.actor, c.tree.namespace(rec["mount_ns"])) is not None and (
            ns_role(c.actor, c.tree.namespace(rec["ns"])) is not None
        )
    return entry_role(c, rec) is not None


def is_writer(role: str | None) -> bool:
    return role in WRITE_ROLES


# ---------------------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------------------
@dataclass
class Loc:
    """A resolved location: a namespace plus a path in it, and where it sits in the caller's view."""

    ns: str
    rel: str  # lowercase path inside ``ns`` ("" = namespace root)
    dpath: str  # display path inside ``ns``
    rec: dict[str, Any] | None  # the entry here (the mount entry for a namespace root)
    pl: str  # view prefix of the namespace (lowercase)
    pd: str  # view prefix of the namespace (display)
    is_view_root: bool = False
    reachable: bool = True  # False for id: lookups outside the path root

    @property
    def path_lower(self) -> str:
        return (self.pl + self.rel) or "/"

    @property
    def path_display(self) -> str:
        return (self.pd + self.dpath) or "/"

    @property
    def is_ns_root(self) -> bool:
        return self.rel == ""


@dataclass
class Walk:
    found: bool
    loc: Loc
    consumed: int = 0
    rest: list[str] = field(default_factory=list)
    file_ancestor: bool = False


def split_path(path: str) -> list[str]:
    return [seg for seg in path.split("/") if seg != ""]


def locate_ns(c: Call, ns_id: str, depth: int = 0) -> tuple[str, str] | None:
    """Where namespace ``ns_id`` is mounted in the caller's view: ``(lower, display)`` prefix."""
    if ns_id in c._prefix:
        return c._prefix[ns_id]
    result: tuple[str, str] | None = None
    if ns_id == c.actor.root_ns:
        result = ("", "")
    elif depth < 6:
        for mount in sorted(c.tree.mounts_to.get(ns_id, ()), key=lambda m: m["id"]):
            parent = locate_ns(c, mount["ns"], depth + 1)
            if parent is None:
                continue
            if mount["ns"] != c.actor.root_ns and ns_role(c.actor, c.tree.namespace(mount["ns"])) is None:
                continue
            if ns_role(c.actor, c.tree.namespace(ns_id)) is None:
                continue
            result = (parent[0] + mount["path"], parent[1] + mount["dpath"])
            break
    c._prefix[ns_id] = result
    return result


def view_root(c: Call) -> Loc:
    return Loc(ns=c.actor.root_ns, rel="", dpath="", rec=None, pl="", pd="", is_view_root=True)


def walk(c: Call, ns: str, segments: list[str], pl: str = "", pd: str = "") -> Walk:
    cur, rel, dpath, rec = ns, "", "", None
    for i, seg in enumerate(segments):
        if rec is not None and rec["type"] == "file":
            return Walk(False, Loc(cur, rel, dpath, rec, pl, pd), i, segments[i:], file_ancestor=True)
        nxt = rel + "/" + seg.lower()
        child = c.tree.live.get((cur, nxt))
        if child is None or not can_see(c, child):
            return Walk(False, Loc(cur, rel, dpath, rec, pl, pd), i, segments[i:])
        if child.get("mount_ns"):
            pl, pd = pl + nxt, pd + child["dpath"]
            cur, rel, dpath, rec = child["mount_ns"], "", "", child
        else:
            rel, dpath, rec = nxt, child["dpath"], child
    return Walk(True, Loc(cur, rel, dpath, rec, pl, pd), len(segments))


def loc_for_entry(c: Call, rec: dict[str, Any]) -> Loc:
    """A :class:`Loc` for an entry found by id; ``reachable`` is False outside the path root."""
    if rec.get("mount_ns"):
        parent = locate_ns(c, rec["ns"])
        pl, pd = parent if parent else ("", "")
        return Loc(
            rec["mount_ns"], "", "", rec, pl + rec["path"], pd + rec["dpath"], reachable=parent is not None
        )
    prefix = locate_ns(c, rec["ns"])
    pl, pd = prefix if prefix else ("", "")
    return Loc(rec["ns"], rec["path"], rec["dpath"], rec, pl, pd, reachable=prefix is not None)


def not_found(kind: str = "not_found", wrapper: str = "path", **fields: Any) -> Exception:
    return path_error(kind, wrapper, **fields)


def parse_ns_path(c: Call, path: str) -> tuple[str, str]:
    """``ns:123/foo`` -> (``123``, ``/foo``)."""
    head, _, rest = path[3:].partition("/")
    return head, ("/" + rest) if rest else ""


def resolve(c: Call, path: str, *, allow_root: bool = True, wrapper: str = "path") -> Loc | None:
    """Resolve any Dropbox path form to a :class:`Loc`, or ``None`` when nothing is there.

    Forms: ``""``/``"/"`` (the path root), ``/a/b``, ``id:xyz[/sub]``, ``rev:abc`` and
    ``ns:123[/sub]`` (a namespace by id, access checked).
    """
    actor = c.actor
    if path.startswith("rev:"):
        rec = c.tree.by_rev(path[4:])
        if rec is None or entry_role(c, rec) is None:
            return None
        return loc_for_entry(c, rec)
    if path.startswith("id:"):
        head, _, rest = path.partition("/")
        rec = c.tree.by_id(head)
        if rec is None or rec.get("deleted") or not can_see(c, rec):
            return None
        loc = loc_for_entry(c, rec)
        if not rest:
            return loc
        w = walk(c, loc.ns, split_path(rest), loc.pl, loc.pd) if rec["type"] == "folder" else None
        if w is None or not w.found:
            return None
        return w.loc
    if path.startswith("ns:"):
        ns_id, rest = parse_ns_path(c, path)
        ns = c.tree.namespace(ns_id)
        if ns is None or ns_role(actor, ns) is None:
            return None
        segments = split_path(rest)
        if not segments:  # the namespace root, addressed as ``ns:123``
            return Loc(ns_id, "", "", None, "", "", is_view_root=True) if allow_root else None
        w = walk(c, ns_id, segments)
        return w.loc if w.found else None
    segments = split_path(path)
    if not segments:
        return view_root(c) if allow_root else None
    w = walk(c, actor.root_ns, segments)
    return w.loc if w.found else None


def resolve_for_write(c: Call, path: str) -> Walk:
    """Walk as far as the path exists; ``rest`` are the segments still to create."""
    actor = c.actor
    if path.startswith("ns:"):
        ns_id, rest = parse_ns_path(c, path)
        ns = c.tree.namespace(ns_id)
        if ns is None or ns_role(actor, ns) is None:
            raise path_error("not_found", "path")
        return walk(c, ns_id, split_path(rest))
    return walk(c, actor.root_ns, split_path(path))


# ---------------------------------------------------------------------------------------
# Metadata rendering
# ---------------------------------------------------------------------------------------
def _prefix_fields(loc: Loc | None, d: dict[str, Any]) -> None:
    if loc is not None and loc.reachable:
        d["path_lower"] = loc.path_lower
        d["path_display"] = loc.path_display


def file_metadata(
    c: Call,
    rec: dict[str, Any],
    *,
    revision: dict[str, Any] | None = None,
    explicit_members: bool = False,
    loc: Loc | None = None,
) -> dict[str, Any]:
    """Wire ``FileMetadata`` (with ``.tag``) for a file entry, optionally at an older revision."""
    loc = loc or loc_for_entry(c, rec)
    ver = revision or rec["revisions"][-1]
    d: dict[str, Any] = {".tag": "file", "name": rec["name"]}
    _prefix_fields(loc, d)
    d.update(
        id=rec["id"],
        client_modified=ver["client_modified"],
        server_modified=ver["server_modified"],
        rev=ver["rev"],
        size=ver["size"],
        is_downloadable=rec.get("downloadable", True),
    )
    if ver.get("content_hash") and rec.get("downloadable", True):
        d["content_hash"] = ver["content_hash"]
    ns = c.tree.namespace(rec["ns"])
    if ns is not None and ns["type"] in ("team_folder", "shared_folder"):
        role = ns_role(c.actor, ns)
        share: dict[str, Any] = {
            "read_only": not is_writer(entry_role(c, rec) or role),
            "parent_shared_folder_id": ns["id"],
        }
        if ver.get("modified_by"):
            share["modified_by"] = ver["modified_by"]
        d["sharing_info"] = share
    if rec.get("export"):
        d["export_info"] = dict(rec["export"])
    if explicit_members:
        d["has_explicit_shared_members"] = bool(rec.get("members"))
    if c.prop_filter and rec.get("props"):
        groups = [
            {"template_id": tid, "fields": [{"name": k, "value": v} for k, v in values.items()]}
            for tid, values in rec["props"].items()
            if tid in c.prop_filter
        ]
        if groups:
            d["property_groups"] = groups
    lock = rec.get("lock")
    if lock:
        info: dict[str, Any] = {
            "is_lockholder": lock["account_id"] == c.actor.id,
            "lockholder_name": lock["name"],
            "lockholder_account_id": lock["account_id"],
            "created": lock["created"],
        }
        d["file_lock_info"] = info
    return d


def folder_sharing_info(c: Call, rec: dict[str, Any]) -> dict[str, Any] | None:
    ns = c.tree.namespace(rec["ns"])
    if rec.get("mount_ns"):
        target = c.tree.namespace(rec["mount_ns"])
        role = ns_role(c.actor, target)
        info: dict[str, Any] = {"read_only": not is_writer(role)}
        if ns is not None and ns["type"] in ("team_folder", "shared_folder"):
            info["parent_shared_folder_id"] = ns["id"]
        info["shared_folder_id"] = rec["mount_ns"]
        info["traverse_only"] = role == "traverse"
        info["no_access"] = False
        return info
    if ns is not None and ns["type"] in ("team_folder", "shared_folder"):
        role = entry_role(c, rec) or ns_role(c.actor, ns)
        return {
            "read_only": not is_writer(role),
            "parent_shared_folder_id": ns["id"],
            "traverse_only": role == "traverse",
            "no_access": False,
        }
    return None


def folder_metadata(c: Call, rec: dict[str, Any], *, loc: Loc | None = None) -> dict[str, Any]:
    loc = loc or loc_for_entry(c, rec)
    d: dict[str, Any] = {".tag": "folder", "name": rec["name"]}
    _prefix_fields(loc, d)
    d["id"] = rec["id"]
    info = folder_sharing_info(c, rec)
    if info is not None:
        d["sharing_info"] = info
    return d


def deleted_metadata(c: Call, rec: dict[str, Any], *, loc: Loc | None = None) -> dict[str, Any]:
    loc = loc or loc_for_entry(c, rec)
    d: dict[str, Any] = {".tag": "deleted", "name": rec["name"]}
    _prefix_fields(loc, d)
    return d


def metadata(
    c: Call,
    rec: dict[str, Any],
    *,
    explicit_members: bool = False,
    loc: Loc | None = None,
    revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if rec.get("deleted"):
        return deleted_metadata(c, rec, loc=loc)
    if rec["type"] == "folder":
        return folder_metadata(c, rec, loc=loc)
    return file_metadata(c, rec, explicit_members=explicit_members, loc=loc, revision=revision)


def leaf(meta: dict[str, Any]) -> dict[str, Any]:
    """Drop ``.tag`` for fields whose Stone type is the leaf struct (``FileMetadata`` /
    ``FolderMetadata``) rather than the polymorphic ``Metadata`` -- Dropbox only tags the latter."""
    return {k: v for k, v in meta.items() if k != ".tag"}


# ---------------------------------------------------------------------------------------
# Accounts, groups and misc lookups
# ---------------------------------------------------------------------------------------
def account(ctx: ProviderContext, account_id: str | None) -> dict[str, Any] | None:
    return ctx.store.collection(ACCOUNTS).get(account_id)


def account_by_email(ctx: ProviderContext, email: str) -> dict[str, Any] | None:
    email = email.lower()
    for acct in ctx.store.collection(ACCOUNTS):
        if acct["email"].lower() == email:
            return acct
    return None


def account_by_member_id(ctx: ProviderContext, member_id: str) -> dict[str, Any] | None:
    for acct in ctx.store.collection(ACCOUNTS):
        if acct.get("team_member_id") == member_id:
            return acct
    return None


def account_by_person(ctx: ProviderContext, person_id: str) -> dict[str, Any] | None:
    for acct in ctx.store.collection(ACCOUNTS):
        if acct["person"] == person_id:
            return acct
    return None


def team_info(ctx: ProviderContext) -> dict[str, Any]:
    return ctx.store.meta["team"]


def name_obj(acct: dict[str, Any]) -> dict[str, Any]:
    return {
        "given_name": acct["given_name"],
        "surname": acct["surname"],
        "familiar_name": acct["familiar_name"],
        "display_name": acct["display_name"],
        "abbreviated_name": acct["abbreviated_name"],
    }


def user_info(c: Call, acct: dict[str, Any]) -> dict[str, Any]:
    """Sharing ``UserInfo`` as seen by the caller."""
    d: dict[str, Any] = {
        "account_id": acct["id"],
        "email": acct["email"],
        "display_name": acct["display_name"],
        "same_team": bool(acct.get("is_team")) and bool(c.actor.account.get("is_team")),
    }
    if d["same_team"] and acct.get("team_member_id"):
        d["team_member_id"] = acct["team_member_id"]
    return d
