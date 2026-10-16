"""``files/*`` metadata and tree routes: list_folder (+continue/cursor/longpoll helpers),
get_metadata, create_folder, delete, move, copy.

Listing is cursor based exactly like Dropbox: the first page walks the tree, and once every entry
has been delivered the cursor flips to *delta* mode, where ``list_folder/continue`` returns only what
changed (including ``deleted`` entries) since the cursor was issued.
"""

from __future__ import annotations

from typing import Any

from emulators.core.errors import EmulatorError
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import (
    PATH_R_OR_ID,
    READ_PATH,
    WRITE_OR_ID,
    WRITE_PATH,
    BadInput,
    Pager,
    api_error,
    path_error,
    tag,
)
from emulators.providers.dropbox.dispatch import route
from emulators.providers.dropbox.fileutil import (
    check_target_names,
    check_writable,
    conflict_error,
    lookup,
    materialize_parents,
    mount_error,
    require_folder,
    unique_name_in,
    write_target,
)
from emulators.providers.dropbox.ops import Ops
from emulators.providers.dropbox.team_log import audit

PAGE_DEFAULT = 2000


# ---------------------------------------------------------------------------------------
# Walking
# ---------------------------------------------------------------------------------------
def child_loc(rec: dict[str, Any], base: m.Loc, pl: str, pd: str) -> m.Loc:
    return m.Loc(rec["ns"], rec["path"], rec["dpath"], rec, pl, pd, reachable=base.reachable)


def collect(
    c: m.Call,
    base: m.Loc,
    *,
    recursive: bool,
    mounted: bool,
    deleted: bool,
    downloadable: bool,
) -> list[tuple[str, dict[str, Any], m.Loc]]:
    """Every visible entry below ``base``, keyed by its lowercase path in the caller's view."""
    out: list[tuple[str, dict[str, Any], m.Loc]] = []

    def visit(ns: str, rel: str, pl: str, pd: str) -> None:
        for rec in c.tree.children(ns, rel):
            if not m.can_see(c, rec):
                continue
            if not downloadable and rec["type"] == "file" and not rec.get("downloadable", True):
                continue
            out.append(((pl + rec["path"]).lower(), rec, child_loc(rec, base, pl, pd)))
            if recursive and rec["type"] == "folder":
                if rec.get("mount_ns"):
                    if mounted:
                        visit(rec["mount_ns"], "", pl + rec["path"], pd + rec["dpath"])
                else:
                    visit(ns, rec["path"], pl, pd)
        if deleted:
            for rec in c.tree.dead_below(ns, rel, recursive=False):
                if m.entry_role(c, rec) is not None:
                    out.append(((pl + rec["path"]).lower(), rec, child_loc(rec, base, pl, pd)))
            if recursive:
                # Deleted entries deeper than one level (folders deleted wholesale).
                seen = {id(r) for _, r, _ in out}
                for rec in c.tree.dead_below(ns, rel, recursive=True):
                    if id(rec) not in seen and m.entry_role(c, rec) is not None:
                        out.append(((pl + rec["path"]).lower(), rec, child_loc(rec, base, pl, pd)))

    visit(base.ns, base.rel, base.pl, base.pd)
    out.sort(key=lambda item: item[0])
    return out


def scope_list(c: m.Call, base: m.Loc, *, recursive: bool, mounted: bool) -> list[tuple[str, str, str, str]]:
    scopes = [(base.ns, base.rel, base.pl, base.pd)]
    if recursive and mounted:
        queue = list(scopes)
        while queue:
            ns, rel, pl, pd = queue.pop()
            for rec in c.tree.subtree(ns, rel):
                if rec.get("mount_ns") and m.can_see(c, rec):
                    scope = (rec["mount_ns"], "", pl + rec["path"], pd + rec["dpath"])
                    scopes.append(scope)
                    queue.append(scope)
    return scopes


def in_scope(
    ev: dict[str, Any], scopes: list[tuple[str, str, str, str]], recursive: bool
) -> tuple[str, str] | None:
    for ns, rel, pl, pd in scopes:
        if ev["ns"] != ns:
            continue
        if recursive:
            if rel == "" or ev["path"].startswith(rel + "/"):
                return pl, pd
        elif m.parent_of(ev["path"]) == rel:
            return pl, pd
    return None


# ---------------------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------------------
def make_state(
    c: m.Call, base: m.Loc, flags: dict[str, Any], *, seq: int, after: str | None, phase: str
) -> dict[str, Any]:
    return {
        "v": 1,
        "a": c.actor.id,
        "adm": c.actor.admin,
        "ns": base.ns,
        "rel": base.rel,
        "pl": base.pl,
        "pd": base.pd,
        "reach": base.reachable,
        "seq": seq,
        "after": after,
        "ph": phase,
        **flags,
    }


def decode_state(cursor: str) -> dict[str, Any]:
    state = Pager.decode(cursor)
    if not state or state.get("v") != 1 or "ns" not in state:
        raise BadInput(f"request body: cursor: '{cursor}' is not a valid cursor")
    return state


def base_from_state(c: m.Call, state: dict[str, Any]) -> m.Loc:
    ns = c.tree.namespace(state["ns"])
    if ns is None or m.ns_role(c.actor, ns) is None:
        raise path_error("not_found", "path")
    rel = state["rel"]
    rec = None
    if rel:
        rec = c.tree.live.get((state["ns"], rel))
        if rec is None:
            raise path_error("not_found", "path")
    return m.Loc(
        state["ns"], rel, rec["dpath"] if rec else "", rec, state["pl"], state["pd"], reachable=state["reach"]
    )


def page_flags(a: Any) -> dict[str, Any]:
    from emulators.providers.dropbox.file_properties import prop_filter_of

    return {
        "pf": prop_filter_of(a),
        "rec": a.bool("recursive"),
        "mnt": a.bool("include_mounted_folders", True),
        "del": a.bool("include_deleted"),
        "dl": a.bool("include_non_downloadable_files", True),
        "ex": a.bool("include_has_explicit_shared_members"),
        "lim": a.int("limit", PAGE_DEFAULT, lo=1, hi=2000),
    }


def render(c: m.Call, rec: dict[str, Any], loc: m.Loc, explicit: bool) -> dict[str, Any]:
    return m.metadata(c, rec, loc=loc, explicit_members=explicit)


def first_page(c: m.Call, state: dict[str, Any], base: m.Loc) -> dict[str, Any]:
    items = collect(
        c, base, recursive=state["rec"], mounted=state["mnt"], deleted=state["del"], downloadable=state["dl"]
    )
    return list_page(c, state, items)


def list_page(
    c: m.Call, state: dict[str, Any], items: list[tuple[str, dict[str, Any], m.Loc]]
) -> dict[str, Any]:
    after = state.get("after")
    remaining = [it for it in items if after is None or it[0] > after]
    page = remaining[: state["lim"]]
    more = len(remaining) > len(page)
    entries = [render(c, rec, loc, state["ex"]) for _, rec, loc in page]
    nxt = {**state, "after": page[-1][0]} if more else {**state, "after": None, "ph": "delta"}
    return {"entries": entries, "cursor": Pager.encode(nxt), "has_more": more}


def delta_page(c: m.Call, state: dict[str, Any], base: m.Loc) -> dict[str, Any]:
    now_seq = m.current_seq(c.ctx)
    recursive = state["rec"]
    scopes = scope_list(c, base, recursive=recursive, mounted=state["mnt"])
    latest: dict[tuple[str, str], tuple[dict[str, Any], tuple[str, str]]] = {}
    for ev in sorted(c.ctx.store.collection(m.CHANGES), key=lambda e: e["seq"]):
        if ev["seq"] <= state["seq"]:
            continue
        prefix = in_scope(ev, scopes, recursive)
        if prefix is None:
            continue
        latest[(ev["ns"], ev["path"])] = (ev, prefix)
        # keep dict order by last event
        latest[(ev["ns"], ev["path"])] = latest.pop((ev["ns"], ev["path"]))
    ordered = list(latest.values())
    limit = state["lim"]
    page, more = ordered[:limit], len(ordered) > limit
    entries: list[dict[str, Any]] = []
    for ev, (pl, pd) in page:
        live = c.tree.live.get((ev["ns"], ev["path"]))
        if live is not None:
            if not m.can_see(c, live):
                continue
            loc = m.Loc(live["ns"], live["path"], live["dpath"], live, pl, pd, reachable=base.reachable)
            entries.append(m.metadata(c, live, loc=loc, explicit_members=state["ex"]))
        else:
            d: dict[str, Any] = {".tag": "deleted", "name": ev["name"]}
            if base.reachable:
                d["path_lower"] = pl + ev["path"]
                d["path_display"] = pd + ev["dpath"]
            entries.append(d)
    new_seq = page[-1][0]["seq"] if more else now_seq
    nxt = {**state, "seq": new_seq, "ph": "delta", "after": None}
    return {"entries": entries, "cursor": Pager.encode(nxt), "has_more": more}


@route("files/list_folder", auth="app,user", scope="files.metadata.read")
def list_folder(c: m.Call) -> Any:
    a = c.args
    path = a.str("path", pattern=PATH_R_OR_ID)
    flags = page_flags(a)
    c.prop_filter = flags["pf"]
    if a.obj("shared_link", None) is not None:
        return list_shared_link(c, a, flags)
    if c.actor.app_auth:
        raise BadInput("request body: shared_link: required when using app authentication")
    loc = lookup(c, path)
    if loc.rec is not None and loc.rec["type"] != "folder":
        raise path_error("not_folder")
    seq = m.current_seq(c.ctx)
    state = make_state(c, loc, flags, seq=seq, after=None, phase="list")
    return first_page(c, state, loc)


def list_shared_link(c: m.Call, a: Any, flags: dict[str, Any]) -> Any:
    from emulators.providers.dropbox import sharing_links

    return sharing_links.list_link_folder(c, a, flags)


@route("files/list_folder/continue", auth="app,user", scope="files.metadata.read")
def list_folder_continue(c: m.Call) -> Any:
    state = decode_state(c.args.str("cursor"))
    c.prop_filter = state.get("pf")
    if state["seq"] > m.current_seq(c.ctx):
        raise api_error(tag("reset"))
    base = base_from_state(c, state)
    if state["ph"] == "list":
        return first_page(c, state, base)
    return delta_page(c, state, base)


@route("files/list_folder/get_latest_cursor", scope="files.metadata.read")
def list_folder_get_latest_cursor(c: m.Call) -> Any:
    a = c.args
    path = a.str("path", pattern=PATH_R_OR_ID)
    flags = page_flags(a)
    loc = lookup(c, path)
    if loc.rec is not None and loc.rec["type"] != "folder":
        raise path_error("not_folder")
    state = make_state(c, loc, flags, seq=m.current_seq(c.ctx), after=None, phase="delta")
    return {"cursor": Pager.encode(state)}


# ---------------------------------------------------------------------------------------
# get_metadata
# ---------------------------------------------------------------------------------------
@route("files/get_metadata", scope="files.metadata.read")
def get_metadata(c: m.Call) -> Any:
    a = c.args
    path = a.str("path", pattern=READ_PATH)
    include_deleted = a.bool("include_deleted")
    explicit = a.bool("include_has_explicit_shared_members")
    from emulators.providers.dropbox.file_properties import prop_filter_of

    c.prop_filter = prop_filter_of(a)
    if path.startswith("rev:"):
        rec = c.tree.by_rev(path[4:])
        if rec is None or m.entry_role(c, rec) is None:
            raise path_error("not_found")
        revision = next(r for r in rec["revisions"] if r["rev"] == path[4:])
        return m.file_metadata(c, rec, revision=revision, explicit_members=explicit)
    loc = m.resolve(c, path)
    if loc is not None and loc.is_view_root:
        raise path_error("malformed_path")
    if loc is None and include_deleted:
        dead = find_deleted(c, path)
        if dead is not None:
            return m.deleted_metadata(c, dead[0], loc=dead[1])
    if loc is None:
        raise path_error("not_found")
    assert loc.rec is not None
    return m.metadata(c, loc.rec, loc=loc, explicit_members=explicit)


def find_deleted(c: m.Call, path: str) -> tuple[dict[str, Any], m.Loc] | None:
    if path.startswith("id:"):
        rec = c.tree.by_id(path.split("/")[0])
        if rec is not None and rec.get("deleted") and m.entry_role(c, rec) is not None:
            return rec, m.loc_for_entry(c, rec)
        return None
    segments = m.split_path(path)
    if not segments or path.startswith("ns:"):
        return None
    w = m.walk(c, c.actor.root_ns, segments[:-1])
    if not w.found:
        return None
    key = (w.loc.ns, w.loc.rel + "/" + segments[-1].lower())
    recs = c.tree.dead.get(key)
    if not recs or key in c.tree.live:
        return None
    rec = max(recs, key=lambda r: (r.get("deleted_at") or "", r.get("seq", 0)))
    if m.entry_role(c, rec) is None:
        return None
    return rec, m.Loc(rec["ns"], rec["path"], rec["dpath"], rec, w.loc.pl, w.loc.pd)


# ---------------------------------------------------------------------------------------
# create_folder
# ---------------------------------------------------------------------------------------
def create_folder_at(c: m.Call, path: str, autorename_: bool) -> dict[str, Any]:
    target = write_target(c, path)
    check_writable(target)
    if target.existing is not None and not autorename_:
        raise conflict_error("folder" if target.existing["type"] == "folder" else "file")
    check_target_names(target)
    parent = materialize_parents(c, target)
    name = target.name
    if target.existing is not None or autorename_:
        name = unique_name_in(c, target.ns, parent, name)
    ops = Ops(c.ctx, c.tree, c.touched)
    rec = ops.create_folder(target.ns, parent, name, when=c.now(), owner=c.actor.id)
    audit(c, "file_add", rec)
    return rec


@route("files/create_folder_v2", scope="files.content.write")
def create_folder_v2(c: m.Call) -> Any:
    path = c.args.str("path", pattern=WRITE_PATH)
    rec = create_folder_at(c, path, c.args.bool("autorename"))
    return {"metadata": m.leaf(m.folder_metadata(c, rec))}


@route("files/create_folder", scope="files.content.write")
def create_folder_v1(c: m.Call) -> Any:
    path = c.args.str("path", pattern=WRITE_PATH)
    rec = create_folder_at(c, path, c.args.bool("autorename"))
    return m.leaf(m.folder_metadata(c, rec))


# ---------------------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------------------
def delete_at(c: m.Call, path: str, parent_rev: str | None) -> dict[str, Any]:
    loc = m.resolve(c, path, allow_root=False)
    if loc is None or loc.rec is None:
        raise path_error("not_found", "path_lookup")
    rec = loc.rec
    if rec.get("mount_ns"):
        if mount_error(rec, c.tree):
            raise path_error("team_folder", "path_write")
        meta = m.folder_metadata(c, rec, loc=loc)
        Ops(c.ctx, c.tree, c.touched).delete(rec, when=c.now(), by=c.actor.id)
        return meta
    role = m.entry_role(c, rec)
    if not m.is_writer(role):
        raise path_error("no_write_permission", "path_write")
    if rec.get("lock") and rec["lock"]["account_id"] != c.actor.id:
        raise path_error("locked", "path_lookup")
    if parent_rev and rec["type"] == "file" and rec["revisions"][-1]["rev"] != parent_rev:
        raise path_error("not_found", "path_lookup")
    meta = m.metadata(c, rec, loc=loc)
    audit(c, "file_delete", rec)
    Ops(c.ctx, c.tree, c.touched).delete(rec, when=c.now(), by=c.actor.id)
    return meta


@route("files/delete_v2", scope="files.content.write")
def delete_v2(c: m.Call) -> Any:
    path = c.args.str("path", pattern=WRITE_OR_ID)
    return {"metadata": delete_at(c, path, c.args.str("parent_rev", None))}


@route("files/delete", scope="files.content.write")
def delete_v1(c: m.Call) -> Any:
    path = c.args.str("path", pattern=WRITE_OR_ID)
    return delete_at(c, path, c.args.str("parent_rev", None))


@route("files/permanently_delete", scope="files.permanent_delete")
def permanently_delete(c: m.Call) -> Any:
    path = c.args.str("path", pattern=PATH_R_OR_ID)
    loc = m.resolve(c, path, allow_root=False)
    rec = loc.rec if loc else None
    if rec is None:
        found = find_deleted(c, path)
        rec = found[0] if found else None
    if rec is None:
        raise path_error("not_found", "path_lookup")
    if not m.is_writer(m.entry_role(c, rec)):
        raise path_error("no_write_permission", "path_write")
    ops = Ops(c.ctx, c.tree, c.touched)
    if not rec.get("deleted"):
        ops.delete(rec, when=c.now(), by=c.actor.id)
    ops.purge(rec)
    return None


# ---------------------------------------------------------------------------------------
# move / copy
# ---------------------------------------------------------------------------------------
def relocation_error(tag_name: str, **fields: Any) -> EmulatorError:
    return api_error({".tag": tag_name, **fields})


def relocate_path(
    c: m.Call, from_path: str, to_path: str, *, copy: bool, autorename_: bool, allow_shared: bool = False
) -> dict[str, Any]:
    """Shared by move/copy (single and batch); returns the new entry's wire metadata."""
    src_loc = m.resolve(c, from_path, allow_root=False)
    if src_loc is None or src_loc.rec is None:
        raise path_error("not_found", "from_lookup")
    src = src_loc.rec
    if src.get("mount_ns"):
        raise relocation_error("cant_copy_shared_folder" if copy else "cant_move_shared_folder")
    if m.entry_role(c, src) is None:
        raise path_error("not_found", "from_lookup")
    if not copy and not m.is_writer(m.entry_role(c, src)):
        raise path_error("no_write_permission", "from_write")
    if not copy and src.get("lock") and src["lock"]["account_id"] != c.actor.id:
        raise path_error("locked", "from_lookup")
    target = write_target(c, to_path)
    if target.is_root:
        raise path_error("conflict", "to", conflict={".tag": "folder"})
    if target.ancestor_file:
        raise path_error("conflict", "to", conflict={".tag": "file_ancestor"})
    if not target.writable:
        raise path_error("no_write_permission", "to")
    for part in [*target.missing, target.name]:
        from emulators.providers.dropbox.common import disallowed_name

        if disallowed_name(part):
            raise path_error("disallowed_name", "to")
    same = target.existing is src
    if target.existing is not None and not same and not autorename_:
        raise path_error(
            "conflict", "to", conflict={".tag": "folder" if target.existing["type"] == "folder" else "file"}
        )
    if same and src["name"] == target.name and not copy:
        raise relocation_error("duplicated_or_nested_paths")
    if src["type"] == "folder":
        dest_rel = target.final_rel()
        if (
            target.ns == src["ns"]
            and (dest_rel == src["path"] or dest_rel.startswith(src["path"] + "/"))
            and not same
        ):
            raise relocation_error("cant_move_folder_into_itself")
        if copy and any(r.get("mount_ns") for r in c.tree.subtree(src["ns"], src["path"])):
            raise relocation_error("cant_copy_shared_folder")
        if len(c.tree.subtree(src["ns"], src["path"])) > 10000:
            raise relocation_error("too_many_files")
    parent = materialize_parents(c, target)
    name = target.name
    if target.existing is not None and not same:
        name = unique_name_in(c, target.ns, parent, name)
    ops = Ops(c.ctx, c.tree, c.touched)
    before = dict(src)
    if copy:
        rec = ops.copy(src, target.ns, parent, name, owner=c.actor.id)
        audit(c, "file_copy", before, dest=rec)
    else:
        rec = ops.relocate(src, target.ns, parent, name)
        renamed = before["ns"] == rec["ns"] and m.parent_of(before["path"]) == m.parent_of(rec["path"])
        audit(c, "file_rename" if renamed else "file_move", before, dest=rec)
    c.invalidate()
    return m.metadata(c, rec)


def relocation_args(c: m.Call) -> tuple[str, str, bool, bool]:
    a = c.args
    return (
        a.str("from_path", pattern=WRITE_OR_ID),
        a.str("to_path", pattern=WRITE_OR_ID),
        a.bool("autorename"),
        a.bool("allow_shared_folder"),
    )


@route("files/move_v2", scope="files.content.write")
def move_v2(c: m.Call) -> Any:
    src, dst, auto, shared = relocation_args(c)
    return {"metadata": relocate_path(c, src, dst, copy=False, autorename_=auto, allow_shared=shared)}


@route("files/copy_v2", scope="files.content.write")
def copy_v2(c: m.Call) -> Any:
    src, dst, auto, shared = relocation_args(c)
    return {"metadata": relocate_path(c, src, dst, copy=True, autorename_=auto, allow_shared=shared)}


@route("files/move", scope="files.content.write")
def move_v1(c: m.Call) -> Any:
    src, dst, auto, shared = relocation_args(c)
    return relocate_path(c, src, dst, copy=False, autorename_=auto, allow_shared=shared)


@route("files/copy", scope="files.content.write")
def copy_v1(c: m.Call) -> Any:
    src, dst, auto, shared = relocation_args(c)
    return relocate_path(c, src, dst, copy=True, autorename_=auto, allow_shared=shared)


__all__ = ["collect", "require_folder"]
