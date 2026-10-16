"""Block trees and their mutation endpoints."""

from __future__ import annotations

import copy
from typing import Any

from fastapi import FastAPI, Request

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext
from emulators.providers.notion.auth import capability
from emulators.providers.notion.common import (
    allowed,
    ancestors,
    authenticate,
    body,
    envelope,
    fail,
    invalid,
    paginate,
    policy,
    record_by_id,
    require,
    rich_text,
    serialize,
    title_of,
    touch,
    uid,
)
from emulators.providers.notion.events import emit
from emulators.providers.notion.properties import attachment, check_upload_access

TEXT_BLOCKS = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "toggle",
    "quote",
    "callout",
    "code",
}
CHILD_BLOCKS = {
    "paragraph",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "toggle",
    "quote",
    "callout",
    "synced_block",
    "column",
    "column_list",
    "table",
    "heading_1",
    "heading_2",
    "heading_3",
}
TYPES = TEXT_BLOCKS | {
    "divider",
    "table",
    "table_row",
    "image",
    "file",
    "pdf",
    "bookmark",
    "synced_block",
    "column_list",
    "column",
    "link_to_page",
    "equation",
    "table_of_contents",
    "embed",
    "video",
    "audio",
    "breadcrumb",
}


def normalize(
    ctx: ProviderContext, kind: str, raw: Any, grant: Grant, *, old: dict[str, Any] | None = None
) -> dict[str, Any]:
    if kind not in TYPES or not isinstance(raw, dict):
        invalid(f"Unsupported or invalid block type: {kind}.")
    if any(key.startswith("_") for key in raw):
        invalid("Block content contains an unsupported field.")
    data = copy.deepcopy(old or {})
    data.update({k: v for k, v in raw.items() if k != "children"})
    if kind in TEXT_BLOCKS:
        data["rich_text"] = rich_text(data.get("rich_text", []), ctx)
        if kind != "code":
            data.setdefault("color", "default")
        if kind.startswith("heading_"):
            data.setdefault("is_toggleable", False)
            if not isinstance(data["is_toggleable"], bool):
                invalid("is_toggleable must be boolean.")
        if kind == "to_do":
            data.setdefault("checked", False)
            if not isinstance(data["checked"], bool):
                invalid("to_do.checked must be boolean.")
        if kind == "callout" and ("icon" in raw or "icon" not in data):
            data["icon"] = attachment(ctx, data.get("icon", {"type": "emoji", "emoji": "💡"}))
        if kind == "code":
            data.setdefault("language", "plain text")
            data["caption"] = rich_text(data.get("caption", []), ctx)
    elif kind in ("image", "file", "pdf", "video", "audio"):
        if any(key in raw for key in ("external", "file_upload")):
            data.pop("_file_id", None)
        if not data.get("_file_id"):
            data = attachment(ctx, data)
        if data.get("type") == "emoji":
            invalid("Media blocks require an external file or file upload.")
        data["caption"] = rich_text(data.get("caption", []), ctx)
        if kind == "file":
            data.setdefault("name", "")
    elif kind in ("bookmark", "embed"):
        if not isinstance(data.get("url"), str):
            invalid("url must be a string.")
        data["caption"] = rich_text(data.get("caption", []), ctx)
    elif kind == "table":
        width = data.get("table_width")
        if not isinstance(width, int) or isinstance(width, bool) or width < 1:
            invalid("table_width must be a positive integer.")
        data.setdefault("has_column_header", False)
        data.setdefault("has_row_header", False)
        if not isinstance(data["has_column_header"], bool) or not isinstance(data["has_row_header"], bool):
            invalid("Table header settings must be boolean.")
    elif kind == "table_row":
        if not isinstance(data.get("cells"), list):
            invalid("table_row.cells must be an array.")
        data["cells"] = [rich_text(cell, ctx) for cell in data["cells"]]
    elif kind == "synced_block":
        data.setdefault("synced_from", None)
        if data["synced_from"]:
            source_id = uid(data["synced_from"].get("block_id"))
            source = require(ctx, grant, "blocks", source_id)
            if source["type"] != "synced_block" or source["synced_block"].get("synced_from"):
                invalid("synced_from must refer to an original synced block.")
            data["synced_from"] = {"type": "block_id", "block_id": source_id}
    elif kind == "link_to_page":
        target_type = data.get("type") or next((k for k in ("page_id", "database_id") if k in data), "")
        if target_type not in ("page_id", "database_id"):
            invalid("link_to_page requires page_id or database_id.")
        target = uid(data[target_type])
        require(ctx, grant, "pages" if target_type == "page_id" else "databases", target)
        data = {"type": target_type, target_type: target}
    elif kind == "equation":
        if not isinstance(data.get("expression"), str):
            invalid("equation.expression must be a string.")
    elif kind == "table_of_contents":
        data.setdefault("color", "default")
    return data


def append(
    ctx: ProviderContext, parent: dict[str, Any], raw: Any, grant: Grant, after: Any = None
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        invalid("children must be an array of 1 to 100 blocks.")
    order = list(parent.get("_children", []))
    if after is not None:
        after = uid(after)
        if after not in order:
            invalid("after must identify an existing child of this block.")
    staged: list[dict[str, Any]] = []

    def build(item: Any, owner: dict[str, Any], depth: int) -> dict[str, Any]:
        if not isinstance(item, dict) or depth > 2:
            invalid("Blocks may be nested at most two levels in one request.")
        kind = item.get("type") or next((k for k in item if k in TYPES), "")
        data = item.get(kind)
        if not isinstance(data, dict):
            invalid("Each block requires a type and its content object.")
        check_upload_access(ctx, data, grant.client_id)
        normalized = normalize(ctx, kind, data, grant)
        record = {
            **envelope(
                ctx.ids.guid(),
                "block",
                {
                    "type": "page_id" if owner["object"] == "page" else "block_id",
                    "page_id" if owner["object"] == "page" else "block_id": owner["id"],
                },
                ctx.clock.now(),
            ),
            "type": kind,
            kind: normalized,
            "has_children": False,
            "_children": [],
            **policy(owner),
        }
        staged.append(record)
        if len(staged) > 1000:
            invalid("A request may contain at most 1000 block elements.")
        children = data.get("children", [])
        if not isinstance(children, list) or len(children) > 100:
            invalid("children must contain at most 100 blocks.")
        if children and (
            kind not in CHILD_BLOCKS
            or (kind.startswith("heading_") and not normalized["is_toggleable"])
            or (kind == "synced_block" and normalized["synced_from"])
        ):
            invalid(f"{kind} does not support children.")
        for child in children:
            entry = build(child, record, depth + 1)
            if kind == "table" and (
                entry["type"] != "table_row" or len(entry["table_row"]["cells"]) != normalized["table_width"]
            ):
                invalid("Table children must be rows with table_width cells.")
            if kind == "column_list" and entry["type"] != "column":
                invalid("column_list children must be columns.")
            record["_children"].append(entry["id"])
        if kind == "table" and not children:
            invalid("A new table must contain at least one table_row.")
        if owner.get("type") == "table" and (
            kind != "table_row" or len(normalized["cells"]) != owner["table"]["table_width"]
        ):
            invalid("Table row cell count must match table_width.")
        record["has_children"] = bool(children)
        return record

    records = [build(item, parent, 0) for item in raw]
    for record in staged:
        ctx.store["blocks"].put(record)
    position = order.index(after) + 1 if after else len(order)
    order[position:position] = [record["id"] for record in records]
    parent["_children"] = order
    parent["has_children"] = True
    touch(ctx, parent, grant)
    return records


def container(ctx: ProviderContext, grant: Grant, block_id: str, *, write: bool = False) -> dict[str, Any]:
    record_id = uid(block_id)
    kind = (
        "pages"
        if ctx.store["pages"].get(record_id)
        else "databases"
        if ctx.store["databases"].get(record_id)
        else "blocks"
    )
    record = require(ctx, grant, kind, record_id, write=write)
    if write and any(r.get("archived") for r in ancestors(ctx, record)):
        fail(409, "conflict_error", "Cannot edit a block that is archived. Restore it first.")
    return record


def as_block(record: dict[str, Any]) -> dict[str, Any]:
    if record["object"] == "block":
        return record
    kind = "child_page" if record["object"] == "page" else "child_database"
    return {
        **{
            k: v
            for k, v in record.items()
            if k
            not in ("properties", "title", "description", "icon", "cover", "url", "public_url", "is_inline")
        },
        "object": "block",
        "type": kind,
        kind: {"title": title_of(record)},
        "has_children": bool(record.get("_children")),
    }


async def content_event(ctx: ProviderContext, record: dict[str, Any]) -> None:
    page = next((r for r in ancestors(ctx, record) if r["object"] == "page"), None)
    if page:
        touch(ctx, page)
        await emit(ctx, "page.content_updated", page["id"])


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/v1/blocks/{block_id}/children")
    async def children(block_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "read_content")
        record = container(ctx, grant, block_id)
        if record.get("type") == "synced_block" and record["synced_block"].get("synced_from"):
            record = require(ctx, grant, "blocks", record["synced_block"]["synced_from"]["block_id"])
        rows = [
            as_block(child)
            for cid in record.get("_children", [])
            if (child := record_by_id(ctx, cid)) and not child.get("archived") and allowed(ctx, grant, child)
        ]
        result = paginate(ctx, rows, request.query_params, key="children:" + record["id"], grant=grant)
        result["results"] = [serialize(request, ctx, r, grant) for r in result["results"]]
        return result

    @app.patch("/v1/blocks/{block_id}/children")
    async def append_children(block_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "insert_content")
        record = container(ctx, grant, block_id, write=True)
        if record["object"] == "database" or (
            record["object"] == "block" and record["type"] not in CHILD_BLOCKS
        ):
            invalid("This block does not support children.")
        if record.get("type") == "synced_block" and record["synced_block"].get("synced_from"):
            invalid("Append children to the original synced block.")
        payload = await body(request)
        records = append(ctx, record, payload.get("children"), grant, payload.get("after"))
        await content_event(ctx, record)
        return {
            "object": "list",
            "results": [serialize(request, ctx, r, grant) for r in records],
            "next_cursor": None,
            "has_more": False,
            "type": "block",
            "block": {},
        }

    @app.get("/v1/blocks/{block_id}")
    async def retrieve(block_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "read_content")
        return serialize(request, ctx, as_block(container(ctx, grant, block_id)), grant)

    @app.patch("/v1/blocks/{block_id}")
    async def update(block_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "update_content")
        record = require(ctx, grant, "blocks", block_id, write=True)
        payload = await body(request)
        kind = record["type"]
        if any(k in TYPES and k != kind for k in payload) or ("type" in payload and payload["type"] != kind):
            invalid("A block's type cannot be changed.")
        if kind in payload:
            check_upload_access(ctx, payload[kind], grant.client_id)
            record[kind] = normalize(ctx, kind, payload[kind], grant, old=record[kind])
        if "archived" in payload or "in_trash" in payload:
            archived = payload.get("in_trash", payload.get("archived"))
            if not isinstance(archived, bool):
                invalid("archived/in_trash must be a boolean.")
            record.update(archived=archived, in_trash=archived)
        touch(ctx, record, grant)
        await content_event(ctx, record)
        return serialize(request, ctx, record, grant)

    @app.delete("/v1/blocks/{block_id}")
    async def delete(block_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "update_content")
        record = container(ctx, grant, block_id, write=True)
        record.update(archived=True, in_trash=True)
        touch(ctx, record, grant)
        await content_event(ctx, record)
        return serialize(request, ctx, as_block(record), grant)
