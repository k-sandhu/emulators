"""Entra users, groups, organization and Graph JSON batching."""

from __future__ import annotations

import copy
import re
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import Response

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import batch
from emulators.providers.microsoft import common as c
from emulators.providers.microsoft import directory_support as s
from emulators.providers.microsoft.query_validation import PROPERTIES
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
DEFAULT_GROUP = tuple(
    sorted(
        PROPERTIES["groups"]
        - {
            "allowExternalSenders",
            "assignedLabels",
            "assignedLicenses",
            "autoSubscribeNewMembers",
            "hasMembersWithLicenseErrors",
            "hideFromAddressLists",
            "hideFromOutlookClients",
            "isArchived",
            "isSubscribedByMail",
            "unseenCount",
            "isManagementRestricted",
            "members",
            "owners",
            "memberOf",
            "transitiveMembers",
        }
    )
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
    def __init__(self, app: Any, ctx: ProviderContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Normalize OData's equivalent key addressing before Starlette matches routes.
        scope["path"] = re.sub(
            r"\b(users|groups|mailFolders|contactFolders|calendars)\('((?:''|[^'])*)'\)",
            lambda m: m[1] + "/" + m[2].replace("''", "'"),
            scope["path"],
        )
        rid, client_id = c.request_ids(scope)
        scope["state"]["graph_now"] = isoformat_z(self.ctx.clock.now())
        token = c.REQUEST_SCOPE.set(scope)

        async def with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in (b"request-id", b"client-request-id")
                ]
                message["headers"] = [
                    *headers,
                    (b"request-id", rid.encode()),
                    (b"client-request-id", client_id.encode()),
                ]
                preferences = scope.get("state", {}).get("graph_preferences", [])
                if preferences:
                    message["headers"].append((b"preference-applied", ", ".join(preferences).encode()))
            await send(message)

        try:
            await self.app(scope, receive, with_headers)
        finally:
            c.REQUEST_SCOPE.reset(token)


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
                "@odata.type": "#microsoft.graph.user",
                **{k: v for k, v in manager.items() if k in (select or DEFAULT_USER) or k == "id"},
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


def project_object(request: Request, record: dict[str, Any]) -> dict[str, Any]:
    if request.query_params.get("$select"):
        return record
    defaults = DEFAULT_USER if record.get("@odata.type") == "#microsoft.graph.user" else DEFAULT_GROUP
    return {k: v for k, v in record.items() if k in defaults or k.startswith("@")}


def object_collection(request: Request, rows: list[dict[str, Any]]) -> Any:
    segments = s.local_path(request).split("/")
    cast = next((part for part in segments if part.startswith("microsoft.graph.")), None)
    advanced(request)
    needs_index = cast or any(key in request.query_params for key in ("$filter", "$orderby", "$search"))
    if (
        needs_index
        and segments[-1] != "$count"
        and (
            request.headers.get("ConsistencyLevel", "").lower() != "eventual"
            or request.query_params.get("$count") != "true"
        )
    ):
        raise c.graph_error(
            400, "Request_UnsupportedQuery", "This query requires ConsistencyLevel: eventual and $count=true."
        )
    if cast:
        rows = [r for r in rows if r["@odata.type"] == "#" + cast]
    if segments[-1] == "$count":
        return c.count_response(request, rows, directory=True)
    result = s.collection(request, rows, "directoryObjects")
    result["value"] = [project_object(request, row) for row in result["value"]]
    return result


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
    app.build_middleware_stack = lambda: GraphHeaders(build_stack(), ctx)
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
        return s.delta(request, ctx, ctx.store["users"].values(), "users", "users", defaults=DEFAULT_USER)

    @router.get("/groups/delta")
    async def groups_delta(request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        return s.delta(request, ctx, ctx.store["groups"].values(), "groups", "groups", defaults=DEFAULT_GROUP)

    @router.get("/users/$count")
    @router.get("/groups/$count")
    async def directory_count(request: Request) -> Response:
        caller = c.principal(request, ctx)
        if caller.delegated and s.user(ctx, c.me_user_id(caller)).get("userType") == "Guest":
            raise c.access_denied()
        kind = s.local_path(request).split("/")[-2]
        return c.count_response(request, ctx.store[kind].values(), directory=True)

    async def directory_single(request: Request, identifier: str) -> dict[str, Any]:
        kind = s.local_path(request).split("/")[-2]
        handler = {"organization": organization, "domains": domains, "subscribedSkus": skus}[kind]
        # Resolve before projecting; an id needn't be included in $select.
        scope = {**request.scope, "query_string": b""}
        records = (await handler(Request(scope)))["value"]
        record = next((r for r in records if r["id"].casefold() == identifier.casefold()), None)
        if record is None:
            raise c.not_found(identifier)
        return s.single(request, record, kind + "/$entity")

    for name in ("organization", "domains", "subscribedSkus"):
        router.add_api_route(f"/{name}/{{identifier}}", directory_single, methods=["GET"])

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
        body = await c.json_body(request)
        if not all(
            k in body
            for k in ("accountEnabled", "displayName", "mailNickname", "userPrincipalName", "passwordProfile")
        ):
            raise c.graph_error(400, "Request_BadRequest", "Required user properties are missing.")
        if (
            not isinstance(body["userPrincipalName"], str)
            or "@" not in body["userPrincipalName"]
            or not isinstance(body["displayName"], str)
            or not isinstance(body["mailNickname"], str)
            or not isinstance(body["accountEnabled"], bool)
            or not isinstance(body["passwordProfile"], dict)
        ):
            raise c.invalid_json()
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
        from emulators.providers.microsoft.contacts import ensure_contacts
        from emulators.providers.microsoft.mail_seed import ensure_mailbox

        ensure_mailbox(ctx, record["id"])
        ensure_contacts(ctx, record["id"])
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

    @router.patch("/me")
    @router.patch("/users/{identifier}")
    async def patch_user(request: Request, identifier: str = "me") -> Response:
        caller = c.principal(request, ctx)
        record = s.user(ctx, c.me_user_id(caller) if identifier == "me" else identifier)
        body = await c.json_body(request)
        for key in (
            "displayName",
            "givenName",
            "surname",
            "jobTitle",
            "mobilePhone",
            "officeLocation",
            "department",
            "mailNickname",
            "userPrincipalName",
            "preferredLanguage",
        ):
            if key in body and body[key] is not None and not isinstance(body[key], str):
                raise c.invalid_json()
        if "accountEnabled" in body and not isinstance(body["accountEnabled"], bool):
            raise c.invalid_json()
        if "businessPhones" in body and (
            not isinstance(body["businessPhones"], list)
            or not all(isinstance(phone, str) for phone in body["businessPhones"])
        ):
            raise c.invalid_json()
        # User.ReadWrite permits self-service profile fields, never directory privileges.
        self_fields = {
            "aboutMe",
            "birthday",
            "interests",
            "mySite",
            "pastProjects",
            "preferredLanguage",
            "responsibilities",
            "schools",
            "skills",
            "displayName",
            "givenName",
            "surname",
            "jobTitle",
            "mobilePhone",
            "businessPhones",
            "officeLocation",
        }
        if identifier != "me" or caller.app_only or record["id"] != caller.user_id or set(body) - self_fields:
            s.admin(request, ctx)
        record.update(
            {k: v for k, v in body.items() if k not in ("id", "passwordProfile") and not k.startswith("_")}
        )
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
        kind = next(
            part
            for part in s.local_path(request).split("/")
            if part in ("manager", "directReports", "ownedObjects", "memberOf", "transitiveMemberOf")
        )
        if kind == "manager":
            mid = ctx.store.meta["ms_managers"].get(uid)
            if not mid or not ctx.store["users"].get(mid):
                raise c.not_found("manager")
            return s.single(
                request, project_object(request, object_record(ctx, mid)), "directoryObjects/$entity"
            )
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
        return object_collection(request, rows)

    for relation_name in ("manager", "directReports", "memberOf", "transitiveMemberOf", "ownedObjects"):
        router.add_api_route(f"/me/{relation_name}", user_relation, methods=["GET"])
        router.add_api_route(f"/users/{{identifier}}/{relation_name}", user_relation, methods=["GET"])
        if relation_name != "manager":
            for suffix in (
                "/$count",
                "/microsoft.graph.user",
                "/microsoft.graph.group",
                "/microsoft.graph.user/$count",
                "/microsoft.graph.group/$count",
            ):
                for base in ("/me", "/users/{identifier}"):
                    router.add_api_route(f"{base}/{relation_name}{suffix}", user_relation, methods=["GET"])

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
    @router.post("/users/{identifier}/getMemberObjects")
    @router.post("/users/{identifier}/checkMemberObjects")
    @router.post("/me/getMemberGroups")
    @router.post("/me/checkMemberGroups")
    @router.post("/me/getMemberObjects")
    @router.post("/me/checkMemberObjects")
    async def check_groups(request: Request, identifier: str = "me") -> dict[str, Any]:
        caller = c.principal(request, ctx)
        record = s.user(ctx, c.me_user_id(caller) if identifier == "me" else identifier)
        body = await c.json_body(request)
        groups = memberships(ctx, record["id"])
        operation = request.url.path.rsplit("/", 1)[-1]
        if operation.startswith("checkMember"):
            key = "groupIds" if operation == "checkMemberGroups" else "ids"
            ids = body.get(key)
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(ids) > 20:
                raise c.invalid_json()
            groups = [g for g in groups if g["id"] in ids]
        elif body.get("securityEnabledOnly"):
            groups = [g for g in groups if g.get("securityEnabled")]
        if "securityEnabledOnly" in body and not isinstance(body["securityEnabledOnly"], bool):
            raise c.invalid_json()
        return {
            "@odata.context": c.context_url(request, "Collection(Edm.String)"),
            "value": [g["id"] for g in groups],
        }

    @router.post("/directoryObjects/getByIds")
    async def get_by_ids(request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        body = await c.json_body(request)
        if not isinstance(body.get("ids"), list) or not all(isinstance(uid, str) for uid in body["ids"]):
            raise c.invalid_json()
        if "types" in body and (
            not isinstance(body["types"], list) or not all(isinstance(t, str) for t in body["types"])
        ):
            raise c.invalid_json()
        rows = []
        for identifier in body.get("ids", []):
            if identifier in ctx.store["users"] or identifier in ctx.store["groups"]:
                record = object_record(ctx, identifier)
                if not body.get("types") or record["@odata.type"].split(".")[-1] in body["types"]:
                    rows.append(record)
        return object_collection(request, rows)

    @router.get("/directoryObjects/{identifier}")
    async def directory_object(identifier: str, request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        return s.single(
            request, project_object(request, object_record(ctx, identifier)), "directoryObjects/$entity"
        )

    @router.get("/groups")
    async def groups(request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        advanced(request)
        return s.collection(request, ctx.store["groups"].values(), "groups", defaults=DEFAULT_GROUP)

    @router.get("/groups/{gid}")
    async def get_group(gid: str, request: Request) -> dict[str, Any]:
        c.principal(request, ctx)
        record = ctx.store["groups"].get(gid)
        if not record:
            raise c.not_found(gid)
        if not request.query_params.get("$select"):
            record = {k: v for k, v in record.items() if k in DEFAULT_GROUP}
        return s.single(request, record, "groups/$entity")

    async def members(request: Request, gid: str) -> Any:
        c.principal(request, ctx)
        if gid not in ctx.store["groups"]:
            raise c.not_found(gid)
        kind = next(
            part
            for part in s.local_path(request).split("/")
            if part in ("owners", "members", "transitiveMembers")
        )
        if "microsoft.graph.group" in request.url.path and (
            kind == "owners" or "Unified" in ctx.store["groups"].get(gid)["groupTypes"]
        ):
            raise c.graph_error(
                400, "Request_UnsupportedQuery", "The requested OData cast is not supported for this group."
            )
        rows = (
            [object_record(ctx, uid) for uid in ctx.store.meta["ms_group_owners"].get(gid, [])]
            if kind == "owners"
            else group_members(ctx, gid, kind == "transitiveMembers")
        )
        return object_collection(request, rows)

    for suffix in ("members", "owners", "transitiveMembers"):
        router.add_api_route(f"/groups/{{gid}}/{suffix}", members, methods=["GET"])
        for extra in (
            "/$count",
            "/microsoft.graph.user",
            "/microsoft.graph.group",
            "/microsoft.graph.user/$count",
            "/microsoft.graph.group/$count",
        ):
            router.add_api_route(f"/groups/{{gid}}/{suffix}{extra}", members, methods=["GET"])

    async def change_members(request: Request, gid: str, uid: str | None = None) -> Response:
        caller = c.principal(request, ctx)
        if gid not in ctx.store["groups"]:
            raise c.not_found(gid)
        if caller.user_id not in ctx.store.meta["ms_group_owners"].get(gid, []):
            s.admin(request, ctx)
        relation = "ms_group_owners" if "/owners/" in request.url.path else "ms_group_members"
        values = ctx.store.meta[relation][gid]
        if request.method == "POST":
            body = await c.json_body(request)
            if not isinstance(body.get("@odata.id"), str):
                raise c.invalid_json()
            uid = str(body.get("@odata.id", "")).rsplit("/", 1)[-1]
            linked = object_record(ctx, uid)
            if relation == "ms_group_owners" and linked["@odata.type"] != "#microsoft.graph.user":
                raise c.graph_error(400, "Request_BadRequest", "Group owners must be users.")
            if uid in values:
                raise c.graph_error(
                    400, "Request_BadRequest", "One or more added object references already exist."
                )
            values.append(uid)
        elif uid in values:
            if relation == "ms_group_owners" and len(values) == 1:
                raise c.graph_error(400, "Request_BadRequest", "The last owner cannot be removed.")
            values.remove(uid)
        else:
            raise c.not_found(str(uid))
        await c.publish_change(ctx, f"groups/{gid}", "updated", ctx.store["groups"].get(gid))
        return Response(status_code=204)

    router.add_api_route("/groups/{gid}/members/$ref", change_members, methods=["POST"])
    router.add_api_route("/groups/{gid}/members/{uid}/$ref", change_members, methods=["DELETE"])
    router.add_api_route("/groups/{gid}/owners/$ref", change_members, methods=["POST"])
    router.add_api_route("/groups/{gid}/owners/{uid}/$ref", change_members, methods=["DELETE"])
    from emulators.providers.microsoft.group_mutations import register as register_groups

    register_groups(router, ctx)
    for version in c.GRAPH_VERSIONS:
        app.include_router(router, prefix=f"/{version}")
