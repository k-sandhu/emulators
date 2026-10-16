"""Workspace users and title search, scoped to the calling integration."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request

from emulators.core.provider import ProviderContext
from emulators.providers.notion.auth import bot, capability
from emulators.providers.notion.common import (
    NEW,
    allowed,
    ancestors,
    authenticate,
    body,
    fail,
    invalid,
    paginate,
    public,
    serialize,
    title_of,
    uid,
    version,
)


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/v1/users/me")
    async def me(request: Request) -> dict[str, Any]:
        return bot(ctx, authenticate(request, ctx))

    def user_wire(record: dict[str, Any], scopes: list[str]) -> dict[str, Any]:
        result = public(record)
        if "read_user_with_email" not in scopes and result.get("type") == "person":
            result["person"] = {}
        return result

    @app.get("/v1/users")
    async def users(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        if not any(s in grant.scopes for s in ("read_user", "read_user_with_email")):
            fail(403, "restricted_resource", "This integration does not have user information capabilities.")
        result = paginate(
            ctx, ctx.store["users"].values(), request.query_params, kind="user", key="users", grant=grant
        )
        result["results"] = [user_wire(r, grant.scopes) for r in result["results"]]
        return result

    @app.get("/v1/users/{user_id}")
    async def user(user_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        if not any(s in grant.scopes for s in ("read_user", "read_user_with_email")):
            fail(403, "restricted_resource", "This integration does not have user information capabilities.")
        record = ctx.store["users"].get(uid(user_id))
        if record is None:
            fail(404, "object_not_found", f"Could not find user with ID: {user_id}.")
        return user_wire(record, grant.scopes)

    @app.post("/v1/search")
    async def search(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        capability(grant, "read_content")
        payload = await body(request)
        query = payload.get("query", "")
        if not isinstance(query, str):
            invalid("query must be a string.")
        target = payload.get("filter")
        data_type = "data_source" if version(request) == NEW else "database"
        if "filter" in payload and (
            not isinstance(target, dict)
            or target.get("property") != "object"
            or target.get("value") not in ("page", data_type)
        ):
            invalid(f"filter must have property object and value page or {data_type}.")
        collection = "data_sources" if version(request) == NEW else "databases"
        rows = [
            record
            for record in [*ctx.store["pages"], *ctx.store[collection]]
            if (not target or target["value"] == record["object"])
            and query.casefold() in title_of(record).casefold()
            and not any(r.get("archived") for r in ancestors(ctx, record))
            and allowed(ctx, grant, record)
        ]
        sort = payload.get("sort", {"timestamp": "last_edited_time", "direction": "descending"})
        if (
            not isinstance(sort, dict)
            or sort.get("timestamp") != "last_edited_time"
            or sort.get("direction") not in ("ascending", "descending")
        ):
            invalid("sort requires timestamp last_edited_time and direction ascending or descending.")
        rows.sort(key=lambda r: (r["last_edited_time"], r["id"]), reverse=sort["direction"] == "descending")
        binding = "search:" + json.dumps(
            {"query": query, "filter": target, "sort": sort, "version": version(request)}, sort_keys=True
        )
        list_kind = "page_or_data_source" if version(request) == NEW else "page_or_database"
        result = paginate(ctx, rows, payload, kind=list_kind, key=binding, grant=grant)
        result["results"] = [serialize(request, ctx, r, grant) for r in result["results"]]
        return result
