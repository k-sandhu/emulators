"""Trello webhook ownership, HEAD verification and signed action delivery."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable

import httpx
from fastapi import Request

from emulators.core.auth import Grant
from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.core.webhooks import WebhookDispatcher, hmac_sha1_b64

from .common import Record, can_view, clean, fail, flag, get_board, http_url, lookup, new_id, text
from .shapes import action_shape, board_shape, card_shape, organization_shape


class TrelloWebhooks(WebhookDispatcher):
    async def _attempt_all(
        self, record: Record, body: bytes, sign: Callable[[bytes, int], dict[str, str]] | None
    ) -> None:
        for attempt in range(1, self.max_attempts + 1):
            record["attempts"] = attempt
            headers = {**record["headers"], **(sign(body, attempt) if sign else {})}
            record["headers"] = headers
            try:
                async with self._client() as client:
                    response = await client.request(
                        record["method"], record["url"], content=body, headers=headers
                    )
                record.update(response_status=response.status_code, response_body=response.text[:2000])
                if 200 <= response.status_code < 300:
                    record.update(status="delivered", delivered_at=isoformat_z(self.clock.now()), error=None)
                    break
                record["error"] = f"HTTP {response.status_code}"
                if response.status_code == 410:
                    self.store["webhooks"].delete(record["metadata"]["webhook_id"])
                    break
            except httpx.HTTPError as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"[:500]
            if attempt < self.max_attempts and self.mode == "async":
                await asyncio.sleep(min(0.25 * 2**attempt, 2.0))
        if record["status"] != "delivered":
            record["status"] = "failed"
        hook = self.store["webhooks"].get(record["metadata"]["webhook_id"])
        if hook:
            if record["status"] == "delivered":
                hook.update(consecutiveFailures=0, firstConsecutiveFailDate=None)
            else:
                hook["consecutiveFailures"] += 1
                hook["firstConsecutiveFailDate"] = hook["firstConsecutiveFailDate"] or record["created_at"]


def model(ctx: ProviderContext, request: Request, value: str, grant: Grant | None) -> Record:
    if ctx.store["boards"].get(value):
        return board_shape(ctx, request, get_board(ctx, value, grant), grant)
    if ctx.store["cards"].get(value):
        card = lookup(ctx, "cards", value)
        get_board(ctx, card["idBoard"], grant)
        return card_shape(ctx, request, card, grant)
    if ctx.store["lists"].get(value):
        record = lookup(ctx, "lists", value)
        get_board(ctx, record["idBoard"], grant)
        return clean(record)
    if ctx.store["organizations"].get(value):
        org = lookup(ctx, "organizations", value)
        if not grant or grant.user_id not in org["_members"]:
            fail(401, "unauthorized permission requested")
        return organization_shape(ctx, request, org, grant=grant)
    if grant and value == grant.user_id:
        return clean(ctx.store["members"].get(value))
    fail(400, "invalid idModel")


async def create_webhook(ctx: ProviderContext, request: Request, params: Record, grant: Grant) -> Record:
    target = http_url(params.get("callbackURL"), "callbackURL")
    model_id = text(params.get("idModel"), "idModel", 24)
    model(ctx, request, model_id, grant)
    description = text(params.get("description", ""), "description", empty=True)
    active = flag(params.get("active", True), "active")
    if ctx.store["webhooks"].first(_token=grant.token, callbackURL=target, idModel=model_id):
        fail(400, "A webhook with that callback, model, and token already exists")
    try:
        response = await ctx.webhooks.request("HEAD", target)
    except httpx.HTTPError:
        fail(400, "URL did not return 200 status code")
    if response.status_code != 200:
        fail(400, f"URL did not return 200 status code, got {response.status_code}")
    record = {
        "id": new_id(ctx, "webhook"),
        "description": description,
        "idModel": model_id,
        "callbackURL": target,
        "active": active,
        "consecutiveFailures": 0,
        "firstConsecutiveFailDate": None,
        "_token": grant.token,
    }
    ctx.store["webhooks"].put(record)
    return clean(record)


async def emit(
    ctx: ProviderContext,
    request: Request,
    grant: Grant,
    kind: str,
    board: Record,
    card: Record | None = None,
    *,
    data: Record | None = None,
) -> Record:
    payload: Record = {"board": {"id": board["id"], "name": board["name"], "shortLink": board["shortLink"]}}
    if card:
        payload["card"] = {
            "id": card["id"],
            "name": card["name"],
            "idShort": card["idShort"],
            "shortLink": card["shortLink"],
        }
    payload.update(copy.deepcopy(data or {}))
    action = {
        "id": new_id(ctx, "action"),
        "idMemberCreator": grant.user_id,
        "type": kind,
        "date": isoformat_z(ctx.clock.now(), timespec="milliseconds"),
        "data": payload,
        "_board": board["id"],
        "_card": card["id"] if card else None,
    }
    ctx.store["actions"].put(action)
    board["dateLastActivity"] = action["date"]
    if card:
        card["dateLastActivity"] = action["date"]
    targets = {board["id"], board["idOrganization"], grant.user_id}
    if card:
        targets.update((card["id"], card["idList"]))
    for hook in ctx.store["webhooks"]:
        owner = ctx.tokens.resolve(hook["_token"])
        if not owner:
            ctx.store["webhooks"].delete(hook["id"])
            continue
        if not hook["active"] or hook["idModel"] not in targets or not can_view(ctx, board, owner):
            continue
        body = json.dumps(
            {
                "action": action_shape(ctx, request, action, owner),
                "model": model(ctx, request, hook["idModel"], owner),
                "webhook": clean(hook),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        signature = hmac_sha1_b64(ctx.store.meta["secret"], body + hook["callbackURL"].encode())
        await ctx.webhooks.deliver(
            hook["callbackURL"],
            body,
            headers={"X-Trello-Webhook": signature},
            event_type=kind,
            metadata={"webhook_id": hook["id"]},
        )
    return action
