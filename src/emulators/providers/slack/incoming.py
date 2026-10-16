"""Incoming webhooks: ``POST https://hooks.slack.com/services/T.../B.../<secret>``.

Slack answers these with plain text (``ok`` or an error word) and HTTP status codes rather
than the Web API's ``ok:false`` JSON, so they get their own handler.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response

from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.slack import access, messages
from emulators.providers.slack.richtext import validate_attachments, validate_blocks
from emulators.providers.slack.runtime import SlackError, load_json
from emulators.providers.slack.util import stable_id

WEBHOOK_ICONS = {
    "image_36": "https://a.slack-edge.com/80588/img/plugins/incoming-webhook/service_36.png",
    "image_48": "https://a.slack-edge.com/80588/img/plugins/incoming-webhook/service_48.png",
    "image_72": "https://a.slack-edge.com/80588/img/plugins/incoming-webhook/service_72.png",
}


def create_webhook(
    ctx: ProviderContext, channel_id: str, *, created_by: str, created: int, label: str, seed: str = ""
) -> dict[str, Any]:
    """Register an incoming webhook (and its own ``B...`` bot id) for a channel."""
    team = access.team_id(ctx)
    app = access.app_config(ctx)
    bot_id = stable_id("B", "webhook", channel_id, seed)
    secret = stable_hash("slack-webhook-secret", channel_id, seed, length=24)
    record = {
        "id": f"{team}/{bot_id}/{secret}",
        "channel_id": channel_id,
        "bot_id": bot_id,
        "app_id": app["app_id"],
        "label": label,
        "created": created,
        "created_by": created_by,
    }
    access.col(ctx, access.WEBHOOKS).put(record)
    ctx.store.collection("bots").put(
        {
            "id": bot_id,
            "app_id": app["app_id"],
            "user_id": app["bot_user_id"],
            "name": "incoming-webhook",
            "updated": created,
            "deleted": False,
            "icons": WEBHOOK_ICONS,
        }
    )
    return record


def _text(body: str, status: int) -> Response:
    return PlainTextResponse(body, status_code=status)


async def _payload(request: Request) -> dict[str, Any] | None:
    raw = (await request.body()).decode("utf-8", errors="replace")
    mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime == "application/x-www-form-urlencoded":
        raw = dict(parse_qsl(raw, keep_blank_values=True)).get("payload", "")
    try:
        parsed = load_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def handle_incoming(ctx: ProviderContext, request: Request, key: str) -> Response:
    hook = access.col(ctx, access.WEBHOOKS).get(key)
    if hook is None:
        return _text("no_service", 404)
    payload = await _payload(request)
    if payload is None:
        return _text("invalid_payload", 400)
    text = payload.get("text")
    if text is not None and not isinstance(text, str):
        return _text("invalid_payload", 400)
    blocks, attachments = payload.get("blocks"), payload.get("attachments")
    if not text and not blocks and not attachments:
        return _text("no_text", 400)
    try:
        if blocks is not None:
            validate_blocks(blocks, "hook")
        if attachments is not None:
            validate_attachments(attachments)
    except SlackError as exc:
        return _text("invalid_blocks" if "block" in exc.error else "invalid_attachments", 400)
    channel = access.channel_by_id(ctx, hook["channel_id"])
    if channel is None:
        return _text("channel_not_found", 404)
    if channel.get("is_archived"):
        return _text("channel_is_archived", 410)
    extra: dict[str, Any] = {"username": "incoming-webhook"}
    try:
        await messages.create_message(
            ctx,
            channel,
            text=text or "",
            subtype="bot_message",
            blocks=blocks,
            attachments=attachments,
            thread_ts=payload.get("thread_ts"),
            bot_id=hook["bot_id"],
            app_id=hook["app_id"],
            extra=extra,
        )
    except SlackError as exc:
        return _text(exc.error, 400)
    return _text("ok", 200)
