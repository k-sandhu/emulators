"""chat.* Web API methods."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from emulators.providers.slack import access, messages
from emulators.providers.slack.records import new_id
from emulators.providers.slack.richtext import validate_attachments, validate_blocks
from emulators.providers.slack.runtime import Call, SlackError, api
from emulators.providers.slack.shapes import message_json, permalink_for
from emulators.providers.slack.util import parse_ts, ts_from_datetime

ID_RE = re.compile(r"^[CGDUW][A-Z0-9]{6,}$")
MAX_SCHEDULE_DAYS = 120


def resolve_target(call: Call, value: str | None) -> dict[str, Any]:
    """``channel`` argument -> conversation: an id, a ``#name`` (public) or a user id (opens the DM)."""
    ctx = call.ctx
    if not value:
        raise SlackError("channel_not_found")
    if ID_RE.match(value):
        if value[0] in "UW":
            user = access.user_by_id(ctx, value)
            if user is None or user.get("is_stranger"):
                raise SlackError("channel_not_found")
            if user.get("deleted"):
                raise SlackError("is_archived")
            return messages.open_im(ctx, call.me.uid, user["id"])
        return access.require_channel(ctx, call.me, value)
    channel = access.channel_by_name(ctx, value)
    if channel is None or not access.can_view(ctx, call.me, channel):
        raise SlackError("channel_not_found")
    return channel


def check_postable(call: Call, channel: dict[str, Any]) -> None:
    actor = call.me
    if channel.get("is_archived"):
        raise SlackError("is_archived")
    if access.is_member(channel, actor.uid):
        return
    if actor.is_bot and access.channel_kind(channel) == "public" and actor.has("chat:write.public"):
        return
    raise SlackError("not_in_channel")


def author_fields(call: Call) -> dict[str, Any]:
    """Who a message posted with this token appears to come from."""
    actor = call.me
    if actor.is_bot:
        return {"user": actor.uid, "bot_id": actor.bot_id, "app_id": actor.app_id}
    return {"user": actor.uid}


def customization(call: Call) -> dict[str, Any]:
    """``username``/``icon_*`` overrides, honoured only with ``chat:write.customize``."""
    extra: dict[str, Any] = {}
    if not (call.me.is_bot and call.me.has("chat:write.customize")):
        return extra
    p = call.params
    if p.str("username"):
        extra["username"] = p.str("username")
    if p.str("icon_emoji"):
        extra["icons"] = {"emoji": p.str("icon_emoji")}
    elif p.str("icon_url"):
        extra["icons"] = {"image_64": p.str("icon_url")}
    if extra:
        extra["subtype"] = "bot_message"
    return extra


def content(call: Call, *, required: bool = True) -> dict[str, Any]:
    """Validated ``text``/``blocks``/``attachments`` arguments (``no_text`` when all are empty)."""
    p = call.params
    text = p.str("text")
    blocks = p.obj("blocks", "invalid_blocks_format")
    attachments = p.obj("attachments", "invalid_attachments")
    if required and not text and not blocks and not attachments:
        raise SlackError("no_text")
    if blocks is not None:
        validate_blocks(blocks, "check")
    if attachments is not None:
        validate_attachments(attachments)
    return {"text": text, "blocks": blocks, "attachments": attachments}


def metadata_arg(call: Call) -> dict[str, Any] | None:
    raw = call.params.obj("metadata", "invalid_metadata_format")
    if raw is None:
        return None
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("event_type"), str)
        or not isinstance(raw.get("event_payload", {}), dict)
    ):
        raise SlackError("invalid_metadata_format")
    return raw


@api("chat.postMessage", scopes=("chat:write", "chat:write:user", "chat:write:bot"))
async def post_message(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    channel = resolve_target(call, p.str("channel"))
    body = content(call)
    check_postable(call, channel)
    thread_ts = p.str("thread_ts")
    if thread_ts and parse_ts(thread_ts) is None:
        raise SlackError("invalid_ts")
    extra = customization(call)
    subtype = extra.pop("subtype", None)
    metadata = metadata_arg(call)
    if metadata:
        extra["metadata"] = metadata
    rec = await messages.create_message(
        ctx,
        channel,
        text=body["text"] or "",
        subtype=subtype,
        blocks=body["blocks"],
        attachments=body["attachments"],
        thread_ts=thread_ts,
        broadcast=bool(thread_ts) and p.bool("reply_broadcast"),
        extra=extra or None,
        **author_fields(call),
    )
    return {
        "channel": channel["id"],
        "ts": rec["ts"],
        "message": message_json(ctx, call.request, call.me, rec),
    }


@api("chat.update", scopes=("chat:write", "chat:write:user", "chat:write:bot"))
async def update(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    channel = access.require_channel(ctx, call.me, p.str("channel"))
    ts = p.need("ts")
    if parse_ts(ts) is None:
        raise SlackError("message_not_found")
    rec = access.get_message(ctx, channel["id"], ts)
    if rec is None:
        raise SlackError("message_not_found")
    body = content(call)
    if rec.get("user") != call.me.uid or rec.get("subtype") == "tombstone":
        raise SlackError("cant_update_message")
    await messages.update_message(
        ctx,
        channel,
        rec,
        editor=call.me.uid,
        text=body["text"],
        blocks=body["blocks"],
        attachments=body["attachments"],
    )
    return {
        "channel": channel["id"],
        "ts": ts,
        "text": rec["text"],
        "message": message_json(ctx, call.request, call.me, rec),
    }


@api("chat.delete", scopes=("chat:write", "chat:write:user", "chat:write:bot"))
async def delete(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    channel = access.require_channel(ctx, call.me, p.str("channel"))
    ts = p.need("ts")
    rec = access.get_message(ctx, channel["id"], ts) if parse_ts(ts) else None
    if rec is None:
        raise SlackError("message_not_found")
    if rec.get("user") != call.me.uid:
        allowed = not call.me.is_bot and access.is_admin(access.actor_user(ctx, call.me))
        if not allowed:
            raise SlackError("cant_delete_message")
    await messages.delete_message(ctx, channel, rec)
    return {"channel": channel["id"], "ts": ts}


@api("chat.postEphemeral", scopes=("chat:write", "chat:write:user", "chat:write:bot"))
async def post_ephemeral(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    channel = resolve_target(call, p.str("channel"))
    target = access.user_by_id(ctx, p.need("user"))
    body = content(call)
    check_postable(call, channel)
    if target is None or not access.is_member(channel, target["id"]):
        raise SlackError("user_not_in_channel")
    stamp = ts_from_datetime(ctx.clock.now())
    access.col(ctx, access.EPHEMERAL).put(
        {
            "id": stamp,
            "channel": channel["id"],
            "user": target["id"],
            "text": body["text"] or "",
            "blocks": body["blocks"],
            "attachments": body["attachments"],
            "thread_ts": p.str("thread_ts"),
            "posted_by": call.me.uid,
        }
    )
    return {"message_ts": stamp}


@api("chat.getPermalink")
async def get_permalink(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    channel = access.require_channel(ctx, call.me, p.str("channel"))
    ts = p.need("message_ts")
    rec = access.get_message(ctx, channel["id"], ts) if parse_ts(ts) else None
    if rec is None:
        raise SlackError("message_not_found")
    return {"channel": channel["id"], "permalink": permalink_for(rec)}


@api("chat.meMessage", scopes=("chat:write", "chat:write:user", "chat:write:bot"))
async def me_message(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    channel = resolve_target(call, p.str("channel"))
    text = p.str("text")
    if not text:
        raise SlackError("no_text")
    check_postable(call, channel)
    rec = await messages.create_message(ctx, channel, text=text, subtype="me_message", user=call.me.uid)
    return {"channel": channel["id"], "ts": rec["ts"]}


@api("chat.scheduleMessage", scopes=("chat:write", "chat:write:user", "chat:write:bot"))
async def schedule_message(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    channel = resolve_target(call, p.str("channel"))
    try:
        post_at = p.int("post_at")
    except SlackError:
        post_at = None
    if post_at is None:
        raise SlackError("invalid_time")
    body = content(call)
    check_postable(call, channel)
    now = ctx.clock.now()
    if post_at <= int(now.timestamp()):
        raise SlackError("time_in_past")
    if post_at > int((now + timedelta(days=MAX_SCHEDULE_DAYS)).timestamp()):
        raise SlackError("time_too_far")
    thread_ts = p.str("thread_ts")
    if thread_ts and access.get_message(ctx, channel["id"], thread_ts) is None:
        raise SlackError("invalid_ts")
    sid = new_id(ctx, "Q", access.SCHEDULED)
    author = author_fields(call)
    payload = {
        "text": body["text"] or "",
        "blocks": body["blocks"],
        "attachments": body["attachments"],
        "thread_ts": thread_ts,
        "broadcast": bool(thread_ts) and p.bool("reply_broadcast"),
        **author,
    }
    access.col(ctx, access.SCHEDULED).put(
        {
            "id": sid,
            "channel_id": channel["id"],
            "post_at": post_at,
            "date_created": int(now.timestamp()),
            "text": payload["text"],
            "_payload": payload,
            "_actor": call.me.uid,
        }
    )
    message: dict[str, Any] = {
        "text": payload["text"],
        "type": "delayed_message",
        "user": call.me.uid,
        "team": access.team_id(ctx),
    }
    if call.me.is_bot:
        message.update({"bot_id": call.me.bot_id, "app_id": call.me.app_id})
    return {"channel": channel["id"], "scheduled_message_id": sid, "post_at": post_at, "message": message}


@api("chat.scheduledMessages.list", scopes=("chat:write", "chat:write:user", "chat:write:bot"))
async def scheduled_list(call: Call) -> dict[str, Any]:
    from emulators.providers.slack.paging import paginate

    p = call.params
    channel_id = p.str("channel")
    oldest, latest = p.int("oldest"), p.int("latest")
    items = [
        s
        for s in access.col(call.ctx, access.SCHEDULED)
        if s["_actor"] == call.me.uid
        and (not channel_id or s["channel_id"] == channel_id)
        and (oldest is None or s["post_at"] >= oldest)
        and (latest is None or s["post_at"] <= latest)
    ]
    items.sort(key=lambda s: (s["post_at"], s["id"]))
    page, meta = paginate(items, call, kind="scheduled", key=lambda s: s["id"], default_limit=100)
    return {
        "scheduled_messages": [
            {
                "id": s["id"],
                "channel_id": s["channel_id"],
                "post_at": s["post_at"],
                "date_created": s["date_created"],
                "text": s["text"],
            }
            for s in page
        ],
        "response_metadata": meta,
    }


@api("chat.deleteScheduledMessage", scopes=("chat:write", "chat:write:user", "chat:write:bot"))
async def delete_scheduled(call: Call) -> dict[str, Any]:
    p = call.params
    item = access.col(call.ctx, access.SCHEDULED).get(p.need("scheduled_message_id"))
    if item is None or item["_actor"] != call.me.uid or item["channel_id"] != p.str("channel"):
        raise SlackError("invalid_scheduled_message_id")
    access.col(call.ctx, access.SCHEDULED).delete(item["id"])
    return {}


@api("chat.unfurl", scopes=("links:write",))
async def unfurl(call: Call) -> dict[str, Any]:
    if not (call.params.has("unfurls") or call.params.has("unfurl_id")):
        raise SlackError("invalid_arguments")
    return {}
