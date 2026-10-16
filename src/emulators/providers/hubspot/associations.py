"""Directional associations, including primary and unlabeled company relationships."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext, external_base

from .common import body, fail, fetch, inputs, integer, object_type, require, scopes_for, stamp, visible

# from, to, labeled ID, inverse ID, unlabeled ID, inverse unlabeled ID
PAIRS = [
    ("contacts", "companies", 1, 2, 279, 280), ("deals", "contacts", 3, 4, 3, 4),
    ("deals", "companies", 5, 6, 341, 342), ("deals", "line_items", 19, 20, 19, 20),
    ("tickets", "contacts", 16, 15, 16, 15), ("tickets", "companies", 26, 25, 26, 25),
    ("tickets", "deals", 28, 27, 28, 27),
    ("calls", "contacts", 194, 193, 194, 193), ("calls", "companies", 182, 181, 182, 181),
    ("calls", "deals", 206, 205, 206, 205),
    ("emails", "contacts", 198, 197, 198, 197), ("emails", "companies", 186, 185, 186, 185),
    ("emails", "deals", 210, 209, 210, 209),
    ("meetings", "contacts", 200, 199, 200, 199), ("meetings", "companies", 188, 187, 188, 187),
    ("meetings", "deals", 212, 211, 212, 211),
    ("notes", "contacts", 202, 201, 202, 201), ("notes", "companies", 190, 189, 190, 189),
    ("notes", "deals", 214, 213, 214, 213),
    ("tasks", "contacts", 204, 203, 204, 203), ("tasks", "companies", 192, 191, 192, 191),
    ("tasks", "deals", 216, 215, 216, 215),
]


def seed_types(ctx: ProviderContext) -> None:
    for left, right, forward, reverse, plain, reverse_plain in PAIRS:
        for src, dst, tid, inverse, unlabeled in [(left, right, forward, reverse, plain), (right, left, reverse, forward, reverse_plain)]:
            label = "Primary" if tid != unlabeled else None
            ctx.store["_association_types"].put({"id": f"HUBSPOT_DEFINED:{tid}", "from": src, "to": dst,
                "category": "HUBSPOT_DEFINED", "typeId": tid, "label": label, "inverse": inverse,
                "name": src.removesuffix("s") + "_to_" + ("company" if dst == "companies" else dst.removesuffix("s"))})
            if tid != unlabeled:
                inv_plain = reverse_plain if src == left else plain
                ctx.store["_association_types"].put({"id": f"HUBSPOT_DEFINED:{unlabeled}", "from": src, "to": dst,
                    "category": "HUBSPOT_DEFINED", "typeId": unlabeled, "label": None, "inverse": inv_plain,
                    "name": ("contact" if src == "contacts" else src.removesuffix("s")) + "_to_" + ("company" if dst == "companies" else dst.removesuffix("s")) + "_unlabeled"})


def types(ctx: ProviderContext, src: str, dst: str) -> list[dict[str, Any]]:
    return [r for r in ctx.store["_association_types"] if r["from"] == src and r["to"] == dst]


def default_type(ctx: ProviderContext, src: str, dst: str, *, primary: bool = False) -> dict[str, Any]:
    found = types(ctx, src, dst)
    if not found:
        fail(400, f"Unsupported association from {src} to {dst}.")
    return next((r for r in found if (r["label"] is not None) == primary), found[0])


def add(ctx: ProviderContext, src: str, sid: str, dst: str, did: str, definitions: list[dict[str, Any]]) -> None:
    for definition in definitions:
        for left, lid, right, rid, tid in [(src, sid, dst, did, definition["typeId"]), (dst, did, src, sid, definition["inverse"])]:
            key = f"{left}:{lid}:{right}:{rid}:{definition['category']}:{tid}"
            ctx.store["associations"].put({"id": key, "from": left, "fromId": lid, "to": right,
                "toId": rid, "typeId": tid, "category": definition["category"]})
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
        match = next((t for t in types(ctx, src, dst) if str(t["typeId"]) == str(value.get("associationTypeId")) and t["category"] == value.get("associationCategory")), None)
        if match is None:
            fail(400, "Invalid association type or direction.")
        result.append(match)
    return result


def related(ctx: ProviderContext, grant: Grant, src: str, sid: str, dst: str) -> list[dict[str, Any]]:
    if not any(s in grant.scopes for s in (*scopes_for(dst), "*")):
        return []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for edge in ctx.store["associations"]:
        if edge["from"] != src or edge["fromId"] != sid or edge["to"] != dst:
            continue
        record = ctx.store[dst].get(edge["toId"])
        if record is None or record["archived"] or not visible(ctx, grant, dst, record):
            continue
        definition = ctx.store["_association_types"].get(f"{edge['category']}:{edge['typeId']}")
        grouped.setdefault(edge["toId"], []).append({"category": edge["category"], "typeId": edge["typeId"], "label": definition["label"] if definition else None})
    return [{"toObjectId": int(rid), "associationTypes": ts} for rid, ts in sorted(grouped.items(), key=lambda p: int(p[0]))]


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/crm/v4/associations/{source}/{target}/labels")
    async def labels(request: Request, source: str, target: str) -> dict[str, Any]:
        src, dst = object_type(source), object_type(target)
        require(request, ctx, scopes_for(src))
        return {"results": [{k: t[k] for k in ("category", "typeId", "label")} for t in types(ctx, src, dst)]}

    @app.get("/crm/v4/objects/{source}/{sid}/associations/{target}")
    async def list_links(request: Request, source: str, sid: str, target: str) -> dict[str, Any]:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, scopes_for(src))
        require(request, ctx, scopes_for(dst))
        fetch(ctx, grant, src, sid)
        limit = integer(request.query_params.get("limit"), 500, 500)
        after = integer(request.query_params.get("after"), 0, 2**63 - 1, minimum=0)
        rows = [r for r in related(ctx, grant, src, sid, dst) if r["toObjectId"] > after]
        result: dict[str, Any] = {"results": rows[:limit]}
        if len(rows) > limit:
            cursor = str(rows[limit - 1]["toObjectId"])
            result["paging"] = {"next": {"after": cursor, "link": external_base(request) + f"/crm/v4/objects/{source}/{sid}/associations/{target}?after={cursor}&limit={limit}"}}
        return result

    async def connect(request: Request, source: str, sid: str, target: str, did: str, plain: bool) -> dict[str, Any]:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, scopes_for(src, "write"))
        require(request, ctx, scopes_for(dst, "write"))
        fetch(ctx, grant, src, sid)
        fetch(ctx, grant, dst, did)
        definitions = [default_type(ctx, src, dst)] if plain else parse_types(ctx, src, dst, await request.json())
        add(ctx, src, sid, dst, did, definitions)
        return {"fromObjectTypeId": ctx.store.meta["object_ids"][src], "fromObjectId": int(sid),
                "toObjectTypeId": ctx.store.meta["object_ids"][dst], "toObjectId": int(did),
                "labels": [d["label"] for d in definitions if d["label"]]}

    @app.put("/crm/v4/objects/{source}/{sid}/associations/default/{target}/{did}", status_code=201)
    async def connect_default(request: Request, source: str, sid: str, target: str, did: str) -> dict[str, Any]:
        return await connect(request, source, sid, target, did, True)

    @app.put("/crm/v4/objects/{source}/{sid}/associations/{target}/{did}", status_code=201)
    async def connect_labeled(request: Request, source: str, sid: str, target: str, did: str) -> dict[str, Any]:
        return await connect(request, source, sid, target, did, False)

    @app.delete("/crm/v4/objects/{source}/{sid}/associations/{target}/{did}", status_code=204)
    async def remove(request: Request, source: str, sid: str, target: str, did: str) -> Response:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, scopes_for(src, "write"))
        require(request, ctx, scopes_for(dst, "write"))
        fetch(ctx, grant, src, sid)
        fetch(ctx, grant, dst, did)
        for edge in ctx.store["associations"]:
            if (edge["from"], edge["fromId"], edge["to"], edge["toId"]) in [(src, sid, dst, did), (dst, did, src, sid)]:
                ctx.store["associations"].delete(edge["id"])
        return Response(status_code=204)

    @app.post("/crm/v4/associations/{source}/{target}/batch/read")
    async def batch_read(request: Request, source: str, target: str) -> dict[str, Any]:
        src, dst = object_type(source), object_type(target)
        grant = require(request, ctx, scopes_for(src))
        require(request, ctx, scopes_for(dst))
        payload = await body(request)
        records = [fetch(ctx, grant, src, str(item.get("id", ""))) for item in inputs(payload, 1000)]
        now = stamp(ctx.clock.now())
        return {"status": "COMPLETE", "startedAt": now, "completedAt": now, "results": [
            {"from": {"id": r["id"]}, "to": related(ctx, grant, src, r["id"], dst)} for r in records]}
