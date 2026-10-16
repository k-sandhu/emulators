"""Helpers shared by the ``files/*`` and ``sharing/*`` routes: path lookup, write targets, errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from emulators.core.errors import EmulatorError
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import (
    api_error,
    autorename,
    disallowed_name,
    path_error,
    split_ext,
)
from emulators.providers.dropbox.ops import Ops


def lookup(
    c: m.Call, path: str, *, wrapper: str = "path", allow_root: bool = True, deleted_ok: bool = False
) -> m.Loc:
    """Resolve ``path`` or raise the route's ``<wrapper>/not_found`` 409."""
    loc = m.resolve(c, path, allow_root=allow_root)
    if loc is None:
        raise path_error("not_found", wrapper)
    return loc


def require_file(loc: m.Loc, wrapper: str = "path") -> dict[str, Any]:
    rec = loc.rec
    if rec is None or rec["type"] != "file":
        raise path_error("not_file", wrapper)
    return rec


def require_folder(loc: m.Loc, wrapper: str = "path") -> None:
    if loc.rec is not None and loc.rec["type"] != "folder":
        raise path_error("not_folder", wrapper)


def lookup_file(c: m.Call, path: str, *, wrapper: str = "path") -> tuple[m.Loc, dict[str, Any]]:
    loc = lookup(c, path, wrapper=wrapper, allow_root=False)
    return loc, require_file(loc, wrapper)


def mount_error(rec: dict[str, Any], tree: m.Tree) -> str | None:
    """``team_folder`` for team-owned roots; ``None`` for plain shared folders / regular entries."""
    if not rec.get("mount_ns"):
        return None
    target = tree.namespace(rec["mount_ns"])
    if target is not None and target["type"] in ("team_folder", "team_member_folder"):
        return "team_folder"
    return None


@dataclass
class Target:
    ns: str
    parent_rel: str
    missing: list[str]
    name: str
    existing: dict[str, Any] | None
    pl: str
    pd: str
    role: str | None
    ancestor_file: bool = False
    is_root: bool = False
    chain: list[str] = field(default_factory=list)

    @property
    def writable(self) -> bool:
        return m.is_writer(self.role)

    def final_rel(self, name: str | None = None) -> str:
        parent = self.parent_rel
        for part in self.missing:
            parent += "/" + part.lower()
        return parent + "/" + (name or self.name).lower()


def write_target(c: m.Call, path: str) -> Target:
    """Where a write to ``path`` lands: the deepest existing folder plus what must be created."""
    segments = m.split_path(path)
    if not segments:
        return Target(c.actor.root_ns, "", [], "", None, "", "", None, is_root=True)
    w = m.resolve_for_write(c, path)
    if w.found:
        rec = w.loc.rec
        assert rec is not None
        ns = rec["ns"]
        prefix = m.locate_ns(c, ns) or ("", "")
        role = m.entry_role(c, rec) if rec["type"] == "file" else m.ns_role(c.actor, c.tree.namespace(ns))
        return Target(ns, m.parent_of(rec["path"]), [], rec["name"], rec, prefix[0], prefix[1], role)
    ns = w.loc.ns
    rest = w.rest
    role = m.ns_role(c.actor, c.tree.namespace(ns))
    ancestor_file = w.file_ancestor or (w.loc.rec is not None and w.loc.rec["type"] == "file")
    return Target(
        ns, w.loc.rel, rest[:-1], rest[-1], None, w.loc.pl, w.loc.pd, role, ancestor_file=ancestor_file
    )


def check_target_names(target: Target, wrapper: str = "path") -> None:
    for part in [*target.missing, target.name]:
        if disallowed_name(part):
            raise path_error("disallowed_name", wrapper)


def conflict_error(kind: str, wrapper: str = "path") -> EmulatorError:
    return path_error("conflict", wrapper, conflict={".tag": kind})


def check_writable(target: Target, wrapper: str = "path") -> None:
    if target.is_root:
        raise path_error("conflict", wrapper, conflict={".tag": "folder"})
    if target.ancestor_file:
        raise conflict_error("file_ancestor", wrapper)
    if not target.writable:
        raise path_error("no_write_permission", wrapper)


def unique_name_in(c: m.Call, ns: str, parent_rel: str, name: str) -> str:
    taken = {r["name"].lower() for r in c.tree.children(ns, parent_rel)}
    if name.lower() not in taken:
        return name
    return autorename(name, taken)


def materialize_parents(c: m.Call, target: Target) -> str:
    """Create any missing parent folders; returns the rel path of the final parent."""
    if not target.missing:
        return target.parent_rel
    ops = Ops(c.ctx, c.tree, c.touched)
    return ops.ensure_folders(
        target.ns, target.parent_rel, target.missing, when=c.now(), owner=c.actor.id
    )


def conflicted_copy_name(c: m.Call, name: str) -> str:
    stem, ext = split_ext(name)
    day = c.now().strftime("%Y-%m-%d")
    return f"{stem} ({c.actor.account['display_name']}'s conflicted copy {day}){ext}"


def gate(condition: bool, error: dict[str, Any], status: int = 409) -> None:
    if not condition:
        raise api_error(error, status)
