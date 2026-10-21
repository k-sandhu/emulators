"""Uploaded and seeded attachments retain real bytes behind expiring signed URLs."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.asana import common as c
from emulators.providers.asana.events import emit
from emulators.world.documents import render


def materialize(ctx: ProviderContext, row: dict[str, Any]) -> bytes:
    if row.get("_blob") is None:
        doc = ctx.world.document(row["_document"])
        content, mime = render(
            doc.kind,
            doc.title,
            list(doc.paragraphs),
            author=ctx.world.person(doc.owner_id).display_name,
            created=doc.created,
            table=[list(values) for values in doc.table] if doc.table else None,
        )
        row.update(_blob=ctx.store.blobs.put(content), _mime=mime, size=len(content))
    return ctx.store.blobs.get(row["_blob"])


def download_url(request: Request, ctx: ProviderContext, row: dict[str, Any]) -> str:
    materialize(ctx, row)
    expiry = ctx.clock.timestamp() + 3600
    signature = hmac.new(
        ctx.store.meta["file_secret"].encode(), f"{row['gid']}:{expiry}".encode(), hashlib.sha256
    ).hexdigest()
    return url_for_host(
        request,
        ctx,
        c.HOST,
        f"{c.BASE}/attachments/{row['gid']}/download?expires={expiry}&signature={signature}",
    )


def parent(ctx: ProviderContext, grant: Any, resource_id: Any) -> dict[str, Any]:
    if not isinstance(resource_id, str):
        c.invalid("parent: Not a valid ID")
    row = c.find(ctx, resource_id)
    if row is None or row["resource_type"] not in ("task", "project"):
        raise c.error(404, "Not Found")
    if not c.allowed(ctx, grant, row):
        raise c.error(403, "Forbidden")
    return row


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get(c.BASE + "/attachments")
    @app.get(c.BASE + "/tasks/{task_gid}/attachments")
    async def listing(request: Request, task_gid: str | None = None) -> dict[str, Any]:
        grant = c.principal(request, ctx, "attachments:read")
        record = parent(ctx, grant, task_gid or request.query_params.get("parent"))
        rows = [
            row
            for row in ctx.store["attachments"]
            if row["parent"]["gid"] == record["gid"] and c.allowed(ctx, grant, row)
        ]
        return c.page(request, ctx, grant, rows)

    @app.post(c.BASE + "/attachments")
    @app.post(c.BASE + "/tasks/{task_gid}/attachments")
    async def upload(request: Request, task_gid: str | None = None) -> Any:
        grant = c.principal(request, ctx, "attachments:write")
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            c.invalid("Attachment uploads must use multipart/form-data")
        form = await request.form()
        record = parent(ctx, grant, task_gid or form.get("parent") or request.query_params.get("parent"))
        upload_file = form.get("file")
        if not isinstance(upload_file, UploadFile) or not upload_file.filename:
            c.invalid("file: Missing attachment data")
        try:
            name = unquote(upload_file.filename, errors="strict")
        except UnicodeError:
            c.invalid("file: Invalid filename encoding")
        c.string(name, "filename", empty=False)
        mime = upload_file.content_type or "application/octet-stream"
        if not re.fullmatch(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+", mime):
            c.invalid("file: Invalid MIME type")
        content = await upload_file.read(100 * 1024 * 1024 + 1)
        if len(content) > 100 * 1024 * 1024:
            c.invalid("Attachments must not exceed 100 MB")
        row = c.put(
            ctx,
            "attachments",
            {
                "gid": c.new_gid(ctx),
                "resource_type": "attachment",
                "resource_subtype": "asana",
                "name": name,
                "created_at": c.stamp(ctx),
                "host": "asana",
                "parent": c.compact(record),
                "size": len(content),
                "_blob": ctx.store.blobs.put(content),
                "_mime": mime,
            },
        )
        await emit(ctx, grant, row, "added")
        return JSONResponse({"data": c.render(request, ctx, grant, row, brief=True)}, status_code=201)

    @app.get(c.BASE + "/attachments/{attachment_gid}")
    async def retrieve(attachment_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "attachments:read")
        return {"data": c.render(request, ctx, grant, c.require(ctx, grant, "attachments", attachment_gid))}

    @app.delete(c.BASE + "/attachments/{attachment_gid}")
    async def delete(attachment_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "attachments:delete")
        row = c.require(ctx, grant, "attachments", attachment_gid)
        await emit(ctx, grant, row, "deleted")
        row["_deleted"] = True
        return {"data": {}}

    @app.get(c.BASE + "/attachments/{attachment_gid}/download")
    async def download(attachment_gid: str, request: Request) -> Response:
        row = ctx.store["attachments"].get(attachment_gid)
        if row is None or row.get("_deleted"):
            raise c.error(404, "Not Found")
        expires, signature = (
            request.query_params.get("expires", ""),
            request.query_params.get("signature", ""),
        )
        if not re.fullmatch(r"\d{1,16}", expires):
            raise c.error(403, "Invalid download URL")
        expected = hmac.new(
            ctx.store.meta["file_secret"].encode(), f"{attachment_gid}:{expires}".encode(), hashlib.sha256
        ).hexdigest()
        if int(expires) <= ctx.clock.timestamp() or not hmac.compare_digest(signature, expected):
            raise c.error(403, "The download URL has expired or is invalid")
        content = materialize(ctx, row)
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["name"], safe=""),
        }
        range_header = request.headers.get("range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d{0,16})-(\d{0,16})", range_header)
            if not match or not any(match.groups()):
                raise c.error(416, "Invalid byte range")
            first, last = match.groups()
            start = int(first) if first else max(0, len(content) - int(last))
            end = min(int(last), len(content) - 1) if first and last else len(content) - 1
            if start > end or start >= len(content):
                raise c.error(416, "Invalid byte range")
            headers["Content-Range"] = f"bytes {start}-{end}/{len(content)}"
            return Response(
                content[start : end + 1], status_code=206, media_type=row["_mime"], headers=headers
            )
        return Response(content, media_type=row["_mime"], headers=headers)
