"""Graph sharing permissions, including inherited grants and revocable direct grants."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import parse_instant
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common
from emulators.providers.microsoft import drive_model as dm


def valid_roles(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or any(r not in ("read", "write", "owner") for r in value):
        raise common.graph_error(400, "invalidRequest", "roles must contain read, write, or owner.")
    return value


async def handle(
    request: Request, ctx: ProviderContext, item: dict[str, Any], operation: str, p: common.Principal
) -> Any:
    if request.method != "GET":
        dm.require_item(ctx, item["_drive"], item["id"], p, write=True)
    if operation == "permissions" and request.method == "GET":
        return dm.collection(request, dm.permissions(ctx, item), context="Collection(permission)")
    if operation.startswith("permissions/"):
        pid = operation.split("/", 1)[1]
        permission = next((r for r in dm.permissions(ctx, item) if r["id"] == pid), None)
        if permission is None:
            raise common.item_not_found()
        if request.method == "GET":
            return dm.single(request, permission, "permission")
        if "inheritedFrom" in permission:
            raise common.access_denied("Inherited permissions must be changed on the parent item.")
        if request.method == "DELETE":
            ctx.store[dm.PERMISSIONS].delete(pid)
            await dm.changed(ctx, item)
            return Response(status_code=204)
        if request.method == "PATCH":
            payload = await dm.json_body(request)
            if set(payload) - {"roles", "expirationDateTime"}:
                raise common.graph_error(
                    400, "invalidRequest", "Only roles and expirationDateTime can be updated."
                )
            if "roles" in payload:
                valid_roles(payload["roles"])
            if payload.get("expirationDateTime"):
                try:
                    parse_instant(payload["expirationDateTime"])
                except (ValueError, TypeError, AttributeError):
                    raise common.graph_error(400, "invalidRequest", "Invalid expirationDateTime.") from None
            permission.update(payload)
            ctx.store[dm.PERMISSIONS].put(permission)
            await dm.changed(ctx, item)
            return dm.public(permission)
    if operation == "invite" and request.method == "POST":
        payload = await dm.json_body(request)
        roles = valid_roles(payload.get("roles"))
        recipients = payload.get("recipients")
        if not isinstance(recipients, list) or not recipients:
            raise common.graph_error(400, "invalidRequest", "At least one recipient is required.")
        resolved: list[tuple[str, str]] = []
        for recipient in recipients:
            if not isinstance(recipient, dict):
                raise common.graph_error(400, "invalidRequest", "Each recipient must be an object.")
            email = recipient.get("email")
            person = ctx.world.person_by_email(email) if isinstance(email, str) else None
            group = next((g for g in ctx.world.groups if g.email == email or common.group_id(ctx.world, g.id) == recipient.get("objectId")), None)
            if person is None and recipient.get("objectId"):
                try:
                    person = ctx.world.person(dm.person_id(ctx, recipient["objectId"]))
                except EmulatorError:
                    person = None
            if person is None and group is None:
                raise common.graph_error(
                    400, "invalidRequest", "The recipient could not be resolved in this tenant."
                )
            resolved.append((person.id, person.email) if person else (group.id, group.email))
        if payload.get("retainInheritedPermissions") is False:
            item["_restricted"] = True
        results = [
            dm.add_permission(
                ctx,
                item,
                target,
                roles,
                invitation={"email": email, "signInRequired": payload.get("requireSignIn", True)},
            )
            for target, email in resolved
        ]
        await dm.changed(ctx, item)
        return {"value": [dm.public(r) for r in results]}
    if operation == "createLink" and request.method == "POST":
        payload = await dm.json_body(request)
        kind, scope = payload.get("type", "view"), payload.get("scope", "organization")
        if kind not in ("view", "edit", "embed") or scope not in ("anonymous", "organization", "users"):
            raise common.graph_error(400, "invalidRequest", "Invalid sharing link type or scope.")
        if payload.get("expirationDateTime"):
            try:
                parse_instant(payload["expirationDateTime"])
            except (ValueError, TypeError, AttributeError):
                raise common.graph_error(400, "invalidRequest", "Invalid expirationDateTime.") from None
        old = next(
            (
                r
                for r in dm.permissions(ctx, item)
                if r.get("_item") == item["id"]
                and r.get("link", {}).get("type") == kind
                and r["link"].get("scope") == scope
                and r.get("_client") == p.client_id
            ),
            None,
        )
        if old:
            return JSONResponse(dm.public(old), status_code=200)
        perm = dm.add_permission(
            ctx,
            item,
            None,
            ["write" if kind == "edit" else "read"],
            link={
                "type": kind,
                "scope": scope,
                "webUrl": f"https://{dm.HOST}/:w:/s/{ctx.store[dm.DRIVES].get(item['_drive'])['_space']}/{ctx.ids.urlsafe(32)}",
                "application": {"id": p.client_id, "displayName": "Emulator client"},
            },
            hasPassword=bool(payload.get("password")),
            _client=p.client_id,
        )
        if payload.get("expirationDateTime"):
            perm["expirationDateTime"] = payload["expirationDateTime"]
        await dm.changed(ctx, item)
        return JSONResponse(dm.public(perm), status_code=201)
    raise common.graph_error(405, "methodNotAllowed", "The HTTP method is not allowed on this resource.")
