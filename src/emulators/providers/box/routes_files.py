"""Files: metadata, content download (302 to boxcloud), versions, thumbnails, locks, copy, trash."""

from __future__ import annotations

import copy
import html
import re
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from emulators.core.clock import utc
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.box import access, content, db, errors, events, ops, sharing, tree
from emulators.providers.box.routing import Api, created, no_content
from emulators.providers.box.util import (
    extension_of,
    input_ts,
    json_body,
    now_iso,
    object_or_empty,
    opt_str,
    page_window,
    paginate,
    parse_fields,
    query_int,
    random_alnum,
    ref_id,
    sanitize_name,
    str_list,
    to_iso,
    try_parse_ts,
    validate_name,
)
from emulators.world import documents

Record = dict[str, Any]
#: Fields a lock protects: everything but ``lock`` itself, ``collections`` (the caller's own favourites).
LOCKED_FIELDS = frozenset(
    {"name", "parent", "description", "tags", "disposition_at", "permissions", "shared_link"}
)
DOWNLOAD_TTL = timedelta(minutes=15)
PNG_SIZES = (32, 64, 128, 256)
JPG_SIZES = (32, 94, 160, 320, 1024, 2048)
NO_THUMBNAIL = {"bin", "zip", "gz", "tar", "7z", "exe", "dll", "iso", "dat", "db", "jar"}


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Single ``bytes=a-b`` range -> inclusive (start, end); raises ValueError when unsatisfiable."""
    if not header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or (match.group(1) == "" and match.group(2) == ""):
        return None
    if match.group(1) == "":
        length = int(match.group(2))
        if length == 0:
            raise ValueError
        return max(size - length, 0), size - 1
    start = int(match.group(1))
    end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
    if start >= size or end < start:
        raise ValueError
    return start, end


def serve_bytes(request: Request, data: bytes, name: str, media_type: str) -> Response:
    disposition = f"attachment; filename=\"{name.encode('ascii', 'replace').decode().replace(chr(34), '')}\"; filename*=UTF-8''{quote(name)}"
    headers = {
        "Content-Disposition": disposition,
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=0",
    }
    try:
        window = parse_range(request.headers.get("range"), len(data))
    except ValueError:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
    if window is None:
        payload, status = data, 200
    else:
        start, end = window
        payload, status = data[start : end + 1], 206
        headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
    headers["Content-Length"] = str(len(payload))
    if request.method == "HEAD":
        return Response(b"", status_code=status, media_type=media_type, headers=headers)
    return Response(payload, status_code=status, media_type=media_type, headers=headers)


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)

    def get_file(p: access.Principal, file_id: str, need: str | None = None, **kw: Any) -> Record:
        return p.get_item("file", file_id, need, **kw)

    # -- read ----------------------------------------------------------------------------
    @api.get("/files/{file_id}")
    async def get_file_route(request: Request, file_id: str) -> Any:
        p = api.principal(request, allow_anonymous=True)
        record = get_file(p, file_id)
        inm = request.headers.get("if-none-match")
        if inm and inm.strip('"') == str(record["_seq"]):
            return Response(status_code=304)
        return api.shaper(p, request).file(record, parse_fields(request))

    # -- update --------------------------------------------------------------------------
    @api.put("/files/{file_id}")
    async def update_file(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request, required=False)
        record = get_file(p, file_id)
        ops.check_if_match(request.headers, record)
        now = now_iso(ctx)
        # Everything that can be refused is checked before anything the request does is kept: the record
        # is restored if a later field is rejected, and the move (the one change that reaches other
        # records) goes last, after its own checks.
        new_parent = None
        if isinstance(body.get("parent"), dict) and "id" in body["parent"]:
            new_parent = p.get_item("folder", str(body["parent"]["id"]), access.UPLOAD)
        if "collections" in body:
            ops.check_collections(p, body["collections"])
        if LOCKED_FIELDS & set(body):
            ops.ensure_unlocked(ctx, record, p)  # a lock stops anyone but its owner changing the file
        backup = copy.deepcopy(record)
        renamed = moved = False
        lock_event: str | None = None
        link_event: str | None = None
        changed = False
        try:
            if "description" in body:
                p.require(record, access.RENAME)
                # Box keeps the first 255 characters of a longer description instead of refusing it.
                record["description"] = opt_str(body, "description")[:255]
                changed = True
            if "tags" in body:
                p.require(record, access.RENAME)
                record["tags"] = str_list(body["tags"], "tags")
                changed = True
            if "disposition_at" in body:
                p.require(record, access.RENAME)
                disposition = (
                    input_ts(body["disposition_at"], "disposition_at") if body["disposition_at"] else None
                )
                current = record.get("disposition_at")
                if current and (disposition is None or disposition < current):
                    raise errors.invalid_parameter(
                        "disposition_at", "The retention expiration date cannot be shortened once set"
                    )
                record["disposition_at"] = disposition
                changed = True
            if "permissions" in body:
                p.require(record, access.SET_SHARE_ACCESS)
                level = object_or_empty(body["permissions"], "permissions").get("can_download")
                if level not in (None, "open", "company"):
                    raise errors.invalid_parameter("permissions.can_download", "Must be 'open' or 'company'")
                if level is not None:
                    record["download_scope"] = level
                    changed = True
            if "lock" in body:
                lock_event = apply_lock(p, record, body["lock"], now)
                changed = True
            if "shared_link" in body:
                link_event = sharing.apply_shared_link(p, record, body["shared_link"], now)
                changed = changed or link_event is not None
            if "name" in body and body["name"] != record["name"]:
                p.require(record, access.RENAME)
                ops.ensure_unlocked(ctx, record, p)
                name = validate_name(body["name"])
                ops.ensure_name_free(p, record["_parent"], name, ignore=record["id"], as_object=True)
                record["name"] = name
                renamed = changed = True
            if new_parent is not None and new_parent["id"] != record["_parent"]:
                p.require(record, access.RENAME)
                ops.ensure_unlocked(ctx, record, p)
                ops.move(p, record, new_parent, now)
                moved = changed = True
        except EmulatorError:
            record.clear()
            record.update(backup)
            p.invalidate()
            raise
        if "collections" in body:
            ops.set_collections(p, record, body["collections"])
        if changed:
            ops.touch(record, p, now)
            p.invalidate()
        for event_type in (
            "ITEM_RENAME" if renamed else None,
            "ITEM_MOVE" if moved else None,
            lock_event,
            link_event,
        ):
            if event_type:
                await events.emit(p, event_type, record)
        return api.shaper(p, request).file(record, parse_fields(request))

    def apply_lock(p: access.Principal, record: Record, value: Any, now: str) -> str:
        p.require(record, access.RENAME)
        current = ops.lock_active(ctx, record)
        if value is None:
            if current and current["created_by"] != p.uid and not p.is_privileged:
                raise errors.box_error(403, "access_denied_item_locked", "Access denied - item locked")
            record["lock"] = None
            return "UNLOCK"
        if not isinstance(value, dict):
            raise errors.invalid_parameter("lock", "'lock' must be an object or null")
        if value.get("access", "lock") != "lock":
            raise errors.invalid_parameter("lock.access", "'access' must be 'lock'")
        if current and current["created_by"] != p.uid:
            raise errors.box_error(403, "access_denied_item_locked", "Access denied - item locked")
        expires = input_ts(value["expires_at"], "lock.expires_at") if value.get("expires_at") else None
        record["lock"] = {
            "id": current["id"] if current else random_alnum(12),
            "created_by": p.uid,
            "created_at": current["created_at"] if current else now,
            "expired_at": expires,
            "is_download_prevented": bool(value.get("is_download_prevented", False)),
        }
        return "LOCK"

    # -- delete / trash ------------------------------------------------------------------
    @api.delete("/files/{file_id}")
    async def delete_file(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        record = get_file(p, file_id, access.DELETE)
        ops.check_if_match(request.headers, record)
        ops.ensure_unlocked(ctx, record, p)
        ops.trash(p, record, now_iso(ctx))
        await events.emit(p, "ITEM_TRASH", record)
        return no_content()

    @api.get("/files/{file_id}/trash")
    async def get_trashed_file(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        record = get_file(p, file_id, allow_trashed=True, only_trashed=True)
        return api.shaper(p, request).file(record, parse_fields(request))

    @api.post("/files/{file_id}")
    async def restore_file(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request, required=False)
        record = get_file(p, file_id, access.DELETE, allow_trashed=True, only_trashed=True)
        parent = p.get_item("folder", ref_id(body, "parent"), access.UPLOAD) if body.get("parent") else None
        name = validate_name(body["name"]) if body.get("name") else None
        ops.restore(p, record, now_iso(ctx), parent=parent, name=name)
        await events.emit(p, "ITEM_UNDELETE_VIA_TRASH", record)
        return created(api.shaper(p, request).file(record, parse_fields(request)))

    @api.delete("/files/{file_id}/trash")
    async def purge_file(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        record = get_file(p, file_id, access.DELETE, allow_trashed=True, only_trashed=True)
        await events.emit(p, "DELETE", record, source=events.item_source(p, record))
        ops.purge(p, record)
        return no_content()

    # -- copy ----------------------------------------------------------------------------
    @api.post("/files/{file_id}/copy", status_code=201)
    async def copy_file(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        source = get_file(p, file_id, access.DOWNLOAD)
        parent = p.get_item("folder", ref_id(body, "parent"), access.UPLOAD)
        name = sanitize_name(body["name"]) if body.get("name") is not None else None
        version = body.get("version")
        if version is not None and db.versions(ctx).get(str(version)) is None:
            raise errors.not_found("Version not found")
        record = ops.copy_file(p, source, parent, name, now_iso(ctx), str(version) if version else None)
        await events.emit(p, "ITEM_COPY", record)
        return created(api.shaper(p, request).file(record, parse_fields(request)))

    # -- content -------------------------------------------------------------------------
    @api.get("/files/{file_id}/content")
    async def file_content(request: Request, file_id: str) -> Any:
        p = api.principal(request, allow_anonymous=True)
        record = get_file(p, file_id, access.DOWNLOAD)
        lock = ops.lock_active(ctx, record)
        if lock and lock.get("is_download_prevented") and lock["created_by"] != p.uid:
            raise errors.forbidden("Access denied - download prevented by lock")
        version_id = request.query_params.get("version")
        if version_id:
            version = db.versions(ctx).get(version_id)
            if version is None or version["_file"] != record["id"] or version.get("trashed_at"):
                raise errors.not_found()
        else:
            version_id = record["_version"]
        token = "b1!" + random_alnum(72)
        expires = to_iso(ctx.clock.now() + DOWNLOAD_TTL)
        ctx.store.collection(db.DOWNLOADS).put(
            {"id": token, "file_id": record["id"], "version_id": version_id, "expires_at": expires}
        )
        for link in p.links:
            shared = link.item.get("shared_link")
            if shared and (link.item["id"] == record["id"] or tree.is_within(ctx, record, link.item["id"])):
                shared["download_count"] = int(shared.get("download_count", 0)) + 1
        if not p.anonymous:
            ops.record_recent(ctx, p.uid, record, "item_preview")
        await events.emit(p, "ITEM_DOWNLOAD", record)
        location = url_for_host(request, ctx, "dl.boxcloud.com", f"/d/1/{token}/download")
        return RedirectResponse(location, status_code=302)

    @app.head("/d/1/{token}/download", include_in_schema=False)
    @app.get("/d/1/{token}/download", operation_id="boxcloud_download")
    async def boxcloud_download(request: Request, token: str) -> Any:
        grant = ctx.store.collection(db.DOWNLOADS).get(token)
        expires = try_parse_ts(grant["expires_at"]) if grant else None
        if grant is None or (expires and utc(expires) <= ctx.clock.now()):
            raise errors.not_found()
        record = db.files(ctx).get(grant["file_id"])
        if record is None:
            raise errors.not_found()
        data = content.file_bytes(ctx, record, grant["version_id"])
        return serve_bytes(request, data, record["name"], content.guess_mime(record["name"]))

    # -- shared-link landing pages (app.box.com) ------------------------------------------
    @app.get("/s/{link_hash}")
    async def shared_landing(request: Request, link_hash: str) -> Any:
        record = access.find_shared_link(ctx, link_hash)
        if record is None or not access.link_is_live(ctx, record) or tree.is_trashed(ctx, record):
            return HTMLResponse("<html><body><h1>Not Found</h1></body></html>", status_code=404)
        link = record["shared_link"]
        link["preview_count"] = int(link.get("preview_count", 0)) + 1
        locked = "This link is password protected." if link.get("password") else ""
        name = html.escape(record["name"])
        page = (
            f"<!doctype html><html><head><title>{name} - Box</title></head><body>"
            f"<h1>{name}</h1><p>Shared {record['type']} (emulated Box shared link).</p><p>{locked}</p></body></html>"
        )
        return HTMLResponse(page)

    @app.get("/shared/static/{name}")
    async def shared_static(request: Request, name: str) -> Any:
        link_hash = name.split(".", 1)[0]
        record = access.find_shared_link(ctx, link_hash)
        if (
            record is None
            or record["type"] != "file"
            or not access.link_is_live(ctx, record)
            or record["shared_link"]["access"] == "collaborators"
            or record["shared_link"]["password"]
            or not record["shared_link"]["permissions"].get("can_download", True)
        ):
            raise errors.not_found()
        record["shared_link"]["download_count"] = int(record["shared_link"].get("download_count", 0)) + 1
        data = content.file_bytes(ctx, record)
        return serve_bytes(request, data, record["name"], content.guess_mime(record["name"]))

    @app.get("/static/img/file_icons/{name}")
    async def file_icon(name: str) -> Any:
        return Response(documents.make_png(32, 32, seed=7), media_type="image/png")

    # -- thumbnails ----------------------------------------------------------------------
    @api.get("/files/{file_id}/thumbnail.{extension}")
    async def thumbnail(request: Request, file_id: str, extension: str) -> Any:
        p = api.principal(request, allow_anonymous=True)
        record = get_file(p, file_id, access.PREVIEW)
        if extension not in ("png", "jpg"):
            raise errors.not_found()
        if extension_of(record["name"]) in NO_THUMBNAIL:
            # Box answers 302 with a generic file-type icon when no thumbnail can be made.
            return RedirectResponse(
                url_for_host(request, ctx, "app.box.com", "/static/img/file_icons/file_default.png"),
                status_code=302,
            )
        valid = PNG_SIZES if extension == "png" else JPG_SIZES
        width = query_int(request, "min_width", 0)
        height = query_int(request, "min_height", 0)
        for name, value in (("min_width", width), ("min_height", height)):
            if value and value not in valid:
                raise errors.invalid_parameter(name, f"'{name}' must be one of {', '.join(map(str, valid))}")
        requested = max(width, height)
        size = next((s for s in PNG_SIZES if s >= requested), 256) if requested else 32
        data = documents.make_png(size, size, seed=int(record["id"]) % 100_000)
        return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    # -- versions ------------------------------------------------------------------------
    def version_or_404(record: Record, version_id: str) -> Record:
        version = db.versions(ctx).get(version_id)
        if version is None or version["_file"] != record["id"]:
            raise errors.not_found()
        return version

    @api.get("/files/{file_id}/versions")
    async def list_versions(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        record = get_file(p, file_id, access.PREVIEW)
        rows = sorted(
            (v for v in db.versions(ctx) if v["_file"] == record["id"] and v["id"] != record["_version"]),
            key=lambda v: v["number"],
            reverse=True,
        )
        offset, limit, use_marker = page_window(request, default_limit=100, max_limit=1000)
        body = paginate(rows, offset, limit, use_marker)
        shaper = api.shaper(p, request)
        fields = parse_fields(request)
        body["entries"] = [
            shaper.version(content.materialize_version(ctx, v), fields) for v in body["entries"]
        ]
        return body

    @api.get("/files/{file_id}/versions/{version_id}")
    async def get_version(request: Request, file_id: str, version_id: str) -> Any:
        p = api.principal(request)
        record = get_file(p, file_id, access.PREVIEW)
        version = content.materialize_version(ctx, version_or_404(record, version_id))
        return api.shaper(p, request).version(version, parse_fields(request))

    @api.delete("/files/{file_id}/versions/{version_id}")
    async def delete_version(request: Request, file_id: str, version_id: str) -> Any:
        p = api.principal(request)
        record = get_file(p, file_id, access.DELETE)
        version = version_or_404(record, version_id)
        if version["id"] == record["_version"]:
            raise errors.forbidden("The current version of a file cannot be deleted")
        ops.ensure_unlocked(ctx, record, p)
        version["trashed_at"] = now_iso(ctx)
        version["trashed_by"] = p.uid
        return no_content()

    @api.put("/files/{file_id}/versions/{version_id}")
    async def restore_version(request: Request, file_id: str, version_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        record = get_file(p, file_id, access.UPLOAD)
        version = content.materialize_version(ctx, version_or_404(record, version_id))
        if "trashed_at" in body and body["trashed_at"] is None:
            version["trashed_at"] = None
            version["trashed_by"] = None
            version["restored_at"] = now_iso(ctx)
            version["restored_by"] = p.uid
        return api.shaper(p, request).version(version, parse_fields(request))

    @api.post("/files/{file_id}/versions/current", status_code=201)
    async def promote_version(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        record = get_file(p, file_id, access.UPLOAD)
        ops.ensure_unlocked(ctx, record, p)
        source = content.materialize_version(
            ctx,
            version_or_404(record, ref_id(body, "version") if "version" in body else str(body.get("id", ""))),
        )
        if source.get("trashed_at"):
            raise errors.not_found()
        data = content.version_bytes(ctx, source)
        ops.add_version(p, record, data, now_iso(ctx))
        await events.emit(p, "ITEM_MAKE_CURRENT_VERSION", record, trigger="FILE.UPLOADED")
        version = db.versions(ctx).get(record["_version"])
        return created(api.shaper(p, request).version(version, parse_fields(request)))  # type: ignore[arg-type]
