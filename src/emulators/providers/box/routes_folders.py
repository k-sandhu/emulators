"""Folders: get, list items, create, update, delete (trash), copy, trash listing/restore/purge."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.provider import ProviderContext
from emulators.providers.box import access, db, errors, events, ops, shapes, sharing
from emulators.providers.box.routing import Api, created, no_content
from emulators.providers.box.shapes import FOLDER_ITEM_ORDER, Shaper
from emulators.providers.box.util import (
    json_body,
    now_iso,
    page_window,
    paginate,
    parse_fields,
    ref_id,
    require_str,
    validate_name,
)

Record = dict[str, Any]
SORTS = ("id", "name", "date", "size")


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)

    def listing_body(p: access.Principal, shaper: Shaper, request: Request, records: list[Record], *, sort: str | None, direction: str) -> dict[str, Any]:
        offset, limit, use_marker = page_window(request, default_limit=100, max_limit=1000)
        fields = parse_fields(request)
        order = [{"by": sort, "direction": direction.upper()}] if sort else list(FOLDER_ITEM_ORDER)
        body = paginate(records, offset, limit, use_marker, order=order)
        body["entries"] = [shaper.item(r, fields, listing=True) for r in body["entries"]]
        return body

    def sort_params(request: Request) -> tuple[str | None, str]:
        sort = request.query_params.get("sort")
        if sort is not None and sort not in SORTS:
            raise errors.invalid_parameter("sort", f"'sort' must be one of {', '.join(SORTS)}")
        direction = (request.query_params.get("direction") or "ASC").upper()
        if direction not in ("ASC", "DESC"):
            raise errors.invalid_parameter("direction", "'direction' must be ASC or DESC")
        return sort, direction

    # -- trash listing (declared first: "trash" must not be read as a folder id) ---------
    @api.get("/folders/trash/items")
    async def trash_items(request: Request) -> Any:
        p = api.principal(request)
        sort, direction = sort_params(request)
        records = [
            r
            for coll in (db.folders(ctx), db.files(ctx), ctx.store.collection(db.WEB_LINKS))
            for r in coll
            if r.get("_trash") and r.get("_owner") == p.uid
        ]
        key = {"id": lambda r: int(r["id"]), "name": lambda r: r["name"].lower(), "date": lambda r: r.get("trashed_at") or "", "size": lambda r: int(r.get("size", 0))}
        records.sort(key=key.get(sort or "date", key["date"]), reverse=direction == "DESC" if sort else True)
        return listing_body(p, api.shaper(p, request), request, records, sort=sort, direction=direction)

    # -- read ----------------------------------------------------------------------------
    @api.get("/folders/{folder_id}")
    async def get_folder(request: Request, folder_id: str) -> Any:
        p = api.principal(request, allow_anonymous=True)
        record = p.get_item("folder", folder_id)
        inm = request.headers.get("if-none-match")
        if inm and not db.is_root(record) and inm.strip('"') == str(record["_seq"]):
            return Response(status_code=304)
        return api.shaper(p, request).folder(record, parse_fields(request))

    @api.get("/folders/{folder_id}/items")
    async def folder_items(request: Request, folder_id: str) -> Any:
        p = api.principal(request, allow_anonymous=True)
        folder = p.get_item("folder", folder_id)
        caps = p.caps(folder)
        if access.PREVIEW not in caps and access.DOWNLOAD not in caps:
            raise errors.forbidden()
        sort, direction = sort_params(request)
        records = shapes.sorted_children(p, folder, sort=sort, direction=direction)
        return listing_body(p, api.shaper(p, request), request, records, sort=sort, direction=direction)

    # -- create --------------------------------------------------------------------------
    @api.post("/folders")
    async def create_folder(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        name = validate_name(require_str(body, "name"))
        parent = p.get_item("folder", ref_id(body, "parent"), access.UPLOAD)
        now = now_iso(ctx)
        record = ops.create_folder(p, parent, name, now)
        if isinstance(body.get("folder_upload_email"), dict):
            record["folder_upload_email"] = upload_email(record, body["folder_upload_email"])
        await events.emit(p, "ITEM_CREATE", record)
        return created(api.shaper(p, request).folder(record, parse_fields(request)))

    # -- update --------------------------------------------------------------------------
    @api.put("/folders/{folder_id}")
    async def update_folder(request: Request, folder_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        record = p.get_item("folder", folder_id)
        if db.is_root(record):
            raise errors.forbidden()
        ops.check_if_match(request.headers, record)
        now = now_iso(ctx)
        emitted: list[tuple[str, Record | None]] = []
        if "name" in body and body["name"] != record["name"]:
            p.require(record, access.RENAME)
            name = validate_name(body["name"])
            ops.ensure_name_free(p, record["_parent"], name, ignore=record["id"])
            record["name"] = name
            emitted.append(("ITEM_RENAME", None))
        if isinstance(body.get("parent"), dict) and "id" in body["parent"]:
            new_parent = p.get_item("folder", str(body["parent"]["id"]), access.UPLOAD)
            if new_parent["id"] != record["_parent"]:
                p.require(record, access.RENAME)
                ops.move(p, record, new_parent, now)
                emitted.append(("ITEM_MOVE", None))
        for key in ("description", "can_non_owners_invite", "can_non_owners_view_collaborators", "is_collaboration_restricted_to_enterprise", "sync_state"):
            if key in body:
                p.require(record, access.RENAME)
                if key == "description" and not isinstance(body[key], str):
                    raise errors.invalid_parameter("description")
                record[key] = body[key]
        if "tags" in body:
            p.require(record, access.RENAME)
            record["tags"] = [str(t) for t in (body["tags"] or [])]
        if "folder_upload_email" in body:
            p.require(record, access.RENAME)
            record["folder_upload_email"] = upload_email(record, body["folder_upload_email"])
        if "collections" in body:
            ops.set_collections(p, record, body["collections"])
        if "shared_link" in body:
            event_type = sharing.apply_shared_link(p, record, body["shared_link"], now)
            if event_type:
                emitted.append((event_type, None))
        if emitted or any(k in body for k in ("description", "tags", "folder_upload_email")):
            ops.touch(record, p, now)
            p.invalidate()
        for event_type, _ in emitted:
            await events.emit(p, event_type, record)
        return api.shaper(p, request).folder(record, parse_fields(request))

    # -- delete --------------------------------------------------------------------------
    @api.delete("/folders/{folder_id}")
    async def delete_folder(request: Request, folder_id: str) -> Any:
        p = api.principal(request)
        record = p.get_item("folder", folder_id)
        if db.is_root(record):
            raise errors.forbidden()
        p.require(record, access.DELETE)
        ops.check_if_match(request.headers, record)
        recursive = request.query_params.get("recursive", "false").lower() == "true"
        if not recursive and p.tree.children(record["id"]):
            raise errors.box_error(400, "folder_not_empty", "Folder not empty")
        ops.trash(p, record, now_iso(ctx))
        await events.emit(p, "ITEM_TRASH", record)
        return no_content()

    @api.post("/folders/{folder_id}/copy", status_code=201)
    async def copy_folder(request: Request, folder_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        source = p.get_item("folder", folder_id, access.DOWNLOAD)
        if db.is_root(source):
            raise errors.forbidden()
        parent = p.get_item("folder", ref_id(body, "parent"), access.UPLOAD)
        name = validate_name(body["name"]) if body.get("name") is not None else None
        record = ops.copy_folder(p, source, parent, name, now_iso(ctx))
        await events.emit(p, "ITEM_COPY", record)
        return created(api.shaper(p, request).folder(record, parse_fields(request)))

    # -- trash ---------------------------------------------------------------------------
    @api.get("/folders/{folder_id}/trash")
    async def get_trashed(request: Request, folder_id: str) -> Any:
        p = api.principal(request)
        record = p.get_item("folder", folder_id, allow_trashed=True, only_trashed=True)
        return api.shaper(p, request).folder(record, parse_fields(request))

    @api.post("/folders/{folder_id}")
    async def restore_folder(request: Request, folder_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request, required=False)
        record = p.get_item("folder", folder_id, access.DELETE, allow_trashed=True, only_trashed=True)
        parent = p.get_item("folder", ref_id(body, "parent"), access.UPLOAD) if body.get("parent") else None
        name = validate_name(body["name"]) if body.get("name") else None
        ops.restore(p, record, now_iso(ctx), parent=parent, name=name)
        await events.emit(p, "ITEM_UNDELETE_VIA_TRASH", record)
        return created(api.shaper(p, request).folder(record, parse_fields(request)))

    @api.delete("/folders/{folder_id}/trash")
    async def purge_folder(request: Request, folder_id: str) -> Any:
        p = api.principal(request)
        record = p.get_item("folder", folder_id, access.DELETE, allow_trashed=True, only_trashed=True)
        snapshot = events.item_source(p, record)
        ops.purge(p, record)
        events.append_event(ctx, "DELETE", created_by=p.uid, source=snapshot, item=None)
        return no_content()



def upload_email(record: Record, value: Any) -> Record | None:
    if value is None:
        return None
    level = value.get("access") if isinstance(value, dict) else None
    if level not in ("open", "collaborators"):
        raise errors.invalid_parameter("folder_upload_email.access", "access must be 'open' or 'collaborators'")
    existing = record.get("folder_upload_email")
    address = existing["email"] if existing else f"upload.{record['id']}@u.box.com"
    return {"access": level, "email": address}

