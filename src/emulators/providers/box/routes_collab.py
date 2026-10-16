"""Collaborations (sharing with users and groups), invitations and ``/shared_items``."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request

from emulators.core.clock import utc
from emulators.core.provider import ProviderContext
from emulators.providers.box import access, db, errors, events, ops
from emulators.providers.box.access import Principal
from emulators.providers.box.routing import Api, created, no_content
from emulators.providers.box.util import (
    decode_marker,
    input_ts,
    json_body,
    next_id,
    now_iso,
    page_window,
    paginate,
    parse_fields,
    query_int,
    try_parse_ts,
)

Record = dict[str, Any]
FILE_ROLES = ("editor", "viewer", "previewer", "co-owner")
FOLDER_ROLES = access.ROLES


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)

    def can_see_collab(p: Principal, collab: Record, item: Record | None) -> bool:
        if collab["accessible_type"] == "user" and collab["accessible_id"] == p.uid:
            return True
        if collab["accessible_type"] == "group" and collab["accessible_id"] in p.groups:
            return True
        if collab.get("created_by") == p.uid:
            return True
        return bool(item and access.INVITE in p.caps(item))

    def collab_or_404(p: Principal, collab_id: str) -> tuple[Record, Record | None]:
        collab = db.collabs(ctx).get(collab_id)
        if collab is None:
            raise errors.not_found()
        item = db.item(ctx, collab["item_type"], collab["item_id"])
        if not can_see_collab(p, collab, item):
            raise errors.not_found()
        return collab, item

    async def emit_collab(
        request: Request, p: Principal, event_type: str, collab: Record, item: Record
    ) -> None:
        source = api.shaper(p, request).collaboration(collab)
        await events.emit(p, event_type, None, source=source, kind="collaboration", webhook_item=item)

    # -- create --------------------------------------------------------------------------
    @api.post("/collaborations")
    async def create_collaboration(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        target = body.get("item")
        if (
            not isinstance(target, dict)
            or target.get("type") not in ("file", "folder")
            or not target.get("id")
        ):
            raise errors.missing_parameter("item")
        by = body.get("accessible_by")
        if not isinstance(by, dict) or by.get("type") not in ("user", "group"):
            raise errors.missing_parameter("accessible_by")
        role = body.get("role")
        if not role:
            raise errors.missing_parameter("role")
        record = p.get_item(target["type"], str(target["id"]), access.INVITE)
        if db.is_root(record):
            raise errors.bad_request(
                "Cannot add collaborators to the root folder", reason="invalid_parameter", name="item"
            )
        allowed = FILE_ROLES if record["type"] == "file" else FOLDER_ROLES
        if role not in allowed:
            raise errors.invalid_parameter("role", f"Invalid role '{role}' for a {record['type']}")
        if role == "co-owner" and p.role(record) not in ("owner", "co-owner"):
            raise errors.forbidden()
        expires = None
        if body.get("expires_at"):
            expires = input_ts(body["expires_at"], "expires_at")
            if (try_parse_ts(expires) or utc(ctx.clock.now())) <= ctx.clock.now():
                raise errors.invalid_parameter("expires_at", "'expires_at' must be in the future")
        now = now_iso(ctx)
        invite_email: str | None = None
        status = "accepted"
        if by["type"] == "group":
            group = db.groups(ctx).get(str(by.get("id", "")))
            if group is None:
                raise errors.not_found("Group not found")
            if not p.is_privileged and group["invitability_level"] == "admins_only":
                raise errors.forbidden()
            accessible_id = group["id"]
        else:
            user = None
            if by.get("id"):
                user = db.users(ctx).get(str(by["id"]))
                if user is None:
                    raise errors.not_found("User not found")
            elif by.get("login"):
                user = db.user_by_login(ctx, by["login"])
                if user is None:
                    user = ops.create_external_user(ctx, by["login"], now)
                    status, invite_email = "pending", by["login"]
            else:
                raise errors.missing_parameter("accessible_by.id")
            if user.get("status") != "active":
                raise errors.bad_request(
                    "The user is not active", reason="invalid_parameter", name="accessible_by"
                )
            if user["id"] == record.get("_owner"):
                raise errors.bad_request(
                    "The owner of an item cannot be added as a collaborator",
                    reason="invalid_parameter",
                    name="accessible_by",
                )
            if user.get("_kind") == "external":
                owner = db.users(ctx).get(record["_owner"]) or {}
                if p.user.get("is_external_collab_restricted") or owner.get("is_external_collab_restricted"):
                    raise errors.forbidden("External collaboration is restricted")
                if status != "pending":
                    status, invite_email = "pending", user["login"]
            accessible_id = user["id"]
        if any(
            c["item_type"] == record["type"] and c["item_id"] == record["id"]
            and c["accessible_type"] == by["type"] and c["accessible_id"] == accessible_id
            for c in db.collabs(ctx)
        ):  # fmt: skip
            raise errors.box_error(
                400, "user_already_collaborator", "User is already a collaborator",
                context_info={"errors": [{"reason": "user_already_collaborator", "name": "accessible_by"}]},
            )  # fmt: skip
        cid = next_id(ctx, "collab", db.collabs(ctx), start=db.SMALL_ID_START * 100)
        collab = {
            "type": "collaboration", "id": cid, "item_type": record["type"], "item_id": record["id"],
            "accessible_type": by["type"], "accessible_id": accessible_id, "role": role, "status": status,
            "created_by": p.uid, "created_at": now, "modified_at": now, "expires_at": expires,
            "invite_email": invite_email, "acknowledged_at": now if status == "accepted" else None,
            "is_access_only": bool(body.get("is_access_only", False)),
        }  # fmt: skip
        db.collabs(ctx).put(collab)
        p.invalidate()
        await emit_collab(
            request,
            p,
            "COLLAB_ADD_COLLABORATOR" if status == "accepted" else "COLLAB_INVITE_COLLABORATOR",
            collab,
            record,
        )
        return created(api.shaper(p, request).collaboration(collab, parse_fields(request)))

    # -- read / update / delete -----------------------------------------------------------
    @api.get("/collaborations/{collab_id}")
    async def get_collaboration(request: Request, collab_id: str) -> Any:
        p = api.principal(request)
        collab, _item = collab_or_404(p, collab_id)
        return api.shaper(p, request).collaboration(collab, parse_fields(request))

    @api.put("/collaborations/{collab_id}")
    async def update_collaboration(request: Request, collab_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        collab, item = collab_or_404(p, collab_id)
        if item is None:
            raise errors.not_found()
        invitee = collab["accessible_type"] == "user" and collab["accessible_id"] == p.uid
        manager = access.INVITE in p.caps(item)
        event_type = "COLLAB_ROLE_CHANGE"
        if "status" in body:
            if body["status"] not in ("accepted", "rejected", "pending"):
                raise errors.invalid_parameter("status")
            if not invitee and not manager:
                raise errors.forbidden()
            if body["status"] == "rejected":
                db.collabs(ctx).delete(collab["id"])
                p.invalidate()
                await emit_collab(request, p, "COLLAB_REJECT", collab, item)
                return no_content()
            if collab["status"] == "pending" and body["status"] == "accepted":
                collab["status"] = "accepted"
                collab["acknowledged_at"] = now_iso(ctx)
                event_type = "COLLAB_ACCEPT"
        if "role" in body:
            if not manager:
                raise errors.forbidden()
            allowed = FILE_ROLES if item["type"] == "file" else FOLDER_ROLES
            if body["role"] == "owner":
                raise errors.bad_request(
                    "Ownership transfer is not supported by this emulator",
                    reason="invalid_parameter",
                    name="role",
                )
            if body["role"] not in allowed:
                raise errors.invalid_parameter("role", f"Invalid role '{body['role']}'")
            if body["role"] == "co-owner" and p.role(item) not in ("owner", "co-owner"):
                raise errors.forbidden()
            collab["role"] = body["role"]
        if "expires_at" in body:
            if not manager:
                raise errors.forbidden()
            collab["expires_at"] = input_ts(body["expires_at"], "expires_at") if body["expires_at"] else None
        if "can_view_path" in body:
            collab["can_view_path"] = bool(body["can_view_path"])
        collab["modified_at"] = now_iso(ctx)
        p.invalidate()
        await emit_collab(request, p, event_type, collab, item)
        return api.shaper(p, request).collaboration(collab, parse_fields(request))

    @api.delete("/collaborations/{collab_id}")
    async def delete_collaboration(request: Request, collab_id: str) -> Any:
        p = api.principal(request)
        collab, item = collab_or_404(p, collab_id)
        leaving = collab["accessible_type"] == "user" and collab["accessible_id"] == p.uid
        if item is None or not (leaving or access.INVITE in p.caps(item)):
            raise errors.forbidden()
        db.collabs(ctx).delete(collab["id"])
        p.invalidate()
        await emit_collab(request, p, "COLLAB_REMOVE_COLLABORATOR", collab, item)
        return no_content()

    @api.get("/collaborations")
    async def pending_collaborations(request: Request) -> Any:
        p = api.principal(request)
        status = request.query_params.get("status")
        if status is None:
            raise errors.missing_parameter("status")
        if status != "pending":
            raise errors.invalid_parameter("status", "'status' must be 'pending'")
        rows = [
            c
            for c in db.collabs(ctx)
            if c["status"] == "pending" and c["accessible_type"] == "user" and c["accessible_id"] == p.uid
        ]
        offset = query_int(request, "offset", 0)
        limit = query_int(request, "limit", 100, maximum=1000)
        shaper = api.shaper(p, request)
        fields = parse_fields(request)
        window = rows[offset : offset + limit]
        return {
            "total_count": len(rows),
            "entries": [shaper.collaboration(c, fields) for c in window],
            "limit": limit,
            "offset": offset,
        }

    # -- listings ------------------------------------------------------------------------
    def listing(p: Principal, request: Request, item: Record) -> dict[str, Any]:
        rows = [c for c in db.collabs(ctx) if c["item_type"] == item["type"] and c["item_id"] == item["id"]]
        offset = decode_marker(request.query_params.get("marker"))
        limit = query_int(request, "limit", 100, maximum=1000)
        body = paginate(rows, offset, limit, True)
        shaper = api.shaper(p, request)
        fields = parse_fields(request)
        body["entries"] = [shaper.collaboration(c, fields) for c in body["entries"]]
        body["total_count"] = len(rows)
        return body

    @api.get("/folders/{folder_id}/collaborations")
    async def folder_collaborations(request: Request, folder_id: str) -> Any:
        p = api.principal(request)
        item = p.get_item("folder", folder_id)
        if access.PREVIEW not in p.caps(item):
            raise errors.forbidden()
        return listing(p, request, item)

    @api.get("/files/{file_id}/collaborations")
    async def file_collaborations(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        item = p.get_item("file", file_id)
        if access.PREVIEW not in p.caps(item):
            raise errors.forbidden()
        return listing(p, request, item)

    @api.get("/groups/{group_id}/collaborations")
    async def group_collaborations(request: Request, group_id: str) -> Any:
        p = api.principal(request)
        group = db.groups(ctx).get(group_id)
        if group is None:
            raise errors.not_found()
        if not (p.is_privileged or group_id in p.groups):
            raise errors.forbidden()
        rows = [
            c for c in db.collabs(ctx) if c["accessible_type"] == "group" and c["accessible_id"] == group_id
        ]
        offset, limit, use_marker = page_window(request, default_limit=100, max_limit=1000)
        body = paginate(rows, offset, limit, use_marker)
        shaper = api.shaper(p, request)
        fields = parse_fields(request)
        body["entries"] = [shaper.collaboration(c, fields) for c in body["entries"]]
        return body

    # -- shared items --------------------------------------------------------------------
    @api.get("/shared_items")
    async def shared_items(request: Request) -> Any:
        p = api.principal(request, allow_anonymous=True)
        parsed = access.parse_boxapi(request)
        if parsed is None:
            raise errors.bad_request(
                "Missing BoxApi header with shared_link", reason="missing_parameter", name="boxapi"
            )
        record = access.find_shared_link(ctx, parsed[0])
        if record is None or not access.link_is_live(ctx, record):
            raise errors.not_found()
        if not p.links and not p.visible(record):
            raise errors.not_found()
        shaper = api.shaper(p, request)
        fields = parse_fields(request)
        if record["type"] == "folder":
            return shaper.folder(record, fields)
        return shaper.file(record, fields)
