"""CRM v3 objects, histories, batches, and searchable lists."""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.auth import Grant
from emulators.core.clock import parse_instant
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext, external_base

from . import associations, search, webhooks
from .common import (
    ACTIVITIES,
    DEFAULTS,
    body,
    fail,
    fetch,
    flag,
    inputs,
    integer,
    names,
    object_type,
    owner_for,
    query_names,
    require,
    scopes_for,
    stamp,
    visible,
)
from .definitions import deal_calculated


def automatic(kind: str) -> list[str]:
    return [
        "hs_object_id",
        "hs_createdate" if kind in ACTIVITIES else "createdate",
        "lastmodifieddate" if kind == "contacts" else "hs_lastmodifieddate",
    ]


def shape(
    ctx: ProviderContext,
    grant: Grant,
    kind: str,
    record: dict[str, Any],
    properties: list[str] | None = None,
    history: list[str] | None = None,
    links: list[str] | None = None,
) -> dict[str, Any]:
    wanted = list(dict.fromkeys([*(DEFAULTS[kind] if properties is None else properties), *automatic(kind)]))
    result = {k: record[k] for k in ("id", "createdAt", "updatedAt", "archived")}
    result["properties"] = {
        p: record["properties"].get(p) for p in wanted if ctx.store[f"properties:{kind}"].get(p)
    }
    if record.get("archivedAt"):
        result["archivedAt"] = record["archivedAt"]
    if history is not None:
        result["propertiesWithHistory"] = {
            p: copy.deepcopy(record.get("_history", {}).get(p, [])) for p in history
        }
    if links:
        mapped = {}
        for target in links:
            dst = object_type(target)
            rows = associations.related(ctx, grant, kind, record["id"], dst)
            expanded = []
            seen: set[tuple[str, str]] = set()
            for row in rows:
                for t in row["associationTypes"]:
                    definition = ctx.store["_association_types"].get(f"{t['category']}:{t['typeId']}")
                    entry = (str(row["toObjectId"]), definition["name"])
                    if entry not in seen:
                        seen.add(entry)
                        expanded.append({"id": entry[0], "type": entry[1]})
            if expanded:
                mapped[dst] = {"results": expanded}
        if mapped:
            result["associations"] = mapped
    return result


def validate_properties(
    ctx: ProviderContext, kind: str, properties: Any, record_id: str | None = None
) -> dict[str, str]:
    if not isinstance(properties, dict):
        fail(400, "properties must be an object.")
    result = {}
    for name, value in properties.items():
        definition = ctx.store[f"properties:{kind}"].get(name)
        if definition is None or definition.get("archived"):
            fail(
                400,
                f"Property {name} does not exist",
                errors=[
                    {
                        "message": f"Property {name} does not exist",
                        "code": "PROPERTY_DOESNT_EXIST",
                        "context": {"propertyName": [name]},
                    }
                ],
            )
        if definition.get("modificationMetadata", {}).get("readOnlyValue"):
            fail(400, f"Property {name} is read-only.")
        if isinstance(value, (dict, list)):
            fail(400, f"Property {name} must be a scalar value.")
        text = "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
        type_ = definition["type"]
        if text and type_ == "number":
            try:
                if not Decimal(text).is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                fail(400, f"{text} was not a valid number for {name}.")
        if text and type_ in ("date", "datetime"):
            try:
                instant = (
                    datetime.fromtimestamp(int(text) / 1000, tz=UTC)
                    if re.fullmatch(r"-?\d+", text)
                    else parse_instant(text)
                )
                if type_ == "date" and any(
                    (instant.hour, instant.minute, instant.second, instant.microsecond)
                ):
                    fail(400, f"Property {name} must be midnight UTC.")
                text = instant.date().isoformat() if type_ == "date" else stamp(instant)
            except (ValueError, OverflowError, OSError):
                fail(400, f"Invalid date value for {name}.")
        if text and type_ == "enumeration" and definition.get("options"):
            options = [o["value"] for o in definition["options"]]
            values = text.split(";") if definition["fieldType"] == "checkbox" else [text]
            if any(v not in options for v in values):
                fail(400, f"{text} was not one of the allowed options for {name}.")
        if text and type_ == "bool" and text not in ("true", "false"):
            fail(400, f"Invalid boolean value for {name}.")
        if name == "email" and text and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text):
            fail(400, "Invalid email address.")
        if (
            name == "hubspot_owner_id"
            and text
            and (ctx.store["owners"].get(text) is None or ctx.store["owners"].get(text)["archived"])
        ):
            fail(400, "Invalid owner ID.")
        if (
            text
            and definition.get("hasUniqueValue")
            and any(
                r["id"] != record_id
                and not r["archived"]
                and r["properties"].get(name, "").casefold() == text.casefold()
                for r in ctx.store[kind]
            )
        ):
            fail(409, f"An object with this {name} already exists.")
        result[name] = text
    return result


def validate_pipeline(ctx: ProviderContext, kind: str, properties: dict[str, str]) -> None:
    if kind not in ("deals", "tickets"):
        return
    pipe_key, stage_key = (
        ("pipeline", "dealstage") if kind == "deals" else ("hs_pipeline", "hs_pipeline_stage")
    )
    pipeline = ctx.store[f"pipelines:{kind}"].get(properties.get(pipe_key))
    if pipeline is None or properties.get(stage_key) not in [s["id"] for s in pipeline["stages"]]:
        fail(400, "Pipeline or stage is invalid for this object.")


def create_links(
    ctx: ProviderContext, grant: Grant, kind: str, values: Any
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    if not isinstance(values, list):
        fail(400, "associations must be an array.")
    result = []
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("to"), dict):
            fail(400, "Invalid association input.")
        definitions = value.get("types")
        if not isinstance(definitions, list) or not definitions or not isinstance(definitions[0], dict):
            fail(400, "Association types are required.")
        first = definitions[0]
        definition = ctx.store["_association_types"].get(
            f"{first.get('associationCategory')}:{first.get('associationTypeId')}"
        )
        if definition is None or definition["from"] != kind:
            fail(400, "Invalid association type or direction.")
        dst = definition["to"]
        parsed = associations.parse_types(ctx, kind, dst, definitions)
        did = str(value["to"].get("id", ""))
        fetch(ctx, grant, dst, did)
        result.append((dst, did, parsed))
    return result


def write(
    ctx: ProviderContext,
    grant: Grant,
    kind: str,
    payload: dict[str, Any],
    existing: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    properties = validate_properties(
        ctx, kind, payload.get("properties"), existing["id"] if existing else None
    )
    owner = owner_for(ctx, grant.user_id)
    if not grant.app_only and owner and owner.get("_owned_only"):
        properties.setdefault(
            "hubspot_owner_id",
            owner["id"] if existing is None else existing["properties"].get("hubspot_owner_id", ""),
        )
        if properties["hubspot_owner_id"] != owner["id"]:
            fail(403, "The user can only write objects they own.", category="FORBIDDEN")
    combined = {**(existing["properties"] if existing else {}), **properties}
    if existing is None:
        # HubSpot uses the default pipeline when none is specified.
        if kind == "deals" and not combined.get("pipeline"):
            combined["pipeline"] = properties["pipeline"] = "default"
        if kind == "tickets" and not combined.get("hs_pipeline"):
            combined["hs_pipeline"] = properties["hs_pipeline"] = "0"
        required = {
            "deals": ["dealname", "dealstage"],
            "tickets": ["subject", "hs_pipeline_stage"],
            "products": ["name"],
            "line_items": ["name"],
        }.get(kind, [])
        if (
            any(not combined.get(p) for p in required)
            or (kind == "companies" and not any(combined.get(p) for p in ("name", "domain")))
            or (kind == "contacts" and not any(combined.get(p) for p in ("email", "firstname", "lastname")))
        ):
            fail(400, "Required properties were not set.")
    validate_pipeline(ctx, kind, combined)
    if kind == "deals":
        combined.update(deal_calculated(ctx, combined.get("pipeline", ""), combined.get("dealstage", "")))
    links = create_links(ctx, grant, kind, payload.get("associations", []))
    now = stamp(ctx.clock.now())
    if existing is None and kind in ACTIVITIES and not combined.get("hs_timestamp"):
        combined["hs_timestamp"] = now
    if existing is None:
        with ctx.store.lock:
            counter = ctx.store.meta.get("object_counter", 900_000_000_000_000) + 1
            ctx.store.meta["object_counter"] = counter
        rid = str(counter)
        auto = {
            "hs_object_id": rid,
            "createdate": now,
            "hs_createdate": now,
            "hs_lastmodifieddate": now,
            "lastmodifieddate": now,
        }
        record = {
            "id": rid,
            "properties": {**combined, **auto},
            "createdAt": now,
            "updatedAt": now,
            "archived": False,
            "_history": {},
        }
        effective = dict(combined)
    else:
        record = existing
        effective = {
            name: combined[name]
            for name in combined
            if name in properties or name in ("hs_deal_stage_probability", "hs_is_closed", "hs_is_closed_won")
        }
        effective = {
            name: value for name, value in effective.items() if record["properties"].get(name) != value
        }
        if not effective and not links:
            return record, {}
        if effective:
            record["properties"].update(effective)
            record["properties"].update(hs_lastmodifieddate=now, lastmodifieddate=now)
            record["updatedAt"] = now
    changed = {**effective, **{p: record["properties"][p] for p in automatic(kind)}} if effective else {}
    for prop, value in changed.items():
        if existing and prop in ("hs_object_id", "createdate", "hs_createdate"):
            continue
        record["_history"].setdefault(prop, []).append(
            {
                "value": value,
                "timestamp": now,
                "sourceType": "API",
                "sourceId": grant.client_id,
                "updatedByUserId": owner["userId"] if owner else 0,
            }
        )
    ctx.store[kind].put(record)
    for dst, did, definitions in links:
        associations.add(ctx, kind, record["id"], dst, did, definitions)
    return record, effective


def paging(request: Request, kind: str, cursor: str) -> dict[str, Any]:
    query = [(k, v) for k, v in request.query_params.multi_items() if k != "after"] + [("after", cursor)]
    return {
        "next": {
            "after": cursor,
            "link": external_base(request) + f"/crm/v3/objects/{kind}?" + urlencode(query),
        }
    }


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/crm/v3/objects/{objectType}")
    @app.get("/crm/v3/objects/{objectType}/", include_in_schema=False)
    async def list_objects(request: Request, objectType: str) -> dict[str, Any]:
        kind = object_type(objectType)
        grant = require(request, ctx, scopes_for(kind))
        q = request.query_params
        limit = integer(q.get("limit"), 10, 100)
        after = integer(q.get("after"), 0, 2**63 - 1, minimum=0)
        archived = flag(q.get("archived"))
        rows = sorted(
            [
                r
                for r in ctx.store[kind]
                if r["archived"] == archived and int(r["id"]) > after and visible(ctx, grant, kind, r)
            ],
            key=lambda r: int(r["id"]),
        )
        result: dict[str, Any] = {
            "results": [
                shape(
                    ctx,
                    grant,
                    kind,
                    r,
                    query_names(request, "properties"),
                    query_names(request, "propertiesWithHistory"),
                    query_names(request, "associations"),
                )
                for r in rows[:limit]
            ]
        }
        if len(rows) > limit:
            result["paging"] = paging(request, objectType, rows[limit - 1]["id"])
        return result

    @app.post("/crm/v3/objects/{objectType}/search")
    async def search_objects(request: Request, objectType: str) -> dict[str, Any]:
        kind = object_type(objectType)
        grant = require(request, ctx, scopes_for(kind))
        payload = await body(request)
        if len((await request.body()).decode("utf-8-sig")) > 3000:
            fail(400, "Search request body must not exceed 3,000 characters.")
        limit = integer(payload.get("limit"), 10, 200)
        after = integer(payload.get("after"), 0, 9999, minimum=0)
        rows = sorted(
            [r for r in ctx.store[kind] if not r["archived"] and visible(ctx, grant, kind, r)],
            key=lambda r: int(r["id"]),
        )
        rows = search.apply(ctx, kind, rows, payload)
        result: dict[str, Any] = {
            "total": min(len(rows), 10000),
            "results": [
                shape(ctx, grant, kind, r, names(payload["properties"]) if "properties" in payload else None)
                for r in rows[after : min(after + limit, 10000)]
            ],
        }
        if after + limit < min(len(rows), 10000):
            result["paging"] = {"next": {"after": str(after + limit)}}
        return result

    @app.post("/crm/v3/objects/{objectType}/batch/{operation}")
    async def batch(request: Request, objectType: str, operation: str) -> Response:
        kind = object_type(objectType)
        if operation not in ("read", "create", "update", "archive", "upsert"):
            fail(404, "Unknown batch operation.")
        grant = require(request, ctx, scopes_for(kind, "read" if operation == "read" else "write"))
        payload = await body(request)
        items = inputs(payload)
        started = stamp(ctx.clock.now())
        results, errors, changes = [], [], []
        created_ids: list[str] = []
        # Multi-status batch create is opt-in: every input needs a unique trace id.
        trace_ids = [item.get("objectWriteTraceId") for item in items]
        multi_status = operation != "create" or (
            all(isinstance(t, str) and t for t in trace_ids) and len(set(trace_ids)) == len(trace_ids)
        )
        for item in items:
            try:
                if operation in ("read", "update", "archive"):
                    record = fetch(
                        ctx,
                        grant,
                        kind,
                        str(item.get("id", "")),
                        item.get("idProperty", payload.get("idProperty")),
                        flag(request.query_params.get("archived")) if operation == "read" else False,
                    )
                elif operation == "upsert":
                    id_property = item.get("idProperty") or "hs_object_id"
                    if not isinstance(id_property, str) or not item.get("id"):
                        fail(400, "Upsert id is required.")
                    definition = ctx.store[f"properties:{kind}"].get(id_property)
                    if not definition or not definition.get("hasUniqueValue"):
                        fail(400, "Upsert requires a unique idProperty on each input.")
                    if id_property == "hs_object_id":
                        record = ctx.store[kind].get(str(item["id"]))
                        if record is not None and (
                            record["archived"] or not visible(ctx, grant, kind, record)
                        ):
                            record = None
                        if record is None:
                            fail(404, f"Object not found. objectId={item['id']}")
                    else:
                        record = next(
                            (
                                r
                                for r in ctx.store[kind]
                                if not r["archived"]
                                and r["properties"].get(id_property, "").casefold()
                                == str(item["id"]).casefold()
                            ),
                            None,
                        )
                        if record and not visible(ctx, grant, kind, record):
                            fail(404, "Object not found.")
                        if isinstance(item.get("properties"), dict):
                            supplied = item["properties"].get(id_property)
                            if (
                                supplied is not None
                                and str(supplied).casefold() != str(item["id"]).casefold()
                            ):
                                fail(400, "Upsert id does not match the idProperty value.")
                            item = {
                                **item,
                                "properties": {**item["properties"], id_property: str(item["id"])},
                            }
                else:
                    record = None
                if operation == "archive":
                    record.update(archived=True, archivedAt=stamp(ctx.clock.now()))
                    changes.append((kind, record["id"], "deletion", {}))
                elif operation == "read":
                    results.append(
                        shape(
                            ctx,
                            grant,
                            kind,
                            record,
                            names(payload["properties"]) if "properties" in payload else None,
                            names(payload["propertiesWithHistory"])
                            if "propertiesWithHistory" in payload
                            else None,
                        )
                    )
                else:
                    new = record is None
                    record, props = write(ctx, grant, kind, item, record)
                    rendered = shape(ctx, grant, kind, record, list(record["properties"]))
                    if operation == "upsert":
                        rendered["new"] = new
                    if "objectWriteTraceId" in item:
                        rendered["objectWriteTraceId"] = item["objectWriteTraceId"]
                    results.append(rendered)
                    if new:
                        created_ids.append(record["id"])
                    changes.append((kind, record["id"], "creation" if new else "propertyChange", props))
            except EmulatorError as exc:
                if not multi_status:
                    for created_id in created_ids:
                        ctx.store[kind].delete(created_id)
                        for edge in list(ctx.store["associations"]):
                            if (edge["from"] == kind and edge["fromId"] == created_id) or (
                                edge["to"] == kind and edge["toId"] == created_id
                            ):
                                ctx.store["associations"].delete(edge["id"])
                    raise
                errors.append(
                    {
                        **exc.body,
                        "context": {
                            "ids": [str(item.get("id", ""))],
                            **(
                                {"objectWriteTraceId": [str(item["objectWriteTraceId"])]}
                                if "objectWriteTraceId" in item
                                else {}
                            ),
                        },
                    }
                )
        await webhooks.emit(ctx, changes)
        if operation == "archive" and not errors:
            return Response(status_code=204)
        response = {
            "status": "COMPLETE",
            "results": results,
            "startedAt": started,
            "completedAt": stamp(ctx.clock.now()),
        }
        if errors:
            response.update(errors=errors, numErrors=len(errors))
        return JSONResponse(response, status_code=207 if errors else 201 if operation == "create" else 200)

    @app.get("/crm/v3/objects/{objectType}/{record_id}")
    async def get_object(request: Request, objectType: str, record_id: str) -> dict[str, Any]:
        kind = object_type(objectType)
        grant = require(request, ctx, scopes_for(kind))
        record = fetch(
            ctx,
            grant,
            kind,
            record_id,
            request.query_params.get("idProperty"),
            flag(request.query_params.get("archived")),
        )
        return shape(
            ctx,
            grant,
            kind,
            record,
            query_names(request, "properties"),
            query_names(request, "propertiesWithHistory"),
            query_names(request, "associations"),
        )

    @app.post("/crm/v3/objects/{objectType}", status_code=201)
    async def create_object(request: Request, objectType: str) -> dict[str, Any]:
        kind = object_type(objectType)
        grant = require(request, ctx, scopes_for(kind, "write"))
        record, props = write(ctx, grant, kind, await body(request))
        await webhooks.emit(ctx, [(kind, record["id"], "creation", props)])
        return shape(ctx, grant, kind, record, list(record["properties"]))

    @app.patch("/crm/v3/objects/{objectType}/{record_id}")
    async def update_object(request: Request, objectType: str, record_id: str) -> dict[str, Any]:
        kind = object_type(objectType)
        grant = require(request, ctx, scopes_for(kind, "write"))
        record = fetch(ctx, grant, kind, record_id, request.query_params.get("idProperty"))
        record, props = write(ctx, grant, kind, await body(request), record)
        await webhooks.emit(ctx, [(kind, record["id"], "propertyChange", props)])
        return shape(ctx, grant, kind, record, list(record["properties"]))

    @app.delete("/crm/v3/objects/{objectType}/{record_id}", status_code=204)
    async def archive_object(request: Request, objectType: str, record_id: str) -> Response:
        kind = object_type(objectType)
        grant = require(request, ctx, scopes_for(kind, "write"))
        record = fetch(ctx, grant, kind, record_id)
        record.update(archived=True, archivedAt=stamp(ctx.clock.now()))
        await webhooks.emit(ctx, [(kind, record_id, "deletion", {})])
        return Response(status_code=204)
