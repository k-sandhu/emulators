"""``files/*`` odds and ends: revisions, restore, tags, locks, copy references, save_url."""

from __future__ import annotations

import base64
import re
from datetime import timedelta
from typing import Any
from urllib.parse import unquote, urlparse

from emulators.core.errors import EmulatorError
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import (
    PATH_ONLY,
    READ_PATH,
    REVISIONS_PATH,
    WRITE_PATH,
    Args,
    api_error,
    path_error,
    tag,
    ts,
)
from emulators.providers.dropbox.dispatch import route
from emulators.providers.dropbox.files_batch import error_of, launch, poll
from emulators.providers.dropbox.files_core import find_deleted
from emulators.providers.dropbox.fileutil import (
    check_target_names,
    check_writable,
    conflict_error,
    lookup,
    materialize_parents,
    require_file,
    write_target,
)
from emulators.providers.dropbox.ops import Ops


def find_any(c: m.Call, path: str) -> tuple[dict[str, Any], m.Loc]:
    """A live or deleted entry at ``path`` (``id:`` and path forms)."""
    loc = m.resolve(c, path, allow_root=False)
    if loc is not None and loc.rec is not None:
        return loc.rec, loc
    found = find_deleted(c, path)
    if found is None:
        raise path_error("not_found")
    return found


# ---------------------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------------------
@route("files/list_revisions", scope="files.metadata.read")
def list_revisions(c: m.Call) -> Any:
    a = c.args
    path = a.str("path", pattern=REVISIONS_PATH)
    mode, _ = a.union("mode", ("path", "id", "other"), default="path")
    limit = a.int("limit", 10, lo=1, hi=100)
    before = a.str("before_rev", None)
    restorable = a.bool("include_restorable_info")
    if mode == "id" and not path.startswith("id:"):
        raise path_error("not_found")
    rec, loc = find_any(c, path)
    if rec["type"] != "file":
        raise path_error("not_file")
    if m.entry_role(c, rec) is None:
        raise path_error("not_found")
    revisions = list(reversed(rec["revisions"]))
    if before is not None:
        idx = next((i for i, r in enumerate(revisions) if r["rev"] == before), None)
        if idx is None:
            raise api_error(tag("invalid_before_rev"))
        revisions = revisions[idx + 1 :]
    page, more = revisions[:limit], len(revisions) > limit
    entries = []
    for revision in page:
        d = m.leaf(m.file_metadata(c, rec, revision=revision, loc=loc))
        if restorable:
            d["is_restorable"] = True
        entries.append(d)
    out: dict[str, Any] = {"is_deleted": bool(rec.get("deleted")), "entries": entries, "has_more": more}
    if rec.get("deleted"):
        out["server_deleted"] = rec.get("deleted_at")
    return out


@route("files/restore", scope="files.content.write")
def restore(c: m.Call) -> Any:
    a = c.args
    path = a.str("path", pattern=WRITE_PATH)
    rev = a.str("rev", pattern=r"[0-9a-f]+")
    try:
        rec, _loc = find_any(c, path)
    except EmulatorError:
        raise path_error("not_found", "path_lookup") from None
    if rec["type"] != "file":
        raise path_error("not_file", "path_lookup")
    revision = next((r for r in rec["revisions"] if r["rev"] == rev), None)
    if revision is None:
        raise api_error(tag("invalid_revision"))
    if not m.is_writer(m.entry_role(c, rec)):
        raise path_error("no_write_permission", "path_write")
    ops = Ops(c.ctx, c.tree, c.touched)
    if rec.get("deleted"):
        occupied = c.tree.live.get((rec["ns"], rec["path"]))
        if occupied is not None:
            raise conflict_error("folder" if occupied["type"] == "folder" else "file", "path_write")
        ops.ensure_folders(
            rec["ns"], "", [p for p in rec["dpath"].split("/")[1:-1]], when=c.now(), owner=c.actor.id
        )
        ops.restore_entry(rec)
    data = c.ctx.store.blobs.get(revision["blob"])
    ops.write_revision(rec, data, when=c.now(), modified_by=c.actor.id)
    c.invalidate()
    return m.leaf(m.file_metadata(c, rec))


# ---------------------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------------------
@route("files/tags/get", scope="files.metadata.read")
def tags_get(c: m.Call) -> Any:
    out = []
    for path in c.args.list("paths"):
        if not isinstance(path, str) or re.fullmatch(PATH_ONLY, path) is None:
            raise path_error("not_found", "path")
        loc = lookup(c, path, allow_root=False)
        assert loc.rec is not None
        out.append(
            {
                "path": path,
                "tags": [tag("user_generated_tag", tag_text=t) for t in loc.rec.get("tags", [])],
            }
        )
    return {"paths_to_tags": out}


@route("files/tags/add", scope="files.content.write")
def tags_add(c: m.Call) -> Any:
    a = c.args
    path = a.str("path", pattern=PATH_ONLY)
    text = a.str("tag_text", pattern=r"[\w]+")
    if len(text) > 32:
        raise api_error({".tag": "other"})
    loc = lookup(c, path, allow_root=False)
    rec = loc.rec
    assert rec is not None
    if not m.is_writer(m.entry_role(c, rec)):
        raise api_error({".tag": "path", "path": {".tag": "restricted_content"}})
    tags = rec.setdefault("tags", [])
    if text.lower() not in (t.lower() for t in tags):
        if len(tags) >= 20:
            raise api_error(tag("too_many_tags"))
        tags.append(text)
    return None


@route("files/tags/remove", scope="files.content.write")
def tags_remove(c: m.Call) -> Any:
    a = c.args
    loc = lookup(c, a.str("path", pattern=PATH_ONLY), allow_root=False)
    rec = loc.rec
    assert rec is not None
    text = a.str("tag_text", pattern=r"[\w]+")
    tags = rec.get("tags", [])
    kept = [t for t in tags if t.lower() != text.lower()]
    if len(kept) == len(tags):
        raise api_error(tag("tag_not_present"))
    rec["tags"] = kept
    return None


# ---------------------------------------------------------------------------------------
# File locks
# ---------------------------------------------------------------------------------------
def lock_content(rec: dict[str, Any]) -> dict[str, Any]:
    lock = rec.get("lock")
    if not lock:
        return tag("unlocked")
    return tag(
        "single_user",
        created=lock["created"],
        lock_holder_account_id=lock["account_id"],
        **({"lock_holder_team_id": lock["team_id"]} if lock.get("team_id") else {}),
    )


def lock_batch(c: m.Call, action: str) -> Any:
    out: list[dict[str, Any]] = []
    for raw in c.args.list("entries"):
        path = Args(raw, "request body: entries").str("path")
        try:
            loc = m.resolve(c, path, allow_root=False)
            if loc is None or loc.rec is None:
                raise path_error("not_found", "path_lookup")
            rec = loc.rec
            if rec["type"] != "file":
                raise api_error(tag("cannot_be_locked"))
            if action != "get":
                if not m.is_writer(m.entry_role(c, rec)):
                    raise api_error(tag("no_write_permission"))
                ns = c.tree.namespace(rec["ns"])
                if ns is None or ns["type"] not in ("team_folder", "shared_folder"):
                    raise api_error(tag("file_not_shared"))
                holder = rec.get("lock")
                if action == "lock":
                    if holder and holder["account_id"] != c.actor.id:
                        raise api_error(tag("lock_conflict", lock={"content": lock_content(rec)}))
                    rec["lock"] = {
                        "account_id": c.actor.id,
                        "name": c.actor.account["display_name"],
                        "created": ts(c.now()),
                        "team_id": m.team_info(c.ctx)["id"] if c.actor.account.get("is_team") else None,
                    }
                else:
                    if holder and holder["account_id"] != c.actor.id and not c.actor.admin:
                        raise api_error(tag("lock_conflict", lock={"content": lock_content(rec)}))
                    rec.pop("lock", None)
            out.append(
                tag(
                    "success",
                    metadata=m.file_metadata(c, rec, loc=loc),
                    lock={"content": lock_content(rec)},
                )
            )
        except EmulatorError as exc:
            out.append(tag("failure", failure=error_of(exc)))
    return {"entries": out}


@route("files/lock_file_batch", scope="files.content.write")
def lock_file_batch(c: m.Call) -> Any:
    return lock_batch(c, "lock")


@route("files/unlock_file_batch", scope="files.content.write")
def unlock_file_batch(c: m.Call) -> Any:
    return lock_batch(c, "unlock")


@route("files/get_file_lock_batch", scope="files.content.read")
def get_file_lock_batch(c: m.Call) -> Any:
    return lock_batch(c, "get")


# ---------------------------------------------------------------------------------------
# Copy references
# ---------------------------------------------------------------------------------------
@route("files/copy_reference/get", scope="files.content.write")
def copy_reference_get(c: m.Call) -> Any:
    loc = lookup(c, c.args.str("path", pattern=READ_PATH), allow_root=False)
    rec = loc.rec
    assert rec is not None
    ref = "AAF" + m.new_token(c.ctx, "copyref", 60)
    expires = c.now() + timedelta(hours=4)
    c.ctx.store.collection(m.COPY_REFS).put(
        {"id": ref, "entry": rec["id"], "account": c.actor.id, "expires": ts(expires)}
    )
    return {"metadata": m.metadata(c, rec, loc=loc), "copy_reference": ref, "expires": ts(expires)}


@route("files/copy_reference/save", scope="files.content.write")
def copy_reference_save(c: m.Call) -> Any:
    a = c.args
    ref = c.ctx.store.collection(m.COPY_REFS).get(a.str("copy_reference"))
    if ref is None or ref["expires"] <= ts(c.now()):
        raise api_error(tag("invalid_copy_reference"))
    src = c.tree.by_id(ref["entry"])
    if src is None or src.get("deleted"):
        raise api_error(tag("not_found"))
    target = write_target(c, a.str("path", pattern=WRITE_PATH))
    check_writable(target)
    if target.existing is not None:
        raise conflict_error("folder" if target.existing["type"] == "folder" else "file")
    check_target_names(target)
    parent = materialize_parents(c, target)
    rec = Ops(c.ctx, c.tree, c.touched).copy(src, target.ns, parent, target.name, owner=c.actor.id)
    return {"metadata": m.metadata(c, rec)}


# ---------------------------------------------------------------------------------------
# save_url -- only emulator-hosted and data: URLs can be fetched (no outbound network calls)
# ---------------------------------------------------------------------------------------
def fetch_url(c: m.Call, url: str) -> bytes | None:
    parsed = urlparse(url)
    if parsed.scheme == "data":
        header, _, payload = url[5:].partition(",")
        try:
            return base64.b64decode(payload) if header.endswith(";base64") else unquote(payload).encode()
        except ValueError:
            return None
    if parsed.scheme in ("http", "https") and c.ctx.provider.matches_host(parsed.hostname or ""):
        token = parsed.path.rsplit("/", 1)[-1]
        link = c.ctx.store.collection(m.TEMP_LINKS).get(token)
        if link and link["kind"] == "download":
            rec = c.tree.by_id(link["entry"])
            if rec is not None:
                rev = next((r for r in rec["revisions"] if r["rev"] == link["rev"]), rec["revisions"][-1])
                return c.ctx.store.blobs.get(rev["blob"])
    return None


@route("files/save_url", scope="files.content.write")
def save_url(c: m.Call) -> Any:
    from emulators.providers.dropbox.files_content import commit_file, finish_fail

    path = c.args.str("path", pattern=PATH_ONLY)
    url = c.args.str("url")
    data = fetch_url(c, url)
    if data is None:
        return launch(c, "save_url", tag("download_failed"), status="failed")
    try:
        rec = commit_file(
            c,
            path,
            data,
            mode="add",
            mode_rev=None,
            autorename_=False,
            client_modified=None,
            strict_conflict=False,
            fail=finish_fail,
        )
    except EmulatorError as exc:
        return launch(c, "save_url", error_of(exc), status="failed")
    meta = {k: v for k, v in m.file_metadata(c, rec).items() if k != ".tag"}
    return launch(c, "save_url", meta)


@route("files/save_url/check_job_status", scope="files.content.write")
def save_url_check(c: m.Call) -> Any:
    return poll(c, "save_url")


__all__ = ["require_file"]
