"""Drive projection, inherited ACLs and lazy binary storage shared by the files modules."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import mimetypes
import re
import secrets
import xml.etree.ElementTree as ET
import zipfile
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import Response

from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.ids import stable_guid
from emulators.core.odata import ODataError
from emulators.core.provider import ProviderContext, external_base, url_for_host
from emulators.providers.microsoft import common
from emulators.providers.microsoft.quickxor import quickxor_hash
from emulators.world import documents, media

DRIVES = "ms_drives"
ITEMS = "ms_drive_items"
PERMISSIONS = "ms_drive_permissions"
HOST = "halcyonadvisory.sharepoint.com"
ROLES = {"viewer": "read", "commenter": "read", "editor": "write", "owner": "owner"}
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


async def json_body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise common.graph_error(400, "invalidRequest", "The request body must contain valid JSON.") from None
    if not isinstance(value, dict):
        raise common.graph_error(400, "invalidRequest", "The request body must be a JSON object.")
    return value


def public(record: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy({k: v for k, v in record.items() if not k.startswith("_")})


def path_of(request: Request) -> str:
    root = request.scope.get("root_path", "")
    path = request.url.path
    return path.removeprefix(root) if root else path


def graph_url(request: Request, ctx: ProviderContext, path: str) -> str:
    return url_for_host(request, ctx, "graph.microsoft.com", path)


def collection(request: Request, records: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    try:
        result = common.collection_response(request, [public(r) for r in records], **kwargs)
    except ODataError as exc:
        raise common.graph_error(400, exc.code, exc.message) from None
    if "@odata.nextLink" in result:
        query = result["@odata.nextLink"].split("?", 1)[1]
        result["@odata.nextLink"] = external_base(request) + path_of(request) + "?" + query
    return result


def single(request: Request, record: dict[str, Any], context: str) -> dict[str, Any]:
    return common.single_response(request, public(record), context=context)


def site_id(ctx: ProviderContext, space_id: str = "root") -> str:
    return f"{HOST},{stable_guid(ctx.world.seed, 'site-collection', space_id)},{stable_guid(ctx.world.seed, 'site-web', space_id)}"


def drive_id(ctx: ProviderContext, space_id: str) -> str:
    digest = hashlib.sha384(f"{ctx.world.seed}/drive/{space_id}".encode()).digest()
    return "b!" + base64.urlsafe_b64encode(digest).decode()


def item_id(ctx: ProviderContext, did: str, key: str) -> str:
    digest = hashlib.sha256(f"{ctx.world.seed}/{did}/{key}".encode()).digest()[:20]
    return "01" + base64.b32encode(digest).decode()


def person_id(ctx: ProviderContext, identifier: str) -> str:
    for person in ctx.world.people:
        if identifier in (person.id, common.user_id(ctx.world, person.id), common.upn(ctx.world, person)):
            return person.id
    raise common.item_not_found()


def personal_drive(ctx: ProviderContext, pid: str) -> dict[str, Any]:
    record = ctx.store[DRIVES].first(_owner=pid, driveType="business")
    if record is None:
        raise common.item_not_found()
    return record


def drive_for_site(ctx: ProviderContext, sid: str) -> dict[str, Any]:
    record = ctx.store[DRIVES].first(_site=sid)
    if record is None:
        raise common.item_not_found()
    return record


def drive_for_group(ctx: ProviderContext, gid: str) -> dict[str, Any]:
    group = next((g for g in ctx.world.groups if common.group_id(ctx.world, g.id) == gid), None)
    team = next(
        (t for t in ctx.world.teams if common.group_id(ctx.world, t.group_id or f"team-{t.id}") == gid), None
    )
    candidates = [d for d in ctx.store[DRIVES] if d["driveType"] == "documentLibrary"]
    if group:
        for drive in candidates:
            if any(p == group.id for p, _ in ctx.world.space(drive["_space"]).members):
                return drive
    if team:
        for drive in candidates:
            if drive["_space"] == "clients" and team.client_id:
                return drive
            if drive["_owner"] in team.owner_ids:
                return drive
    raise common.item_not_found()


def permissions(ctx: ProviderContext, item: dict[str, Any]) -> list[dict[str, Any]]:
    direct = ctx.store[PERMISSIONS].where(_item=item["id"], _drive=item["_drive"])
    if item.get("_restricted") or "root" in item:
        return direct
    parent = ctx.store[ITEMS].get(item["parentReference"]["id"])
    if parent is None:
        return direct
    inherited = []
    for perm in permissions(ctx, parent):
        record = copy.deepcopy(perm)
        record.setdefault("inheritedFrom", {"driveId": item["_drive"], "id": parent["id"]})
        inherited.append(record)
    return inherited + direct


def role_for(ctx: ProviderContext, item: dict[str, Any], principal: common.Principal) -> str | None:
    if principal.app_only:
        return "owner"
    best = None
    for perm in permissions(ctx, item):
        expiry = perm.get("expirationDateTime")
        if expiry and parse_instant(expiry) <= ctx.clock.now():
            continue
        targets = perm.get("_principals", [])
        allowed = any(principal.person_id in ctx.world.expand_principal(p) for p in targets)
        link_scope = perm.get("link", {}).get("scope")
        if link_scope == "anonymous":
            allowed = True
        if link_scope == "organization" and principal.person_id:
            allowed |= not ctx.world.person(principal.person_id).is_external
        if allowed:
            role = max(perm["roles"], key=lambda r: {"read": 1, "write": 2, "owner": 3}.get(r, 0))
            if {None: 0, "read": 1, "write": 2, "owner": 3}[role] > {
                None: 0,
                "read": 1,
                "write": 2,
                "owner": 3,
            }[best]:
                best = role
    return best


def require_item(
    ctx: ProviderContext, did: str, iid: str, principal: common.Principal, *, write: bool = False
) -> dict[str, Any]:
    item = ctx.store[ITEMS].get(iid)
    if item is None or item["_drive"] != did or item.get("_deleted"):
        raise common.item_not_found()
    role = role_for(ctx, item, principal)
    if role is None:
        raise common.item_not_found()
    if write and role not in ("write", "owner"):
        raise common.access_denied()
    return item


def root_item(ctx: ProviderContext, drive: dict[str, Any]) -> dict[str, Any]:
    return ctx.store[ITEMS].get(drive["_root"])  # type: ignore[return-value]


def can_see_drive(ctx: ProviderContext, drive: dict[str, Any], p: common.Principal) -> bool:
    return role_for(ctx, root_item(ctx, drive), p) is not None or any(
        not i.get("_deleted") and role_for(ctx, i, p) for i in ctx.store[ITEMS].where(_drive=drive["id"])
    )


def add_permission(
    ctx: ProviderContext,
    item: dict[str, Any],
    target: str | None,
    roles: list[str],
    *,
    key: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    pid = key or stable_guid(ctx.world.seed, item["id"], target, len(ctx.store[PERMISSIONS]))
    record: dict[str, Any] = {
        "id": pid,
        "roles": roles,
        "_item": item["id"],
        "_drive": item["_drive"],
        "_principals": [target] if target else [],
        **fields,
    }
    if target:
        group = next((g for g in ctx.world.groups if g.id == target), None)
        granted = (
            {"group": {"id": common.group_id(ctx.world, group.id), "displayName": group.name}}
            if group
            else common.identity(ctx.world, ctx.world.person(target))
        )
        record["grantedToV2"] = granted
        record["grantedToIdentitiesV2"] = [copy.deepcopy(granted)]
    return ctx.store[PERMISSIONS].put(record)


def new_item(
    ctx: ProviderContext,
    drive: dict[str, Any],
    parent: dict[str, Any] | None,
    name: str,
    *,
    key: str,
    owner: str | None = None,
    folder: bool = False,
    created: str | None = None,
) -> dict[str, Any]:
    owner = owner or drive["_owner"]
    iid = item_id(ctx, drive["id"], key)
    timestamp = created or isoformat_z(ctx.world.anchor)
    path = ((parent["_path"] + "/") if parent and parent["_path"] else "") + name if parent else ""
    parent_ref: dict[str, Any] = {"driveType": drive["driveType"], "driveId": drive["id"]}
    if parent:
        parent_ref.update(
            id=parent["id"],
            name=parent["name"],
            path="/drive/root:" + ("/" + parent["_path"] if parent["_path"] else ""),
        )
    if drive.get("_site"):
        parent_ref["siteId"] = drive["_site"]
    item: dict[str, Any] = {
        "id": iid,
        "name": name,
        "size": 0,
        "eTag": f'"{{{stable_guid(iid).upper()}}},1"',
        "cTag": f'"c:{{{stable_guid(iid).upper()}}},1"',
        "createdDateTime": timestamp,
        "lastModifiedDateTime": timestamp,
        "createdBy": common.identity(ctx.world, ctx.world.person(owner)),
        "lastModifiedBy": common.identity(ctx.world, ctx.world.person(owner)),
        "webUrl": drive["webUrl"] + ("/" + quote(path) if path else ""),
        "parentReference": parent_ref,
        "fileSystemInfo": {"createdDateTime": timestamp, "lastModifiedDateTime": timestamp},
        "shared": {"scope": "users", "owner": common.identity(ctx.world, ctx.world.person(owner))},
        "_drive": drive["id"],
        "_path": path,
        "_owner": owner,
        "_version": 1,
    }
    if folder:
        item["folder"] = {
            "childCount": 0,
            "view": {"viewType": "details", "sortBy": "name", "sortOrder": "ascending"},
        }
    else:
        item["file"] = {"mimeType": mimetypes.guess_type(name)[0] or "application/octet-stream", "hashes": {}}
    if parent is None:
        item["root"] = {}
    else:
        parent["folder"]["childCount"] += 1
    return ctx.store[ITEMS].put(item)


def ensure_folder(ctx: ProviderContext, drive: dict[str, Any], path: str) -> dict[str, Any]:
    parent = root_item(ctx, drive)
    for segment in path.strip("/").split("/") if path else []:
        match = child_named(ctx, parent, segment)
        parent = match or new_item(
            ctx,
            drive,
            parent,
            segment,
            key="folder/" + ((parent["_path"] + "/") if parent["_path"] else "") + segment,
            folder=True,
        )
    return parent


def child_named(ctx: ProviderContext, parent: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next(
        (
            i
            for i in ctx.store[ITEMS].where(_drive=parent["_drive"])
            if not i.get("_deleted")
            and i.get("parentReference", {}).get("id") == parent["id"]
            and i["name"].casefold() == name.casefold()
        ),
        None,
    )


def children(ctx: ProviderContext, parent: dict[str, Any], p: common.Principal) -> list[dict[str, Any]]:
    return [
        i
        for i in ctx.store[ITEMS].where(_drive=parent["_drive"])
        if not i.get("_deleted")
        and i.get("parentReference", {}).get("id") == parent["id"]
        and role_for(ctx, i, p)
    ]


def seed(ctx: ProviderContext) -> None:
    for space in ctx.world.spaces:
        did = drive_id(ctx, space.id)
        owner = ctx.world.person(space.owner_id)
        web = (
            f"https://{ctx.world.org.slug}-my.sharepoint.com/personal/{common.upn(ctx.world, owner).replace('.', '_').replace('@', '_')}/Documents"
            if space.kind == "personal"
            else f"https://{HOST}/sites/{space.slug}/Shared%20Documents"
        )
        drive = ctx.store[DRIVES].put(
            {
                "id": did,
                "name": "OneDrive" if space.kind == "personal" else "Documents",
                "driveType": "business" if space.kind == "personal" else "documentLibrary",
                "description": space.description,
                "webUrl": web,
                "createdDateTime": isoformat_z(ctx.world.anchor - timedelta(days=365)),
                "lastModifiedDateTime": isoformat_z(ctx.world.anchor),
                "owner": common.identity(ctx.world, owner),
                "quota": {
                    "total": 1099511627776,
                    "used": 0,
                    "remaining": 1099511627776,
                    "deleted": 0,
                    "state": "normal",
                },
                "_space": space.id,
                "_owner": owner.id,
                "_site": site_id(ctx, space.id) if space.kind == "team" else None,
            }
        )
        root = new_item(ctx, drive, None, "root", key="root", folder=True)
        drive["_root"] = root["id"]
        add_permission(ctx, root, space.owner_id, ["owner"])
        for target, role in space.members:
            add_permission(ctx, root, target, [ROLES[role]])
        if space.kind == "personal":
            ensure_folder(ctx, drive, "Apps")
        for path in ctx.world.folders_in(space.id):
            ensure_folder(ctx, drive, path)
        for doc in ctx.world.documents_in(space.id):
            parent = ensure_folder(ctx, drive, doc.folder)
            item = new_item(
                ctx, drive, parent, doc.name, key=doc.id, owner=doc.owner_id, created=isoformat_z(doc.created)
            )
            item.update(
                _doc=doc.id,
                _restricted=doc.restricted,
                _version=doc.versions,
                _deleted=doc.trashed,
                lastModifiedDateTime=isoformat_z(doc.modified),
                description=doc.description,
                lastModifiedBy=common.identity(ctx.world, ctx.world.person(doc.modified_by_id)),
            )
            item["file"]["mimeType"] = documents.EXTENSION_TO_MIME.get(doc.kind, "application/octet-stream")
            item["fileSystemInfo"]["lastModifiedDateTime"] = isoformat_z(doc.modified)
            add_permission(ctx, item, doc.owner_id, ["owner"])
            for index, share in enumerate(doc.shares):
                fields: dict[str, Any] = {}
                if share.expires:
                    fields["expirationDateTime"] = isoformat_z(share.expires)
                if share.link != "none":
                    fields["link"] = {
                        "scope": "anonymous" if share.link == "anyone" else "organization",
                        "type": "view" if share.role in ("viewer", "commenter") else "edit",
                        "webUrl": f"https://{HOST}/:w:/s/{space.slug}/{item['id']}",
                    }
                elif share.email:
                    fields["invitation"] = {"email": share.email, "signInRequired": True}
                add_permission(
                    ctx,
                    item,
                    share.principal_id,
                    [ROLES[share.role]],
                    key=stable_guid(item["id"], "share", index),
                    **fields,
                )
    for meeting in ctx.world.meetings:
        if meeting.platform == "teams" and meeting.recorded and meeting.happened:
            add_recording(ctx, meeting.id, meeting.organizer_id, meeting.title, meeting.start, meeting.end)


def add_recording(
    ctx: ProviderContext, mid: str, organizer: str, title: str, start: Any, end: Any
) -> dict[str, Any]:
    drive = personal_drive(ctx, organizer)
    iid = item_id(ctx, drive["id"], "recording/" + mid)
    old = ctx.store[ITEMS].get(iid)
    if old:
        return old
    parent = ensure_folder(ctx, drive, "Recordings")
    name = re.sub(r'["*:<>?/\\|]', "_", title) + start.strftime("-%Y%m%d_%H%M%S-Meeting Recording.mp4")
    item = new_item(ctx, drive, parent, name, key="recording/" + mid, created=isoformat_z(end))
    item.update(_recording=mid, _duration=max(1, (end - start).total_seconds()), _title=title)
    item["file"]["mimeType"] = "video/mp4"
    return item


def content(ctx: ProviderContext, item: dict[str, Any], fmt: str | None = None) -> tuple[bytes, str]:
    if "folder" in item:
        raise common.graph_error(400, "invalidRequest", "Cannot download a folder.")
    if fmt and fmt != "pdf":
        raise common.graph_error(400, "invalidRequest", "Unsupported conversion format.")
    cache = "_pdf_blob" if fmt == "pdf" else "_blob"
    if cache not in item:
        doc = ctx.world.document(item["_doc"]) if item.get("_doc") else None
        if fmt == "pdf":
            original, mime = content(ctx, item)
            if mime == "application/pdf":
                data = original
            else:
                kind = item["name"].rsplit(".", 1)[-1].lower()
                paragraphs = (
                    list(doc.paragraphs) if doc and not item.get("_uploaded") else office_text(original, kind)
                )
                if kind not in ("docx", "xlsx", "pptx"):
                    raise common.graph_error(406, "notSupported", "This item cannot be converted to PDF.")
                data, _ = documents.render(
                    "pdf",
                    doc.title if doc and not item.get("_uploaded") else item["name"],
                    paragraphs,
                    author=ctx.world.person(item["_owner"]).display_name,
                    created=parse_instant(item["createdDateTime"]),
                )
        elif item.get("_recording"):
            data = media.make_mp4(
                item["_duration"], title=item["_title"], created=parse_instant(item["createdDateTime"])
            )
        elif doc:
            data, _ = documents.render(
                doc.kind,
                doc.title,
                list(doc.paragraphs),
                author=ctx.world.person(doc.owner_id).display_name,
                created=doc.created,
                table=[list(r) for r in doc.table] if doc.table else None,
            )
        else:
            data = b""
        item[cache] = ctx.store.blobs.put(data)
    data = ctx.store.blobs.get(item[cache])
    if not fmt:
        item["size"] = len(data)
        if not item["file"]["hashes"]:
            item["file"]["hashes"] = {
                "quickXorHash": quickxor_hash(data),
                "sha1Hash": hashlib.sha1(data).hexdigest().upper(),
                "sha256Hash": hashlib.sha256(data).hexdigest().upper(),
            }
    return data, "application/pdf" if fmt == "pdf" else item["file"]["mimeType"]


def office_text(data: bytes, kind: str) -> list[str]:
    """Extract text with the stdlib so uploaded Office renditions never use old seed text."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if kind == "docx":
                roots = [ET.fromstring(archive.read("word/document.xml"))]
            elif kind == "pptx":
                roots = [
                    ET.fromstring(archive.read(name))
                    for name in sorted(archive.namelist())
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ]
            elif kind == "xlsx":
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                strings = (
                    ET.fromstring(archive.read("xl/sharedStrings.xml"))
                    if "xl/sharedStrings.xml" in archive.namelist()
                    else None
                )
                shared = ["".join(node.itertext()) for node in strings] if strings is not None else []
                paragraphs = []
                for name in sorted(archive.namelist()):
                    if not re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name):
                        continue
                    for row in ET.fromstring(archive.read(name)).findall(".//s:row", ns):
                        values = []
                        for cell in row:
                            value = cell.findtext("s:v", default="", namespaces=ns)
                            if cell.get("t") == "s":
                                value = shared[int(value)]
                            elif cell.get("t") == "inlineStr":
                                value = "".join(cell.find("s:is", ns).itertext())
                            values.append(value)
                        paragraphs.append(" | ".join(values))
                return paragraphs
            else:
                raise ValueError
            return [
                node.text or ""
                for root in roots
                for node in root.iter()
                if node.tag.rsplit("}", 1)[-1] == "t"
            ]
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, IndexError, AttributeError):
        raise common.graph_error(406, "notSupported", "The file cannot be converted to PDF.") from None


def binary_response(request: Request, data: bytes, mime: str, etag: str | None = None) -> Response:
    headers = {"Accept-Ranges": "bytes"}
    if etag:
        headers["ETag"] = etag
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
    requested = request.headers.get("range")
    if requested and (not request.headers.get("if-range") or request.headers["if-range"] == etag):
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
        if not match or not any(match.groups()):
            raise common.graph_error(416, "invalidRange", "Invalid byte range.")
        lo, hi = match.groups()
        start = int(lo) if lo else max(0, len(data) - int(hi))
        end = min(int(hi), len(data) - 1) if hi and lo else len(data) - 1
        if start >= len(data) or end < start:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{len(data)}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        return Response(data[start : end + 1], status_code=206, media_type=mime, headers=headers)
    return Response(data, media_type=mime, headers=headers)


def download_url(request: Request, ctx: ProviderContext, item: dict[str, Any], fmt: str | None = None) -> str:
    capability = secrets.token_urlsafe(32)
    expiry = isoformat_z(ctx.clock.now() + timedelta(hours=1))
    ctx.store["ms_drive_downloads"].put(
        {"id": capability, "item": item["id"], "expires": expiry, "format": fmt}
    )
    host = (
        "halcyonadvisory-my.sharepoint.com"
        if ctx.store[DRIVES].get(item["_drive"])["driveType"] == "business"
        else HOST
    )
    return url_for_host(request, ctx, host, "/_api/v2.0/download?" + urlencode({"authkey": capability}))


def wire_item(request: Request, ctx: ProviderContext, item: dict[str, Any]) -> dict[str, Any]:
    if "file" in item:
        content(ctx, item)
    record = public(item)
    if "file" in item:
        record["@microsoft.graph.downloadUrl"] = download_url(request, ctx, item)
    if "thumbnails" in request.query_params.get("$expand", "") and "file" in item:
        record["thumbnails"] = thumbnails(request, ctx, item)
    return record


def thumbnails(request: Request, ctx: ProviderContext, item: dict[str, Any]) -> list[dict[str, Any]]:
    base = graph_url(
        request, ctx, f"/{common.version_of(request)}/drives/{item['_drive']}/items/{item['id']}/thumbnails/0"
    )
    return [
        {
            "id": "0",
            **{
                size: {"height": pixels, "width": pixels, "url": f"{base}/{size}/content"}
                for size, pixels in (("small", 96), ("medium", 176), ("large", 800))
            },
        }
    ]


def validate_name(name: Any) -> str:
    if (
        not isinstance(name, str)
        or not name.strip()
        or name in (".", "..")
        or re.search(r'["*:<>?/\\|]', name)
        or name.endswith((".", " "))
    ):
        raise common.graph_error(400, "invalidRequest", "Invalid item name.")
    return name


async def changed(ctx: ProviderContext, item: dict[str, Any], change: str = "updated") -> None:
    item["_version"] += 1
    item["eTag"] = f'"{{{stable_guid(item["id"]).upper()}}},{item["_version"]}"'
    item["cTag"] = f'"c:{{{stable_guid(item["id"]).upper()}}},{item["_version"]}"'
    item["lastModifiedDateTime"] = isoformat_z(ctx.clock.now())
    item["fileSystemInfo"]["lastModifiedDateTime"] = item["lastModifiedDateTime"]
    sequence = ctx.store.meta.get("ms_drive_sequence", 0) + 1
    ctx.store.meta["ms_drive_sequence"] = sequence
    ctx.store["ms_drive_changes"].put(
        {"id": str(sequence), "drive": item["_drive"], "item": copy.deepcopy(item)}
    )
    resource = f"drives/{item['_drive']}/root"
    before = {d["id"] for d in ctx.webhooks.deliveries}
    await common.publish_change(ctx, resource, "updated", public(item))
    # Root notifications describe the root itself. Older bus listeners match only
    # descendants; fill in undelivered subscriptions without duplicating newer listeners.
    drive = ctx.store[DRIVES].get(item["_drive"])
    aliases = {resource, f"users/{common.user_id(ctx.world, drive['_owner'])}/drive/root"}
    for subscription in ctx.store["subscriptions"]:
        if (
            subscription.get("_resource") not in aliases
            or "updated" not in subscription.get("changeType", "").split(",")
            or parse_instant(subscription["expirationDateTime"]) <= ctx.clock.now()
        ):
            continue
        if any(
            d["id"] not in before and d.get("metadata", {}).get("subscriptionId") == subscription["id"]
            for d in ctx.webhooks.deliveries
        ):
            continue
        body = {
            "value": [
                {
                    "subscriptionId": subscription["id"],
                    "subscriptionExpirationDateTime": subscription["expirationDateTime"],
                    "changeType": "updated",
                    "resource": subscription["_resource"],
                    "resourceData": {
                        "@odata.type": "#microsoft.graph.driveItem",
                        "@odata.id": subscription["_resource"],
                        "id": drive["_root"],
                    },
                    "clientState": subscription.get("clientState"),
                    "tenantId": common.tenant_id(ctx.world),
                }
            ]
        }
        await ctx.webhooks.deliver(
            subscription["notificationUrl"],
            json.dumps(body).encode(),
            event_type="updated",
            metadata={"subscriptionId": subscription["id"]},
        )


def delta_token(
    ctx: ProviderContext,
    resource: str,
    principal: common.Principal,
    records: list[dict[str, Any]],
    sequence: int,
    offset: int = 0,
) -> str:
    token = secrets.token_urlsafe(32)
    ctx.store["ms_files_cursors"].put(
        {
            "id": token,
            "resource": resource,
            "user": principal.user_id,
            "records": records,
            "sequence": sequence,
            "offset": offset,
        }
    )
    return token
