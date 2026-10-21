"""Webhook payload journals and signed notification pings."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request

from emulators.core.auth import Grant
from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.provider import ProviderContext

from .common import (
    base_access,
    body,
    fail,
    field_for,
    integer,
    invalid,
    new_id,
    public,
    role,
    table_for,
    text,
    timestamp,
)
from .formulas import values


def expiry(ctx: ProviderContext) -> str:
    return isoformat_z(ctx.clock.now() + timedelta(days=7), timespec="milliseconds")


def active(ctx: ProviderContext, hook: dict[str, Any]) -> bool:
    user = ctx.store.collection("users").get(hook["_owner"])
    base = ctx.store.collection("bases").get(hook["_base"])
    return bool(
        user
        and user["_active"]
        and base["_acl"].get(user["id"]) == "create"
        and parse_instant(hook["expirationTime"]) > ctx.clock.now()
    )


def visible(ctx: ProviderContext, grant: Grant, base_id: str) -> list[dict[str, Any]]:
    return [
        h
        for h in ctx.store.collection("webhooks")
        if h["_base"] == base_id
        and (personal_token(grant) or h["_client"] == grant.client_id)
        and parse_instant(h["expirationTime"]) + timedelta(days=7) > ctx.clock.now()
    ]


def personal_token(grant: Grant) -> bool:
    return grant.client_id == "airtable-pat" or grant.extra.get("auth_type") == "pat"


def hook_wire(ctx: ProviderContext, hook: dict[str, Any]) -> dict[str, Any]:
    result = public(hook)
    result["isHookEnabled"] = active(ctx, hook)
    delivery = ctx.webhooks.deliveries.get(hook.get("_last_delivery"))
    if delivery and delivery["status"] in {"delivered", "failed"}:
        result["lastNotificationResult"] = {
            "success": delivery["status"] == "delivered",
            "completionTimestamp": delivery.get("delivered_at") or timestamp(ctx),
        }
        if delivery["status"] == "delivered":
            result["lastSuccessfulNotificationTime"] = delivery["delivered_at"]
    return result


def cell_values(
    ctx: ProviderContext,
    request: Request,
    table: dict[str, Any],
    record: dict[str, Any],
    attachments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    from .records import attachment_wire

    result = values(table, record)
    for field in table["fields"]:
        value = result.get(field["id"])
        if field["type"] == "singleSelect" and value:
            result[field["id"]] = next(c for c in field["options"]["choices"] if c["name"] == value)
        elif field["type"] == "multipleRecordLinks" and value:
            result[field["id"]] = [{"id": key} for key in value]
        elif field["type"] == "multipleAttachments" and value:
            for item in value:
                if item["id"] not in attachments:
                    attachments[item["id"]] = attachment_wire(ctx, request, item["id"])
            result[field["id"]] = [attachments[item["id"]] for item in value]
    return result


async def ping(ctx: ProviderContext, hook: dict[str, Any]) -> None:
    if not hook["notificationUrl"] or not hook["areNotificationsEnabled"]:
        return
    payload = {"base": {"id": hook["_base"]}, "webhook": {"id": hook["id"]}, "timestamp": timestamp(ctx)}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "hmac-sha256=" + hmac.new(base64.b64decode(hook["_secret"]), raw, hashlib.sha256).hexdigest()
    delivery = await ctx.webhooks.deliver(
        hook["notificationUrl"],
        raw,
        headers={"X-Airtable-Content-MAC": signature},
        event_type="airtable.webhook",
        metadata={"webhook_id": hook["id"]},
    )
    hook["_last_delivery"] = delivery["id"]
    hook["lastNotificationResult"] = {
        "success": delivery["status"] == "delivered",
        "completionTimestamp": timestamp(ctx),
    }
    if delivery["status"] == "delivered":
        hook["lastSuccessfulNotificationTime"] = timestamp(ctx)


async def notify(
    ctx: ProviderContext,
    request: Request,
    table: dict[str, Any],
    grant: Grant,
    events: list[tuple[dict[str, Any] | None, dict[str, Any] | None]],
) -> None:
    if not events:
        return
    base = ctx.store.collection("bases").get(table["_base"])
    base["_transaction"] = base.get("_transaction", 0) + 1
    attachments: dict[str, dict[str, Any]] = {}
    for hook in ctx.store.collection("webhooks"):
        if hook["_base"] != table["_base"] or not active(ctx, hook):
            continue
        opts = hook["specification"]["options"]
        filters = opts["filters"]
        if filters.get("recordChangeScope", table["id"]) != table["id"] or "publicApi" not in filters.get(
            "fromSources", ["publicApi"]
        ):
            continue
        change: dict[str, Any] = {}
        for old, new in events:
            kind = "add" if old is None else "remove" if new is None else "update"
            if kind not in filters.get("changeTypes", ["add", "update", "remove"]):
                continue
            current = cell_values(ctx, request, table, new, attachments) if new else {}
            previous = cell_values(ctx, request, table, old, attachments) if old else {}
            changed = {
                key for key in current.keys() | previous.keys() if current.get(key) != previous.get(key)
            }
            if old and new and not changed:
                continue
            watch = filters.get("watchDataInFieldIds")
            if watch is not None and not changed.intersection(watch):
                continue
            if old is None:
                change.setdefault("createdRecordsById", {})[new["id"]] = {
                    "createdTime": new["createdTime"],
                    "cellValuesByFieldId": current,
                }
            elif new is None:
                change.setdefault("destroyedRecordIds", []).append(old["id"])
            else:
                entry = {"current": {"cellValuesByFieldId": {key: current.get(key) for key in changed}}}
                if opts.get("includes", {}).get("includePreviousCellValues"):
                    entry["previous"] = {"cellValuesByFieldId": {key: previous.get(key) for key in changed}}
                included = opts.get("includes", {}).get("includeCellValuesInFieldIds", [])
                included = current.keys() if included == "all" else included
                unchanged = {key: current.get(key) for key in included if key not in changed}
                if unchanged:
                    entry["unchanged"] = {"cellValuesByFieldId": unchanged}
                change.setdefault("changedRecordsById", {})[new["id"]] = entry
        if not change:
            continue
        sequence = hook["cursorForNextPayload"]
        user = ctx.store.collection("users").get(grant.user_id)
        payload = {
            "timestamp": timestamp(ctx),
            "baseTransactionNumber": base["_transaction"],
            "payloadFormat": "v0",
            "actionMetadata": {
                "source": "publicApi",
                "sourceMetadata": {
                    "user": {
                        "id": user["id"],
                        "email": user["email"],
                        "permissionLevel": role(
                            ctx, grant, ctx.store.collection("bases").get(table["_base"])
                        ),
                    }
                },
            },
            "changedTablesById": {table["id"]: change},
        }
        hook["_payloads"].append(
            {"cursor": sequence, "payload": copy.deepcopy(payload), "expires": ctx.clock.timestamp() + 604800}
        )
        hook["cursorForNextPayload"] += 1
        await ping(ctx, hook)


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.api_route("/v0/bases/{base_id}/webhooks", methods=["GET", "POST"])
    async def collection(request: Request, base_id: str) -> dict[str, Any]:
        creating = request.method == "POST"
        grant, _ = base_access(
            ctx, request, base_id, "webhook:manage", level="create" if creating else "read"
        )
        if not creating:
            return {"webhooks": [hook_wire(ctx, h) for h in visible(ctx, grant, base_id)]}
        if "data.records:read" not in grant.scopes:
            fail(403, "INVALID_PERMISSIONS", "data.records:read is required for tableData webhooks.")
        data = await body(request)
        if set(data) - {"notificationUrl", "specification"}:
            invalid("Unsupported webhook option.")
        specification = data.get("specification")
        if (
            not isinstance(specification, dict)
            or set(specification) != {"options"}
            or not isinstance(specification["options"], dict)
        ):
            invalid("specification.options is required.")
        opts = specification["options"]
        filters = opts.get("filters")
        if (
            set(opts) - {"filters", "includes"}
            or not isinstance(filters, dict)
            or filters.get("dataTypes") != ["tableData"]
        ):
            invalid("This emulator supports tableData webhook specifications only.")
        if set(filters) - {
            "dataTypes",
            "recordChangeScope",
            "changeTypes",
            "fromSources",
            "watchDataInFieldIds",
        }:
            invalid("Unsupported webhook filter.")
        for key, allowed in (
            ("changeTypes", {"add", "remove", "update"}),
            (
                "fromSources",
                {
                    "client",
                    "publicApi",
                    "formSubmission",
                    "formPageSubmission",
                    "automation",
                    "system",
                    "sync",
                    "anonymousUser",
                    "unknown",
                },
            ),
        ):
            if key in filters and (
                not isinstance(filters[key], list)
                or not filters[key]
                or any(not isinstance(v, str) or v not in allowed for v in filters[key])
            ):
                invalid(f"Invalid {key}.")
        table = None
        if "recordChangeScope" in filters:
            table = table_for(ctx, base_id, text(filters["recordChangeScope"], "recordChangeScope"))
            if table["id"] != filters["recordChangeScope"]:
                invalid("recordChangeScope must be a table ID.")
        includes = opts.get("includes", {})
        if not isinstance(includes, dict) or set(includes) - {
            "includePreviousCellValues",
            "includeCellValuesInFieldIds",
        }:
            invalid("Unsupported webhook includes.")
        if "includePreviousCellValues" in includes and not isinstance(
            includes["includePreviousCellValues"], bool
        ):
            invalid("includePreviousCellValues must be a boolean.")
        for name, value in (
            ("watchDataInFieldIds", filters.get("watchDataInFieldIds")),
            ("includeCellValuesInFieldIds", includes.get("includeCellValuesInFieldIds")),
        ):
            if value is None or (name == "includeCellValuesInFieldIds" and value == "all"):
                continue
            if not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value):
                invalid("Expected an array of field IDs.")
            for key in value:
                if table:
                    if field_for(table, key)["id"] != key:
                        invalid("Expected field IDs.")
                elif not any(
                    f["id"] == key
                    for t in ctx.store.collection("tables")
                    if t["_base"] == base_id
                    for f in t["fields"]
                ):
                    invalid("Unknown field ID.")
        url = data.get("notificationUrl")
        if url is not None:
            text(url, "notificationUrl", maximum=8192, empty=False)
            try:
                parsed = urlparse(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.fragment or parsed.username:
                    raise ValueError
                _ = parsed.port
            except ValueError:
                invalid("notificationUrl must be a valid HTTPS URL.")
        all_hooks = ctx.store.collection("webhooks").where(_base=base_id)
        if len(all_hooks) >= 10 or (
            not personal_token(grant) and sum(h["_client"] == grant.client_id for h in all_hooks) >= 2
        ):
            invalid("Webhook limit exceeded.", "TOO_MANY_WEBHOOKS")
        secret = base64.b64encode(secrets.token_bytes(32)).decode()
        hook = {
            "id": new_id(ctx, "ach"),
            "expirationTime": expiry(ctx),
            "specification": copy.deepcopy(specification),
            "notificationUrl": url,
            "areNotificationsEnabled": url is not None,
            "isHookEnabled": True,
            "cursorForNextPayload": 1,
            "lastSuccessfulNotificationTime": None,
            "lastNotificationResult": None,
            "_base": base_id,
            "_owner": grant.user_id,
            "_client": grant.client_id,
            "_secret": secret,
            "_payloads": [],
        }
        ctx.store.collection("webhooks").put(hook)
        return {"id": hook["id"], "expirationTime": hook["expirationTime"], "macSecretBase64": secret}

    def get(request: Request, base_id: str, hook_id: str, *, creator: bool = False) -> dict[str, Any]:
        grant, _ = base_access(ctx, request, base_id, "webhook:manage", level="create" if creator else "read")
        hook = next((h for h in visible(ctx, grant, base_id) if h["id"] == hook_id), None)
        if hook is None:
            fail(404, "NOT_FOUND", "Webhook not found")
        return hook

    @app.delete("/v0/bases/{base_id}/webhooks/{hook_id}")
    async def delete(request: Request, base_id: str, hook_id: str) -> dict[str, Any]:
        hook = get(request, base_id, hook_id, creator=True)
        ctx.store.collection("webhooks").delete(hook["id"])
        return {}

    @app.post("/v0/bases/{base_id}/webhooks/{hook_id}/refresh")
    async def refresh(request: Request, base_id: str, hook_id: str) -> dict[str, Any]:
        hook = get(request, base_id, hook_id, creator=True)
        if not active(ctx, hook):
            invalid("An expired webhook cannot be refreshed.")
        hook["expirationTime"] = expiry(ctx)
        return {"expirationTime": hook["expirationTime"]}

    @app.post("/v0/bases/{base_id}/webhooks/{hook_id}/enableNotifications")
    async def toggle(request: Request, base_id: str, hook_id: str) -> dict[str, Any]:
        hook = get(request, base_id, hook_id, creator=True)
        data = await body(request)
        if set(data) != {"enable"} or not isinstance(data["enable"], bool):
            invalid("enable must be a boolean.")
        hook["areNotificationsEnabled"] = data["enable"]
        return {}

    @app.get("/v0/bases/{base_id}/webhooks/{hook_id}/payloads")
    async def payloads(request: Request, base_id: str, hook_id: str) -> dict[str, Any]:
        hook = get(request, base_id, hook_id)
        base_access(ctx, request, base_id, "data.records:read")
        if set(request.query_params) - {"cursor", "limit"} or len(request.query_params) != len(
            request.query_params.multi_items()
        ):
            invalid("Unknown webhook payload parameter.")
        cursor = integer(request.query_params.get("cursor", "1"), "cursor", 1, hook["cursorForNextPayload"])
        limit = integer(request.query_params.get("limit", "50"), "limit", 1, 50)
        rows = [
            p for p in hook["_payloads"] if p["cursor"] >= cursor and p["expires"] > ctx.clock.timestamp()
        ]
        page = rows[:limit]
        if active(ctx, hook):
            hook["expirationTime"] = expiry(ctx)
        return {
            "cursor": page[-1]["cursor"] + 1 if page else cursor,
            "mightHaveMore": len(rows) > limit,
            "payloads": [p["payload"] for p in page],
        }


async def simulate_change(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Replay a notification for an existing hook without inventing a vendor mutation."""
    hook = ctx.store.collection("webhooks").get(payload.get("webhook_id"))
    if hook is None or not active(ctx, hook):
        invalid("An active webhook_id is required.")
    await ping(ctx, hook)
    return {"webhook_id": hook["id"], "cursorForNextPayload": hook["cursorForNextPayload"]}
