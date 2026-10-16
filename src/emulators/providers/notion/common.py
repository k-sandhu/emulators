"""Notion wire shapes, access checks, and version-aware serialization."""

from __future__ import annotations

import copy
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Any, NoReturn
from urllib.parse import quote, unquote
from uuid import UUID, uuid4

from fastapi import Request

from emulators.core.auth import Grant, bearer_token
from emulators.core.clock import isoformat_z
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_guid, stable_hash
from emulators.core.provider import ProviderContext, url_for_host

OLD = "2022-06-28"
NEW = "2025-09-03"
INTERNAL_TOKEN = "ntn_halcyon_internal_integration"
LEGACY_TOKEN = "secret_halcyon_internal_integration"
BOT_ID = stable_guid("notion", "bot")
ANNOTATIONS = dict(
    bold=False, italic=False, strikethrough=False, underline=False, code=False, color="default"
)
WRITE_ROLES = {"owner", "editor"}


def error_body(status: int, code: str, message: str) -> dict[str, Any]:
    return {"object": "error", "status": status, "code": code, "message": message, "request_id": str(uuid4())}


def fail(status: int, code: str, message: str) -> NoReturn:
    raise EmulatorError(status, error_body(status, code, message))


def invalid(message: str) -> NoReturn:
    fail(400, "validation_error", message)


def uid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        invalid(f"{value!s} is not a valid UUID.")


def stamp(value: datetime) -> str:
    return isoformat_z(value, timespec="milliseconds")


def partial_user(user_id: str = BOT_ID) -> dict[str, str]:
    return {"object": "user", "id": user_id}


def person_id(world_id: str) -> str:
    return stable_guid("notion", "user", world_id)


def rich_text(items: Any, ctx: ProviderContext | None = None) -> list[dict[str, Any]]:
    if isinstance(items, str):
        items = [{"type": "text", "text": {"content": items}}] if items else []
    if not isinstance(items, list) or len(items) > 100:
        invalid("rich_text must be an array containing at most 100 items.")
    result = []
    for raw in items:
        if not isinstance(raw, dict):
            invalid("Each rich text item must be an object.")
        kind = raw.get("type", "text")
        data = copy.deepcopy(raw.get(kind, {}))
        if not isinstance(data, dict) or not isinstance(raw.get("annotations", {}), dict):
            invalid("Rich text content and annotations must be objects.")
        href = None
        if kind == "text":
            text = data.get("content")
            if not isinstance(text, str) or len(text) > 2000:
                invalid("text.content must be a string of at most 2000 characters.")
            data.setdefault("link", None)
            if data["link"] is not None and (
                not isinstance(data["link"], dict) or not isinstance(data["link"].get("url"), str)
            ):
                invalid("text.link must be null or an object containing url.")
            href = (data.get("link") or {}).get("url")
        elif kind == "mention":
            mention_type = data.get("type") or next((k for k in ("user", "page", "date") if k in data), "")
            data["type"] = mention_type
            target = data.get(mention_type, {})
            if not isinstance(target, dict):
                invalid("Mention reference must be an object.")
            if mention_type == "user":
                target_id = uid(target.get("id"))
                user = ctx.store["users"].get(target_id) if ctx else None
                data["user"] = public(user) if user else partial_user(target_id)
                text = user["name"] if user else "Unknown user"
            elif mention_type == "page":
                target_id = uid(target.get("id"))
                data["page"] = {"id": target_id}
                page = ctx.store["pages"].get(target_id) if ctx else None
                text = title_of(page) if page else "Untitled"
                href = f"https://www.notion.so/{target_id.replace('-', '')}"
            elif mention_type == "date" and isinstance(target.get("start"), str):
                data["date"] = {
                    "start": target["start"],
                    "end": target.get("end"),
                    "time_zone": target.get("time_zone"),
                }
                text = target["start"]
            else:
                invalid("Unsupported mention type; expected user, page or date.")
        elif kind == "equation" and isinstance(data.get("expression"), str):
            text = data["expression"]
        else:
            invalid(f"Unsupported rich text type: {kind}.")
        result.append(
            {
                "type": kind,
                kind: data,
                "annotations": {**ANNOTATIONS, **raw.get("annotations", {})},
                "plain_text": text,
                "href": href,
            }
        )
    return result


def text_of(items: list[dict[str, Any]]) -> str:
    return "".join(item.get("plain_text", item.get("text", {}).get("content", "")) for item in items)


def title_of(record: dict[str, Any] | None) -> str:
    if not record:
        return "Untitled"
    if "title" in record:
        return text_of(record["title"])
    for prop in record.get("properties", {}).values():
        if prop.get("type") == "title":
            return text_of(prop["title"])
    return "Untitled"


def public(value: Any, *, preserve_keys: bool = False) -> Any:
    if isinstance(value, dict):
        return {
            k: public(v, preserve_keys=k == "properties")
            for k, v in value.items()
            if preserve_keys or not k.startswith("_")
        }
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


async def body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except (ValueError, UnicodeDecodeError):
        fail(400, "invalid_json", "Error parsing JSON body.")
    if not isinstance(value, dict):
        invalid("Request body must be an object.")
    return value


def version(request: Request) -> str:
    value = request.headers.get("notion-version")
    if not value:
        fail(400, "missing_version", "Notion-Version header is required.")
    if value not in (OLD, NEW):
        invalid(f"Unsupported Notion-Version: {value}. Supported versions are {OLD} and {NEW}.")
    return value


def authenticate(request: Request, ctx: ProviderContext) -> Grant:
    version(request)
    grant = ctx.tokens.resolve(bearer_token(request))
    if grant is None:
        fail(401, "unauthorized", "API token is invalid.")
    return grant


def world_user(ctx: ProviderContext, value: str | None) -> str | None:
    if not value:
        return None
    for person in ctx.world.active_employees:
        if value.lower() in (person.id, person.email.lower(), person_id(person.id)):
            return person.id
    return None


def record_by_id(ctx: ProviderContext, record_id: str) -> dict[str, Any] | None:
    for collection in ("pages", "blocks", "databases", "data_sources"):
        if record := ctx.store[collection].get(record_id):
            return record
    return None


def ancestors(ctx: ProviderContext, record: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    while record and record["id"] not in seen:
        seen.add(record["id"])
        result.append(record)
        parent = record.get("parent", {})
        record = record_by_id(ctx, parent.get(parent.get("type"), ""))
    return result


def role(ctx: ProviderContext, grant: Grant, record: dict[str, Any]) -> str | None:
    chain = ancestors(ctx, record)
    if grant.app_only:
        if record.get("_internal"):
            return "editor"
        return None
    person = world_user(ctx, grant.user_id)
    selected = grant.extra.get("shared_page_ids")
    if selected is not None and not any(r["id"] in selected for r in chain):
        return None
    return record.get("_acl", {}).get(person)


def allowed(ctx: ProviderContext, grant: Grant, record: dict[str, Any]) -> bool:
    return role(ctx, grant, record) is not None


def require(
    ctx: ProviderContext,
    grant: Grant,
    collection: str,
    record_id: str,
    *,
    write: bool = False,
    comment: bool = False,
) -> dict[str, Any]:
    record_id = uid(record_id)
    record = ctx.store[collection].get(record_id)
    access = role(ctx, grant, record) if record else None
    if access is None:
        label = {
            "pages": "page",
            "blocks": "block",
            "databases": "database",
            "data_sources": "data source",
        }.get(collection, "object")
        fail(
            404,
            "object_not_found",
            f"Could not find {label} with ID: {record_id}. Make sure the relevant pages and databases are shared with your integration.",
        )
    if (write and access not in WRITE_ROLES) or (comment and access not in WRITE_ROLES | {"commenter"}):
        fail(403, "restricted_resource", "Insufficient permissions for this operation.")
    return record


def policy(parent: dict[str, Any]) -> dict[str, Any]:
    return {"_acl": copy.deepcopy(parent.get("_acl", {})), "_internal": parent.get("_internal", False)}


def touch(ctx: ProviderContext, record: dict[str, Any], grant: Grant | None = None) -> None:
    record["last_edited_time"] = stamp(ctx.clock.now())
    record["last_edited_by"] = partial_user(BOT_ID)


def envelope(
    record_id: str, kind: str, parent: dict[str, Any], at: datetime, creator: str = BOT_ID
) -> dict[str, Any]:
    return dict(
        object=kind,
        id=record_id,
        parent=parent,
        created_time=stamp(at),
        last_edited_time=stamp(at),
        created_by=partial_user(creator),
        last_edited_by=partial_user(creator),
        archived=False,
        in_trash=False,
    )


def paginate(
    ctx: ProviderContext,
    rows: list[dict[str, Any]],
    params: Any,
    *,
    kind: str = "block",
    key: str = "",
    grant: Grant | None = None,
) -> dict[str, Any]:
    raw_size = params.get("page_size", 100)
    try:
        size = int(raw_size)
    except (ValueError, TypeError):
        invalid("page_size must be an integer between 1 and 100.")
    if isinstance(raw_size, bool) or str(size) != str(raw_size) or not 1 <= size <= 100:
        invalid("page_size must be an integer between 1 and 100.")
    binding = stable_hash(key, grant.token if grant else "")
    start = 0
    cursor = params.get("start_cursor")
    if cursor:
        saved = ctx.store["_cursors"].get(str(cursor))
        if saved is None or saved["binding"] != binding:
            invalid("start_cursor is invalid.")
        start = saved["offset"]
    more = start + size < len(rows)
    next_cursor = None
    if more:
        next_cursor = ctx.ids.guid()
        ctx.store["_cursors"].put({"id": next_cursor, "binding": binding, "offset": start + size})
    return {
        "object": "list",
        "results": rows[start : start + size],
        "next_cursor": next_cursor,
        "has_more": more,
        "type": kind,
        kind: {},
    }


def file_object(request: Request, ctx: ProviderContext, file_id: str) -> dict[str, Any]:
    expires = int((ctx.clock.now() + timedelta(hours=1)).timestamp())
    signature = hmac.new(
        ctx.store.meta["file_secret"].encode(), f"{file_id}:{expires}".encode(), hashlib.sha256
    ).hexdigest()
    record = ctx.store["files"].get(file_id)
    path = f"/secure/{file_id}/{quote(record['filename'])}?expires={expires}&signature={signature}"
    return {
        "type": "file",
        "file": {
            "url": url_for_host(request, ctx, "file.notion.so", path),
            "expiry_time": stamp(ctx.clock.now() + timedelta(hours=1)),
        },
    }


def wire_value(
    request: Request,
    ctx: ProviderContext,
    raw: Any,
    grant: Grant | None = None,
    *,
    preserve_keys: bool = False,
) -> Any:
    """Refresh file links and redact mention metadata using the reader's access."""
    if isinstance(raw, list):
        return [wire_value(request, ctx, item, grant) for item in raw]
    if not isinstance(raw, dict):
        return raw
    result = {
        key: wire_value(request, ctx, value, grant, preserve_keys=key == "properties")
        for key, value in raw.items()
        if preserve_keys or not key.startswith("_")
    }
    if raw.get("_file_id"):
        result.pop("file_upload", None)
        result.update(file_object(request, ctx, raw["_file_id"]))
    if raw.get("type") == "mention" and isinstance(raw.get("mention"), dict):
        mention = result["mention"]
        if mention.get("type") == "page":
            target_id = mention["page"]["id"]
            target = ctx.store["pages"].get(target_id)
            if target is None or (grant and not allowed(ctx, grant, target)):
                result.update(plain_text="Untitled", annotations=dict(ANNOTATIONS))
            else:
                result["plain_text"] = title_of(target)
            result["href"] = url_for_host(request, ctx, "www.notion.so", "/" + target_id.replace("-", ""))
        elif (
            mention.get("type") == "user"
            and grant
            and grant.scopes
            and "read_user_with_email" not in grant.scopes
        ):
            if "person" in mention["user"]:
                mention["user"]["person"] = {}
    return result


def serialize(
    request: Request, ctx: ProviderContext, record: dict[str, Any], grant: Grant | None = None
) -> dict[str, Any]:
    result = wire_value(request, ctx, record, grant)
    kind = result.get("object")
    if kind != "block":
        result.pop("has_children", None)
    if kind in ("page", "database"):
        result["url"] = url_for_host(request, ctx, "www.notion.so", "/" + record["id"].replace("-", ""))
    if kind == "database":
        if version(request) == OLD:
            sources = record.get("_sources", [])
            if len(sources) != 1:
                invalid("This database contains multiple data sources. Use Notion-Version 2025-09-03.")
            result["properties"] = public(
                ctx.store["data_sources"].get(sources[0])["properties"], preserve_keys=True
            )
        else:
            result["data_sources"] = [
                {"id": sid, "name": title_of(ctx.store["data_sources"].get(sid))}
                for sid in record.get("_sources", [])
            ]
    if kind == "page":
        parent = result["parent"]
        if version(request) == OLD and parent["type"] == "data_source_id":
            source = ctx.store["data_sources"].get(parent["data_source_id"])
            result["parent"] = {"type": "database_id", "database_id": source["parent"]["database_id"]}
        for prop in result["properties"].values():
            ptype = prop["type"]
            if ptype == "relation" and grant:
                prop[ptype] = [
                    v
                    for v in prop[ptype]
                    if (target := ctx.store["pages"].get(v["id"])) and allowed(ctx, grant, target)
                ]
            if ptype in ("title", "rich_text", "people", "relation"):
                if ptype == "relation":
                    prop["has_more"] = len(prop[ptype]) > 25
                prop[ptype] = prop[ptype][:25]
    if kind in ("database", "data_source"):
        for prop in result.get("properties", {}).values():
            relation = prop.get("relation")
            if relation and version(request) == OLD and "data_source_id" in relation:
                source = ctx.store["data_sources"].get(relation.pop("data_source_id"))
                relation["database_id"] = source["parent"]["database_id"]
    if kind == "block":
        child_source = record
        if record["type"] == "synced_block" and record["synced_block"].get("synced_from"):
            child_source = (
                ctx.store["blocks"].get(record["synced_block"]["synced_from"]["block_id"]) or record
            )
        result["has_children"] = any(
            (child := record_by_id(ctx, cid))
            and not child.get("archived")
            and (grant is None or allowed(ctx, grant, child))
            for cid in child_source.get("_children", [])
        )

    return result


def property_by_key(properties: dict[str, Any], key: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(key, str):
        invalid("Property name or id must be a string.")
    for name, prop in properties.items():
        if key == name or unquote(str(prop["id"])) == unquote(key):
            return name, prop
    invalid(f"{key} is not a property that exists.")
