"""Message lifecycle shared by chat.*, incoming webhooks, files and the simulation actions.

Creating, editing and deleting messages keep thread counters, file shares, pins and the
Events API in step, so every entry point (API call, webhook, action) behaves the same.
"""

from __future__ import annotations

import copy
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.slack import access, events
from emulators.providers.slack.records import make_channel, new_id
from emulators.providers.slack.richtext import text_to_blocks, validate_attachments, validate_blocks
from emulators.providers.slack.runtime import BEFORE_CALL, SlackError
from emulators.providers.slack.util import ts_from_datetime, ts_key

MAX_TEXT = 40_000


def _now_ms(ctx: ProviderContext) -> int:
    return int(ctx.clock.now().timestamp() * 1000)


def touch(ctx: ProviderContext, channel: dict[str, Any]) -> None:
    channel["updated"] = _now_ms(ctx)


def refresh_thread(ctx: ProviderContext, channel_id: str, parent: dict[str, Any]) -> None:
    """Recompute the counters Slack keeps on a thread's parent message."""
    replies = access.thread_replies(ctx, channel_id, parent["ts"])
    if not replies:
        for key in ("reply_count", "reply_users", "reply_users_count", "latest_reply"):
            parent.pop(key, None)
        if parent.get("subtype") != "tombstone":
            parent.pop("thread_ts", None)
        return
    users: list[str] = []
    for reply in replies:
        who = reply.get("user")
        if who and who not in users:
            users.append(who)
    parent["thread_ts"] = parent["ts"]
    parent["reply_count"] = len(replies)
    parent["reply_users"] = users
    parent["reply_users_count"] = len(users)
    parent["latest_reply"] = replies[-1]["ts"]


async def create_message(
    ctx: ProviderContext,
    channel: dict[str, Any],
    *,
    user: str | None = None,
    text: str = "",
    subtype: str | None = None,
    blocks: Any = None,
    attachments: Any = None,
    thread_ts: str | None = None,
    broadcast: bool = False,
    files: list[str] | None = None,
    bot_id: str | None = None,
    app_id: str | None = None,
    extra: dict[str, Any] | None = None,
    ts: str | None = None,
    emit: bool = True,
) -> dict[str, Any]:
    if len(text) > MAX_TEXT:
        raise SlackError("msg_too_long")
    stamp = ts or access.new_ts(ctx, channel)
    if ts is not None:
        channel["_last_ts"] = max(channel.get("_last_ts") or "0.000000", ts, key=ts_key)
    rec: dict[str, Any] = {
        "id": access.message_key(channel["id"], stamp),
        "_channel": channel["id"],
        "type": "message",
    }
    if subtype:
        rec["subtype"] = subtype
    if user:
        rec["user"] = user
    rec["text"] = text
    rec["ts"] = stamp
    rec["team"] = access.team_id(ctx)
    if bot_id:
        rec["bot_id"] = bot_id
    if app_id:
        rec["app_id"] = app_id
    if blocks is not None:
        rec["blocks"] = validate_blocks(blocks, stamp)
    elif text and (not subtype or subtype in ("thread_broadcast", "bot_message", "me_message", "file_share")):
        generated = text_to_blocks(text, stamp)
        if generated:
            rec["blocks"] = generated
    if attachments is not None:
        rec["attachments"] = validate_attachments(attachments)
    if channel.get("is_ext_shared") and user:
        owner = access.user_by_id(ctx, user)
        rec["user_team"] = (owner or {}).get("team_id", access.team_id(ctx))
        rec["source_team"] = rec["user_team"]
    parent: dict[str, Any] | None = None
    if thread_ts:
        parent = access.get_message(ctx, channel["id"], thread_ts)
        if parent is None:
            raise SlackError("thread_not_found")
        if parent.get("thread_ts") and parent["thread_ts"] != parent["ts"]:
            parent = access.get_message(ctx, channel["id"], parent["thread_ts"]) or parent
        rec["thread_ts"] = parent["ts"]
        if parent.get("user"):
            rec["parent_user_id"] = parent["user"]
        if broadcast:
            rec["subtype"] = "thread_broadcast"
    if files:
        rec["_files"] = list(files)
    if extra:
        rec.update(extra)
    access.col(ctx, access.MESSAGES).put(rec)
    if parent is not None:
        refresh_thread(ctx, channel["id"], parent)
    for file_id in files or []:
        record = access.col(ctx, access.FILES).get(file_id)
        if record is not None:
            record.setdefault("_shares", []).append({"channel": channel["id"], "ts": stamp})
    touch(ctx, channel)
    if emit:
        await events.emit_message(ctx, channel, rec)
    return rec


async def update_message(
    ctx: ProviderContext,
    channel: dict[str, Any],
    rec: dict[str, Any],
    *,
    editor: str,
    text: str | None = None,
    blocks: Any = None,
    attachments: Any = None,
) -> dict[str, Any]:
    previous = copy.deepcopy(rec)
    if text is not None:
        if len(text) > MAX_TEXT:
            raise SlackError("msg_too_long")
        rec["text"] = text
        if blocks is None:
            generated = text_to_blocks(text, rec["ts"])
            if generated:
                rec["blocks"] = generated
            else:
                rec.pop("blocks", None)
    if blocks is not None:
        rec["blocks"] = validate_blocks(blocks, rec["ts"])
    if attachments is not None:
        rec["attachments"] = validate_attachments(attachments)
    rec["edited"] = {"user": editor, "ts": ts_from_datetime(ctx.clock.now())}
    touch(ctx, channel)
    await events.emit_message_changed(ctx, channel, rec, previous)
    return rec


async def delete_message(ctx: ProviderContext, channel: dict[str, Any], rec: dict[str, Any]) -> None:
    """Delete a message; a thread parent that still has replies becomes a tombstone."""
    previous = copy.deepcopy(rec)
    cid = channel["id"]
    messages = access.col(ctx, access.MESSAGES)
    if rec.get("reply_count") and rec.get("thread_ts") == rec["ts"]:
        for key in ("user", "blocks", "attachments", "_files", "reactions", "files", "bot_id", "app_id"):
            rec.pop(key, None)
        rec.update({"subtype": "tombstone", "text": "This message was deleted.", "hidden": True})
        rec["user"] = access.SLACKBOT
    else:
        messages.delete(rec["id"])
        if rec.get("thread_ts") and rec["thread_ts"] != rec["ts"]:
            parent = access.get_message(ctx, cid, rec["thread_ts"])
            if parent is not None:
                refresh_thread(ctx, cid, parent)
    for pin in access.col(ctx, access.PINS).filter(lambda p: p["channel"] == cid and p["ts"] == rec["ts"]):
        access.col(ctx, access.PINS).delete(pin["id"])
    for file_id in previous.get("_files", []):
        record = access.col(ctx, access.FILES).get(file_id)
        if record is not None:
            record["_shares"] = [s for s in record.get("_shares", []) if s["ts"] != rec["ts"]]
    touch(ctx, channel)
    await events.emit_message_deleted(ctx, channel, previous)


# -- system messages ---------------------------------------------------------------------------


async def system_message(
    ctx: ProviderContext,
    channel: dict[str, Any],
    uid: str,
    subtype: str,
    text: str,
    **extra: Any,
) -> dict[str, Any]:
    return await create_message(ctx, channel, user=uid, text=text, subtype=subtype, extra=extra or None)


async def join_channel(
    ctx: ProviderContext, channel: dict[str, Any], uid: str, inviter: str | None = None
) -> bool:
    """Add a member, post the ``channel_join`` message and fire ``member_joined_channel``."""
    if not access.add_member(channel, uid):
        return False
    extra = {"inviter": inviter} if inviter else {}
    if access.channel_kind(channel) in ("public", "private"):
        await system_message(ctx, channel, uid, "channel_join", f"<@{uid}> has joined the channel", **extra)
    await events.emit_membership(ctx, True, channel, uid, inviter)
    return True


async def leave_channel(ctx: ProviderContext, channel: dict[str, Any], uid: str) -> bool:
    if not access.is_member(channel, uid):
        return False
    if access.channel_kind(channel) in ("public", "private"):
        await system_message(ctx, channel, uid, "channel_leave", f"<@{uid}> has left the channel")
    # The bot's own departure must still be delivered, so notify before removing it.
    await events.emit_membership(ctx, False, channel, uid)
    access.remove_member(channel, uid)
    return True


# -- direct messages ---------------------------------------------------------------------------


def open_im(ctx: ProviderContext, a: str, b: str) -> dict[str, Any]:
    """The IM between two users, created on demand."""
    existing = access.find_im(ctx, a, b)
    if existing is not None:
        return existing
    now = int(ctx.clock.now().timestamp())
    record = make_channel(
        ctx,
        channel_id=new_id(ctx, "D", access.CHANNELS),
        creator=a,
        created=now,
        members=[a, b],
        is_im=True,
    )
    return access.col(ctx, access.CHANNELS).put(record)


def mpim_name(ctx: ProviderContext, uids: list[str]) -> str:
    handles = [(access.user_by_id(ctx, u) or {}).get("name", u.lower()) for u in uids]
    return "mpdm-" + "--".join(handles) + "-1"


def open_mpim(ctx: ProviderContext, uids: list[str]) -> dict[str, Any]:
    existing = access.find_mpim(ctx, set(uids))
    if existing is not None:
        return existing
    now = int(ctx.clock.now().timestamp())
    name = mpim_name(ctx, uids)
    record = make_channel(
        ctx,
        channel_id=new_id(ctx, "G", access.CHANNELS),
        name=name,
        creator=uids[0],
        created=now,
        members=uids,
        is_mpim=True,
        purpose=f"Group messaging with: {', '.join('@' + (access.user_by_id(ctx, u) or {}).get('name', u) for u in uids)}",
    )
    return access.col(ctx, access.CHANNELS).put(record)


# -- scheduled messages ------------------------------------------------------------------------


async def process_scheduled(ctx: ProviderContext) -> None:
    """Post scheduled messages whose time has come (the clock is controllable, so this is lazy)."""
    scheduled = access.col(ctx, access.SCHEDULED)
    if not len(scheduled):
        return
    now = int(ctx.clock.now().timestamp())
    for item in sorted(scheduled.values(), key=lambda s: s["post_at"]):
        if item["post_at"] > now:
            continue
        scheduled.delete(item["id"])
        channel = access.channel_by_id(ctx, item["channel_id"])
        if channel is None or channel.get("is_archived"):
            continue
        payload = item["_payload"]
        await create_message(
            ctx,
            channel,
            user=payload.get("user"),
            text=payload.get("text", ""),
            blocks=payload.get("blocks"),
            attachments=payload.get("attachments"),
            thread_ts=payload.get("thread_ts"),
            broadcast=payload.get("broadcast", False),
            bot_id=payload.get("bot_id"),
            app_id=payload.get("app_id"),
        )


BEFORE_CALL.append(process_scheduled)
