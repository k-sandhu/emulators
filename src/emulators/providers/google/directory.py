"""Directory's hierarchy and membership are mutable projections of the shared world."""

from __future__ import annotations

import base64
import copy
import fnmatch
import hashlib
import json
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.google.common import google_error, json_object, numeric_id, principal, user_id
from emulators.world.model import Person

BASE = "/admin/directory/v1"


def etag(record: dict[str, Any]) -> str:
    return (
        '"' + hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()[:24] + '"'
    )


def clean(record: dict[str, Any]) -> dict[str, Any]:
    return {k: copy.deepcopy(v) for k, v in record.items() if not k.startswith("_")}


def admin(request: Request, ctx: ProviderContext, *, public: bool = False) -> None:
    actor = principal(request, ctx)
    area = next(
        (
            scope
            for segment, scope in (
                ("/users", "user"),
                ("/groups", "group"),
                ("/orgunits", "orgunit"),
                ("/resources/calendars", "resource.calendar"),
                ("/domains", "domain"),
            )
            if segment in request.url.path
        ),
        "customer",
    )
    scopes = {"https://www.googleapis.com/auth/admin.directory." + area}
    if area == "group" and ("/members" in request.url.path or "/hasMember" in request.url.path):
        scopes.add("https://www.googleapis.com/auth/admin.directory.group.member")
    if request.method == "GET":
        scopes |= {scope + ".readonly" for scope in list(scopes)}
    if actor.scopes and not scopes.intersection(actor.scopes):
        raise google_error(
            403, "Request had insufficient authentication scopes.", reason="insufficientPermissions"
        )
    record = next(
        (r for r in ctx.store.collection("directory_users") if r.get("_person") == actor.person_id), None
    )
    if not record or record.get("suspended") or (not public and not record.get("isAdmin")):
        raise google_error(403, "Not Authorized to access this resource/api", reason="forbidden")


def customer(ctx: ProviderContext, key: str) -> None:
    if key not in ("my_customer", ctx.world.org.google_customer_id):
        raise google_error(404, "Resource Not Found: customerKey", reason="notFound")


def lookup(ctx: ProviderContext, collection: str, key: str) -> dict[str, Any]:
    row = next(
        (
            r
            for r in ctx.store.collection(collection)
            if key.lower()
            in [
                str(r.get("id", "")).lower(),
                str(r.get("primaryEmail", r.get("email", ""))).lower(),
                *[a.lower() for a in r.get("aliases", [])],
            ]
        ),
        None,
    )
    if row is None or row.get("_deleted"):
        raise google_error(
            404,
            "Resource Not Found: " + ("userKey" if collection == "directory_users" else "groupKey"),
            reason="notFound",
        )
    return row


def page(
    request: Request,
    rows: list[dict[str, Any]],
    key: str,
    kind: str,
    *,
    default: int = 100,
    maximum: int = 500,
) -> dict[str, Any]:
    q = request.query_params
    try:
        size = int(q.get("maxResults", str(default)))
        if not 1 <= size <= maximum:
            raise ValueError
        start = int(base64.urlsafe_b64decode(q["pageToken"]).decode()) if q.get("pageToken") else 0
        if start < 0:
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        raise google_error(400, "Invalid Input", reason="invalid") from None
    result: dict[str, Any] = {"kind": kind, "etag": etag({key: rows})}
    if rows[start : start + size]:
        result[key] = [clean(r) for r in rows[start : start + size]]
    if start + size < len(rows):
        result["nextPageToken"] = base64.urlsafe_b64encode(str(start + size).encode()).decode()
    return result


def user_shape(ctx: ProviderContext, person: Person) -> dict[str, Any]:
    dept = ctx.world.department(person.department_id) if person.department_id else None
    office = ctx.world.office(person.office_id) if person.office_id else None
    ou = "/" + dept.name if dept else "/"
    if not person.active:
        ou = "/Departed"
    elif person.department_id == "advisory" and person.office_id == "london":
        ou += "/London"
    return {
        "kind": "admin#directory#user",
        "id": user_id(ctx.world, person.id),
        "_person": person.id,
        "etag": etag({"person": person.id}),
        "primaryEmail": person.email,
        "name": {
            "givenName": person.first_name,
            "familyName": person.last_name,
            "fullName": person.display_name,
        },
        "isAdmin": person.is_admin,
        "isDelegatedAdmin": False,
        "lastLoginTime": isoformat_z(ctx.world.anchor),
        "creationTime": str(person.hire_date or "2010-01-01") + "T00:00:00.000Z",
        "agreedToTerms": True,
        "suspended": not person.active,
        "archived": False,
        "changePasswordAtNextLogin": False,
        "ipWhitelisted": False,
        "emails": [{"address": person.email, "primary": True}],
        "phones": [
            {"value": value, "type": kind}
            for value, kind in ((person.phone_business, "work"), (person.phone_mobile, "mobile"))
            if value
        ],
        "addresses": [
            {
                "type": "work",
                "formatted": office.address.one_line(),
                "streetAddress": office.address.street,
                "locality": office.address.city,
                "region": office.address.state,
                "postalCode": office.address.postal_code,
                "country": office.address.country,
                "countryCode": office.address.country_code,
            }
        ]
        if office
        else [],
        "organizations": [
            {
                "title": person.job_title,
                "primary": True,
                "customType": "",
                "department": dept.name if dept else "",
                "costCenter": dept.cost_center if dept else "",
                "location": office.name if office else "",
            }
        ],
        "relations": [{"value": ctx.world.person(person.manager_id).email, "type": "manager"}]
        if person.manager_id
        else [],
        "externalIds": [{"value": person.employee_id, "type": "organization"}],
        "locations": [{"type": "desk", "buildingId": person.office_id, "area": "desk"}],
        "customerId": ctx.world.org.google_customer_id,
        "orgUnitPath": ou,
        "isMailboxSetup": True,
        "isEnrolledIn2Sv": True,
        "isEnforcedIn2Sv": person.is_admin,
        "includeInGlobalAddressList": person.active,
        "thumbnailPhotoUrl": f"https://www.googleapis.com/admin/directory/v1/users/{user_id(ctx.world, person.id)}/photos/thumbnail",
        "languages": [{"languageCode": person.locale, "preference": "preferred"}],
        "aliases": [],
        "nonEditableAliases": [],
    }


def seed(ctx: ProviderContext) -> None:
    for person in ctx.world.employees:
        ctx.store.collection("directory_users").put(user_shape(ctx, person))
    paths = ["/", *("/" + d.name for d in ctx.world.departments), "/Departed", "/Advisory/London"]
    for path in paths:
        parent = path.rsplit("/", 1)[0] or "/"
        ou = {
            "kind": "admin#directory#orgUnit",
            "name": path.rsplit("/", 1)[-1] or ctx.world.org.name,
            "description": "Organizational unit for " + path,
            "orgUnitPath": path,
            "orgUnitId": "id:03ph8a2z" + stable_hash(ctx.world.seed, path, length=12),
            "blockInheritance": False,
        }
        if path != "/":
            ou.update(
                parentOrgUnitPath=parent,
                parentOrgUnitId="id:03ph8a2z" + stable_hash(ctx.world.seed, parent, length=12),
            )
        ou["etag"] = etag(ou)
        ctx.store.collection("directory_orgunits").put_as(path, ou)
    for group in ctx.world.groups:
        gid = numeric_id(ctx.world, "group", group.id)
        members = []
        if group.kind == "all_staff":
            for dept in ctx.world.groups:
                if dept.kind == "department":
                    members.append(
                        {
                            "kind": "admin#directory#member",
                            "id": numeric_id(ctx.world, "group", dept.id),
                            "email": dept.email,
                            "role": "MEMBER",
                            "type": "GROUP",
                            "status": "ACTIVE",
                        }
                    )
        else:
            for pid in group.member_ids:
                p = ctx.world.person(pid)
                members.append(
                    {
                        "kind": "admin#directory#member",
                        "id": user_id(ctx.world, pid),
                        "email": p.email,
                        "role": "OWNER" if pid in group.owner_ids else "MEMBER",
                        "type": "USER",
                        "status": "ACTIVE",
                    }
                )
        for m in members:
            m["etag"] = etag(m)
        row = {
            "kind": "admin#directory#group",
            "id": gid,
            "_world": group.id,
            "email": group.email,
            "name": group.name,
            "description": group.description,
            "directMembersCount": str(len(members)),
            "adminCreated": True,
            "aliases": [],
            "nonEditableAliases": [],
        }
        row["etag"] = etag(row)
        ctx.store.collection("directory_groups").put(row)
        ctx.store.collection("directory_members").put_as(gid, {"members": members})


def expanded_members(
    ctx: ProviderContext, gid: str, *, derived: bool = False, seen: set[str] | None = None
) -> list[dict[str, Any]]:
    seen = set() if seen is None else seen
    if gid in seen:
        return []
    seen.add(gid)
    members = (ctx.store.collection("directory_members").get(gid) or {}).get("members", [])
    result = list(members)
    if derived:
        for member in members:
            if member["type"] == "GROUP":
                result.extend(expanded_members(ctx, member["id"], derived=True, seen=seen))
    return list({m["id"]: m for m in result}.values())


def matches_user(row: dict[str, Any], query: str, ctx: ProviderContext) -> bool:
    pattern = r"(\w+)(=|:)(?:'([^']*)'|\"([^\"]*)\"|([^\s]+))"
    terms = list(re.finditer(pattern, query))
    if re.sub(pattern, "", query).strip():
        raise google_error(400, "Invalid Input: query", reason="invalid")
    for term in terms:
        field, op = term.group(1, 2)
        value = next(v for v in term.group(3, 4, 5) if v is not None)
        values: list[Any]
        if field == "name":
            values = (
                [row.get("name", {}).get("fullName", "")] if op == "=" else list(row.get("name", {}).values())
            )
        elif field in ("givenName", "familyName"):
            values = [row.get("name", {}).get(field, "")]
        elif field == "email":
            values = [row.get("primaryEmail", ""), *row.get("aliases", [])]
        elif field in ("manager", "directManager"):
            values = [r["value"] for r in row.get("relations", []) if r["type"] == "manager"]
            if field == "manager":
                seen: set[str] = set()
                pending = list(values)
                while pending:
                    manager_email = pending.pop()
                    if manager_email in seen:
                        continue
                    seen.add(manager_email)
                    manager = next(
                        (
                            r
                            for r in ctx.store.collection("directory_users")
                            if r["primaryEmail"] == manager_email
                        ),
                        None,
                    )
                    if manager:
                        ancestors = [
                            r["value"] for r in manager.get("relations", []) if r["type"] == "manager"
                        ]
                        values.extend(ancestors)
                        pending.extend(ancestors)
        elif field == "isSuspended":
            values = [row.get("suspended", False)]
        elif field in ("isAdmin", "orgUnitPath"):
            values = [row.get(field, "")]
        else:
            raise google_error(400, "Invalid Input: query", reason="invalid")
        if not any(
            fnmatch.fnmatchcase(
                str(v).lower(), value.lower() + ("*" if op == ":" and "*" not in value else "")
            )
            for v in values
        ):
            return False
    return True


def project_user(row: dict[str, Any], request: Request) -> dict[str, Any]:
    result = clean(row)
    result.pop("password", None)
    if request.query_params.get("viewType") == "domain_public":
        allowed = {
            "kind",
            "id",
            "etag",
            "primaryEmail",
            "name",
            "emails",
            "phones",
            "organizations",
            "relations",
            "thumbnailPhotoUrl",
            "languages",
        }
        result = {k: v for k, v in result.items() if k in allowed}
    return result


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get(BASE + "/users")
    async def list_users(request: Request) -> dict[str, Any]:
        q = request.query_params
        admin(request, ctx, public=q.get("viewType") == "domain_public")
        if not q.get("customer") and not q.get("domain"):
            raise google_error(400, "Invalid Input: customer", reason="required")
        if q.get("customer"):
            customer(ctx, q["customer"])
        if q.get("domain") and q["domain"] != ctx.world.org.domain:
            raise google_error(404, "Domain not found.", reason="notFound")
        deleted = q.get("showDeleted", "false") == "true"
        rows = [
            r
            for r in ctx.store.collection("directory_users")
            if bool(r.get("_deleted")) == deleted and matches_user(r, q.get("query", ""), ctx)
        ]
        order = q.get("orderBy", "email")
        if order not in ("email", "givenName", "familyName"):
            raise google_error(400, "Invalid Input: orderBy", reason="invalid")
        rows.sort(
            key=lambda r: r["primaryEmail"].lower() if order == "email" else r["name"].get(order, "").lower(),
            reverse=q.get("sortOrder") == "DESCENDING",
        )
        return page(request, [project_user(r, request) for r in rows], "users", "admin#directory#users")

    @app.post(BASE + "/users")
    async def create_user(request: Request) -> dict[str, Any]:
        admin(request, ctx)
        data = await json_object(request)
        if not data.get("primaryEmail") or not data.get("name") or not data.get("password"):
            raise google_error(
                400, "Missing required field: primaryEmail, name or password", reason="required"
            )
        if any(
            r["primaryEmail"].lower() == data["primaryEmail"].lower()
            for r in ctx.store.collection("directory_users")
        ):
            raise google_error(409, "Entity already exists.", reason="duplicate")
        row = {
            "kind": "admin#directory#user",
            "id": ctx.ids.digits(21),
            "isAdmin": False,
            "isDelegatedAdmin": False,
            "suspended": False,
            "archived": False,
            "orgUnitPath": "/",
            "customerId": ctx.world.org.google_customer_id,
            "creationTime": isoformat_z(ctx.clock.now()),
            "lastLoginTime": "1970-01-01T00:00:00.000Z",
            "aliases": [],
            **{k: v for k, v in data.items() if k not in ("password", "id", "isAdmin", "isDelegatedAdmin")},
        }
        row["name"].setdefault(
            "fullName", row["name"].get("givenName", "") + " " + row["name"].get("familyName", "")
        )
        row["etag"] = etag(row)
        ctx.store.collection("directory_users").put(row)
        return clean(row)

    @app.get(BASE + "/users/{user_key}")
    async def get_user(user_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx, public=request.query_params.get("viewType") == "domain_public")
        return project_user(lookup(ctx, "directory_users", user_key), request)

    @app.patch(BASE + "/users/{user_key}")
    @app.put(BASE + "/users/{user_key}")
    async def update_user(user_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        row = lookup(ctx, "directory_users", user_key)
        data = await json_object(request)
        if data.get("orgUnitPath") and data["orgUnitPath"] not in ctx.store.collection("directory_orgunits"):
            raise google_error(400, "Invalid Input: orgUnitPath", reason="invalid")
        for key, value in data.items():
            if key not in ("id", "kind", "password", "isAdmin", "isDelegatedAdmin") and not key.startswith(
                "_"
            ):
                if key == "name":
                    row["name"].update(value)
                    row["name"]["fullName"] = (
                        row["name"].get("givenName", "") + " " + row["name"].get("familyName", "")
                    )
                else:
                    row[key] = value
        row["etag"] = etag(row)
        return clean(row)

    @app.delete(BASE + "/users/{user_key}")
    async def delete_user(user_key: str, request: Request) -> Response:
        admin(request, ctx)
        row = lookup(ctx, "directory_users", user_key)
        row["_deleted"] = True
        row["deletionTime"] = isoformat_z(ctx.clock.now())
        if row.get("_person"):
            ctx.tokens.revoke_user(row["_person"])
        return Response(status_code=204)

    @app.post(BASE + "/users/{user_key}/makeAdmin")
    async def make_admin(user_key: str, request: Request) -> Response:
        admin(request, ctx)
        lookup(ctx, "directory_users", user_key)["isAdmin"] = bool((await json_object(request)).get("status"))
        return Response(status_code=200)

    @app.get(BASE + "/users/{user_key}/photos/thumbnail")
    async def photo(user_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx, public=True)
        row = lookup(ctx, "directory_users", user_key)
        from emulators.world.documents import make_png

        return {
            "kind": "admin#directory#userPhoto",
            "id": row["id"],
            "etag": row["etag"],
            "primaryEmail": row["primaryEmail"],
            "mimeType": "image/png",
            "width": 64,
            "height": 64,
            "photoData": base64.urlsafe_b64encode(make_png(seed=int(row["id"][-6:]))).decode(),
        }

    @app.get(BASE + "/users/{user_key}/aliases")
    async def aliases(user_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        row = lookup(ctx, "directory_users", user_key)
        return {
            "kind": "admin#directory#aliases",
            "etag": row["etag"],
            "aliases": [
                {
                    "kind": "admin#directory#alias",
                    "id": row["id"],
                    "primaryEmail": row["primaryEmail"],
                    "alias": a,
                }
                for a in row.get("aliases", [])
            ],
        }

    @app.post(BASE + "/users/{user_key}/aliases")
    async def add_alias(user_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        row = lookup(ctx, "directory_users", user_key)
        value = (await json_object(request)).get("alias")
        if not value or "@" not in value:
            raise google_error(400, "Invalid Input: alias", reason="invalid")
        if any(
            value in [r["primaryEmail"], *r.get("aliases", [])]
            for r in ctx.store.collection("directory_users")
        ):
            raise google_error(409, "Entity already exists.", reason="duplicate")
        row.setdefault("aliases", []).append(value)
        return {
            "kind": "admin#directory#alias",
            "id": row["id"],
            "primaryEmail": row["primaryEmail"],
            "alias": value,
        }

    @app.delete(BASE + "/users/{user_key}/aliases/{alias}")
    async def remove_alias(user_key: str, alias: str, request: Request) -> Response:
        admin(request, ctx)
        row = lookup(ctx, "directory_users", user_key)
        if alias not in row.get("aliases", []):
            raise google_error(404, "Resource Not Found: alias", reason="notFound")
        row["aliases"].remove(alias)
        return Response(status_code=204)

    @app.get(BASE + "/groups")
    async def groups(request: Request) -> dict[str, Any]:
        admin(request, ctx)
        q = request.query_params
        if q.get("customer"):
            customer(ctx, q["customer"])
        if not any(q.get(k) for k in ("customer", "domain", "userKey")):
            raise google_error(400, "Missing required parameter", reason="required")
        rows = ctx.store.collection("directory_groups").values()
        if q.get("userKey"):
            user = lookup(ctx, "directory_users", q["userKey"])
            rows = [
                g
                for g in rows
                if any(m["id"] == user["id"] for m in expanded_members(ctx, g["id"], derived=True))
            ]
        if q.get("domain"):
            rows = [g for g in rows if g["email"].endswith("@" + q["domain"])]
        if q.get("query"):
            query = q["query"]
            match = re.fullmatch(r"(email|name)(:|=)'?([^']+)'?", query)
            if not match:
                raise google_error(400, "Invalid Input: query", reason="invalid")
            rows = [
                g
                for g in rows
                if fnmatch.fnmatchcase(
                    g[match[1]].lower(), match[3].lower() + ("*" if match[2] == ":" else "")
                )
            ]
        return page(
            request,
            sorted(rows, key=lambda r: r["email"]),
            "groups",
            "admin#directory#groups",
            default=200,
            maximum=200,
        )

    @app.post(BASE + "/groups")
    async def create_group(request: Request) -> dict[str, Any]:
        admin(request, ctx)
        data = await json_object(request)
        if not data.get("email"):
            raise google_error(400, "Missing required field: email", reason="required")
        if any(g["email"] == data["email"] for g in ctx.store.collection("directory_groups")):
            raise google_error(409, "Entity already exists.", reason="duplicate")
        row = {
            "kind": "admin#directory#group",
            "id": ctx.ids.digits(21),
            "name": data.get("name", ""),
            "email": data["email"],
            "description": data.get("description", ""),
            "directMembersCount": "0",
            "adminCreated": True,
            "aliases": [],
        }
        row["etag"] = etag(row)
        ctx.store.collection("directory_groups").put(row)
        return row

    @app.get(BASE + "/groups/{group_key}")
    async def group(group_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        return clean(lookup(ctx, "directory_groups", group_key))

    @app.patch(BASE + "/groups/{group_key}")
    @app.put(BASE + "/groups/{group_key}")
    async def update_group(group_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        row = lookup(ctx, "directory_groups", group_key)
        data = await json_object(request)
        row.update({k: data[k] for k in ("email", "name", "description") if k in data})
        row["etag"] = etag(row)
        return clean(row)

    @app.delete(BASE + "/groups/{group_key}")
    async def delete_group(group_key: str, request: Request) -> Response:
        admin(request, ctx)
        row = lookup(ctx, "directory_groups", group_key)
        ctx.store.collection("directory_groups").delete(row["id"])
        ctx.store.collection("directory_members").delete(row["id"])
        return Response(status_code=204)

    @app.get(BASE + "/groups/{group_key}/members")
    async def members(group_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        row = lookup(ctx, "directory_groups", group_key)
        rows = expanded_members(
            ctx, row["id"], derived=request.query_params.get("includeDerivedMembership") == "true"
        )
        if request.query_params.get("roles"):
            rows = [m for m in rows if m["role"] in request.query_params["roles"].split(",")]
        return page(request, rows, "members", "admin#directory#members", default=200, maximum=200)

    @app.post(BASE + "/groups/{group_key}/members")
    async def insert_member(group_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        group = lookup(ctx, "directory_groups", group_key)
        data = await json_object(request)
        key = data.get("email") or data.get("id", "")
        target = next(
            (
                r
                for name in ("directory_users", "directory_groups")
                for r in ctx.store.collection(name)
                if key in (r["id"], r.get("primaryEmail", r.get("email")))
            ),
            None,
        )
        role = data.get("role", "MEMBER")
        if not key or role not in ("OWNER", "MANAGER", "MEMBER"):
            raise google_error(400, "Invalid Input", reason="invalid")
        row = {
            "kind": "admin#directory#member",
            "id": target["id"] if target else ctx.ids.digits(21),
            "email": (target.get("primaryEmail", target.get("email")) if target else key),
            "role": role,
            "type": "GROUP" if target and target["kind"].endswith("#group") else "USER",
            "status": "ACTIVE",
        }
        if row["id"] == group["id"] or any(
            m["id"] == row["id"] or m["email"] == row["email"] for m in expanded_members(ctx, group["id"])
        ):
            raise google_error(409, "Member already exists.", reason="duplicate")
        row["etag"] = etag(row)
        collection = ctx.store.collection("directory_members")
        holder = collection.get(group["id"]) or collection.put_as(group["id"], {"members": []})
        holder["members"].append(row)
        group["directMembersCount"] = str(len(holder["members"]))
        return row

    def member_row(group_key: str, member_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
        group = lookup(ctx, "directory_groups", group_key)
        row = next(
            (m for m in expanded_members(ctx, group["id"]) if member_key in (m["id"], m.get("email"))), None
        )
        if row is None:
            raise google_error(404, "Resource Not Found: memberKey", reason="notFound")
        return group, row

    @app.get(BASE + "/groups/{group_key}/hasMember/{member_key}")
    async def has_member(group_key: str, member_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        group = lookup(ctx, "directory_groups", group_key)
        return {
            "isMember": any(
                member_key in (m["id"], m.get("email"))
                for m in expanded_members(ctx, group["id"], derived=True)
            )
        }

    @app.get(BASE + "/groups/{group_key}/members/{member_key}")
    async def get_member(group_key: str, member_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        return member_row(group_key, member_key)[1]

    @app.patch(BASE + "/groups/{group_key}/members/{member_key}")
    @app.put(BASE + "/groups/{group_key}/members/{member_key}")
    async def update_member(group_key: str, member_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        row = member_row(group_key, member_key)[1]
        role = (await json_object(request)).get("role")
        if role not in ("OWNER", "MANAGER", "MEMBER"):
            raise google_error(400, "Invalid Input: role", reason="invalid")
        row["role"] = role
        row["etag"] = etag(row)
        return row

    @app.delete(BASE + "/groups/{group_key}/members/{member_key}")
    async def delete_member(group_key: str, member_key: str, request: Request) -> Response:
        admin(request, ctx)
        group, row = member_row(group_key, member_key)
        holder = ctx.store.collection("directory_members").get(group["id"])
        holder["members"].remove(row)
        group["directMembersCount"] = str(len(holder["members"]))
        return Response(status_code=204)

    @app.get(BASE + "/customer/{customer_id}/orgunits")
    async def orgunits(customer_id: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        customer(ctx, customer_id)
        path = request.query_params.get("orgUnitPath", "/")
        if path not in ctx.store.collection("directory_orgunits"):
            raise google_error(404, "Org unit not found", reason="notFound")
        all_descendants = request.query_params.get("type", "children") in ("all", "allIncludingParent")
        rows = [
            r
            for r in ctx.store.collection("directory_orgunits")
            if (
                r.get("parentOrgUnitPath") == path
                or (all_descendants and r["orgUnitPath"].startswith(path.rstrip("/") + "/"))
            )
        ]
        if request.query_params.get("type") == "allIncludingParent":
            rows.insert(0, ctx.store.collection("directory_orgunits").get(path))
        return {"kind": "admin#directory#orgUnits", "etag": etag({"rows": rows}), "organizationUnits": rows}

    @app.post(BASE + "/customer/{customer_id}/orgunits")
    async def create_ou(customer_id: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        customer(ctx, customer_id)
        data = await json_object(request)
        parent = data.get("parentOrgUnitPath", "/")
        if not data.get("name") or parent not in ctx.store.collection("directory_orgunits"):
            raise google_error(400, "Invalid Input", reason="invalid")
        path = parent.rstrip("/") + "/" + data["name"]
        if path in ctx.store.collection("directory_orgunits"):
            raise google_error(409, "Entity already exists.", reason="duplicate")
        row = {
            "kind": "admin#directory#orgUnit",
            "orgUnitId": "id:03ph8a2z" + ctx.ids.alnum_lower(12),
            "name": data["name"],
            "description": data.get("description", ""),
            "orgUnitPath": path,
            "parentOrgUnitPath": parent,
            "parentOrgUnitId": ctx.store.collection("directory_orgunits").get(parent)["orgUnitId"],
            "blockInheritance": data.get("blockInheritance", False),
        }
        row["etag"] = etag(row)
        ctx.store.collection("directory_orgunits").put_as(path, row)
        return row

    def ou_row(path: str) -> dict[str, Any]:
        row = next(
            (
                r
                for r in ctx.store.collection("directory_orgunits")
                if path == r["orgUnitId"] or "/" + path.lstrip("/") == r["orgUnitPath"]
            ),
            None,
        )
        if not row:
            raise google_error(404, "Org unit not found", reason="notFound")
        return row

    @app.get(BASE + "/customer/{customer_id}/orgunits/{path:path}")
    async def get_ou(customer_id: str, path: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        customer(ctx, customer_id)
        return ou_row(path)

    @app.patch(BASE + "/customer/{customer_id}/orgunits/{path:path}")
    @app.put(BASE + "/customer/{customer_id}/orgunits/{path:path}")
    async def patch_ou(customer_id: str, path: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        customer(ctx, customer_id)
        row = ou_row(path)
        data = await json_object(request)
        old = row["orgUnitPath"]
        parent = data.get("parentOrgUnitPath", row.get("parentOrgUnitPath", "/"))
        if (
            parent not in ctx.store.collection("directory_orgunits")
            or parent == old
            or parent.startswith(old.rstrip("/") + "/")
        ):
            raise google_error(400, "Invalid parent org unit", reason="invalid")
        new = parent.rstrip("/") + "/" + data.get("name", row["name"])
        if new != old and new in ctx.store.collection("directory_orgunits"):
            raise google_error(409, "Entity already exists.", reason="duplicate")
        row.update({k: data[k] for k in ("name", "description", "blockInheritance") if k in data})
        if old != "/":
            row.update(
                parentOrgUnitPath=parent,
                parentOrgUnitId=ctx.store.collection("directory_orgunits").get(parent)["orgUnitId"],
            )
            for item in ctx.store.collection("directory_orgunits").values():
                original = item["orgUnitPath"]
                if original == old or original.startswith(old + "/"):
                    ctx.store.collection("directory_orgunits").delete(original)
                    item["orgUnitPath"] = new + original[len(old) :]
                    if item.get("parentOrgUnitPath", "").startswith(old):
                        item["parentOrgUnitPath"] = new + item["parentOrgUnitPath"][len(old) :]
                    ctx.store.collection("directory_orgunits").put_as(item["orgUnitPath"], item)
            for user in ctx.store.collection("directory_users"):
                if user["orgUnitPath"] == old or user["orgUnitPath"].startswith(old + "/"):
                    user["orgUnitPath"] = new + user["orgUnitPath"][len(old) :]
        row["etag"] = etag(row)
        return row

    @app.delete(BASE + "/customer/{customer_id}/orgunits/{path:path}")
    async def delete_ou(customer_id: str, path: str, request: Request) -> Response:
        admin(request, ctx)
        customer(ctx, customer_id)
        row = ou_row(path)
        prefix = row["orgUnitPath"]
        if (
            prefix == "/"
            or any(r.get("parentOrgUnitPath") == prefix for r in ctx.store.collection("directory_orgunits"))
            or any(
                r["orgUnitPath"] == prefix
                for r in ctx.store.collection("directory_users")
                if not r.get("_deleted")
            )
        ):
            raise google_error(412, "Org unit not empty", reason="conditionNotMet")
        ctx.store.collection("directory_orgunits").delete(prefix)
        return Response(status_code=204)

    @app.get(BASE + "/customers/{customer_key}")
    async def get_customer(customer_key: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        customer(ctx, customer_key)
        return {
            "kind": "admin#directory#customer",
            "id": ctx.world.org.google_customer_id,
            "etag": etag({"customer": ctx.world.org.google_customer_id}),
            "customerDomain": ctx.world.org.domain,
            "phoneNumber": ctx.world.org.phone,
            "language": "en",
            "customerCreationTime": "2009-03-02T00:00:00.000Z",
            "postalAddress": {
                "organizationName": ctx.world.org.name,
                "addressLine1": ctx.world.org.address.street,
                "locality": ctx.world.org.address.city,
                "region": ctx.world.org.address.state,
                "postalCode": ctx.world.org.address.postal_code,
                "countryCode": ctx.world.org.address.country_code,
            },
        }

    @app.get(BASE + "/customer/{customer_id}/domains")
    async def domains(customer_id: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        customer(ctx, customer_id)
        return {
            "kind": "admin#directory#domains",
            "etag": etag({"domain": ctx.world.org.domain}),
            "domains": [
                {
                    "kind": "admin#directory#domain",
                    "domainName": ctx.world.org.domain,
                    "isPrimary": True,
                    "verified": True,
                    "creationTime": "1235952000000",
                }
            ],
        }

    @app.get(BASE + "/customer/{customer_id}/resources/calendars")
    async def rooms(customer_id: str, request: Request) -> dict[str, Any]:
        admin(request, ctx)
        customer(ctx, customer_id)
        rows = []
        for office in ctx.world.offices:
            for name, capacity in (("Boardroom", 12), ("Meeting Room", 6)):
                rid = numeric_id(ctx.world, "room", office.id, name)
                city = office.address.city
                rows.append(
                    {
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
                )
        return page(
            request,
            rows,
            "items",
            "admin#directory#resources#calendars#calendarResources",
            default=500,
            maximum=500,
        )
