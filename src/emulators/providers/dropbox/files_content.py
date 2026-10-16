"""``files/*`` content routes: upload, upload sessions, download, zip, export, temporary links,
thumbnails and previews."""

from __future__ import annotations

import html
import io
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from emulators.core.errors import EmulatorError
from emulators.core.provider import url_for_host
from emulators.providers.dropbox import catalog
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import (
    READ_PATH,
    WRITE_OR_ID,
    Args,
    BadInput,
    api_error,
    content_hash,
    parse_ts,
    path_error,
    tag,
    ts,
)
from emulators.providers.dropbox.dispatch import Download, ranged, route
from emulators.providers.dropbox.files_batch import launch, poll
from emulators.providers.dropbox.fileutil import (
    check_target_names,
    conflicted_copy_name,
    lookup,
    materialize_parents,
    quota_exceeded,
    require_file,
    unique_name_in,
    write_target,
)
from emulators.providers.dropbox.ops import Ops
from emulators.providers.dropbox.team_log import audit

MAX_UPLOAD = 150 * 1024 * 1024
SESSIONS = m.SESSIONS

Fail = Callable[..., EmulatorError]


def parse_mode(a: Args) -> tuple[str, str | None]:
    """``(tag, rev)`` from a WriteMode union; ``update`` carries the revision."""
    name, payload = a.union("mode", ("add", "overwrite", "update"), default="add")
    return name, (payload if isinstance(payload, str) else None)


def verify_hash(a: Args, data: bytes) -> None:
    expected = a.str("content_hash", None)
    if expected is not None and expected != content_hash(data):
        raise api_error(tag("content_hash_mismatch"))


def upload_fail(session_id: str) -> Fail:
    def fail(kind: str, **fields: Any) -> EmulatorError:
        return api_error(
            {".tag": "path", "reason": {".tag": kind, **fields}, "upload_session_id": session_id}
        )

    return fail


def finish_fail(kind: str, **fields: Any) -> EmulatorError:
    return path_error(kind, "path", **fields)


def commit_file(
    c: m.Call,
    path: str,
    data: bytes,
    *,
    mode: str,
    mode_rev: str | None,
    autorename_: bool,
    client_modified: datetime | None,
    strict_conflict: bool,
    fail: Fail,
) -> dict[str, Any]:
    """Create or update a file with Dropbox's add / overwrite / update semantics."""
    target = write_target(c, path)
    if target.is_root:
        raise fail("conflict", conflict={".tag": "folder"})
    if target.ancestor_file:
        raise fail("conflict", conflict={".tag": "file_ancestor"})
    if not target.writable:
        raise fail("no_write_permission")
    try:
        check_target_names(target)
    except EmulatorError:
        raise fail("disallowed_name") from None
    existing = target.existing
    replaced = existing["revisions"][-1]["size"] if existing and existing["type"] == "file" else 0
    if quota_exceeded(c, target.ns, len(data), replacing=replaced if mode != "add" else 0):
        raise fail("insufficient_space")
    ops = Ops(c.ctx, c.tree, c.touched)
    when = c.now()
    actor = c.actor.id

    def create(name: str) -> dict[str, Any]:
        parent = materialize_parents(c, target)
        made = ops.create_file(
            target.ns,
            parent,
            name,
            data,
            when=when,
            client_modified=client_modified,
            modified_by=actor,
            owner=actor,
        )
        audit(c, "file_add", made)
        return made

    if existing is None:
        return create(target.name)
    if existing["type"] == "folder" or existing.get("mount_ns"):
        if not autorename_:
            raise fail("conflict", conflict={".tag": "folder"})
        return create(unique_name_in(c, target.ns, target.parent_rel, target.name))
    if existing.get("lock") and existing["lock"]["account_id"] != actor:
        raise fail("conflict", conflict={".tag": "file"})
    if mode == "overwrite" or (mode == "update" and existing["revisions"][-1]["rev"] == mode_rev):
        ops.write_revision(existing, data, when=when, client_modified=client_modified, modified_by=actor)
        audit(c, "file_edit", existing)
        return existing
    if mode == "update":
        if strict_conflict:
            raise fail("conflict", conflict={".tag": "file"})
        return create(unique_name_in(c, target.ns, target.parent_rel, conflicted_copy_name(c, target.name)))
    if not autorename_:
        raise fail("conflict", conflict={".tag": "file"})
    return create(unique_name_in(c, target.ns, target.parent_rel, target.name))


@route("files/upload", style="upload", scope="files.content.write")
def upload(c: m.Call) -> Any:
    a = c.args
    path = a.str("path", pattern=WRITE_OR_ID)
    mode, mode_rev = parse_mode(a)
    if len(c.body) > MAX_UPLOAD:
        raise api_error(tag("payload_too_large"))
    verify_hash(a, c.body)
    session_id = "pid_upload_session:" + m.new_token(c.ctx, "session", 40)
    rec = commit_file(
        c,
        path,
        c.body,
        mode=mode,
        mode_rev=mode_rev,
        autorename_=a.bool("autorename"),
        client_modified=a.ts("client_modified"),
        strict_conflict=a.bool("strict_conflict"),
        fail=upload_fail(session_id),
    )
    return m.leaf(m.file_metadata(c, rec))


# ---------------------------------------------------------------------------------------
# Upload sessions
# ---------------------------------------------------------------------------------------
def session_data(c: m.Call, sess: dict[str, Any]) -> bytes:
    blobs = c.ctx.store.blobs
    return b"".join(blobs.get(key) for _, key, _ in sorted(sess["parts"]))


def new_session(c: m.Call, *, close: bool, kind: str = "sequential") -> dict[str, Any]:
    sid = "pid_upload_session:" + m.new_token(c.ctx, "session", 40)
    sess = {
        "id": sid,
        "account": c.actor.id,
        "parts": [],
        "offset": 0,
        "closed": close,
        "type": kind,
        "created": ts(c.now()),
    }
    c.ctx.store.collection(SESSIONS).put(sess)
    return sess


def find_session(c: m.Call, session_id: str, *, wrapper: str | None = None) -> dict[str, Any]:
    sess = c.ctx.store.collection(SESSIONS).get(session_id)
    if sess is None or sess["account"] != c.actor.id:
        raise lookup_error("not_found", wrapper)
    return sess


def lookup_error(kind: str, wrapper: str | None, **fields: Any) -> EmulatorError:
    err = {".tag": kind, **fields}
    if wrapper:
        err = {".tag": wrapper, wrapper: err}
    return api_error(err)


def append_chunk(c: m.Call, sess: dict[str, Any], offset: int, data: bytes, wrapper: str | None) -> None:
    if sess["closed"]:
        raise lookup_error("closed", wrapper)
    if sess["type"] == "concurrent":
        if any(o < offset + len(data) and offset < o + n for o, _, n in sess["parts"]):
            raise lookup_error("concurrent_session_invalid_offset", wrapper)
    elif offset != sess["offset"]:
        raise lookup_error("incorrect_offset", wrapper, correct_offset=sess["offset"])
    if len(data) > MAX_UPLOAD:
        raise lookup_error("too_large", wrapper)
    key = c.ctx.store.blobs.put(data)
    sess["parts"].append([offset, key, len(data)])
    sess["offset"] = max(sess["offset"], offset + len(data))


@route("files/upload_session/start", style="upload", scope="files.content.write")
def upload_session_start(c: m.Call) -> Any:
    a = c.args
    kind, _ = a.union("session_type", ("sequential", "concurrent", "other"), default="sequential")
    if len(c.body) > MAX_UPLOAD:
        raise api_error(tag("payload_too_large"))
    verify_hash(a, c.body)
    if kind == "concurrent" and c.body:
        raise api_error(tag("concurrent_session_data_not_allowed"))
    sess = new_session(c, close=False, kind="concurrent" if kind == "concurrent" else "sequential")
    if c.body:
        append_chunk(c, sess, 0, c.body, None)
    sess["closed"] = a.bool("close")
    return {"session_id": sess["id"]}


@route("files/upload_session/start_batch", scope="files.content.write")
def upload_session_start_batch(c: m.Call) -> Any:
    n = c.args.int("num_sessions", lo=1, hi=1000)
    kind, _ = c.args.union("session_type", ("sequential", "concurrent", "other"), default="sequential")
    ids = [
        new_session(c, close=False, kind="concurrent" if kind == "concurrent" else "sequential")["id"]
        for _ in range(n)
    ]
    return {"session_ids": ids}


def cursor_of(a: Args, key: str = "cursor") -> tuple[str, int]:
    cur = a.obj(key)
    assert cur is not None
    return cur.str("session_id"), cur.int("offset", lo=0)


@route("files/upload_session/append_v2", style="upload", scope="files.content.write")
def upload_session_append_v2(c: m.Call) -> Any:
    sid, offset = cursor_of(c.args)
    verify_hash(c.args, c.body)
    sess = find_session(c, sid)
    append_chunk(c, sess, offset, c.body, None)
    if c.args.bool("close"):
        sess["closed"] = True
    return None


@route("files/upload_session/append", style="upload", scope="files.content.write")
def upload_session_append_v1(c: m.Call) -> Any:
    sid = c.args.str("session_id")
    offset = c.args.int("offset", lo=0)
    sess = find_session(c, sid)
    append_chunk(c, sess, offset, c.body, None)
    if c.args.bool("close"):
        sess["closed"] = True
    return None


def finish_one(c: m.Call, entry: Args, extra: bytes, *, batch: bool = False) -> dict[str, Any]:
    sid, offset = cursor_of(entry)
    commit = entry.obj("commit")
    assert commit is not None
    sess = find_session(c, sid, wrapper="lookup_failed")
    if extra:
        if sess["closed"]:
            raise lookup_error("closed", "lookup_failed")
        if offset != sess["offset"]:
            raise lookup_error("incorrect_offset", "lookup_failed", correct_offset=sess["offset"])
        append_chunk(c, sess, offset, extra, "lookup_failed")
    elif offset != sess["offset"]:
        raise lookup_error("incorrect_offset", "lookup_failed", correct_offset=sess["offset"])
    data = session_data(c, sess)
    if sess["type"] == "concurrent":
        pos = 0
        for off, _, n in sorted(sess["parts"]):
            if off != pos:
                raise lookup_error("concurrent_session_missing_data", None)
            pos += n
    mode, mode_rev = parse_mode(commit)
    rec = commit_file(
        c,
        commit.str("path", pattern=WRITE_OR_ID),
        data,
        mode=mode,
        mode_rev=mode_rev,
        autorename_=commit.bool("autorename"),
        client_modified=commit.ts("client_modified"),
        strict_conflict=commit.bool("strict_conflict"),
        fail=finish_fail,
    )
    c.ctx.store.collection(SESSIONS).delete(sid)
    return rec


@route("files/upload_session/finish", style="upload", scope="files.content.write")
def upload_session_finish(c: m.Call) -> Any:
    verify_hash(c.args, c.body)  # the hash covers this call's chunk, not the whole file
    rec = finish_one(c, c.args, c.body)
    return m.leaf(m.file_metadata(c, rec))


def _flatten_failure(exc: EmulatorError) -> dict[str, Any]:
    body = exc.body
    error = body["error"] if isinstance(body, dict) and "error" in body else {".tag": "other"}
    return {"failure": error}


def success_entry(meta: dict[str, Any]) -> dict[str, Any]:
    """``UploadSessionFinishBatchResultEntry.success`` flattens FileMetadata (no ``file`` tag)."""
    flat = {k: v for k, v in meta.items() if k != ".tag"}
    return {".tag": "success", **flat}


def finish_batch_result(c: m.Call) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for raw in c.args.list("entries"):
        entry = Args(raw, "request body: entries")
        try:
            rec = finish_one(c, entry, b"", batch=True)
            entries.append(success_entry(m.file_metadata(c, rec)))
        except EmulatorError as exc:
            entries.append({".tag": "failure", "failure": _flatten_failure(exc)["failure"]})
    return {"entries": entries}


@route("files/upload_session/finish_batch_v2", scope="files.content.write")
def upload_session_finish_batch_v2(c: m.Call) -> Any:
    return finish_batch_result(c)


@route("files/upload_session/finish_batch", scope="files.content.write")
def upload_session_finish_batch(c: m.Call) -> Any:
    return launch(c, "finish_batch", finish_batch_result(c))


@route("files/upload_session/finish_batch/check", scope="files.content.write")
def upload_session_finish_batch_check(c: m.Call) -> Any:
    return poll(c, "finish_batch")


# ---------------------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------------------
def file_revision(rec: dict[str, Any], rev: str | None) -> dict[str, Any]:
    if rev is None:
        return rec["revisions"][-1]
    for revision in rec["revisions"]:
        if revision["rev"] == rev:
            return revision
    raise path_error("not_found")


def locate_file(
    c: m.Call, path: str, rev_arg: str | None = None
) -> tuple[m.Loc, dict[str, Any], dict[str, Any]]:
    """Resolve ``path`` (or ``rev:``) to ``(loc, file record, revision)``."""
    if path.startswith("rev:"):
        rec = c.tree.by_rev(path[4:])
        if rec is None or m.entry_role(c, rec) is None:
            raise path_error("not_found")
        return m.loc_for_entry(c, rec), rec, file_revision(rec, path[4:])
    loc = lookup(c, path, allow_root=False)
    rec = require_file(loc)
    return loc, rec, file_revision(rec, rev_arg)


def serve(c: m.Call, rec: dict[str, Any], revision: dict[str, Any], result: Any) -> Download:
    data = c.ctx.store.blobs.get(revision["blob"])
    status, body, headers = ranged(c.request, data, {})
    return Download(result, body, "application/octet-stream", headers, status)


@route("files/download", style="download", scope="files.content.read")
def download(c: m.Call) -> Any:
    a = c.args
    loc, rec, revision = locate_file(c, a.str("path", pattern=READ_PATH), a.str("rev", None))
    if not rec.get("downloadable", True):
        raise api_error(tag("unsupported_file"))
    return serve(c, rec, revision, m.leaf(m.file_metadata(c, rec, revision=revision, loc=loc)))


@route("files/download_zip", style="download", scope="files.content.read")
def download_zip(c: m.Call) -> Any:
    loc = lookup(c, c.args.str("path", pattern=READ_PATH))
    if loc.rec is not None and loc.rec["type"] != "folder":
        raise path_error("not_folder")
    from emulators.providers.dropbox.files_core import collect

    items = collect(c, loc, recursive=True, mounted=True, deleted=False, downloadable=False)
    files = [it for it in items if it[1]["type"] == "file"]
    if len(files) > 10000:
        raise api_error(tag("too_many_files"))
    top = loc.rec["name"] if loc.rec is not None else "Dropbox"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _key, rec, item_loc in items:
            rel = (
                (item_loc.pd + item_loc.dpath)[len(loc.pd + loc.dpath) :].lstrip("/")
                if loc.reachable
                else rec["name"]
            )
            name = f"{top}/{rel}"
            if rec["type"] == "folder":
                zf.writestr(zipfile.ZipInfo(name.rstrip("/") + "/", (1980, 1, 1, 0, 0, 0)), b"")
                continue
            latest = rec["revisions"][-1]
            when = parse_ts(latest["server_modified"])
            info = zipfile.ZipInfo(
                name, (max(when.year, 1980), when.month, when.day, when.hour, when.minute, when.second)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, c.ctx.store.blobs.get(latest["blob"]))
    folder_meta = (
        m.leaf(m.folder_metadata(c, loc.rec, loc=loc))
        if loc.rec is not None
        else {".tag": "folder", "name": "Dropbox", "id": "id:root"}
    )
    status, body, headers = ranged(c.request, buf.getvalue(), {})
    return Download({"metadata": folder_meta}, body, "application/zip", headers, status)


# ---------------------------------------------------------------------------------------
# Export (Paper-like, non-downloadable files)
# ---------------------------------------------------------------------------------------
def markdown_to_html(md: str) -> str:
    out: list[str] = []
    in_list = False
    for line in md.splitlines():
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{html.escape(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("# "):
            out.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.strip():
            out.append(f"<p>{html.escape(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "<html><body>" + "\n".join(out) + "</body></html>"


@route("files/export", style="download", scope="files.content.read")
def export(c: m.Call) -> Any:
    a = c.args
    loc, rec, revision = locate_file(c, a.str("path", pattern=READ_PATH))
    info = rec.get("export")
    if not info:
        raise api_error(tag("non_exportable"))
    fmt = a.str("export_format", info["export_as"])
    if fmt not in info["export_options"]:
        raise api_error(tag("invalid_export_format"))
    md = c.ctx.store.blobs.get(revision["blob"]).decode()
    body = md.encode() if fmt == "markdown" else markdown_to_html(md).encode()
    ext = "md" if fmt == "markdown" else "html"
    stem = rec["name"].rsplit(".", 1)[0]
    result = {
        "export_metadata": {"name": f"{stem}.{ext}", "size": len(body), "export_hash": content_hash(body)},
        "file_metadata": m.leaf(m.file_metadata(c, rec, revision=revision, loc=loc)),
    }
    status, payload, headers = ranged(c.request, body, {})
    ctype = "text/markdown" if fmt == "markdown" else "text/html"
    return Download(result, payload, ctype, headers, status)


# ---------------------------------------------------------------------------------------
# Temporary links
# ---------------------------------------------------------------------------------------
@route("files/get_temporary_link", scope="files.content.read")
def get_temporary_link(c: m.Call) -> Any:
    loc, rec, revision = locate_file(c, c.args.str("path", pattern=READ_PATH))
    if not rec.get("downloadable", True):
        raise api_error(tag("unsupported_file"))
    token = m.new_token(c.ctx, "templink", 40)
    c.ctx.store.collection(m.TEMP_LINKS).put(
        {
            "id": token,
            "kind": "download",
            "entry": rec["id"],
            "rev": revision["rev"],
            "account": c.actor.id,
            "expires": ts(c.now().replace(microsecond=0) + timedelta(hours=4)),
        }
    )
    link = url_for_host(c.request, c.ctx, "dl.dropboxusercontent.com", f"/apitl/1/{token}")
    return {"metadata": m.leaf(m.file_metadata(c, rec, revision=revision, loc=loc)), "link": link}


@route("files/get_temporary_upload_link", scope="files.content.write")
def get_temporary_upload_link(c: m.Call) -> Any:
    a = c.args
    commit = a.obj("commit_info")
    assert commit is not None
    commit.str("path", pattern=WRITE_OR_ID)
    duration = a.raw("duration", 14400.0)
    if not isinstance(duration, (int, float)) or not 60 <= duration <= 14400:
        raise BadInput("request body: duration: value must be between 60 and 14400")
    token = m.new_token(c.ctx, "uplink", 40)
    c.ctx.store.collection(m.TEMP_LINKS).put(
        {
            "id": token,
            "kind": "upload",
            "commit": commit.data,
            "account": c.actor.id,
            "root": c.actor.root_ns,
            "expires": ts(c.now() + timedelta(seconds=float(duration))),
        }
    )
    return {"link": url_for_host(c.request, c.ctx, "content.dropboxapi.com", f"/apitul/1/{token}")}


# ---------------------------------------------------------------------------------------
# Thumbnails and previews
# ---------------------------------------------------------------------------------------
THUMB_SIZES = {
    "w32h32": (32, 32),
    "w64h64": (64, 64),
    "w128h128": (128, 128),
    "w256h256": (256, 256),
    "w480h320": (480, 320),
    "w640h480": (640, 480),
    "w960h640": (960, 640),
    "w1024h768": (1024, 768),
    "w2048h1536": (2048, 1536),
}
THUMB_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


def make_thumbnail(rec: dict[str, Any], size: tuple[int, int], fmt: str) -> tuple[bytes, str]:
    from emulators.providers.dropbox.imaging import encode_thumbnail

    return encode_thumbnail(rec["id"], size, fmt)


def thumbnail_for(c: m.Call, path: str, args: Args) -> tuple[dict[str, Any], dict[str, Any], bytes, str]:
    loc, rec, revision = locate_file(c, path)
    ext = rec["name"].rsplit(".", 1)[-1].lower() if "." in rec["name"] else ""
    if ext not in catalog.THUMBNAILABLE:
        raise api_error(tag("unsupported_extension"))
    size_tag, _ = args.union("size", tuple(THUMB_SIZES), default="w64h64")
    fmt, _ = args.union("format", ("jpeg", "png", "webp"), default="jpeg")
    data, ctype = make_thumbnail(rec, THUMB_SIZES[size_tag], fmt)
    return m.leaf(m.file_metadata(c, rec, revision=revision, loc=loc)), rec, data, ctype


@route("files/get_thumbnail_v2", style="download", auth="app,user", scope="files.content.read")
def get_thumbnail_v2(c: m.Call) -> Any:
    name, payload = c.args.union("resource", ("path", "link"))
    if name == "link":
        from emulators.providers.dropbox import sharing_links

        return sharing_links.thumbnail_for_link(c, payload)
    path = payload if isinstance(payload, str) else Args(c.args.data["resource"]).str("path")
    meta, _, data, ctype = thumbnail_for(c, path, c.args)
    status, body, headers = ranged(c.request, data, {})
    return Download({"file_metadata": meta}, body, ctype, headers, status)


@route("files/get_thumbnail", style="download", scope="files.content.read")
def get_thumbnail_v1(c: m.Call) -> Any:
    meta, _, data, ctype = thumbnail_for(c, c.args.str("path", pattern=READ_PATH), c.args)
    status, body, headers = ranged(c.request, data, {})
    return Download(meta, body, ctype, headers, status)


@route("files/get_thumbnail_batch", style="rpc", scope="files.content.read")
def get_thumbnail_batch(c: m.Call) -> Any:
    import base64

    entries = c.args.list("entries")
    if len(entries) > 25:
        raise api_error(tag("too_many_files"))
    out: list[dict[str, Any]] = []
    for raw in entries:
        entry = Args(raw, "request body: entries")
        try:
            meta, _, data, _ = thumbnail_for(c, entry.str("path", pattern=READ_PATH), entry)
            out.append(tag("success", metadata=meta, thumbnail=base64.b64encode(data).decode()))
        except EmulatorError as exc:
            err = exc.body["error"] if isinstance(exc.body, dict) else tag("other")
            if err.get(".tag") == "path":
                out.append(tag("failure", failure=tag("path", path=err["path"])))
            else:
                out.append(tag("failure", failure=err))
    return {"entries": out}


@route("files/get_preview", style="download", scope="files.content.read")
def get_preview(c: m.Call) -> Any:
    a = c.args
    loc, rec, revision = locate_file(c, a.str("path", pattern=READ_PATH), a.str("rev", None))
    ext = rec["name"].rsplit(".", 1)[-1].lower() if "." in rec["name"] else ""
    if ext not in catalog.PREVIEWABLE:
        raise api_error(tag("unsupported_extension"))
    from emulators.world import documents

    world_doc = None
    if rec.get("doc"):
        try:
            world_doc = c.ctx.world.document(rec["doc"])
        except KeyError:
            world_doc = None
    if world_doc is not None:
        pdf = documents.make_pdf(world_doc.title, list(world_doc.paragraphs), created=world_doc.created)
    else:
        pdf = documents.make_pdf(rec["name"], [f"Preview of {rec['name']} ({revision['size']} bytes)."])
    status, body, headers = ranged(c.request, pdf, {})
    return Download(
        m.leaf(m.file_metadata(c, rec, revision=revision, loc=loc)), body, "application/pdf", headers, status
    )
