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
                    "_owner": uid,
                }
            )


def register(app: FastAPI, ctx: ProviderContext) -> None:
    router = APIRouter()

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
            payload = await request.json()
            if not payload.get("displayName"):
                raise c.graph_error(400, "ErrorInvalidParameter", "displayName is required.")
            record = {
                "id": s.outlook_id(ctx),
                "displayName": payload["displayName"],
                "parentFolderId": fid,
                "_owner": uid,
            }
            ctx.store["contact_folders"].put(record)
            return JSONResponse(s.clean(record), status_code=201)
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
        target_folder = fid or folder_id(ctx, uid)
        get_folder(uid, target_folder)
        record = ctx.store["contacts"].get(contact_id) if contact_id else None
        if contact_id and (
            not record or record["_owner"] != uid or (fid and record["parentFolderId"] != fid)
        ):
            raise c.graph_error(404, "ErrorItemNotFound", "The specified contact was not found.")
        if request.method == "GET":
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
            await c.publish_change(ctx, f"users/{uid}/contacts/{contact_id}", "deleted", record)
            return Response(status_code=204)
        payload = await request.json()
        if request.method == "POST":
            record = {
                "id": s.outlook_id(ctx),
                "parentFolderId": target_folder,
                "_owner": uid,
                "createdDateTime": s.now(ctx),
                "categories": [],
                "emailAddresses": [],
                "businessPhones": [],
                "givenName": "",
                "surname": "",
                "displayName": "",
                "companyName": "",
                "jobTitle": "",
                "mobilePhone": None,
                "businessAddress": {},
            }
        record.update(
            {
                k: v
                for k, v in payload.items()
                if not k.startswith("_") and k not in ("id", "parentFolderId", "createdDateTime")
            }
        )
        record["lastModifiedDateTime"] = s.now(ctx)
        if not record["displayName"]:
            record["displayName"] = f"{record['givenName']} {record['surname']}".strip()
        ctx.store["contacts"].put(record)
        await c.publish_change(
            ctx,
            f"users/{uid}/contacts/{record['id']}",
            "created" if request.method == "POST" else "updated",
            record,
        )
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
            router.add_api_route(
                f"{base}/{prefix}/{{contact_id}}", contacts, methods=["GET", "PATCH", "DELETE"]
            )
    for version in c.GRAPH_VERSIONS:
        app.include_router(router, prefix=f"/{version}")
