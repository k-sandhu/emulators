"""Interactivity: slash commands, block actions, modals (``views.*``) and ``response_url``.

Slack posts these to the app's Interactivity / Slash Command request URLs as signed
``application/x-www-form-urlencoded`` bodies (``payload=<json>`` for interactions). The
control-plane actions here play the user; ``response_url`` and ``views.*`` are the app's way
to answer, so they are real endpoints of the emulator.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response

from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.providers.slack import access, messages
from emulators.providers.slack.events import USER_AGENT, signature
from emulators.providers.slack.records import new_id
from emulators.providers.slack.richtext import validate_attachments, validate_blocks
from emulators.providers.slack.runtime import Call, SlackError, api
from emulators.providers.slack.shapes import message_json
from emulators.providers.slack.util import WORKSPACE_DOMAIN

VIEWS = "views"
TRIGGERS = "trigger_ids"
RESPONSE_URL_TTL = timedelta(minutes=30)
RESPONSE_URL_USES = 5
TRIGGER_TTL = timedelta(seconds=30)
TRIGGER_RE = re.compile(r"^\d+\.\d+\.[0-9a-f]+$")
VIEW_TYPES = ("modal", "home")


def _fail(status: int, error: str, **extra: Any) -> EmulatorError:
    return EmulatorError(status, {"ok": False, "error": error, **extra})


# -- trigger ids and views.* -------------------------------------------------------------------


def new_trigger(ctx: ProviderContext, user_id: str) -> str:
    trigger = f"{ctx.ids.digits(12)}.{ctx.ids.digits(12)}.{ctx.ids.hex(24)}"
    ctx.store.collection(TRIGGERS).put(
        {"id": trigger, "user": user_id, "created": isoformat_z(ctx.clock.now()), "used": False}
    )
    return trigger


def _consume_trigger(call: Call) -> None:
    ctx = call.ctx
    trigger = call.params.need("trigger_id")
    if not TRIGGER_RE.match(trigger):
        raise SlackError("invalid_trigger_id")
    record = ctx.store.collection(TRIGGERS).get(trigger)
    if record is None:
        raise SlackError("invalid_trigger_id")
    if record["used"]:
        raise SlackError("exchanged_trigger_id")
    created = parse_instant(record["created"])
    if ctx.clock.now() - created > TRIGGER_TTL:
        raise SlackError("expired_trigger_id")
    record["used"] = True


def _view_input(call: Call) -> dict[str, Any]:
    raw = call.params.obj("view", "invalid_arguments")
    if not isinstance(raw, dict):
        raise SlackError("invalid_arguments")
    kind = raw.get("type")
    if kind not in VIEW_TYPES:
        raise SlackError("invalid_arguments", response_metadata={"messages": ["[ERROR] invalid view type"]})
    title = raw.get("title")
    if kind == "modal" and not (isinstance(title, dict) and title.get("text")):
        raise SlackError("invalid_arguments", response_metadata={"messages": ["[ERROR] missing title"]})
    if isinstance(title, dict) and len(str(title.get("text", ""))) > 24:
        raise SlackError("invalid_arguments", response_metadata={"messages": ["[ERROR] title too long"]})
    blocks = validate_blocks(raw.get("blocks") or [], "view")
    if kind == "modal" and not blocks:
        raise SlackError("invalid_arguments", response_metadata={"messages": ["[ERROR] missing blocks"]})
    return {**raw, "blocks": blocks}


def _new_hash(ctx: ProviderContext) -> str:
    return f"{int(ctx.clock.now().timestamp())}.{ctx.ids.digits(7)}"


def _store_view(
    call: Call, spec: dict[str, Any], *, previous: str | None, root: str | None, user: str | None
) -> dict[str, Any]:
    ctx = call.ctx
    app = access.app_config(ctx)
    view_id = new_id(ctx, "V", VIEWS)
    record = {
        "id": view_id,
        "team_id": access.team_id(ctx),
        "type": spec["type"],
        "blocks": spec["blocks"],
        "private_metadata": spec.get("private_metadata", ""),
        "callback_id": spec.get("callback_id", ""),
        "state": {"values": {}},
        "hash": _new_hash(ctx),
        "title": spec.get("title") or {"type": "plain_text", "text": "", "emoji": True},
        "clear_on_close": bool(spec.get("clear_on_close", False)),
        "notify_on_close": bool(spec.get("notify_on_close", False)),
        "close": spec.get("close"),
        "submit": spec.get("submit"),
        "previous_view_id": previous,
        "root_view_id": root or view_id,
        "app_id": app["app_id"],
        "external_id": spec.get("external_id", ""),
        "app_installed_team_id": access.team_id(ctx),
        "bot_id": app["bot_id"],
        "_user": user,
    }
    return ctx.store.collection(VIEWS).put(record)


def _view_out(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if not k.startswith("_")}


@api("views.open")
async def views_open(call: Call) -> dict[str, Any]:
    spec = _view_input(call)
    if spec["type"] != "modal":
        raise SlackError("invalid_arguments")
    trigger = call.ctx.store.collection(TRIGGERS).get(call.params.str("trigger_id"))
    _consume_trigger(call)
    return {
        "view": _view_out(_store_view(call, spec, previous=None, root=None, user=(trigger or {}).get("user")))
    }


@api("views.push")
async def views_push(call: Call) -> dict[str, Any]:
    spec = _view_input(call)
    trigger = call.ctx.store.collection(TRIGGERS).get(call.params.str("trigger_id"))
    _consume_trigger(call)
    user = (trigger or {}).get("user")
    stack = [v for v in call.ctx.store.collection(VIEWS) if v["type"] == "modal" and v.get("_user") == user]
    if not stack:
        raise SlackError("not_found")
    top = stack[-1]
    if len([v for v in stack if v["root_view_id"] == top["root_view_id"]]) >= 3:
        raise SlackError("push_limit_reached")
    return {
        "view": _view_out(_store_view(call, spec, previous=top["id"], root=top["root_view_id"], user=user))
    }


def _find_view(call: Call) -> dict[str, Any]:
    p = call.params
    views = call.ctx.store.collection(VIEWS)
    record = views.get(p.str("view_id")) if p.str("view_id") else None
    if record is None and p.str("external_id"):
        record = next((v for v in views if v["external_id"] == p.str("external_id")), None)
    if record is None:
        raise SlackError("not_found")
    return record


@api("views.update")
async def views_update(call: Call) -> dict[str, Any]:
    record = _find_view(call)
    spec = _view_input(call)
    if call.params.str("hash") and call.params.str("hash") != record["hash"]:
        raise SlackError("hash_conflict")
    for key in (
        "blocks",
        "title",
        "close",
        "submit",
        "private_metadata",
        "callback_id",
        "clear_on_close",
        "notify_on_close",
    ):
        if key in spec:
            record[key] = spec[key]
    record["hash"] = _new_hash(call.ctx)
    return {"view": _view_out(record)}


@api("views.publish")
async def views_publish(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    user = access.user_by_id(ctx, call.params.need("user_id"))
    if user is None:
        raise SlackError("user_not_found")
    spec = _view_input(call)
    if spec["type"] != "home":
        raise SlackError("invalid_arguments")
    views = ctx.store.collection(VIEWS)
    existing = next((v for v in views if v["type"] == "home" and v.get("_user") == user["id"]), None)
    if existing is not None:
        if call.params.str("hash") and call.params.str("hash") != existing["hash"]:
            raise SlackError("hash_conflict")
        views.delete(existing["id"])
    return {"view": _view_out(_store_view(call, spec, previous=None, root=None, user=user["id"]))}


# -- response_url ------------------------------------------------------------------------------


def new_response_url(
    ctx: ProviderContext, kind: str, *, channel_id: str, user_id: str, message_ts: str | None = None
) -> tuple[str, str]:
    """``(path, key)`` of a fresh ``hooks.slack.com`` response URL (valid 30 minutes, 5 uses)."""
    team = access.team_id(ctx)
    key = f"{team}/{ctx.ids.digits(13)}/{ctx.ids.base62(24)}"
    ctx.store.collection(access.RESPONSE_URLS).put(
        {
            "id": key,
            "kind": kind,
            "channel_id": channel_id,
            "user_id": user_id,
            "message_ts": message_ts,
            "created": isoformat_z(ctx.clock.now()),
            "uses": 0,
        }
    )
    return f"/{kind}s/{key}", key


async def handle_response_url(ctx: ProviderContext, request: Request, key: str) -> Response:
    record = ctx.store.collection(access.RESPONSE_URLS).get(key)
    if record is None:
        return PlainTextResponse("no_service", status_code=404)
    created = parse_instant(record["created"])
    if record["uses"] >= RESPONSE_URL_USES or ctx.clock.now() - created > RESPONSE_URL_TTL:
        return PlainTextResponse("used_url", status_code=400)
    try:
        payload = json.loads((await request.body()) or b"{}")
    except ValueError:
        return PlainTextResponse("invalid_payload", status_code=400)
    if not isinstance(payload, dict):
        return PlainTextResponse("invalid_payload", status_code=400)
    channel = access.channel_by_id(ctx, record["channel_id"])
    if channel is None:
        return PlainTextResponse("channel_not_found", status_code=404)
    original = (
        access.get_message(ctx, channel["id"], record.get("message_ts")) if record.get("message_ts") else None
    )
    app = access.app_config(ctx)
    text, blocks, attachments = payload.get("text"), payload.get("blocks"), payload.get("attachments")
    if not payload.get("delete_original") and not (text or blocks or attachments):
        return PlainTextResponse("no_text", status_code=400)
    try:
        if payload.get("delete_original"):
            if original is not None:
                await messages.delete_message(ctx, channel, original)
        elif payload.get("replace_original") and original is not None:
            await messages.update_message(
                ctx,
                channel,
                original,
                editor=original.get("user") or app["bot_user_id"],
                text=text,
                blocks=blocks,
                attachments=attachments,
            )
        elif payload.get("response_type") == "in_channel":
            await messages.create_message(
                ctx,
                channel,
                user=app["bot_user_id"],
                text=text or "",
                blocks=blocks,
                attachments=attachments,
                bot_id=app["bot_id"],
                app_id=app["app_id"],
                thread_ts=payload.get("thread_ts"),
            )
        else:
            if blocks is not None:
                validate_blocks(blocks, "response")
            if attachments is not None:
                validate_attachments(attachments)
            access.col(ctx, access.EPHEMERAL).put(
                {
                    "id": ctx.ids.hex(12),
                    "channel": channel["id"],
                    "user": record["user_id"],
                    "text": text or "",
                    "blocks": blocks,
                    "attachments": attachments,
                    "thread_ts": payload.get("thread_ts"),
                    "posted_by": app["bot_user_id"],
                }
            )
    except SlackError as exc:
        return PlainTextResponse(exc.error, status_code=400)
    record["uses"] += 1
    return PlainTextResponse("ok")


# -- user simulations (control-plane actions) --------------------------------------------------


def _target_url(ctx: ProviderContext, payload: dict[str, Any], default: str | None = None) -> str:
    url = payload.get("request_url") or default or access.app_config(ctx).get("interactivity_url")
    if not url:
        raise _fail(
            400, "invalid_arguments", detail="request_url is required (or configure_app interactivity_url)"
        )
    return str(url)


async def _post_signed(
    ctx: ProviderContext, url: str, body: bytes, content_type: str, event_type: str
) -> dict[str, Any]:
    config = access.app_config(ctx)
    stamp = int(ctx.clock.now().timestamp())
    headers = {
        "Content-Type": content_type,
        "User-Agent": USER_AGENT,
        "X-Slack-Signature": signature(config["signing_secret"], stamp, body),
        "X-Slack-Request-Timestamp": str(stamp),
    }
    record = {
        "id": ctx.ids.prefixed("whd", 20),
        "provider": ctx.name,
        "url": url,
        "method": "POST",
        "event_type": event_type,
        "headers": headers,
        "body": body.decode("utf-8", errors="replace"),
        "status": "pending",
        "attempts": 1,
        "response_status": None,
        "response_body": None,
        "error": None,
        "created_at": isoformat_z(ctx.clock.now()),
        "delivered_at": None,
        "metadata": {},
    }
    ctx.webhooks.deliveries.put(record)
    try:
        response = await ctx.webhooks.request("POST", url, headers=headers, content=body)
    except Exception as exc:
        record["status"], record["error"] = "failed", f"{type(exc).__name__}: {exc}"[:500]
        raise _fail(502, "request_url_unreachable", detail=record["error"]) from None
    record["response_status"], record["response_body"] = response.status_code, response.text[:2000]
    record["status"] = "delivered" if 200 <= response.status_code < 300 else "failed"
    record["delivered_at"] = isoformat_z(ctx.clock.now())
    parsed: Any = None
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    return {
        "status": record["status"],
        "response_status": response.status_code,
        "response_body": response.text,
        "response_json": parsed,
    }


def _user_stub(user: dict[str, Any]) -> dict[str, Any]:
    return {"id": user["id"], "username": user["name"], "name": user["name"], "team_id": user["team_id"]}


def _team_stub(ctx: ProviderContext) -> dict[str, Any]:
    return {"id": access.team_id(ctx), "domain": WORKSPACE_DOMAIN}


def _resolve(ctx: ProviderContext, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    user = access.resolve_user_ref(ctx, str(payload.get("user") or ""))
    if user is None:
        raise _fail(404, "user_not_found", user=payload.get("user"))
    ref = str(payload.get("channel") or "")
    channel = access.channel_by_id(ctx, ref) or access.channel_by_name(ctx, ref)
    if channel is None:
        raise _fail(404, "channel_not_found", channel=ref)
    return user, channel


async def apply_command_response(
    ctx: ProviderContext, outcome: dict[str, Any], channel: dict[str, Any], user: dict[str, Any]
) -> str | None:
    """An immediate slash-command reply: ephemeral by default, in-channel as the app when asked."""
    if outcome["response_status"] != 200 or not outcome["response_body"].strip():
        return None
    reply = (
        outcome["response_json"]
        if isinstance(outcome["response_json"], dict)
        else {"text": outcome["response_body"]}
    )
    app = access.app_config(ctx)
    if reply.get("response_type") == "in_channel":
        rec = await messages.create_message(
            ctx,
            channel,
            user=app["bot_user_id"],
            text=reply.get("text", ""),
            blocks=reply.get("blocks"),
            attachments=reply.get("attachments"),
            bot_id=app["bot_id"],
            app_id=app["app_id"],
        )
        return "in_channel:" + rec["ts"]
    access.col(ctx, access.EPHEMERAL).put(
        {
            "id": ctx.ids.hex(12),
            "channel": channel["id"],
            "user": user["id"],
            "text": reply.get("text", ""),
            "blocks": reply.get("blocks"),
            "attachments": reply.get("attachments"),
            "thread_ts": None,
            "posted_by": app["bot_user_id"],
        }
    )
    return "ephemeral"


async def slash_command(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A user runs a slash command: ``{user, channel, command, text?, request_url}``.

    Posts Slack's signed form body to ``request_url`` and applies the immediate reply
    (plain text or JSON ``response_type`` ephemeral/in_channel) to the conversation.
    """
    user, channel = _resolve(ctx, payload)
    command = str(payload.get("command") or "")
    if not command.startswith("/"):
        raise _fail(400, "invalid_arguments", detail="command must start with '/'")
    url = _target_url(ctx, payload)
    config = access.app_config(ctx)
    path, _ = new_response_url(ctx, "command", channel_id=channel["id"], user_id=user["id"])
    trigger = new_trigger(ctx, user["id"])
    form = {
        "token": config["verification_token"],
        "team_id": access.team_id(ctx),
        "team_domain": WORKSPACE_DOMAIN,
        "channel_id": channel["id"],
        "channel_name": "directmessage" if channel.get("is_im") else channel.get("name", ""),
        "user_id": user["id"],
        "user_name": user["name"],
        "command": command,
        "text": str(payload.get("text") or ""),
        "api_app_id": config["app_id"],
        "is_enterprise_install": "false",
        "response_url": f"https://hooks.slack.com{path}",
        "trigger_id": trigger,
    }
    outcome = await _post_signed(
        ctx, url, urlencode(form).encode(), "application/x-www-form-urlencoded", "slash_command"
    )
    applied = await apply_command_response(ctx, outcome, channel, user)
    return {
        "ok": True,
        "trigger_id": trigger,
        "response_url": form["response_url"],
        "applied": applied,
        **outcome,
    }


async def interact(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A user clicks a Block Kit element: ``{user, channel, message_ts?, actions: [...], request_url}``.

    Sends a signed ``block_actions`` payload (``payload=<json>``) to the interactivity URL.
    """
    user, channel = _resolve(ctx, payload)
    config = access.app_config(ctx)
    message_ts = payload.get("message_ts")
    message = access.get_message(ctx, channel["id"], message_ts) if message_ts else None
    path, _ = new_response_url(
        ctx, "action", channel_id=channel["id"], user_id=user["id"], message_ts=message_ts
    )
    actions = []
    for item in payload.get("actions") or []:
        entry = {
            "type": "button",
            "block_id": "b" + ctx.ids.hex(3),
            "action_ts": f"{ctx.clock.timestamp()}.{ctx.ids.digits(6)}",
            **item,
        }
        entry.setdefault("action_id", "action")
        actions.append(entry)
    if not actions:
        raise _fail(400, "invalid_arguments", detail="actions is required")
    body: dict[str, Any] = {
        "type": payload.get("type") or "block_actions",
        "user": _user_stub(user),
        "api_app_id": config["app_id"],
        "token": config["verification_token"],
        "container": {
            "type": "message",
            "message_ts": message_ts,
            "channel_id": channel["id"],
            "is_ephemeral": False,
        },
        "trigger_id": new_trigger(ctx, user["id"]),
        "team": _team_stub(ctx),
        "enterprise": None,
        "is_enterprise_install": False,
        "channel": {"id": channel["id"], "name": channel.get("name", "directmessage")},
        "state": {"values": payload.get("values") or {}},
        "response_url": f"https://hooks.slack.com{path}",
        "actions": actions,
    }
    if message is not None:
        body["message"] = message_json(ctx, None, None, message)
    form = urlencode({"payload": json.dumps(body, separators=(",", ":"))}).encode()
    outcome = await _post_signed(
        ctx, _target_url(ctx, payload), form, "application/x-www-form-urlencoded", "block_actions"
    )
    return {"ok": True, "trigger_id": body["trigger_id"], "response_url": body["response_url"], **outcome}


async def submit_view(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A user submits a modal: ``{view_id|callback_id, user, values?, request_url}`` (``view_submission``)."""
    return await _view_event(ctx, payload, "view_submission")


async def close_view(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A user closes a modal: ``{view_id|callback_id, user, request_url}`` (``view_closed`` when requested)."""
    return await _view_event(ctx, payload, "view_closed")


async def _view_event(ctx: ProviderContext, payload: dict[str, Any], kind: str) -> dict[str, Any]:
    views = ctx.store.collection(VIEWS)
    record = views.get(payload.get("view_id")) if payload.get("view_id") else None
    if record is None and payload.get("callback_id"):
        record = next((v for v in views if v["callback_id"] == payload["callback_id"]), None)
    if record is None:
        raise _fail(404, "view_not_found")
    user = access.resolve_user_ref(ctx, str(payload.get("user") or record.get("_user") or ""))
    if user is None:
        raise _fail(404, "user_not_found")
    if kind == "view_closed" and not record["notify_on_close"]:
        return {"ok": True, "delivered": False, "reason": "notify_on_close is false"}
    config = access.app_config(ctx)
    if payload.get("values"):
        record["state"] = {"values": payload["values"]}
    body = {
        "type": kind,
        "team": _team_stub(ctx),
        "user": _user_stub(user),
        "api_app_id": config["app_id"],
        "token": config["verification_token"],
        "trigger_id": new_trigger(ctx, user["id"]),
        "view": _view_out(record),
        "response_urls": [],
        "is_enterprise_install": False,
        "enterprise": None,
    }
    form = urlencode({"payload": json.dumps(body, separators=(",", ":"))}).encode()
    outcome = await _post_signed(
        ctx, _target_url(ctx, payload), form, "application/x-www-form-urlencoded", kind
    )
    return {"ok": True, "delivered": True, **outcome}


async def configure_interactivity(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Set the default Interactivity request URL: ``{interactivity_url}``."""
    access.app_config(ctx)["interactivity_url"] = payload.get("interactivity_url")
    return {"ok": True, "interactivity_url": payload.get("interactivity_url")}


async def trigger(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Mint a fresh ``trigger_id`` for a user (to call ``views.open`` without a user simulation)."""
    user = access.resolve_user_ref(ctx, str(payload.get("user") or ""))
    if user is None:
        raise _fail(404, "user_not_found")
    return {"ok": True, "trigger_id": new_trigger(ctx, user["id"])}


ACTIONS = {
    "slash_command": slash_command,
    "interact": interact,
    "submit_view": submit_view,
    "close_view": close_view,
    "configure_interactivity": configure_interactivity,
    "trigger_id": trigger,
}
