"""Directory security lifecycle and office resource reads."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response

from emulators.core.provider import ProviderContext
from emulators.providers.google.common import google_error, json_object, numeric_id
from emulators.providers.google.directory import BASE, admin, customer, lookup, page


def matches_user(grant: dict[str, Any], user: dict[str, Any]) -> bool:
    person = user.get("_person")
    identifiers = {user["id"], user["primaryEmail"]}
    if person:
        identifiers.add(person)
    return grant.get("user_id") in identifiers or bool(
        person and grant.get("extra", {}).get("sub_person") == person
    )


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.post(BASE + "/users/{user_key}/signOut")
    async def sign_out(request: Request, user_key: str) -> Response:
        admin(request, ctx)
        user = lookup(ctx, "directory_users", user_key)
        for grant in ctx.store["_grants"]:
            if matches_user(grant, user):
                ctx.tokens.revoke(grant["id"])
        return Response(status_code=204)

    @app.get(BASE + "/users/{user_key}/tokens")
    async def tokens(request: Request, user_key: str) -> dict[str, Any]:
        admin(request, ctx)
        user = lookup(ctx, "directory_users", user_key)
        clients: dict[str, dict[str, Any]] = {}
        for grant in ctx.store["_grants"]:
            if grant.get("revoked") or not matches_user(grant, user):
                continue
            key = grant["client_id"]
            record = clients.setdefault(
                key,
                {
                    "kind": "admin#directory#token",
                    "clientId": key,
                    "displayText": key,
                    "anonymous": False,
                    "nativeApp": False,
                    "userKey": user["id"],
                    "scopes": [],
                },
            )
            record["scopes"] = sorted(set(record["scopes"]) | set(grant["scopes"]))
        return {"kind": "admin#directory#tokenList", "items": list(clients.values())}

    @app.delete(BASE + "/users/{user_key}/tokens/{client_id}")
    async def revoke_token(request: Request, user_key: str, client_id: str) -> Response:
        admin(request, ctx)
        user = lookup(ctx, "directory_users", user_key)
        found = False
        for grant in ctx.store["_grants"]:
            if grant["client_id"] == client_id and matches_user(grant, user):
                found = True
                ctx.tokens.revoke(grant["id"])
        if not found:
            raise google_error(404, "Resource Not Found: clientId", reason="notFound")
        return Response(status_code=204)

    @app.post(BASE + "/users/{user_key}/undelete")
    async def undelete(request: Request, user_key: str) -> Response:
        admin(request, ctx)
        user = ctx.store["directory_users"].get(user_key)
        if not user or not user.get("_deleted"):
            raise google_error(404, "Resource Not Found: userKey", reason="notFound")
        body = await json_object(request)
        path = body.get("orgUnitPath", "/")
        if path not in ctx.store["directory_orgunits"]:
            raise google_error(400, "Invalid Input: orgUnitPath", reason="invalid")
        user.pop("_deleted", None)
        user.pop("deletionTime", None)
        user["orgUnitPath"] = path
        return Response(status_code=204)

    def buildings() -> list[dict[str, Any]]:
        return [
            {
                "kind": "admin#directory#resources#buildings#Building",
                "buildingId": office.id,
                "buildingName": office.name,
                "description": office.name,
                "floorNames": ["1"],
                "address": {
                    "addressLines": [office.address.street],
                    "locality": office.address.city,
                    "administrativeArea": office.address.state,
                    "postalCode": office.address.postal_code,
                    "regionCode": office.address.country_code,
                },
            }
            for office in ctx.world.offices
        ]

    @app.get(BASE + "/customer/{customer_id}/resources/buildings")
    async def buildings_list(request: Request, customer_id: str) -> dict[str, Any]:
        admin(request, ctx)
        customer(ctx, customer_id)
        return page(
            request,
            buildings(),
            "buildings",
            "admin#directory#resources#buildings#Buildings",
            default=100,
            maximum=500,
        )

    @app.get(BASE + "/customer/{customer_id}/resources/buildings/{building_id}")
    async def building_get(request: Request, customer_id: str, building_id: str) -> dict[str, Any]:
        admin(request, ctx)
        customer(ctx, customer_id)
        result = next((r for r in buildings() if r["buildingId"] == building_id), None)
        if not result:
            raise google_error(404, "Resource Not Found: buildingId", reason="notFound")
        return result

    @app.get(BASE + "/customer/{customer_id}/resources/calendars/{resource_id}")
    async def resource_get(request: Request, customer_id: str, resource_id: str) -> dict[str, Any]:
        admin(request, ctx)
        customer(ctx, customer_id)
        for office in ctx.world.offices:
            for name, capacity in (("Boardroom", 12), ("Meeting Room", 6)):
                rid = numeric_id(ctx.world, "room", office.id, name)
                if resource_id == rid:
                    city = office.address.city
                    return {
                        "kind": "admin#directory#resources#calendars#CalendarResource",
                        "resourceId": rid,
                        "resourceName": f"{city} - {name} ({capacity})",
                        "resourceEmail": f"{rid}@resources.halcyonadvisory.example",
                        "resourceDescription": f"{name} at {office.name}",
                        "resourceType": "Conference Room",
                        "resourceCategory": "CONFERENCE_ROOM",
                        "capacity": capacity,
                        "buildingId": office.id,
                        "floorName": "1",
                        "generatedResourceName": f"{city} - {name} ({capacity})",
                    }
        raise google_error(404, "Resource Not Found: calendarResourceId", reason="notFound")
