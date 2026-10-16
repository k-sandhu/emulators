"""Directional associations, including primary and unlabeled company relationships."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.auth import Grant
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext, external_base

from .common import body, fail, fetch, inputs, integer, object_type, require, scopes_for, stamp, visible

# from, to, labeled ID, inverse ID, unlabeled ID, inverse unlabeled ID
PAIRS = [
    ("contacts", "companies", 1, 2, 279, 280),
    ("deals", "contacts", 3, 4, 3, 4),
    ("deals", "companies", 5, 6, 341, 342),
    ("deals", "line_items", 19, 20, 19, 20),
    ("tickets", "contacts", 16, 15, 16, 15),
    ("tickets", "companies", 26, 25, 26, 25),
    ("tickets", "deals", 28, 27, 28, 27),
    ("calls", "contacts", 194, 193, 194, 193),
    ("calls", "companies", 182, 181, 182, 181),
    ("calls", "deals", 206, 205, 206, 205),
    ("emails", "contacts", 198, 197, 198, 197),
    ("emails", "companies", 186, 185, 186, 185),
    ("emails", "deals", 210, 209, 210, 209),
    ("meetings", "contacts", 200, 199, 200, 199),
    ("meetings", "companies", 188, 187, 188, 187),
    ("meetings", "deals", 212, 211, 212, 211),
    ("notes", "contacts", 202, 201, 202, 201),
    ("notes", "companies", 190, 189, 190, 189),
    ("notes", "deals", 214, 213, 214, 213),
    ("tasks", "contacts", 204, 203, 204, 203),
    ("tasks", "companies", 192, 191, 192, 191),
    ("tasks", "deals", 216, 215, 216, 215),
]


def seed_types(ctx: ProviderContext) -> None:
    for left, right, forward, reverse, plain, reverse_plain in PAIRS:
        for src, dst, tid, inverse, unlabeled in [
            (left, right, forward, reverse, plain),
            (right, left, reverse, forward, reverse_plain),
        ]:
            label = "Primary" if tid != unlabeled else None
            ctx.store["_association_types"].put(
                {
                    "id": f"HUBSPOT_DEFINED:{tid}",
                    "from": src,
                    "to": dst,
                    "category": "HUBSPOT_DEFINED",
                    "typeId": tid,
                    "label": label,
                    "inverse": inverse,
                    "name": ("company" if src == "companies" else src.removesuffix("s"))
                    + "_to_"
                    + ("company" if dst == "companies" else dst.removesuffix("s")),
                }
            )
            if tid != unlabeled:
                inv_plain = reverse_plain if src == left else plain
                ctx.store["_association_types"].put(
                    {
                        "id": f"HUBSPOT_DEFINED:{unlabeled}",
                        "from": src,
                        "to": dst,
                        "category": "HUBSPOT_DEFINED",
                        "typeId": unlabeled,
                        "label": None,
                        "inverse": inv_plain,
                        # Labeled and unlabeled types share the legacy v3 type name.
                        "name": ("company" if src == "companies" else src.removesuffix("s"))
                        + "_to_"
                        + ("company" if dst == "companies" else dst.removesuffix("s")),
                    }
                )


def types(ctx: ProviderContext, src: str, dst: str) -> list[dict[str, Any]]:
    return [r for r in ctx.store["_association_types"] if r["from"] == src and r["to"] == dst]


def default_type(ctx: ProviderContext, src: str, dst: str, *, primary: bool = False) -> dict[str, Any]:
    found = types(ctx, src, dst)
    if not found:
        fail(400, f"Unsupported association from {src} to {dst}.")
    return next((r for r in found if (r["label"] is not None) == primary), found[0])


def add(
    ctx: ProviderContext, src: str, sid: str, dst: str, did: str, definitions: list[dict[str, Any]]
) -> None:
    for definition in definitions:
        if (
            dst == "companies"
            and definition["category"] == "HUBSPOT_DEFINED"
            and definition["label"] == "Primary"
        ):
            for edge in ctx.store["associations"]:
                if (
                    edge["from"] == src
                    and edge["fromId"] == sid
                    and edge["to"] == dst
                    and edge["toId"] != did
                    and edge["typeId"] == definition["typeId"]
                ):
                    ctx.store["associations"].delete(edge["id"])
                    inverse_key = (
                        f"{dst}:{edge['toId']}:{src}:{sid}:{definition['category']}:{definition['inverse']}"
                    )
                    ctx.store["associations"].delete(inverse_key)
        for left, lid, right, rid, tid in [
            (src, sid, dst, did, definition["typeId"]),
            (dst, did, src, sid, definition["inverse"]),
        ]:
            key = f"{left}:{lid}:{right}:{rid}:{definition['category']}:{tid}"
            ctx.store["associations"].put(
                {
                    "id": key,
                    "from": left,
                    "fromId": lid,
                    "to": right,
                    "toId": rid,
                    "typeId": tid,
                    "category": definition["category"],
                }
            )
        # A primary association also carries the unlabeled relationship.
        if definition["label"] is not None:
            plain = default_type(ctx, src, dst)
            if plain["typeId"] != definition["typeId"]:
                add(ctx, src, sid, dst, did, [plain])


def parse_types(ctx: ProviderContext, src: str, dst: str, values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list) or not values or any(not isinstance(v, dict) for v in values):
        fail(400, "Association types must be a non-empty array.")
    result = []
    for value in values:
        match = next(
            (
                t
                for t in types(ctx, src, dst)
                if str(t["typeId"]) == str(value.get("associationTypeId"))
                and t["category"] == value.get("associationCategory")
            ),
            None,
        )
        if match is None:
            fail(400, "Invalid association type or direction.")
        result.append(match)
    return result


def related(ctx: ProviderContext, grant: Grant, src: str, sid: str, dst: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for edge in ctx.store["associations"]:
        if edge["from"] != src or edge["fromId"] != sid or edge["to"] != dst:
            continue
        record = ctx.store[dst].get(edge["toId"])
        if record is None or record["archived"] or not visible(ctx, grant, dst, record):
            continue
        definition = ctx.store["_association_types"].get(f"{edge['category']}:{edge['typeId']}")
        grouped.setdefault(edge["toId"], []).append(
            {
                "category": edge["category"],
                "typeId": edge["typeId"],
                "label": definition["label"] if definition else None,
            }
        )
    return [
        {"toObjectId": int(rid), "associationTypes": ts}
        for rid, ts in sorted(grouped.items(), key=lambda p: int(p[0]))
    ]


def remove_edges(
    ctx: ProviderContext,
    src: str,
    sid: str,
    dst: str,
    did: str,
    type_ids: set[int] | None = None,
) -> None:
    for edge in list(ctx.store["associations"]):
        if (edge["from"], edge["fromId"], edge["to"], edge["toId"]) not in [
            (src, sid, dst, did),
            (dst, did, src, sid),
        ]:
            continue
        if type_ids is not None:
            forward = edge["from"] == src
            definition = ctx.store["_association_types"].get(f"{edge['category']}:{edge['typeId']}")
            forward_id = edge["typeId"] if forward else (definition["inverse"] if definition else None)
            if forward_id not in type_ids:
                continue
        ctx.store["associations"].delete(edge["id"])


def pair_id(value: Any) -> str:
    if not isinstance(value, dict) or isinstance(value.get("id"), bool) or not str(value.get("id", "")):
        fail(400, "Association inputs require from and to object ids.")
    return str(value["id"])


def error_id(item: dict[str, Any]) -> str:
    source = item.get("from")
    return str(source.get("id", "")) if isinstance(source, dict) else ""


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/crm/v4/associations/{source}/{target}/labels")
    async def labels(request: Request, source: str, target: str) -> dict[str, Any]:
        src, dst = object_type(source), object_type(target)
        require(request, ctx, scopes_for(src))
        return {"results": [{k: t[k] for k in ("category", "typeId", "label")} for t in types(ctx, src, dst)]}

    @app.post("/crm/v4/associations/{source}/{target}/labels")
    async def create_label(request: Request, source: str, target: str) -> dict[str, Any]:
        src, dst = object_type(source), object_type(target)
        require(request, ctx, [f"crm.schemas.{src}.write", f"crm.schemas.{dst}.write"])
        payload = await body(request)
        if any(not isinstance(payload.get(k), str) or not payload[k] for k in ("name", "label")):
            fail(400, "name and label are required.")
        if "inverseLabel" in payload and (
            not isinstance(payload["inverseLabel"], str) or not payload["inverseLabel"]
        ):
            fail(400, "inverseLabel must be a non-empty string.")
        if any(t["name"] == payload["name"] for t in types(ctx, src, dst)):
            fail(409, "An association label with this name already exists.")
        default_type(ctx, src, dst)
        counter = ctx.store.meta.get("association_type_counter", 1000)
        ctx.store.meta["association_type_counter"] = counter + 2
        result = []
        for left, right, tid, inverse, label in [
            (src, dst, counter, counter + 1, payload["label"]),
            (dst, src, counter + 1, counter, payload.get("inverseLabel", payload["label"])),
        ]:
            record = {
                "id": f"USER_DEFINED:{tid}",
                "from": left,
                "to": right,
                "typeId": tid,
                "category": "USER_DEFINED",
                "inverse": inverse,
                "label": label,
                "name": payload["name"],
            }
            ctx.store["_association_types"].put(record)
            result.append({k: record[k] for k in ("category", "typeId", "label")})
        return {"results": result if "inverseLabel" in payload else result[:1]}

    @app.get("/crm/v4/objects/{source}/{sid}/associations/{target}")
    async def list_links(request: Request, source: str, sid: str, target: str) -> dict[str, Any]:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, [*scopes_for(src), *scopes_for(dst)])
        fetch(ctx, grant, src, sid)
        limit = integer(request.query_params.get("limit"), 500, 500)
        after = integer(request.query_params.get("after"), 0, 2**63 - 1, minimum=0)
        rows = [r for r in related(ctx, grant, src, sid, dst) if r["toObjectId"] > after]
        result: dict[str, Any] = {"results": rows[:limit]}
        if len(rows) > limit:
            cursor = str(rows[limit - 1]["toObjectId"])
            result["paging"] = {
                "next": {
                    "after": cursor,
                    "link": external_base(request)
                    + f"/crm/v4/objects/{source}/{sid}/associations/{target}?after={cursor}&limit={limit}",
                }
            }
        return result

    async def connect(
        request: Request, source: str, sid: str, target: str, did: str, plain: bool
    ) -> dict[str, Any]:
        src, dst = object_type(source), object_type(target)
        grant = require(
            request,
            ctx,
            [*scopes_for(src), *scopes_for(src, "write"), *scopes_for(dst), *scopes_for(dst, "write")],
        )
        fetch(ctx, grant, src, sid)
        fetch(ctx, grant, dst, did)
        if plain:
            definitions = [default_type(ctx, src, dst)]
        else:
            try:
                values = await request.json()
            except (ValueError, UnicodeDecodeError):
                fail(400, "Invalid JSON request body.")
            definitions = parse_types(ctx, src, dst, values)
        add(ctx, src, sid, dst, did, definitions)
        if plain:
            now = stamp(ctx.clock.now())
            return {
                "status": "COMPLETE",
                "startedAt": now,
                "completedAt": now,
                "results": [
                    {
                        "from": {"id": sid},
                        "to": {"id": did},
                        "associationSpec": {
                            "associationCategory": definitions[0]["category"],
                            "associationTypeId": definitions[0]["typeId"],
                        },
                    }
                ],
            }
        return {
            "fromObjectTypeId": ctx.store.meta["object_ids"][src],
            "fromObjectId": int(sid),
            "toObjectTypeId": ctx.store.meta["object_ids"][dst],
            "toObjectId": int(did),
            "labels": [d["label"] for d in definitions if d["label"]],
        }

    @app.put("/crm/v4/objects/{source}/{sid}/associations/default/{target}/{did}")
    async def connect_default(
        request: Request, source: str, sid: str, target: str, did: str
    ) -> dict[str, Any]:
        return await connect(request, source, sid, target, did, True)

    @app.put("/crm/v4/objects/{source}/{sid}/associations/{target}/{did}", status_code=201)
    async def connect_labeled(
        request: Request, source: str, sid: str, target: str, did: str
    ) -> dict[str, Any]:
        return await connect(request, source, sid, target, did, False)

    @app.delete("/crm/v4/objects/{source}/{sid}/associations/{target}/{did}", status_code=204)
    async def remove(request: Request, source: str, sid: str, target: str, did: str) -> Response:
        src, dst = object_type(source), object_type(target)
        grant = require(
            request,
            ctx,
            [*scopes_for(src), *scopes_for(src, "write"), *scopes_for(dst), *scopes_for(dst, "write")],
        )
        fetch(ctx, grant, src, sid)
        fetch(ctx, grant, dst, did)
        remove_edges(ctx, src, sid, dst, did)
        return Response(status_code=204)

    @app.post("/crm/v4/associations/{source}/{target}/batch/read")
    async def batch_read(request: Request, source: str, target: str) -> Response:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, [*scopes_for(src), *scopes_for(dst)])
        payload = await body(request)
        records, errors = [], []
        for item in inputs(payload, 1000):
            try:
                records.append(fetch(ctx, grant, src, str(item.get("id", ""))))
            except EmulatorError as exc:
                errors.append({**exc.body, "context": {"ids": [str(item.get("id", ""))]}})
        now = stamp(ctx.clock.now())
        result = {
            "status": "COMPLETE",
            "startedAt": now,
            "completedAt": now,
            "results": [
                {"from": {"id": r["id"]}, "to": related(ctx, grant, src, r["id"], dst)} for r in records
            ],
        }
        if errors:
            result.update(errors=errors, numErrors=len(errors))
        return JSONResponse(result, status_code=207 if errors else 200)

    def write_scopes(src: str, dst: str) -> list[str]:
        return [*scopes_for(src), *scopes_for(src, "write"), *scopes_for(dst), *scopes_for(dst, "write")]

    @app.post("/crm/v4/associations/{source}/{target}/batch/create")
    async def batch_create(request: Request, source: str, target: str) -> Response:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, write_scopes(src, dst))
        payload = await body(request)
        results, errors = [], []
        for item in inputs(payload):
            try:
                sid, did = pair_id(item.get("from")), pair_id(item.get("to"))
                fetch(ctx, grant, src, sid)
                fetch(ctx, grant, dst, did)
                definitions = parse_types(ctx, src, dst, item.get("types"))
                add(ctx, src, sid, dst, did, definitions)
                results.append(
                    {
                        "fromObjectTypeId": ctx.store.meta["object_ids"][src],
                        "fromObjectId": int(sid),
                        "toObjectTypeId": ctx.store.meta["object_ids"][dst],
                        "toObjectId": int(did),
                        "labels": [d["label"] for d in definitions if d["label"]],
                    }
                )
            except EmulatorError as exc:
                errors.append({**exc.body, "context": {"ids": [error_id(item)]}})
        now = stamp(ctx.clock.now())
        result = {"status": "COMPLETE", "startedAt": now, "completedAt": now, "results": results}
        if errors:
            result.update(errors=errors, numErrors=len(errors))
        return JSONResponse(result, status_code=207 if errors else 201)

    @app.post("/crm/v4/associations/{source}/{target}/batch/associate/default")
    async def batch_default(request: Request, source: str, target: str) -> Response:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, write_scopes(src, dst))
        payload = await body(request)
        results, errors = [], []
        for item in inputs(payload):
            try:
                sid, did = pair_id(item.get("from")), pair_id(item.get("to"))
                fetch(ctx, grant, src, sid)
                fetch(ctx, grant, dst, did)
                definition = default_type(ctx, src, dst)
                add(ctx, src, sid, dst, did, [definition])
                results.append(
                    {
                        "from": {"id": sid},
                        "to": {"id": did},
                        "associationSpec": {
                            "associationCategory": definition["category"],
                            "associationTypeId": definition["typeId"],
                        },
                    }
                )
            except EmulatorError as exc:
                errors.append({**exc.body, "context": {"ids": [error_id(item)]}})
        now = stamp(ctx.clock.now())
        result = {"status": "COMPLETE", "startedAt": now, "completedAt": now, "results": results}
        if errors:
            result.update(errors=errors, numErrors=len(errors))
        return JSONResponse(result, status_code=207 if errors else 200)

    @app.post("/crm/v4/associations/{source}/{target}/batch/archive", status_code=204)
    async def batch_archive(request: Request, source: str, target: str) -> Response:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, write_scopes(src, dst))
        payload = await body(request)
        pairs = []
        for item in inputs(payload):
            if not isinstance(item.get("to"), list) or not item["to"]:
                fail(400, "Association archive inputs require a non-empty to array.")
            sid = pair_id(item.get("from"))
            fetch(ctx, grant, src, sid)
            for target_ref in item["to"]:
                did = pair_id(target_ref)
                fetch(ctx, grant, dst, did)
                pairs.append((sid, did))
        for sid, did in pairs:
            remove_edges(ctx, src, sid, dst, did)
        return Response(status_code=204)

    @app.post("/crm/v4/associations/{source}/{target}/batch/labels/archive", status_code=204)
    async def batch_labels_archive(request: Request, source: str, target: str) -> Response:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, write_scopes(src, dst))
        payload = await body(request)
        removals = []
        for item in inputs(payload):
            sid, did = pair_id(item.get("from")), pair_id(item.get("to"))
            fetch(ctx, grant, src, sid)
            fetch(ctx, grant, dst, did)
            definitions = parse_types(ctx, src, dst, item.get("types"))
            removals.append((sid, did, {d["typeId"] for d in definitions}))
        for sid, did, type_ids in removals:
            remove_edges(ctx, src, sid, dst, did, type_ids)
        return Response(status_code=204)

    def v3_rows(grant: Any, src: str, sid: str, dst: str) -> list[dict[str, Any]]:
        rows, seen = [], set()
        for row in related(ctx, grant, src, sid, dst):
            for t in row["associationTypes"]:
                definition = ctx.store["_association_types"].get(f"{t['category']}:{t['typeId']}")
                entry = (str(row["toObjectId"]), definition["name"])
                if entry not in seen:
                    seen.add(entry)
                    rows.append({"id": entry[0], "type": entry[1]})
        return rows

    def v3_definition(src: str, dst: str, value: Any) -> dict[str, Any]:
        match = [t for t in types(ctx, src, dst) if t["name"] == value]
        if not match:
            fail(400, "Invalid association type or direction.")
        return next((t for t in match if t["label"] is None), match[0])

    @app.post("/crm/v3/associations/{source}/{target}/batch/read")
    async def v3_batch_read(request: Request, source: str, target: str) -> Response:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, [*scopes_for(src), *scopes_for(dst)])
        payload = await body(request)
        results, errors = [], []
        for item in inputs(payload, 1000):
            try:
                record = fetch(ctx, grant, src, str(item.get("id", "")))
                results.append({"from": {"id": record["id"]}, "to": v3_rows(grant, src, record["id"], dst)})
            except EmulatorError as exc:
                errors.append({**exc.body, "context": {"ids": [str(item.get("id", ""))]}})
        now = stamp(ctx.clock.now())
        result = {"status": "COMPLETE", "startedAt": now, "completedAt": now, "results": results}
        if errors:
            result.update(errors=errors, numErrors=len(errors))
        return JSONResponse(result, status_code=207 if errors else 200)

    @app.post("/crm/v3/associations/{source}/{target}/batch/create")
    async def v3_batch_create(request: Request, source: str, target: str) -> Response:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, write_scopes(src, dst))
        payload = await body(request)
        results, errors = [], []
        for item in inputs(payload):
            try:
                sid, did = pair_id(item.get("from")), pair_id(item.get("to"))
                fetch(ctx, grant, src, sid)
                fetch(ctx, grant, dst, did)
                definition = v3_definition(src, dst, item.get("type"))
                add(ctx, src, sid, dst, did, [definition])
                results.append({"from": {"id": sid}, "to": {"id": did}, "type": definition["name"]})
            except EmulatorError as exc:
                errors.append({**exc.body, "context": {"ids": [error_id(item)]}})
        now = stamp(ctx.clock.now())
        result = {"status": "COMPLETE", "startedAt": now, "completedAt": now, "results": results}
        if errors:
            result.update(errors=errors, numErrors=len(errors))
        return JSONResponse(result, status_code=207 if errors else 201)

    @app.post("/crm/v3/associations/{source}/{target}/batch/archive", status_code=204)
    async def v3_batch_archive(request: Request, source: str, target: str) -> Response:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, write_scopes(src, dst))
        payload = await body(request)
        removals = []
        for item in inputs(payload):
            sid, did = pair_id(item.get("from")), pair_id(item.get("to"))
            fetch(ctx, grant, src, sid)
            fetch(ctx, grant, dst, did)
            named = {t["typeId"] for t in types(ctx, src, dst) if t["name"] == item.get("type")}
            if not named:
                fail(400, "Invalid association type or direction.")
            removals.append((sid, did, named))
        for sid, did, type_ids in removals:
            remove_edges(ctx, src, sid, dst, did, type_ids)
        return Response(status_code=204)
