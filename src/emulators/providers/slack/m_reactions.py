"""reactions.*, pins.* and bookmarks.* Web API methods."""

from __future__ import annotations

import re
from typing import Any

from emulators.providers.slack import access, events, messages
from emulators.providers.slack.paging import paginate
from emulators.providers.slack.records import new_id
from emulators.providers.slack.runtime import Call, SlackError, api
from emulators.providers.slack.shapes import file_json, file_visible, message_json
from emulators.providers.slack.util import parse_ts, ts_key

NAME_RE = re.compile(r"^[a-z0-9_+\-']+(?:::skin-tone-[2-6])?$")
MAX_DISTINCT_REACTIONS = 23
MAX_REACTIONS = 50
MAX_BOOKMARKS = 100


def _emoji_name(call: Call) -> str:
    name = (call.params.str("name") or "").strip().strip(":").lower()
    if not name or not NAME_RE.match(name):
        raise SlackError("invalid_name")
    return name


def _message_target(call: Call, *, write: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """The (conversation, message) addressed by ``channel`` + ``timestamp``."""
    ctx, p = call.ctx, call.params
    if not p.str("channel") or not p.str("timestamp"):
        if p.str("file") or p.str("file_comment"):
            raise SlackError("invalid_arguments")
        raise SlackError("no_item_specified")
    channel = access.require_readable(ctx, call.me, p.str("channel"))
    ts = p.str("timestamp") or ""
    if parse_ts(ts) is None:
        raise SlackError("bad_timestamp")
    rec = access.get_message(ctx, channel["id"], ts)
    if rec is None:
        raise SlackError("message_not_found")
    if write and channel.get("is_archived"):
        raise SlackError("is_archived")
    return channel, rec


def _file_target(call: Call) -> dict[str, Any] | None:
    ref = call.params.str("file")
    if not ref:
        return None
    record = access.col(call.ctx, access.FILES).get(ref)
    if record is None or not file_visible(call.ctx, call.me, record):
        raise SlackError("file_not_found")
    return record


@api("reactions.add", scopes=("reactions:write",))
async def reactions_add(call: Call) -> dict[str, Any]:
    name = _emoji_name(call)
    target_file = _file_target(call)
    holder: dict[str, Any]
    if target_file is not None:
        holder, channel = target_file, None
    else:
        channel, holder = _message_target(call, write=True)
    reactions: list[dict[str, Any]] = holder.setdefault("reactions", [])
    existing = next((r for r in reactions if r["name"] == name), None)
    if existing and call.me.uid in existing["users"]:
        raise SlackError("already_reacted")
    if existing is None and len(reactions) >= MAX_DISTINCT_REACTIONS:
        raise SlackError("too_many_emoji")
    if sum(r["count"] for r in reactions) >= MAX_REACTIONS:
        raise SlackError("too_many_reactions")
    if existing is None:
        reactions.append({"name": name, "users": [call.me.uid], "count": 1})
    else:
        existing["users"].append(call.me.uid)
        existing["count"] = len(existing["users"])
    if channel is not None:
        await events.emit_reaction(call.ctx, True, channel, holder, call.me.uid, name)
    return {}


@api("reactions.remove", scopes=("reactions:write",))
async def reactions_remove(call: Call) -> dict[str, Any]:
    name = _emoji_name(call)
    target_file = _file_target(call)
    if target_file is not None:
        holder, channel = target_file, None
    else:
        channel, holder = _message_target(call, write=True)
    reactions: list[dict[str, Any]] = holder.get("reactions", [])
    existing = next((r for r in reactions if r["name"] == name), None)
    if existing is None or call.me.uid not in existing["users"]:
        raise SlackError("no_reaction")
    existing["users"].remove(call.me.uid)
    existing["count"] = len(existing["users"])
    if not existing["users"]:
        reactions.remove(existing)
    if not reactions:
        holder.pop("reactions", None)
    if channel is not None:
        await events.emit_reaction(call.ctx, False, channel, holder, call.me.uid, name)
    return {}


@api("reactions.get", scopes=("reactions:read",))
async def reactions_get(call: Call) -> dict[str, Any]:
    target_file = _file_target(call)
    if target_file is not None:
        return {"type": "file", "file": file_json(call.ctx, call.request, target_file)}
    channel, rec = _message_target(call, write=False)
    return {
        "type": "message",
        "channel": channel["id"],
        "message": message_json(call.ctx, call.request, call.me, rec),
    }


@api("reactions.list", scopes=("reactions:read",))
async def reactions_list(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    uid = p.str("user") or call.me.uid
    if access.user_by_id(ctx, uid) is None:
        raise SlackError("user_not_found")
    items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for rec in access.col(ctx, access.MESSAGES):
        if not any(uid in r["users"] for r in rec.get("reactions", [])):
            continue
        channel = access.channel_by_id(ctx, rec["_channel"])
        if channel is None or not access.can_view(ctx, call.me, channel):
            continue
        if not access.is_member(channel, call.me.uid) and access.channel_kind(channel) != "public":
            continue
        items.append((channel, rec))
    items.sort(key=lambda pair: ts_key(pair[1]["ts"]), reverse=True)
    paging: dict[str, Any] | None = None
    if p.has("page") or p.has("count"):
        count = p.int("count", 100, lo=1, hi=1000) or 100
        page_no = p.int("page", 1, lo=1) or 1
        total = len(items)
        window = items[(page_no - 1) * count : page_no * count]
        paging = {"count": count, "total": total, "page": page_no, "pages": max(1, -(-total // count))}
        meta: dict[str, str] = {"next_cursor": ""}
    else:
        keyed = [(f"{c['id']}:{r['ts']}", c, r) for c, r in items]
        window_keyed, meta = paginate(keyed, call, kind="item", key=lambda t: t[0])
        window = [(c, r) for _, c, r in window_keyed]
    body: dict[str, Any] = {
        "items": [
            {
                "type": "message",
                "channel": channel["id"],
                "message": message_json(ctx, call.request, call.me, rec),
            }
            for channel, rec in window
        ],
        "response_metadata": meta,
    }
    if paging:
        body["paging"] = paging
    return body


# -- pins --------------------------------------------------------------------------------------


@api("pins.add", scopes=("pins:write",))
async def pins_add(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_channel(ctx, call.me, call.params.str("channel"))
    if not call.params.str("timestamp"):
        raise SlackError("no_item_specified")
    ts = call.params.str("timestamp") or ""
    if parse_ts(ts) is None:
        raise SlackError("bad_timestamp")
    if not access.is_member(channel, call.me.uid):
        raise SlackError("not_in_channel")
    if channel.get("is_archived"):
        raise SlackError("is_archived")
    rec = access.get_message(ctx, channel["id"], ts)
    if rec is None:
        raise SlackError("message_not_found")
    key = access.message_key(channel["id"], ts)
    if access.col(ctx, access.PINS).get(key) is not None:
        raise SlackError("already_pinned")
    pin = access.col(ctx, access.PINS).put(
        {
            "id": key,
            "channel": channel["id"],
            "ts": ts,
            "created": int(ctx.clock.now().timestamp()),
            "created_by": call.me.uid,
            "type": "message",
        }
    )
    await messages.system_message(
        ctx,
        channel,
        call.me.uid,
        "pinned_item",
        f"<@{call.me.uid}> pinned a message to this channel.",
        item_type="C",
        pinned_ts=ts,
    )
    await events.emit_pin(ctx, True, channel, rec, call.me.uid, pin)
    return {}


@api("pins.remove", scopes=("pins:write",))
async def pins_remove(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_channel(ctx, call.me, call.params.str("channel"))
    ts = call.params.str("timestamp")
    if not ts:
        raise SlackError("no_item_specified")
    if parse_ts(ts) is None:
        raise SlackError("bad_timestamp")
    if not access.is_member(channel, call.me.uid):
        raise SlackError("not_in_channel")
    pin = access.col(ctx, access.PINS).get(access.message_key(channel["id"], ts))
    if pin is None:
        raise SlackError("not_pinned")
    access.col(ctx, access.PINS).delete(pin["id"])
    rec = access.get_message(ctx, channel["id"], ts)
    if rec is not None:
        await events.emit_pin(ctx, False, channel, rec, call.me.uid, pin)
    return {}


@api("pins.list", scopes=("pins:read",))
async def pins_list(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    channel = access.require_readable(ctx, call.me, call.params.str("channel"))
    pins = [p for p in access.col(ctx, access.PINS) if p["channel"] == channel["id"]]
    pins.sort(key=lambda p: (p["created"], ts_key(p["ts"])), reverse=True)
    items = []
    for pin in pins:
        rec = access.get_message(ctx, channel["id"], pin["ts"])
        if rec is None:
            continue
        items.append(
            {
                "type": "message",
                "created": pin["created"],
                "created_by": pin["created_by"],
                "channel": channel["id"],
                "message": message_json(ctx, call.request, call.me, rec),
            }
        )
    return {"items": items}


# -- bookmarks ---------------------------------------------------------------------------------


def _bookmark_channel(call: Call) -> dict[str, Any]:
    channel = access.require_channel(call.ctx, call.me, call.params.str("channel_id"))
    if not access.is_member(channel, call.me.uid):
        raise SlackError("not_in_channel")
    return channel


def _bookmark_out(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if not k.startswith("_")}


@api("bookmarks.list", scopes=("bookmarks:read",))
async def bookmarks_list(call: Call) -> dict[str, Any]:
    channel = _bookmark_channel(call)
    found = [b for b in access.col(call.ctx, access.BOOKMARKS) if b["channel_id"] == channel["id"]]
    found.sort(key=lambda b: b["rank"])
    return {"bookmarks": [_bookmark_out(b) for b in found]}


@api("bookmarks.add", scopes=("bookmarks:write",))
async def bookmarks_add(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    channel = _bookmark_channel(call)
    title = p.need("title")
    kind = p.need("type")
    if kind != "link":
        raise SlackError("invalid_type")
    link = p.str("link")
    if not link or not re.match(r"^https?://", link):
        raise SlackError("invalid_link")
    existing = [b for b in access.col(ctx, access.BOOKMARKS) if b["channel_id"] == channel["id"]]
    if len(existing) >= MAX_BOOKMARKS:
        raise SlackError("too_many_bookmarks")
    now = int(ctx.clock.now().timestamp())
    record = {
        "id": new_id(ctx, "Bk", access.BOOKMARKS, 9),
        "channel_id": channel["id"],
        "title": title,
        "link": link,
        "emoji": p.str("emoji") or "",
        "icon_url": "",
        "type": "link",
        "entity_id": p.str("entity_id") or "",
        "date_created": now,
        "date_updated": now,
        "rank": f"{len(existing):02d}" if len(existing) < 100 else str(len(existing)),
        "last_updated_by_user_id": call.me.uid,
        "last_updated_by_team_id": access.team_id(ctx),
        "shortcut_id": "",
        "app_id": call.me.app_id or "",
    }
    access.col(ctx, access.BOOKMARKS).put(record)
    return {"bookmark": _bookmark_out(record)}


def _bookmark(call: Call, channel: dict[str, Any]) -> dict[str, Any]:
    record = access.col(call.ctx, access.BOOKMARKS).get(call.params.need("bookmark_id"))
    if record is None or record["channel_id"] != channel["id"]:
        raise SlackError("bookmark_not_found")
    return record


@api("bookmarks.edit", scopes=("bookmarks:write",))
async def bookmarks_edit(call: Call) -> dict[str, Any]:
    p = call.params
    channel = _bookmark_channel(call)
    record = _bookmark(call, channel)
    if p.str("title"):
        record["title"] = p.str("title")
    if p.str("link"):
        if not re.match(r"^https?://", p.str("link") or ""):
            raise SlackError("invalid_link")
        record["link"] = p.str("link")
    if p.has("emoji"):
        record["emoji"] = str(p.raw("emoji"))
    record["date_updated"] = int(call.ctx.clock.now().timestamp())
    record["last_updated_by_user_id"] = call.me.uid
    return {"bookmark": _bookmark_out(record)}


@api("bookmarks.remove", scopes=("bookmarks:write",))
async def bookmarks_remove(call: Call) -> dict[str, Any]:
    channel = _bookmark_channel(call)
    record = _bookmark(call, channel)
    access.col(call.ctx, access.BOOKMARKS).delete(record["id"])
    return {}
