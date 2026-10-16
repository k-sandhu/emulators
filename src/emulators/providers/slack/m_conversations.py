"""conversations.* Web API methods."""

from __future__ import annotations

import re
from typing import Any

from emulators.providers.slack import access, events, messages
from emulators.providers.slack.paging import paginate
from emulators.providers.slack.records import make_channel, new_id
from emulators.providers.slack.runtime import Call, SlackError, api
from emulators.providers.slack.shapes import channel_json, message_json
from emulators.providers.slack.util import decode_cursor, encode_cursor, parse_ts, ts_key

TYPE_SCOPES = {
    "public_channel": "channels:read",
    "private_channel": "groups:read",
    "mpim": "mpim:read",
    "im": "im:read",
}
TYPE_OF_KIND = {"public": "public_channel", "private": "private_channel", "mpim": "mpim", "im": "im"}
NAME_RE = re.compile(r"^[a-z0-9\-_]+$")


def _channel_out(call: Call, channel: dict[str, Any], **flags: Any) -> dict[str, Any]:
    return channel_json(call.ctx, channel, call.me.uid, **flags)


def _visible_in_list(call: Call, channel: dict[str, Any]) -> bool:
    uid = call.me.uid
    if channel.get("is_im") and uid in channel.get("_closed_for", []):
        return False
    return access.can_view(call.ctx, call.me, channel)


def validate_channel_name(call: Call, raw: str | None, *, current: str | None = None) -> str:
    """Slack's channel naming rules; returns the normalised name."""
    if not raw or not raw.strip().lstrip("#"):
        raise SlackError("invalid_name_required")
    name = raw.strip().lstrip("#").lower()
    if len(name) > 80:
        raise SlackError("invalid_name_maxlength")
    if not NAME_RE.match(name):
        raise SlackError("invalid_name_specials")
    if name[0] in "-_" and len(set(name)) == 1:
        raise SlackError("invalid_name_punctuation")
    existing = access.channel_by_name(call.ctx, name)
    if existing is not None and name != current:
        raise SlackError("name_taken")
    return name


def _require_member(call: Call, channel: dict[str, Any]) -> None:
    if not access.is_member(channel, call.me.uid):
        raise SlackError("not_in_channel")


def _not_archived(channel: dict[str, Any]) -> None:
    if channel.get("is_archived"):
        raise SlackError("is_archived")


def _reject_dm(channel: dict[str, Any]) -> None:
    if channel.get("is_im") or channel.get("is_mpim"):
        raise SlackError("method_not_supported_for_channel_type")


@api("conversations.list")
async def conversations_list(call: Call) -> dict[str, Any]:
    types = call.params.csv("types") or ["public_channel"]
    for kind in types:
        if kind not in TYPE_SCOPES:
            raise SlackError("invalid_types")
        call.need_scope(TYPE_SCOPES[kind])
    exclude_archived = call.params.bool("exclude_archived")
    items = [
        c
        for c in access.col(call.ctx, access.CHANNELS)
        if TYPE_OF_KIND[access.channel_kind(c)] in types
        and not (exclude_archived and c.get("is_archived"))
        and _visible_in_list(call, c)
    ]
    page, meta = paginate(items, call, kind="team", key=lambda c: c["id"])
    return {"channels": [_channel_out(call, c, num_members=True) for c in page], "response_metadata": meta}


@api("conversations.info")
async def conversations_info(call: Call) -> dict[str, Any]:
    channel = access.require_channel(call.ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "read"))
    flags = {"num_members": call.params.bool("include_num_members"), "last_read": True}
    return {"channel": _channel_out(call, channel, **flags)}


def _history_window(call: Call) -> tuple[str, str, bool]:
    oldest = call.params.str("oldest", "0") or "0"
    latest = call.params.str("latest")
    if parse_ts(oldest) is None:
        raise SlackError("invalid_ts_oldest")
    if latest is not None and parse_ts(latest) is None:
        raise SlackError("invalid_ts_latest")
    return oldest, latest or "9999999999.999999", call.params.bool("inclusive")


def _in_window(ts: str, oldest: str, latest: str, inclusive: bool) -> bool:
    key = ts_key(ts)
    if inclusive:
        return ts_key(oldest) <= key <= ts_key(latest)
    return ts_key(oldest) < key < ts_key(latest)


def _page_by_ts(
    call: Call, items: list[dict[str, Any]], *, newest_first: bool, default_limit: int, max_limit: int
) -> tuple[list[dict[str, Any]], bool, str]:
    """Slice messages by ``limit`` and the ``next_ts`` cursor Slack uses for history paging."""
    cursor = call.params.str("cursor")
    ordered = sorted(items, key=lambda m: ts_key(m["ts"]), reverse=newest_first)
    if cursor:
        start_ts = decode_cursor(cursor, "next_ts")
        if start_ts is None or parse_ts(start_ts) is None:
            raise SlackError("invalid_cursor")
        ordered = [
            m for m in ordered if (ts_key(m["ts"]) <= ts_key(start_ts)) == newest_first or m["ts"] == start_ts
        ]
    limit = call.params.int("limit", default_limit, lo=1, hi=max_limit) or default_limit
    page = ordered[:limit]
    more = len(ordered) > limit
    return page, more, encode_cursor("next_ts", ordered[limit]["ts"]) if more else ""


@api("conversations.history")
async def conversations_history(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_readable(ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "history"))
    oldest, latest, inclusive = _history_window(call)
    top_level = [
        m
        for m in access.channel_messages(ctx, channel["id"])
        if (not m.get("thread_ts") or m["thread_ts"] == m["ts"] or m.get("subtype") == "thread_broadcast")
        and _in_window(m["ts"], oldest, latest, inclusive)
    ]
    page, more, cursor = _page_by_ts(call, top_level, newest_first=True, default_limit=100, max_limit=999)
    pins = [p for p in access.col(ctx, access.PINS) if p["channel"] == channel["id"]]
    return {
        "messages": [message_json(ctx, call.request, call.me, m) for m in page],
        "has_more": more,
        "pin_count": len(pins),
        "channel_actions_ts": None,
        "channel_actions_count": 0,
        "response_metadata": {"next_cursor": cursor},
    }


@api("conversations.replies")
async def conversations_replies(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_readable(ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "history"))
    ts = call.params.need("ts")
    if parse_ts(ts) is None:
        raise SlackError("invalid_ts")
    anchor = access.get_message(ctx, channel["id"], ts)
    if anchor is None:
        raise SlackError("thread_not_found")
    parent = anchor
    if anchor.get("thread_ts") and anchor["thread_ts"] != anchor["ts"]:
        parent = access.get_message(ctx, channel["id"], anchor["thread_ts"]) or anchor
    oldest, latest, inclusive = _history_window(call)
    replies = [
        m
        for m in access.thread_replies(ctx, channel["id"], parent["ts"])
        if _in_window(m["ts"], oldest, latest, inclusive)
    ]
    cursor_param = call.params.str("cursor")
    page, more, cursor = _page_by_ts(call, replies, newest_first=False, default_limit=1000, max_limit=1000)
    out = page if cursor_param else [parent, *page]
    body: dict[str, Any] = {
        "messages": [message_json(ctx, call.request, call.me, m) for m in out],
        "has_more": more,
    }
    if more:
        body["response_metadata"] = {"next_cursor": cursor}
    return body


@api("conversations.members")
async def conversations_members(call: Call) -> dict[str, Any]:
    channel = access.require_channel(call.ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "read"))
    live = [uid for uid in channel["_members"] if not (access.user_by_id(call.ctx, uid) or {}).get("deleted")]
    page, meta = paginate(live, call, kind="user", key=lambda uid: uid)
    return {"members": page, "response_metadata": meta}


@api("conversations.join", scopes=("channels:join", "channels:write"))
async def conversations_join(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.channel_by_id(ctx, call.params.str("channel"))
    if channel is None or not access.can_view(ctx, call.me, channel):
        raise SlackError("channel_not_found")
    if channel.get("is_im") or channel.get("is_mpim") or access.channel_kind(channel) == "private":
        raise SlackError("method_not_supported_for_channel_type")
    _not_archived(channel)
    if access.is_guest(access.actor_user(ctx, call.me)):
        raise SlackError("restricted_action")
    if access.is_member(channel, call.me.uid):
        call.warnings.append("already_in_channel")
    else:
        await messages.join_channel(ctx, channel, call.me.uid)
    return {"channel": _channel_out(call, channel, num_members=False)}


@api("conversations.leave")
async def conversations_leave(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_channel(ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "write"))
    _reject_dm(channel)
    if channel.get("is_general"):
        raise SlackError("cant_leave_general")
    if not access.is_member(channel, call.me.uid):
        return {"not_in_channel": True}
    await messages.leave_channel(ctx, channel, call.me.uid)
    return {}


@api("conversations.create")
async def conversations_create(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    private = call.params.bool("is_private")
    call.need_scope(
        *(("groups:write", "channels:manage") if private else ("channels:manage", "channels:write"))
    )
    if access.is_guest(access.actor_user(ctx, call.me)):
        raise SlackError("restricted_action")
    name = validate_channel_name(call, call.params.str("name"))
    now = int(ctx.clock.now().timestamp())
    record = make_channel(
        ctx,
        channel_id=new_id(ctx, "G" if private else "C", access.CHANNELS),
        name=name,
        creator=call.me.uid,
        created=now,
        members=[call.me.uid],
        is_private=private,
    )
    access.col(ctx, access.CHANNELS).put(record)
    await events.emit_channel_event(ctx, "channel_created", record)
    await events.emit_membership(ctx, True, record, call.me.uid)
    return {"channel": _channel_out(call, record, num_members=False)}


@api("conversations.invite")
async def conversations_invite(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_channel(ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "write"))
    _reject_dm(channel)
    refs = call.params.csv("users")
    if not refs:
        raise SlackError("no_user")
    if len(refs) > 100:
        raise SlackError("too_many_users")
    _not_archived(channel)
    _require_member(call, channel)
    if access.is_guest(access.actor_user(ctx, call.me)):
        raise SlackError("restricted_action")
    targets: list[dict[str, Any]] = []
    for ref in refs:
        user = access.user_by_id(ctx, ref)
        if user is None or user.get("is_stranger"):
            raise SlackError("user_not_found")
        if user.get("deleted"):
            raise SlackError("user_disabled")
        if user["id"] == call.me.uid:
            raise SlackError("cant_invite_self")
        targets.append(user)
    if len(targets) == 1 and access.is_member(channel, targets[0]["id"]):
        raise SlackError("already_in_channel")
    for user in targets:
        await messages.join_channel(ctx, channel, user["id"], inviter=call.me.uid)
    return {"channel": _channel_out(call, channel, num_members=False)}


@api("conversations.kick")
async def conversations_kick(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_channel(ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "write"))
    _reject_dm(channel)
    _require_member(call, channel)
    ref = call.params.need("user")
    user = access.user_by_id(ctx, ref)
    if user is None:
        raise SlackError("user_not_found")
    if user["id"] == call.me.uid:
        raise SlackError("cant_kick_self")
    if channel.get("is_general"):
        raise SlackError("cant_kick_from_general")
    if not access.is_member(channel, user["id"]):
        raise SlackError("not_in_channel")
    _not_archived(channel)
    await messages.leave_channel(ctx, channel, user["id"])
    return {}


@api("conversations.archive")
async def conversations_archive(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_channel(ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "write"))
    _reject_dm(channel)
    if channel.get("is_general"):
        raise SlackError("cant_archive_general")
    if channel.get("is_archived"):
        raise SlackError("already_archived")
    if not access.is_member(channel, call.me.uid) and not access.is_admin(access.actor_user(ctx, call.me)):
        raise SlackError("not_in_channel")
    await messages.system_message(
        ctx, channel, call.me.uid, "channel_archive", f"<@{call.me.uid}> archived the channel"
    )
    channel["is_archived"] = True
    messages.touch(ctx, channel)
    await events.emit_channel_event(ctx, "channel_archive", channel, call.me.uid)
    return {}


@api("conversations.unarchive")
async def conversations_unarchive(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_channel(ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "write"))
    _reject_dm(channel)
    if not channel.get("is_archived"):
        raise SlackError("not_archived")
    channel["is_archived"] = False
    messages.touch(ctx, channel)
    await messages.system_message(
        ctx, channel, call.me.uid, "channel_unarchive", f"<@{call.me.uid}> un-archived the channel"
    )
    await events.emit_channel_event(ctx, "channel_unarchive", channel, call.me.uid)
    return {}


@api("conversations.rename")
async def conversations_rename(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_channel(ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "write"))
    _reject_dm(channel)
    _not_archived(channel)
    _require_member(call, channel)
    if channel.get("is_general"):
        raise SlackError("not_authorized")
    old = channel["name"]
    name = validate_channel_name(call, call.params.str("name"), current=old)
    if name != old:
        channel["previous_names"].append(old)
        channel["name"] = channel["name_normalized"] = name
        messages.touch(ctx, channel)
        await messages.system_message(
            ctx,
            channel,
            call.me.uid,
            "channel_name",
            f'<@{call.me.uid}> has renamed the channel from "{old}" to "{name}"',
            old_name=old,
            name=name,
        )
        await events.emit_channel_event(ctx, "channel_rename", channel)
    return {"channel": _channel_out(call, channel, num_members=False)}


async def _set_meta(call: Call, field: str) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_channel(ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "write"))
    _reject_dm(channel)
    _not_archived(channel)
    _require_member(call, channel)
    if not call.params.has(field):
        raise SlackError("invalid_arguments")
    value = str(call.params.raw(field))
    if len(value) > 250:
        raise SlackError("too_long")
    now = int(ctx.clock.now().timestamp())
    channel[field] = {"value": value, "creator": call.me.uid, "last_set": now}
    messages.touch(ctx, channel)
    subtype = f"channel_{field}"
    verb = "set the channel topic" if field == "topic" else "set the channel purpose"
    await messages.system_message(
        ctx, channel, call.me.uid, subtype, f"<@{call.me.uid}> {verb}: {value}", **{field: value}
    )
    return {"channel": _channel_out(call, channel, num_members=False), field: value}


@api("conversations.setTopic")
async def conversations_set_topic(call: Call) -> dict[str, Any]:
    return await _set_meta(call, "topic")


@api("conversations.setPurpose")
async def conversations_set_purpose(call: Call) -> dict[str, Any]:
    return await _set_meta(call, "purpose")


def _open_targets(call: Call) -> list[str]:
    """Other participants named by ``users``: workspace members only, deduplicated."""
    refs = call.params.csv("users")
    if not refs:
        raise SlackError("users_list_not_supplied")
    others: list[str] = []
    for ref in refs:
        user = access.user_by_id(call.ctx, ref)
        if user is None or user.get("is_stranger"):
            raise SlackError("user_not_found")
        if user.get("deleted"):
            raise SlackError("user_disabled")
        if user["id"] != call.me.uid and user["id"] not in others:
            others.append(user["id"])
    if len(others) > 8:
        raise SlackError("too_many_users")
    return others


@api("conversations.open")
async def conversations_open(call: Call) -> dict[str, Any]:
    ctx, me = call.ctx, call.me.uid
    existing_id = call.params.str("channel")
    if existing_id:
        channel = access.require_channel(ctx, call.me, existing_id)
        if not (channel.get("is_im") or channel.get("is_mpim")):
            raise SlackError("channel_not_found")
        call.need_scope(*access.conversation_scope(channel, "write"))
        found = True
    else:
        others = _open_targets(call)
        call.need_scope("im:write" if len(others) <= 1 else "mpim:write")
        if len(others) <= 1:
            target = others[0] if others else me
            channel = access.find_im(ctx, me, target)
        else:
            channel = access.find_mpim(ctx, {me, *others})
        found = channel is not None
        if channel is None:
            if call.params.bool("prevent_creation"):
                return {"no_op": True}
            channel = (
                messages.open_im(ctx, me, others[0] if others else me)
                if len(others) <= 1
                else messages.open_mpim(ctx, [me, *others])
            )
    was_closed = me in channel.get("_closed_for", [])
    if was_closed:
        channel["_closed_for"].remove(me)
    body: dict[str, Any] = {"no_op": True, "already_open": True} if found and not was_closed else {}
    body["channel"] = _channel_out(call, channel) if call.params.bool("return_im") else {"id": channel["id"]}
    return body


@api("conversations.close")
async def conversations_close(call: Call) -> dict[str, Any]:
    channel = access.require_channel(call.ctx, call.me, call.params.str("channel"))
    if not (channel.get("is_im") or channel.get("is_mpim")):
        raise SlackError("channel_not_found")
    call.need_scope(*access.conversation_scope(channel, "write"))
    if call.me.uid in channel.get("_closed_for", []):
        return {"no_op": True, "already_closed": True}
    channel.setdefault("_closed_for", []).append(call.me.uid)
    return {}


@api("conversations.mark")
async def conversations_mark(call: Call) -> dict[str, Any]:
    channel = access.require_channel(call.ctx, call.me, call.params.str("channel"))
    call.need_scope(*access.conversation_scope(channel, "write"))
    ts = call.params.need("ts")
    if parse_ts(ts) is None:
        raise SlackError("invalid_timestamp")
    _require_member(call, channel)
    channel.setdefault("_marks", {})[call.me.uid] = ts
    return {}
