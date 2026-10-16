"""Page lifecycle and paginated property-item reads."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import quote, unquote, urlencode

from fastapi import FastAPI, Request

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext, external_base
from emulators.providers.notion.auth import capability
from emulators.providers.notion.blocks import append
from emulators.providers.notion.common import (
    NEW,
    allowed,
    ancestors,
    authenticate,
    body,
    envelope,
    fail,
    invalid,
    paginate,
    policy,
    public,
    require,
    serialize,
    touch,
    version,
    world_user,
)
from emulators.providers.notion.events import emit
from emulators.providers.notion.properties import attachment, check_upload_access, computed, values

TITLE_SCHEMA = {"title": {"id": "title", "name": "title", "type": "title", "title": {}}}


def page_schema(ctx: ProviderContext, page: dict[str, Any]) -> dict[str, Any]:
    parent = page["parent"]
    if parent["type"] == "data_source_id":
        return ctx.store["data_sources"].get(parent["data_source_id"])["properties"]
    return TITLE_SCHEMA


def parent_for(
    ctx: ProviderContext, grant: Grant, raw: Any, request: Request
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(raw, dict):
        invalid("parent must be an object.")
    kind = raw.get("type") or next(
        (k for k in ("page_id", "database_id", "data_source_id", "workspace") if k in raw), ""
    )
    if kind == "workspace" and raw.get("workspace") is True:
        if grant.app_only:
            fail(
                403,
                "restricted_resource",
                "Internal integrations must create a page under a shared page or database.",
            )
        return (
            {"type": "workspace", "workspace": True},
            {"_acl": {world_user(ctx, grant.user_id): "owner"}, "_internal": False},
            TITLE_SCHEMA,
        )
    if kind == "page_id":
        parent = require(ctx, grant, "pages", raw.get(kind), write=True)
        if any(r.get("archived") for r in ancestors(ctx, parent)):
            fail(409, "conflict_error", "Cannot create content under an archived page.")
        return {"type": "page_id", "page_id": parent["id"]}, parent, TITLE_SCHEMA
    if kind == "database_id":
        if version(request) == NEW:
            invalid("Use a data_source_id parent with Notion-Version 2025-09-03.")
        db = require(ctx, grant, "databases", raw.get(kind), write=True)
        if len(db["_sources"]) != 1:
            invalid("This database has multiple data sources. Use the 2025-09-03 API.")
        source = require(ctx, grant, "data_sources", db["_sources"][0], write=True)
    elif kind == "data_source_id":
        if version(request) != NEW:
            invalid("data_source_id requires Notion-Version 2025-09-03.")
        source = require(ctx, grant, "data_sources", raw.get(kind), write=True)
    else:
        invalid("parent must specify page_id, database_id, data_source_id, or workspace.")
    if any(r.get("archived") for r in ancestors(ctx, source)):
        fail(409, "conflict_error", "Cannot create a page under an archived database.")
    return (
        {
            "type": "data_source_id",
            "data_source_id": source["id"],
            "database_id": source["parent"]["database_id"],
        },
        source,
        source["properties"],
    )


def validate_relations(
    ctx: ProviderContext, grant: Grant, specs: dict[str, Any], props: dict[str, Any]
) -> None:
    for name, value in props.items():
        if value["type"] != "relation":
            continue
        config = specs[name]["relation"]
        for relation in value["relation"]:
            target = require(ctx, grant, "pages", relation["id"])
            if (
                config.get("data_source_id")
                and target["parent"].get("data_source_id") != config["data_source_id"]
            ):
                invalid("Related page does not belong to the relation's target data source.")


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.post("/v1/pages")
    async def create(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "insert_content")
        payload = await body(request)
        parent, access, specs = parent_for(ctx, grant, payload.get("parent"), request)
        check_upload_access(ctx, payload, grant.client_id)
        properties = values(ctx, specs, payload.get("properties", {}))
        validate_relations(ctx, grant, specs, properties)
        record = {
            **envelope(ctx.ids.guid(), "page", parent, ctx.clock.now()),
            "properties": properties,
            "icon": attachment(ctx, payload.get("icon")),
            "cover": attachment(ctx, payload.get("cover")),
            "url": "",
            "public_url": None,
            "_children": [],
            **policy(access),
        }
        computed(record, specs)
        if payload.get("children"):
            append(ctx, record, payload["children"], grant)
        ctx.store["pages"].put(record)
        if parent["type"] == "page_id":
            access["_children"].append(record["id"])
            touch(ctx, access, grant)
        await emit(ctx, "page.created", record["id"])
        return serialize(request, ctx, record, grant)

    @app.get("/v1/pages/{page_id}")
    async def retrieve(page_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "read_content")
        record = require(ctx, grant, "pages", page_id)
        computed(record, page_schema(ctx, record))
        result = serialize(request, ctx, record, grant)
        selected = request.query_params.getlist("filter_properties")
        if selected:
            result["properties"] = {
                n: p for n, p in result["properties"].items() if n in selected or p["id"] in selected
            }
        return result

    @app.patch("/v1/pages/{page_id}")
    async def update(page_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "update_content")
        current = require(ctx, grant, "pages", page_id, write=True)
        record = copy.deepcopy(current)
        payload = await body(request)
        if "parent" in payload:
            invalid("A page's parent cannot be changed through this endpoint.")
        check_upload_access(ctx, payload, grant.client_id)
        specs = page_schema(ctx, record)
        if "properties" in payload:
            record["properties"] = values(ctx, specs, payload["properties"], record["properties"])
            validate_relations(ctx, grant, specs, record["properties"])
        for key in ("icon", "cover"):
            if key in payload:
                record[key] = attachment(ctx, payload[key])
        if "archived" in payload or "in_trash" in payload:
            archived = payload.get("in_trash", payload.get("archived"))
            if not isinstance(archived, bool):
                invalid("archived/in_trash must be a boolean.")
            record.update(archived=archived, in_trash=archived)
        touch(ctx, record, grant)
        computed(record, specs)
        ctx.store["pages"].put(record)
        await emit(
            ctx,
            "page.properties_updated",
            record["id"],
            data={"updated_properties": list(payload.get("properties", {}))},
        )
        return serialize(request, ctx, record, grant)

    @app.get("/v1/pages/{page_id}/properties/{property_id}")
    async def property_item(page_id: str, property_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "read_content")
        record = require(ctx, grant, "pages", page_id)
        computed(record, page_schema(ctx, record))
        prop = next(
            (
                p
                for n, p in record["properties"].items()
                if n == property_id or unquote(p["id"]) == unquote(property_id)
            ),
            None,
        )
        if prop is None:
            fail(404, "object_not_found", f"Could not find property with ID: {property_id}.")
        kind = prop["type"]
        if kind not in ("title", "rich_text", "people", "relation"):
            return serialize(request, ctx, {"object": "property_item", **prop}, grant)
        items = prop[kind]
        if kind == "relation":
            items = [
                v
                for v in items
                if (target := ctx.store["pages"].get(v["id"])) and allowed(ctx, grant, target)
            ]
        rows = [
            {"object": "property_item", "id": prop["id"], "type": kind, kind: public(value)}
            for value in items
        ]
        result = paginate(
            ctx,
            rows,
            request.query_params,
            kind="property_item",
            key=f"property:{record['id']}:{prop['id']}",
            grant=grant,
        )
        result["results"] = [serialize(request, ctx, item, grant) for item in result["results"]]
        next_url = None
        if result["has_more"]:
            next_url = (
                external_base(request)
                + f"/v1/pages/{record['id']}/properties/{quote(prop['id'], safe='')}?"
                + urlencode(
                    {
                        "start_cursor": result["next_cursor"],
                        "page_size": request.query_params.get("page_size", 100),
                    }
                )
            )
        result["property_item"] = {"id": prop["id"], "next_url": next_url, "type": kind, kind: {}}
        return result
