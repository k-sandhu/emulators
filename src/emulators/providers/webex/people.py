"""Organization directory and hierarchy in Webex's public wire shape."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.provider import ProviderContext, url_for_host
from emulators.world.documents import make_png

from .common import authenticate, fail, is_admin, page, person, public, require_scope, subject


def register(app: FastAPI, ctx: ProviderContext) -> None:
    def wire(request: Request, row: dict[str, Any]) -> dict[str, Any]:
        result = {
            **public(row),
            "avatar": url_for_host(request, ctx, "webexapis.com", f"/avatars/{row['id']}.png"),
        }
        grant = authenticate(request, ctx)
        user = person(ctx, grant.user_id)
        same_org = row["orgId"] == (
            user["orgId"] if user else ctx.store.collection("organizations").values()[0]["id"]
        )
        if (
            not same_org
            or request.query_params.get("id")
            or request.query_params.get("excludeStatus", "false").lower() == "true"
        ):
            result.pop("status", None)
            result.pop("lastActivity", None)
        if not is_admin(ctx, grant, "spark-admin:people_read") and row["id"] != grant.user_id:
            result.pop("roles", None)
            result.pop("licenses", None)
        return result

    @app.get("/v1/people/me")
    async def me(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        require_scope(
            grant, "spark:all", "spark:people_read", "spark:people_read_self", "spark-admin:people_read"
        )
        return wire(request, subject(ctx, grant))

    @app.get("/v1/people")
    async def people(request: Request) -> JSONResponse:
        grant = authenticate(request, ctx)
        require_scope(grant, "spark:all", "spark:people_read", "spark-admin:people_read")
        query = request.query_params
        if not any(query.get(k) for k in ("email", "displayName", "id")) and not is_admin(
            ctx, grant, "spark-admin:people_read"
        ):
            fail(403, "An administrator scope is required to list all people.")
        if grant.app_only and not is_admin(ctx, grant, "spark-admin:people_read"):
            fail(403, "A service app requires spark-admin:people_read.")
        rows = ctx.store.collection("people").values()
        if query.get("orgId"):
            own_org = ctx.store.collection("organizations").values()[0]["id"]
            if query["orgId"] != own_org:
                fail(403, "The requested organization is not managed by this identity.")
            rows = [p for p in rows if p["orgId"] == query["orgId"]]
        if query.get("email"):
            rows = [p for p in rows if query["email"].lower() in [e.lower() for e in p["emails"]]]
        if query.get("displayName"):
            rows = [p for p in rows if p["displayName"].lower().startswith(query["displayName"].lower())]
        if query.get("id"):
            if len(query["id"].split(",")) > 85:
                fail(400, "At most 85 person IDs can be requested.")
            rows = [p for p in rows if p["id"] in query["id"].split(",")]
        if query.get("roles"):
            if not is_admin(ctx, grant, "spark-admin:people_read"):
                fail(403, "Filtering by roles requires administrator access.")
            rows = [p for p in rows if set(p["roles"]) & set(query["roles"].split(","))]
        if query.get("excludeStatus", "false").lower() not in ("true", "false"):
            fail(400, "excludeStatus must be true or false.")
        # External contacts remain searchable, but aren't members of the Halcyon organization.
        if not any(query.get(k) for k in ("email", "displayName", "id")):
            rows = [p for p in rows if not p.get("_external")]
        return page(request, [wire(request, p) for p in rows])

    @app.get("/v1/people/{person_id}")
    async def get_person(person_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        require_scope(grant, "spark:all", "spark:people_read", "spark-admin:people_read")
        if grant.app_only and not is_admin(ctx, grant, "spark-admin:people_read"):
            fail(403, "A service app requires spark-admin:people_read.")
        row = ctx.store.collection("people").get(person_id)
        if row is None:
            fail(404)
        return wire(request, row)

    @app.get("/avatars/{person_id}.png")
    async def avatar(person_id: str) -> Response:
        if person(ctx, person_id) is None:
            fail(404)
        cache = ctx.store.collection("_avatars")
        row = cache.get(person_id)
        if not row:
            row = cache.put(
                {"id": person_id, "blob": ctx.store.blobs.put(make_png(seed=sum(person_id.encode())))}
            )
        return Response(ctx.store.blobs.get(row["blob"]), media_type="image/png")

    def directory(collection: str) -> None:
        async def listing(request: Request) -> JSONResponse:
            grant = authenticate(request, ctx)
            if not is_admin(ctx, grant, f"spark-admin:{collection}_read"):
                fail(403, "An administrator scope is required for this resource.")
            return page(request, ctx.store.collection(collection).values())

        app.add_api_route(f"/v1/{collection}", listing, methods=["GET"])

    for collection in ("organizations", "roles", "licenses"):
        directory(collection)
