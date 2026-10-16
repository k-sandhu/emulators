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
    extension_of,
    json_body,
    parse_fields,
    parse_ts,
    query_int,
    query_list,
    require_str,
    try_parse_ts,
)

Record = dict[str, Any]
CONTENT_TYPES = ("name", "description", "file_content", "comments", "tags")
CONTENT_TYPE_ALIASES = {"tag": "tags"}


def parse_range(raw: str | None, name: str) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    low, _, high = raw.partition(",")
    for value in (low, high):
        if value and try_parse_ts(value) is None:
            raise errors.invalid_parameter(name, f"Invalid date in '{name}'")
    return (low or None), (high or None)


def parse_size_range(raw: str | None) -> tuple[int | None, int | None]:
    """``size_range=1000,5000`` (either end optional) -> inclusive byte bounds."""
    if not raw:
        return None, None
    low, sep, high = raw.partition(",")
    bounds: list[int | None] = []
    for value in (low, high):
        if not value.strip():
            bounds.append(None)
            continue
        try:
            number = int(value)
        except ValueError:
            raise errors.invalid_parameter(
                "size_range", "'size_range' must be two comma separated integers"
            ) from None
        if number < 0 or not sep:
            raise errors.invalid_parameter("size_range", "'size_range' must be two comma separated integers")
        bounds.append(number)
    return bounds[0], bounds[1]


def parse_mdfilters(raw: str | None) -> list[dict[str, Any]]:
    """``mdfilters`` is a JSON list of ``{scope, templateKey, filters}`` objects."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        raise errors.invalid_parameter("mdfilters", "'mdfilters' must be valid JSON") from None
    if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
        raise errors.invalid_parameter(
            "mdfilters", "'mdfilters' must be a list holding exactly one metadata filter object"
        )
    for flt in parsed:
        if not isinstance(flt.get("templateKey"), str) or not isinstance(flt.get("scope", "enterprise"), str):
            raise errors.invalid_parameter("mdfilters", "Each filter needs a 'scope' and a 'templateKey'")
        if not isinstance(flt.get("filters", {}), dict):
            raise errors.invalid_parameter("mdfilters", "'filters' must be an object")
    return parsed


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
                    try:
                        if "gt" in expected and not (actual is not None and actual > expected["gt"]):
                            return False
                        if "lt" in expected and not (actual is not None and actual < expected["lt"]):
                            return False
                    except TypeError:  # comparing a number with a date or text matches nothing
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
        mdfilters = parse_mdfilters(raw_md)
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
        content_types = [CONTENT_TYPE_ALIASES.get(t, t) for t in query_list(request, "content_types")]
        content_types = content_types or list(CONTENT_TYPES)
        if any(t not in CONTENT_TYPES for t in content_types):
            raise errors.invalid_parameter("content_types")
        extensions = {e.lower().lstrip(".") for e in query_list(request, "file_extensions")}
        ancestors = set(query_list(request, "ancestor_folder_ids"))
        if scope == "user_content":
            for folder_id in ancestors:  # a folder the caller cannot see (or that does not exist) is a 404
                p.get_item("folder", folder_id)
        if db.ROOT_WIRE_ID in ancestors:
            ancestors = set()  # folder 0 is the whole account: it restricts nothing
        deleted_by = set(query_list(request, "deleted_user_ids"))
        deleted_range = parse_range(q.get("deleted_at_range"), "deleted_at_range")
        searching_trash = bool(deleted_by) or deleted_range != (None, None)
        owners = set(query_list(request, "owner_user_ids"))
        updaters = set(query_list(request, "recent_updater_user_ids"))
        created = parse_range(q.get("created_at_range"), "created_at_range")
        updated = parse_range(q.get("updated_at_range"), "updated_at_range")
        size_low, size_high = parse_size_range(q.get("size_range"))
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
            if extensions and (record["type"] != "file" or extension_of(record["name"]) not in extensions):
                continue
            if ancestors and not any(a["id"] in ancestors for a in tree.ancestors(ctx, record)):
                continue
            if owners and record.get("_owner") not in owners:
                continue
            if updaters and record.get("modified_by") not in updaters:
                continue
            if created != (None, None) and not in_ts_range(record["created_at"], created):
                continue
            if updated != (None, None) and not in_ts_range(record["modified_at"], updated):
                continue
            if record["type"] != "web_link":
                size = record["size"] if record["type"] == "file" else p.tree.size(record["id"])
                if size_low is not None and size < size_low:
                    continue
                if size_high is not None and size > size_high:
                    continue
            if searching_trash:
                if trash != "trashed_only" or not record.get("_trash"):
                    continue
                if deleted_by and record.get("_trashed_by") not in deleted_by:
                    continue
                if deleted_range != (None, None) and not in_ts_range(record["trashed_at"], deleted_range):
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
                if "tags" in content_types:
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
            # Relevance is always most relevant first: Box ignores ``direction`` here.
            scored.sort(key=lambda row: (-row[0], row[1]["modified_at"], row[1]["id"]))
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
        if key == metadata.CLASSIFICATION_KEY:
            raise errors.invalid_parameter(
                "from", "Classification templates cannot be used in a metadata query"
            )
        ancestor = require_str(body, "ancestor_folder_id")
        root = p.get_item("folder", ancestor)
        params = body.get("query_params")
        params = {} if params is None else params
        if not isinstance(params, dict):
            raise errors.invalid_parameter("query_params", "'query_params' must be an object")
        text = body.get("query")
        if text is not None and not isinstance(text, str):
            raise errors.invalid_parameter("query", "'query' must be a string")
        ordering = body.get("order_by")
        ordering = [] if ordering is None else ordering
        if not isinstance(ordering, list) or not all(
            isinstance(o, dict) and isinstance(o.get("field_key"), str) for o in ordering
        ):
            raise errors.invalid_parameter("order_by", "'order_by' must be a list of {field_key, direction}")
        directions = {str(o.get("direction", "asc")).lower() for o in ordering}
        if not directions <= {"asc", "desc"} or len(directions) > 1:
            raise errors.invalid_parameter(
                "order_by", "'direction' must be asc or desc, and the same for every entry"
            )
        wanted_fields = body.get("fields")
        if wanted_fields is not None and (
            not isinstance(wanted_fields, list) or not all(isinstance(f, str) for f in wanted_fields)
        ):
            raise errors.invalid_parameter("fields", "'fields' must be a list of field names")
        marker = body.get("marker")
        if marker is not None and not isinstance(marker, str):
            raise errors.invalid_parameter("marker", "'marker' must be a string")
        predicate = query.parse_metadata_query(text, params) if text else (lambda v: True)
        limit = body.get("limit", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100:
            raise errors.invalid_parameter("limit", "'limit' must be between 0 and 100")
        rows: list[tuple[Record, Record]] = []
        for record in db.files(ctx):
            if record.get("_trash") or not p.visible(record) or tree.is_trashed(ctx, record):
                continue
            if not db.is_root(root) and not tree.is_within(ctx, record, root["id"]):
                continue
            inst = metadata.find_instance(ctx, record, scope, key)
            if inst is not None and predicate(inst["values"]):
                rows.append((record, inst))
        for order in reversed(ordering):
            field = order["field_key"]
            present = [row for row in rows if row[1]["values"].get(field) is not None]
            missing = [row for row in rows if row[1]["values"].get(field) is None]
            descending = str(order.get("direction", "asc")).lower() == "desc"
            try:
                present.sort(key=lambda row, f=field: row[1]["values"][f], reverse=descending)
            except TypeError:  # mixed value types cannot be ordered: keep the stored order
                pass
            rows = present + missing
        offset = decode_marker(marker)
        window = rows[offset : offset + limit]
        shaper = api.shaper(p, request)
        entries = [shaper.item(r, wanted_fields, listing=True) for r, _ in window]
        more = limit > 0 and offset + limit < len(rows)
        return {
            "entries": entries,
            "limit": limit,
            "next_marker": encode_marker(offset + limit) if more else None,
        }
