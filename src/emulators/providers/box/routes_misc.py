"""Collections (favourites), recent items and zip downloads."""

from __future__ import annotations

import io
import zipfile
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import utc
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.box import access, content, db, errors, tree
from emulators.providers.box.access import Principal
from emulators.providers.box.routing import Api
from emulators.providers.box.util import (
    box_ts,
    decode_marker,
    encode_marker,
    json_body,
    opt_str,
    page_window,
    paginate,
    parse_fields,
    query_int,
    random_alnum,
    to_iso,
    try_parse_ts,
)

Record = dict[str, Any]
ZIP_TTL = timedelta(hours=1)
MAX_ZIP_ITEMS = 10_000
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)
    collections = db.Live(ctx, db.COLLECTIONS)

    # -- collections ---------------------------------------------------------------------
    def collection_body(coll: Record) -> Record:
        return {
            "type": "collection",
            "id": coll["id"],
            "name": coll["name"],
            "collection_type": coll["collection_type"],
        }

    def mine(p: Principal) -> list[Record]:
        return [c for c in collections if c["user_id"] == p.uid]

    @api.get("/collections")
    async def list_collections(request: Request) -> Any:
        p = api.principal(request)
        rows = mine(p)
        offset, limit, use_marker = page_window(request, default_limit=100, max_limit=1000)
        body = paginate(rows, offset, limit, use_marker)
        body["entries"] = [collection_body(c) for c in body["entries"]]
        return body

    def collection_or_404(p: Principal, collection_id: str) -> Record:
        coll = collections.get(collection_id)
        if coll is None or coll["user_id"] != p.uid:
            raise errors.not_found()
        return coll

    @api.get("/collections/{collection_id}")
    async def get_collection(request: Request, collection_id: str) -> Any:
        p = api.principal(request)
        return collection_body(collection_or_404(p, collection_id))

    @api.get("/collections/{collection_id}/items")
    async def collection_items(request: Request, collection_id: str) -> Any:
        p = api.principal(request)
        coll = collection_or_404(p, collection_id)
        records: list[Record] = []
        for item_type in ("folder", "file"):
            for item_id in coll["items"].get(item_type, []):
                record = db.item(ctx, item_type, item_id)
                if record is not None and p.visible(record) and not tree.is_trashed(ctx, record):
                    records.append(record)
        offset, limit, use_marker = page_window(request, default_limit=100, max_limit=1000)
        body = paginate(records, offset, limit, use_marker)
        shaper = api.shaper(p, request)
        fields = parse_fields(request)
        body["entries"] = [shaper.item(r, fields, listing=True) for r in body["entries"]]
        return body

    # -- recent items --------------------------------------------------------------------
    @api.get("/recent_items")
    async def recent_items(request: Request) -> Any:
        p = api.principal(request)
        rows = sorted(
            (r for r in ctx.store.collection(db.RECENTS) if r["user_id"] == p.uid),
            key=lambda r: (r["interacted_at"], r["id"]),
            reverse=True,
        )
        entries = []
        shaper = api.shaper(p, request)
        fields = parse_fields(request)
        for row in rows:
            record = db.item(ctx, row["item_type"], row["item_id"])
            if record is None or not p.visible(record) or tree.is_trashed(ctx, record):
                continue
            entries.append(
                {
                    "type": "recent_item",
                    "item": shaper.item(record, fields),
                    "interaction_type": row["interaction_type"],
                    "interacted_at": box_ts(row["interacted_at"]),
                    "interaction_shared_link": None,
                }
            )
        limit = query_int(request, "limit", 100, minimum=1, maximum=1000)
        offset = decode_marker(request.query_params.get("marker"))
        window = entries[offset : offset + limit]
        more = offset + limit < len(entries)
        return {
            "limit": limit,
            "next_marker": encode_marker(offset + limit) if more else None,
            "entries": window,
        }

    # -- zip downloads -------------------------------------------------------------------
    def collect(p: Principal, item: Record, prefix: str, out: list[tuple[str, Record]]) -> None:
        if item["type"] == "file":
            if access.DOWNLOAD in p.caps(item):
                out.append((f"{prefix}{item['name']}", item))
            return
        if item["type"] == "folder":
            for child in sorted(p.tree.children(item["id"]), key=lambda r: r["name"].lower()):
                if p.visible(child):
                    collect(p, child, f"{prefix}{item['name']}/", out)

    @api.post("/zip_downloads", status_code=202)
    async def create_zip(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise errors.missing_parameter("items")
        if len(items) > MAX_ZIP_ITEMS:
            raise errors.invalid_parameter("items", f"At most {MAX_ZIP_ITEMS} items may be requested")
        entries: list[tuple[str, Record]] = []
        for ref in items:
            if not isinstance(ref, dict) or ref.get("type") not in ("file", "folder") or not ref.get("id"):
                raise errors.invalid_parameter(
                    "items", "Each item needs a 'type' (file or folder) and an 'id'"
                )
            record = p.get_item(ref["type"], str(ref["id"]), access.DOWNLOAD)
            collect(p, record, "", entries)
        # Items that share a path are *all* renamed (``name (1).ext``, ``name (2).ext``...) and reported
        # together, so a conflict always lists at least two items.
        groups: dict[str, list[tuple[str, Record]]] = {}
        for path, record in entries:
            groups.setdefault(path.lower(), []).append((path, record))
        downloads: dict[str, str] = {}
        conflicts: list[list[dict[str, str]]] = []
        for members in groups.values():
            if len(members) < 2:
                continue
            conflict = []
            for number, (path, record) in enumerate(members, start=1):
                downloads[record["id"]] = _dedupe(path, number)
                conflict.append(
                    {
                        "id": record["id"],
                        "type": "file",
                        "original_name": path.rsplit("/", 1)[-1],
                        "download_name": downloads[record["id"]].rsplit("/", 1)[-1],
                    }
                )
            conflicts.append(conflict)
        files = [
            {"path": downloads.get(record["id"], path), "file_id": record["id"]} for path, record in entries
        ]
        zid = random_alnum(24)
        ctx.store.collection(db.ZIPS).put(
            {
                "id": zid,
                "files": files,
                "name": opt_str(body, "download_file_name", "") or "Download",
                "expires_at": to_iso(ctx.clock.now() + ZIP_TTL),
                "created_by": p.uid,
            }
        )
        return JSONResponse(
            {
                "download_url": url_for_host(
                    request, ctx, "dl.boxcloud.com", f"/2.0/zip_downloads/{zid}/content"
                ),
                "status_url": url_for_host(request, ctx, "api.box.com", f"/2.0/zip_downloads/{zid}/status"),
                "expires_at": box_ts(to_iso(ctx.clock.now() + ZIP_TTL)),
                "name_conflicts": conflicts,
            },
            status_code=202,
        )

    def zip_or_404(zip_id: str) -> Record:
        record = ctx.store.collection(db.ZIPS).get(zip_id)
        expires = try_parse_ts(record["expires_at"]) if record else None
        if record is None or (expires and utc(expires) <= ctx.clock.now()):
            raise errors.not_found()
        return record

    @api.get("/zip_downloads/{zip_id}/status")
    async def zip_status(request: Request, zip_id: str) -> Any:
        api.principal(request)
        record = zip_or_404(zip_id)
        return {
            "total_file_count": len(record["files"]),
            "downloaded_file_count": len(record["files"]),
            "skipped_file_count": 0,
            "skipped_folder_count": 0,
            "state": "succeeded",
        }

    @api.get("/zip_downloads/{zip_id}/content")
    async def zip_content(request: Request, zip_id: str) -> Any:
        record = zip_or_404(zip_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in record["files"]:
                file_record = db.files(ctx).get(entry["file_id"])
                if file_record is None:
                    continue
                info = zipfile.ZipInfo(entry["path"], date_time=ZIP_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content.file_bytes(ctx, file_record))
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{record["name"]}.zip"'},
        )


def _dedupe(path: str, n: int) -> str:
    stem, dot, ext = path.rpartition(".")
    if not dot or "/" in ext:
        return f"{path} ({n})"
    return f"{stem} ({n}).{ext}"
