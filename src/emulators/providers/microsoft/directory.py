"""Entra users, groups, organization and Graph JSON batching."""

from __future__ import annotations

import copy
import uuid
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import Response

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import batch
from emulators.providers.microsoft import common as c
from emulators.providers.microsoft import directory_support as s
from emulators.world import documents

DEFAULT_USER = (
    "businessPhones",
    "displayName",
    "givenName",
    "id",
    "jobTitle",
    "mail",
    "mobilePhone",
    "officeLocation",
    "preferredLanguage",
    "surname",
    "userPrincipalName",
)
E3 = "05e9a617-0261-4cee-bb44-138d3ef5d965"
E5 = "06ebc4ee-1bb5-47dd-8120-11324bc54e06"
EXCHANGE = "9aaf7827-d63c-4b61-89c3-182f06f82e5c"


def seed(ctx: ProviderContext) -> None:
    world = ctx.world
    guests = {cl.primary_contact_id for cl in world.clients if cl.status == "Active"}
    for person in world.people:
        if person.is_external and person.id not in guests:
            continue
        office = world.office(person.office_id) if person.office_id else None
        address = office.address if office else world.org.address
        guest = person.is_external
        license_id = E5 if person.is_partner or person.is_admin else E3
        plan = {
            "assignedDateTime": isoformat_z(world.anchor),
            "capabilityStatus": "Enabled",
            "service": "exchange",
            "servicePlanId": EXCHANGE,
        }
        record = {
            "id": c.user_id(world, person.id),
            "businessPhones": [person.phone_business] if person.phone_business else [],
            "displayName": person.display_name,
            "givenName": person.first_name,
            "surname": person.last_name,
            "jobTitle": person.job_title,
            "mail": person.email,
            "mobilePhone": person.phone_mobile,
            "officeLocation": office.name if office else None,
            "preferredLanguage": person.locale,
            "userPrincipalName": c.upn(world, person),
            "accountEnabled": person.active,
            "department": world.department(person.department_id).name if person.department_id else None,
            "employeeId": person.employee_id,
            "employeeHireDate": f"{person.hire_date}T00:00:00Z" if person.hire_date else None,
            "companyName": world.client(person.company_id).name if guest else world.org.name,
            "city": address.city,
            "state": address.state,
            "country": address.country,
            "streetAddress": address.street,
            "postalCode": address.postal_code,
            "usageLocation": address.country_code,
            "createdDateTime": f"{person.hire_date or world.org.founded}T00:00:00Z",
            "onPremisesSyncEnabled": None,
            "proxyAddresses": [f"SMTP:{person.email}"],
            "mailNickname": person.username,
            "userType": "Guest" if guest else "Member",
            "ageGroup": "Adult",
            "assignedLicenses": []
            if guest or not person.active
            else [{"disabledPlans": [], "skuId": license_id}],
            "assignedPlans": [] if guest or not person.active else [plan],
            "identities": [
                {
                    "signInType": "userPrincipalName",
                    "issuer": world.org.domain,
                    "issuerAssignedId": c.upn(world, person),
                }
            ],
            "otherMails": [person.email] if guest else [],
            "lastPasswordChangeDateTime": isoformat_z(world.anchor),
            "creationType": "Invitation" if guest else None,
            "externalUserState": "Accepted" if guest else None,
        }
        ctx.store["users"].put(record)
    ctx.store.meta["ms_people"] = {c.user_id(world, p.id): p.id for p in world.people}
    ctx.store.meta["ms_managers"] = {
        c.user_id(world, p.id): c.user_id(world, p.manager_id) for p in world.employees if p.manager_id
    }
    for group in world.groups:
        unified = group.kind in ("department", "all_staff", "project")
        ctx.store["groups"].put(
            {
                "id": c.group_id(world, group.id),
                "displayName": group.name,
                "description": group.description,
                "mail": group.email if group.kind != "role" else None,
                "mailNickname": group.email.split("@")[0],
                "mailEnabled": group.kind != "role",
                "securityEnabled": group.kind == "role",
                "groupTypes": ["Unified"] if unified else [],
                "visibility": group.visibility,
                "createdDateTime": isoformat_z(world.anchor),
                "resourceProvisioningOptions": ["Team"]
                if any(t.group_id == group.id for t in world.teams)
                else [],
                "renewedDateTime": isoformat_z(world.anchor),
            }
        )
        ctx.store.meta.setdefault("ms_group_members", {})[c.group_id(world, group.id)] = [
            c.user_id(world, pid) for pid in group.member_ids
        ]
        ctx.store.meta.setdefault("ms_group_owners", {})[c.group_id(world, group.id)] = [
            c.user_id(world, pid) for pid in group.owner_ids
        ]


class GraphHeaders:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        async def with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in (b"request-id", b"client-request-id")
                ]
                rid = str(uuid.uuid4()).encode()
                client_id = next(
                    (v for k, v in scope.get("headers", []) if k.lower() == b"client-request-id"), rid
                )
                message["headers"] = [*headers, (b"request-id", rid), (b"client-request-id", client_id)]
            await send(message)

        await self.app(scope, receive, with_headers)


def expanded(ctx: ProviderContext, request: Request, record: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(record)
    expand = request.query_params.get("$expand", "")
    if expand.startswith("manager"):
        current = result
        uid = record["id"]
        seen = {uid}
        select = (
            expand.split("$select=", 1)[1].split(")")[0].split(";")[0].split(",")
            if "$select=" in expand
            else None
        )
        while mid := ctx.store.meta["ms_managers"].get(uid):
            if mid in seen:
                break
            seen.add(mid)
            manager = ctx.store["users"].get(mid)
            if not manager:
                break
            current["manager"] = {
                k: v for k, v in manager.items() if k in (select or DEFAULT_USER) or k == "id"
            }
            current = current["manager"]
            uid = mid
            if "$levels=max" not in expand:
                break
    return result


def advanced(request: Request) -> None:
    if (
        "$search" in request.query_params or request.query_params.get("$count") == "true"
    ) and request.headers.get("ConsistencyLevel", "").lower() != "eventual":
        raise c.graph_error(
            400, "Request_UnsupportedQuery", "Advanced queries require ConsistencyLevel: eventual."
        )


def object_record(ctx: ProviderContext, identifier: str) -> dict[str, Any]:
    for name, kind in (("users", "user"), ("groups", "group")):
        record = ctx.store[name].get(identifier)
        if record:
            return {"@odata.type": f"#microsoft.graph.{kind}", **record}
    raise c.not_found(identifier)


def group_members(ctx: ProviderContext, gid: str, transitive: bool = False) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    def walk(group: str) -> None:
        for uid in ctx.store.meta["ms_group_members"].get(group, []):
            if uid not in result:
                result[uid] = object_record(ctx, uid)
                if transitive and uid in ctx.store["groups"]:
                    walk(uid)

    walk(gid)
    return list(result.values())


def memberships(ctx: ProviderContext, uid: str) -> list[dict[str, Any]]:
    found = {uid}
    changed = True
    while changed:
        before = len(found)
        found.update(
            g for g, members in ctx.store.meta["ms_group_members"].items() if found.intersection(members)
        )
        changed = len(found) != before
    return [object_record(ctx, g) for g in found if g != uid and g in ctx.store["groups"]]


def register(app: FastAPI, ctx: ProviderContext) -> None:
    # Include shared fault responses in Graph's correlation headers.
    build_stack = app.build_middleware_stack
    app.build_middleware_stack = lambda: GraphHeaders(build_stack())
    router = APIRouter()

    @router.post("/$batch")
    async def batch_route(request: Request) -> dict[str, Any]:
        return await batch.execute(request, ctx)

    @router.get("/organization")
    async def organization(request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        a = ctx.world.org.address
        return s.collection(
            request,
            [
                {
                    "id": c.tenant_id(ctx.world),
                    "displayName": ctx.world.org.name,
                    "verifiedDomains": [
                        {
                            "name": ctx.world.org.domain,
                            "isDefault": True,
                            "isInitial": False,
                            "capabilities": "Email, OfficeCommunicationsOnline",
                        },
                        {
                            "name": f"{ctx.world.org.slug}.onmicrosoft.com",
                            "isDefault": False,
                            "isInitial": True,
                            "capabilities": "Email, OfficeCommunicationsOnline",
                        },
                    ],
                    "city": a.city,
                    "country": a.country,
                    "countryLetterCode": a.country_code,
                    "state": a.state,
                    "street": a.street,
                    "postalCode": a.postal_code,
                    "businessPhones": [ctx.world.org.phone],
                    "assignedPlans": [
                        {
                            "assignedDateTime": isoformat_z(ctx.world.anchor),
                            "capabilityStatus": "Enabled",
                            "service": "exchange",
                            "servicePlanId": EXCHANGE,
                        }
                    ],
                }
            ],
            "organization",
        )

    @router.get("/domains")
    async def domains(request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        return s.collection(
            request,
            [
                {
                    "id": name,
                    "authenticationType": "Managed",
                    "isVerified": True,
                    "isDefault": index == 0,
                    "isInitial": index == 1,
                    "supportedServices": ["Email", "OfficeCommunicationsOnline"],
                    "availabilityStatus": "Available",
                    "state": None,
                    "isAdminManaged": True,
                    "isRoot": True,
                }
                for index, name in enumerate((ctx.world.org.domain, f"{ctx.world.org.slug}.onmicrosoft.com"))
            ],
            "domains",
        )

    @router.get("/subscribedSkus")
    async def skus(request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        return s.collection(
            request,
            [
                {
                    "id": f"{c.tenant_id(ctx.world)}_{sku}",
                    "skuId": sku,
                    "skuPartNumber": name,
                    "capabilityStatus": "Enabled",
                    "appliesTo": "User",
                    "consumedUnits": sum(
                        any(license["skuId"] == sku for license in u["assignedLicenses"])
                        for u in ctx.store["users"]
                    ),
                    "prepaidUnits": {"enabled": 100, "suspended": 0, "warning": 0, "lockedOut": 0},
                    "servicePlans": [
                        {
                            "servicePlanId": EXCHANGE,
                            "servicePlanName": "EXCHANGE_S_ENTERPRISE",
                            "provisioningStatus": "Success",
                            "appliesTo": "User",
                        }
                    ],
                }
                for sku, name in ((E3, "SPE_E3"), (E5, "SPE_E5"))
            ],
            "subscribedSkus",
        )

    @router.get("/users/delta")
    async def users_delta(request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        return s.delta(request, ctx, ctx.store["users"].values(), "users", "users")

    @router.get("/groups/delta")
    async def groups_delta(request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        return s.delta(request, ctx, ctx.store["groups"].values(), "groups", "groups")

    @router.get("/users")
    async def users(request: Request) -> dict[str, Any]:
        caller = c.principal(request, ctx)
        if caller.delegated and caller.user_id and s.user(ctx, caller.user_id).get("userType") == "Guest":
            raise c.access_denied()
        advanced(request)
        return s.collection(
            request, [expanded(ctx, request, r) for r in ctx.store["users"]], "users", defaults=DEFAULT_USER
        )

    @router.post("/users", status_code=201)
    async def create_user(request: Request) -> dict[str, Any]:
        s.admin(request, ctx)
        body = await request.json()
        if not all(
            k in body
            for k in ("accountEnabled", "displayName", "mailNickname", "userPrincipalName", "passwordProfile")
        ):
            raise c.graph_error(400, "Request_BadRequest", "Required user properties are missing.")
        if any(
            u["userPrincipalName"].casefold() == body["userPrincipalName"].casefold()
            for u in ctx.store["users"]
        ):
            raise c.graph_error(
                400,
                "Request_BadRequest",
                "Another object with the same value for property userPrincipalName already exists.",
            )
        record = {k: None for k in DEFAULT_USER}
        record.update(
            {
                "businessPhones": [],
                "userType": "Member",
                "assignedLicenses": [],
                "assignedPlans": [],
                **{k: v for k, v in body.items() if k not in ("id", "passwordProfile")},
                "id": ctx.ids.guid(),
            }
        )
        ctx.store["users"].put(record)
        await c.publish_change(ctx, f"users/{record['id']}", "created", record)
        return s.single(request, record, "users/$entity")

    @router.get("/me")
    @router.get("/users/{identifier}")
    async def get_user(request: Request, identifier: str = "me") -> dict[str, Any]:
        p = c.principal(request, ctx)
        uid = c.me_user_id(p) if identifier == "me" else identifier
        record = expanded(ctx, request, s.user(ctx, uid))
        if not request.query_params.get("$select"):
            record = {k: v for k, v in record.items() if k in DEFAULT_USER or k == "manager"}
        result = s.single(request, record, "users/$entity")
        if "manager" in record:
            result["manager"] = record["manager"]
        return result

    @router.patch("/users/{identifier}")
    async def patch_user(identifier: str, request: Request) -> Response:
        s.admin(request, ctx)
        record = s.user(ctx, identifier)
        record.update({k: v for k, v in (await request.json()).items() if k not in ("id", "passwordProfile")})
        await c.publish_change(ctx, f"users/{record['id']}", "updated", record)
        return Response(status_code=204)

    @router.delete("/users/{identifier}")
    async def delete_user(identifier: str, request: Request) -> Response:
        s.admin(request, ctx)
        record = s.user(ctx, identifier)
        ctx.store["users"].delete(record["id"])
        for values in ctx.store.meta["ms_group_members"].values():
            if record["id"] in values:
                values.remove(record["id"])
        await c.publish_change(ctx, f"users/{record['id']}", "deleted", record)
        return Response(status_code=204)

    async def user_relation(request: Request, identifier: str = "me") -> Any:
        caller = c.principal(request, ctx)
        record = s.user(ctx, c.me_user_id(caller) if identifier == "me" else identifier)
        uid = record["id"]
        kind = s.local_path(request).split("/")[-1]
        if kind == "manager":
            mid = ctx.store.meta["ms_managers"].get(uid)
            if not mid or not ctx.store["users"].get(mid):
                raise c.not_found("manager")
            return s.single(request, ctx.store["users"].get(mid), "directoryObjects/$entity")
        if kind == "directReports":
            rows = [
                object_record(ctx, k)
                for k, v in ctx.store.meta["ms_managers"].items()
                if v == uid and k in ctx.store["users"]
            ]
        elif kind == "ownedObjects":
            rows = [object_record(ctx, k) for k, v in ctx.store.meta["ms_group_owners"].items() if uid in v]
        elif kind == "memberOf":
            rows = [object_record(ctx, k) for k, v in ctx.store.meta["ms_group_members"].items() if uid in v]
        else:
            rows = memberships(ctx, uid)
        return s.collection(request, rows, "directoryObjects")

    for relation_name in ("manager", "directReports", "memberOf", "transitiveMemberOf", "ownedObjects"):
        router.add_api_route(f"/me/{relation_name}", user_relation, methods=["GET"])
        router.add_api_route(f"/users/{{identifier}}/{relation_name}", user_relation, methods=["GET"])

    async def photo(request: Request, identifier: str = "me") -> Any:
        caller = c.principal(request, ctx)
        record = s.user(ctx, c.me_user_id(caller) if identifier == "me" else identifier)
        pid = ctx.store.meta["ms_people"].get(record["id"], "")
        if not pid or pid.endswith(("7", "0")):
            raise c.graph_error(404, "ImageNotFound", "The photo wasn't found.")
        path = s.local_path(request)
        if path.endswith("/$value"):
            key = ctx.store.meta.setdefault("ms_photo_blobs", {}).get(record["id"])
            if not key:
                key = ctx.store.blobs.put(documents.make_png(seed=int(record["id"].replace("-", "")[:8], 16)))
                ctx.store.meta["ms_photo_blobs"][record["id"]] = key
            return Response(ctx.store.blobs.get(key), media_type="image/png")
        metadata = {"id": "64X64", "height": 64, "width": 64, "@odata.mediaContentType": "image/png"}
        return (
            s.collection(request, [metadata], "users('" + record["id"] + "')/photos")
            if path.endswith("/photos")
            else metadata
        )

    for suffix in ("photo", "photo/$value", "photos", "photos/{size}", "photos/{size}/$value"):
        router.add_api_route(f"/me/{suffix}", photo, methods=["GET"])
        router.add_api_route(f"/users/{{identifier}}/{suffix}", photo, methods=["GET"])

    @router.post("/users/{identifier}/checkMemberGroups")
    @router.post("/users/{identifier}/getMemberGroups")
    async def check_groups(identifier: str, request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        record = s.user(ctx, identifier)
        body = await request.json()
        groups = memberships(ctx, record["id"])
        if request.url.path.endswith("checkMemberGroups"):
            groups = [g for g in groups if g["id"] in body.get("groupIds", [])]
        elif body.get("securityEnabledOnly"):
            groups = [g for g in groups if g.get("securityEnabled")]
        return {
            "@odata.context": c.context_url(request, "Collection(Edm.String)"),
            "value": [g["id"] for g in groups],
        }

    @router.post("/directoryObjects/getByIds")
    async def get_by_ids(request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        body = await request.json()
        rows = []
        for identifier in body.get("ids", []):
            if identifier in ctx.store["users"] or identifier in ctx.store["groups"]:
                record = object_record(ctx, identifier)
                if not body.get("types") or record["@odata.type"].split(".")[-1] in body["types"]:
                    rows.append(record)
        return s.collection(request, rows, "directoryObjects")

    @router.get("/directoryObjects/{identifier}")
    async def directory_object(identifier: str, request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        return s.single(request, object_record(ctx, identifier), "directoryObjects/$entity")

    @router.get("/groups")
    async def groups(request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        advanced(request)
        return s.collection(request, ctx.store["groups"].values(), "groups")

    @router.get("/groups/{gid}")
    async def get_group(gid: str, request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        record = ctx.store["groups"].get(gid)
        if not record:
            raise c.not_found(gid)
        return s.single(request, record, "groups/$entity")

    async def members(request: Request, gid: str) -> dict[str, Any]:
        c.principal(request, ctx)
        if gid not in ctx.store["groups"]:
            raise c.not_found(gid)
        kind = request.url.path.split("/")[-1]
        rows = (
            [object_record(ctx, uid) for uid in ctx.store.meta["ms_group_owners"].get(gid, [])]
            if kind == "owners"
            else group_members(ctx, gid, kind == "transitiveMembers")
        )
        return s.collection(request, rows, "directoryObjects")

    for suffix in ("members", "owners", "transitiveMembers"):
        router.add_api_route(f"/groups/{{gid}}/{suffix}", members, methods=["GET"])

    async def change_members(request: Request, gid: str, uid: str | None = None) -> Response:
        caller = c.principal(request, ctx)
        if gid not in ctx.store["groups"]:
            raise c.not_found(gid)
        if caller.user_id not in ctx.store.meta["ms_group_owners"].get(gid, []):
            s.admin(request, ctx)
        values = ctx.store.meta["ms_group_members"][gid]
        if request.method == "POST":
            body = await request.json()
            uid = str(body.get("@odata.id", "")).rsplit("/", 1)[-1]
            object_record(ctx, uid)
            if uid in values:
                raise c.graph_error(
                    400, "Request_BadRequest", "One or more added object references already exist."
                )
            values.append(uid)
        elif uid in values:
            values.remove(uid)
        else:
            raise c.not_found(str(uid))
        await c.publish_change(ctx, f"groups/{gid}", "updated", ctx.store["groups"].get(gid))
        return Response(status_code=204)

    router.add_api_route("/groups/{gid}/members/$ref", change_members, methods=["POST"])
    router.add_api_route("/groups/{gid}/members/{uid}/$ref", change_members, methods=["DELETE"])
    for version in c.GRAPH_VERSIONS:
        app.include_router(router, prefix=f"/{version}")
