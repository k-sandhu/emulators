"""Uploads on ``upload.box.com``: multipart upload, new versions, preflight and chunked sessions."""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from emulators.core.clock import utc
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.box import access, db, errors, events, ops
from emulators.providers.box.routing import UPLOAD_API, Api, created, no_content
from emulators.providers.box.util import (
    box_ts,
    input_ts,
    json_body,
    now_iso,
    opt_str,
    parse_fields,
    query_int,
    ref_id,
    require_str,
    sha1_b64,
    sha1_hex,
    to_iso,
    try_parse_ts,
    validate_name,
)

Record = dict[str, Any]
SESSION_TTL = timedelta(days=7)
MAX_FILE_SIZE = 50 * 1024 * 1024 * 1024
DEFAULT_PART_SIZE = 8_388_608


def part_size(ctx: ProviderContext) -> int:
    return int(ctx.store.meta.get("settings", {}).get("upload_part_size", DEFAULT_PART_SIZE))


def bad_digest() -> Exception:
    return errors.box_error(400, "bad_digest", "The specified content-md5 did not match what we received")


def check_content_md5(request: Request, data: bytes) -> None:
    """Box calls this header Content-MD5 but it carries the SHA-1 hex digest of the content."""
    expected = request.headers.get("content-md5")
    if expected and expected.strip().lower() != sha1_hex(data):
        raise bad_digest()


def parse_digest(request: Request) -> str | None:
    header = request.headers.get("digest")
    if not header:
        return None
    match = re.fullmatch(r"\s*sha=(.+?)\s*", header)
    return match.group(1) if match else None


async def read_multipart(request: Request) -> tuple[dict[str, Any], bytes, str]:
    """``(attributes, file bytes, filename)`` from a Box multipart upload."""
    try:
        form = await request.form()
    except Exception:  # malformed multipart body
        raise errors.bad_request("Invalid multipart request", reason="invalid_request_body") from None
    attributes_raw = form.get("attributes")
    upload = form.get("file")
    order = [name for name, _ in form.multi_items()]
    if "attributes" in order and "file" in order and order.index("file") < order.index("attributes"):
        # Box: "The attributes part of the body must come before the file part".
        raise errors.bad_request(
            "The 'attributes' part must come before the 'file' part", reason="invalid_request_body"
        )
    if upload is None or isinstance(upload, str):
        raise errors.missing_parameter("file")
    data = await upload.read()
    attributes: dict[str, Any] = {}
    if attributes_raw is not None:
        try:
            text = (
                attributes_raw if isinstance(attributes_raw, str) else (await attributes_raw.read()).decode()
            )
            attributes = json.loads(text)
        except (ValueError, RecursionError):
            raise errors.invalid_parameter("attributes", "'attributes' must be valid JSON") from None
        if not isinstance(attributes, dict):
            raise errors.invalid_parameter("attributes", "'attributes' must be a JSON object")
    return attributes, data, upload.filename or ""


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)

    def upload_url(request: Request, path: str) -> str:
        return url_for_host(request, ctx, "upload.box.com", f"{UPLOAD_API}{path}")

    # -- preflight -----------------------------------------------------------------------
    @api.options("/files/content")
    async def preflight(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        name = validate_name(require_str(body, "name"))
        parent = p.get_item("folder", ref_id(body, "parent"), access.UPLOAD)
        size = body.get("size")
        if size is not None and (not isinstance(size, int) or size < 0):
            raise errors.invalid_parameter("size")
        if size is not None and size > MAX_FILE_SIZE:
            raise errors.box_error(413, "file_size_limit_exceeded", "File size exceeds the file size limit.")
        ops.ensure_name_free(p, parent["id"], name, as_object=True)
        return {"upload_token": "", "upload_url": upload_url(request, "/files/content")}

    @api.options("/files/{file_id}/content")
    async def preflight_version(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request, required=False)
        record = p.get_item("file", file_id, access.UPLOAD)
        if body.get("name"):
            ops.ensure_name_free(
                p, record["_parent"], validate_name(body["name"]), ignore=record["id"], as_object=True
            )
        return {"upload_token": "", "upload_url": upload_url(request, f"/files/{file_id}/content")}

    # -- simple upload -------------------------------------------------------------------
    async def finish(request: Request, p: access.Principal, record: Record, event_type: str) -> JSONResponse:
        await events.emit(p, event_type, record)
        shaper = api.shaper(p, request)
        return created({"total_count": 1, "entries": [shaper.file(record, parse_fields(request))]})

    @api.post("/files/content", prefix=UPLOAD_API)
    async def upload_file(request: Request) -> Any:
        p = api.principal(request)
        attributes, data, filename = await read_multipart(request)
        name = validate_name(attributes.get("name") or filename or None)
        parent = p.get_item("folder", ref_id(attributes, "parent"), access.UPLOAD)
        if len(data) > ops.MAX_SIMPLE_UPLOAD:
            raise errors.box_error(
                413, "file_size_limit_exceeded", "File size exceeds the 50MB limit; use a chunked upload"
            )
        check_content_md5(request, data)
        record = ops.create_file(
            p, parent, name, data, now_iso(ctx),
            content_created_at=input_ts(attributes["content_created_at"], "content_created_at") if attributes.get("content_created_at") else None,
            content_modified_at=input_ts(attributes["content_modified_at"], "content_modified_at") if attributes.get("content_modified_at") else None,
            description=opt_str(attributes, "description", "")[:255],  # Box keeps the first 255 characters
        )  # fmt: skip
        return await finish(request, p, record, "ITEM_UPLOAD")

    @api.post("/files/{file_id}/content", prefix=UPLOAD_API)
    async def upload_version(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        record = p.get_item("file", file_id, access.UPLOAD)
        ops.check_if_match(request.headers, record)
        attributes, data, _filename = await read_multipart(request)
        ops.ensure_unlocked(ctx, record, p)
        if len(data) > ops.MAX_SIMPLE_UPLOAD:
            raise errors.box_error(
                413, "file_size_limit_exceeded", "File size exceeds the 50MB limit; use a chunked upload"
            )
        check_content_md5(request, data)
        name = validate_name(attributes["name"]) if attributes.get("name") else None
        ops.add_version(
            p, record, data, now_iso(ctx), name=name,
            content_modified_at=input_ts(attributes["content_modified_at"], "content_modified_at") if attributes.get("content_modified_at") else None,
        )  # fmt: skip
        return await finish(request, p, record, "ITEM_UPLOAD")

    # -- chunked uploads -----------------------------------------------------------------
    def session_or_404(session_id: str, p: access.Principal) -> Record:
        session = ctx.store.collection(db.UPLOADS).get(session_id)
        expires = try_parse_ts(session["expires_at"]) if session else None
        if session is None or session["created_by"] != p.uid or (expires and utc(expires) <= ctx.clock.now()):
            raise errors.not_found("Upload session not found or expired")
        return session

    def session_body(request: Request, session: Record) -> Record:
        base = f"/files/upload_sessions/{session['id']}"
        return {
            "id": session["id"],
            "type": "upload_session",
            "num_parts_processed": len(session["parts"]),
            "part_size": session["part_size"],
            "session_endpoints": {
                "abort": upload_url(request, base),
                "commit": upload_url(request, base + "/commit"),
                "list_parts": upload_url(request, base + "/parts"),
                "log_event": upload_url(request, base + "/log"),
                "status": upload_url(request, base),
                "upload_part": upload_url(request, base),
            },
            "session_expires_at": box_ts(session["expires_at"]),
            "total_parts": session["total_parts"],
        }

    def new_session(
        request: Request,
        p: access.Principal,
        *,
        folder_id: str | None,
        file_id: str | None,
        name: str,
        size: int,
    ) -> Record:
        if size < 1:
            raise errors.invalid_parameter("file_size", "'file_size' must be a positive integer")
        if size > MAX_FILE_SIZE:
            raise errors.box_error(413, "file_size_limit_exceeded", "File size exceeds the file size limit.")
        chunk = part_size(ctx)
        session = {
            "id": secrets.token_hex(16).upper(),
            "folder_id": folder_id,
            "file_id": file_id,
            "file_name": name,
            "file_size": size,
            "part_size": chunk,
            "total_parts": -(-size // chunk),
            "parts": {},
            "created_by": p.uid,
            "expires_at": to_iso(ctx.clock.now() + SESSION_TTL),
        }
        ctx.store.collection(db.UPLOADS).put(session)
        return session

    @api.post("/files/upload_sessions", prefix=UPLOAD_API, status_code=201)
    async def create_session(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        folder = (
            p.get_item("folder", str(body.get("folder_id") or ""), access.UPLOAD)
            if body.get("folder_id")
            else None
        )
        if folder is None:
            raise errors.missing_parameter("folder_id")
        name = validate_name(require_str(body, "file_name"))
        size = body.get("file_size")
        if not isinstance(size, int):
            raise errors.missing_parameter("file_size")
        ops.ensure_name_free(p, folder["id"], name, as_object=True)
        session = new_session(request, p, folder_id=folder["id"], file_id=None, name=name, size=size)
        return created(session_body(request, session))

    @api.post("/files/{file_id}/upload_sessions", prefix=UPLOAD_API, status_code=201)
    async def create_version_session(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        record = p.get_item("file", file_id, access.UPLOAD)
        size = body.get("file_size")
        if not isinstance(size, int):
            raise errors.missing_parameter("file_size")
        name = validate_name(body["file_name"]) if body.get("file_name") else record["name"]
        session = new_session(request, p, folder_id=None, file_id=record["id"], name=name, size=size)
        return created(session_body(request, session))

    @api.get("/files/upload_sessions/{session_id}", prefix=UPLOAD_API)
    async def get_session(request: Request, session_id: str) -> Any:
        p = api.principal(request)
        return session_body(request, session_or_404(session_id, p))

    @api.put("/files/upload_sessions/{session_id}", prefix=UPLOAD_API)
    async def upload_part(request: Request, session_id: str) -> Any:
        p = api.principal(request)
        session = session_or_404(session_id, p)
        data = await request.body()
        digest = parse_digest(request)
        if digest is None:
            raise errors.missing_parameter("digest")
        match = re.fullmatch(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*", request.headers.get("content-range", ""))
        if match is None:
            raise errors.invalid_parameter("content-range", "A valid Content-Range header is required")
        start, end = int(match.group(1)), int(match.group(2))
        if match.group(3) != "*" and int(match.group(3)) != session["file_size"]:
            raise errors.invalid_parameter(
                "content-range", "Content-Range total does not match the session file size"
            )
        if len(data) != end - start + 1:
            raise errors.invalid_parameter("content-range", "Content-Range does not match the body length")
        chunk = session["part_size"]
        last = start + len(data) == session["file_size"]
        if start % chunk != 0 or (len(data) != chunk and not last) or end >= session["file_size"]:
            raise errors.bad_request(
                "Part offset or size does not align with the session's part_size",
                reason="invalid_parameter",
                name="content-range",
            )
        try:
            if base64.b64decode(digest, validate=True) != bytes.fromhex(sha1_hex(data)):
                raise bad_digest()
        except (ValueError, binascii.Error):
            raise bad_digest() from None
        part = {
            "part_id": secrets.token_hex(4).upper(),
            "offset": start,
            "size": len(data),
            "sha1": sha1_hex(data),
        }
        session["parts"][str(start)] = {**part, "_blob": ctx.store.blobs.put(data)}
        return {"part": part}

    @api.get("/files/upload_sessions/{session_id}/parts", prefix=UPLOAD_API)
    async def list_parts(request: Request, session_id: str) -> Any:
        p = api.principal(request)
        session = session_or_404(session_id, p)
        parts = sorted(session["parts"].values(), key=lambda part: part["offset"])
        public = [{k: v for k, v in part.items() if not k.startswith("_")} for part in parts]
        offset = query_int(request, "offset", 0, maximum=10_000)
        limit = query_int(request, "limit", 1000, maximum=1000)
        return {
            "entries": public[offset : offset + limit],
            "limit": limit,
            "offset": offset,
            "total_count": len(public),
        }

    @api.delete("/files/upload_sessions/{session_id}", prefix=UPLOAD_API)
    async def abort_session(request: Request, session_id: str) -> Any:
        p = api.principal(request)
        session = session_or_404(session_id, p)
        ctx.store.collection(db.UPLOADS).delete(session["id"])
        return no_content()

    @api.post("/files/upload_sessions/{session_id}/commit", prefix=UPLOAD_API)
    async def commit_session(request: Request, session_id: str) -> Any:
        p = api.principal(request)
        session = session_or_404(session_id, p)
        body = await json_body(request, required=False)
        digest = parse_digest(request)
        if digest is None:
            raise errors.missing_parameter("digest")
        by_offset = session["parts"]
        wanted = body.get("parts")
        if isinstance(wanted, list) and wanted:
            ordered = []
            known = {part["part_id"]: part for part in by_offset.values()}
            for entry in wanted:
                part = known.get(str(entry.get("part_id"))) if isinstance(entry, dict) else None
                if part is None:
                    raise errors.bad_request(
                        "Unknown part_id in commit request", reason="invalid_parameter", name="parts"
                    )
                ordered.append(part)
        else:
            ordered = sorted(by_offset.values(), key=lambda part: part["offset"])
        if len(ordered) != session["total_parts"]:
            raise errors.bad_request(
                f"Expected {session['total_parts']} parts but received {len(ordered)}",
                reason="invalid_parameter",
                name="parts",
            )
        data = b"".join(
            ctx.store.blobs.get(part["_blob"]) for part in sorted(ordered, key=lambda part: part["offset"])
        )
        if len(data) != session["file_size"]:
            raise errors.bad_request(
                "Assembled size does not match the session's file_size",
                reason="invalid_parameter",
                name="file_size",
            )
        if digest != sha1_b64(data):
            raise bad_digest()
        now = now_iso(ctx)
        if session["file_id"]:
            record = p.get_item("file", session["file_id"], access.UPLOAD)
            ops.check_if_match(request.headers, record)
            ops.ensure_unlocked(ctx, record, p)
            ops.add_version(p, record, data, now, name=session["file_name"])
        else:
            parent = p.get_item("folder", session["folder_id"], access.UPLOAD)
            record = ops.create_file(p, parent, session["file_name"], data, now)
        ctx.store.collection(db.UPLOADS).delete(session["id"])
        return await finish(request, p, record, "ITEM_UPLOAD")

    @api.post("/files/upload_sessions/{session_id}/log", prefix=UPLOAD_API)
    async def log_session(request: Request, session_id: str) -> Any:
        p = api.principal(request)
        session_or_404(session_id, p)
        return JSONResponse({}, status_code=200)
