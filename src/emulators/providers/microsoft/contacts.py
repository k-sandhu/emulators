"""World client relationships projected into private Outlook contact folders."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common as c
from emulators.providers.microsoft import directory_support as s


def folder_id(ctx: ProviderContext, uid: str) -> str:
    return "AAMkA" + stable_hash(ctx.world.seed, "contacts", uid, length=64)


def ensure_contacts(ctx: ProviderContext, uid: str) -> dict[str, Any]:
    fid = folder_id(ctx, uid)
    record = ctx.store["contact_folders"].get(fid)
    if record is None:
        record = {"id": fid, "displayName": "Contacts", "parentFolderId": None, "_owner": uid}
        ctx.store["contact_folders"].put(record)
    return record


def defaults() -> dict[str, Any]:
    record: dict[str, Any] = dict.fromkeys(
        (
            "assistantName",
            "birthday",
            "businessHomePage",
            "companyName",
            "department",
            "displayName",
            "fileAs",
            "generation",
            "givenName",
            "initials",
            "jobTitle",
            "manager",
            "middleName",
            "mobilePhone",
            "nickName",
            "officeLocation",
            "personalNotes",
            "profession",
            "spouseName",
            "surname",
            "title",
            "yomiCompanyName",
            "yomiGivenName",
            "yomiSurname",
        )
    )
    record.update(
        {
            key: []
            for key in (
                "categories",
                "children",
                "emailAddresses",
                "businessPhones",
                "homePhones",
                "imAddresses",
            )
        }
    )
    record.update({key: {} for key in ("businessAddress", "homeAddress", "otherAddress")})
    return record


def seed(ctx: ProviderContext) -> None:
    world = ctx.world
    for owner in world.employees:
        uid = c.user_id(world, owner.id)
        fid = folder_id(ctx, uid)
        ctx.store["contact_folders"].put(
            {"id": fid, "displayName": "Contacts", "parentFolderId": None, "_owner": uid}
        )
        clients = {cl.id for cl in world.clients if cl.owner_id == owner.id}
        clients.update(e.client_id for e in world.engagements if owner.id in (e.lead_id, *e.team_ids))
        people = {pid for cl in world.clients if cl.id in clients for pid in cl.contact_ids}
        people.update(
            p.id
            for p in world.active_employees
            if p.id != owner.id and p.department_id == owner.department_id
        )
        for pid in sorted(people):
            person = world.person(pid)
            client = world.client(person.company_id) if person.is_external else None
            address = client.address if client else world.office(person.office_id).address
            ctx.store["contacts"].put(
                {
                    **defaults(),
                    "id": "AAMkA" + stable_hash(world.seed, "contact", owner.id, pid, length=64),
                    "parentFolderId": fid,
                    "givenName": person.first_name,
                    "surname": person.last_name,
                    "displayName": person.display_name,
                    "fileAs": f"{person.last_name}, {person.first_name}",
                    "emailAddresses": [{"name": person.display_name, "address": person.email}],
                    "businessPhones": [person.phone_business] if person.phone_business else [],
                    "mobilePhone": person.phone_mobile,
                    "homePhones": [],
                    "companyName": client.name if client else world.org.name,
                    "jobTitle": person.job_title,
                    "businessAddress": {
                        "street": address.street,
                        "city": address.city,
                        "state": address.state,
                        "postalCode": address.postal_code,
                        "countryOrRegion": address.country,
                    },
                    "categories": ["Clients" if client else "Colleagues"],
                    "createdDateTime": world.anchor.isoformat().replace("+00:00", "Z"),
                    "lastModifiedDateTime": world.anchor.isoformat().replace("+00:00", "Z"),
                    "changeKey": stable_hash(world.seed, "contact", owner.id, pid),
                    "@odata.etag": f'W/"{stable_hash(world.seed, "contact", owner.id, pid)}"',
                    "_owner": uid,
                }
            )


def register(app: FastAPI, ctx: ProviderContext) -> None:
    router = APIRouter()

    async def changed(record: dict[str, Any], kind: str) -> None:
        uid, cid, fid = record["_owner"], record["id"], record["parentFolderId"]
        await c.publish_change(ctx, f"users/{uid}/contacts/{cid}", kind, record)
        await c.publish_change(ctx, f"users/{uid}/contactFolders/{fid}/contacts/{cid}", kind, record)

    def get_folder(uid: str, fid: str) -> dict[str, Any]:
        record = ctx.store["contact_folders"].get(fid)
        if not record or record["_owner"] != uid:
            raise c.graph_error(404, "ErrorItemNotFound", "The specified contact folder was not found.")
        return record

    async def folders(request: Request, identifier: str = "me", fid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        uid = owner["id"]
        parent = get_folder(uid, fid) if fid else None
        if request.method == "POST":
            payload = await c.json_body(request)
            if not isinstance(payload.get("displayName"), str) or not payload["displayName"]:
                raise c.graph_error(400, "ErrorInvalidParameter", "displayName is required.")
            record = {
                "id": s.outlook_id(ctx),
                "displayName": payload["displayName"],
                "parentFolderId": fid,
                "_owner": uid,
            }
            ctx.store["contact_folders"].put(record)
            return JSONResponse(s.single(request, record, "contactFolders/$entity"), status_code=201)
        if parent and not request.url.path.endswith("childFolders"):
            return s.single(request, parent, "contactFolders/$entity")
        return s.collection(
            request, ctx.store["contact_folders"].where(_owner=uid, parentFolderId=fid), "contactFolders"
        )

    async def contacts(
        request: Request, identifier: str = "me", fid: str | None = None, contact_id: str | None = None
    ) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        uid = owner["id"]
        ensure_contacts(ctx, uid)
        target_folder = fid or folder_id(ctx, uid)
        get_folder(uid, target_folder)
        record = ctx.store["contacts"].get(contact_id) if contact_id else None
        if contact_id and (
            not record or record["_owner"] != uid or (fid and record["parentFolderId"] != fid)
        ):
            raise c.graph_error(404, "ErrorItemNotFound", "The specified contact was not found.")
        if request.method == "GET":
            if request.url.path.endswith("/delta"):
                rows = ctx.store["contacts"].where(_owner=uid, parentFolderId=target_folder)
                resource = f"users/{uid}/contactFolders/{fid}/contacts" if fid else f"users/{uid}/contacts"
                return s.delta(request, ctx, rows, resource, "contacts")
            return (
                s.single(request, record, "contacts/$entity")
                if record
                else s.collection(
                    request,
                    ctx.store["contacts"].where(_owner=uid, parentFolderId=target_folder),
                    "contacts",
                    size=10,
                    search=("displayName", "emailAddresses", "companyName"),
                )
            )
        if request.method == "DELETE":
            ctx.store["contacts"].delete(contact_id)
            await changed(record, "deleted")
            return Response(status_code=204)
        payload = await c.json_body(request)
        schema = defaults()
        for key, value in payload.items():
            if key not in schema:
                continue
            expected = (
                dict if isinstance(schema[key], dict) else list if isinstance(schema[key], list) else str
            )
            if value is not None and not isinstance(value, expected):
                raise c.invalid_json()
        if payload.get("emailAddresses") is not None:
            for email in payload.get("emailAddresses", []):
                if not isinstance(email, dict) or not isinstance(email.get("address"), str):
                    raise c.invalid_json()
        if request.method == "POST":
            record = {
                **defaults(),
                "id": s.outlook_id(ctx),
                "parentFolderId": target_folder,
                "_owner": uid,
                "createdDateTime": s.now(ctx),
            }
        record.update(
            {
                k: v
                for k, v in payload.items()
                if not k.startswith(("_", "@"))
                and k not in ("id", "parentFolderId", "createdDateTime", "changeKey")
            }
        )
        record["lastModifiedDateTime"] = s.now(ctx)
        record["changeKey"] = ctx.ids.urlsafe(24)
        record["@odata.etag"] = f'W/"{record["changeKey"]}"'
        if not record["displayName"]:
            record["displayName"] = " ".join(v for v in (record["givenName"], record["surname"]) if v)
        if not record["fileAs"]:
            record["fileAs"] = ", ".join(v for v in (record["surname"], record["givenName"]) if v)
        ctx.store["contacts"].put(record)
        await changed(record, "created" if request.method == "POST" else "updated")
        return JSONResponse(
            s.single(request, record, "contacts/$entity"),
            status_code=201 if request.method == "POST" else 200,
        )

    for base in ("/me", "/users/{identifier}"):
        for suffix in ("contactFolders", "contactFolders/{fid}/childFolders"):
            router.add_api_route(f"{base}/{suffix}", folders, methods=["GET", "POST"])
        router.add_api_route(f"{base}/contactFolders/{{fid}}", folders, methods=["GET"])
        for prefix in ("contacts", "contactFolders/{fid}/contacts"):
            router.add_api_route(f"{base}/{prefix}", contacts, methods=["GET", "POST"])
            router.add_api_route(f"{base}/{prefix}/delta", contacts, methods=["GET"])
            router.add_api_route(
                f"{base}/{prefix}/{{contact_id}}", contacts, methods=["GET", "PATCH", "DELETE"]
            )
    for version in c.GRAPH_VERSIONS:
        app.include_router(router, prefix=f"/{version}")
