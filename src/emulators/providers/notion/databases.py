"""Versioned database containers, data sources, schema updates, and queries."""

from __future__ import annotations

import copy
import json
from typing import Any

from fastapi import FastAPI, Request

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext
from emulators.providers.notion.auth import capability
from emulators.providers.notion.common import (
    NEW,
    OLD,
    allowed,
    ancestors,
    authenticate,
    body,
    envelope,
    fail,
    invalid,
    paginate,
    policy,
    require,
    rich_text,
    serialize,
    touch,
    version,
)
from emulators.providers.notion.events import emit
from emulators.providers.notion.properties import (
    attach_uploads,
    attachment,
    check_upload_access,
    computed,
    property_value,
    schema,
)
from emulators.providers.notion.query import compile_filter, evaluate, sorted_rows
from emulators.providers.notion.validation import archive_value, editable


def normalize_relations(ctx: ProviderContext, grant: Grant, specs: dict[str, Any]) -> None:
    for prop in specs.values():
        if prop["type"] != "relation":
            continue
        config = prop["relation"]
        if "database_id" in config:
            db = require(ctx, grant, "databases", config.pop("database_id"))
            if len(db["_sources"]) != 1:
                invalid("Specify a data_source_id for a multi-source database.")
            config["data_source_id"] = db["_sources"][0]
        if "data_source_id" not in config:
            invalid("A relation requires a target database_id or data_source_id.")
        require(ctx, grant, "data_sources", config["data_source_id"])


def update_schema(ctx: ProviderContext, source: dict[str, Any], raw: Any, grant: Grant) -> None:
    specs = schema(raw, source["id"], source["properties"])
    normalize_relations(ctx, grant, specs)
    for page in ctx.store["pages"]:
        if page["parent"].get("data_source_id") != source["id"]:
            continue
        by_id = {p["id"]: p for p in page["properties"].values()}
        page["properties"] = {
            name: (
                by_id[spec["id"]]
                if spec["id"] in by_id and by_id[spec["id"]]["type"] == spec["type"]
                else property_value(ctx, spec, None)
            )
            for name, spec in specs.items()
        }
        computed(page, specs)
    source["properties"] = specs
    touch(ctx, source, grant)


def schema_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    previous = {p["id"]: p for p in before.values()}
    current = {p["id"]: p for p in after.values()}
    changes = []
    for property_id in previous.keys() | current.keys():
        if previous.get(property_id) == current.get(property_id):
            continue
        prop = current.get(property_id) or previous[property_id]
        action = (
            "deleted"
            if property_id not in current
            else "created"
            if property_id not in previous
            else "updated"
        )
        changes.append({"id": property_id, "name": prop["name"], "action": action})
    return sorted(changes, key=lambda p: p["id"])


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.post("/v1/databases")
    async def create(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "insert_content")
        payload = await body(request)
        raw_parent = payload.get("parent", {})
        if not isinstance(raw_parent, dict):
            invalid("parent must be an object containing page_id.")
        parent = require(ctx, grant, "pages", raw_parent.get("page_id"), write=True)
        editable(ctx, parent)
        check_upload_access(ctx, payload, grant.client_id)
        db_id, source_id = ctx.ids.guid(), ctx.ids.guid()
        initial = payload.get("initial_data_source", {}) if version(request) == NEW else payload
        if not isinstance(initial, dict):
            invalid("initial_data_source must be an object.")
        if not isinstance(payload.get("is_inline", False), bool):
            invalid("is_inline must be boolean.")
        if version(request) == NEW and "properties" in payload:
            invalid("Database properties belong in initial_data_source for version 2025-09-03.")
        specs = schema(initial.get("properties", {"Name": {"title": {}}}), source_id)
        normalize_relations(ctx, grant, specs)
        db = {
            **envelope(db_id, "database", {"type": "page_id", "page_id": parent["id"]}, ctx.clock.now()),
            "title": rich_text(payload.get("title", []), ctx),
            "description": rich_text(payload.get("description", []), ctx),
            "icon": attachment(ctx, payload.get("icon"), icon=True),
            "cover": attachment(ctx, payload.get("cover")),
            "is_inline": payload.get("is_inline", False),
            "url": "",
            "public_url": None,
            "_sources": [source_id],
            **policy(parent),
        }
        source = {
            **envelope(
                source_id, "data_source", {"type": "database_id", "database_id": db_id}, ctx.clock.now()
            ),
            "database_parent": db["parent"],
            "title": rich_text(initial.get("title", payload.get("title", [])), ctx),
            "description": [],
            "icon": db["icon"],
            "properties": specs,
            **policy(parent),
        }
        ctx.store["databases"].put(db)
        ctx.store["data_sources"].put(source)
        attach_uploads(ctx, db)
        parent["_children"].append(db_id)
        touch(ctx, parent, grant)
        return serialize(request, ctx, db, grant)

    @app.get("/v1/databases/{database_id}")
    async def retrieve(database_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "read_content")
        return serialize(request, ctx, require(ctx, grant, "databases", database_id), grant)

    @app.patch("/v1/databases/{database_id}")
    async def update(database_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "update_content")
        current = require(ctx, grant, "databases", database_id, write=True)
        if version(request) == OLD and len(current["_sources"]) != 1:
            invalid("Use the 2025-09-03 API for a database with multiple data sources.")
        record = copy.deepcopy(current)
        payload = await body(request)
        archived = archive_value(payload)
        editable(ctx, current, payload)
        check_upload_access(ctx, payload, grant.client_id)
        for field in ("title", "description"):
            if field in payload:
                record[field] = rich_text(payload[field], ctx)
        for field in ("icon", "cover"):
            if field in payload:
                record[field] = attachment(ctx, payload[field], icon=field == "icon")
        if "is_inline" in payload:
            if not isinstance(payload["is_inline"], bool):
                invalid("is_inline must be boolean.")
            record["is_inline"] = payload["is_inline"]
        if archived is not None:
            record.update(archived=archived, in_trash=archived)
        changes = []
        if "properties" in payload:
            if version(request) == NEW:
                invalid("Update data source properties through /v1/data_sources/{id}.")
            if len(record["_sources"]) != 1:
                invalid("Use the 2025-09-03 API for a database with multiple data sources.")
            source = ctx.store["data_sources"].get(record["_sources"][0])
            before = copy.deepcopy(source["properties"])
            update_schema(ctx, source, payload["properties"], grant)
            changes = schema_changes(before, source["properties"])
        touch(ctx, record, grant)
        ctx.store["databases"].put(record)
        attach_uploads(ctx, record)
        await emit(
            ctx,
            "database.schema_updated",
            record["id"],
            "database",
            {"updated_properties": changes},
        )
        return serialize(request, ctx, record, grant)

    async def query_source(request: Request, source: dict[str, Any], grant: Grant) -> dict[str, Any]:
        payload = await body(request)
        specs = source["properties"]
        expression = compile_filter(payload.get("filter"), specs)
        rows = []
        for record in ctx.store["pages"]:
            if (
                record["parent"].get("data_source_id") != source["id"]
                or any(r.get("archived") for r in ancestors(ctx, record))
                or not allowed(ctx, grant, record)
            ):
                continue
            computed(record, specs)
            query_record = record
            if any(prop["type"] == "relation" for prop in record["properties"].values()):
                query_record = copy.deepcopy(record)
                for prop in query_record["properties"].values():
                    if prop["type"] == "relation":
                        prop["relation"] = [
                            value
                            for value in prop["relation"]
                            if (target := ctx.store["pages"].get(value["id"])) and allowed(ctx, grant, target)
                        ]
            if evaluate(expression, query_record, ctx.clock.now()):
                rows.append(record)
        rows = sorted_rows(rows, payload.get("sorts", []), specs)
        binding = json.dumps(
            {"source": source["id"], "filter": payload.get("filter"), "sorts": payload.get("sorts", [])},
            sort_keys=True,
        )
        list_kind = "page_or_data_source" if version(request) == NEW else "page_or_database"
        result = paginate(ctx, rows, payload, kind=list_kind, key=binding, grant=grant)
        result["results"] = [serialize(request, ctx, r, grant) for r in result["results"]]
        selected = request.query_params.getlist("filter_properties") or payload.get("filter_properties", [])
        if not isinstance(selected, list) or any(not isinstance(v, str) for v in selected):
            invalid("filter_properties must be an array.")
        if selected:
            for record in result["results"]:
                record["properties"] = {
                    n: p for n, p in record["properties"].items() if p["id"] in selected or n in selected
                }
        return result

    @app.post("/v1/databases/{database_id}/query")
    async def query_database(database_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "read_content")
        if version(request) != OLD:
            fail(
                400,
                "invalid_request_url",
                "Database query moved to /v1/data_sources/{data_source_id}/query in version 2025-09-03.",
            )
        db = require(ctx, grant, "databases", database_id)
        if len(db["_sources"]) != 1:
            invalid("Use the 2025-09-03 API for a database with multiple data sources.")
        return await query_source(request, ctx.store["data_sources"].get(db["_sources"][0]), grant)

    def new_version(request: Request) -> None:
        if version(request) != NEW:
            fail(400, "invalid_request_url", "Data sources require Notion-Version 2025-09-03.")

    @app.get("/v1/data_sources/{data_source_id}")
    async def retrieve_source(data_source_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        new_version(request)
        capability(grant, "read_content")
        return serialize(request, ctx, require(ctx, grant, "data_sources", data_source_id), grant)

    @app.post("/v1/data_sources/{data_source_id}/query")
    async def query_data_source(data_source_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        new_version(request)
        capability(grant, "read_content")
        return await query_source(request, require(ctx, grant, "data_sources", data_source_id), grant)

    @app.post("/v1/data_sources")
    async def create_source(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        new_version(request)
        capability(grant, "insert_content")
        payload = await body(request)
        parent = payload.get("parent", {})
        if not isinstance(parent, dict):
            invalid("parent must be an object containing database_id.")
        db = require(ctx, grant, "databases", parent.get("database_id"), write=True)
        editable(ctx, db)
        check_upload_access(ctx, payload, grant.client_id)
        source_id = ctx.ids.guid()
        specs = schema(payload.get("properties", {"Name": {"title": {}}}), source_id)
        normalize_relations(ctx, grant, specs)
        record = {
            **envelope(
                source_id, "data_source", {"type": "database_id", "database_id": db["id"]}, ctx.clock.now()
            ),
            "database_parent": db["parent"],
            "title": rich_text(payload.get("title", []), ctx),
            "description": [],
            "icon": attachment(ctx, payload.get("icon", db["icon"]), icon=True)
            if "icon" in payload
            else copy.deepcopy(db["icon"]),
            "properties": specs,
            **policy(db),
        }
        ctx.store["data_sources"].put(record)
        attach_uploads(ctx, record)
        db["_sources"].append(source_id)
        touch(ctx, db, grant)
        return serialize(request, ctx, record, grant)

    @app.patch("/v1/data_sources/{data_source_id}")
    async def update_source(data_source_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        new_version(request)
        capability(grant, "update_content")
        current = require(ctx, grant, "data_sources", data_source_id, write=True)
        record = copy.deepcopy(current)
        payload = await body(request)
        archived = archive_value(payload)
        editable(ctx, current, payload)
        check_upload_access(ctx, payload, grant.client_id)
        for field in ("title", "description"):
            if field in payload:
                record[field] = rich_text(payload[field], ctx)
        if "icon" in payload:
            record["icon"] = attachment(ctx, payload["icon"], icon=True)
        if "parent" in payload:
            invalid("Moving data sources is not supported by this emulator.")
        if archived is not None:
            record.update(archived=archived, in_trash=archived)
        if "properties" in payload:
            update_schema(ctx, record, payload["properties"], grant)
        touch(ctx, record, grant)
        ctx.store["data_sources"].put(record)
        attach_uploads(ctx, record)
        await emit(
            ctx,
            "data_source.schema_updated",
            record["id"],
            "data_source",
            {"updated_properties": schema_changes(current["properties"], record["properties"])},
        )
        return serialize(request, ctx, record, grant)
