"""Content transfer between users, watermarks, folder locks and file representations."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.provider import ProviderContext
from emulators.providers.box import access, db, errors, events, ops, representations
from emulators.providers.box.access import Principal
from emulators.providers.box.routing import UPLOAD_API, Api, created, no_content
from emulators.providers.box.shapes import Shaper
from emulators.providers.box.util import (
    box_ts,
    json_body,
    next_id,
    now_iso,
    object_or_empty,
    parse_fields,
    query_int,
    ref_id,
)

Record = dict[str, Any]


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)
    locks = db.Live(ctx, db.FOLDER_LOCKS)

    # -- transfer a user's content ----------------------------------------------------------
    @api.put("/users/{user_id}/folders/0")
    async def transfer_content(request: Request, user_id: str) -> Any:
        """Offboarding: move everything a user owns into a folder in another user's account."""
        p = api.principal(request)
        p.require_privileged()
        body = await json_body(request)
        source = db.users(ctx).get(user_id)
        target = db.users(ctx).get(ref_id(body, "owned_by"))
        if source is None or target is None:
            raise errors.not_found()
        if source["id"] == target["id"] or target.get("_kind") == "external" or not db.is_active(target):
            raise errors.invalid_parameter("owned_by", "The new owner must be another active managed user")
        now = now_iso(ctx)
        owner = Principal(ctx, target, target)
        folder = ops.create_folder(owner, owner.root, f"{source['name']}'s Files and Folders", now)
        source_root = db.folders(ctx).get(source["_root"])
        moved = 0
        for child in list(owner.tree.all_children(source_root["id"])) if source_root else []:
            if child.get("_trash"):
                continue
            child["_parent"] = folder["id"]
            ops.reown(owner, child, target["id"])
            moved += 1
        owner.invalidate()
        events.append_event(
            ctx, "ITEM_MOVE", created_by=p.uid, source=events.item_source(owner, folder), item=folder,
            additional_details={"transferred_items": moved, "from_user": source["id"]},
        )  # fmt: skip
        return Shaper(owner, request).folder(folder, parse_fields(request))

    # -- watermarks ---------------------------------------------------------------------------
    def watermark_routes(kind: str) -> None:
        plural = f"{kind}s"

        def body_of(record: Record) -> Record:
            mark = record["watermark"]
            return {
                "watermark": {
                    "created_at": box_ts(mark["created_at"]),
                    "modified_at": box_ts(mark["modified_at"]),
                }
            }

        @api.get(f"/{plural}/{{item_id}}/watermark")
        async def get_watermark(request: Request, item_id: str) -> Any:
            p = api.principal(request)
            record = p.get_item(kind, item_id)
            if not record.get("watermark"):
                raise errors.not_found("Watermark not found")
            return body_of(record)

        @api.put(f"/{plural}/{{item_id}}/watermark")
        async def put_watermark(request: Request, item_id: str) -> Any:
            p = api.principal(request)
            record = p.get_item(kind, item_id, access.RENAME)
            body = await json_body(request)
            if object_or_empty(body.get("watermark"), "watermark").get("imprint") != "default":
                raise errors.invalid_parameter("watermark.imprint", "'imprint' must be 'default'")
            now = now_iso(ctx)
            existing = record.get("watermark")
            record["watermark"] = {
                "created_at": existing["created_at"] if existing else now,
                "modified_at": now,
            }
            if not existing:
                await events.emit(p, "WATERMARK_LABEL_CREATE", record)
            return body_of(record)

        @api.delete(f"/{plural}/{{item_id}}/watermark")
        async def delete_watermark(request: Request, item_id: str) -> Any:
            p = api.principal(request)
            record = p.get_item(kind, item_id, access.RENAME)
            if not record.get("watermark"):
                raise errors.not_found("Watermark not found")
            record["watermark"] = None
            await events.emit(p, "WATERMARK_LABEL_DELETE", record)
            return no_content()

    watermark_routes("file")
    watermark_routes("folder")

    # -- app item associations (the emulator has no app items, so the list is always empty) ----
    def app_item_routes(kind: str) -> None:
        @api.get(f"/{kind}s/{{item_id}}/app_item_associations")
        async def list_app_item_associations(request: Request, item_id: str) -> Any:
            p = api.principal(request)
            p.get_item(kind, item_id)
            limit = query_int(request, "limit", 100, minimum=1, maximum=1000)
            return {"limit": limit, "next_marker": None, "entries": []}

    app_item_routes("file")
    app_item_routes("folder")

    # -- folder locks ----------------------------------------------------------------------------
    def lock_body(shaper: Shaper, lock: Record) -> Record:
        folder = db.folders(ctx).get(lock["folder_id"])
        return {
            "id": lock["id"],
            "type": "folder_lock",
            "folder": shaper.folder_mini(folder) if folder else None,
            "created_by": shaper.user_mini(lock["created_by"]),
            "created_at": box_ts(lock["created_at"]),
            "locked_operations": dict(lock["locked_operations"]),
            "lock_type": "freeze",
        }

    @api.post("/folder_locks")
    async def create_folder_lock(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        folder = p.get_item("folder", ref_id(body, "folder"), access.INVITE)
        if db.is_root(folder) or p.role(folder) not in ("owner", "co-owner"):
            raise errors.forbidden()
        operations = body.get("locked_operations")
        if operations is None:
            operations = {"move": True, "delete": True}
        # Box: "if locked_operations is included, both move and delete must be included and both true".
        if not isinstance(operations, dict) or operations != {"move": True, "delete": True}:
            raise errors.invalid_parameter(
                "locked_operations", "'locked_operations' must set both 'move' and 'delete' to true"
            )
        if any(lock["folder_id"] == folder["id"] for lock in locks):
            raise errors.box_error(409, "conflict", "A lock already exists on this folder")
        lock = {
            "id": next_id(ctx, "folder_lock", locks, start=db.SMALL_ID_START * 5),
            "folder_id": folder["id"],
            "created_by": p.uid,
            "created_at": now_iso(ctx),
            "locked_operations": {"move": bool(operations.get("move", False)), "delete": bool(operations.get("delete", False))},
        }  # fmt: skip
        locks.put(lock)
        shaper = Shaper(p, request)
        await events.emit(
            p, "LOCK_CREATE", None, source=lock_body(shaper, lock), kind="folder_lock", webhook_item=folder
        )
        return created(lock_body(shaper, lock))

    @api.get("/folder_locks")
    async def list_folder_locks(request: Request) -> Any:
        p = api.principal(request)
        folder_id = request.query_params.get("folder_id")
        if not folder_id:
            raise errors.missing_parameter("folder_id")
        folder = p.get_item("folder", folder_id, access.PREVIEW)
        shaper = Shaper(p, request)
        rows = [lock_body(shaper, lock) for lock in locks if lock["folder_id"] == folder["id"]]
        return {"entries": rows, "limit": 100, "next_marker": None}

    @api.delete("/folder_locks/{lock_id}")
    async def delete_folder_lock(request: Request, lock_id: str) -> Any:
        p = api.principal(request)
        lock = locks.get(lock_id)
        folder = db.folders(ctx).get(lock["folder_id"]) if lock else None
        if lock is None or folder is None:
            raise errors.not_found()
        p.get_item("folder", folder["id"])
        if p.role(folder) not in ("owner", "co-owner"):
            raise errors.forbidden()
        locks.delete(lock["id"])
        await events.emit(
            p,
            "LOCK_DESTROY",
            None,
            source=lock_body(Shaper(p, request), lock),
            kind="folder_lock",
            webhook_item=folder,
        )
        return no_content()

    # -- representations -----------------------------------------------------------------------------
    def rep_file(p: Principal, file_id: str, version_id: str) -> Record:
        record = p.get_item("file", file_id, access.PREVIEW)
        version = db.versions(ctx).get(version_id)
        if version is None or version["_file"] != record["id"]:
            raise errors.not_found()
        return record

    @api.get("/internal_files/{file_id}/versions/{version_id}/representations/{name}")
    async def representation_info(request: Request, file_id: str, version_id: str, name: str) -> Any:
        p = api.principal(request)
        record = rep_file(p, file_id, version_id)
        kind = representations.kind_of(name)
        if kind not in representations.KINDS:
            raise errors.not_found()
        root = f"/2.0/internal_files/{file_id}/versions/{version_id}/representations/{name}"
        dims = name.split("_", 1)[1] if "_" in name and kind in representations.IMAGE_KINDS else None
        shaper = Shaper(p, request)
        return representations.entry(
            record,
            kind,
            {"dimensions": dims} if dims else {},
            shaper.host_url("api.box.com", root),
            shaper.host_url("public.boxcloud.com", f"/api{root}/content"),
        )

    @api.get(
        "/internal_files/{file_id}/versions/{version_id}/representations/{name}/content/{asset_path:path}",
        prefix=UPLOAD_API,
    )
    async def representation_content(
        request: Request, file_id: str, version_id: str, name: str, asset_path: str
    ) -> Any:
        p = api.principal(request)
        record = rep_file(p, file_id, version_id)
        kind = representations.kind_of(name)
        if kind not in representations.KINDS:
            raise errors.not_found()
        data, media_type = representations.render(ctx, record, name)
        return Response(data, media_type=media_type)

    # -- enterprise invitations --------------------------------------------------------------
    invites = db.Live(ctx, db.INVITES)

    def invite_body(shaper: Shaper, invite: Record) -> Record:
        enterprise = ctx.world.org
        return {
            "type": "invite",
            "id": invite["id"],
            "invited_to": {"type": "enterprise", "id": enterprise.box_enterprise_id, "name": enterprise.name},
            "actionable_by": shaper.user_mini(invite["user_id"]),
            "invited_by": shaper.user_mini(invite["invited_by"]),
            "status": invite["status"],
            "created_at": box_ts(invite["created_at"]),
            "modified_at": box_ts(invite["modified_at"]),
        }

    @api.post("/invites")
    async def create_invite(request: Request) -> Any:
        """Invite an existing Box account (or an e-mail) to join the enterprise; stays pending."""
        p = api.principal(request)
        p.require_privileged()
        body = await json_body(request)
        login = object_or_empty(body.get("actionable_by"), "actionable_by").get("login")
        if not login:
            raise errors.missing_parameter("actionable_by.login")
        if not isinstance(login, str) or "@" not in login:
            raise errors.invalid_parameter("actionable_by.login", "'login' must be an e-mail address")
        if str(ref_id(body, "enterprise")) != ctx.world.org.box_enterprise_id:
            raise errors.not_found("Enterprise not found")
        user = db.user_by_login(ctx, login)
        if user is not None and user.get("_kind") in ("managed", "service"):
            raise errors.box_error(
                409, "user_already_in_enterprise", "The user is already a member of this enterprise"
            )
        now = now_iso(ctx)
        if user is None:
            user = ops.create_external_user(ctx, login, now)
        invite = {
            "id": next_id(ctx, "invite", invites, start=db.SMALL_ID_START * 3),
            "user_id": user["id"],
            "invited_by": p.uid,
            "status": "pending",
            "created_at": now,
            "modified_at": now,
        }
        invites.put(invite)
        return created(invite_body(Shaper(p, request), invite))

    @api.get("/invites/{invite_id}")
    async def get_invite(request: Request, invite_id: str) -> Any:
        p = api.principal(request)
        p.require_privileged()
        invite = invites.get(invite_id)
        if invite is None:
            raise errors.not_found()
        return invite_body(Shaper(p, request), invite)
