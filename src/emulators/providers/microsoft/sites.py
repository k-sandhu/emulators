"""SharePoint site discovery and document-library/custom-list projections."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_guid
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common
from emulators.providers.microsoft import drive_model as dm

SITES = "ms_sites"
LISTS = "ms_lists"
LIST_ITEMS = "ms_list_items"
COLUMNS = "ms_list_columns"


def seed(ctx: ProviderContext) -> None:
    root_id = dm.site_id(ctx)
    ctx.store[SITES].put(
        {
            "id": root_id,
            "name": ctx.world.org.slug,
            "displayName": ctx.world.org.name,
            "webUrl": "https://" + dm.HOST,
            "createdDateTime": isoformat_z(ctx.world.anchor),
            "lastModifiedDateTime": isoformat_z(ctx.world.anchor),
            "description": "Halcyon Advisory Group intranet",
            "siteCollection": {"hostname": dm.HOST},
            "root": {},
            "_space": None,
        }
    )
    for space in ctx.world.spaces:
        if space.kind != "team":
            continue
        sid = dm.site_id(ctx, space.id)
        site = ctx.store[SITES].put(
            {
                "id": sid,
                "name": space.slug,
                "displayName": space.name,
                "webUrl": f"https://{dm.HOST}/sites/{space.slug}",
                "description": space.description,
                "createdDateTime": isoformat_z(ctx.world.anchor),
                "lastModifiedDateTime": isoformat_z(ctx.world.anchor),
                "siteCollection": {"hostname": dm.HOST},
                "_space": space.id,
            }
        )
        lid = stable_guid(ctx.world.seed, "list", space.id, "documents")
        ctx.store[LISTS].put(
            {
                "id": lid,
                "name": "Documents",
                "displayName": "Documents",
                "description": space.description,
                "webUrl": site["webUrl"] + "/Shared%20Documents/Forms/AllItems.aspx",
                "createdDateTime": site["createdDateTime"],
                "lastModifiedDateTime": site["lastModifiedDateTime"],
                "list": {"contentTypesEnabled": False, "hidden": False, "template": "documentLibrary"},
                "_site": sid,
                "_drive": dm.drive_id(ctx, space.id),
            }
        )
        for index, item in enumerate(
            (i for i in ctx.store[dm.ITEMS].where(_drive=dm.drive_id(ctx, space.id)) if "root" not in i), 1
        ):
            item["_list_item"] = str(index)
        add_columns(
            ctx,
            lid,
            ["Title", "FileLeafRef", "FileRef", "Created", "Modified", "AuthorLookupId", "EditorLookupId"],
        )
        if space.id == "clients":
            lid = stable_guid(ctx.world.seed, "list", space.id, "engagement-tracker")
            ctx.store[LISTS].put(
                {
                    "id": lid,
                    "name": "EngagementTracker",
                    "displayName": "Engagement Tracker",
                    "description": "Client engagement delivery tracker",
                    "webUrl": site["webUrl"] + "/Lists/EngagementTracker/AllItems.aspx",
                    "createdDateTime": site["createdDateTime"],
                    "lastModifiedDateTime": site["lastModifiedDateTime"],
                    "list": {"contentTypesEnabled": False, "hidden": False, "template": "genericList"},
                    "_site": sid,
                }
            )
            add_columns(
                ctx,
                lid,
                ["Title", "Client", "Service", "Status", "StartDate", "EndDate", "BudgetHours", "Lead"],
            )
            for index, engagement in enumerate(ctx.world.engagements, 1):
                ctx.store[LIST_ITEMS].put_as(
                    f"{lid}/{index}",
                    {
                        "id": str(index),
                        "_list": lid,
                        "createdDateTime": site["createdDateTime"],
                        "lastModifiedDateTime": site["lastModifiedDateTime"],
                        "eTag": f'"{stable_guid(lid, index)},1"',
                        "webUrl": site["webUrl"] + f"/Lists/EngagementTracker/DispForm.aspx?ID={index}",
                        "fields": {
                            "id": str(index),
                            "Title": engagement.name,
                            "Client": ctx.world.client(engagement.client_id).name,
                            "Service": engagement.service,
                            "Status": engagement.status,
                            "StartDate": engagement.start.isoformat() + "T00:00:00Z",
                            "EndDate": engagement.end.isoformat() + "T00:00:00Z",
                            "BudgetHours": engagement.budget_hours,
                            "Lead": ctx.world.person(engagement.lead_id).display_name,
                        },
                    },
                )


def add_columns(ctx: ProviderContext, lid: str, names: list[str]) -> None:
    for name in names:
        record: dict[str, Any] = {
            "id": stable_guid(lid, "column", name),
            "name": name,
            "displayName": name,
            "columnGroup": "Custom Columns",
            "description": "",
            "hidden": False,
            "readOnly": name in ("Created", "Modified", "FileRef"),
            "required": name == "Title",
            "indexed": False,
            "_list": lid,
        }
        if name == "BudgetHours":
            record["number"] = {"decimalPlaces": "none", "displayAs": "number"}
        elif name in ("StartDate", "EndDate", "Created", "Modified"):
            record["dateTime"] = {"displayAs": "default", "format": "dateTime"}
        else:
            record["text"] = {"allowMultipleLines": False, "maxLength": 255}
        ctx.store[COLUMNS].put(record)


def require_site(
    ctx: ProviderContext, sid: str, p: common.Principal, *, write: bool = False
) -> dict[str, Any]:
    site = ctx.store[SITES].get(dm.site_id(ctx) if sid in ("root", dm.HOST) else sid)
    if site is None:
        raise common.item_not_found()
    if site["_space"]:
        drive = dm.drive_for_site(ctx, site["id"])
        dm.require_item(ctx, drive["id"], drive["_root"], p, write=write)
    elif p.delegated and (not p.person_id or ctx.world.person(p.person_id).is_external):
        raise common.item_not_found()
    elif write and p.delegated:
        raise common.access_denied()
    return site


def visible(ctx: ProviderContext, site: dict[str, Any], p: common.Principal) -> bool:
    if p.app_only:
        return True
    if not site["_space"]:
        return bool(p.person_id and not ctx.world.person(p.person_id).is_external)
    drive = dm.drive_for_site(ctx, site["id"])
    return dm.role_for(ctx, dm.root_item(ctx, drive), p) is not None


def library_items(ctx: ProviderContext, listing: dict[str, Any], p: common.Principal) -> list[dict[str, Any]]:
    records = []
    for item in ctx.store[dm.ITEMS].where(_drive=listing["_drive"]):
        if "root" in item or item.get("_deleted") or not dm.role_for(ctx, item, p):
            continue
        if "_list_item" not in item:
            item["_list_item"] = str(
                max(
                    (
                        int(i.get("_list_item", "0"))
                        for i in ctx.store[dm.ITEMS].where(_drive=listing["_drive"])
                    ),
                    default=0,
                )
                + 1
            )
        records.append(
            {
                "id": item["_list_item"],
                "createdDateTime": item["createdDateTime"],
                "lastModifiedDateTime": item["lastModifiedDateTime"],
                "createdBy": item["createdBy"],
                "lastModifiedBy": item["lastModifiedBy"],
                "webUrl": item["webUrl"],
                "eTag": item["eTag"],
                "_drive_item": item["id"],
                "fields": {
                    "id": item["_list_item"],
                    "Title": item.get("_fields", {}).get("Title", item["name"]),
                    "FileLeafRef": item["name"],
                    "FileRef": "/sites/"
                    + ctx.world.space(ctx.store[dm.DRIVES].get(item["_drive"])["_space"]).slug
                    + "/Shared Documents/"
                    + item["_path"],
                    "Created": item["createdDateTime"],
                    "Modified": item["lastModifiedDateTime"],
                    **item.get("_fields", {}),
                },
            }
        )
    return records


def list_wire(request: Request, ctx: ProviderContext, item: dict[str, Any]) -> dict[str, Any]:
    record = dm.public(item)
    expand = request.query_params.get("$expand", request.query_params.get("expand", ""))
    if "fields" not in expand:
        record.pop("fields", None)
    elif match := re.search(r"fields\(\$?select=([^)]*)\)", expand):
        names = {n.strip() for n in match.group(1).split(",")} | {"id"}
        record["fields"] = {k: v for k, v in record["fields"].items() if k in names}
    if "driveItem" in expand and item.get("_drive_item"):
        record["driveItem"] = dm.wire_item(request, ctx, ctx.store[dm.ITEMS].get(item["_drive_item"]))
    return record


async def list_dispatch(
    request: Request, ctx: ProviderContext, site: dict[str, Any], tail: str, p: common.Principal
) -> Any:
    parts = tail.strip("/").split("/")
    listings = ctx.store[LISTS].where(_site=site["id"])
    if not tail:
        return dm.collection(request, listings, context="Collection(list)")
    listing = next((r for r in listings if r["id"] == parts[0] or r["name"] == parts[0]), None)
    if listing is None:
        raise common.item_not_found()
    if len(parts) == 1 and request.method == "GET":
        record = dm.public(listing)
        if "columns" in request.query_params.get("$expand", ""):
            record["columns"] = [dm.public(c) for c in ctx.store[COLUMNS].where(_list=listing["id"])]
        return dm.single(request, record, "list")
    if len(parts) > 1 and parts[1] == "columns" and request.method == "GET":
        columns = ctx.store[COLUMNS].where(_list=listing["id"])
        if len(parts) == 2:
            return dm.collection(request, columns, context="Collection(columnDefinition)")
        column = next((c for c in columns if c["id"] == parts[2] or c["name"] == parts[2]), None)
        if column:
            return dm.single(request, column, "columnDefinition")
        raise common.item_not_found()
    if len(parts) < 2 or parts[1] != "items":
        raise common.item_not_found()
    records = (
        library_items(ctx, listing, p)
        if listing.get("_drive")
        else ctx.store[LIST_ITEMS].where(_list=listing["id"])
    )
    if len(parts) == 2:
        if request.method == "GET":
            return dm.collection(
                request,
                [list_wire(request, ctx, r) for r in records],
                context="Collection(listItem)",
                search_fields=("fields/Title",),
            )
        if request.method == "POST":
            require_site(ctx, site["id"], p, write=True)
            if listing.get("_drive"):
                raise common.graph_error(
                    400, "invalidRequest", "Use the drive API to create document-library items."
                )
            payload = await dm.json_body(request)
            fields = payload.get("fields")
            if not isinstance(fields, dict) or not fields.get("Title"):
                raise common.graph_error(400, "invalidRequest", "fields.Title is required.")
            validate_fields(ctx, listing, fields)
            iid = str(max((int(r["id"]) for r in records), default=0) + 1)
            timestamp = isoformat_z(ctx.clock.now())
            item = {
                "id": iid,
                "_list": listing["id"],
                "createdDateTime": timestamp,
                "lastModifiedDateTime": timestamp,
                "fields": {"id": iid, **fields},
                "eTag": f'"{stable_guid(listing["id"], iid)},1"',
                "webUrl": listing["webUrl"].replace("AllItems.aspx", f"DispForm.aspx?ID={iid}"),
            }
            ctx.store[LIST_ITEMS].put_as(listing["id"] + "/" + iid, item)
            await common.publish_change(
                ctx, f"sites/{site['id']}/lists/{listing['id']}", "updated", dm.public(item)
            )
            return JSONResponse(dm.public(item), status_code=201)
    if len(parts) < 3:
        raise common.graph_error(405, "methodNotAllowed", "The HTTP method is not allowed on this resource.")
    item = next((r for r in records if r["id"] == parts[2]), None)
    if item is None:
        raise common.item_not_found()
    op = "/".join(parts[3:])
    if request.method == "GET":
        if op == "fields":
            return common.single_response(request, item["fields"], context="fieldValueSet")
        if op == "driveItem" and item.get("_drive_item"):
            return dm.single(
                request, dm.wire_item(request, ctx, ctx.store[dm.ITEMS].get(item["_drive_item"])), "driveItem"
            )
        if not op:
            return dm.single(request, list_wire(request, ctx, item), "listItem")
    if request.method == "PATCH" and op in ("", "fields"):
        require_site(ctx, site["id"], p, write=True)
        payload = await dm.json_body(request)
        fields = payload if op else payload.get("fields", {})
        validate_fields(ctx, listing, fields)
        if request.headers.get("if-match") not in (None, "*", item["eTag"]):
            raise common.graph_error(412, "preconditionFailed", "The item has changed.")
        item["fields"].update(fields)
        item["lastModifiedDateTime"] = isoformat_z(ctx.clock.now())
        if listing.get("_drive"):
            source = dm.require_item(ctx, listing["_drive"], item["_drive_item"], p, write=True)
            source.setdefault("_fields", {}).update(fields)
            await dm.changed(ctx, source)
        else:
            revision = int(item["eTag"].rstrip('"').rsplit(",", 1)[1]) + 1
            item["eTag"] = f'"{stable_guid(listing["id"], item["id"])},{revision}"'
            await common.publish_change(
                ctx, f"sites/{site['id']}/lists/{listing['id']}", "updated", dm.public(item)
            )
        return item["fields"] if op else dm.public(item)
    if request.method == "DELETE" and not op:
        require_site(ctx, site["id"], p, write=True)
        if listing.get("_drive"):
            from emulators.providers.microsoft.drive import delete_item

            await delete_item(ctx, ctx.store[dm.ITEMS].get(item["_drive_item"]), p)
        else:
            ctx.store[LIST_ITEMS].delete(listing["id"] + "/" + item["id"])
        return Response(status_code=204)
    raise common.graph_error(405, "methodNotAllowed", "The HTTP method is not allowed on this resource.")


def validate_fields(ctx: ProviderContext, listing: dict[str, Any], fields: Any) -> None:
    if not isinstance(fields, dict):
        raise common.graph_error(400, "invalidRequest", "fields must be an object.")
    columns = {c["name"]: c for c in ctx.store[COLUMNS].where(_list=listing["id"])}
    for name, value in fields.items():
        if name not in columns or columns[name]["readOnly"] or name in ("FileLeafRef", "id"):
            raise common.graph_error(400, "invalidRequest", f"Field '{name}' is not writable.")
        if "number" in columns[name] and (not isinstance(value, (float, int)) or isinstance(value, bool)):
            raise common.graph_error(400, "invalidRequest", f"Field '{name}' must be numeric.")


def register(app: FastAPI, ctx: ProviderContext) -> None:
    def router() -> APIRouter:
        routes = APIRouter()

        @routes.get("/groups/{gid}/sites/root")
        async def group_site(request: Request, gid: str) -> dict[str, Any]:
            p = common.principal(request, ctx)
            drive = dm.drive_for_group(ctx, gid)
            return dm.single(request, require_site(ctx, drive["_site"], p), "site")

        @routes.get("/sites")
        async def sites(request: Request) -> dict[str, Any]:
            p = common.principal(request, ctx)
            search = request.query_params.get("search", "*").casefold()
            records = [
                s
                for s in ctx.store[SITES]
                if visible(ctx, s, p)
                and (search == "*" or search in (s["displayName"] + " " + s["name"]).casefold())
            ]
            return dm.collection(request, records, context="Collection(site)")

        @routes.api_route("/sites/{tail:path}", methods=dm.METHODS)
        async def site_route(request: Request, tail: str) -> Any:
            p = common.principal(request, ctx)
            if ":" in tail:
                match = re.match(r"^([^:]+):/(?:sites/)?([^/:]*):?(?:/(.*))?$", tail)
                if not match or match.group(1).casefold() != dm.HOST:
                    raise common.item_not_found()
                slug = match.group(2)
                site = (
                    next((s for s in ctx.store[SITES] if s["name"].casefold() == slug.casefold()), None)
                    if slug
                    else ctx.store[SITES].get(dm.site_id(ctx))
                )
                if not site:
                    raise common.item_not_found()
                sid, op = site["id"], match.group(3) or ""
            else:
                sid, _, op = tail.partition("/")
            site = require_site(ctx, sid, p)
            if not op and request.method == "GET":
                return dm.single(request, site, "site")
            if op == "sites" and request.method == "GET":
                return dm.collection(request, [], context="Collection(site)")
            if op == "drives" and request.method == "GET":
                records = [
                    d for d in ctx.store[dm.DRIVES].where(_site=site["id"]) if dm.can_see_drive(ctx, d, p)
                ]
                return dm.collection(request, records, context="Collection(drive)")
            if op == "permissions" and request.method == "GET":
                if not p.app_only:
                    raise common.access_denied("This API requires application permissions.")
                drive = dm.drive_for_site(ctx, site["id"])
                return dm.collection(
                    request, dm.permissions(ctx, dm.root_item(ctx, drive)), context="Collection(permission)"
                )
            if op == "lists" or op.startswith("lists/"):
                return await list_dispatch(request, ctx, site, op.removeprefix("lists").lstrip("/"), p)
            raise common.item_not_found()

        return routes

    for version in common.GRAPH_VERSIONS:
        app.include_router(router(), prefix="/" + version, tags=["SharePoint sites / lists"])
