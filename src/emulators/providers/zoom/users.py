"""Users, groups and user settings (``/v2/users``, ``/v2/groups``)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, Request, Response

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.providers.zoom import lookup, shapes
from emulators.providers.zoom import webhooks as hooks
from emulators.providers.zoom.common import (
    E_VALIDATION,
    Need,
    authenticate,
    invalid_field,
    json_body,
    no_permission,
    owner_person,
    paginate,
    query_int,
    respond,
    runtime_b64_22,
    zoom_error,
    zoom_token,
)

LIST_USERS = Need("user:read", "user:read:list_users", admin_only=True)
READ_USER = Need("user:read", "user:read:user")
WRITE_USER = Need("user:write", "user:update:user")
CREATE_USER = Need("user:write", "user:write:user", admin_only=True)
DELETE_USER = Need("user:write", "user:delete:user", admin_only=True)
READ_SETTINGS = Need("user:read", "user:read:settings")
WRITE_SETTINGS = Need("user:write", "user:update:settings")
LIST_GROUPS = Need("group:read", "group:read:list_groups", admin_only=True)
READ_GROUP = Need("group:read", "group:read:group", admin_only=True)
WRITE_GROUP = Need("group:write", "group:write:group", admin_only=True)
GROUP_MEMBERS = Need("group:read", "group:read:list_group_members", admin_only=True)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USER_TYPES = (1, 2, 3, 99)
PATCHABLE = (
    "first_name last_name display_name type pmi timezone dept cost_center language company location job_title "
    "phone_number host_key manager vanity_name use_pmi verified"
).split()


def user_event(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rec["id"],
        "first_name": rec["first_name"],
        "last_name": rec["last_name"],
        "email": rec["email"],
        "type": rec["type"],
        "created_at": rec["created_at"],
    }


def register(app: FastAPI, ctx: ProviderContext) -> None:
    users = ctx.store.collection("users")
    groups = ctx.store.collection("groups")

    def operator(principal: Any) -> Any:
        return principal.person

    # -- users --------------------------------------------------------------------------

    @app.get("/v2/users")
    async def list_users(request: Request) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(LIST_USERS):
            raise no_permission()
        q = request.query_params
        status = q.get("status", "active")
        if status not in ("active", "inactive", "pending"):
            raise invalid_field("status")
        rows = [u for u in lookup.users(ctx) if u["status"] == status]
        if q.get("role_id") not in (None, ""):
            rows = [u for u in rows if u["role_id"] == q["role_id"]]
        if q.get("license") in ("basic", "licensed"):
            rows = [u for u in rows if (u["type"] == 1) == (q["license"] == "basic")]
        rows.sort(key=lambda u: (u["last_name"].lower(), u["first_name"].lower()))
        fields = set((q.get("include_fields") or "").split(","))
        page, meta = paginate(request, rows)
        body = {**meta, "users": [shapes.user_list_item(u, fields) for u in page]}
        return respond(request, ctx, body)

    @app.post("/v2/users")
    async def create_user(request: Request) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(CREATE_USER):
            raise no_permission()
        data = await json_body(request)
        action = data.get("action")
        info = data.get("user_info") or {}
        if action not in ("create", "autoCreate", "custCreate", "ssoCreate"):
            raise invalid_field("action")
        email = str(info.get("email", "")).strip()
        if not EMAIL_RE.match(email):
            raise invalid_field("user_info.email")
        if info.get("type") not in USER_TYPES:
            raise invalid_field("user_info.type")
        if lookup.find_user(ctx, email):
            raise zoom_error(409, 1005, f"User already in the account: {email}")
        first, last = info.get("first_name", ""), info.get("last_name", "")
        now = ctx.clock.now()
        zid = runtime_b64_22(ctx)
        rec = {
            "id": zid,
            "first_name": first,
            "last_name": last,
            "display_name": info.get("display_name") or f"{first} {last}".strip(),
            "email": email,
            "type": info["type"],
            "pmi": int(ctx.ids.digits(10)),
            "timezone": "",
            "verified": 0 if action == "create" else 1,
            "dept": "",
            "created_at": isoformat_z(now),
            "last_login_time": "",
            "last_client_version": "",
            "pic_url": "",
            "language": "en-US",
            "phone_number": "",
            "status": "pending" if action == "create" else "active",
            "role_id": "2",
            "employee_unique_id": "",
            "user_created_at": isoformat_z(now),
            "group_ids": [],
            "role_name": "Member",
            "use_pmi": False,
            "personal_meeting_url": "",
            "cms_user_id": runtime_b64_22(ctx),
            "jid": f"{ctx.ids.hex(32)}@xmpp.zoom.us",
            "account_id": ctx.world.org.zoom_account_id,
            "account_number": lookup.owner_user(ctx)["account_number"],
            "cluster": "us06",
            "host_key": ctx.ids.digits(6),
            "job_title": "",
            "company": "",
            "location": "",
            "login_types": [100],
            "phone_country": "",
            "cost_center": "",
            "manager": "",
            "vanity_url": "",
            "_person_id": None,
        }
        users.put(rec)
        await hooks.emit(ctx, "user.created", user_event(rec), operator=operator(principal))
        return respond(
            request,
            ctx,
            {"id": zid, "first_name": first, "last_name": last, "email": email, "type": info["type"]},
            status=201,
        )

    @app.get("/v2/users/email")
    async def user_email_exists(request: Request) -> Response:
        principal = authenticate(ctx, request)
        principal.authorize(READ_USER)
        email = request.query_params.get("email", "")
        if not EMAIL_RE.match(email):
            raise invalid_field("email")
        return respond(request, ctx, {"existed_email": lookup.find_user(ctx, email) is not None})

    @app.get("/v2/users/{user_id}")
    async def get_user(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(READ_USER)
        rec = lookup.acting_user(ctx, principal, user_id, admin_level=admin)
        fields = set((request.query_params.get("include_fields") or "").split(","))
        return respond(request, ctx, shapes.user_detail(rec, fields))

    @app.patch("/v2/users/{user_id}")
    async def patch_user(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(WRITE_USER)
        rec = lookup.acting_user(ctx, principal, user_id, admin_level=admin)
        data = await json_body(request)
        if "type" in data:
            if not admin:
                raise no_permission()
            if data["type"] not in USER_TYPES:
                raise invalid_field("type")
        if "host_key" in data and not re.fullmatch(r"\d{6,10}", str(data["host_key"])):
            raise invalid_field("host_key")
        if "pmi" in data and not re.fullmatch(r"\d{10}", str(data["pmi"])):
            raise invalid_field("pmi")
        old: dict[str, Any] = {}
        for key in PATCHABLE:
            if key in data:
                old[key] = rec.get(key)
                rec[key] = int(data[key]) if key == "pmi" else data[key]
        if data.get("group_id") and groups.get(data["group_id"]):
            members = groups.get(data["group_id"])["_member_ids"]
            if rec["id"] not in members:
                members.append(rec["id"])
                rec["group_ids"].append(data["group_id"])
        if "first_name" in data or "last_name" in data:
            if "display_name" not in data:
                rec["display_name"] = f"{rec['first_name']} {rec['last_name']}".strip()
        changed = {k: rec[k] for k in old}
        if changed:
            await hooks.emit(
                ctx,
                "user.updated",
                {"id": rec["id"], **changed},
                operator=operator(principal),
                payload_extra={"old_object": {"id": rec["id"], **old}, "time_stamp": ctx.clock.timestamp() * 1000},
            )
        return Response(status_code=204)

    @app.delete("/v2/users/{user_id}")
    async def delete_user(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(DELETE_USER):
            raise no_permission()
        rec = lookup.acting_user(ctx, principal, user_id, admin_level=True)
        if rec.get("_person_id") == owner_person(ctx.world).id:
            raise zoom_error(400, E_VALIDATION, "Cannot delete the account owner.")
        action = request.query_params.get("action", "disassociate")
        if action not in ("disassociate", "delete"):
            raise invalid_field("action")
        users.delete(rec["id"])
        for group in groups:
            if rec["id"] in group["_member_ids"]:
                group["_member_ids"].remove(rec["id"])
        if rec.get("_person_id"):
            ctx.tokens.revoke_user(rec["_person_id"])
        await hooks.emit(ctx, "user.deleted", user_event(rec), operator=operator(principal))
        return Response(status_code=204)

    @app.put("/v2/users/{user_id}/status")
    async def user_status(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(Need("user:write", "user:update:user_status", admin_only=True)):
            raise no_permission()
        rec = lookup.acting_user(ctx, principal, user_id, admin_level=True)
        action = (await json_body(request)).get("action")
        if action not in ("activate", "deactivate"):
            raise invalid_field("action")
        if action == "deactivate" and rec.get("_person_id") == owner_person(ctx.world).id:
            raise zoom_error(400, E_VALIDATION, "Cannot deactivate the account owner.")
        rec["status"] = "active" if action == "activate" else "inactive"
        if action == "deactivate" and rec.get("_person_id"):
            ctx.tokens.revoke_user(rec["_person_id"])
        await hooks.emit(
            ctx, f"user.{action}d", {"id": rec["id"], "email": rec["email"], "type": rec["type"]}, operator=operator(principal)
        )
        return Response(status_code=204)

    @app.get("/v2/users/{user_id}/settings")
    async def get_settings(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(READ_SETTINGS)
        rec = lookup.acting_user(ctx, principal, user_id, admin_level=admin)
        settings = shapes.user_settings(rec)
        for section, values in (rec.get("_settings") or {}).items():
            settings.setdefault(section, {}).update(values)
        option = request.query_params.get("option")
        if option:
            wanted = {
                "meeting_authentication": ("schedule_meeting",),
                "recording_authentication": ("recording",),
                "meeting_security": ("in_meeting",),
            }.get(option, ())
            settings = {k: v for k, v in settings.items() if k in wanted}
        return respond(request, ctx, settings)

    @app.patch("/v2/users/{user_id}/settings")
    async def patch_settings(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(WRITE_SETTINGS)
        rec = lookup.acting_user(ctx, principal, user_id, admin_level=admin)
        data = await json_body(request)
        overlay = rec.setdefault("_settings", {})
        for section, values in data.items():
            if isinstance(values, dict):
                overlay.setdefault(section, {}).update(values)
        return Response(status_code=204)

    @app.get("/v2/users/{user_id}/token")
    async def user_token(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(Need("user:read", "user:read:token"))
        rec = lookup.acting_user(ctx, principal, user_id, admin_level=admin)
        kind = request.query_params.get("type", "zak")
        if kind not in ("zak", "token"):
            raise invalid_field("type")
        ttl = query_int(request, "ttl", 7200, minimum=0, maximum=7776000)
        token = zoom_token(ctx, kind=kind, uid=rec["id"], client_id="zoom-user-token", ttl_s=ttl)
        return respond(request, ctx, {"token": token})

    @app.get("/v2/users/{user_id}/permissions")
    async def user_permissions(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(Need("user:read", "user:read:permissions"))
        rec = lookup.acting_user(ctx, principal, user_id, admin_level=admin)
        perms = ["Meeting:Read", "Meeting:Edit", "Recording:Read", "Recording:Edit", "User:Read"]
        if rec["role_id"] in ("0", "1"):
            perms += ["User:Edit", "Group:Read", "Group:Edit", "Report:Read", "Dashboard:Read"]
        return respond(request, ctx, {"permissions": perms})

    # -- groups -------------------------------------------------------------------------

    def find_group(group_id: str) -> dict[str, Any]:
        group = groups.get(group_id)
        if group is None:
            raise zoom_error(404, 4130, f"The group does not exist: {group_id}.")
        return group

    def member_view(zid: str, group_id: str) -> dict[str, Any] | None:
        user = users.get(zid)
        if user is None:
            return None
        return {
            "id": user["id"],
            "email": user["email"],
            "first_name": user["first_name"],
            "last_name": user["last_name"],
            "type": user["type"],
            "primary_group": bool(user["group_ids"]) and user["group_ids"][0] == group_id,
        }

    @app.get("/v2/groups")
    async def list_groups(request: Request) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(LIST_GROUPS):
            raise no_permission()
        page, meta = paginate(request, groups.values())
        body = {
            "groups": [shapes.group_wire(g) for g in page],
            "next_page_token": meta["next_page_token"],
            "page_size": meta["page_size"],
            "total_records": meta["total_records"],
        }
        return respond(request, ctx, body)

    @app.post("/v2/groups")
    async def create_group(request: Request) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(WRITE_GROUP):
            raise no_permission()
        name = str((await json_body(request)).get("name", "")).strip()
        if not name or len(name) > 128:
            raise invalid_field("name")
        if any(g["name"].lower() == name.lower() for g in groups):
            raise zoom_error(409, 4132, f"A group with this name already exists: {name}.")
        group = groups.put({"id": runtime_b64_22(ctx), "name": name, "_member_ids": []})
        return respond(request, ctx, shapes.group_wire(group), status=201)

    @app.get("/v2/groups/{group_id}")
    async def get_group(request: Request, group_id: str) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(READ_GROUP):
            raise no_permission()
        return respond(request, ctx, shapes.group_wire(find_group(group_id)))

    @app.patch("/v2/groups/{group_id}")
    async def patch_group(request: Request, group_id: str) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(WRITE_GROUP):
            raise no_permission()
        group = find_group(group_id)
        name = str((await json_body(request)).get("name", "")).strip()
        if not name:
            raise invalid_field("name")
        group["name"] = name
        return Response(status_code=204)

    @app.delete("/v2/groups/{group_id}")
    async def delete_group(request: Request, group_id: str) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(WRITE_GROUP):
            raise no_permission()
        group = find_group(group_id)
        for zid in group["_member_ids"]:
            user = users.get(zid)
            if user and group_id in user["group_ids"]:
                user["group_ids"].remove(group_id)
        groups.delete(group_id)
        return Response(status_code=204)

    @app.get("/v2/groups/{group_id}/members")
    async def group_members(request: Request, group_id: str) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(GROUP_MEMBERS):
            raise no_permission()
        group = find_group(group_id)
        members = [m for m in (member_view(z, group_id) for z in group["_member_ids"]) if m]
        page, meta = paginate(request, members)
        return respond(request, ctx, {**meta, "members": page})

    @app.post("/v2/groups/{group_id}/members")
    async def add_group_members(request: Request, group_id: str) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(WRITE_GROUP):
            raise no_permission()
        group = find_group(group_id)
        data = await json_body(request)
        added = []
        for entry in data.get("members") or []:
            user = lookup.find_user(ctx, entry.get("id") or entry.get("email") or "")
            if user is None:
                raise zoom_error(404, 1001, f"User does not exist: {entry.get('id') or entry.get('email')}.")
            if user["id"] not in group["_member_ids"]:
                group["_member_ids"].append(user["id"])
                user["group_ids"].append(group_id)
            added.append(user["id"])
        return respond(request, ctx, {"ids": ",".join(added), "added_at": isoformat_z(ctx.clock.now())}, status=201)

    @app.delete("/v2/groups/{group_id}/members/{member_id}")
    async def remove_group_member(request: Request, group_id: str, member_id: str) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(WRITE_GROUP):
            raise no_permission()
        group = find_group(group_id)
        user = lookup.find_user(ctx, member_id)
        if user is None or user["id"] not in group["_member_ids"]:
            raise zoom_error(404, 1001, f"User does not exist: {member_id}.")
        group["_member_ids"].remove(user["id"])
        if group_id in user["group_ids"]:
            user["group_ids"].remove(group_id)
        return Response(status_code=204)
