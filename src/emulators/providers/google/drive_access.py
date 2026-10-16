"""Evaluate inherited ACLs at request time so Directory membership changes take effect."""

from __future__ import annotations

import copy
import hashlib
import html
from typing import Any

from fastapi import Request

from emulators.core.clock import parse_instant
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.google.common import Principal, google_error, person_ref
from emulators.providers.google.common import principal as google_principal
from emulators.providers.google.directory import expanded_members
from emulators.providers.google.drive_fields import needs_binary_metadata
from emulators.providers.google.drive_seed import DOC, FOLDER, NATIVE, SHEET, SLIDE, permission, root_id
from emulators.world.documents import EXTENSION_TO_MIME, render
from emulators.world.media import make_mp4

RANK = {"reader": 1, "commenter": 2, "writer": 3, "fileOrganizer": 4, "organizer": 5, "owner": 6}
EXPORTS = {
    DOC: {
        "text/plain": "txt",
        "text/html": "html",
        "text/markdown": "md",
        "application/pdf": "pdf",
        EXTENSION_TO_MIME["docx"]: "docx",
    },
    SHEET: {
        "text/plain": "txt",
        "text/csv": "csv",
        "application/pdf": "pdf",
        EXTENSION_TO_MIME["xlsx"]: "xlsx",
    },
    SLIDE: {"text/plain": "txt", "application/pdf": "pdf", EXTENSION_TO_MIME["pptx"]: "pptx"},
}


def principal(request: Request, ctx: ProviderContext, *, legacy: bool = True) -> Principal:
    actor = google_principal(request, ctx, legacy=legacy)
    write = request.method not in ("GET", "HEAD") and not request.url.path.endswith(
        ("/watch", "/channels/stop")
    )
    allowed = {"drive", "drive.file", "drive.appdata"}
    if not write:
        allowed |= {"drive.readonly", "drive.metadata", "drive.metadata.readonly"}
    elif (
        request.url.path.startswith("/drive/v3/files")
        and request.method == "PATCH"
        and "/permissions" not in request.url.path
    ):
        allowed.add("drive.metadata")
    if request.url.path.startswith("/v1/documents"):
        allowed.add("documents")
        if not write:
            allowed.add("documents.readonly")
    if actor.scopes and not any(
        "https://www.googleapis.com/auth/" + scope in actor.scopes for scope in allowed
    ):
        raise google_error(
            403,
            "Request had insufficient authentication scopes.",
            reason="insufficientPermissions",
            legacy=legacy,
        )
    return actor


def content_scope(actor: Principal) -> None:
    allowed = {
        "https://www.googleapis.com/auth/" + name
        for name in (
            "drive",
            "drive.readonly",
            "drive.file",
            "drive.appdata",
            "documents",
            "documents.readonly",
        )
    }
    if actor.scopes and not allowed.intersection(actor.scopes):
        raise google_error(
            403, "Request had insufficient authentication scopes.", reason="insufficientPermissions"
        )


def actor_email(ctx: ProviderContext, actor: Principal) -> str:
    return ctx.world.person(actor.person_id).email if actor.person_id else actor.client_id


def all_permissions(
    ctx: ProviderContext, row: dict[str, Any], seen: set[str] | None = None
) -> list[dict[str, Any]]:
    seen = set() if seen is None else seen
    if row["id"] in seen:
        return []
    seen.add(row["id"])
    result = copy.deepcopy(row.get("_permissions", []))
    if not row.get("driveId") and row.get("_owner") and not any(p["role"] == "owner" for p in result):
        result.append(permission(ctx, row["_owner"], "owner"))
    if row.get("driveId"):
        for perm in result:
            perm["permissionDetails"] = [
                {
                    "permissionType": "member" if row["id"] == row["driveId"] else "file",
                    "role": perm["role"],
                    "inherited": False,
                }
            ]
    if not row.get("_restricted"):
        for parent_id in row.get("parents", []):
            parent = ctx.store.collection("drive_files").get(parent_id)
            if not parent:
                continue
            for perm in all_permissions(ctx, parent, seen):
                if perm["role"] == "owner":
                    # Folder access is inherited; ownership of a different file is not.
                    perm["role"] = "writer"
                if row.get("driveId"):
                    inherited_from = next(
                        (
                            d.get("inheritedFrom")
                            for d in perm.get("permissionDetails", [])
                            if d.get("inheritedFrom")
                        ),
                        parent_id,
                    )
                    perm["permissionDetails"] = [
                        {
                            "permissionType": "member",
                            "role": perm["role"],
                            "inheritedFrom": inherited_from,
                            "inherited": True,
                        }
                    ]
                result.append(perm)
    combined: dict[str, dict[str, Any]] = {}
    for perm in result:
        if perm["id"] not in combined or RANK.get(perm["role"], 0) > RANK.get(
            combined[perm["id"]]["role"], 0
        ):
            combined[perm["id"]] = perm
    return list(combined.values())


def applies(ctx: ProviderContext, perm: dict[str, Any], actor: Principal, *, known_id: bool = False) -> bool:
    if perm.get("expirationTime") and parse_instant(perm["expirationTime"]) <= ctx.clock.now():
        return False
    email = actor_email(ctx, actor)
    if perm["type"] == "anyone":
        return known_id or perm.get("allowFileDiscovery", False)
    if perm["type"] == "domain":
        return "@" in email and email.split("@")[-1] == perm.get("domain")
    if perm["type"] == "user":
        return email.lower() == str(perm.get("emailAddress", "")).lower()
    if perm["type"] == "group" and actor.person_id:
        group = next(
            (g for g in ctx.store.collection("directory_groups") if g["email"] == perm.get("emailAddress")),
            None,
        )
        if group:
            return any(m.get("email") == email for m in expanded_members(ctx, group["id"], derived=True))
    return False


def role_for(
    ctx: ProviderContext, row: dict[str, Any], actor: Principal, *, known_id: bool = False
) -> str | None:
    roles = [p["role"] for p in all_permissions(ctx, row) if applies(ctx, p, actor, known_id=known_id)]
    return max(roles, key=lambda r: RANK.get(r, 0)) if roles else None


def get_file(
    ctx: ProviderContext, actor: Principal, fid: str, *, write: bool = False, owner: bool = False
) -> dict[str, Any]:
    if fid == "root":
        fid = root_id(ctx, actor.person_id or actor.client_id)
    row = ctx.store.collection("drive_files").get(fid)
    role = role_for(ctx, row, actor, known_id=True) if row else None
    if not row or not role:
        raise google_error(404, f"File not found: {fid}.", reason="notFound", location="fileId")
    if (write and RANK[role] < 3) or (owner and role not in ("owner", "organizer")):
        raise google_error(
            403,
            "The user does not have sufficient permissions for this file.",
            reason="insufficientFilePermissions",
        )
    return row


def binary(ctx: ProviderContext, row: dict[str, Any]) -> bytes:
    if row.get("_blob"):
        return ctx.store.blobs.get(row["_blob"])
    if row.get("_kind") == "mp4":
        data = make_mp4(
            row.get("_duration", 60), title=row["name"], created=parse_instant(row["createdTime"])
        )
    else:
        data, _ = render(
            row.get("_kind", "txt"),
            row.get("_title", row["name"]),
            row.get("_paragraphs", []),
            created=parse_instant(row["createdTime"]),
            table=row.get("_table"),
        )
    store_binary(ctx, row, data)
    return data


def is_trashed(ctx: ProviderContext, row: dict[str, Any], seen: set[str] | None = None) -> bool:
    seen = set() if seen is None else seen
    if row["id"] in seen:
        return False
    seen.add(row["id"])
    return bool(row.get("trashed")) or any(
        is_trashed(ctx, parent, seen)
        for parent_id in row.get("parents", [])
        if (parent := ctx.store.collection("drive_files").get(parent_id))
    )


def store_binary(ctx: ProviderContext, row: dict[str, Any], data: bytes) -> None:
    row["_blob"] = ctx.store.blobs.put(data)
    row["size"] = str(len(data))
    row["quotaBytesUsed"] = str(len(data))
    row["md5Checksum"] = hashlib.md5(data).hexdigest()
    row["sha1Checksum"] = hashlib.sha1(data).hexdigest()
    row["sha256Checksum"] = hashlib.sha256(data).hexdigest()
    row["headRevisionId"] = row["version"]
    row["originalFilename"] = row["name"]
    extension = row["name"].rsplit(".", 1)[-1] if "." in row["name"] else ""
    row.update(fileExtension=extension, fullFileExtension=extension)
    if row["mimeType"].startswith("text/"):
        row["_paragraphs"] = data.decode(errors="replace").splitlines()


def export_content(ctx: ProviderContext, row: dict[str, Any], mime: str) -> bytes:
    kind = EXPORTS.get(row["mimeType"], {}).get(mime)
    if not kind:
        raise google_error(
            400, "Export only supports Docs Editors files and supported export formats.", reason="badRequest"
        )
    cached = row.setdefault("_exports", {})
    if mime not in cached:
        paragraphs = row.get("_paragraphs", [])
        if kind == "txt":
            data = ("\n".join(paragraphs) + ("\n" if paragraphs else "")).encode()
        elif kind == "html":
            data = (
                "<!doctype html><html><body>"
                + "".join("<p>" + html.escape(p) + "</p>" for p in paragraphs)
                + "</body></html>"
            ).encode()
        else:
            data, _ = render(
                kind,
                row.get("_title", row["name"]),
                paragraphs,
                created=parse_instant(row["createdTime"]),
                table=row.get("_table"),
            )
        cached[mime] = ctx.store.blobs.put(data)
    data = ctx.store.blobs.get(cached[mime])
    if len(data) > 10 * 1024 * 1024:
        raise google_error(403, "This file is too large to be exported.", reason="exportSizeLimitExceeded")
    return data


def file_shape(
    ctx: ProviderContext,
    request: Request,
    row: dict[str, Any],
    actor: Principal,
    *,
    content_metadata: bool = False,
) -> dict[str, Any]:
    mime = row["mimeType"]
    if (
        (content_metadata or needs_binary_metadata(request.query_params.get("fields")))
        and mime not in NATIVE
        and mime != FOLDER
    ):
        binary(ctx, row)
    result = {k: copy.deepcopy(v) for k, v in row.items() if not k.startswith("_")}
    result["trashed"] = is_trashed(ctx, row)
    owner = next((p for p in ctx.world.people if p.id == row.get("_owner")), None)
    modified = next((p for p in ctx.world.people if p.id == row.get("_modified_by", row.get("_owner"))), None)
    if not row.get("driveId") and owner:
        result["owners"] = [{**person_ref(owner), "me": owner.id == actor.person_id}]
    if modified:
        result["lastModifyingUser"] = {**person_ref(modified), "me": modified.id == actor.person_id}
    permissions = all_permissions(ctx, row)
    role = role_for(ctx, row, actor, known_id=True)
    edit = RANK.get(role or "", 0) >= 3
    own = role in ("owner", "organizer")
    result.update(
        ownedByMe=role == "owner",
        shared=bool(row.get("driveId") or any(p["role"] != "owner" for p in permissions)),
        permissions=permissions,
        permissionIds=[p["id"] for p in permissions],
        capabilities={
            "canEdit": edit,
            "canComment": RANK.get(role or "", 0) >= 2,
            "canShare": edit and (row.get("writersCanShare", True) or own),
            "canCopy": True,
            "canDownload": True,
            "canDelete": own,
            "canTrash": edit,
            "canUntrash": edit,
            "canAddChildren": edit and mime == FOLDER,
            "canListChildren": mime == FOLDER,
            "canRename": edit,
            "canMoveItemWithinDrive": edit,
            "canReadRevisions": True,
        },
        modifiedByMe=row.get("_modified_by", row.get("_owner")) == actor.person_id,
    )
    if result["modifiedByMe"]:
        result["modifiedByMeTime"] = row["modifiedTime"]
    if any(
        p["type"] == "user" and p.get("emailAddress") == actor_email(ctx, actor) and p["role"] != "owner"
        for p in row.get("_permissions", [])
    ):
        result["sharedWithMeTime"] = row["createdTime"]
        if owner:
            result["sharingUser"] = person_ref(owner)
    doc_type = {DOC: "document", SHEET: "spreadsheets", SLIDE: "presentation"}.get(mime)
    if doc_type:
        result["webViewLink"] = url_for_host(
            request, ctx, "docs.google.com", f"/{doc_type}/d/{row['id']}/edit?usp=drivesdk"
        )
        result["exportLinks"] = {
            m: url_for_host(
                request, ctx, "www.googleapis.com", f"/drive/v3/files/{row['id']}/export?mimeType={m}"
            )
            for m in EXPORTS[mime]
        }
    else:
        path = f"/drive/folders/{row['id']}" if mime == FOLDER else f"/file/d/{row['id']}/view?usp=drivesdk"
        result["webViewLink"] = url_for_host(request, ctx, "drive.google.com", path)
        if mime != FOLDER:
            result["webContentLink"] = url_for_host(
                request, ctx, "www.googleapis.com", f"/drive/v3/files/{row['id']}?alt=media"
            )
    result["iconLink"] = url_for_host(request, ctx, "drive.google.com", f"/favicon.ico?mime={mime}")
    if row.get("hasThumbnail"):
        result["thumbnailLink"] = url_for_host(
            request, ctx, "drive.google.com", f"/thumbnail?id={row['id']}&sz=s220"
        )
    return result


def query_shape(ctx: ProviderContext, row: dict[str, Any], actor: Principal) -> dict[str, Any]:
    result = dict(row)
    result["trashed"] = is_trashed(ctx, row)
    result["_text"] = (
        row["name"] + " " + row.get("description", "") + " " + " ".join(row.get("_paragraphs", []))
    )
    perms = all_permissions(ctx, row)
    email = actor_email(ctx, actor)
    result["sharedWithMe"] = any(
        p["type"] == "user" and p.get("emailAddress") == email and p["role"] != "owner"
        for p in row.get("_permissions", [])
    )
    for key, roles in (
        ("owners", {"owner"}),
        ("writers", {"owner", "organizer", "fileOrganizer", "writer"}),
        ("readers", set(RANK)),
    ):
        emails = [p["emailAddress"] for p in perms if p.get("emailAddress") and p["role"] in roles]
        for perm in perms:
            if perm["role"] not in roles:
                continue
            if perm["type"] == "group":
                group = next(
                    (
                        g
                        for g in ctx.store.collection("directory_groups")
                        if g["email"] == perm.get("emailAddress")
                    ),
                    None,
                )
                if group:
                    emails.extend(
                        m["email"] for m in expanded_members(ctx, group["id"], derived=True) if m.get("email")
                    )
            elif perm["type"] == "domain":
                emails.extend(
                    p.email for p in ctx.world.people if p.email.endswith("@" + perm.get("domain", ""))
                )
        if email in emails or any(p["role"] in roles and applies(ctx, p, actor) for p in perms):
            emails.append("me")
        result["_" + key] = emails
    result["visibility"] = (
        "anyoneWithLink"
        if any(p["type"] == "anyone" and not p.get("allowFileDiscovery") for p in perms)
        else "limited"
    )
    return result
