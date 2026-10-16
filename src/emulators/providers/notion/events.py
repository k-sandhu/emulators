"""Signed integration webhooks through the shared dispatcher."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx

from emulators.core.provider import ProviderContext
from emulators.core.webhooks import WebhookDispatcher, hmac_sha256_hex
from emulators.providers.notion.common import (
    BOT_ID,
    INTERNAL_TOKEN,
    NEW,
    OLD,
    allowed,
    fail,
    invalid,
    person_id,
    record_by_id,
    stamp,
    world_user,
)
from emulators.providers.notion.validation import url as validate_url


class NotionWebhooks(WebhookDispatcher):
    """Use Notion's attempt metadata while keeping delivery in the shared journal."""

    async def _attempt_all(
        self,
        record: dict[str, Any],
        body: bytes,
        sign: Callable[[bytes, int], dict[str, str]] | None,
    ) -> None:
        payload = json.loads(body)
        for attempt in range(1, self.max_attempts + 1):
            payload["attempt_number"] = attempt
            content = json.dumps(payload, separators=(",", ":")).encode()
            record.update(attempts=attempt, body=content.decode())
            headers = {**record["headers"], **(sign(content, attempt) if sign else {})}
            record["headers"] = headers
            try:
                response = await self.request(
                    record["method"], record["url"], headers=headers, content=content
                )
                record.update(response_status=response.status_code, response_body=response.text[:2000])
                if 200 <= response.status_code < 300:
                    record.update(status="delivered", delivered_at=stamp(self.clock.now()), error=None)
                    return
                record["error"] = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"[:500]
            if attempt < self.max_attempts and self.mode == "async":
                await asyncio.sleep(min(0.05 * 2**attempt, 2.0))
        record["status"] = "failed"


async def configure_webhook(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Register an integration webhook and send the verification token to its receiver."""
    url = payload.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        invalid("url must be an absolute HTTP(S) URL.")
    validate_url(url)
    access_token = payload.get("token", INTERNAL_TOKEN)
    if ctx.tokens.resolve(access_token) is None:
        fail(401, "unauthorized", "API token is invalid.")
    api_version = payload.get("api_version", OLD)
    if api_version not in (OLD, NEW):
        invalid("api_version must be 2022-06-28 or 2025-09-03.")
    token = ctx.ids.token("secret_")
    try:
        response = await ctx.webhooks.request(
            "POST",
            url,
            headers={"Content-Type": "application/json"},
            content=json.dumps({"verification_token": token}).encode(),
        )
    except httpx.HTTPError:
        invalid("Webhook verification request failed.")
    if not 200 <= response.status_code < 300:
        invalid("Webhook verification endpoint did not return a successful status.")
    record = {
        "id": ctx.ids.guid(),
        "url": url,
        "verification_token": token,
        "integration_id": BOT_ID,
        "_access_token": access_token,
        "_api_version": api_version,
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
        wanted = "read_comments" if event_type.startswith("comment.") else "read_content"
        if wanted not in grant.scopes:
            continue
        version = subscription["_api_version"]
        kind = event_type
        eid, etype = entity_id, entity_type
        if event_type == "database.schema_updated" and version == NEW:
            kind = "data_source.schema_updated"
            eid, etype = target["_sources"][0], "data_source"
        elif event_type == "data_source.schema_updated" and version == OLD:
            kind = "database.schema_updated"
            eid, etype = target["parent"]["database_id"], "database"
        event_target = record_by_id(ctx, eid) if entity_type != "comment" else target
        parent = (event_target or target).get("parent", {})
        parent_kind = parent.get("type", "workspace")
        parent_data = {
            "id": parent.get(parent_kind, ctx.store.meta["workspace_id"]),
            "type": {
                "page_id": "page",
                "block_id": "block",
                "database_id": "database",
                "data_source_id": "data_source",
            }.get(parent_kind, "space"),
        }
        if parent_kind == "workspace":
            parent_data["id"] = ctx.store.meta["workspace_id"]
        accessible = [{"id": BOT_ID, "type": "bot"}]
        if pid := world_user(ctx, grant.user_id):
            accessible.insert(0, {"id": person_id(pid), "type": "person"})
        payload = {
            "id": ctx.ids.guid(),
            "timestamp": stamp(ctx.clock.now()),
            "workspace_id": ctx.store.meta["workspace_id"],
            "workspace_name": ctx.world.org.name,
            "subscription_id": subscription["id"],
            "integration_id": subscription["integration_id"],
            "type": kind,
            "authors": [{"id": BOT_ID, "type": "bot"}],
            "attempt_number": 1,
            "entity": {"id": eid, "type": etype},
            "data": {"parent": parent_data, **(data or {})},
        }
        if grant.user_id is not None:
            payload["accessible_by"] = accessible
        content = json.dumps(payload, separators=(",", ":")).encode()
        await ctx.webhooks.deliver(
            subscription["url"],
            content,
            event_type=kind,
            sign=lambda raw, attempt, secret=subscription["verification_token"]: {
                "X-Notion-Signature": "sha256=" + hmac_sha256_hex(secret, raw)
            },
        )
