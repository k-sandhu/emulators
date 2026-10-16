"""Store records -> Slack wire objects (users, conversations, messages, files, usergroups)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Request

from emulators.core.provider import ProviderContext, ctx_url_for_host, url_for_host
from emulators.providers.slack import access
from emulators.providers.slack.runtime import Actor
from emulators.providers.slack.util import (
    WORKSPACE_DOMAIN,
    WORKSPACE_URL,
    clean,
    message_permalink,
    stable_id,
)

TZ_LABELS: dict[str, tuple[str, str]] = {
    "America/Toronto": ("Eastern Standard Time", "Eastern Daylight Time"),
    "America/New_York": ("Eastern Standard Time", "Eastern Daylight Time"),
    "America/Chicago": ("Central Standard Time", "Central Daylight Time"),
    "America/Denver": ("Mountain Standard Time", "Mountain Daylight Time"),
    "America/Los_Angeles": ("Pacific Standard Time", "Pacific Daylight Time"),
    "America/Halifax": ("Atlantic Standard Time", "Atlantic Daylight Time"),
    "Europe/London": ("Greenwich Mean Time", "British Summer Time"),
    "UTC": ("Coordinated Universal Time", "Coordinated Universal Time"),
}

FILETYPES: dict[str, tuple[str, str]] = {
    "docx": ("docx", "Word Document"),
    "doc": ("doc", "Word Document"),
    "xlsx": ("xlsx", "Excel Spreadsheet"),
    "xls": ("xls", "Excel Spreadsheet"),
    "pptx": ("pptx", "PowerPoint Presentation"),
    "ppt": ("ppt", "PowerPoint Presentation"),
    "pdf": ("pdf", "PDF"),
    "csv": ("csv", "CSV"),
    "md": ("markdown", "Markdown (raw)"),
    "txt": ("text", "Plain Text"),
    "png": ("png", "PNG"),
    "jpg": ("jpg", "JPEG"),
    "jpeg": ("jpg", "JPEG"),
    "gif": ("gif", "GIF"),
    "zip": ("zip", "Zip"),
    "json": ("json", "JSON"),
    "html": ("html", "HTML"),
    "mp4": ("mp4", "MPEG 4 Video"),
    "m4a": ("m4a", "MPEG 4 Audio"),
    "mp3": ("mp3", "MP3"),
    "wav": ("wav", "WAV"),
}
IMAGE_TYPES = {"png", "jpg", "gif"}


def tz_fields(tz_name: str, at: datetime) -> dict[str, Any]:
    try:
        local = at.astimezone(ZoneInfo(tz_name))
    except Exception:  # unknown zone name: report UTC rather than failing the call
        local = at.astimezone(ZoneInfo("UTC"))
        tz_name = "UTC"
    labels = TZ_LABELS.get(tz_name, (tz_name, tz_name))
    offset = local.utcoffset()
    return {
        "tz": tz_name,
        "tz_label": labels[1 if local.dst() else 0],
        "tz_offset": int(offset.total_seconds()) if offset else 0,
    }


def user_json(
    ctx: ProviderContext, user: dict[str, Any], actor: Actor | None = None, *, locale: bool = False
) -> dict[str, Any]:
    """A ``user`` object. ``profile.email`` needs ``users:read.email``; tz fields track the clock."""
    out = clean(user)
    profile = dict(user["profile"])
    if actor is not None and not actor.has("users:read.email"):
        profile.pop("email", None)
    out["profile"] = profile
    out.update(tz_fields(user.get("tz") or "UTC", ctx.clock.now()))
    if not locale:
        out.pop("locale", None)
    return out


def bot_profile(ctx: ProviderContext, bot_id: str | None) -> dict[str, Any] | None:
    bot = ctx.store.collection("bots").get(bot_id) if bot_id else None
    if bot is None:
        return None
    return {
        "id": bot["id"],
        "deleted": bot.get("deleted", False),
        "name": bot["name"],
        "updated": bot["updated"],
        "app_id": bot["app_id"],
        "icons": bot["icons"],
        "team_id": access.team_id(ctx),
    }


def _channel_base(ctx: ProviderContext, ch: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ch["id"],
        "name": ch["name"],
        "is_channel": ch["is_channel"],
        "is_group": ch["is_group"],
        "is_im": False,
        "is_mpim": ch["is_mpim"],
        "is_private": ch["is_private"],
        "created": ch["created"],
        "is_archived": ch["is_archived"],
        "is_general": ch["is_general"],
        "unlinked": 0,
        "name_normalized": ch["name_normalized"],
        "is_shared": ch["is_shared"],
        "is_org_shared": False,
        "is_pending_ext_shared": False,
        "pending_shared": [],
        "context_team_id": access.team_id(ctx),
        "updated": ch["updated"],
        "parent_conversation": None,
        "creator": ch["creator"],
        "is_ext_shared": ch["is_ext_shared"],
        "shared_team_ids": ch["shared_team_ids"],
        "pending_connected_team_ids": [],
    }


def channel_json(
    ctx: ProviderContext,
    ch: dict[str, Any],
    viewer: str | None,
    *,
    num_members: bool = False,
    member_flag: bool = True,
    last_read: bool = False,
) -> dict[str, Any]:
    """A ``channel`` object as seen by ``viewer`` (affects ``is_member``, ``user`` of an IM...)."""
    if ch.get("is_im"):
        others = [m for m in ch["_members"] if m != viewer] or ch["_members"][:1]
        other = access.user_by_id(ctx, others[0]) if others else None
        out: dict[str, Any] = {
            "id": ch["id"],
            "created": ch["created"],
            "is_archived": False,
            "is_im": True,
            "is_org_shared": False,
            "context_team_id": access.team_id(ctx),
            "updated": ch["updated"],
            "user": others[0] if others else "",
            "is_user_deleted": bool(other and other.get("deleted")),
            "priority": 0,
        }
        if last_read:
            out["last_read"] = ch.get("_marks", {}).get(viewer or "", "0000000000.000000")
        return out
    out = _channel_base(ctx, ch)
    if ch.get("is_shared"):
        out["conversation_host_id"] = ch.get("_host_team") or access.team_id(ctx)
        out["internal_team_ids"] = [access.team_id(ctx)]
        out["connected_team_ids"] = ch["shared_team_ids"]
    if member_flag and viewer is not None:
        out["is_member"] = access.is_member(ch, viewer)
        if last_read and out["is_member"]:
            out["last_read"] = ch.get("_marks", {}).get(viewer, "0000000000.000000")
    out["topic"] = ch["topic"]
    out["purpose"] = ch["purpose"]
    out["previous_names"] = ch["previous_names"]
    if ch.get("is_mpim"):
        out["priority"] = 0
    if num_members:
        out["num_members"] = len(ch["_members"])
    return out


# -- files -------------------------------------------------------------------------------------


def file_url(request: Request | None, ctx: ProviderContext, path: str) -> str:
    """URL on ``files.slack.com``; events (no request in hand) always use the real hostname."""
    if request is None:
        return f"https://files.slack.com{path}"
    return url_for_host(request, ctx, "files.slack.com", path)


def file_json(
    ctx: ProviderContext, request: Request | None, rec: dict[str, Any], actor: Actor | None = None
) -> dict[str, Any]:
    """A ``file`` object. Private URLs point at ``files.slack.com`` and need the Bearer token."""
    team = access.team_id(ctx)
    out = clean(rec)
    name = quote(rec["name"], safe="")
    out["url_private"] = file_url(request, ctx, f"/files-pri/{team}-{rec['id']}/{name}")
    out["url_private_download"] = file_url(request, ctx, f"/files-pri/{team}-{rec['id']}/download/{name}")
    path = f"/files/{rec['user']}/{rec['id']}/{name}"
    out["permalink"] = (
        url_for_host(request, ctx, f"{WORKSPACE_DOMAIN}.slack.com", path)
        if request
        else ctx_url_for_host(ctx, f"{WORKSPACE_DOMAIN}.slack.com", path)
    )
    secret = stable_id("", rec["id"], "pub", length=10).lower()
    out["permalink_public"] = f"https://slack-files.com/{team}-{rec['id']}-{secret}"
    if rec.get("public_url_shared"):
        out["public_url_shared"] = True
    if rec["filetype"] in IMAGE_TYPES:
        base = f"/files-tmb/{team}-{rec['id']}-{secret}/{name.rsplit('.', 1)[0]}"
        for size in (64, 80, 360, 480, 160):
            out[f"thumb_{size}"] = file_url(request, ctx, f"{base}_{size}.{rec['filetype']}")
        out["original_w"], out["original_h"] = 256, 256
    shares: dict[str, dict[str, list[dict[str, Any]]]] = {}
    public: list[str] = []
    private: list[str] = []
    ims: list[str] = []
    for share in rec.get("_shares", []):
        channel = access.channel_by_id(ctx, share["channel"])
        if channel is None:
            continue
        kind = access.channel_kind(channel)
        viewer = actor.uid if actor else access.app_config(ctx)["bot_user_id"]
        if not access.is_member(channel, viewer) and (
            kind != "public" or access.is_guest(access.user_by_id(ctx, viewer))
        ):
            continue
        entry = {
            "reply_users": [],
            "reply_users_count": 0,
            "reply_count": 0,
            "ts": share["ts"],
            "channel_name": channel.get("name") or channel["id"],
            "team_id": team,
            "share_user_id": rec["user"],
            "source": "UNKNOWN",
            "is_silent_share": False,
        }
        if kind == "im":
            ims.append(channel["id"])
        elif kind == "public":
            public.append(channel["id"])
            shares.setdefault("public", {}).setdefault(channel["id"], []).append(entry)
        else:
            private.append(channel["id"])
            shares.setdefault("private", {}).setdefault(channel["id"], []).append(entry)
    out["channels"], out["groups"], out["ims"] = public, private, ims
    out["is_public"] = bool(rec.get("public_url_shared")) or any(
        (channel := access.channel_by_id(ctx, share["channel"])) is not None
        and access.channel_kind(channel) == "public"
        for share in rec.get("_shares", [])
    )
    out["shares"] = shares
    out["comments_count"] = 0
    return out


def file_visible(ctx: ProviderContext, actor: Actor, rec: dict[str, Any]) -> bool:
    """Uploader, members of any conversation it was shared into, or anyone for public shares."""
    if rec.get("_deleted"):
        return False
    if rec["user"] == actor.uid:
        return True
    for share in rec.get("_shares", []):
        channel = access.channel_by_id(ctx, share["channel"])
        if channel is None:
            continue
        if access.is_member(channel, actor.uid):
            return True
        if access.can_view(ctx, actor, channel) and not actor.is_bot:
            return True
    return False


# -- messages ----------------------------------------------------------------------------------


def message_json(
    ctx: ProviderContext,
    request: Request | None,
    actor: Actor | None,
    rec: dict[str, Any],
    *,
    root: bool = True,
) -> dict[str, Any]:
    out = clean(rec, keep_id=False)
    if rec.get("_files"):
        files = [
            file_json(ctx, request, f, actor)
            for fid in rec["_files"]
            if (f := ctx.store.collection(access.FILES).get(fid)) and not f.get("_deleted")
        ]
        out["files"] = files
        out.setdefault("upload", False)
        out.setdefault("display_as_bot", False)
    if rec.get("bot_id") and "bot_profile" not in out:
        profile = bot_profile(ctx, rec["bot_id"])
        if profile:
            out["bot_profile"] = profile
    if rec.get("reply_count") and rec.get("thread_ts") == rec["ts"]:
        participants = {rec.get("user"), *rec.get("reply_users", [])}
        out["subscribed"] = bool(actor and actor.uid in participants)
        out["is_locked"] = False
    if root and rec.get("subtype") == "thread_broadcast":
        parent = access.get_message(ctx, rec["_channel"], rec.get("thread_ts"))
        if parent is not None:
            out["root"] = message_json(ctx, request, actor, parent, root=False)
    return out


def permalink_for(
    rec: dict[str, Any], request: Request | None = None, ctx: ProviderContext | None = None
) -> str:
    parent = rec.get("thread_ts")
    link = message_permalink(rec["_channel"], rec["ts"], parent if parent and parent != rec["ts"] else None)
    if request is not None and ctx is not None:
        return url_for_host(request, ctx, f"{WORKSPACE_DOMAIN}.slack.com", link[len(WORKSPACE_URL) :])
    return link


# -- usergroups --------------------------------------------------------------------------------


def usergroup_json(
    ctx: ProviderContext, group: dict[str, Any], *, include_users: bool = False, include_count: bool = True
) -> dict[str, Any]:
    out = clean(group)
    users = list(group["_users"])
    if include_users:
        out["users"] = users
    if include_count:
        out["user_count"] = len(users)
    return out
