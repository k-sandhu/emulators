"""Webhooks v2: create, list, update and delete subscriptions on files and folders."""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, Request

from emulators.core.provider import ProviderContext
from emulators.providers.box import access, db, errors, events
from emulators.providers.box.access import Principal
from emulators.providers.box.routing import Api, created, no_content
from emulators.providers.box.shapes import Shaper
from emulators.providers.box.util import (
    box_ts,
    decode_marker,
    encode_marker,
    json_body,
    next_id,
    now_iso,
    query_int,
)

Record = dict[str, Any]


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)
    webhooks = db.Live(ctx, db.WEBHOOKS)

    def body_of(shaper: Shaper, webhook: Record, *, mini: bool = False) -> Record:
        out: Record = {"id": webhook["id"], "type": "webhook", "target": dict(webhook["target"])}
        if mini:
            return out
        out["created_by"] = shaper.user_mini(webhook["created_by"])
        out["created_at"] = box_ts(webhook["created_at"])
        out["address"] = webhook["address"]
        out["triggers"] = list(webhook["triggers"])
        return out

    def validate(body: dict[str, Any], *, partial: bool) -> None:
        if not partial or "address" in body:
            address = body.get("address")
            if not address:
                raise errors.missing_parameter("address")
            if not re.match(r"^https?://[^\s/]+", str(address)):
                raise errors.invalid_parameter("address", "'address' must be an http(s) URL")
        if not partial or "triggers" in body:
            triggers = body.get("triggers")
            if not isinstance(triggers, list) or not triggers:
                raise errors.missing_parameter("triggers")
            unknown = [t for t in triggers if t not in events.ALL_TRIGGERS]
            if unknown:
                raise errors.invalid_parameter("triggers", f"Invalid trigger '{unknown[0]}'")

    def visible(p: Principal, webhook: Record) -> bool:
        return webhook["created_by"] == p.uid or p.is_privileged

    def webhook_or_404(p: Principal, webhook_id: str) -> Record:
        webhook = webhooks.get(webhook_id)
        if webhook is None or not visible(p, webhook):
            raise errors.not_found()
        return webhook

    @api.post("/webhooks")
    async def create_webhook(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        target = body.get("target")
        if (
            not isinstance(target, dict)
            or target.get("type") not in ("file", "folder")
            or not target.get("id")
        ):
            raise errors.missing_parameter("target")
        validate(body, partial=False)
        record = p.get_item(target["type"], str(target["id"]), access.RENAME)
        target_id = db.ROOT_WIRE_ID if db.is_root(record) else record["id"]
        if any(
            w["target"] == {"id": target_id, "type": record["type"]} and w["address"] == body["address"] and w["created_by"] == p.uid
            for w in webhooks
        ):  # fmt: skip
            raise errors.box_error(409, "conflict", "A webhook with this target and address already exists")
        webhook = {
            "id": next_id(ctx, "webhook", webhooks, start=db.SMALL_ID_START * 10),
            "type": "webhook",
            "target": {"id": target_id, "type": record["type"]},
            "created_by": p.uid,
            "created_at": now_iso(ctx),
            "address": body["address"],
            "triggers": list(dict.fromkeys(body["triggers"])),
        }
        webhooks.put(webhook)
        return created(body_of(Shaper(p, request), webhook))

    @api.get("/webhooks")
    async def list_webhooks(request: Request) -> Any:
        p = api.principal(request)
        rows = [w for w in webhooks if visible(p, w)]
        limit = query_int(request, "limit", 100, minimum=1, maximum=200)
        offset = decode_marker(request.query_params.get("marker"))
        shaper = Shaper(p, request)
        window = rows[offset : offset + limit]
        more = offset + limit < len(rows)
        return {
            "limit": limit,
            "next_marker": encode_marker(offset + limit) if more else None,
            "entries": [body_of(shaper, w, mini=True) for w in window],
        }

    @api.get("/webhooks/{webhook_id}")
    async def get_webhook(request: Request, webhook_id: str) -> Any:
        p = api.principal(request)
        return body_of(Shaper(p, request), webhook_or_404(p, webhook_id))

    @api.put("/webhooks/{webhook_id}")
    async def update_webhook(request: Request, webhook_id: str) -> Any:
        p = api.principal(request)
        webhook = webhook_or_404(p, webhook_id)
        body = await json_body(request)
        validate(body, partial=True)
        if isinstance(body.get("target"), dict):
            target = body["target"]
            record = p.get_item(target.get("type", ""), str(target.get("id", "")), access.RENAME)
            webhook["target"] = {
                "id": db.ROOT_WIRE_ID if db.is_root(record) else record["id"],
                "type": record["type"],
            }
        if "address" in body:
            webhook["address"] = body["address"]
        if "triggers" in body:
            webhook["triggers"] = list(dict.fromkeys(body["triggers"]))
        return body_of(Shaper(p, request), webhook)

    @api.delete("/webhooks/{webhook_id}")
    async def delete_webhook(request: Request, webhook_id: str) -> Any:
        p = api.principal(request)
        webhook = webhook_or_404(p, webhook_id)
        webhooks.delete(webhook["id"])
        return no_content()
