"""Read-only base schemas and current-token identity."""

from __future__ import annotations

import copy
from typing import Any

from fastapi import FastAPI, Request

from emulators.core.provider import ProviderContext

from .common import authenticate, base_access, invalid, public, role


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/v0/meta/whoami")
    async def whoami(request: Request) -> dict[str, Any]:
        grant = authenticate(ctx, request)
        result = {"id": grant.user_id, "scopes": grant.scopes}
        if "user.email:read" in grant.scopes:
            result["email"] = ctx.store.collection("users").get(grant.user_id)["email"]
        return result

    @app.get("/v0/meta/bases")
    async def bases(request: Request) -> dict[str, Any]:
        grant = authenticate(ctx, request, "schema.bases:read")
        if request.query_params:
            invalid("The base iterator is not available.", "LIST_BASES_ITERATOR_NOT_AVAILABLE")
        return {
            "bases": [
                {"id": b["id"], "name": b["name"], "permissionLevel": role(ctx, grant, b)}
                for b in ctx.store.collection("bases")
                if role(ctx, grant, b) != "none"
            ]
        }

    @app.get("/v0/meta/bases/{base_id}/tables")
    async def tables(request: Request, base_id: str) -> dict[str, Any]:
        base_access(ctx, request, base_id, "schema.bases:read")
        includes = request.query_params.getlist("include[]")
        if set(request.query_params) - {"include[]"} or any(v != "visibleFieldIds" for v in includes):
            invalid("Unsupported schema include option.")
        result = [copy.deepcopy(public(t)) for t in ctx.store.collection("tables") if t["_base"] == base_id]
        if includes:
            for table in result:
                for view in table["views"]:
                    view["visibleFieldIds"] = [f["id"] for f in table["fields"]]
        return {"tables": result}
