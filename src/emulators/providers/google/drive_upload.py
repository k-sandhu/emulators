"""Handle media, multipart/related and resumable HTTP upload protocols."""

from __future__ import annotations

import json
import re
from email import policy
from email.parser import BytesParser
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.google.common import google_error
from emulators.providers.google.drive_access import file_shape, get_file, principal
from emulators.providers.google.drive_fields import select
from emulators.providers.google.drive_mutations import create_file, update_file


def register(app: FastAPI, ctx: ProviderContext) -> None:
    async def upload(request: Request, fid: str | None = None) -> Response:
        actor = principal(request, ctx)
        q = request.query_params
        upload_type = q.get("uploadType")
        if q.get("upload_id"):
            return await resume(request, q["upload_id"])
        if fid:
            get_file(ctx, actor, fid, write=True)
        body = await request.body()
        metadata: dict[str, Any] = {}
        data: bytes | None = None
        content_type = request.headers.get("content-type", "application/octet-stream").split(";")[0]
        if upload_type == "resumable":
            try:
                metadata = json.loads(body) if body else {}
                if not isinstance(metadata, dict):
                    raise ValueError
                total = (
                    int(request.headers["x-upload-content-length"])
                    if "x-upload-content-length" in request.headers
                    else None
                )
                if total is not None and total < 0:
                    raise ValueError
            except (ValueError, TypeError):
                raise google_error(400, "Invalid upload metadata", reason="badRequest") from None
            sid = ctx.ids.token("ADPycd", 48)
            ctx.store.collection("drive_uploads").put_as(
                sid,
                {
                    "_person": actor.person_id,
                    "_client": actor.client_id,
                    "metadata": metadata,
                    "file": fid,
                    "type": request.headers.get(
                        "x-upload-content-type", metadata.get("mimeType", "application/octet-stream")
                    ),
                    "total": total,
                    "blob": ctx.store.blobs.put(b""),
                    "params": dict(q),
                    "complete": False,
                },
            )
            path = "/upload/drive/v3/files" + ("/" + fid if fid else "")
            location = (
                url_for_host(request, ctx, "www.googleapis.com", path)
                + "?uploadType=resumable&upload_id="
                + sid
            )
            return Response(status_code=200, headers={"Location": location, "Content-Length": "0"})
        if upload_type == "media":
            data = body
        elif upload_type == "multipart":
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: "
                + request.headers.get("content-type", "").encode()
                + b"\r\nMIME-Version: 1.0\r\n\r\n"
                + body
            )
            parts = list(message.iter_parts()) if message.is_multipart() else []
            if len(parts) != 2 or parts[0].get_content_type() != "application/json":
                raise google_error(400, "Invalid multipart request with 0 mime parts.", reason="badRequest")
            try:
                metadata = json.loads(parts[0].get_payload(decode=True))
                if not isinstance(metadata, dict):
                    raise ValueError
            except (ValueError, TypeError):
                raise google_error(400, "Invalid JSON metadata", reason="badRequest") from None
            data = parts[1].get_payload(decode=True) or b""
            content_type = parts[1].get_content_type()
        else:
            raise google_error(400, "Invalid uploadType", reason="invalid", location="uploadType")
        row = (
            await update_file(ctx, request, actor, fid, metadata, data, content_type)
            if fid
            else await create_file(ctx, actor, metadata, data, content_type)
        )
        return JSONResponse(
            select(file_shape(ctx, request, row, actor), q.get("fields"), "id,name,mimeType,kind")
        )

    async def resume(request: Request, sid: str) -> Response:
        actor = principal(request, ctx)
        session = ctx.store.collection("drive_uploads").get(sid)
        if not session or session["_person"] != actor.person_id or session["_client"] != actor.client_id:
            raise google_error(404, "Not Found", reason="notFound")
        if session["complete"]:
            return JSONResponse(session["response"])
        data = ctx.store.blobs.get(session["blob"])
        content_range = request.headers.get("content-range", "")
        status = re.fullmatch(r"bytes \*/(\d+|\*)", content_range)
        if status:
            return Response(status_code=308, headers={"Range": f"bytes=0-{len(data) - 1}"} if data else {})
        chunk = await request.body()
        if content_range:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
            if not match:
                raise google_error(400, "Invalid Content-Range", reason="badRequest")
            start, end = int(match[1]), int(match[2])
            total = int(match[3]) if match[3] != "*" else session["total"]
            if (
                start != len(data)
                or end - start + 1 != len(chunk)
                or (
                    total is not None
                    and (end >= total or (session["total"] is not None and total != session["total"]))
                )
            ):
                raise google_error(400, "Invalid Content-Range", reason="badRequest")
            session["total"] = total
        else:
            if data:
                raise google_error(400, "Content-Range required", reason="badRequest")
            if session["total"] is not None and len(chunk) != session["total"]:
                raise google_error(400, "Upload size does not match the declared length", reason="badRequest")
            session["total"] = len(chunk)
        data += chunk
        session["blob"] = ctx.store.blobs.put(data)
        if session["total"] is None or len(data) < session["total"]:
            return Response(status_code=308, headers={"Range": f"bytes=0-{len(data) - 1}"} if data else {})
        if len(data) != session["total"]:
            raise google_error(400, "Upload size does not match Content-Range", reason="badRequest")
        if session["file"]:
            update_request = Request({**request.scope, "query_string": urlencode(session["params"]).encode()})
            row = await update_file(
                ctx, update_request, actor, session["file"], session["metadata"], data, session["type"]
            )
        else:
            row = await create_file(ctx, actor, session["metadata"], data, session["type"])
        session["response"] = select(
            file_shape(ctx, request, row, actor), session["params"].get("fields"), "id,name,mimeType,kind"
        )
        session["complete"] = True
        return JSONResponse(session["response"])

    app.add_api_route("/upload/drive/v3/files", upload, methods=["POST", "PUT"])
    app.add_api_route("/upload/drive/v3/files/{fid}", upload, methods=["PATCH", "PUT"])
