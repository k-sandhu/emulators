"""users.*, team.*, usergroups.*, dnd.*, bots.* and emoji.* Web API methods."""

from __future__ import annotations

import re
from typing import Any

from emulators.providers.slack import access, events
from emulators.providers.slack.m_conversations import TYPE_OF_KIND, TYPE_SCOPES
from emulators.providers.slack.paging import paginate
from emulators.providers.slack.records import new_id
from emulators.providers.slack.runtime import USER, Call, SlackError, api
from emulators.providers.slack.shapes import channel_json, user_json, usergroup_json
from emulators.providers.slack.util import WORKSPACE_DOMAIN, WORKSPACE_URL, slugify

PROFILE_FIELDS = {
    "first_name",
    "last_name",
    "real_name",
    "display_name",
    "phone",
    "title",
    "pronouns",
    "status_text",
    "status_emoji",
    "status_expiration",
    "skype",
}
EMOJI_RE = re.compile(r"^:[a-z0-9_+\-']+(?:::skin-tone-[2-6])?:$")


def _workspace_users(call: Call) -> list[dict[str, Any]]:
    return [u for u in access.col(call.ctx, access.USERS) if not u.get("is_stranger")]


# -- users -------------------------------------------------------------------------------------


@api("users.list", scopes=("users:read",))
async def users_list(call: Call) -> dict[str, Any]:
    page, meta = paginate(_workspace_users(call), call, kind="user", key=lambda u: u["id"])
    locale = call.params.bool("include_locale")
    return {
        "members": [user_json(call.ctx, u, call.me, locale=locale) for u in page],
        "cache_ts": int(call.ctx.clock.now().timestamp()),
        "response_metadata": meta,
    }


@api("users.info", scopes=("users:read",))
async def users_info(call: Call) -> dict[str, Any]:
    user = access.user_by_id(call.ctx, call.params.str("user"))
    if user is None:
        raise SlackError("user_not_found")
    return {"user": user_json(call.ctx, user, call.me, locale=call.params.bool("include_locale"))}


@api("users.lookupByEmail", scopes=("users:read.email",))
async def users_lookup_by_email(call: Call) -> dict[str, Any]:
    call.need_scope("users:read")
    user = access.user_by_email(call.ctx, call.params.need("email"))
    if user is None or user.get("is_stranger"):
        raise SlackError("users_not_found")
    return {"user": user_json(call.ctx, user, call.me)}


@api("users.profile.get", scopes=("users.profile:read",))
async def users_profile_get(call: Call) -> dict[str, Any]:
    uid = call.params.str("user") or call.me.uid
    user = access.user_by_id(call.ctx, uid)
    if user is None:
        raise SlackError("user_not_found")
    return {"profile": user_json(call.ctx, user, call.me)["profile"]}


def _profile_changes(call: Call) -> dict[str, Any]:
    p = call.params
    raw = p.obj("profile", "invalid_profile")
    if raw is None and p.str("name"):
        raw = {p.str("name"): p.raw("value", "")}
    if not isinstance(raw, dict) or not raw:
        raise SlackError("invalid_profile")
    unknown = set(raw) - PROFILE_FIELDS - {"email", "fields"}
    if unknown:
        raise SlackError("invalid_profile")
    return raw


@api("users.profile.set", scopes=("users.profile:write",), kinds=(USER,))
async def users_profile_set(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    target_id = call.params.str("user") or call.me.uid
    target = access.user_by_id(ctx, target_id)
    if target is None:
        raise SlackError("user_not_found")
    if target["id"] != call.me.uid and not access.is_admin(access.actor_user(ctx, call.me)):
        raise SlackError("not_admin")
    changes = _profile_changes(call)
    if "email" in changes and not access.is_admin(access.actor_user(ctx, call.me)):
        raise SlackError("not_admin")
    emoji = changes.get("status_emoji")
    if emoji not in (None, "") and not EMOJI_RE.match(str(emoji)):
        raise SlackError("profile_status_set_failed_not_valid_emoji")
    profile = target["profile"]
    for key, value in changes.items():
        if key == "fields":
            profile["fields"] = value
        elif key == "status_expiration":
            profile[key] = int(value or 0)
        else:
            profile[key] = value if key == "email" else str(value)
    if "real_name" in changes:
        target["real_name"] = profile["real_name_normalized"] = str(changes["real_name"])
    elif "first_name" in changes or "last_name" in changes:
        target["real_name"] = profile["real_name"] = profile["real_name_normalized"] = (
            f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
        )
    if "display_name" in changes:
        profile["display_name_normalized"] = str(changes["display_name"])
    target["updated"] = int(ctx.clock.now().timestamp())
    await events.emit_workspace(
        ctx, {"type": "user_change", "user": user_json(ctx, target), "event_ts": str(ctx.clock.timestamp())}
    )
    return {"profile": user_json(ctx, target, call.me)["profile"], "username": target["name"]}


@api("users.conversations")
async def users_conversations(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    types = call.params.csv("types") or ["public_channel"]
    for kind in types:
        if kind not in TYPE_SCOPES:
            raise SlackError("invalid_types")
        call.need_scope(TYPE_SCOPES[kind])
    uid = call.params.str("user") or call.me.uid
    subject = access.user_by_id(ctx, uid)
    if subject is None:
        raise SlackError("user_not_found")
    exclude_archived = call.params.bool("exclude_archived")
    items = [
        c
        for c in access.col(ctx, access.CHANNELS)
        if access.is_member(c, uid)
        and TYPE_OF_KIND[access.channel_kind(c)] in types
        and not (exclude_archived and c.get("is_archived"))
        and (uid == call.me.uid or access.can_view(ctx, call.me, c))
    ]
    page, meta = paginate(items, call, kind="team", key=lambda c: c["id"], max_limit=999)
    return {
        "channels": [channel_json(ctx, c, uid, num_members=True, member_flag=False) for c in page],
        "response_metadata": meta,
    }


@api("users.getPresence", scopes=("users:read",))
async def users_get_presence(call: Call) -> dict[str, Any]:
    uid = call.params.str("user") or call.me.uid
    user = access.user_by_id(call.ctx, uid)
    if user is None:
        raise SlackError("user_not_found")
    presence = user.get("_presence", "active")
    body: dict[str, Any] = {"presence": presence}
    if uid == call.me.uid:
        body.update(
            {
                "online": presence == "active",
                "auto_away": False,
                "manual_away": presence == "away" and user.get("_manual_away", False),
                "connection_count": 1 if presence == "active" else 0,
                "last_activity": int(call.ctx.clock.now().timestamp()) - 60,
            }
        )
    return body


@api("users.setPresence", scopes=("users:write",), kinds=(USER,))
async def users_set_presence(call: Call) -> dict[str, Any]:
    presence = call.params.need("presence")
    if presence not in ("auto", "away"):
        raise SlackError("invalid_presence")
    user = access.actor_user(call.ctx, call.me)
    user["_presence"] = "active" if presence == "auto" else "away"
    user["_manual_away"] = presence == "away"
    return {}


# -- team --------------------------------------------------------------------------------------


@api("team.info", scopes=("team:read",))
async def team_info(call: Call) -> dict[str, Any]:
    ctx = call.ctx
    domain = call.params.str("domain")
    team = call.params.str("team")
    if (domain and domain != WORKSPACE_DOMAIN) or (team and team != access.team_id(ctx)):
        raise SlackError("team_not_found")
    org = ctx.world.org
    icons = {
        f"image_{size}": f"https://a.slack-edge.com/80588/img/avatars-teams/ava_0001-{size}.png"
        for size in (34, 44, 68, 88, 102, 132)
    }
    return {
        "team": {
            "id": access.team_id(ctx),
            "name": org.name,
            "url": WORKSPACE_URL + "/",
            "domain": WORKSPACE_DOMAIN,
            "email_domain": org.domain,
            "icon": {**icons, "image_default": True},
            "avatar_base_url": "https://ca.slack-edge.com/",
            "is_verified": False,
        }
    }


@api("team.profile.get", scopes=("users.profile:read",))
async def team_profile_get(call: Call) -> dict[str, Any]:
    fields = [
        ("Xf0TEAMID01", "employee_id", "Employee ID", "text", None),
        ("Xf0TEAMOFF2", "office", "Office", "options_list", ["Toronto", "New York", "Chicago", "London"]),
        ("Xf0TEAMDEP3", "department", "Department", "text", None),
    ]
    return {
        "profile": {
            "fields": [
                {
                    "id": fid,
                    "ordering": position,
                    "field_name": "",
                    "label": label,
                    "hint": "",
                    "type": kind,
                    "possible_values": values,
                    "options": {"is_protected": False, "is_scim": False},
                    "is_hidden": False,
                }
                for position, (fid, _, label, kind, values) in enumerate(fields)
            ]
        }
    }


# -- usergroups --------------------------------------------------------------------------------


def _group(call: Call, ref: str | None) -> dict[str, Any]:
    group = access.col(call.ctx, access.USERGROUPS).get(ref) if ref else None
    if group is None:
        raise SlackError("no_such_subteam")
    return group


def _group_out(call: Call, group: dict[str, Any]) -> dict[str, Any]:
    return usergroup_json(
        call.ctx,
        group,
        include_users=call.params.bool("include_users"),
        include_count=call.params.bool("include_count"),
    )


def _require_group_admin(call: Call) -> None:
    if not call.me.is_bot and not access.is_admin(access.actor_user(call.ctx, call.me)):
        raise SlackError("permission_denied")


@api("usergroups.list", scopes=("usergroups:read",))
async def usergroups_list(call: Call) -> dict[str, Any]:
    include_disabled = call.params.bool("include_disabled")
    groups = [g for g in access.col(call.ctx, access.USERGROUPS) if include_disabled or not g["date_delete"]]
    return {"usergroups": [_group_out(call, g) for g in groups]}


@api("usergroups.users.list", scopes=("usergroups:read",))
async def usergroups_users_list(call: Call) -> dict[str, Any]:
    group = _group(call, call.params.str("usergroup"))
    if group["date_delete"] and not call.params.bool("include_disabled"):
        raise SlackError("no_such_subteam")
    return {"users": list(group["_users"])}


def _resolve_channels(call: Call) -> list[str]:
    result = []
    for ref in call.params.csv("channels"):
        channel = access.channel_by_id(call.ctx, ref)
        if channel is None:
            raise SlackError("invalid_channel")
        result.append(channel["id"])
    return result


@api("usergroups.create", scopes=("usergroups:write",))
async def usergroups_create(call: Call) -> dict[str, Any]:
    ctx, p = call.ctx, call.params
    _require_group_admin(call)
    name = p.str("name")
    if not name:
        raise SlackError("invalid_arguments")
    groups = access.col(ctx, access.USERGROUPS)
    if any(g["name"].lower() == name.lower() for g in groups):
        raise SlackError("name_already_exists")
    handle = (p.str("handle") or slugify(name)).lstrip("@").lower()
    if any(g["handle"] == handle for g in groups):
        raise SlackError("handle_already_exists")
    channels = _resolve_channels(call)
    now = int(ctx.clock.now().timestamp())
    record = {
        "id": new_id(ctx, "S", access.USERGROUPS),
        "team_id": access.team_id(ctx),
        "is_usergroup": True,
        "is_subteam": True,
        "name": name,
        "description": p.str("description") or "",
        "handle": handle,
        "is_external": False,
        "date_create": now,
        "date_update": now,
        "date_delete": 0,
        "auto_type": None,
        "auto_provision": False,
        "enterprise_subteam_id": "",
        "created_by": call.me.uid,
        "updated_by": call.me.uid,
        "prefs": {"channels": channels, "groups": []},
        "channel_count": len(channels),
        "_users": [],
    }
    groups.put(record)
    return {"usergroup": _group_out(call, record)}


@api("usergroups.update", scopes=("usergroups:write",))
async def usergroups_update(call: Call) -> dict[str, Any]:
    p = call.params
    _require_group_admin(call)
    group = _group(call, p.str("usergroup"))
    others = [g for g in access.col(call.ctx, access.USERGROUPS) if g["id"] != group["id"]]
    if p.str("name"):
        if any(g["name"].lower() == p.str("name", "").lower() for g in others):
            raise SlackError("name_already_exists")
        group["name"] = p.str("name")
    if p.str("handle"):
        handle = p.str("handle", "").lstrip("@").lower()
        if any(g["handle"] == handle for g in others):
            raise SlackError("handle_already_exists")
        group["handle"] = handle
    if p.has("description"):
        group["description"] = str(p.raw("description"))
    if p.has("channels"):
        channels = _resolve_channels(call)
        group["prefs"]["channels"] = channels
        group["channel_count"] = len(channels)
    group["date_update"] = int(call.ctx.clock.now().timestamp())
    group["updated_by"] = call.me.uid
    return {"usergroup": _group_out(call, group)}


@api("usergroups.users.update", scopes=("usergroups:write",))
async def usergroups_users_update(call: Call) -> dict[str, Any]:
    _require_group_admin(call)
    group = _group(call, call.params.str("usergroup"))
    refs = call.params.csv("users")
    if not refs:
        raise SlackError("no_users_provided")
    members: list[str] = []
    for ref in refs:
        user = access.user_by_id(call.ctx, ref)
        if user is None or user.get("deleted") or user.get("is_stranger"):
            raise SlackError("invalid_users")
        if user["id"] not in members:
            members.append(user["id"])
    group["_users"] = members
    group["date_update"] = int(call.ctx.clock.now().timestamp())
    group["updated_by"] = call.me.uid
    return {"usergroup": _group_out(call, group)}


@api("usergroups.disable", scopes=("usergroups:write",))
async def usergroups_disable(call: Call) -> dict[str, Any]:
    _require_group_admin(call)
    group = _group(call, call.params.str("usergroup"))
    if group["date_delete"]:
        raise SlackError("already_disabled")
    group["date_delete"] = int(call.ctx.clock.now().timestamp())
    return {"usergroup": _group_out(call, group)}


@api("usergroups.enable", scopes=("usergroups:write",))
async def usergroups_enable(call: Call) -> dict[str, Any]:
    _require_group_admin(call)
    group = _group(call, call.params.str("usergroup"))
    group["date_delete"] = 0
    return {"usergroup": _group_out(call, group)}


# -- do not disturb ----------------------------------------------------------------------------


def _dnd_state(call: Call, user: dict[str, Any]) -> dict[str, Any]:
    now = int(call.ctx.clock.now().timestamp())
    dnd = user.setdefault("_dnd", {"enabled": False, "snooze_end": 0})
    snoozing = dnd.get("snooze_end", 0) > now
    body: dict[str, Any] = {
        "dnd_enabled": bool(dnd.get("enabled")),
        "next_dnd_start_ts": 1,
        "next_dnd_end_ts": 1,
        "snooze_enabled": snoozing,
    }
    if snoozing:
        body["snooze_endtime"] = dnd["snooze_end"]
        body["snooze_remaining"] = dnd["snooze_end"] - now
    return body


@api("dnd.info", scopes=("dnd:read",))
async def dnd_info(call: Call) -> dict[str, Any]:
    user = access.user_by_id(call.ctx, call.params.str("user") or call.me.uid)
    if user is None:
        raise SlackError("user_not_found")
    return _dnd_state(call, user)


@api("dnd.teamInfo", scopes=("dnd:read",))
async def dnd_team_info(call: Call) -> dict[str, Any]:
    refs = call.params.csv("users")
    if not refs:
        raise SlackError("missing_user")
    result = {}
    for ref in refs:
        user = access.user_by_id(call.ctx, ref)
        if user is None:
            raise SlackError("user_not_found")
        state = _dnd_state(call, user)
        result[ref] = {k: state[k] for k in ("dnd_enabled", "next_dnd_start_ts", "next_dnd_end_ts")}
    return {"users": result}


@api("dnd.setSnooze", scopes=("dnd:write",), kinds=(USER,))
async def dnd_set_snooze(call: Call) -> dict[str, Any]:
    minutes = call.params.int("num_minutes")
    if minutes is None or minutes < 1:
        raise SlackError("missing_duration")
    user = access.actor_user(call.ctx, call.me)
    now = int(call.ctx.clock.now().timestamp())
    user.setdefault("_dnd", {})["snooze_end"] = now + minutes * 60
    state = _dnd_state(call, user)
    return {k: state[k] for k in ("snooze_enabled", "snooze_endtime", "snooze_remaining")}


@api("dnd.endSnooze", scopes=("dnd:write",), kinds=(USER,))
async def dnd_end_snooze(call: Call) -> dict[str, Any]:
    user = access.actor_user(call.ctx, call.me)
    if not _dnd_state(call, user)["snooze_enabled"]:
        raise SlackError("snooze_not_active")
    user["_dnd"]["snooze_end"] = 0
    return _dnd_state(call, user)


@api("dnd.endDnd", scopes=("dnd:write",), kinds=(USER,))
async def dnd_end_dnd(call: Call) -> dict[str, Any]:
    user = access.actor_user(call.ctx, call.me)
    user["_dnd"] = {"enabled": False, "snooze_end": 0}
    return {}


# -- bots and emoji ----------------------------------------------------------------------------


@api("bots.info", scopes=("users:read",))
async def bots_info(call: Call) -> dict[str, Any]:
    bot = call.ctx.store.collection("bots").get(call.params.str("bot"))
    if bot is None:
        raise SlackError("bot_not_found")
    return {
        "bot": {
            "id": bot["id"],
            "deleted": bot["deleted"],
            "name": bot["name"],
            "updated": bot["updated"],
            "app_id": bot["app_id"],
            "user_id": bot["user_id"],
            "icons": bot["icons"],
        }
    }


@api("emoji.list", scopes=("emoji:read",))
async def emoji_list(call: Call) -> dict[str, Any]:
    emoji = {e["id"]: e["url"] for e in access.col(call.ctx, access.EMOJI)}
    body: dict[str, Any] = {"emoji": emoji}
    if call.params.bool("include_categories"):
        body["categories"] = []
        body["categories_version"] = "2"
    return body
