"""files.* Web API methods, the external upload flow and file downloads."""

from __future__ import annotations

import os
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from emulators.core.auth import bearer_token
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.slack import access, events, messages
from emulators.providers.slack.records import new_id
from emulators.providers.slack.runtime import Call, SlackError, api
from emulators.providers.slack.shapes import FILETYPES, IMAGE_TYPES, file_json, file_visible
from emulators.providers.slack.util import WORKSPACE_URL
from emulators.world.documents import EXTENSION_TO_MIME

MAX_FILE_BYTES = 1_000_000_000
TYPE_FILTERS = {
    "images": lambda f: f["filetype"] in IMAGE_TYPES,
    "pdfs": lambda f: f["filetype"] == "pdf",
    "zips": lambda f: f["filetype"] == "zip",
    "spaces": lambda f: f["filetype"] in ("docx", "xlsx", "pptx", "doc", "xls", "ppt"),
    "snippets": lambda f: f["filetype"] in ("text", "markdown", "json", "csv"),
    "gdocs": lambda f: False,
    "email": lambda f: False,
}


def _extension(name: str) -> str:
    return os.path.splitext(name)[1].lstrip(".").lower()


def new_file_record(
    ctx: ProviderContext,
    *,
    name: str,
    title: str,
    data: bytes,
    uploader: str,
    mimetype: str | None = None,
    filetype: str | None = None,
    file_id: str | None = None,
) -> dict[str, Any]:
    ext = _extension(name)
    kind, pretty = FILETYPES.get(
        filetype or ext, (filetype or ext or "binary", (filetype or ext or "Binary").upper())
    )
    now = int(ctx.clock.now().timestamp())
    record = {
        "id": file_id or new_id(ctx, "F", access.FILES, 10),
        "created": now,
        "timestamp": now,
        "name": name,
        "title": title,
        "mimetype": mimetype or EXTENSION_TO_MIME.get(ext, "application/octet-stream"),
        "filetype": kind,
        "pretty_type": pretty,
        "user": uploader,
        "user_team": access.team_id(ctx),
        "editable": False,
        "size": len(data),
        "mode": "hosted",
        "is_external": False,
        "external_type": "",
        "is_public": False,
        "public_url_shared": False,
        "display_as_bot": False,
        "username": "",
        "has_rich_preview": False,
        "file_access": "visible",
        "_blob": ctx.store.blobs.put(data),
        "_shares": [],
    }
    return access.col(ctx, access.FILES).put(record)


def _target_channels(call: Call, refs: list[str]) -> list[dict[str, Any]]:
    result = []
    for ref in refs:
        channel = access.require_channel(call.ctx, call.me, ref)
        if channel.get("is_archived"):
            raise SlackError("is_archived")
        if not access.is_member(channel, call.me.uid):
            raise SlackError("not_in_channel")
        result.append(channel)
    return result


async def share_files(
    call: Call,
    files: list[dict[str, Any]],
    channels: list[dict[str, Any]],
    *,
    comment: str | None,
    thread_ts: str | None,
    blocks: Any = None,
) -> None:
    """Post one ``file_share`` message per target conversation and fire ``file_shared``."""
    ctx = call.ctx
    author: dict[str, Any] = {"user": call.me.uid}
    if call.me.is_bot:
        author.update(bot_id=call.me.bot_id, app_id=call.me.app_id)
    for channel in channels:
        await messages.create_message(
            ctx,
            channel,
            text=comment or "",
            subtype="file_share",
            blocks=blocks,
            thread_ts=thread_ts,
            files=[f["id"] for f in files],
            extra={"upload": True, "display_as_bot": False},
            **author,
        )
        for record in files:
            await events.emit_file_shared(ctx, channel, record["id"], call.me.uid)


def _public(call: Call, record: dict[str, Any]) -> dict[str, Any]:
    return file_json(call.ctx, call.request, record)


@api("files.upload", scopes=("files:write", "files:write:user"))
async def files_upload(call: Call) -> dict[str, Any]:
    p = call.params
    if (call.ctx.setting("LEGACY_FILES_UPLOAD", "true") or "true").lower() in ("0", "false", "no", "off"):
        raise SlackError("method_deprecated")
    upload = p.files.get("file")
    content = p.str("content")
    if upload is None and content is None:
        raise SlackError("no_file_data")
    channels = _target_channels(call, p.csv("channels"))
    if upload is not None:
        data, name, mimetype = upload.data, p.str("filename") or upload.name, upload.content_type
    else:
        data, name, mimetype = (content or "").encode(), p.str("filename") or "untitled", None
        if not _extension(name) and not p.str("filetype"):
            mimetype = "text/plain"
    record = new_file_record(
        call.ctx,
        name=name,
        title=p.str("title") or name,
        data=data,
        uploader=call.me.uid,
        mimetype=None if mimetype in (None, "application/octet-stream") else mimetype,
        filetype=p.str("filetype") if p.str("filetype") not in (None, "auto") else None,
    )
    await share_files(
        call, [record], channels, comment=p.str("initial_comment"), thread_ts=p.str("thread_ts")
    )
    return {"file": _public(call, record)}


@api("files.getUploadURLExternal", scopes=("files:write",))
async def get_upload_url(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    name = p.need("filename")
    length = p.int("length")
    if length is None or length < 0:
        raise SlackError("invalid_arguments")
    if length > MAX_FILE_BYTES:
        raise SlackError("file_too_large")
    token = "CwABAAAA" + ctx.ids.urlsafe(96)
    file_id = new_id(ctx, "F", access.FILES, 10)
    access.col(ctx, access.UPLOADS).put(
        {
            "id": token,
            "file_id": file_id,
            "filename": name,
            "length": length,
            "user": call.me.uid,
            "received": False,
            "_blob": None,
            "_content_type": None,
        }
    )
    return {
        "upload_url": url_for_host(call.request, ctx, "files.slack.com", f"/upload/v1/{token}"),
        "file_id": file_id,
    }


async def receive_upload(ctx: ProviderContext, token: str, request: Request) -> Response:
    """``POST files.slack.com/upload/v1/<token>``: the raw bytes (or one multipart part) of a file."""
    pending = access.col(ctx, access.UPLOADS).get(token)
    if pending is None:
        return Response("Not found", status_code=404, media_type="text/plain")
    content_type = request.headers.get("content-type", "application/octet-stream")
    if content_type.lower().startswith("multipart/"):
        form = await request.form()
        parts = [v for _, v in form.multi_items() if hasattr(v, "read")]
        if not parts:
            return Response("Bad request", status_code=400, media_type="text/plain")
        data = await parts[0].read()
        content_type = parts[0].content_type or "application/octet-stream"
    else:
        data = await request.body()
    pending["_blob"] = ctx.store.blobs.put(data)
    pending["_content_type"] = content_type
    pending["received"] = True
    pending["received_length"] = len(data)
    return Response(f"OK - {len(data)}", media_type="text/plain")


@api("files.completeUploadExternal", scopes=("files:write",))
async def complete_upload(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    requested = p.obj("files", "invalid_arguments")
    if not isinstance(requested, list) or not requested:
        raise SlackError("invalid_arguments")
    refs = p.csv("channels") or ([p.str("channel_id")] if p.str("channel_id") else [])
    channels = _target_channels(call, refs)
    records: list[dict[str, Any]] = []
    for item in requested:
        if not isinstance(item, dict) or not item.get("id"):
            raise SlackError("invalid_arguments")
        pending = next((u for u in access.col(ctx, access.UPLOADS) if u["file_id"] == item["id"]), None)
        if pending is None or pending["user"] != call.me.uid or not pending["received"]:
            raise SlackError("file_not_found")
        data = ctx.store.blobs.get(pending["_blob"])
        record = new_file_record(
            ctx,
            name=pending["filename"],
            title=str(item.get("title") or pending["filename"]),
            data=data,
            uploader=call.me.uid,
            mimetype=pending["_content_type"]
            if pending["_content_type"] != "application/octet-stream"
            else None,
            file_id=pending["file_id"],
        )
        access.col(ctx, access.UPLOADS).delete(pending["id"])
        records.append(record)
    if channels:
        await share_files(
            call,
            records,
            channels,
            comment=p.str("initial_comment"),
            thread_ts=p.str("thread_ts"),
            blocks=p.obj("blocks", "invalid_blocks_format"),
        )
    return {"files": [_public(call, r) for r in records]}


def _visible_file(call: Call, ref: str | None) -> dict[str, Any]:
    record = access.col(call.ctx, access.FILES).get(ref) if ref else None
    if record is None:
        raise SlackError("file_not_found")
    if record.get("_deleted"):
        raise SlackError("file_deleted")
    if not file_visible(call.ctx, call.me, record):
        raise SlackError("file_not_found")
    return record


@api("files.info", scopes=("files:read",))
async def files_info(call: Call) -> dict[str, Any]:
    record = _visible_file(call, call.params.str("file"))
    return {"file": _public(call, record), "comments": [], "response_metadata": {"next_cursor": ""}}


@api("files.list", scopes=("files:read",))
async def files_list(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    channel_id = p.str("channel")
    user = p.str("user")
    ts_from, ts_to = p.int("ts_from", 0), p.int("ts_to")
    types = p.csv("types") or ["all"]
    found: list[dict[str, Any]] = []
    for record in access.col(ctx, access.FILES):
        if not file_visible(ctx, call.me, record):
            continue
        if user and record["user"] != user:
            continue
        if channel_id and not any(s["channel"] == channel_id for s in record.get("_shares", [])):
            continue
        if ts_from is not None and record["created"] < ts_from:
            continue
        if ts_to is not None and record["created"] > ts_to:
            continue
        if "all" not in types and not any(TYPE_FILTERS.get(t, lambda f: False)(record) for t in types):
            continue
        found.append(record)
    order = {f["id"]: position for position, f in enumerate(found)}
    found.sort(key=lambda f: (f["created"], order[f["id"]]), reverse=True)
    count = p.int("count", 100, lo=1, hi=1000) or 100
    page = p.int("page", 1, lo=1) or 1
    total = len(found)
    window = found[(page - 1) * count : page * count]
    return {
        "files": [_public(call, r) for r in window],
        "paging": {"count": count, "total": total, "page": page, "pages": max(1, -(-total // count))},
    }


@api("files.delete", scopes=("files:write",))
async def files_delete(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    record = _visible_file(call, call.params.str("file"))
    if record["user"] != call.me.uid and not (
        not call.me.is_bot and access.is_admin(access.actor_user(ctx, call.me))
    ):
        raise SlackError("cant_delete_file")
    record["_deleted"] = True
    return {}


@api("files.sharedPublicURL", scopes=("files:write",), kinds=("user",))
async def files_shared_public_url(call: Call) -> dict[str, Any]:
    record = _visible_file(call, call.params.str("file"))
    if record["user"] != call.me.uid:
        raise SlackError("not_allowed")
    record["public_url_shared"] = True
    record["is_public"] = True
    return {"file": _public(call, record)}


@api("files.revokePublicURL", scopes=("files:write",), kinds=("user",))
async def files_revoke_public_url(call: Call) -> dict[str, Any]:
    record = _visible_file(call, call.params.str("file"))
    if record["user"] != call.me.uid:
        raise SlackError("not_allowed")
    record["public_url_shared"] = False
    return {"file": _public(call, record)}


# -- downloads ---------------------------------------------------------------------------------


def serve_file(ctx: ProviderContext, request: Request, team_file: str, download: bool) -> Response:
    """``files.slack.com/files-pri/T..-F../name``: bytes for callers holding a token with ``files:read``."""
    from emulators.core.clock import utc

    token = bearer_token(request)
    grant = ctx.tokens.get(token) if token else None
    expired = bool(grant and grant.expires_at and utc(grant.expires_at) <= ctx.clock.now())
    if grant is None or grant.revoked or expired or grant.kind != "access":
        target = f"{WORKSPACE_URL}/?redir=" + request.url.path
        return RedirectResponse(target, status_code=302)
    team, _, file_id = team_file.partition("-")
    record = access.col(ctx, access.FILES).get(file_id.split("-")[0])
    if record is None or team != access.team_id(ctx) or record.get("_deleted"):
        return Response("File not found", status_code=404, media_type="text/plain")
    if "files:read" not in grant.scopes and "files:read:user" not in grant.scopes:
        return Response("missing_scope", status_code=403, media_type="text/plain")
    from emulators.providers.slack.runtime import Actor

    extra = grant.extra or {}
    actor = Actor(
        token=token or "",
        kind=extra.get("kind", "user"),
        uid=extra.get("slack_user", ""),
        scopes=tuple(grant.scopes),
        grant=grant,
    )
    if not file_visible(ctx, actor, record):
        return Response("File not found", status_code=404, media_type="text/plain")
    data = ctx.store.blobs.get(record["_blob"])
    headers = {"content-length": str(len(data))}
    if download:
        headers["content-disposition"] = f'attachment; filename="{record["name"]}"'
    return Response(data, media_type=record["mimetype"], headers=headers)
