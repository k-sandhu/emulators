"""OneDrive and SharePoint document libraries through Graph's drive addressing forms."""

from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from emulators.core.clock import parse_instant
from emulators.core.provider import ProviderContext, external_base, url_for_host
from emulators.providers.microsoft import common, drive_permissions, drive_upload
from emulators.providers.microsoft import drive_model as dm
from emulators.world import documents

seed = dm.seed


def resolve_address(
    ctx: ProviderContext, drive: dict[str, Any], tail: str, p: common.Principal
) -> tuple[dict[str, Any], str]:
    match = re.match(r"^(root|items/[^/:]+):/(.*)$", tail)
    if match:
        base, addressed = match.groups()
        path, colon, operation = addressed.partition(":")
        item = dm.root_item(ctx, drive) if base == "root" else ctx.store[dm.ITEMS].get(base.split("/", 1)[1])
        if item is None or item["_drive"] != drive["id"] or item.get("_deleted"):
            raise common.item_not_found()
        # A direct share can be addressed without granting access to every ancestor.
        for segment in path.strip("/").split("/") if path.strip("/") else []:
            item = dm.child_named(ctx, item, segment)
            if item is None:
                raise common.item_not_found()
        item = dm.require_item(ctx, drive["id"], item["id"], p)
        return item, operation.lstrip("/") if colon else ""
    parts = tail.strip("/").split("/")
    if parts[0] in ("root", "special"):
        if parts[0] == "special":
            if len(parts) < 2 or parts[1].lower() not in ("documents", "approot"):
                raise common.item_not_found()
            if parts[1].lower() == "documents":
                item = dm.root_item(ctx, drive)
            else:
                parent = dm.require_item(ctx, drive["id"], drive["_root"], p, write=True)
                item = dm.ensure_folder(ctx, drive, "Apps/" + re.sub(r"[^\w.-]", "_", p.client_id))
                if parent is None:
                    raise common.item_not_found()
            operation = "/".join(parts[2:])
        else:
            item, operation = dm.root_item(ctx, drive), "/".join(parts[1:])
    elif len(parts) >= 2 and parts[0] == "items":
        item, operation = ctx.store[dm.ITEMS].get(parts[1]), "/".join(parts[2:])
    else:
        raise common.item_not_found()
    if item is None:
        raise common.item_not_found()
    return dm.require_item(ctx, drive["id"], item["id"], p), operation


def upload_address(
    ctx: ProviderContext, drive: dict[str, Any], tail: str, p: common.Principal
) -> tuple[dict[str, Any], str] | None:
    match = re.fullmatch(r"(root|items/[^/:]+):/(.+):/(content|createUploadSession)", tail)
    if not match:
        return None
    base, path, _ = match.groups()
    folder, _, name = path.rpartition("/")
    parent_tail = f"{base}:/{folder}:" if folder else base
    parent, _ = resolve_address(ctx, drive, parent_tail, p)
    return dm.require_item(ctx, drive["id"], parent["id"], p, write=True), dm.validate_name(name)


async def delete_item(ctx: ProviderContext, item: dict[str, Any], p: common.Principal) -> None:
    dm.require_item(ctx, item["_drive"], item["id"], p, write=True)
    if "root" in item:
        raise common.graph_error(400, "invalidRequest", "The root item cannot be deleted.")
    descendants = [
        i
        for i in ctx.store[dm.ITEMS].where(_drive=item["_drive"])
        if i["id"] == item["id"] or i["_path"].startswith(item["_path"] + "/")
    ]
    if any(dm.role_for(ctx, child, p) not in ("write", "owner") for child in descendants):
        raise common.access_denied()
    parent = ctx.store[dm.ITEMS].get(item["parentReference"]["id"])
    parent["folder"]["childCount"] = max(0, parent["folder"]["childCount"] - 1)
    for child in descendants:
        child["_deleted"] = True
        child["deleted"] = {"state": "deleted"}
        await dm.changed(ctx, child, "deleted")


async def rename_move(
    request: Request, ctx: ProviderContext, drive: dict[str, Any], item: dict[str, Any], p: common.Principal
) -> dict[str, Any]:
    dm.require_item(ctx, drive["id"], item["id"], p, write=True)
    drive_upload.precondition(request, item)
    if "root" in item:
        raise common.graph_error(400, "invalidRequest", "The root cannot be renamed or moved.")
    payload = await dm.json_body(request)
    if set(payload) - {"name", "parentReference", "description"}:
        raise common.graph_error(400, "invalidRequest", "One or more properties cannot be updated.")
    if "description" in payload and payload["description"] is not None and not isinstance(payload["description"], str):
        raise common.graph_error(400, "invalidRequest", "description must be a string or null.")
    if "name" not in payload and "parentReference" not in payload:
        for key in ("description",):
            if key in payload:
                item[key] = payload[key]
        item["lastModifiedBy"] = common.identity(ctx.world, ctx.world.person(p.person_id or drive["_owner"]))
        await dm.changed(ctx, item)
        return dm.wire_item(request, ctx, item)
    name = dm.validate_name(payload.get("name", item["name"]))
    ref = payload.get("parentReference", {})
    if not isinstance(ref, dict):
        raise common.graph_error(400, "invalidRequest", "parentReference must be an object.")
    if ref.get("driveId", drive["id"]) != drive["id"]:
        raise common.graph_error(400, "invalidRequest", "Items cannot be moved between drives.")
    parent = dm.require_item(ctx, drive["id"], ref.get("id", item["parentReference"]["id"]), p, write=True)
    if (
        "folder" not in parent
        or parent["id"] == item["id"]
        or parent["_path"].startswith(item["_path"] + "/")
    ):
        raise common.graph_error(400, "invalidRequest", "Invalid destination parent.")
    existing = dm.child_named(ctx, parent, name)
    if existing and existing["id"] != item["id"]:
        raise common.graph_error(409, "nameAlreadyExists", "An item with that name already exists.")
    old_parent = ctx.store[dm.ITEMS].get(item["parentReference"]["id"])
    if old_parent["id"] != parent["id"]:
        old_parent["folder"]["childCount"] -= 1
        parent["folder"]["childCount"] += 1
    old_path = item["_path"]
    item["name"] = name
    item["_path"] = (parent["_path"] + "/" if parent["_path"] else "") + name
    item["parentReference"].update(
        id=parent["id"],
        name=parent["name"],
        path="/drive/root:" + ("/" + parent["_path"] if parent["_path"] else ""),
    )
    item["webUrl"] = drive["webUrl"] + "/" + dm.quote(item["_path"])
    item["lastModifiedBy"] = common.identity(ctx.world, ctx.world.person(p.person_id or drive["_owner"]))
    if "description" in payload:
        item["description"] = payload["description"]
    for child in ctx.store[dm.ITEMS].where(_drive=drive["id"]):
        if child["id"] != item["id"] and child["_path"].startswith(old_path + "/"):
            child["_path"] = item["_path"] + child["_path"][len(old_path) :]
            child["webUrl"] = drive["webUrl"] + "/" + dm.quote(child["_path"])
            parent_path = child["_path"].rsplit("/", 1)[0]
            child["parentReference"]["path"] = "/drive/root:" + ("/" + parent_path if parent_path else "")
            if child["parentReference"].get("id") == item["id"]:
                child["parentReference"]["name"] = name
            await dm.changed(ctx, child)
    await dm.changed(ctx, item)
    return dm.wire_item(request, ctx, item)


def delta(
    request: Request, ctx: ProviderContext, drive: dict[str, Any], p: common.Principal
) -> dict[str, Any]:
    token = request.query_params.get("token")
    sequence = ctx.store.meta.get("ms_drive_sequence", 0)
    resource = "drive/" + drive["id"]
    records: list[dict[str, Any]] = []
    if token and token != "latest":
        cursor = ctx.store["ms_files_cursors"].get(token)
        if not cursor or cursor["resource"] != resource or cursor["user"] != p.user_id:
            raise common.graph_error(
                410, "resyncRequired", "The delta token is no longer valid. Resynchronize the drive."
            )
        if cursor["records"]:
            records, sequence = cursor["records"][cursor["offset"] :], cursor["sequence"]
        else:
            latest: dict[str, dict[str, Any]] = {}
            for change in ctx.store["ms_drive_changes"]:
                if change["drive"] == drive["id"] and int(change["id"]) > cursor["sequence"]:
                    latest[change["item"]["id"]] = change["item"]
            records = list(latest.values())
    elif token != "latest":
        records = [
            copy.deepcopy(i) for i in ctx.store[dm.ITEMS].where(_drive=drive["id"]) if not i.get("_deleted")
        ]
    records = [i for i in records if dm.role_for(ctx, i, p)]
    options = common.query_options(request)
    size = min(options.top or 200, 200)
    page = records[:size]
    result: dict[str, Any] = {
        "@odata.context": common.context_url(request, "Collection(driveItem)"),
        "value": [],
    }
    for item in page:
        if item.get("_deleted"):
            result["value"].append(
                {
                    "id": item["id"],
                    "deleted": {"state": "deleted"},
                    "parentReference": item["parentReference"],
                }
            )
        else:
            result["value"].append(
                common.single_response(request, dm.wire_item(request, ctx, item), context="driveItem")
            )
            result["value"][-1].pop("@odata.context", None)
    more = len(records) > size
    next_token = dm.delta_token(ctx, resource, p, records if more else [], sequence, size if more else 0)
    params = {k: v for k, v in request.query_params.items() if k != "token"}
    params["token"] = next_token
    result["@odata.nextLink" if more else "@odata.deltaLink"] = (
        external_base(request) + dm.path_of(request) + "?" + urlencode(params)
    )
    return result


async def copy_item(
    request: Request, ctx: ProviderContext, item: dict[str, Any], p: common.Principal
) -> Response:
    payload = await dm.json_body(request)
    ref = payload.get("parentReference", {})
    if not isinstance(ref, dict):
        raise common.graph_error(400, "invalidRequest", "parentReference must be an object.")
    drive = ctx.store[dm.DRIVES].get(ref.get("driveId", item["_drive"]))
    if drive is None:
        raise common.item_not_found()
    parent = dm.require_item(ctx, drive["id"], ref.get("id", drive["_root"]), p, write=True)
    if "folder" not in parent or (
        drive["id"] == item["_drive"]
        and (parent["id"] == item["id"] or parent["_path"].startswith(item["_path"] + "/"))
    ):
        raise common.graph_error(400, "invalidRequest", "Invalid copy destination.")
    name, existing = drive_upload.conflict(
        ctx,
        parent,
        dm.validate_name(payload.get("name", item["name"])),
        request.query_params.get("@microsoft.graph.conflictBehavior", "fail"),
    )
    if existing:
        await delete_item(ctx, existing, p)

    async def duplicate(source: dict[str, Any], dest: dict[str, Any], new_name: str) -> dict[str, Any]:
        dm.require_item(ctx, source["_drive"], source["id"], p)
        clone = dm.new_item(
            ctx,
            drive,
            dest,
            new_name,
            key=ctx.ids.guid(),
            owner=p.person_id or drive["_owner"],
            folder="folder" in source,
        )
        if "file" in source:
            data, _ = dm.content(ctx, source)
            clone["_blob"] = ctx.store.blobs.put(data)
            clone["file"] = copy.deepcopy(source["file"])
            clone["size"] = len(data)
        else:
            for child in list(dm.children(ctx, source, p)):
                await duplicate(child, clone, child["name"])
        await dm.changed(ctx, clone, "created")
        return clone

    copied = await duplicate(item, parent, name)
    monitor = ctx.ids.urlsafe(32)
    ctx.store["ms_drive_operations"].put(
        {
            "id": monitor,
            "status": "completed",
            "percentageComplete": 100.0,
            "resourceId": copied["id"],
            "resourceLocation": dm.graph_url(
                request, ctx, f"/{common.version_of(request)}/drives/{drive['id']}/items/{copied['id']}"
            ),
        }
    )
    return Response(
        status_code=202,
        headers={
            "Location": url_for_host(request, ctx, dm.HOST, "/_api/v2.0/operations/" + monitor),
            "Retry-After": "1",
        },
    )


async def dispatch(request: Request, ctx: ProviderContext, drive: dict[str, Any], tail: str = "") -> Any:
    p = common.principal(request, ctx)
    if not dm.can_see_drive(ctx, drive, p):
        raise common.item_not_found()
    if not tail:
        if request.method != "GET":
            raise common.graph_error(405, "methodNotAllowed", "This resource is read only.")
        return dm.single(request, drive, "drive")
    target = upload_address(ctx, drive, tail, p) if request.method in ("PUT", "POST") else None
    if target:
        parent, name = target
        if tail.endswith("createUploadSession") and request.method == "POST":
            return await drive_upload.create_session(request, ctx, drive, parent, name, p)
        if tail.endswith("content") and request.method == "PUT":
            existing = dm.child_named(ctx, parent, name)
            if existing:
                drive_upload.precondition(request, existing)
            item, status = await drive_upload.put_content(
                ctx,
                drive,
                parent,
                name,
                await request.body(),
                p,
                behavior=request.query_params.get("@microsoft.graph.conflictBehavior", "replace"),
            )
            return JSONResponse(dm.wire_item(request, ctx, item), status_code=status)
    if tail in ("recent", "sharedWithMe"):
        if request.method != "GET":
            raise common.graph_error(405, "methodNotAllowed", "This resource is read only.")
        me = common.me_user_id(p)
        items = [
            i for i in ctx.store[dm.ITEMS] if "file" in i and not i.get("_deleted") and dm.role_for(ctx, i, p)
        ]
        if tail == "recent":
            items.sort(key=lambda i: i["lastModifiedDateTime"], reverse=True)
            records = [dm.wire_item(request, ctx, i) for i in items]
        else:
            records = []
            for item in items:
                if common.user_id(ctx.world, item["_owner"]) != me and any(
                    perm.get("_item") == item["id"]
                    and p.person_id
                    in [
                        pid
                        for target in perm.get("_principals", [])
                        for pid in ctx.world.expand_principal(target)
                    ]
                    for perm in dm.permissions(ctx, item)
                ):
                    records.append(
                        {
                            "id": item["id"],
                            "name": item["name"],
                            "remoteItem": dm.wire_item(request, ctx, item),
                        }
                    )
        return dm.collection(
            request, records, context="Collection(driveItem)", default_page_size=200, search_fields=("name",)
        )
    if tail in ("root/delta", "delta") and request.method == "GET":
        return delta(request, ctx, drive, p)
    search = re.fullmatch(r"(?:root|items/([^/]+))/search\(q='((?:[^']|'')*)'\)", tail)
    if search and request.method == "GET":
        anchor = dm.require_item(ctx, drive["id"], search.group(1) or drive["_root"], p)
        term = search.group(2).replace("''", "'").casefold()
        items = [
            i
            for i in ctx.store[dm.ITEMS].where(_drive=drive["id"])
            if not i.get("_deleted")
            and dm.role_for(ctx, i, p)
            and (not anchor["_path"] or i["_path"].startswith(anchor["_path"] + "/"))
            and term
            in (
                i["name"]
                + " "
                + i.get("description", "")
                + " "
                + (" ".join(ctx.world.document(i["_doc"]).paragraphs) if i.get("_doc") else "")
            ).casefold()
        ]
        return dm.collection(
            request,
            [dm.wire_item(request, ctx, i) for i in items],
            context="Collection(driveItem)",
            default_page_size=200,
            search_fields=("name",),
        )
    item, op = resolve_address(ctx, drive, tail, p)
    if not op:
        if request.method == "GET":
            return dm.single(request, dm.wire_item(request, ctx, item), "driveItem")
        if request.method == "PATCH":
            return await rename_move(request, ctx, drive, item, p)
        if request.method == "DELETE":
            drive_upload.precondition(request, item)
            await delete_item(ctx, item, p)
            return Response(status_code=204)
    if op == "children":
        if "folder" not in item:
            raise common.graph_error(400, "invalidRequest", "The item is not a folder.")
        if request.method == "GET":
            return dm.collection(
                request,
                [dm.wire_item(request, ctx, i) for i in dm.children(ctx, item, p)],
                context="Collection(driveItem)",
                default_page_size=200,
                search_fields=("name",),
            )
        if request.method == "POST":
            dm.require_item(ctx, drive["id"], item["id"], p, write=True)
            payload = await dm.json_body(request)
            if not isinstance(payload.get("folder"), dict):
                raise common.graph_error(400, "invalidRequest", "A folder facet is required.")
            behavior = payload.get("@microsoft.graph.conflictBehavior", "fail")
            name = dm.validate_name(payload.get("name"))
            old = dm.child_named(ctx, item, name)
            if old and behavior == "replace":
                if "folder" in old:
                    return JSONResponse(dm.wire_item(request, ctx, old), status_code=200)
                await delete_item(ctx, old, p)
            else:
                name, _ = drive_upload.conflict(ctx, item, name, behavior)
            new = dm.new_item(
                ctx, drive, item, name, key=ctx.ids.guid(), owner=p.person_id or drive["_owner"], folder=True
            )
            await dm.changed(ctx, new, "created")
            return JSONResponse(dm.wire_item(request, ctx, new), status_code=201)
    if op == "content":
        if request.method == "GET":
            dm.content(ctx, item, request.query_params.get("format"))
            return RedirectResponse(
                dm.download_url(request, ctx, item, request.query_params.get("format")), status_code=302
            )
        if request.method == "PUT":
            drive_upload.precondition(request, item)
            dm.require_item(ctx, drive["id"], item["id"], p, write=True)
            parent = ctx.store[dm.ITEMS].get(item["parentReference"].get("id"))
            if parent is None:
                raise common.graph_error(400, "invalidRequest", "Cannot replace root content.")
            updated, status = await drive_upload.put_content(
                ctx, drive, parent, item["name"], await request.body(), p, existing=item
            )
            return JSONResponse(dm.wire_item(request, ctx, updated), status_code=status)
    if op == "createUploadSession" and request.method == "POST":
        parent = ctx.store[dm.ITEMS].get(item["parentReference"].get("id"))
        if parent is None or "file" not in item:
            raise common.graph_error(400, "invalidRequest", "A file item is required.")
        return await drive_upload.create_session(request, ctx, drive, parent, item["name"], p, item)
    if op.split("/", 1)[0] in ("permissions", "invite", "createLink"):
        return await drive_permissions.handle(request, ctx, item, op, p)
    if op == "copy" and request.method == "POST":
        return await copy_item(request, ctx, item, p)
    if op == "versions" and request.method == "GET":
        dm.content(ctx, item)
        versions = [
            {
                "id": str(n) + ".0",
                "lastModifiedDateTime": item["lastModifiedDateTime"],
                "lastModifiedBy": item["lastModifiedBy"],
                "size": item["size"],
                "publication": {"level": "published", "versionId": f"{n}.0"},
            }
            for n in range(item["_version"], 0, -1)
        ]
        for version in versions:
            prior = ctx.store["ms_drive_versions"].get(item["id"] + ":" + version["id"])
            if prior:
                version.update({k: prior[k] for k in ("size", "lastModifiedDateTime", "lastModifiedBy")})
        return dm.collection(request, versions, context="Collection(driveItemVersion)")
    if op.startswith("versions/") and request.method == "GET":
        parts = op.split("/")
        version = ctx.store["ms_drive_versions"].get(item["id"] + ":" + parts[1])
        if version is None:
            raise common.item_not_found()
        if len(parts) == 3 and parts[2] == "content":
            return dm.binary_response(request, ctx.store.blobs.get(version["blob"]), item["file"]["mimeType"])
        return {
            "id": version["version"],
            **{k: version[k] for k in ("size", "lastModifiedDateTime", "lastModifiedBy")},
        }
    if op.startswith("thumbnails") and request.method == "GET":
        if "file" not in item:
            return {"value": []}
        if op.endswith("/content"):
            key = item.get("_thumbnail")
            if key is None:
                data, _ = documents.render("png", item["name"], [])
                key = item["_thumbnail"] = ctx.store.blobs.put(data)
            return dm.binary_response(request, ctx.store.blobs.get(key), "image/png")
        return dm.collection(request, dm.thumbnails(request, ctx, item), context="Collection(thumbnailSet)")
    raise common.graph_error(405, "methodNotAllowed", "The HTTP method is not allowed on this resource.")


def register(app: FastAPI, ctx: ProviderContext) -> None:
    drive_upload.register(app, ctx)

    @app.get("/_api/v2.0/download")
    async def download(request: Request) -> Response:
        ticket = ctx.store["ms_drive_downloads"].get(request.query_params.get("authkey"))
        if not ticket or parse_instant(ticket["expires"]) <= ctx.clock.now():
            raise common.graph_error(401, "unauthenticated", "The download URL has expired or is invalid.")
        item = ctx.store[dm.ITEMS].get(ticket["item"])
        if item is None or item.get("_deleted"):
            raise common.item_not_found()
        data, mime = dm.content(ctx, item, ticket.get("format"))
        return dm.binary_response(request, data, mime, item["eTag"])

    @app.get("/_api/v2.0/operations/{operation_id}")
    async def operation(operation_id: str) -> dict[str, Any]:
        record = ctx.store["ms_drive_operations"].get(operation_id)
        if record is None:
            raise common.item_not_found()
        return dm.public(record)

    def router() -> APIRouter:
        routes = APIRouter()

        def resolve_drive(request: Request, kind: str, identifier: str = "") -> dict[str, Any]:
            p = common.principal(request, ctx)
            if kind == "me":
                common.me_user_id(p)
                drive = dm.personal_drive(ctx, p.person_id)
            elif kind == "users":
                drive = dm.personal_drive(ctx, dm.person_id(ctx, identifier))
            elif kind == "groups":
                drive = dm.drive_for_group(ctx, identifier)
            elif kind == "sites":
                drive = dm.drive_for_site(ctx, identifier)
            else:
                drive = ctx.store[dm.DRIVES].get(identifier)
            if drive is None:
                raise common.item_not_found()
            return drive

        @routes.get("/me/drives")
        async def me_drives(request: Request) -> dict[str, Any]:
            p = common.principal(request, ctx)
            common.me_user_id(p)
            return dm.collection(request, [dm.personal_drive(ctx, p.person_id)], context="Collection(drive)")

        @routes.get("/drives")
        async def drives(request: Request) -> dict[str, Any]:
            p = common.principal(request, ctx)
            return dm.collection(
                request,
                [d for d in ctx.store[dm.DRIVES] if dm.can_see_drive(ctx, d, p)],
                context="Collection(drive)",
            )

        @routes.api_route("/me/drive", methods=dm.METHODS)
        @routes.api_route("/me/drive/{tail:path}", methods=dm.METHODS)
        async def me_drive(request: Request, tail: str = "") -> Any:
            return await dispatch(request, ctx, resolve_drive(request, "me"), tail)

        @routes.api_route("/drives/{identifier}", methods=dm.METHODS)
        @routes.api_route("/drives/{identifier}/{tail:path}", methods=dm.METHODS)
        async def by_id(request: Request, identifier: str, tail: str = "") -> Any:
            return await dispatch(request, ctx, resolve_drive(request, "drives", identifier), tail)

        @routes.api_route("/users/{identifier}/drive", methods=dm.METHODS)
        @routes.api_route("/users/{identifier}/drive/{tail:path}", methods=dm.METHODS)
        async def user_drive(request: Request, identifier: str, tail: str = "") -> Any:
            return await dispatch(request, ctx, resolve_drive(request, "users", identifier), tail)

        @routes.api_route("/groups/{identifier}/drive", methods=dm.METHODS)
        @routes.api_route("/groups/{identifier}/drive/{tail:path}", methods=dm.METHODS)
        async def group_drive(request: Request, identifier: str, tail: str = "") -> Any:
            return await dispatch(request, ctx, resolve_drive(request, "groups", identifier), tail)

        @routes.api_route("/sites/{identifier}/drive", methods=dm.METHODS)
        @routes.api_route("/sites/{identifier}/drive/{tail:path}", methods=dm.METHODS)
        async def site_drive(request: Request, identifier: str, tail: str = "") -> Any:
            return await dispatch(request, ctx, resolve_drive(request, "sites", identifier), tail)

        return routes

    for version in common.GRAPH_VERSIONS:
        app.include_router(router(), prefix="/" + version, tags=["OneDrive / files"])
