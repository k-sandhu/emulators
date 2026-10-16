"""Events API: signed ``event_callback`` deliveries, URL verification and retries.

The app configuration (request URL, signing secret, subscribed events) lives in the
``app_config`` record and is set through the ``configure_events`` action. Delivery follows
Slack: bot-scoped events (messages, reactions, joins...) are only sent for conversations the
app's bot user belongs to, workspace events (``channel_created``, ``team_join``...) always,
and the app never receives the messages its own bot user posted.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs

from emulators.core.provider import ProviderContext
from emulators.core.webhooks import hmac_sha256_hex
from emulators.providers.slack import access
from emulators.providers.slack.shapes import file_json
from emulators.providers.slack.util import clean, ts_from_datetime

USER_AGENT = "Slackbot 1.0 (+https://api.slack.com/robots)"
MESSAGE_SUBSCRIPTIONS = {
    "public": "message.channels",
    "private": "message.groups",
    "im": "message.im",
    "mpim": "message.mpim",
}
CHANNEL_TYPE = {"public": "channel", "private": "group", "im": "im", "mpim": "mpim"}
CHANNEL_LETTER = {"public": "C", "private": "G"}
#: Bare names accepted in ``events`` that expand to several subscriptions.
ALIASES = {"message": list(MESSAGE_SUBSCRIPTIONS.values())}


def subscribed(config: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for name in config.get("events", []):
        names.update(ALIASES.get(name, [name]))
    return names


def signature(secret: str, timestamp: int | str, body: bytes) -> str:
    return "v0=" + hmac_sha256_hex(secret, f"v0:{timestamp}:{body.decode()}")


def _context(event_type: str, team: str, app_id: str, channel_id: str | None) -> str:
    raw = {"et": event_type, "tid": team, "aid": app_id}
    if channel_id:
        raw["cid"] = channel_id
    encoded = base64.urlsafe_b64encode(json.dumps(raw, separators=(",", ":")).encode()).rstrip(b"=")
    return "4-" + encoded.decode()


def _retry_reason(ctx: ProviderContext, event_id: str) -> str:
    """Slack names why the previous attempt failed (``http_error``, ``connection_failed``...)."""
    for record in reversed(ctx.webhooks.deliveries.values()):
        if record.get("metadata", {}).get("event_id") == event_id:
            error = record.get("error") or ""
            if error.startswith("HTTP"):
                return "http_error"
            if "Timeout" in error:
                return "http_timeout"
            return "connection_failed"
    return "http_error"


async def deliver_payload(
    ctx: ProviderContext, config: dict[str, Any], payload: dict[str, Any], *, event_type: str
) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    secret = config["signing_secret"]
    event_id = payload.get("event_id", "")

    def sign(raw: bytes, attempt: int) -> dict[str, str]:
        stamp = int(ctx.clock.now().timestamp())
        headers = {
            "X-Slack-Signature": signature(secret, stamp, raw),
            "X-Slack-Request-Timestamp": str(stamp),
        }
        if attempt > 1:
            headers["X-Slack-Retry-Num"] = str(attempt - 1)
            headers["X-Slack-Retry-Reason"] = _retry_reason(ctx, event_id)
        return headers

    return await ctx.webhooks.deliver(
        config["request_url"],
        body,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip,deflate"},
        event_type=event_type,
        metadata={"event_id": event_id, "team_id": access.team_id(ctx)},
        sign=sign,
    )


def envelope(
    ctx: ProviderContext,
    config: dict[str, Any],
    event: dict[str, Any],
    *,
    channel: dict[str, Any] | None = None,
) -> dict[str, Any]:
    team = access.team_id(ctx)
    return {
        "token": config["verification_token"],
        "team_id": team,
        "api_app_id": config["app_id"],
        "event": event,
        "type": "event_callback",
        "event_id": "Ev" + ctx.ids.base62(10).upper(),
        "event_time": int(ctx.clock.now().timestamp()),
        "authorizations": [
            {
                "enterprise_id": None,
                "team_id": team,
                "user_id": config["bot_user_id"],
                "is_bot": True,
                "is_enterprise_install": False,
            }
        ],
        "is_ext_shared_channel": bool(channel and channel.get("is_ext_shared")),
        "event_context": _context(event["type"], team, config["app_id"], channel["id"] if channel else None),
    }


async def emit(
    ctx: ProviderContext,
    event: dict[str, Any],
    *,
    subscription: str | None = None,
    channel: dict[str, Any] | None = None,
    needs_membership: bool = True,
) -> dict[str, Any] | None:
    """Deliver ``event`` if the app subscribed to it. Returns the delivery record (or ``None``)."""
    config = access.app_config(ctx)
    if not config.get("request_url"):
        return None
    if (subscription or event["type"]) not in subscribed(config):
        return None
    if channel is not None and needs_membership and not access.is_member(channel, config["bot_user_id"]):
        return None
    payload = envelope(ctx, config, event, channel=channel)
    return await deliver_payload(ctx, config, payload, event_type=event["type"])


def _event_ts(ctx: ProviderContext) -> str:
    return ts_from_datetime(ctx.clock.now())


def message_event_body(ctx: ProviderContext, channel: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    """A stored message as the ``event`` object of a ``message`` event."""
    event = clean(rec, keep_id=False)
    if rec.get("_files"):
        event["files"] = [
            file_json(ctx, None, f)
            for fid in rec["_files"]
            if (f := ctx.store.collection(access.FILES).get(fid)) and not f.get("_deleted")
        ]
        event["upload"] = False
        event["display_as_bot"] = False
    event["channel"] = channel["id"]
    event["event_ts"] = rec["ts"]
    event["channel_type"] = CHANNEL_TYPE[access.channel_kind(channel)]
    return event


async def emit_message(ctx: ProviderContext, channel: dict[str, Any], rec: dict[str, Any]) -> None:
    config = access.app_config(ctx)
    if not config.get("request_url"):
        return
    kind = access.channel_kind(channel)
    if not _own(config, rec):
        await emit(
            ctx,
            {"type": "message", **message_event_body(ctx, channel, rec)},
            subscription=MESSAGE_SUBSCRIPTIONS[kind],
            channel=channel,
        )
        if f"<@{config['bot_user_id']}>" in rec.get("text", "") and not rec.get("subtype"):
            mention = {
                "type": "app_mention",
                "user": rec.get("user"),
                "text": rec["text"],
                "ts": rec["ts"],
                "channel": channel["id"],
                "event_ts": rec["ts"],
                "team": access.team_id(ctx),
            }
            for key in ("client_msg_id", "thread_ts", "blocks"):
                if rec.get(key):
                    mention[key] = rec[key]
            await emit(ctx, mention, channel=channel)


def _own(config: dict[str, Any], rec: dict[str, Any]) -> bool:
    """Messages authored by the app's own bot user are never sent back to the app."""
    return rec.get("user") == config["bot_user_id"] or (rec.get("bot_id") or "") == config["bot_id"]


async def emit_message_changed(
    ctx: ProviderContext, channel: dict[str, Any], new: dict[str, Any], previous: dict[str, Any]
) -> None:
    if _own(access.app_config(ctx), new):
        return
    event = {
        "type": "message",
        "subtype": "message_changed",
        "message": clean(new, keep_id=False),
        "previous_message": clean(previous, keep_id=False),
        "channel": channel["id"],
        "hidden": True,
        "ts": _event_ts(ctx),
        "event_ts": _event_ts(ctx),
        "channel_type": CHANNEL_TYPE[access.channel_kind(channel)],
    }
    await emit(ctx, event, subscription=MESSAGE_SUBSCRIPTIONS[access.channel_kind(channel)], channel=channel)


async def emit_message_deleted(
    ctx: ProviderContext, channel: dict[str, Any], previous: dict[str, Any]
) -> None:
    if _own(access.app_config(ctx), previous):
        return
    stamp = _event_ts(ctx)
    event = {
        "type": "message",
        "subtype": "message_deleted",
        "previous_message": clean(previous, keep_id=False),
        "channel": channel["id"],
        "hidden": True,
        "deleted_ts": previous["ts"],
        "event_ts": stamp,
        "ts": stamp,
        "channel_type": CHANNEL_TYPE[access.channel_kind(channel)],
    }
    await emit(ctx, event, subscription=MESSAGE_SUBSCRIPTIONS[access.channel_kind(channel)], channel=channel)


async def emit_membership(
    ctx: ProviderContext, joined: bool, channel: dict[str, Any], uid: str, inviter: str | None = None
) -> None:
    kind = access.channel_kind(channel)
    if kind not in CHANNEL_LETTER:
        return
    event: dict[str, Any] = {
        "type": "member_joined_channel" if joined else "member_left_channel",
        "user": uid,
        "channel": channel["id"],
        "channel_type": CHANNEL_LETTER[kind],
        "team": access.team_id(ctx),
        "event_ts": _event_ts(ctx),
    }
    if joined and inviter:
        event["inviter"] = inviter
    await emit(ctx, event, channel=channel)


async def emit_reaction(
    ctx: ProviderContext, added: bool, channel: dict[str, Any], rec: dict[str, Any], uid: str, name: str
) -> None:
    event = {
        "type": "reaction_added" if added else "reaction_removed",
        "user": uid,
        "reaction": name,
        "item": {"type": "message", "channel": channel["id"], "ts": rec["ts"]},
        "item_user": rec.get("user") or "",
        "event_ts": _event_ts(ctx),
    }
    await emit(ctx, event, channel=channel)


async def emit_file_shared(ctx: ProviderContext, channel: dict[str, Any], file_id: str, uid: str) -> None:
    event = {
        "type": "file_shared",
        "channel_id": channel["id"],
        "file_id": file_id,
        "user_id": uid,
        "file": {"id": file_id},
        "event_ts": _event_ts(ctx),
    }
    await emit(ctx, event, channel=channel)


async def emit_channel_event(
    ctx: ProviderContext, event_type: str, channel: dict[str, Any], uid: str | None = None
) -> None:
    """``channel_created``/``rename``/``archive``/``unarchive``/``deleted`` (public channels, workspace-wide)."""
    if access.channel_kind(channel) != "public":
        return
    if event_type in ("channel_created", "channel_rename"):
        body: Any = {
            "id": channel["id"],
            "is_channel": True,
            "name": channel["name"],
            "name_normalized": channel["name_normalized"],
            "created": channel["created"],
        }
        if event_type == "channel_created":
            body.update(
                {"creator": channel["creator"], "is_shared": channel["is_shared"], "is_org_shared": False}
            )
        event = {"type": event_type, "channel": body, "event_ts": _event_ts(ctx)}
    else:
        event = {"type": event_type, "channel": channel["id"], "event_ts": _event_ts(ctx)}
        if uid:
            event["user"] = uid
    await emit(ctx, event, channel=channel, needs_membership=False)


async def emit_pin(
    ctx: ProviderContext,
    added: bool,
    channel: dict[str, Any],
    rec: dict[str, Any],
    uid: str,
    pin: dict[str, Any],
) -> None:
    pins = ctx.store.collection(access.PINS).filter(lambda p: p["channel"] == channel["id"])
    event = {
        "type": "pin_added" if added else "pin_removed",
        "user": uid,
        "channel_id": channel["id"],
        "item": {
            "type": "message",
            "created": pin["created"],
            "created_by": pin["created_by"],
            "channel": channel["id"],
            "message": clean(rec, keep_id=False),
        },
        "item_user": rec.get("user") or "",
        "pin_count": len(pins),
        "event_ts": _event_ts(ctx),
    }
    if added:
        event["pinned_info"] = {"channel": channel["id"], "pinned_by": uid, "pinned_ts": pin["created"]}
    await emit(ctx, event, channel=channel)


async def emit_workspace(ctx: ProviderContext, event: dict[str, Any]) -> None:
    """Events with no conversation (``team_join``, ``user_change``...)."""
    await emit(ctx, event)


# -- URL verification --------------------------------------------------------------------------


async def verify_request_url(
    ctx: ProviderContext, url: str, secret: str, token: str, challenge: str
) -> tuple[bool, str]:
    """Send Slack's ``url_verification`` request; the endpoint must echo the challenge."""
    payload = json.dumps({"token": token, "challenge": challenge, "type": "url_verification"}).encode()
    stamp = int(ctx.clock.now().timestamp())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Slack-Signature": signature(secret, stamp, payload),
        "X-Slack-Request-Timestamp": str(stamp),
    }
    try:
        response = await ctx.webhooks.request("POST", url, headers=headers, content=payload)
    except Exception as exc:
        return False, f"could not reach the request URL: {type(exc).__name__}: {exc}"
    if response.status_code != 200:
        return False, f"request URL answered HTTP {response.status_code}"
    text = response.text.strip()
    if text == challenge:
        return True, ""
    try:
        if isinstance(parsed := response.json(), dict) and parsed.get("challenge") == challenge:
            return True, ""
    except ValueError:
        pass
    if parse_qs(text).get("challenge", [None])[0] == challenge:
        return True, ""
    return False, "the request URL did not echo the challenge"
