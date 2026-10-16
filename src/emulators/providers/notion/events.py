"""Signed integration webhooks through the shared dispatcher."""

from __future__ import annotations

import json
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.core.webhooks import hmac_sha256_hex
from emulators.providers.notion.common import (
    BOT_ID,
    INTERNAL_TOKEN,
    allowed,
    fail,
    invalid,
    record_by_id,
    stamp,
)


async def configure_webhook(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Register an integration webhook and send the verification token to its receiver."""
    url = payload.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        invalid("url must be an absolute HTTP(S) URL.")
    access_token = payload.get("token", INTERNAL_TOKEN)
    if ctx.tokens.resolve(access_token) is None:
        fail(401, "unauthorized", "API token is invalid.")
    token = ctx.ids.token("secret_")
    response = await ctx.webhooks.request(
        "POST",
        url,
        headers={"Content-Type": "application/json"},
        content=json.dumps({"verification_token": token}).encode(),
    )
    if not 200 <= response.status_code < 300:
        invalid("Webhook verification endpoint did not return a successful status.")
    record = {
        "id": ctx.ids.guid(),
        "url": url,
        "verification_token": token,
        "integration_id": BOT_ID,
        "_access_token": access_token,
    }
    ctx.store["_subscriptions"].put(record)
    return {"subscription_id": record["id"], "verification_token": token}


async def emit(
    ctx: ProviderContext,
    event_type: str,
    entity_id: str,
    entity_type: str = "page",
    data: dict[str, Any] | None = None,
) -> None:
    for subscription in ctx.store["_subscriptions"]:
        grant = ctx.tokens.resolve(subscription["_access_token"])
        target_id = (data or {}).get("page_id", entity_id) if entity_type == "comment" else entity_id
        target = record_by_id(ctx, target_id)
        if grant is None or target is None or not allowed(ctx, grant, target):
            continue
        payload = {
            "id": ctx.ids.guid(),
            "timestamp": stamp(ctx.clock.now()),
            "workspace_id": ctx.store.meta["workspace_id"],
            "subscription_id": subscription["id"],
            "integration_id": subscription["integration_id"],
            "type": event_type,
            "entity": {"id": entity_id, "type": entity_type},
            "data": data or {},
        }
        content = json.dumps(payload, separators=(",", ":")).encode()
        await ctx.webhooks.deliver(
            subscription["url"],
            content,
            headers={
                "X-Notion-Signature": "sha256=" + hmac_sha256_hex(subscription["verification_token"], content)
            },
            event_type=event_type,
        )
