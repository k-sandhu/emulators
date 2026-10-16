"""Comments, staged file uploads, and expiring binary downloads."""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import re
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile

from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.notion.auth import capability
from emulators.providers.notion.blocks import container
from emulators.providers.notion.common import (
    BOT_ID,
    authenticate,
    body,
    fail,
    invalid,
    paginate,
    partial_user,
    public,
    require,
    rich_text,
    serialize,
    stamp,
    uid,
)
from emulators.providers.notion.events import emit
from emulators.providers.notion.validation import editable, string
from emulators.world.documents import render


def upload_metadata(filename: Any, content_type: Any) -> tuple[str | None, str | None]:
    if filename is not None:
        string(filename, "filename", empty=False)
        filename = re.sub(r"\s", "_", filename.replace('"', ""))
    if content_type and not re.fullmatch(r"[\w.+-]+/[\w.+-]+", content_type):
        invalid("content_type must be a MIME type string.")
    if not content_type or content_type == "application/octet-stream":
        content_type = mimetypes.guess_type(filename)[0] if filename else None
    if filename and content_type and "." not in filename.rsplit("/", 1)[-1]:
        filename += mimetypes.guess_extension(content_type) or ""
    if filename and len(filename.encode("utf-8")) > 900:
        invalid("filename must be at most 900 bytes including its extension.")
    return filename, content_type


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/v1/comments")
    async def comments(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "read_comments")
        record = container(ctx, grant, request.query_params.get("block_id", ""))
        rows = ctx.store["comments"].where(_parent_id=record["id"])
        result = paginate(
            ctx, rows, request.query_params, kind="comment", key="comments:" + record["id"], grant=grant
        )
        result["results"] = [serialize(request, ctx, record, grant) for record in result["results"]]
        return result

    @app.post("/v1/comments")
    async def create_comment(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "insert_comments")
        payload = await body(request)
        if bool(payload.get("parent")) == bool(payload.get("discussion_id")):
            invalid("Specify exactly one of parent.page_id or discussion_id.")
        if payload.get("discussion_id"):
            discussion_id = uid(payload["discussion_id"])
            first = ctx.store["comments"].first(discussion_id=discussion_id)
            if first is None:
                fail(404, "object_not_found", "Could not find discussion.")
            record = container(ctx, grant, first["_parent_id"])
        else:
            if not isinstance(payload["parent"], dict):
                invalid("parent must be an object containing page_id.")
            record = require(ctx, grant, "pages", payload["parent"].get("page_id"), comment=True)
            discussion_id = ctx.ids.guid()
        if payload.get("discussion_id"):
            require(
                ctx, grant, "pages" if record["object"] == "page" else "blocks", record["id"], comment=True
            )
        editable(ctx, record)
        if not payload.get("rich_text"):
            invalid("rich_text is required.")
        comment = {
            "object": "comment",
            "id": ctx.ids.guid(),
            "parent": {
                "type": "page_id" if record["object"] == "page" else "block_id",
                "page_id" if record["object"] == "page" else "block_id": record["id"],
            },
            "discussion_id": discussion_id,
            "created_time": stamp(ctx.clock.now()),
            "last_edited_time": stamp(ctx.clock.now()),
            "created_by": partial_user(BOT_ID),
            "rich_text": rich_text(payload["rich_text"], ctx),
            "_parent_id": record["id"],
        }
        ctx.store["comments"].put(comment)
        await emit(ctx, "comment.created", comment["id"], "comment", {"page_id": record["id"]})
        return serialize(request, ctx, comment, grant)

    def upload_record(upload_id: str, client_id: str) -> dict[str, Any]:
        record = ctx.store["file_uploads"].get(uid(upload_id))
        if record is None or record["_client_id"] != client_id:
            fail(404, "object_not_found", "Could not find file upload.")
        if record["expiry_time"] and record["expiry_time"] <= stamp(ctx.clock.now()):
            record["status"] = "expired"
        return record

    def upload_wire(request: Request, record: dict[str, Any]) -> dict[str, Any]:
        result = public(record)
        if record["_mode"] == "multi_part":
            result["number_of_parts"] = {"total": record["_number_of_parts"], "sent": len(record["_parts"])}
        if record["status"] == "pending":
            result["upload_url"] = url_for_host(
                request, ctx, "api.notion.com", f"/v1/file_uploads/{record['id']}/send"
            )
            if record["_mode"] == "multi_part":
                result["complete_url"] = url_for_host(
                    request, ctx, "api.notion.com", f"/v1/file_uploads/{record['id']}/complete"
                )
        return result

    @app.post("/v1/file_uploads")
    async def create_upload(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "insert_content")
        payload = await body(request)
        mode = payload.get("mode", "single_part")
        if mode not in ("single_part", "multi_part"):
            invalid(
                "Supported upload modes are single_part and multi_part. External URL imports are unavailable."
            )
        count = payload.get("number_of_parts", 1)
        if mode == "multi_part" and (
            "number_of_parts" not in payload
            or not isinstance(count, int)
            or isinstance(count, bool)
            or not 1 <= count <= 10000
            or not payload.get("filename")
        ):
            invalid("multi_part requires filename and number_of_parts from 1 to 10000.")
        if "number_of_parts" in payload and (
            not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 10000
        ):
            invalid("number_of_parts must be an integer from 1 to 10000.")
        filename = payload.get("filename")
        if "filename" in payload and not isinstance(filename, str):
            invalid("filename must be a string.")
        if filename is not None and (not isinstance(filename, str) or len(filename.encode()) > 900):
            invalid("filename must be at most 900 bytes.")
        content_type = payload.get("content_type")
        if "content_type" in payload and (
            not isinstance(content_type, str) or not re.fullmatch(r"[\w.+-]+/[\w.+-]+", content_type)
        ):
            invalid("content_type must be a MIME type string.")
        filename, content_type = upload_metadata(filename, content_type)
        record = {
            "object": "file_upload",
            "id": ctx.ids.guid(),
            "created_time": stamp(ctx.clock.now()),
            "last_edited_time": stamp(ctx.clock.now()),
            "created_by": {"id": BOT_ID, "type": "bot"},
            "in_trash": False,
            "expiry_time": stamp(ctx.clock.now() + timedelta(hours=1)),
            "status": "pending",
            "filename": filename,
            "content_type": content_type,
            "content_length": None,
            "_client_id": grant.client_id,
            "_mode": mode,
            "_number_of_parts": count,
            "_parts": {},
        }
        ctx.store["file_uploads"].put(record)
        return upload_wire(request, record)

    @app.get("/v1/file_uploads")
    async def uploads(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        rows = [
            upload_record(r["id"], grant.client_id)
            for r in ctx.store["file_uploads"]
            if r["_client_id"] == grant.client_id
        ]
        if status := request.query_params.get("status"):
            if status not in ("pending", "uploaded", "expired", "failed"):
                invalid("Invalid upload status.")
            rows = [r for r in rows if r["status"] == status]
        result = paginate(
            ctx,
            rows,
            request.query_params,
            kind="file_upload",
            key="uploads:" + request.query_params.get("status", ""),
            grant=grant,
        )
        result["results"] = [upload_wire(request, r) for r in result["results"]]
        return result

    @app.get("/v1/file_uploads/{file_upload_id}")
    async def retrieve_upload(file_upload_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        return upload_wire(request, upload_record(file_upload_id, grant.client_id))

    @app.post("/v1/file_uploads/{file_upload_id}/send")
    async def send_upload(file_upload_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "insert_content")
        record = upload_record(file_upload_id, grant.client_id)
        if record["status"] != "pending":
            invalid("File upload is no longer pending.")
        try:
            form = await request.form()
        except (ValueError, AssertionError):
            invalid("Expected multipart/form-data with a file field.")
        file = form.get("file")
        if not isinstance(file, UploadFile):
            invalid("file must be provided as multipart/form-data.")
        content = await file.read()
        await file.close()
        if len(content) > 20 * 1024 * 1024:
            invalid("A file part must be no larger than 20 MiB.")
        part = 1
        if record["_mode"] == "multi_part":
            try:
                part = int(str(form.get("part_number")))
            except ValueError:
                invalid("multi_part send requires part_number.")
            if not 1 <= part <= record["_number_of_parts"]:
                invalid("part_number is outside number_of_parts.")
        filename, content_type = upload_metadata(
            record["filename"]
            or (file.filename or "untitled").replace("%22", '"').replace("%0A", "\n").replace("%0D", "\r"),
            record["content_type"] or file.content_type,
        )
        blob = ctx.store.blobs.put(content)
        record["_parts"][str(part)] = blob
        record["content_length"] = sum(ctx.store.blobs.size(key) for key in record["_parts"].values())
        record["filename"] = filename
        record["content_type"] = content_type or "application/octet-stream"
        record["last_edited_time"] = stamp(ctx.clock.now())
        if record["_mode"] == "single_part":
            record["status"] = "uploaded"
            ctx.store["files"].put(
                {
                    "id": record["id"],
                    "filename": record["filename"],
                    "_blob": blob,
                    "_content_type": record["content_type"],
                }
            )
        return upload_wire(request, record)

    @app.post("/v1/file_uploads/{file_upload_id}/complete")
    async def complete_upload(file_upload_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "insert_content")
        if await request.body():
            await body(request)
        record = upload_record(file_upload_id, grant.client_id)
        if record["_mode"] != "multi_part" or record["status"] != "pending":
            invalid("Only pending multi_part uploads can be completed.")
        if len(record["_parts"]) != record["_number_of_parts"]:
            invalid("All file parts must be uploaded before completing.")
        content = b"".join(
            ctx.store.blobs.get(record["_parts"][str(i)]) for i in range(1, record["_number_of_parts"] + 1)
        )
        blob = ctx.store.blobs.put(content)
        record.update(status="uploaded", content_length=len(content), last_edited_time=stamp(ctx.clock.now()))
        ctx.store["files"].put(
            {
                "id": record["id"],
                "filename": record["filename"],
                "_blob": blob,
                "_content_type": record["content_type"],
            }
        )
        return upload_wire(request, record)

    @app.get("/secure/{file_id}/{filename:path}")
    async def download(file_id: str, filename: str, request: Request) -> Response:
        try:
            expires = int(request.query_params.get("expires", ""))
        except ValueError:
            fail(403, "restricted_resource", "Invalid signed file URL.")
        expected = hmac.new(
            ctx.store.meta["file_secret"].encode(), f"{file_id}:{expires}".encode(), hashlib.sha256
        ).hexdigest()
        if expires <= int(ctx.clock.now().timestamp()) or not hmac.compare_digest(
            expected, request.query_params.get("signature", "")
        ):
            fail(403, "restricted_resource", "File URL is invalid or has expired.")
        record = ctx.store["files"].get(file_id)
        if record is None:
            fail(404, "object_not_found", "File not found.")
        if not record.get("_blob"):
            doc = ctx.world.document(record["_world_document"])
            content, mime = render(
                doc.kind,
                doc.title,
                list(doc.paragraphs),
                author=ctx.world.person(doc.owner_id).display_name,
                created=doc.created,
                table=[list(r) for r in doc.table] if doc.table else None,
            )
            record.update(_blob=ctx.store.blobs.put(content), _content_type=mime)
        return Response(
            ctx.store.blobs.get(record["_blob"]),
            media_type=record["_content_type"],
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(record["filename"])},
        )
