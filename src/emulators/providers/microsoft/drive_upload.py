"""Simple uploads and preauthenticated, sequential Graph upload sessions."""

from __future__ import annotations

import mimetypes
import re
import secrets
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.microsoft import common
from emulators.providers.microsoft import drive_model as dm

SESSIONS = "ms_drive_upload_sessions"


def conflict(
    ctx: ProviderContext, parent: dict[str, Any], name: str, behavior: str
) -> tuple[str, dict[str, Any] | None]:
    if behavior not in ("fail", "replace", "rename"):
        raise common.graph_error(400, "invalidRequest", "Invalid conflict behavior.")
    old = dm.child_named(ctx, parent, name)
    if old and behavior == "fail":
        raise common.graph_error(409, "nameAlreadyExists", "An item with the same name already exists.")
    if old and behavior == "rename":
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        number = 1
        while dm.child_named(ctx, parent, f"{stem} ({number})" + (f".{ext}" if ext else "")):
            number += 1
        return f"{stem} ({number})" + (f".{ext}" if ext else ""), None
    if old and "folder" in old:
        raise common.graph_error(409, "nameAlreadyExists", "A folder with this name already exists.")
    return name, old


async def put_content(
    ctx: ProviderContext,
    drive: dict[str, Any],
    parent: dict[str, Any],
    name: str,
    data: bytes,
    p: common.Principal,
    *,
    behavior: str = "replace",
    existing: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    if existing is None:
        dm.require_item(ctx, drive["id"], parent["id"], p, write=True)
    if "folder" not in parent:
        raise common.graph_error(400, "invalidRequest", "The parent must be a folder.")
    name = dm.validate_name(name)
    if existing is None:
        name, existing = conflict(ctx, parent, name, behavior)
    if existing:
        dm.require_item(ctx, drive["id"], existing["id"], p, write=True)
        if "folder" in existing:
            raise common.graph_error(400, "invalidRequest", "Cannot replace a folder's content.")
        dm.content(ctx, existing)
        ctx.store["ms_drive_versions"].put(
            {
                "id": f"{existing['id']}:{existing['_version']}.0",
                "item": existing["id"],
                "version": f"{existing['_version']}.0",
                "blob": existing["_blob"],
                "size": existing["size"],
                "lastModifiedDateTime": existing["lastModifiedDateTime"],
                "lastModifiedBy": existing["lastModifiedBy"],
            }
        )
    item = existing or dm.new_item(
        ctx,
        drive,
        parent,
        name,
        key=ctx.ids.guid(),
        owner=p.person_id or drive["_owner"],
        created=isoformat_z(ctx.clock.now()),
    )
    item["_blob"] = ctx.store.blobs.put(data)
    item["_uploaded"] = True
    item.pop("_pdf_blob", None)
    item["file"] = {"mimeType": mimetypes.guess_type(name)[0] or "application/octet-stream", "hashes": {}}
    item["lastModifiedBy"] = common.identity(ctx.world, ctx.world.person(p.person_id or drive["_owner"]))
    dm.content(ctx, item)
    await dm.changed(ctx, item, "updated" if existing else "created", content_changed=True)
    return item, 200 if existing else 201


def precondition(request: Request, item: dict[str, Any]) -> None:
    tags = (item["eTag"], item["cTag"])
    if request.headers.get("if-match") not in (None, "*", *tags):
        raise common.graph_error(412, "preconditionFailed", "The item has changed.")
    if request.headers.get("if-none-match") in ("*", *tags):
        raise common.graph_error(412, "preconditionFailed", "The item already exists or has not changed.")


async def create_session(
    request: Request,
    ctx: ProviderContext,
    drive: dict[str, Any],
    parent: dict[str, Any],
    name: str,
    p: common.Principal,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = await dm.json_body(request) if await request.body() else {}
    if "deferCommit" in payload and not isinstance(payload["deferCommit"], bool):
        raise common.graph_error(400, "invalidRequest", "deferCommit must be a Boolean.")
    if payload.get("deferCommit"):
        raise common.graph_error(400, "notSupported", "Deferred commit is not supported.")
    options = payload.get("item", {})
    if not isinstance(options, dict):
        raise common.graph_error(400, "invalidRequest", "item must be an object.")
    requested_name = dm.validate_name(options.get("name", name))
    if requested_name != name:
        raise common.graph_error(400, "invalidRequest", "item.name must match the name in the request URL.")
    behavior = options.get("@microsoft.graph.conflictBehavior", "fail")
    if behavior not in ("fail", "replace", "rename"):
        raise common.graph_error(400, "invalidRequest", "Invalid conflict behavior.")
    if existing:
        precondition(request, existing)
        dm.require_item(ctx, drive["id"], existing["id"], p, write=True)
    else:
        dm.require_item(ctx, drive["id"], parent["id"], p, write=True)
        old = dm.child_named(ctx, parent, name)
        if old:
            precondition(request, old)
        conflict(ctx, parent, name, behavior)
    sid = secrets.token_urlsafe(32)
    expiry = isoformat_z(ctx.clock.now() + timedelta(hours=1))
    ctx.store[SESSIONS].put(
        {
            "id": sid,
            "drive": drive["id"],
            "parent": parent["id"],
            "name": name,
            "behavior": behavior,
            "existing": existing["id"] if existing else None,
            "user": p.person_id,
            "client": p.client_id,
            "app": p.app_only,
            "expires": expiry,
            "received": 0,
            "chunks": [],
            "total": None,
        }
    )
    return {
        "uploadUrl": url_for_host(request, ctx, dm.HOST, f"/_api/v2.0/upload/{sid}"),
        "expirationDateTime": expiry,
        "nextExpectedRanges": ["0-"],
    }


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.api_route("/_api/v2.0/upload/{sid}", methods=["GET", "PUT", "DELETE"])
    async def upload(request: Request, sid: str) -> Any:
        session = ctx.store[SESSIONS].get(sid)
        if session is None or parse_instant(session["expires"]) <= ctx.clock.now():
            raise common.item_not_found()
        if request.method == "DELETE":
            ctx.store[SESSIONS].delete(sid)
            return Response(status_code=204)
        if request.method == "GET":
            return {
                "expirationDateTime": session["expires"],
                "nextExpectedRanges": [f"{session['received']}-"],
            }
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", request.headers.get("content-range", ""))
        if not match or any(len(v) > 20 for v in match.groups()):
            raise common.graph_error(400, "invalidRequest", "A valid Content-Range header is required.")
        start, end, total = map(int, match.groups())
        data = await request.body()
        if start != session["received"]:
            raise common.graph_error(
                416, "invalidRange", "The byte range has already been received or is out of order."
            )
        if (
            end < start
            or end >= total
            or len(data) != end - start + 1
            or (session["total"] is not None and total != session["total"])
            or len(data) >= 60 * 1024 * 1024
            or (end + 1 != total and len(data) % 327680)
        ):
            raise common.graph_error(400, "invalidRequest", "Invalid upload fragment size or total length.")
        session["total"] = total
        session["chunks"].append(ctx.store.blobs.put(data))
        session["received"] = end + 1
        session["expires"] = isoformat_z(ctx.clock.now() + timedelta(hours=1))
        if end + 1 < total:
            return JSONResponse(
                {"expirationDateTime": session["expires"], "nextExpectedRanges": [f"{end + 1}-"]},
                status_code=202,
            )
        p = common.Principal(
            common.user_id(ctx.world, session["user"]) if session["user"] else None,
            session["user"],
            session["app"],
            session["client"],
        )
        drive = ctx.store[dm.DRIVES].get(session["drive"])
        parent = ctx.store[dm.ITEMS].get(session["parent"])
        if parent is None or parent.get("_deleted"):
            raise common.item_not_found()
        existing = (
            dm.require_item(ctx, drive["id"], session["existing"], p, write=True)
            if session["existing"]
            else None
        )
        item, status = await put_content(
            ctx,
            drive,
            parent,
            session["name"],
            b"".join(ctx.store.blobs.get(c) for c in session["chunks"]),
            p,
            behavior=session["behavior"],
            existing=existing,
        )
        ctx.store[SESSIONS].delete(sid)
        return JSONResponse(dm.wire_item(request, ctx, item), status_code=status)
