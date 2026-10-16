"""Search (``GET /search``) and metadata queries (``POST /metadata_queries/execute_read``)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request

from emulators.core.provider import ProviderContext
from emulators.providers.box import db, errors, metadata, query, tree
from emulators.providers.box.access import Principal
from emulators.providers.box.routing import Api
from emulators.providers.box.util import (
    decode_marker,
    encode_marker,
    json_body,
    parse_fields,
    parse_ts,
    query_int,
    query_list,
    require_str,
    try_parse_ts,
)

Record = dict[str, Any]
CONTENT_TYPES = ("name", "description", "file_content", "comments", "tag")


def parse_range(raw: str | None, name: str) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    low, _, high = raw.partition(",")
    for value in (low, high):
        if value and try_parse_ts(value) is None:
            raise errors.invalid_parameter(name, f"Invalid date in '{name}'")
    return (low or None), (high or None)


def in_ts_range(stamp: str, window: tuple[str | None, str | None]) -> bool:
    moment = parse_ts(stamp)
    low, high = window
    if low and moment < parse_ts(low):
        return False
    return not (high and moment > parse_ts(high))


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)

    def candidates(p: Principal, scope: str, trash: str) -> list[Record]:
        pool = [*db.files(ctx), *db.folders(ctx), *ctx.store.collection(db.WEB_LINKS)]
        out = []
        for record in pool:
            if db.is_root(record):
                continue
            trashed = tree.is_trashed(ctx, record)
            if (trash == "non_trashed_only" and trashed) or (trash == "trashed_only" and not trashed):
                continue
            if scope == "enterprise_content" or p.visible(record):
                out.append(record)
        return out

    def comments_text(record: Record) -> str:
        return " ".join(
            c["message"]
            for c in ctx.store.collection(db.COMMENTS)
            if c["item_id"] == record["id"] and record["type"] == "file"
        ).lower()

    def metadata_filters_pass(record: Record, mdfilters: list[dict[str, Any]]) -> bool:
        for flt in mdfilters:
            inst = metadata.find_instance(
                ctx, record, flt.get("scope", "enterprise"), flt.get("templateKey", "")
            )
            if inst is None:
                return False
            for key, expected in (flt.get("filters") or {}).items():
                actual = inst["values"].get(key)
                if isinstance(expected, dict):
                    if "gt" in expected and not (actual is not None and actual > expected["gt"]):
                        return False
                    if "lt" in expected and not (actual is not None and actual < expected["lt"]):
                        return False
                elif isinstance(actual, list):
                    if expected not in actual:
                        return False
                elif actual != expected:
                    return False
        return True

    @api.get("/search")
    async def search(request: Request) -> Any:
        p = api.principal(request)
        q = request.query_params
        text = q.get("query")
        raw_md = q.get("mdfilters")
        if not text and not raw_md:
            raise errors.missing_parameter("query")
        mdfilters: list[dict[str, Any]] = []
        if raw_md:
            try:
                mdfilters = json.loads(raw_md)
            except ValueError:
                raise errors.invalid_parameter("mdfilters", "'mdfilters' must be valid JSON") from None
        scope = q.get("scope", "user_content")
        if scope not in ("user_content", "enterprise_content"):
            raise errors.invalid_parameter("scope")
        if scope == "enterprise_content":
            p.require_privileged()
        trash = q.get("trash_content", "non_trashed_only")
        if trash not in ("non_trashed_only", "trashed_only", "all_items"):
            raise errors.invalid_parameter("trash_content")
        kind = q.get("type")
        if kind and kind not in ("file", "folder", "web_link"):
            raise errors.invalid_parameter("type", "'type' must be file, folder or web_link")
        content_types = query_list(request, "content_types") or list(CONTENT_TYPES)
        if any(t not in CONTENT_TYPES for t in content_types):
            raise errors.invalid_parameter("content_types")
        extensions = {e.lower().lstrip(".") for e in query_list(request, "file_extensions")}
        ancestors = set(query_list(request, "ancestor_folder_ids"))
        owners = set(query_list(request, "owner_user_ids"))
        updaters = set(query_list(request, "recent_updater_user_ids"))
        created = parse_range(q.get("created_at_range"), "created_at_range")
        updated = parse_range(q.get("updated_at_range"), "updated_at_range")
        size_low, _, size_high = (q.get("size_range") or "").partition(",")
        sort = q.get("sort", "relevance")
        if sort not in ("relevance", "modified_at"):
            raise errors.invalid_parameter("sort")
        direction = q.get("direction", "DESC").upper()
        limit = query_int(request, "limit", 30, maximum=200)
        offset = query_int(request, "offset", 0, maximum=10_000)
        expression = query.parse_search(text) if text else None

        scored: list[tuple[int, Record]] = []
        for record in candidates(p, scope, trash):
            if kind and record["type"] != kind:
                continue
            if extensions and (
                record["type"] != "file" or record["name"].rsplit(".", 1)[-1].lower() not in extensions
            ):
                continue
            if ancestors and not any(tree.is_within(ctx, record, a) for a in ancestors):
                continue
            if owners and record.get("_owner") not in owners:
                continue
            if updaters and record.get("modified_by") not in updaters:
                continue
            if created != (None, None) and not in_ts_range(record["created_at"], created):
                continue
            if updated != (None, None) and not in_ts_range(record["modified_at"], updated):
                continue
            if record["type"] == "file":
                if size_low and record["size"] < int(size_low):
                    continue
                if size_high and record["size"] > int(size_high):
                    continue
            if mdfilters and not metadata_filters_pass(record, mdfilters):
                continue
            score = 1
            if expression is not None:
                fields: list[str] = []
                weights: dict[str, int] = {}
                if "name" in content_types:
                    fields.append(record["name"])
                    weights["name"] = 10
                if "description" in content_types:
                    fields.append(record.get("description", ""))
                    weights["description"] = 4
                if "tag" in content_types:
                    fields.append(" ".join(record.get("tags", [])))
                if "file_content" in content_types and record["type"] == "file":
                    fields.append(record.get("_text", ""))
                if "comments" in content_types:
                    fields.append(comments_text(record))
                haystack = "\n".join(fields)
                if not expression(query.text_matcher(haystack)):
                    continue
                score = 1 + sum(
                    w
                    for key, w in weights.items()
                    if key == "name" and expression(query.text_matcher(record["name"]))
                )
            scored.append((score, record))

        if sort == "modified_at":
            scored.sort(key=lambda row: (row[1]["modified_at"], row[1]["id"]), reverse=direction != "ASC")
        else:
            scored.sort(key=lambda row: (-row[0], row[1]["modified_at"], row[1]["id"]))
            if direction == "ASC":
                scored.reverse()
        shaper = api.shaper(p, request)
        fields_param = parse_fields(request)
        window = [shaper.item(r, fields_param) for _, r in scored[offset : offset + limit]]
        return {
            "total_count": len(scored),
            "entries": window,
            "limit": limit,
            "offset": offset,
            "type": "search_results_items",
        }

    @api.post("/metadata_queries/execute_read")
    async def metadata_query(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        source = require_str(body, "from")
        scope, _, key = source.rpartition(".")
        if not scope or not key:
            raise errors.invalid_parameter("from", "'from' must look like 'enterprise_123.templateKey'")
        template = metadata.find_template(ctx, scope, key)
        if template is None:
            raise errors.not_found("Specified Metadata Template not found")
        ancestor = require_str(body, "ancestor_folder_id")
        root = p.get_item("folder", ancestor)
        params = body.get("query_params") or {}
        predicate = (
            query.parse_metadata_query(body["query"], params) if body.get("query") else (lambda v: True)
        )
        limit = body.get("limit", 100)
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise errors.invalid_parameter("limit", "'limit' must be between 1 and 100")
        rows: list[tuple[Record, Record]] = []
        for record in db.files(ctx):
            if record.get("_trash") or not p.visible(record) or tree.is_trashed(ctx, record):
                continue
            if not db.is_root(root) and not tree.is_within(ctx, record, root["id"]):
                continue
            inst = metadata.find_instance(ctx, record, scope, key)
            if inst is not None and predicate(inst["values"]):
                rows.append((record, inst))
        for order in reversed(body.get("order_by") or []):
            field = order.get("field_key")
            rows.sort(
                key=lambda row, f=field: (row[1]["values"].get(f) is None, row[1]["values"].get(f)),
                reverse=str(order.get("direction", "asc")).lower() == "desc",
            )
        offset = decode_marker(body.get("marker"))
        window = rows[offset : offset + limit]
        shaper = api.shaper(p, request)
        fields = body.get("fields")
        entries = [shaper.item(r, fields, listing=True) for r, _ in window]
        more = offset + limit < len(rows)
        return {
            "entries": entries,
            "limit": limit,
            "next_marker": encode_marker(offset + limit) if more else None,
        }
