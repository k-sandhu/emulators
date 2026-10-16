"""Reads, lookups and visibility rules over the Slack store.

The store holds records in (nearly) Slack's wire shape; keys starting with ``_`` are
emulator-internal (membership lists, blob keys...) and never leave the process.
"""

from __future__ import annotations

from typing import Any

from emulators.core.provider import ProviderContext
from emulators.core.store import Collection
from emulators.providers.slack.runtime import Actor, SlackError
from emulators.providers.slack.util import bump_ts, ts_from_datetime, ts_key

USERS = "users"
CHANNELS = "channels"
MESSAGES = "messages"
FILES = "files"
PINS = "pins"
BOOKMARKS = "bookmarks"
USERGROUPS = "usergroups"
SCHEDULED = "scheduled_messages"
WEBHOOKS = "incoming_webhooks"
UPLOADS = "pending_uploads"
EPHEMERAL = "ephemeral_messages"
EMOJI = "emoji"
APP = "app_config"
RESPONSE_URLS = "response_urls"
SLACKBOT = "USLACKBOT"


def col(ctx: ProviderContext, name: str) -> Collection:
    return ctx.store.collection(name)


def team_id(ctx: ProviderContext) -> str:
    return ctx.world.org.slack_team_id


def app_config(ctx: ProviderContext) -> dict[str, Any]:
    record = col(ctx, APP).get("app")
    assert record is not None, "slack app configuration is seeded"
    return record


def bot_user(ctx: ProviderContext) -> dict[str, Any]:
    user = col(ctx, USERS).get(app_config(ctx)["bot_user_id"])
    assert user is not None
    return user


# -- users -------------------------------------------------------------------------------------


def user_by_id(ctx: ProviderContext, uid: str | None) -> dict[str, Any] | None:
    return col(ctx, USERS).get(uid) if uid else None


def user_by_person(ctx: ProviderContext, person_id: str) -> dict[str, Any] | None:
    return col(ctx, USERS).first(_person=person_id)


def user_by_email(ctx: ProviderContext, email: str) -> dict[str, Any] | None:
    wanted = email.strip().lower()
    for user in col(ctx, USERS):
        if (user.get("profile", {}).get("email") or "").lower() == wanted:
            return user
    return None


def resolve_user_ref(ctx: ProviderContext, ref: str | None) -> dict[str, Any] | None:
    """A Slack id, world person id (``p018``) or e-mail address -> user record."""
    if not ref:
        return None
    return (
        user_by_id(ctx, ref) or user_by_person(ctx, ref) or (user_by_email(ctx, ref) if "@" in ref else None)
    )


def actor_user(ctx: ProviderContext, actor: Actor) -> dict[str, Any]:
    user = user_by_id(ctx, actor.uid)
    if user is None:
        raise SlackError("invalid_auth")
    return user


def is_guest(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("is_restricted"))


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user and (user.get("is_admin") or user.get("is_owner")))


# -- channels ----------------------------------------------------------------------------------


def channel_kind(channel: dict[str, Any]) -> str:
    if channel.get("is_im"):
        return "im"
    if channel.get("is_mpim"):
        return "mpim"
    return "private" if channel.get("is_private") else "public"


def channel_by_id(ctx: ProviderContext, channel_id: str | None) -> dict[str, Any] | None:
    return col(ctx, CHANNELS).get(channel_id) if channel_id else None


def channel_by_name(ctx: ProviderContext, name: str) -> dict[str, Any] | None:
    wanted = name.lstrip("#").lower()
    for channel in col(ctx, CHANNELS):
        if channel.get("is_im") or channel.get("is_mpim"):
            continue
        if channel.get("name_normalized") == wanted or wanted in channel.get("previous_names", []):
            return channel
    return None


def is_member(channel: dict[str, Any], uid: str) -> bool:
    return uid in channel["_members"]


def add_member(channel: dict[str, Any], uid: str) -> bool:
    if uid in channel["_members"]:
        return False
    channel["_members"].append(uid)
    return True


def remove_member(channel: dict[str, Any], uid: str) -> bool:
    if uid not in channel["_members"]:
        return False
    channel["_members"].remove(uid)
    return True


def can_view(ctx: ProviderContext, actor: Actor, channel: dict[str, Any]) -> bool:
    """Whether the caller may know the conversation exists (private ones only to members)."""
    if is_member(channel, actor.uid):
        return True
    return channel_kind(channel) == "public" and not is_guest(user_by_id(ctx, actor.uid))


def require_channel(ctx: ProviderContext, actor: Actor, channel_id: str | None) -> dict[str, Any]:
    """The conversation, or ``channel_not_found`` (also for private ones the caller cannot see)."""
    channel = channel_by_id(ctx, channel_id)
    if channel is None or not can_view(ctx, actor, channel):
        raise SlackError("channel_not_found")
    return channel


def require_readable(ctx: ProviderContext, actor: Actor, channel_id: str | None) -> dict[str, Any]:
    """History access: bots must be members; users may read public channels they have not joined."""
    channel = require_channel(ctx, actor, channel_id)
    if not is_member(channel, actor.uid) and (actor.is_bot or channel_kind(channel) != "public"):
        raise SlackError("not_in_channel")
    return channel


def conversation_scope(channel: dict[str, Any], action: str) -> tuple[str, ...]:
    """Scopes (any one suffices) that unlock ``read``/``history``/``write`` on this conversation type."""
    family = {"public": "channels", "private": "groups", "im": "im", "mpim": "mpim"}[channel_kind(channel)]
    if action == "write":
        if family == "channels":
            return ("channels:manage", "channels:write")
        if family == "groups":
            return ("groups:write",)
        return (f"{family}:write",)
    return (f"{family}:{action}",)


def find_im(ctx: ProviderContext, a: str, b: str) -> dict[str, Any] | None:
    wanted = {a, b}
    for channel in col(ctx, CHANNELS):
        if channel.get("is_im") and set(channel["_members"]) == wanted:
            return channel
    return None


def find_mpim(ctx: ProviderContext, uids: set[str]) -> dict[str, Any] | None:
    for channel in col(ctx, CHANNELS):
        if channel.get("is_mpim") and set(channel["_members"]) == uids:
            return channel
    return None


# -- messages ----------------------------------------------------------------------------------


def message_key(channel_id: str, ts: str) -> str:
    return f"{channel_id}:{ts}"


def get_message(ctx: ProviderContext, channel_id: str, ts: str | None) -> dict[str, Any] | None:
    return col(ctx, MESSAGES).get(message_key(channel_id, ts)) if ts else None


def channel_messages(ctx: ProviderContext, channel_id: str) -> list[dict[str, Any]]:
    """All stored messages of a conversation, oldest first."""
    found = col(ctx, MESSAGES).filter(lambda m: m["_channel"] == channel_id)
    found.sort(key=lambda m: ts_key(m["ts"]))
    return found


def thread_replies(ctx: ProviderContext, channel_id: str, parent_ts: str) -> list[dict[str, Any]]:
    replies = col(ctx, MESSAGES).filter(
        lambda m: m["_channel"] == channel_id and m.get("thread_ts") == parent_ts and m["ts"] != parent_ts
    )
    replies.sort(key=lambda m: ts_key(m["ts"]))
    return replies


def new_ts(ctx: ProviderContext, channel: dict[str, Any] | None = None) -> str:
    """A fresh message ``ts`` from the emulator clock, unique and increasing per conversation."""
    candidate = ts_from_datetime(ctx.clock.now())
    if channel is not None:
        channel["_generated_at"] = candidate
        last = channel.get("_last_ts") or "0.000000"
        if ts_key(candidate) <= ts_key(last):
            candidate = bump_ts(last, 100)
        channel["_last_ts"] = candidate
    return candidate
