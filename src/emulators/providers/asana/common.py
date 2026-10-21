"""Asana envelopes, projections, principal checks and immutable paging cursors."""

from __future__ import annotations

import copy
import json
import re
from datetime import date
from typing import Any, NoReturn
from urllib.parse import urlencode

from fastapi import Request

from emulators.core.auth import Grant, bearer_token
from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext, url_for_host

HOST = "app.asana.com"
BASE = "/api/1.0"
COLLECTIONS = (
    "users",
    "workspaces",
    "teams",
    "projects",
    "sections",
    "tasks",
    "stories",
    "attachments",
    "tags",
    "webhooks",
)
FIELD_NAMES = set(
    [
        "gid",
        "resource_type",
        "resource_subtype",
        "name",
        "email",
        "photo",
        "workspaces",
        "workspace",
        "is_organization",
        "email_domains",
        "organization",
        "description",
        "html_description",
        "visibility",
        "permalink_url",
        "team",
        "owner",
        "members",
        "followers",
        "archived",
        "public",
        "privacy_setting",
        "color",
        "notes",
        "html_notes",
        "created_at",
        "modified_at",
        "completed",
        "completed_at",
        "completed_by",
        "assignee",
        "assignee_status",
        "due_on",
        "due_at",
        "start_on",
        "start_at",
        "projects",
        "memberships",
        "section",
        "parent",
        "tags",
        "num_subtasks",
        "dependencies",
        "dependents",
        "custom_fields",
        "liked",
        "likes",
        "num_likes",
        "is_rendered_as_separator",
        "text",
        "html_text",
        "created_by",
        "type",
        "is_pinned",
        "source",
        "target",
        "download_url",
        "view_url",
        "permanent_url",
        "host",
        "size",
        "connected_to_app",
        "active",
        "filters",
        "last_success_at",
        "last_failure_at",
        "last_failure_content",
        "delivery_retry_count",
        "field",
        "action",
        "resource",
        "subtype",
        "new_value",
        "added_value",
        "removed_value",
        "new_subtype",
        "access_level",
        "default_view",
        "default_access_level",
        "minimum_access_level_for_sharing",
        "minimum_access_level_for_customization",
        "due_date",
        "current_status",
        "current_status_update",
        "start_date",
        "status_updates",
        "write_access_level",
        "is_commenting_restricted",
        "is_organization",
        "is_guest",
        "is_read_only",
        "avatar_url",
        "task",
        "project",
        "user",
        "enabled",
        "display_value",
        "number_value",
        "text_value",
        "enum_value",
        "enum_options",
        "precision",
        "format",
        "currency_code",
        "result",
        "sha1",
        "mime_type",
        "num_comments",
        "approval_status",
        "is_global_to_workspace",
    ]
)


def error(status: int, message: str) -> EmulatorError:
    return EmulatorError(
        status,
        {"errors": [{"message": message}]},
        headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
    )


def invalid(message: str) -> NoReturn:
    raise error(400, message)


def stamp(ctx: ProviderContext) -> str:
    return isoformat_z(ctx.clock.now(), timespec="milliseconds")


def gid(kind: str, world_id: str) -> str:
    return str(10**15 + int(stable_hash("asana", kind, world_id, length=13), 16))


def new_gid(ctx: ProviderContext) -> str:
    return str(ctx.ids.sequence("asana_runtime", start=9900000000000000))


def put(ctx: ProviderContext, collection: str, row: dict[str, Any]) -> dict[str, Any]:
    return ctx.store[collection].put(row, key="gid")


def compact(row: dict[str, Any]) -> dict[str, Any]:
    keys = ["gid", "resource_type", "name"]
    if row.get("resource_type") == "task":
        keys.append("resource_subtype")
    if row.get("resource_type") == "story":
        keys.extend(("created_at", "created_by", "resource_subtype", "text"))
    return {key: copy.deepcopy(row[key]) for key in keys if key in row}


def string(value: Any, name: str, *, nullable: bool = False, empty: bool = True) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or (not empty and not value):
        invalid(f"{name}: must be a string")
    try:
        value.encode("utf-8")
    except UnicodeError:
        invalid(f"{name}: must contain valid Unicode")
    return value


def boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        invalid(f"{name}: must be a boolean")
    return value


def query_bool(request: Request, name: str, default: bool | None = None) -> bool | None:
    value = request.query_params.get(name)
    if value is None:
        return default
    if value not in ("true", "false"):
        invalid(f"{name}: must be true or false")
    return value == "true"


def ids(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        invalid(f"{name}: must be an array of resource IDs")
    return list(dict.fromkeys(value))


def date_value(value: Any, name: str, *, instant: bool = False) -> str | None:
    if value is None:
        return None
    string(value, name, empty=False)
    try:
        if instant:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})", value):
                raise ValueError
            parse_instant(value)
        else:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            date.fromisoformat(value)
    except (ValueError, TypeError, OverflowError):
        invalid(f"{name}: invalid date")
    return value


def parse_fields(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        if any(not isinstance(item, str) for item in value):
            invalid("opt_fields must contain field names")
        value = ",".join(value)
    if not isinstance(value, str):
        invalid("opt_fields must be a comma-separated field list")
    expanded = []
    pending = value.split(",")
    while pending:
        path = pending.pop(0).strip().removeprefix("this.")
        group = re.search(r"\(([a-z_]+(?:\|[a-z_]+)+)\)", path)
        if group:
            pending.extend(path[: group.start()] + item + path[group.end() :] for item in group[1].split("|"))
            if len(pending) + len(expanded) > 100:
                invalid("opt_fields contains too many fields")
            continue
        parts = path.split(".")
        if not path or len(parts) > 8 or any(part not in FIELD_NAMES for part in parts):
            invalid(f"Invalid field: {path}")
        expanded.append(path)
    return list(dict.fromkeys(expanded))


def fields(request: Request) -> list[str] | None:
    if not hasattr(request.state, "asana_fields"):
        request.state.asana_fields = parse_fields(request.query_params.get("opt_fields"))
    return request.state.asana_fields


async def payload(request: Request) -> dict[str, Any]:
    fields(request)
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type == "application/x-www-form-urlencoded":
        form = await request.form()
        data: dict[str, Any] = {}
        arrays = {"projects", "members", "followers", "dependencies", "dependents", "tags"}
        booleans = {"completed", "archived", "public", "is_pinned", "liked", "is_rendered_as_separator"}
        for key in form:
            name = key.removesuffix("[]")
            value = form.get(key)
            if name == "opt_fields":
                request.state.asana_fields = parse_fields(value)
            elif name in arrays:
                data[name] = form.getlist(key) if key.endswith("[]") else str(value).split(",")
            elif name in booleans:
                if value not in ("true", "false"):
                    invalid(f"{name}: must be true or false")
                data[name] = value == "true"
            else:
                data[name] = value
        return data
    if media_type not in ("", "application/json"):
        invalid("Request body must be JSON or form-encoded")
    try:
        envelope = json.loads(await request.body())
        json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError):
        invalid("Invalid JSON body")
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict):
        invalid("data: must be an object")
    options = envelope.get("options", {})
    if not isinstance(options, dict):
        invalid("options: must be an object")
    if "fields" in options:
        request.state.asana_fields = parse_fields(options["fields"])
    return envelope["data"]


def user_id(ctx: ProviderContext, value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    for row in ctx.store["users"]:
        if value.lower() in (row["gid"], row["email"].lower(), row["_world"]):
            return row["gid"]
    return None


def principal(request: Request, ctx: ProviderContext, scope: str | None = None) -> Grant:
    grant = ctx.tokens.resolve(bearer_token(request))
    if grant is None or grant.app_only or user_id(ctx, grant.user_id) is None:
        raise error(401, "Not Authorized")
    if scope and "default" not in grant.scopes and scope not in grant.scopes:
        raise error(403, "You do not have the necessary permissions to perform this action.")
    fields(request)
    query_bool(request, "opt_pretty")
    return grant


def actor(ctx: ProviderContext, grant: Grant) -> str:
    result = user_id(ctx, grant.user_id)
    if result is None:
        raise error(401, "Not Authorized")
    return result


def find(ctx: ProviderContext, resource_id: str) -> dict[str, Any] | None:
    return next((row for name in COLLECTIONS if (row := ctx.store[name].get(resource_id))), None)


def allowed(
    ctx: ProviderContext, grant: Grant, row: dict[str, Any], seen: frozenset[str] = frozenset()
) -> bool:
    if row.get("_deleted") or row["gid"] in seen:
        return False
    seen |= {row["gid"]}
    uid = actor(ctx, grant)
    kind = row["resource_type"]
    if kind in ("user", "workspace", "tag"):
        return True
    if kind == "team":
        return row.get("visibility") == "public" or uid in row.get("_members", [])
    if kind == "project":
        if uid in row.get("_members", []) or (row.get("owner") or {}).get("gid") == uid:
            return True
        privacy = row.get("privacy_setting")
        team = ctx.store["teams"].get((row.get("team") or {}).get("gid"))
        return privacy == "public_to_workspace" or bool(
            privacy == "private_to_team" and team and uid in team.get("_members", [])
        )
    if kind == "task":
        if uid in {
            (row.get("assignee") or {}).get("gid"),
            *[u["gid"] for u in row.get("followers", [])],
        }:
            return True
        if any(
            allowed(ctx, grant, project, seen)
            for ref in row.get("projects", [])
            if (project := ctx.store["projects"].get(ref["gid"]))
        ):
            return True
        parent = ctx.store["tasks"].get((row.get("parent") or {}).get("gid"))
        return bool(parent and allowed(ctx, grant, parent, seen))
    if kind in ("section", "story", "attachment", "webhook"):
        if kind == "webhook" and row.get("_token") != grant.token:
            return False
        parent_ref = (
            row.get("project")
            if kind == "section"
            else row.get("resource")
            if kind == "webhook"
            else row.get("parent") or row.get("target")
        )
        parent = find(ctx, (parent_ref or {}).get("gid", ""))
        return bool(parent and allowed(ctx, grant, parent, seen))
    return False


def require(ctx: ProviderContext, grant: Grant, collection: str, resource_id: Any) -> dict[str, Any]:
    if not isinstance(resource_id, str) or not resource_id:
        invalid(f"{collection.rstrip('s')}: Not a valid ID")
    if collection == "users" and resource_id == "me":
        resource_id = actor(ctx, grant)
    row = ctx.store[collection].get(resource_id)
    if row is None or row.get("_deleted"):
        raise error(404, "Not Found")
    if not allowed(ctx, grant, row):
        raise error(403, "Forbidden")
    return row


def public(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [public(item) for item in value]
    return copy.deepcopy(value)


def full(request: Request, ctx: ProviderContext, grant: Grant, row: dict[str, Any]) -> dict[str, Any]:
    result = public(row)
    kind = row["resource_type"]
    if kind in ("task", "project", "team"):
        result["permalink_url"] = url_for_host(request, ctx, HOST, f"/0/{row['gid']}/{row['gid']}")
    if kind == "task":
        visible = {r["gid"] for r in ctx.store["projects"] if allowed(ctx, grant, r)}
        result["projects"] = [ref for ref in result["projects"] if ref["gid"] in visible]
        result["memberships"] = [m for m in result["memberships"] if m["project"]["gid"] in visible]
        result["num_subtasks"] = len(
            [
                r
                for r in ctx.store["tasks"]
                if not r.get("_deleted")
                and (r.get("parent") or {}).get("gid") == row["gid"]
                and allowed(ctx, grant, r)
            ]
        )
        result["dependencies"] = [
            ref
            for ref in result.get("dependencies", [])
            if (other := ctx.store["tasks"].get(ref["gid"])) and allowed(ctx, grant, other)
        ]
        result["dependents"] = [
            compact(other)
            for other in ctx.store["tasks"]
            if any(ref["gid"] == row["gid"] for ref in other.get("dependencies", []))
            and allowed(ctx, grant, other)
        ]
    if kind == "user":
        result["workspaces"] = [compact(w) for w in ctx.store["workspaces"]]
    if kind == "attachment":
        from emulators.providers.asana.attachments import download_url

        result["download_url"] = download_url(request, ctx, row)
        result["size"] = row["size"]
        result["view_url"] = result["download_url"]
        result["permanent_url"] = url_for_host(
            request, ctx, HOST, f"/0/{row['parent']['gid']}/{row['parent']['gid']}?attachment={row['gid']}"
        )
    return result


def render(
    request: Request, ctx: ProviderContext, grant: Grant, row: dict[str, Any], *, brief: bool = False
) -> dict[str, Any]:
    selected = fields(request)
    if selected is None:
        return compact(row) if brief else full(request, ctx, grant, row)
    tree: dict[str, Any] = {}
    for path in selected:
        node = tree
        for part in path.split("."):
            node = node.setdefault(part, {})

    def project(value: Any, node: dict[str, Any], depth: int = 0) -> Any:
        if isinstance(value, list):
            return [project(item, node, depth + 1) for item in value]
        if not isinstance(value, dict):
            return value
        resource = find(ctx, value["gid"]) if value.get("gid") else None
        if resource and depth < 9 and allowed(ctx, grant, resource):
            value = full(request, ctx, grant, resource)
        out = {"gid": value["gid"]} if "gid" in value else {}
        for key, child in node.items():
            if key in value:
                out[key] = project(value[key], child, depth + 1) if child else public(value[key])
        return out

    return project(full(request, ctx, grant, row), tree)


def page(request: Request, ctx: ProviderContext, grant: Grant, rows: list[dict[str, Any]]) -> dict[str, Any]:
    raw_limit = request.query_params.get("limit", "100")
    if not re.fullmatch(r"\d{1,3}", raw_limit) or not 1 <= int(raw_limit) <= 100:
        invalid("limit: must be between 1 and 100")
    limit = int(raw_limit)
    query = sorted(
        (key, value) for key, value in request.query_params.multi_items() if key not in ("offset", "limit")
    )
    path = request.scope["path"].removeprefix(request.scope.get("root_path", ""))
    binding = stable_hash(path, json.dumps(query), actor(ctx, grant), grant.client_id, grant.scopes)
    offset = request.query_params.get("offset")
    start = 0
    resource_ids = [row["gid"] for row in rows]
    if offset is not None:
        cursor = ctx.store["_cursors"].get(offset)
        if cursor is None or cursor["binding"] != binding:
            invalid("offset: The offset is invalid or has expired")
        start, resource_ids = cursor["start"], cursor["ids"]
    by_id = {row["gid"]: row for row in rows}
    selected = [by_id[rid] for rid in resource_ids[start : start + limit] if rid in by_id]
    next_page = None
    if start + limit < len(resource_ids):
        token = ctx.ids.urlsafe(40)
        ctx.store["_cursors"].put(
            {"id": token, "binding": binding, "start": start + limit, "ids": resource_ids}
        )
        params = [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key not in ("offset", "limit")
        ]
        params += [("limit", str(limit)), ("offset", token)]
        next_path = path + "?" + urlencode(params)
        next_page = {"offset": token, "path": next_path, "uri": url_for_host(request, ctx, HOST, next_path)}
    return {
        "data": [render(request, ctx, grant, row, brief=True) for row in selected],
        "next_page": next_page,
    }
