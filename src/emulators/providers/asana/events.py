"""Webhook handshakes and signed deliveries use the shared dispatcher."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext
from emulators.providers.asana import common as c


async def emit(
    ctx: ProviderContext,
    grant: Grant,
    row: dict[str, Any],
    action: str,
    *,
    field: str | None = None,
    value: Any = None,
    affected: list[str] | None = None,
) -> None:
    targets = {row["gid"], *[p["gid"] for p in row.get("projects", [])], *(affected or [])}
    if row.get("workspace"):
        targets.add(row["workspace"]["gid"])
    parent = row.get("parent") or row.get("target")
    if parent:
        targets.add(parent["gid"])
        ancestor = c.find(ctx, parent["gid"])
        if ancestor:
            targets.update(p["gid"] for p in ancestor.get("projects", []))
            if ancestor.get("workspace"):
                targets.add(ancestor["workspace"]["gid"])
    event = {
        "resource": c.compact(row),
        "user": c.compact(ctx.store["users"].get(c.actor(ctx, grant))),
        "action": action,
        "created_at": c.stamp(ctx),
    }
    if field:
        event["change"] = {"field": field, "action": "changed"}
        if isinstance(value, dict) and "gid" in value and "resource_type" in value:
            event["change"]["new_value"] = {key: value[key] for key in ("gid", "resource_type")}
    for hook in ctx.store["webhooks"]:
        token = ctx.tokens.resolve(hook["_token"])
        if token is None:
            ctx.store["webhooks"].delete(hook["gid"])
            continue
        resource = c.find(ctx, hook["resource"]["gid"])
        if resource is None or not c.allowed(ctx, token, resource) or resource["gid"] not in targets:
            continue
        # A project watcher cannot see an otherwise inaccessible private task.
        if not row.get("_deleted") and not c.allowed(ctx, token, row):
            continue
        filters = hook["filters"]
        if filters and not any(
            rule.get("resource_type", row["resource_type"]) == row["resource_type"]
            and ("resource_subtype" not in rule or rule["resource_subtype"] == row.get("resource_subtype"))
            and rule.get("action", action) == action
            and (not rule.get("fields") or field in rule["fields"])
            for rule in filters
        ):
            continue
        delivered = dict(event)
        if resource["gid"] != row["gid"]:
            delivered["parent"] = c.compact(resource)
        body = json.dumps({"events": [delivered]}, ensure_ascii=False, allow_nan=False).encode()
        signature = hmac.new(hook["_secret"].encode(), body, hashlib.sha256).hexdigest()
        result = await ctx.webhooks.deliver(
            hook["target"],
            body,
            headers={"X-Hook-Signature": signature},
            event_type=f"{row['resource_type']}.{action}",
            metadata={"webhook_gid": hook["gid"]},
        )
        if result["response_status"] == 410:
            ctx.store["webhooks"].delete(hook["gid"])
        elif result["status"] == "delivered":
            hook.update(active=True, last_success_at=c.stamp(ctx), delivery_retry_count=0)
        elif result["status"] == "failed":
            hook.update(
                last_failure_at=c.stamp(ctx),
                last_failure_content=result["response_body"],
                delivery_retry_count=result["attempts"],
            )


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.post(c.BASE + "/webhooks")
    async def create(request: Request) -> Any:
        grant = c.principal(request, ctx, "webhooks:write")
        data = await c.payload(request)
        resource_id = c.string(data.get("resource"), "resource", empty=False)
        resource = c.find(ctx, resource_id)
        if resource is None or resource["resource_type"] not in ("task", "project", "workspace"):
            c.invalid("resource: Not a supported webhook resource")
        if not c.allowed(ctx, grant, resource):
            raise c.error(403, "Forbidden")
        target = c.string(data.get("target"), "target", empty=False)
        try:
            parsed = urlsplit(target)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError
            # Construct the actual transport request before any handshake side effects.
            httpx.Request("POST", target, content=b"{}")
        except (ValueError, UnicodeError, httpx.InvalidURL):
            c.invalid("target: must be an HTTP or HTTPS URL")
        filters = data.get("filters", [])
        if not isinstance(filters, list) or any(not isinstance(rule, dict) for rule in filters):
            c.invalid("filters: must be an array of filters")
        if resource["resource_type"] == "workspace" and not filters:
            c.invalid("Workspace webhooks must specify filters")
        for rule in filters:
            if any(key not in ("resource_type", "resource_subtype", "action", "fields") for key in rule):
                c.invalid("Invalid webhook filter")
            for key in ("resource_type", "resource_subtype", "action"):
                if key in rule:
                    c.string(rule[key], key, empty=False)
            if "fields" in rule:
                c.ids(rule["fields"], "fields")
                if rule.get("action") != "changed" or resource["resource_type"] == "workspace":
                    c.invalid("fields filters require action changed on a task or project webhook")
            if rule.get("action") not in (None, "added", "changed", "deleted", "removed", "undeleted"):
                c.invalid("Invalid webhook filter action")
        secret = secrets.token_hex(32)
        try:
            response = await ctx.webhooks.request(
                "POST", target, headers={"X-Hook-Secret": secret}, content=b"{}"
            )
        except httpx.HTTPError:
            c.invalid("The remote server could not complete the webhook handshake")
        if response.status_code not in (200, 204) or not hmac.compare_digest(
            response.headers.get("x-hook-secret", ""), secret
        ):
            c.invalid("The remote server did not respond with the correct X-Hook-Secret")
        hook = c.put(
            ctx,
            "webhooks",
            {
                "gid": c.new_gid(ctx),
                "resource_type": "webhook",
                "resource": c.compact(resource),
                "target": target,
                "active": False,
                "created_at": c.stamp(ctx),
                "last_success_at": None,
                "last_failure_at": None,
                "last_failure_content": None,
                "delivery_retry_count": 0,
                "filters": filters,
                "_secret": secret,
                "_token": grant.token,
            },
        )
        return JSONResponse(
            {"data": c.render(request, ctx, grant, hook), "X-Hook-Secret": secret}, status_code=201
        )

    @app.get(c.BASE + "/webhooks")
    async def listing(request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "webhooks:read")
        workspace = request.query_params.get("workspace")
        if not workspace:
            c.invalid("workspace: Missing required parameter")
        c.require(ctx, grant, "workspaces", workspace)
        resource = request.query_params.get("resource")
        rows = [
            h
            for h in ctx.store["webhooks"]
            if c.allowed(ctx, grant, h) and (not resource or h["resource"]["gid"] == resource)
        ]
        return c.page(request, ctx, grant, rows)

    @app.get(c.BASE + "/webhooks/{webhook_gid}")
    async def retrieve(webhook_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "webhooks:read")
        return {"data": c.render(request, ctx, grant, c.require(ctx, grant, "webhooks", webhook_gid))}

    @app.delete(c.BASE + "/webhooks/{webhook_gid}")
    async def delete(webhook_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "webhooks:delete")
        c.require(ctx, grant, "webhooks", webhook_gid)
        ctx.store["webhooks"].delete(webhook_gid)
        return {"data": {}}
