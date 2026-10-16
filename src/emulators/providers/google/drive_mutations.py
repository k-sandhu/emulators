"""File mutations keep ACLs, binary metadata, revisions and the change journal together."""

from __future__ import annotations

import copy
from typing import Any

from fastapi import Request

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.providers.google.common import Principal, google_error
from emulators.providers.google.drive_access import get_file, store_binary
from emulators.providers.google.drive_changes import record_change, visibility
from emulators.providers.google.drive_seed import FOLDER, NATIVE, new_file, root_id

WRITABLE = {
    "name",
    "mimeType",
    "description",
    "starred",
    "trashed",
    "properties",
    "appProperties",
    "modifiedTime",
    "createdTime",
    "viewersCanCopyContent",
    "copyRequiresWriterPermission",
    "writersCanShare",
    "folderColorRgb",
    "contentHints",
    "shortcutDetails",
    "contentRestrictions",
}


def parents_for(ctx: ProviderContext, actor: Principal, parents: list[str]) -> tuple[list[str], str | None]:
    if len(parents) > 1:
        raise google_error(400, "A file can only have one parent.", reason="badRequest")
    resolved = []
    drive = None
    for fid in parents:
        parent = get_file(ctx, actor, fid, write=True)
        if parent["mimeType"] != FOLDER:
            raise google_error(403, "The specified parent is not a folder.", reason="parentNotAFolder")
        resolved.append(parent["id"])
        drive = parent.get("driveId")
    return resolved, drive


def save_revision(ctx: ProviderContext, row: dict[str, Any]) -> None:
    revision = {
        "kind": "drive#revision",
        "id": row["version"],
        "mimeType": row["mimeType"],
        "modifiedTime": row["modifiedTime"],
        "keepForever": False,
        "originalFilename": row["name"],
        "_file": row["id"],
    }
    for key in ("size", "md5Checksum", "_blob"):
        if key in row:
            revision[key] = row[key]
    ctx.store.collection("drive_revisions").put_as(row["id"] + ":" + row["version"], revision)


async def create_file(
    ctx: ProviderContext,
    actor: Principal,
    metadata: dict[str, Any],
    data: bytes | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    if not actor.person_id and not metadata.get("parents"):
        raise google_error(403, "Service Accounts do not have storage quota.", reason="storageQuotaExceeded")
    parents, drive = parents_for(
        ctx, actor, metadata.get("parents", [root_id(ctx, actor.person_id or actor.client_id)])
    )
    fid = metadata.get("id") or ctx.ids.token("1", 32)
    if fid in ctx.store.collection("drive_files"):
        raise google_error(409, "A file with this ID already exists.", reason="alreadyExists")
    mime = metadata.get("mimeType") or content_type or "application/octet-stream"
    row = new_file(
        ctx,
        fid,
        metadata.get("name", "Untitled"),
        mime,
        actor.person_id,
        parents,
        drive=drive,
        stamp=isoformat_z(ctx.clock.now()),
    )
    row.update({k: copy.deepcopy(v) for k, v in metadata.items() if k in WRITABLE})
    row["_modified_by"] = actor.person_id
    if mime in NATIVE:
        row["_paragraphs"] = data.decode(errors="replace").splitlines() if data else []
    elif mime != FOLDER:
        store_binary(ctx, row, data or b"")
    ctx.store.collection("drive_files").put(row)
    save_revision(ctx, row)
    await record_change(ctx, row, state="add")
    return row


async def update_file(
    ctx: ProviderContext,
    request: Request,
    actor: Principal,
    fid: str,
    metadata: dict[str, Any],
    data: bytes | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    row = get_file(ctx, actor, fid, write=True)
    before = visibility(ctx, row)
    old_trash = row["trashed"]
    if "parents" in metadata:
        raise google_error(
            403,
            "The parents field is not directly writable in update requests. Use addParents and removeParents.",
            reason="fieldNotWritable",
        )
    candidate = [
        p for p in row.get("parents", []) if p not in request.query_params.get("removeParents", "").split(",")
    ]
    if request.query_params.get("addParents"):
        candidate.extend(request.query_params["addParents"].split(","))
    if candidate != row.get("parents", []):
        parents, drive = parents_for(ctx, actor, list(dict.fromkeys(candidate)))
        if row["id"] in parents:
            raise google_error(403, "A file cannot be its own parent.", reason="invalidParent")
        ancestors = list(parents)
        seen: set[str] = set()
        while ancestors:
            parent = ancestors.pop()
            if parent == row["id"]:
                raise google_error(403, "A folder cannot be moved into itself.", reason="invalidParent")
            if parent not in seen:
                seen.add(parent)
                ancestors.extend((ctx.store.collection("drive_files").get(parent) or {}).get("parents", []))
        row["parents"] = parents
        if drive:
            row.update(driveId=drive, teamDriveId=drive)
        else:
            row.pop("driveId", None)
            row.pop("teamDriveId", None)
    row.update({k: copy.deepcopy(v) for k, v in metadata.items() if k in WRITABLE})
    row["explicitlyTrashed"] = row["trashed"]
    row["version"] = str(int(row["version"]) + 1)
    row["modifiedTime"] = metadata.get("modifiedTime", isoformat_z(ctx.clock.now()))
    row["_modified_by"] = actor.person_id
    row.pop("_exports", None)
    if data is not None:
        if row["mimeType"] in NATIVE:
            row["_paragraphs"] = data.decode(errors="replace").splitlines()
        else:
            if content_type and "mimeType" not in metadata:
                row["mimeType"] = content_type
            store_binary(ctx, row, data)
    save_revision(ctx, row)
    state = ("trash" if row["trashed"] else "untrash") if old_trash != row["trashed"] else "update"
    changed = (
        "content"
        if data is not None
        else "parents"
        if "addParents" in request.query_params or "removeParents" in request.query_params
        else "properties"
    )
    await record_change(ctx, row, before=before, state=state, changed=changed)
    return row


async def delete_file(ctx: ProviderContext, actor: Principal, fid: str) -> None:
    row = get_file(ctx, actor, fid, owner=True)
    pending = [row]
    while pending:
        current = pending.pop()
        pending.extend(
            r for r in ctx.store.collection("drive_files") if current["id"] in r.get("parents", [])
        )
        before = visibility(ctx, current)
        ctx.store.collection("drive_files").delete(current["id"])
        await record_change(ctx, current, removed=True, state="remove", before=before)
