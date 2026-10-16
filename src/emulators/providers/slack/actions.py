"""``POST /_emulator/actions/*``: simulate humans and configure the app for Events API tests."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.providers.slack import access, events, messages
from emulators.providers.slack.incoming import create_webhook
from emulators.providers.slack.m_files import new_file_record
from emulators.providers.slack.m_oauth import new_bot_token, new_user_token
from emulators.providers.slack.records import make_channel, new_id
from emulators.providers.slack.runtime import APP, BOT, USER
from emulators.providers.slack.seed_users import (
    APP_SCOPES,
    BOT_SCOPES_FULL,
    USER_SCOPES,
    seeded_tokens,
    user_record,
)
from emulators.providers.slack.shapes import message_json, user_json
from emulators.providers.slack.util import slugify

Action = Callable[[ProviderContext, dict[str, Any]], Awaitable[Any]]
DEFAULT_EVENTS = [
    "message.channels",
    "message.groups",
    "message.im",
    "app_mention",
    "member_joined_channel",
    "reaction_added",
    "channel_created",
    "file_shared",
    "team_join",
]


def _fail(status: int, error: str, **extra: Any) -> EmulatorError:
    return EmulatorError(status, {"ok": False, "error": error, **extra})


def _user(ctx: ProviderContext, ref: Any) -> dict[str, Any]:
    user = access.resolve_user_ref(ctx, str(ref) if ref else None)
    if user is None:
        raise _fail(404, "user_not_found", user=ref)
    return user


def _channel(ctx: ProviderContext, ref: Any) -> dict[str, Any]:
    text = str(ref or "")
    channel = access.channel_by_id(ctx, text) or access.channel_by_name(ctx, text)
    if channel is None:
        raise _fail(404, "channel_not_found", channel=ref)
    return channel


def _scopes(raw: Any, default: list[str]) -> list[str]:
    if raw is None or raw in ([], ""):
        return list(default)
    items = raw.replace(" ", ",").split(",") if isinstance(raw, str) else list(raw)
    items = [i for i in items if i]
    return list(default) if "*" in items else items


async def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Mint a Slack token: ``{"user": "<id|email|U...>", "type": "bot|user|app", "scopes": [...]}``.

    Without ``user`` a bot token for the app is minted. ``scopes`` empty or ``["*"]`` grants the
    full default scope set; ``expires_in`` (seconds) makes the token expire (rotation tests).
    """
    config = access.app_config(ctx)
    kind = payload.get("type") or (USER if payload.get("user") else BOT)
    if kind not in (BOT, USER, APP):
        raise _fail(400, "invalid_type")
    ttl = timedelta(seconds=int(payload["expires_in"])) if payload.get("expires_in") else None
    if kind == USER:
        user = _user(ctx, payload.get("user"))
        uid, scopes, token = user["id"], _scopes(payload.get("scopes"), USER_SCOPES), new_user_token(ctx)
    elif kind == BOT:
        uid = config["bot_user_id"]
        scopes, token = _scopes(payload.get("scopes"), BOT_SCOPES_FULL), new_bot_token(ctx)
    else:
        uid, scopes = "", _scopes(payload.get("scopes"), APP_SCOPES)
        token = f"xapp-1-{config['app_id']}-{ctx.ids.digits(13)}-{ctx.ids.hex(40)}"
    ctx.tokens.issue_access(
        token=token,
        client_id=payload.get("client_id") or config["client_id"],
        user_id=uid if kind == USER else None,
        scopes=scopes,
        ttl=ttl,
        app_only=kind != USER,
        extra={"kind": kind, "slack_user": uid, "bot_id": config["bot_id"], "app_id": config["app_id"]},
    )
    body: dict[str, Any] = {
        "access_token": token,
        "token": token,
        "token_type": "Bearer",
        "scope": ",".join(scopes),
        "team_id": access.team_id(ctx),
        "user_id": uid,
    }
    if kind == BOT:
        body["bot_id"] = config["bot_id"]
    if ttl:
        body["expires_in"] = int(ttl.total_seconds())
    return body


async def configure_events(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Point the app at an Events API endpoint: ``{request_url, signing_secret?, events?}``.

    Sends Slack's ``url_verification`` challenge first and only saves the configuration if the
    endpoint echoes it (``skip_verification: true`` bypasses the handshake).
    """
    config = access.app_config(ctx)
    url = payload.get("request_url")
    if not url:
        raise _fail(400, "invalid_arguments", detail="request_url is required")
    secret = payload.get("signing_secret") or config["signing_secret"]
    token = payload.get("verification_token") or config["verification_token"]
    subscriptions = list(payload.get("events") or DEFAULT_EVENTS)
    verified = False
    if not payload.get("skip_verification"):
        challenge = "ch-" + ctx.ids.base62(32)
        ok, reason = await events.verify_request_url(ctx, url, secret, token, challenge)
        if not ok:
            raise _fail(400, "url_verification_failed", detail=reason, request_url=url)
        verified = True
    config.update(
        request_url=url,
        signing_secret=secret,
        verification_token=token,
        events=subscriptions,
        verified=verified,
    )
    return {
        "ok": True,
        "request_url": url,
        "events": subscriptions,
        "signing_secret": secret,
        "verification_token": token,
        "app_id": config["app_id"],
        "bot_user_id": config["bot_user_id"],
        "verified": verified,
    }


async def disable_events(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Stop delivering events (clears the request URL and subscriptions)."""
    config = access.app_config(ctx)
    config.update(request_url=None, events=[], verified=False)
    return {"ok": True}


async def configure_app(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Change app settings: ``{token_rotation?, client_id?, client_secret?, redirect_uris?}``."""
    config = access.app_config(ctx)
    if "token_rotation" in payload:
        config["token_rotation"] = bool(payload["token_rotation"])
    if payload.get("client_id") or payload.get("client_secret"):
        config["client_id"] = payload.get("client_id") or config["client_id"]
        config["client_secret"] = payload.get("client_secret") or config["client_secret"]
        ctx.tokens.register_client(
            config["client_id"], config["client_secret"], redirect_uris=payload.get("redirect_uris") or []
        )
    if "redirect_uris" in payload:
        config["redirect_uris"] = list(payload["redirect_uris"])
    return {
        key: config[key]
        for key in ("app_id", "client_id", "client_secret", "token_rotation", "redirect_uris")
    }


async def post_as_user(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Simulate a human posting: ``{user, channel, text, thread_ts?, blocks?, reply_broadcast?}``."""
    user = _user(ctx, payload.get("user"))
    channel = _channel(ctx, payload.get("channel"))
    if user.get("deleted"):
        raise _fail(400, "account_inactive")
    access.add_member(channel, user["id"]) if access.channel_kind(channel) == "public" else None
    if not access.is_member(channel, user["id"]):
        raise _fail(403, "not_in_channel", detail="add the user to the private conversation first")
    rec = await messages.create_message(
        ctx,
        channel,
        user=user["id"],
        text=payload.get("text", ""),
        blocks=payload.get("blocks"),
        attachments=payload.get("attachments"),
        thread_ts=payload.get("thread_ts"),
        broadcast=bool(payload.get("reply_broadcast")),
        extra={"client_msg_id": str(uuid.UUID(ctx.ids.hex(32)))},
    )
    return {
        "ok": True,
        "channel": channel["id"],
        "ts": rec["ts"],
        "message": message_json(ctx, None, None, rec),
    }


async def _react(ctx: ProviderContext, payload: dict[str, Any], *, add: bool) -> dict[str, Any]:
    user = _user(ctx, payload.get("user"))
    channel = _channel(ctx, payload.get("channel"))
    rec = access.get_message(ctx, channel["id"], payload.get("ts") or payload.get("timestamp"))
    if rec is None:
        raise _fail(404, "message_not_found")
    name = str(payload.get("name") or "").strip(":")
    reactions: list[dict[str, Any]] = rec.setdefault("reactions", [])
    existing = next((r for r in reactions if r["name"] == name), None)
    if add:
        if existing is None:
            reactions.append({"name": name, "users": [user["id"]], "count": 1})
        elif user["id"] not in existing["users"]:
            existing["users"].append(user["id"])
            existing["count"] = len(existing["users"])
    elif existing and user["id"] in existing["users"]:
        existing["users"].remove(user["id"])
        existing["count"] = len(existing["users"])
        if not existing["users"]:
            reactions.remove(existing)
    await events.emit_reaction(ctx, add, channel, rec, user["id"], name)
    return {"ok": True}


async def add_reaction(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A user reacts: ``{user, channel, ts, name}`` (fires ``reaction_added``)."""
    return await _react(ctx, payload, add=True)


async def remove_reaction(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A user removes a reaction: ``{user, channel, ts, name}`` (fires ``reaction_removed``)."""
    return await _react(ctx, payload, add=False)


async def user_join(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A user joins a conversation: ``{user, channel, inviter?}`` (``member_joined_channel``)."""
    user = _user(ctx, payload.get("user"))
    channel = _channel(ctx, payload.get("channel"))
    inviter = _user(ctx, payload["inviter"])["id"] if payload.get("inviter") else None
    joined = await messages.join_channel(ctx, channel, user["id"], inviter)
    return {"ok": True, "joined": joined, "channel": channel["id"], "user": user["id"]}


async def user_leave(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A user leaves a conversation: ``{user, channel}`` (``member_left_channel``)."""
    user = _user(ctx, payload.get("user"))
    channel = _channel(ctx, payload.get("channel"))
    left = await messages.leave_channel(ctx, channel, user["id"])
    return {"ok": True, "left": left, "channel": channel["id"], "user": user["id"]}


async def team_join(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A new member joins the workspace: ``{first_name, last_name, email?, title?}`` (``team_join``)."""
    first, last = payload.get("first_name", "New"), payload.get("last_name", "Member")
    handle = payload.get("name") or slugify(f"{first}.{last}").replace("-", ".")
    email = payload.get("email") or f"{handle}@{ctx.world.org.domain}"
    if access.user_by_email(ctx, email) or any(u["name"] == handle for u in access.col(ctx, access.USERS)):
        raise _fail(409, "user_already_exists")
    uid = new_id(ctx, "U", access.USERS)
    record = user_record(
        ctx,
        uid,
        name=handle,
        first=first,
        last=last,
        email=email,
        title=payload.get("title", ""),
        tz=payload.get("tz", "America/Toronto"),
        updated=int(ctx.clock.now().timestamp()),
    )
    access.col(ctx, access.USERS).put(record)
    general = next((c for c in access.col(ctx, access.CHANNELS) if c.get("is_general")), None)
    await events.emit_workspace(
        ctx, {"type": "team_join", "user": user_json(ctx, record), "event_ts": str(ctx.clock.timestamp())}
    )
    if general is not None:
        await messages.join_channel(ctx, general, uid)
    return {"ok": True, "user": user_json(ctx, record)}


async def create_channel(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a channel without API permissions: ``{name, is_private?, creator?, members?}``."""
    name = str(payload.get("name") or "").lstrip("#").lower()
    if not name or access.channel_by_name(ctx, name):
        raise _fail(400, "name_taken" if name else "invalid_name_required")
    creator = (
        _user(ctx, payload["creator"])["id"]
        if payload.get("creator")
        else access.app_config(ctx)["bot_user_id"]
    )
    private = bool(payload.get("is_private"))
    members = [creator, *[_user(ctx, m)["id"] for m in payload.get("members", [])]]
    record = make_channel(
        ctx,
        channel_id=new_id(ctx, "G" if private else "C", access.CHANNELS),
        name=name,
        creator=creator,
        created=int(ctx.clock.now().timestamp()),
        members=members,
        is_private=private,
    )
    access.col(ctx, access.CHANNELS).put(record)
    await events.emit_channel_event(ctx, "channel_created", record)
    return {"ok": True, "channel": record["id"], "name": name}


async def upload_file(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A user uploads a file to a channel: ``{user, channel, filename, content|content_base64, title?, comment?}``."""
    user = _user(ctx, payload.get("user"))
    channel = _channel(ctx, payload.get("channel"))
    if payload.get("content_base64"):
        data = base64.b64decode(payload["content_base64"])
    else:
        data = str(payload.get("content", "")).encode()
    name = payload.get("filename") or "upload.txt"
    record = new_file_record(
        ctx, name=name, title=payload.get("title") or name, data=data, uploader=user["id"]
    )
    access.add_member(channel, user["id"]) if access.channel_kind(channel) == "public" else None
    rec = await messages.create_message(
        ctx,
        channel,
        user=user["id"],
        text=payload.get("comment", ""),
        subtype="file_share",
        files=[record["id"]],
        extra={"upload": True, "display_as_bot": False},
    )
    await events.emit_file_shared(ctx, channel, record["id"], user["id"])
    return {"ok": True, "file": record["id"], "channel": channel["id"], "ts": rec["ts"]}


async def flush_scheduled(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Post scheduled messages that are due (also happens before every API call)."""
    before = len(access.col(ctx, access.SCHEDULED))
    await messages.process_scheduled(ctx)
    return {"ok": True, "posted": before - len(access.col(ctx, access.SCHEDULED))}


async def create_webhook_action(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Create an incoming webhook for a channel: ``{channel, label?}`` -> its ``hooks.slack.com`` URL."""
    channel = _channel(ctx, payload.get("channel"))
    hook = create_webhook(
        ctx,
        channel["id"],
        created_by=access.app_config(ctx)["bot_user_id"],
        created=int(ctx.clock.now().timestamp()),
        label=payload.get("label") or f"Webhook for #{channel['name']}",
        seed=ctx.ids.hex(8),
    )
    return {"ok": True, "url": f"https://hooks.slack.com/services/{hook['id']}", "channel": channel["id"]}


async def set_presence(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Set a user's presence: ``{user, presence: active|away}``."""
    user = _user(ctx, payload.get("user"))
    presence = payload.get("presence", "active")
    if presence not in ("active", "away"):
        raise _fail(400, "invalid_presence")
    user["_presence"] = presence
    return {"ok": True, "user": user["id"], "presence": presence}


async def tokens(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """List the seeded, documented tokens."""
    return {"ok": True, "tokens": seeded_tokens(ctx)}


ACTIONS: dict[str, Action] = {
    "mint_token": mint_token,
    "configure_events": configure_events,
    "disable_events": disable_events,
    "configure_app": configure_app,
    "post_as_user": post_as_user,
    "add_reaction": add_reaction,
    "remove_reaction": remove_reaction,
    "user_join": user_join,
    "user_leave": user_leave,
    "team_join": team_join,
    "create_channel": create_channel,
    "upload_file": upload_file,
    "flush_scheduled": flush_scheduled,
    "create_webhook": create_webhook_action,
    "set_presence": set_presence,
    "tokens": tokens,
}
