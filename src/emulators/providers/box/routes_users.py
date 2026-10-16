"""Users, groups and group memberships (enterprise administration)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.provider import ProviderContext
from emulators.providers.box import db, errors, events
from emulators.providers.box.access import Principal
from emulators.providers.box.routing import Api, created, no_content
from emulators.providers.box.shapes import Shaper
from emulators.providers.box.util import (
    json_body,
    next_id,
    now_iso,
    object_or_empty,
    opt_bool,
    opt_int,
    opt_str,
    page_window,
    paginate,
    parse_fields,
    query_list,
    random_alnum,
    ref_id,
    require_str,
)
from emulators.world import documents

Record = dict[str, Any]
#: Roles the API can hand out: Box has no call that makes someone an enterprise admin.
ROLES = ("user", "coadmin")
MAX_AVATAR = 1024 * 1024
IMAGE_MAGIC = (
    (bytes.fromhex("89504e470d0a1a0a"), "image/png"),
    (bytes.fromhex("ffd8ff"), "image/jpeg"),
    (b"GIF8", "image/gif"),
)
GROUP_LEVELS = ("admins_only", "admins_and_members", "all_managed_users")
USER_STATUSES = ("active", "inactive", "cannot_delete_edit", "cannot_delete_edit_upload")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
USER_STRINGS = ("name", "job_title", "phone", "address", "language", "timezone")
USER_FLAGS = (
    "can_see_managed_users", "is_sync_enabled", "is_external_collab_restricted",
    "is_exempt_from_device_limits", "is_exempt_from_login_verification",
)  # fmt: skip


def user_fields(body: dict[str, Any]) -> dict[str, Any]:
    """The editable user attributes present in ``body``, type-checked as Box does (400 otherwise)."""
    clean: dict[str, Any] = {}
    for key in USER_STRINGS:
        if key in body:
            value = opt_str(body, key)
            if key == "name" and not value.strip():
                raise errors.invalid_parameter("name", "'name' must not be empty")
            clean[key] = value
    for key in USER_FLAGS:
        if key in body:
            clean[key] = opt_bool(body, key, True)
    if "space_amount" in body:
        clean["space_amount"] = opt_int(body, "space_amount", -1, minimum=-1)
    if "tracking_codes" in body:
        codes = body["tracking_codes"]
        if codes is not None and (not isinstance(codes, list) or not all(isinstance(c, dict) for c in codes)):
            raise errors.invalid_parameter("tracking_codes", "'tracking_codes' must be an array of objects")
        clean["tracking_codes"] = list(codes or [])
    return clean


def valid_login(value: Any, *, name: str = "login") -> str:
    if not isinstance(value, str) or not EMAIL.fullmatch(value.strip()):
        raise errors.invalid_parameter(name, f"'{name}' must be an e-mail address")
    return value.strip()


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)

    def user_or_404(p: Principal, user_id: str) -> Record:
        record = p.user if user_id == "me" else db.users(ctx).get(user_id)
        if record is None or (record.get("_kind") == "service" and not p.is_privileged):
            raise errors.not_found()
        if not p.can_see_user(record):
            raise errors.forbidden()
        return record

    # -- users ---------------------------------------------------------------------------
    @api.get("/users/me")
    async def me(request: Request) -> Any:
        p = api.principal(request)
        if p.anonymous:
            raise errors.unauthorized()
        return Shaper(p, request).user(p.user, parse_fields(request), full=True)

    @api.get("/users")
    async def list_users(request: Request) -> Any:
        p = api.principal(request)
        p.require_privileged()
        term = (request.query_params.get("filter_term") or "").strip().lower()
        kind = request.query_params.get("user_type", "all")
        if kind not in ("all", "managed", "external"):
            raise errors.invalid_parameter("user_type", "'user_type' must be all, managed or external")
        wanted = query_list(request, "external_app_user_id")
        everyone = list(db.users(ctx))
        # Outsiders are never listed with the enterprise: only an exact login match finds one.
        exact_external = [
            u for u in everyone if u.get("_kind") == "external" and term and u["login"].lower() == term
        ]
        if kind == "external" or (kind == "all" and exact_external):
            records = exact_external
        else:
            records = [
                u
                for u in everyone
                if u.get("_kind") != "external"
                and (not term or term in u["name"].lower() or term in u["login"].lower())
                and (not wanted or u.get("external_app_user_id") in wanted)
            ]
        records.sort(key=lambda u: (u["name"].lower(), u["id"]))
        offset, limit, use_marker = page_window(request, default_limit=100, max_limit=1000)
        body = paginate(records, offset, limit, use_marker, order=[{"by": "name", "direction": "ASC"}])
        fields = parse_fields(request)
        shaper = Shaper(p, request)
        body["entries"] = [shaper.user(u, fields) for u in body["entries"]]
        return body

    @api.post("/users")
    async def create_user(request: Request) -> Any:
        p = api.principal(request)
        p.require_privileged()
        body = await json_body(request)
        name = require_str(body, "name")
        app_user = opt_bool(body, "is_platform_access_only")
        login = body.get("login")
        if not app_user and not login:
            raise errors.missing_parameter("login")
        if login:
            login = valid_login(login)
        role = body.get("role", "user")
        if role != "user" and not p.is_admin:
            raise errors.forbidden()
        if role not in ROLES:
            raise errors.invalid_parameter("role", "'role' must be user or coadmin")
        status = body.get("status", "active")
        if status not in USER_STATUSES:
            raise errors.invalid_parameter("status", "Invalid status")
        fields = user_fields(body)
        uid = next_id(ctx, "user", db.users(ctx), start=db.USER_ID_START)
        if not login:
            login = f"AppUser_{uid}_{random_alnum(10)}@boxdevedition.com"
        if db.user_by_login(ctx, login) is not None:
            raise errors.box_error(
                409, "user_login_already_used", "Item with the same name already exists",
                context_info={"errors": [{"reason": "user_login_already_used", "name": "login"}]},
            )  # fmt: skip
        now = now_iso(ctx)
        record: Record = {
            "type": "user",
            "id": uid,
            "name": name,
            "login": login,
            "created_at": now,
            "modified_at": now,
            "language": "en",
            "timezone": "America/Toronto",
            "status": status,
            "job_title": "",
            "phone": "",
            "address": "",
            "role": role,
            "_pid": None,
            "_kind": "managed",
            "_root": db.root_id(uid),
            "is_platform_access_only": app_user,
        }
        record.update(fields)
        db.users(ctx).put(record)
        db.folders(ctx).put(
            {
                "type": "folder", "id": record["_root"], "name": "All Files", "_root_of": uid, "_owner": uid,
                "_parent": None, "_seq": 0, "created_at": now, "modified_at": now,
            }
        )  # fmt: skip
        ctx.store.collection(db.COLLECTIONS).put(
            {
                "type": "collection",
                "id": next_id(
                    ctx, "collection", ctx.store.collection(db.COLLECTIONS), start=db.SMALL_ID_START
                ),
                "user_id": uid,
                "name": "Favorites",
                "collection_type": "favorites",
                "items": {"file": [], "folder": []},
            }
        )
        events.append_event(ctx, "NEW_USER", created_by=p.uid, source=Shaper(p, request).user_mini(uid))
        return created(Shaper(p, request).user(record, parse_fields(request), full=True))

    @api.get("/users/{user_id}")
    async def get_user(request: Request, user_id: str) -> Any:
        p = api.principal(request)
        record = user_or_404(p, user_id)
        return Shaper(p, request).user(record, parse_fields(request), full=True)

    @api.put("/users/{user_id}")
    async def update_user(request: Request, user_id: str) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        record = user_or_404(p, user_id)
        own = record["id"] == p.uid
        if not p.is_privileged and not own:
            raise errors.forbidden()
        restricted = {
            "role",
            "status",
            "enterprise",
            "is_exempt_from_device_limits",
            "space_amount",
            "can_see_managed_users",
        }
        if not p.is_privileged and restricted & set(body):
            raise errors.forbidden()
        if not own and not p.can_manage_user(record):
            raise errors.forbidden()
        # Validate everything first: a rejected request must change nothing.
        fields = user_fields(body)
        if "role" in body:
            if not p.is_admin:
                raise errors.forbidden()
            if body["role"] not in ROLES:
                raise errors.invalid_parameter("role", "'role' must be user or coadmin")
        if "status" in body and body["status"] not in USER_STATUSES:
            raise errors.invalid_parameter("status", "Invalid status")
        login = None
        if "login" in body and p.is_privileged:
            login = valid_login(body["login"])
            if db.user_by_login(ctx, login) not in (None, record):
                raise errors.box_error(
                    409, "user_login_already_used", "Item with the same name already exists"
                )
        email: Any = None
        if "notification_email" in body:
            email = object_or_empty(body["notification_email"], "notification_email").get("email")
            if email is not None:
                email = valid_login(email, name="notification_email.email")
        if "role" in body:
            record["role"] = body["role"]
        if "status" in body:
            record["status"] = body["status"]
            if not db.is_active(record):
                ctx.tokens.revoke_user(record["id"])
        if login is not None:
            record["login"] = login
        record.update(fields)
        if "notification_email" in body:
            record["notification_email"] = {"email": email, "is_confirmed": True} if email else None
        if "enterprise" in body and body["enterprise"] is None and p.is_privileged:
            record["_kind"] = "external"
        record["modified_at"] = now_iso(ctx)
        events.append_event(
            ctx, "EDIT_USER", created_by=p.uid, source=Shaper(p, request).user_mini(record["id"])
        )
        return Shaper(p, request).user(record, parse_fields(request), full=True)

    @api.delete("/users/{user_id}")
    async def delete_user(request: Request, user_id: str) -> Any:
        p = api.principal(request)
        p.require_privileged()
        record = db.users(ctx).get(user_id)
        if record is None:
            raise errors.not_found()
        if not p.can_manage_user(record):
            raise errors.forbidden()
        force = request.query_params.get("force", "false").lower() == "true"
        owned = [f for f in db.files(ctx) if f["_owner"] == record["id"]]
        if owned and not force:
            raise errors.box_error(
                400, "user_has_content", "The user has content; delete it first or pass force=true"
            )
        from emulators.providers.box import ops

        root = db.folders(ctx).get(record["_root"])
        if root is not None:
            for child in list(p.tree.all_children(root["id"])):
                ops.drop_item(ctx, child)
            db.folders(ctx).delete(root["id"])
        for membership in [m for m in db.memberships(ctx) if m["user_id"] == record["id"]]:
            db.memberships(ctx).delete(membership["id"])
        for collab in [
            c
            for c in db.collabs(ctx)
            if c["accessible_type"] == "user" and c["accessible_id"] == record["id"]
        ]:
            db.collabs(ctx).delete(collab["id"])
        ctx.tokens.revoke_user(record["id"])
        db.users(ctx).delete(record["id"])
        events.append_event(
            ctx, "DELETE_USER", created_by=p.uid,
            source={"type": "user", "id": record["id"], "name": record["name"], "login": record["login"]},
        )  # fmt: skip
        return no_content()

    @api.get("/users/{user_id}/avatar")
    async def avatar(request: Request, user_id: str) -> Any:
        p = api.principal(request)
        record = user_or_404(p, user_id)
        return avatar_response(record)

    @app.get("/api/avatar/{size}/{user_id}", operation_id="public_avatar")
    async def avatar_public(size: str, user_id: str) -> Any:
        record = db.users(ctx).get(user_id)
        if record is None:
            raise errors.not_found()
        return avatar_response(record)

    def avatar_response(record: Record) -> Response:
        uploaded = record.get("_avatar")
        if uploaded:
            return Response(ctx.store.blobs.get(uploaded["blob"]), media_type=uploaded["media_type"])
        seed = int(record["id"]) % 1_000_000
        return Response(documents.make_png(64, 64, seed=seed), media_type="image/png")

    def avatar_target(p: Principal, user_id: str) -> Record:
        """Admins and the user themself change an avatar (managed accounts only)."""
        record = user_or_404(p, user_id)
        if not (p.is_privileged or record["id"] == p.uid) or record.get("_kind") == "service":
            raise errors.forbidden()
        return record

    @api.post("/users/{user_id}/avatar")
    async def upload_avatar(request: Request, user_id: str) -> Any:
        p = api.principal(request)
        record = avatar_target(p, user_id)
        try:
            form = await request.form()
        except Exception:  # malformed multipart body
            raise errors.bad_request("Invalid multipart request", reason="invalid_request_body") from None
        pic = form.get("pic")
        if pic is None or isinstance(pic, str):
            raise errors.missing_parameter("pic")
        data = await pic.read()
        if len(data) > MAX_AVATAR:
            raise errors.box_error(413, "request_entity_too_large", "The avatar must be 1MB or smaller")
        media_type = next((kind for magic, kind in IMAGE_MAGIC if data.startswith(magic)), None)
        if media_type is None:
            raise errors.box_error(400, "bad_request", "The avatar must be a PNG, JPEG or GIF image")
        record["_avatar"] = {"blob": ctx.store.blobs.put(data), "media_type": media_type}
        shaper = Shaper(p, request)
        return {
            "pic_urls": {
                size: shaper.host_url("app.box.com", f"/api/avatar/{size}/{record['id']}")
                for size in ("small", "large", "preview")
            }
        }

    @api.delete("/users/{user_id}/avatar")
    async def delete_avatar(request: Request, user_id: str) -> Any:
        p = api.principal(request)
        record = avatar_target(p, user_id)
        if not record.get("_avatar"):
            raise errors.not_found("The user has no uploaded avatar")
        record.pop("_avatar")
        return no_content()

    # -- e-mail aliases ------------------------------------------------------------------
    def alias_body(alias: Record) -> Record:
        return {
            "type": "email_alias",
            "id": alias["id"],
            "is_confirmed": bool(alias.get("is_confirmed", True)),
            "email": alias["email"],
        }

    def managed_target(p: Principal, user_id: str) -> Record:
        p.require_privileged()
        record = db.users(ctx).get(user_id)
        if record is None or record.get("_kind") in ("external", "service"):
            raise errors.not_found()
        if not p.can_manage_user(record):
            raise errors.forbidden()
        return record

    @api.get("/users/{user_id}/email_aliases")
    async def list_email_aliases(request: Request, user_id: str) -> Any:
        p = api.principal(request)
        record = managed_target(p, user_id)
        rows = [alias_body(a) for a in record.get("_aliases", [])]
        return {"total_count": len(rows), "entries": rows}

    @api.post("/users/{user_id}/email_aliases")
    async def create_email_alias(request: Request, user_id: str) -> Any:
        p = api.principal(request)
        record = managed_target(p, user_id)
        body = await json_body(request)
        email = valid_login(body.get("email"), name="email")
        taken = db.user_by_login(ctx, email) is not None or any(
            a["email"].lower() == email.lower() for u in db.users(ctx) for a in u.get("_aliases", [])
        )
        if taken:
            raise errors.box_error(
                409,
                "conflict",
                "Email alias already exists",
                context_info={"errors": [{"reason": "email_alias_conflict", "name": "email"}]},
            )
        alias = {
            "id": next_id(ctx, "alias", {}, start=db.SMALL_ID_START),
            "email": email,
            "is_confirmed": True,
        }
        record.setdefault("_aliases", []).append(alias)
        return created(alias_body(alias))

    @api.delete("/users/{user_id}/email_aliases/{alias_id}")
    async def delete_email_alias(request: Request, user_id: str, alias_id: str) -> Any:
        p = api.principal(request)
        record = managed_target(p, user_id)
        aliases = record.get("_aliases", [])
        if not any(a["id"] == alias_id for a in aliases):
            raise errors.not_found()
        record["_aliases"] = [a for a in aliases if a["id"] != alias_id]
        return no_content()

    @api.get("/users/{user_id}/memberships")
    async def user_memberships(request: Request, user_id: str) -> Any:
        p = api.principal(request)
        record = user_or_404(p, user_id)
        shaper = Shaper(p, request)
        rows = [m for m in db.memberships(ctx) if m["user_id"] == record["id"]]
        offset, limit, use_marker = page_window(request, default_limit=100, max_limit=1000)
        body = paginate(rows, offset, limit, use_marker)
        body["entries"] = [membership_body(shaper, m, parse_fields(request)) for m in body["entries"]]
        return body

    # -- groups --------------------------------------------------------------------------
    def membership_body(shaper: Shaper, membership: Record, fields: list[str] | None = None) -> Record:
        group = db.groups(ctx).get(membership["group_id"])
        full = {
            "type": "group_membership",
            "id": membership["id"],
            "user": shaper.user_mini(membership["user_id"]),
            "group": shaper.group_mini(group) if group else None,
            "role": membership["role"],
            "created_at": shaper_ts(membership["created_at"]),
            "modified_at": shaper_ts(membership["modified_at"]),
        }
        if fields is None:
            return full
        keep = {"type", "id", *[f.split(".")[0] for f in fields]}
        return {k: v for k, v in full.items() if k in keep}

    def shaper_ts(value: str) -> str | None:
        from emulators.providers.box.util import box_ts

        return box_ts(value)

    def group_or_404(
        p: Principal, group_id: str, *, manage: bool = False, admins_may: bool = False
    ) -> Record:
        """``manage`` is for enterprise admins only; ``admins_may`` also lets the group's own admins in."""
        group = db.groups(ctx).get(group_id)
        if group is None:
            raise errors.not_found()
        member = group_id in p.groups
        if manage and not (p.is_privileged or (admins_may and p.is_group_admin(group_id))):
            raise errors.forbidden()
        if not (p.is_privileged or member):
            raise errors.forbidden()
        return group

    def validate_group_body(body: dict[str, Any]) -> None:
        for key in ("invitability_level", "member_viewability_level"):
            if key in body and body[key] not in GROUP_LEVELS:
                raise errors.invalid_parameter(key, f"'{key}' must be one of {', '.join(GROUP_LEVELS)}")
        if "name" in body and not opt_str(body, "name").strip():
            raise errors.invalid_parameter("name", "'name' must not be empty")
        for key in ("description", "provenance", "external_sync_identifier"):
            if key in body:
                opt_str(body, key)

    @api.get("/groups")
    async def list_groups(request: Request) -> Any:
        p = api.principal(request)
        p.require_privileged()
        term = (request.query_params.get("filter_term") or "").lower()
        rows = sorted(
            (g for g in db.groups(ctx) if not term or g["name"].lower().startswith(term)),
            key=lambda g: g["name"].lower(),
        )
        offset, limit, use_marker = page_window(request, default_limit=100, max_limit=1000)
        body = paginate(rows, offset, limit, use_marker, order=[{"by": "name", "direction": "ASC"}])
        shaper = Shaper(p, request)
        fields = parse_fields(request)
        body["entries"] = [
            shaper.group_mini(g) if fields is None else shaper.group(g, fields) for g in body["entries"]
        ]
        return body

    @api.post("/groups")
    async def create_group(request: Request) -> Any:
        p = api.principal(request)
        p.require_privileged()
        body = await json_body(request)
        name = require_str(body, "name")
        validate_group_body(body)
        if any(g["name"].lower() == name.lower() for g in db.groups(ctx)):
            raise errors.box_error(
                409, "conflict", "A group with this name already exists",
                context_info={"errors": [{"reason": "name_already_exists", "name": "name"}]},
            )  # fmt: skip
        now = now_iso(ctx)
        gid = next_id(ctx, "group", db.groups(ctx), start=db.SMALL_ID_START)
        record = {
            "type": "group", "id": gid, "name": name, "group_type": "managed_group", "created_at": now,
            "modified_at": now, "provenance": body.get("provenance"),
            "external_sync_identifier": body.get("external_sync_identifier"),
            "description": body.get("description", ""),
            "invitability_level": body.get("invitability_level", "admins_and_members"),
            "member_viewability_level": body.get("member_viewability_level", "admins_and_members"),
        }  # fmt: skip
        db.groups(ctx).put(record)
        shaper = Shaper(p, request)
        events.append_event(ctx, "GROUP_CREATION", created_by=p.uid, source=shaper.group_mini(record))
        return created(shaper.group(record, parse_fields(request)))

    @api.get("/groups/{group_id}")
    async def get_group(request: Request, group_id: str) -> Any:
        p = api.principal(request)
        group = group_or_404(p, group_id)
        return Shaper(p, request).group(group, parse_fields(request))

    @api.put("/groups/{group_id}")
    async def update_group(request: Request, group_id: str) -> Any:
        p = api.principal(request)
        group = group_or_404(p, group_id, manage=True, admins_may=True)
        body = await json_body(request)
        validate_group_body(body)
        if "name" in body and any(
            g["id"] != group["id"] and g["name"].lower() == body["name"].lower() for g in db.groups(ctx)
        ):
            raise errors.box_error(
                409, "conflict", "A group with this name already exists",
                context_info={"errors": [{"reason": "name_already_exists", "name": "name"}]},
            )  # fmt: skip
        for key in (
            "name",
            "provenance",
            "external_sync_identifier",
            "description",
            "invitability_level",
            "member_viewability_level",
        ):
            if key in body:
                group[key] = body[key]
        group["modified_at"] = now_iso(ctx)
        shaper = Shaper(p, request)
        events.append_event(ctx, "GROUP_EDITED", created_by=p.uid, source=shaper.group_mini(group))
        return shaper.group(group, parse_fields(request))

    @api.delete("/groups/{group_id}")
    async def delete_group(request: Request, group_id: str) -> Any:
        p = api.principal(request)
        group = group_or_404(p, group_id, manage=True)
        for membership in [m for m in db.memberships(ctx) if m["group_id"] == group_id]:
            db.memberships(ctx).delete(membership["id"])
        for collab in [
            c for c in db.collabs(ctx) if c["accessible_type"] == "group" and c["accessible_id"] == group_id
        ]:
            db.collabs(ctx).delete(collab["id"])
        db.groups(ctx).delete(group_id)
        events.append_event(
            ctx, "GROUP_DELETION", created_by=p.uid, source=Shaper(p, request).group_mini(group)
        )
        return no_content()

    @api.get("/groups/{group_id}/memberships")
    async def group_memberships(request: Request, group_id: str) -> Any:
        p = api.principal(request)
        group = group_or_404(p, group_id)
        if not p.can_view_members(group):
            raise errors.forbidden()
        rows = [m for m in db.memberships(ctx) if m["group_id"] == group["id"]]
        offset, limit, use_marker = page_window(request, default_limit=100, max_limit=1000)
        body = paginate(rows, offset, limit, use_marker)
        shaper = Shaper(p, request)
        body["entries"] = [membership_body(shaper, m, parse_fields(request)) for m in body["entries"]]
        return body

    @api.post("/group_memberships")
    async def add_member(request: Request) -> Any:
        p = api.principal(request)
        p.require_privileged()
        body = await json_body(request)
        group = db.groups(ctx).get(ref_id(body, "group"))
        user = db.users(ctx).get(ref_id(body, "user"))
        if group is None or user is None:
            raise errors.not_found()
        role = body.get("role", "member")
        if role not in ("member", "admin"):
            raise errors.invalid_parameter("role", "'role' must be member or admin")
        if any(m["group_id"] == group["id"] and m["user_id"] == user["id"] for m in db.memberships(ctx)):
            raise errors.box_error(409, "conflict", "User is already a member of the group")
        now = now_iso(ctx)
        record = {
            "type": "group_membership",
            "id": next_id(ctx, "membership", db.memberships(ctx), start=db.SMALL_ID_START * 10),
            "user_id": user["id"], "group_id": group["id"], "role": role, "created_at": now, "modified_at": now,
        }  # fmt: skip
        db.memberships(ctx).put(record)
        shaper = Shaper(p, request)
        events.append_event(ctx, "GROUP_ADD_USER", created_by=p.uid, source=shaper.user_mini(user["id"]),
                            additional_details={"group": shaper.group_mini(group)})  # fmt: skip
        return created(membership_body(shaper, record, parse_fields(request)))

    def membership_or_404(p: Principal, membership_id: str) -> Record:
        record = db.memberships(ctx).get(membership_id)
        if record is None:
            raise errors.not_found()
        if not (p.is_privileged or record["user_id"] == p.uid or p.is_group_admin(record["group_id"])):
            raise errors.forbidden()
        return record

    @api.get("/group_memberships/{membership_id}")
    async def get_membership(request: Request, membership_id: str) -> Any:
        p = api.principal(request)
        return membership_body(Shaper(p, request), membership_or_404(p, membership_id), parse_fields(request))

    @api.put("/group_memberships/{membership_id}")
    async def update_membership(request: Request, membership_id: str) -> Any:
        p = api.principal(request)
        record = membership_or_404(p, membership_id)
        if not (p.is_privileged or p.is_group_admin(record["group_id"])):
            raise errors.forbidden()
        body = await json_body(request)
        if "role" in body:
            if body["role"] not in ("member", "admin"):
                raise errors.invalid_parameter("role", "'role' must be member or admin")
            record["role"] = body["role"]
        record["modified_at"] = now_iso(ctx)
        return membership_body(Shaper(p, request), record, parse_fields(request))

    @api.delete("/group_memberships/{membership_id}")
    async def remove_member(request: Request, membership_id: str) -> Any:
        p = api.principal(request)
        record = membership_or_404(p, membership_id)
        if not (p.is_privileged or p.is_group_admin(record["group_id"])):
            raise errors.forbidden()
        db.memberships(ctx).delete(record["id"])
        shaper = Shaper(p, request)
        group = db.groups(ctx).get(record["group_id"])
        events.append_event(ctx, "GROUP_REMOVE_USER", created_by=p.uid, source=shaper.user_mini(record["user_id"]),
                            additional_details={"group": shaper.group_mini(group)} if group else None)  # fmt: skip
        return no_content()
