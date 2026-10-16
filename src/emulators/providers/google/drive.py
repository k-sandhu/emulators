"""Drive v3 file routes; resource areas register independently of other Workspace APIs."""

from __future__ import annotations

import copy
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.provider import ProviderContext
from emulators.providers.google import docs_api, drive_changes, drive_resources, drive_upload
from emulators.providers.google.common import google_error, json_object, person_ref
from emulators.providers.google.drive_access import (
    EXPORTS,
    binary,
    content_scope,
    export_content,
    file_shape,
    get_file,
    principal,
    query_shape,
    role_for,
)
from emulators.providers.google.drive_changes import watch
from emulators.providers.google.drive_fields import select
from emulators.providers.google.drive_mutations import create_file, delete_file, update_file
from emulators.providers.google.drive_query import compile_query, order_files
from emulators.providers.google.drive_resources import list_page
from emulators.providers.google.drive_seed import FOLDER, NATIVE, root_id, seed

__all__ = ["register", "seed"]
DEFAULT_FILE = "kind,id,name,mimeType"
DEFAULT_LIST = "kind,nextPageToken,incompleteSearch,files(kind,id,name,mimeType)"


def supports(request: Request, row: dict[str, Any]) -> None:
    if (
        row.get("driveId")
        and request.query_params.get("supportsAllDrives", request.query_params.get("supportsTeamDrives"))
        != "true"
    ):
        raise google_error(404, f"File not found: {row['id']}.", reason="notFound")


def needs_content(fields: str | None) -> bool:
    return fields is not None and (
        "*" in fields
        or bool(
            re.search(
                r"\b(size|quotaBytesUsed|md5Checksum|sha1Checksum|sha256Checksum|headRevisionId)\b", fields
            )
        )
    )


def media_response(request: Request, data: bytes, mime: str) -> Response:
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(len(data))}
    range_header = request.headers.get("range")
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
        try:
            if not match or not any(match.groups()) or not data:
                raise ValueError
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]) if match[2] else len(data) - 1, len(data) - 1)
            else:
                length = int(match[2])
                if length < 1:
                    raise ValueError
                start, end = max(0, len(data) - length), len(data) - 1
            if start > end or start >= len(data):
                raise ValueError
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
        headers.update(
            {"Content-Range": f"bytes {start}-{end}/{len(data)}", "Content-Length": str(end - start + 1)}
        )
        return Response(data[start : end + 1], status_code=206, media_type=mime, headers=headers)
    return Response(data, media_type=mime, headers=headers)


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/drive/v3/about")
    async def about(request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        if not request.query_params.get("fields"):
            raise google_error(
                400,
                "The 'fields' parameter is required for this method.",
                reason="required",
                location="fields",
            )
        user = (
            person_ref(ctx.world.person(actor.person_id))
            if actor.person_id
            else {
                "kind": "drive#user",
                "displayName": actor.client_id,
                "emailAddress": actor.client_id,
                "permissionId": actor.client_id,
            }
        )
        user["me"] = True
        result = {
            "kind": "drive#about",
            "user": user,
            "storageQuota": {
                "limit": "2199023255552",
                "usage": str(
                    sum(
                        int(r.get("size", 0))
                        for r in ctx.store.collection("drive_files")
                        if r.get("_owner") == actor.person_id
                    )
                ),
                "usageInDrive": "0",
                "usageInDriveTrash": "0",
            },
            "exportFormats": {mime: list(formats) for mime, formats in EXPORTS.items()},
            "importFormats": {},
            "maxUploadSize": "5497558138880",
            "appInstalled": True,
            "canCreateDrives": actor.person_id is not None,
            "canCreateTeamDrives": actor.person_id is not None,
            "folderColorPalette": ["#4986e7", "#16a765"],
        }
        return select(result, request.query_params.get("fields"))

    @app.get("/drive/v3/files/generateIds")
    async def generate_ids(request: Request) -> dict[str, Any]:
        principal(request, ctx)
        try:
            count = int(request.query_params.get("count", "10"))
            if not 1 <= count <= 1000:
                raise ValueError
        except ValueError:
            raise google_error(400, "Invalid count", reason="invalid") from None
        return {
            "kind": "drive#generatedIds",
            "ids": [ctx.ids.token("1", 32) for _ in range(count)],
            "space": request.query_params.get("space", "drive"),
        }

    @app.get("/drive/v3/files")
    async def files(request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        q = request.query_params
        corpus = q.get("corpora", "user")
        if corpus not in ("user", "drive", "domain", "allDrives"):
            raise google_error(400, "Invalid corpora", reason="invalid", location="corpora")
        if corpus == "drive":
            if (
                not q.get("driveId")
                or q.get("includeItemsFromAllDrives") != "true"
                or q.get("supportsAllDrives") != "true"
            ):
                raise google_error(
                    400,
                    "The driveId parameter must be specified if and only if corpora is set to drive, and includeItemsFromAllDrives must be true.",
                    reason="invalid",
                )
            get_file(ctx, actor, q["driveId"])
        elif q.get("driveId"):
            raise google_error(
                400,
                "The driveId parameter must be specified if and only if corpora is set to drive.",
                reason="invalid",
            )
        predicate = compile_query(q.get("q", ""))
        spaces = set(q.get("spaces", "drive").split(","))
        if not spaces <= {"drive", "appDataFolder", "photos"}:
            raise google_error(400, "Invalid spaces", reason="invalid")
        rows = []
        for row in ctx.store.collection("drive_files"):
            if not row.get("parents") or not spaces.intersection(row.get("spaces", [])):
                continue
            if corpus == "drive" and row.get("driveId") != q["driveId"]:
                continue
            if row.get("driveId") and q.get("includeItemsFromAllDrives") != "true":
                continue
            if not role_for(ctx, row, actor):
                continue
            view = query_shape(ctx, row, actor)
            view["_parents"] = [
                "root" if p == root_id(ctx, actor.person_id or actor.client_id) else p
                for p in row.get("parents", [])
            ] + row.get("parents", [])
            if corpus == "domain" and not any(
                p["type"] == "domain" and p.get("allowFileDiscovery") for p in view.get("_permissions", [])
            ):
                continue
            if predicate(view):
                rows.append(view)
        rows = order_files(rows, q.get("orderBy", ""))
        result = list_page(ctx, request, rows, "files", "drive#fileList")
        result["files"] = [
            file_shape(
                ctx,
                request,
                ctx.store.collection("drive_files").get(r["id"]),
                actor,
                content_metadata=needs_content(q.get("fields")),
            )
            for r in result["files"]
        ]
        return select(result, q.get("fields"), DEFAULT_LIST)

    @app.post("/drive/v3/files")
    async def create(request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        select({"kind": "drive#file"}, request.query_params.get("fields"), DEFAULT_FILE)
        metadata = await json_object(request)
        row = await create_file(ctx, actor, metadata)
        return select(file_shape(ctx, request, row, actor), request.query_params.get("fields"), DEFAULT_FILE)

    @app.delete("/drive/v3/files/trash")
    async def empty_trash(request: Request) -> Response:
        actor = principal(request, ctx)
        for row in ctx.store.collection("drive_files").values():
            if (
                row["trashed"]
                and role_for(ctx, row, actor) == "owner"
                and row["id"] in ctx.store.collection("drive_files")
            ):
                await delete_file(ctx, actor, row["id"])
        return Response(status_code=204)

    @app.get("/drive/v3/files/{fid}")
    async def get(fid: str, request: Request) -> Any:
        actor = principal(request, ctx)
        row = get_file(ctx, actor, fid)
        supports(request, row)
        if request.query_params.get("alt") == "media":
            content_scope(actor)
            if row["mimeType"] in NATIVE:
                raise google_error(
                    403,
                    "Only files with binary content can be downloaded. Use Export with Docs Editors files.",
                    reason="fileNotDownloadable",
                )
            if row["mimeType"] == FOLDER:
                raise google_error(
                    403, "Only files with binary content can be downloaded.", reason="fileNotDownloadable"
                )
            return media_response(request, binary(ctx, row), row["mimeType"])
        fields = request.query_params.get("fields")
        return select(
            file_shape(ctx, request, row, actor, content_metadata=needs_content(fields)), fields, DEFAULT_FILE
        )

    @app.patch("/drive/v3/files/{fid}")
    async def update(fid: str, request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        select({"kind": "drive#file"}, request.query_params.get("fields"), DEFAULT_FILE)
        supports(request, get_file(ctx, actor, fid))
        row = await update_file(ctx, request, actor, fid, await json_object(request))
        return select(file_shape(ctx, request, row, actor), request.query_params.get("fields"), DEFAULT_FILE)

    @app.delete("/drive/v3/files/{fid}")
    async def delete(fid: str, request: Request) -> Response:
        actor = principal(request, ctx)
        supports(request, get_file(ctx, actor, fid))
        await delete_file(ctx, actor, fid)
        return Response(status_code=204)

    @app.post("/drive/v3/files/{fid}/copy")
    async def copy_file(fid: str, request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        row = get_file(ctx, actor, fid)
        supports(request, row)
        if row["mimeType"] == FOLDER:
            raise google_error(403, "Folders cannot be copied.", reason="cannotCopyFile")
        metadata = await json_object(request)
        metadata.setdefault("name", "Copy of " + row["name"])
        metadata.setdefault("mimeType", row["mimeType"])
        if "parents" not in metadata:
            writable_parents = [
                p
                for p in row.get("parents", [])
                if (parent := ctx.store.collection("drive_files").get(p))
                and role_for(ctx, parent, actor) in ("owner", "organizer", "fileOrganizer", "writer")
            ]
            if writable_parents:
                metadata["parents"] = writable_parents[:1]
        new = await create_file(
            ctx, actor, metadata, binary(ctx, row) if row["mimeType"] not in NATIVE else None
        )
        if row["mimeType"] in NATIVE:
            new["_paragraphs"] = copy.deepcopy(row["_paragraphs"])
            new["_table"] = copy.deepcopy(row.get("_table"))
        return select(file_shape(ctx, request, new, actor), request.query_params.get("fields"), DEFAULT_FILE)

    @app.get("/drive/v3/files/{fid}/export")
    async def export(fid: str, request: Request) -> Response:
        actor = principal(request, ctx)
        content_scope(actor)
        row = get_file(ctx, actor, fid)
        mime = request.query_params.get("mimeType", "")
        data = export_content(ctx, row, mime)
        return Response(data, media_type=mime, headers={"Content-Length": str(len(data))})

    @app.post("/drive/v3/files/{fid}/watch")
    async def watch_file(fid: str, request: Request) -> dict[str, Any]:
        return await watch(ctx, request, principal(request, ctx), fid=fid)

    @app.get("/drive/v3/files/{fid}/listLabels")
    async def labels(fid: str, request: Request) -> dict[str, Any]:
        get_file(ctx, principal(request, ctx), fid)
        return {"kind": "drive#labelList", "labels": []}

    @app.get("/drive/v3/files/{fid}/revisions")
    async def revisions(fid: str, request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        row = get_file(ctx, actor, fid)
        rows = [r for r in ctx.store.collection("drive_revisions") if r["_file"] == row["id"]]
        if not rows:
            if row["mimeType"] not in NATIVE and row["mimeType"] != FOLDER:
                binary(ctx, row)
            rows = [
                {
                    "kind": "drive#revision",
                    "id": row["version"],
                    "mimeType": row["mimeType"],
                    "modifiedTime": row["modifiedTime"],
                    "keepForever": False,
                    **{k: row[k] for k in ("size", "md5Checksum", "originalFilename") if k in row},
                }
            ]
        return select({"kind": "drive#revisionList", "revisions": rows}, request.query_params.get("fields"))

    @app.get("/drive/v3/files/{fid}/revisions/{rid}")
    async def revision(fid: str, rid: str, request: Request) -> Any:
        actor = principal(request, ctx)
        if request.query_params.get("alt") == "media":
            content_scope(actor)
        row = get_file(ctx, actor, fid)
        record = ctx.store.collection("drive_revisions").get(row["id"] + ":" + rid)
        if record is None and rid in (row["version"], row.get("headRevisionId"), "head"):
            if request.query_params.get("alt") == "media":
                return media_response(request, binary(ctx, row), row["mimeType"])
            record = {
                "kind": "drive#revision",
                "id": row["version"],
                "mimeType": row["mimeType"],
                "modifiedTime": row["modifiedTime"],
                "keepForever": False,
            }
        if not record:
            raise google_error(404, "Revision not found: " + rid, reason="notFound")
        if request.query_params.get("alt") == "media" and record.get("_blob"):
            return media_response(request, ctx.store.blobs.get(record["_blob"]), record["mimeType"])
        return select(record, request.query_params.get("fields"))

    drive_resources.register(app, ctx)
    drive_changes.register(app, ctx)
    drive_upload.register(app, ctx)
    docs_api.register(app, ctx)
