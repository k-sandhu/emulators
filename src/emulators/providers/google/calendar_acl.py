"""Mutable calendar ACL rules and calendar-list change versions."""

from __future__ import annotations

import copy
from typing import Any

from fastapi import FastAPI, Request, Response

from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.google.calendar_model import PREFIX, calendar_access
from emulators.providers.google.common import google_error, json_object


def list_revision(ctx: ProviderContext, entry: dict[str, Any]) -> None:
    ctx.store.meta["calendar_revision"] += 1
    entry["_rev"] = ctx.store.meta["calendar_revision"]
    entry["etag"] = '"' + stable_hash(entry["id"], entry["_rev"]) + '"'


def acl_rows(ctx: ProviderContext, cal: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {"scope": {"type": "user", "value": ctx.world.person(pid).email}, "role": role}
        for pid, role in cal["_acl"].items()
    ]
    if cal.get("_domainRole"):
        rows.append({"scope": {"type": "domain", "value": ctx.world.org.domain}, "role": cal["_domainRole"]})
    rows += copy.deepcopy(cal.get("_extraAcl", []))
    return [
        {
            "kind": "calendar#aclRule",
            "id": r["scope"]["type"] + (":" + r["scope"]["value"] if "value" in r["scope"] else ""),
            "etag": '"' + stable_hash(cal["id"], r) + '"',
            **r,
        }
        for r in rows
    ]


def register(app: FastAPI, ctx: ProviderContext) -> None:
    def owner(request: Request, calendar_id: str) -> dict[str, Any]:
        cal, role, _ = calendar_access(request, ctx, calendar_id)
        if role != "owner":
            raise google_error(403, "Only owners may modify calendar ACLs", reason="forbidden")
        return cal

    def save(cal: dict[str, Any], body: dict[str, Any], old: dict[str, Any] | None = None) -> dict[str, Any]:
        scope = old["scope"] if old else body.get("scope", {})
        role = body.get("role", old["role"] if old else "none")
        kind, value = scope.get("type"), scope.get("value")
        if (
            kind not in ("user", "group", "domain", "default")
            or (kind != "default" and not value)
            or role not in ("none", "freeBusyReader", "reader", "writer", "owner")
        ):
            raise google_error(400, "Invalid ACL rule", reason="invalid")
        rule_id = kind + (":" + value if value else "")
        if not old and any(r["id"] == rule_id for r in acl_rows(ctx, cal)):
            raise google_error(409, "The requested identifier already exists.", reason="duplicate")
        if old and old["role"] == "owner" and role != "owner":
            raise google_error(403, "Cannot remove calendar owner", reason="forbidden")
        person = ctx.world.person_by_email(value) if kind == "user" else None
        if person:
            if role == "none":
                cal["_acl"].pop(person.id, None)
            else:
                cal["_acl"][person.id] = role
        elif kind == "domain" and value == ctx.world.org.domain:
            cal["_domainRole"] = role if role != "none" else None
        else:
            cal["_extraAcl"] = [r for r in cal.get("_extraAcl", []) if r["scope"] != scope]
            if role != "none":
                cal["_extraAcl"].append({"scope": copy.deepcopy(scope), "role": role})
        if person:
            entry = ctx.store["calendar_list"].get(person.id + ":" + cal["id"])
            if entry:
                entry["accessRole"] = role
                if role == "none":
                    entry["deleted"] = True
                list_revision(ctx, entry)
        return {
            "kind": "calendar#aclRule",
            "id": rule_id,
            "scope": scope,
            "role": role,
            "etag": '"' + stable_hash(cal["id"], {"scope": scope, "role": role}) + '"',
        }

    @app.post(PREFIX + "/calendars/{calendar_id}/acl")
    async def insert(request: Request, calendar_id: str) -> dict[str, Any]:
        return save(owner(request, calendar_id), await json_object(request))

    @app.api_route(
        PREFIX + "/calendars/{calendar_id}/acl/{rule_id}", methods=["GET", "PATCH", "PUT", "DELETE"]
    )
    async def item(request: Request, calendar_id: str, rule_id: str) -> Any:
        cal = owner(request, calendar_id)
        old = next((r for r in acl_rows(ctx, cal) if r["id"] == rule_id), None)
        if old is None:
            raise google_error(404, "Not Found", reason="notFound")
        if request.method == "GET":
            return old
        if request.method == "DELETE":
            save(cal, {"role": "none"}, old)
            return Response(status_code=204)
        return save(cal, await json_object(request), old)
