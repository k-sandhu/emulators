"""Permissions and shared drives expose the same ACLs used by file access checks."""

from __future__ import annotations

import copy
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.google.common import Principal, google_error, json_object
from emulators.providers.google.drive_access import (
    RANK,
    actor_email,
    all_permissions,
    get_file,
    principal,
    role_for,
)
from emulators.providers.google.drive_changes import record_change, visibility
from emulators.providers.google.drive_fields import select
from emulators.providers.google.drive_query import compile_query
from emulators.providers.google.drive_seed import FOLDER, new_file, permission


def admin_access(ctx: ProviderContext, request: Request, actor: Principal) -> bool:
    return request.query_params.get("useDomainAdminAccess") == "true" and any(
        r.get("_person") == actor.person_id and r.get("isAdmin")
        for r in ctx.store.collection("directory_users")
    )


def drive_shape(ctx: ProviderContext, row: dict[str, Any], actor: Principal) -> dict[str, Any]:
    root = ctx.store.collection("drive_files").get(row["id"])
    role = role_for(ctx, root, actor) if root else None
    edit = RANK.get(role or "", 0) >= 3
    return {
        **{k: copy.deepcopy(v) for k, v in row.items() if not k.startswith("_")},
        "hidden": actor.person_id in row.get("_hidden", []),
        "capabilities": {
            "canAddChildren": edit,
            "canComment": RANK.get(role or "", 0) >= 2,
            "canCopy": True,
            "canDeleteChildren": role in ("organizer", "fileOrganizer"),
            "canDeleteDrive": role == "organizer",
            "canDownload": True,
            "canEdit": edit,
            "canListChildren": True,
            "canManageMembers": role == "organizer",
            "canReadRevisions": True,
            "canRename": role == "organizer",
            "canRenameDrive": role == "organizer",
            "canShare": role == "organizer",
            "canTrashChildren": edit,
        },
    }


def list_page(
    ctx: ProviderContext,
    request: Request,
    rows: list[dict[str, Any]],
    key: str,
    kind: str,
    *,
    default: int = 100,
) -> dict[str, Any]:
    q = request.query_params
    try:
        size = min(int(q.get("pageSize", str(default))), 1000 if key == "files" else 100)
        if size < 1:
            raise ValueError
    except ValueError:
        raise google_error(400, "Invalid pageSize", reason="invalid", location="pageSize") from None
    signature = stable_hash(
        *(f"{k}={v}" for k, v in sorted(q.items()) if k not in ("pageToken", "fields", "pageSize")),
        request.url.path,
        length=24,
    )
    offset = 0
    if q.get("pageToken"):
        record = ctx.store.collection("drive_page_tokens").get(q["pageToken"])
        if not record or record["signature"] != signature:
            raise google_error(400, "Invalid Value", reason="invalid", location="pageToken")
        offset = record["offset"]
    result = {"kind": kind, key: rows[offset : offset + size]}
    if key == "files":
        result["incompleteSearch"] = False
    if offset + size < len(rows):
        token = ctx.ids.token("~", 32)
        ctx.store.collection("drive_page_tokens").put_as(
            token, {"offset": offset + size, "signature": signature}
        )
        result["nextPageToken"] = token
    return result


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/drive/v3/files/{fid}/permissions")
    async def permissions(fid: str, request: Request) -> dict[str, Any]:
        row = get_file(ctx, principal(request, ctx), fid)
        result = list_page(ctx, request, all_permissions(ctx, row), "permissions", "drive#permissionList")
        return select(
            result, request.query_params.get("fields"), "kind,nextPageToken,permissions(id,type,role)"
        )

    @app.get("/drive/v3/files/{fid}/permissions/{pid}")
    async def get_permission(fid: str, pid: str, request: Request) -> dict[str, Any]:
        row = get_file(ctx, principal(request, ctx), fid)
        perm = next((p for p in all_permissions(ctx, row) if p["id"] == pid), None)
        if not perm:
            raise google_error(404, "Permission not found: " + pid, reason="notFound")
        return select(perm, request.query_params.get("fields"))

    def can_share(row: dict[str, Any], actor: Principal) -> None:
        role = role_for(ctx, row, actor)
        if RANK.get(role or "", 0) < 3 or (not row.get("writersCanShare", True) and role != "owner"):
            raise google_error(
                403,
                "The user does not have sufficient permissions for this file.",
                reason="insufficientFilePermissions",
            )

    @app.post("/drive/v3/files/{fid}/permissions")
    async def create_permission(fid: str, request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        row = get_file(ctx, actor, fid, write=True)
        can_share(row, actor)
        data = await json_object(request)
        kind, role = data.get("type"), data.get("role")
        if kind not in ("user", "group", "domain", "anyone") or role not in RANK:
            raise google_error(400, "Invalid permission type or role", reason="invalid")
        if (kind in ("user", "group") and not data.get("emailAddress")) or (
            kind == "domain" and not data.get("domain")
        ):
            raise google_error(400, "Missing emailAddress or domain", reason="required")
        if role == "owner":
            if request.query_params.get("transferOwnership") != "true":
                raise google_error(
                    403,
                    "The transferOwnership parameter must be enabled when the permission role is 'owner'.",
                    reason="ownershipChangeAcrossDomainsNotPermitted",
                )
            if row.get("driveId") or role_for(ctx, row, actor) != "owner" or kind != "user":
                raise google_error(403, "Ownership transfer is not permitted.", reason="forbidden")
        before = visibility(ctx, row)
        perm = permission(
            ctx,
            None,
            role,
            email=data.get("emailAddress"),
            link="anyone" if kind == "anyone" else "organization" if kind == "domain" else "none",
        )
        perm["type"] = kind
        if kind == "domain":
            perm.update(id=data["domain"], domain=data["domain"])
        for key in ("allowFileDiscovery", "expirationTime", "pendingOwner"):
            if key in data:
                perm[key] = data[key]
        if role == "owner":
            for previous in row["_permissions"]:
                if previous["role"] == "owner":
                    previous["role"] = "writer"
            owner = ctx.world.person_by_email(data["emailAddress"])
            row["_owner"] = owner.id if owner else None
        row["_permissions"] = [p for p in row["_permissions"] if p["id"] != perm["id"]]
        row["_permissions"].append(perm)
        await record_change(ctx, row, before=before, changed="permissions")
        return select(perm, request.query_params.get("fields"), "id,type,role,kind")

    @app.patch("/drive/v3/files/{fid}/permissions/{pid}")
    async def update_permission(fid: str, pid: str, request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        row = get_file(ctx, actor, fid, write=True)
        can_share(row, actor)
        perm = next((p for p in row["_permissions"] if p["id"] == pid), None)
        if not perm:
            if any(p["id"] == pid for p in all_permissions(ctx, row)):
                raise google_error(
                    403,
                    "Cannot update or delete an inherited permission on a shared drive item.",
                    reason="cannotModifyInheritedTeamDrivePermission",
                )
            raise google_error(404, "Permission not found: " + pid, reason="notFound")
        data = await json_object(request)
        if "role" in data and data["role"] not in RANK:
            raise google_error(400, "Invalid role", reason="invalid")
        if data.get("role") == "owner" and (
            request.query_params.get("transferOwnership") != "true"
            or row.get("driveId")
            or role_for(ctx, row, actor) != "owner"
        ):
            raise google_error(
                403,
                "Ownership transfer requires transferOwnership=true and the current owner.",
                reason="forbidden",
            )
        before = visibility(ctx, row)
        if data.get("role") == "owner":
            for previous in row["_permissions"]:
                if previous["role"] == "owner":
                    previous["role"] = "writer"
            owner = ctx.world.person_by_email(perm.get("emailAddress", ""))
            row["_owner"] = owner.id if owner else None
        perm.update(
            {
                k: data[k]
                for k in ("role", "allowFileDiscovery", "expirationTime", "pendingOwner")
                if k in data
            }
        )
        if request.query_params.get("removeExpiration") == "true":
            perm.pop("expirationTime", None)
        await record_change(ctx, row, before=before, changed="permissions")
        return select(perm, request.query_params.get("fields"))

    @app.delete("/drive/v3/files/{fid}/permissions/{pid}")
    async def delete_permission(fid: str, pid: str, request: Request) -> Response:
        actor = principal(request, ctx)
        row = get_file(ctx, actor, fid, write=True)
        can_share(row, actor)
        perm = next((p for p in row["_permissions"] if p["id"] == pid), None)
        if not perm:
            if any(p["id"] == pid for p in all_permissions(ctx, row)):
                raise google_error(
                    403,
                    "Cannot update or delete an inherited permission on a shared drive item.",
                    reason="cannotModifyInheritedTeamDrivePermission",
                )
            raise google_error(404, "Permission not found: " + pid, reason="notFound")
        if perm["role"] == "owner":
            raise google_error(403, "Cannot remove the owner.", reason="cannotDeleteOwner")
        before = visibility(ctx, row)
        row["_permissions"].remove(perm)
        await record_change(ctx, row, before=before, changed="permissions")
        return Response(status_code=204)

    @app.get("/drive/v3/drives")
    async def drives(request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        rows = [
            drive_shape(ctx, d, actor)
            for d in ctx.store.collection("drive_drives")
            if admin_access(ctx, request, actor)
            or role_for(ctx, ctx.store.collection("drive_files").get(d["id"]), actor)
        ]
        if request.query_params.get("q"):
            predicate = compile_query(re.sub(r"\bhidden\b", "starred", request.query_params["q"]))
            rows = [r for r in rows if predicate({**r, "starred": r["hidden"]})]
        return select(
            list_page(ctx, request, rows, "drives", "drive#driveList", default=10),
            request.query_params.get("fields"),
            "kind,nextPageToken,drives(id,name,kind)",
        )

    def drive_row(did: str, request: Request, actor: Principal, *, write: bool = False) -> dict[str, Any]:
        row = ctx.store.collection("drive_drives").get(did)
        if row is None:
            raise google_error(404, "Shared drive not found: " + did, reason="notFound")
        if not admin_access(ctx, request, actor):
            get_file(ctx, actor, did, owner=write)
        return row

    @app.get("/drive/v3/drives/{did}")
    async def get_drive(did: str, request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        return select(
            drive_shape(ctx, drive_row(did, request, actor), actor), request.query_params.get("fields")
        )

    @app.post("/drive/v3/drives")
    async def create_drive(request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        data = await json_object(request)
        request_id = request.query_params.get("requestId")
        if not request_id or not data.get("name"):
            raise google_error(400, "Missing requestId or name", reason="required")
        if not actor.person_id:
            raise google_error(403, "The user does not have sufficient permissions.", reason="forbidden")
        key = actor_email(ctx, actor) + ":" + request_id
        old = ctx.store.collection("drive_requests").get(key)
        if old:
            return drive_shape(ctx, ctx.store.collection("drive_drives").get(old["drive"]), actor)
        did = "0A" + ctx.ids.urlsafe(17)
        row = {
            "kind": "drive#drive",
            "id": did,
            "name": data["name"],
            "createdTime": isoformat_z(ctx.clock.now()),
            "hidden": False,
            "restrictions": data.get("restrictions", {}),
        }
        ctx.store.collection("drive_drives").put(row)
        root = new_file(
            ctx, did, row["name"], FOLDER, actor.person_id, [], drive=did, stamp=row["createdTime"]
        )
        root["_permissions"] = [permission(ctx, actor.person_id, "organizer")]
        ctx.store.collection("drive_files").put(root)
        ctx.store.collection("drive_requests").put_as(key, {"drive": did})
        await record_change(ctx, row, drive=True, before=[actor.person_id])
        return select(drive_shape(ctx, row, actor), request.query_params.get("fields"))

    @app.patch("/drive/v3/drives/{did}")
    async def update_drive(did: str, request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        row = drive_row(did, request, actor, write=True)
        data = await json_object(request)
        before = visibility(ctx, ctx.store.collection("drive_files").get(did))
        row.update(
            {
                k: data[k]
                for k in ("name", "colorRgb", "backgroundImageFile", "restrictions", "themeId")
                if k in data
            }
        )
        ctx.store.collection("drive_files").get(did)["name"] = row["name"]
        await record_change(ctx, row, drive=True, before=before)
        return select(drive_shape(ctx, row, actor), request.query_params.get("fields"))

    @app.delete("/drive/v3/drives/{did}")
    async def delete_drive(did: str, request: Request) -> Response:
        actor = principal(request, ctx)
        row = drive_row(did, request, actor, write=True)
        if any(f.get("driveId") == did and f["id"] != did for f in ctx.store.collection("drive_files")):
            raise google_error(403, "The shared drive is not empty.", reason="teamDriveNotEmpty")
        before = visibility(ctx, ctx.store.collection("drive_files").get(did))
        ctx.store.collection("drive_drives").delete(did)
        ctx.store.collection("drive_files").delete(did)
        await record_change(ctx, row, drive=True, removed=True, before=before, state="remove")
        return Response(status_code=204)

    @app.post("/drive/v3/drives/{did}/hide")
    async def hide(did: str, request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        row = drive_row(did, request, actor)
        if actor.person_id not in row.setdefault("_hidden", []):
            row["_hidden"].append(actor.person_id)
        return drive_shape(ctx, row, actor)

    @app.post("/drive/v3/drives/{did}/unhide")
    async def unhide(did: str, request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        row = drive_row(did, request, actor)
        if actor.person_id in row.setdefault("_hidden", []):
            row["_hidden"].remove(actor.person_id)
        return drive_shape(ctx, row, actor)
