"""Seed the Slack app (credentials, bot user), tokens, users, user groups and custom emoji."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any

from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.slack import access
from emulators.providers.slack.util import digits_from, stable_id

#: Scopes a typical Slack app requests. ``chat:write.public``/``chat:write.customize`` are left
#: out of the default bot token on purpose so ``not_in_channel`` can be exercised.
BOT_SCOPES = (
    "app_mentions:read,bookmarks:read,bookmarks:write,channels:history,channels:join,channels:manage,"
    "channels:read,chat:write,commands,dnd:read,emoji:read,files:read,files:write,groups:history,"
    "groups:read,groups:write,im:history,im:read,im:write,incoming-webhook,mpim:history,mpim:read,"
    "mpim:write,pins:read,pins:write,reactions:read,reactions:write,team:read,usergroups:read,"
    "usergroups:write,users.profile:read,users:read,users:read.email"
).split(",")
BOT_SCOPES_FULL = sorted({*BOT_SCOPES, "chat:write.public", "chat:write.customize", "links:read", "links:write"})
BOT_SCOPES_READONLY = ["channels:read", "groups:read", "users:read", "team:read"]
USER_SCOPES = (
    "bookmarks:read,bookmarks:write,channels:history,channels:read,channels:write,chat:write,dnd:read,"
    "dnd:write,emoji:read,files:read,files:write,groups:history,groups:read,groups:write,identify,"
    "im:history,im:read,im:write,mpim:history,mpim:read,mpim:write,pins:read,pins:write,reactions:read,"
    "reactions:write,search:read,team:read,usergroups:read,usergroups:write,users.profile:read,"
    "users.profile:write,users:read,users:read.email,users:write"
).split(",")
APP_SCOPES = ["connections:write", "authorizations:read", "app_configurations:write"]

STATUSES = (
    ("In a meeting", ":spiral_calendar_pad:"),
    ("On vacation", ":palm_tree:"),
    ("Out sick", ":face_with_thermometer:"),
    ("Heads down on fieldwork", ":headphones:"),
    ("Commuting", ":bus:"),
)
OWNER_PERSON = "p001"
GUEST_PERSONS = {"c002-1": False, "c002-2": True}  # person id -> single-channel (ultra restricted)
EXT_CLIENTS = ("c004", "c005", "c006", "c007")  # clients that get a Slack Connect channel


def avatar_urls(email: str, seed: str) -> dict[str, str]:
    digest = hashlib.md5(email.lower().encode(), usedforsecurity=False).hexdigest()
    shade = int(stable_hash(seed, length=4), 16) % 25
    fallback = f"https%3A%2F%2Fa.slack-edge.com%2Fdf10d%2Fimg%2Favatars%2Fava_{shade:04d}-"
    urls = {
        f"image_{size}": f"https://secure.gravatar.com/avatar/{digest}.jpg?s={size}&d={fallback}{size}.png"
        for size in (24, 32, 48, 72, 192, 512)
    }
    urls["image_original"] = f"https://avatars.slack-edge.com/{digest[:16]}_original.jpg"
    return urls


def user_record(
    ctx: ProviderContext,
    uid: str,
    *,
    name: str,
    first: str,
    last: str,
    email: str,
    title: str = "",
    phone: str = "",
    tz: str = "America/Toronto",
    locale: str = "en-US",
    team: str | None = None,
    updated: int = 0,
    person: str | None = None,
    deleted: bool = False,
    admin: bool = False,
    owner: bool = False,
    primary: bool = False,
    restricted: bool = False,
    ultra: bool = False,
    stranger: bool = False,
    pronouns: str | None = None,
) -> dict[str, Any]:
    team_id = team or access.team_id(ctx)
    real = f"{first} {last}".strip()
    status = STATUSES[int(stable_hash(uid, "status", length=4), 16) % len(STATUSES)]
    has_status = int(stable_hash(uid, "has-status", length=3), 16) % 10 == 0 and not deleted
    profile: dict[str, Any] = {
        "title": title,
        "phone": phone,
        "skype": "",
        "real_name": real,
        "real_name_normalized": real,
        "display_name": first,
        "display_name_normalized": first,
        "fields": None,
        "status_text": status[0] if has_status else "",
        "status_emoji": status[1] if has_status else "",
        "status_emoji_display_info": [],
        "status_expiration": 0,
        "avatar_hash": "g" + stable_hash(email, length=11),
        "email": email,
        "first_name": first,
        "last_name": last,
        **avatar_urls(email, uid),
        "status_text_canonical": "",
        "team": team_id,
    }
    if pronouns:
        profile["pronouns"] = pronouns
    record: dict[str, Any] = {
        "id": uid,
        "team_id": team_id,
        "name": name,
        "deleted": deleted,
        "color": stable_hash(uid, "color", length=6),
        "real_name": real,
        "tz": tz,
        "locale": locale,
        "profile": profile,
        "is_admin": admin or owner,
        "is_owner": owner,
        "is_primary_owner": primary,
        "is_restricted": restricted,
        "is_ultra_restricted": ultra,
        "is_bot": False,
        "is_app_user": False,
        "updated": updated,
        "is_email_confirmed": True,
        "who_can_share_contact_card": "EVERYONE",
        "_person": person,
        "_presence": "active" if int(stable_hash(uid, "presence", length=3), 16) % 10 < 7 else "away",
        "_dnd": {"enabled": False, "snooze_end": 0},
    }
    if stranger:
        record["is_stranger"] = True
    return record


def _epoch(day: date | None, fallback: int) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) if day else fallback


def seed_app(ctx: ProviderContext) -> dict[str, Any]:
    team = access.team_id(ctx)
    founded = int(datetime(2019, 4, 1, tzinfo=UTC).timestamp())
    app_id = stable_id("A", "app")
    bot_id = stable_id("B", "bot")
    bot_uid = stable_id("U", "halcyon-bot")
    client_id = f"{digits_from(team, 'client-a', length=12)}.{digits_from(team, 'client-b', length=13)}"
    config = {
        "id": "app",
        "app_id": app_id,
        "bot_id": bot_id,
        "bot_user_id": bot_uid,
        "name": "Halcyon Bot",
        "client_id": client_id,
        "client_secret": stable_hash("slack-client-secret", team, length=32),
        "signing_secret": stable_hash("slack-signing-secret", team, length=32),
        "verification_token": stable_hash("slack-verification-token", team, length=24),
        "redirect_uris": [],
        "token_rotation": ctx.setting("TOKEN_ROTATION", "false") in ("1", "true", "yes"),
        "request_url": None,
        "events": [],
        "verified": False,
        "created": founded,
    }
    access.col(ctx, access.APP).put(config)
    icons = {
        "image_36": "https://a.slack-edge.com/80588/img/plugins/app/bot_36.png",
        "image_48": "https://a.slack-edge.com/80588/img/plugins/app/bot_48.png",
        "image_72": "https://a.slack-edge.com/80588/img/plugins/app/service_72.png",
    }
    ctx.store.collection("bots").put(
        {
            "id": bot_id,
            "app_id": app_id,
            "user_id": bot_uid,
            "name": "Halcyon Bot",
            "updated": founded,
            "deleted": False,
            "icons": icons,
        }
    )
    ctx.tokens.register_client(
        client_id,
        config["client_secret"],
        redirect_uris=[],
        name="Halcyon Bot",
    )
    return config


def seed_users(ctx: ProviderContext) -> dict[str, str]:
    """Create every user record; returns ``{world person id: slack user id}``."""
    w = ctx.world
    users = access.col(ctx, access.USERS)
    founded = int(datetime(2019, 4, 1, tzinfo=UTC).timestamp())
    mapping: dict[str, str] = {}
    slackbot = user_record(
        ctx,
        access.SLACKBOT,
        name="slackbot",
        first="Slackbot",
        last="",
        email="",
        tz="America/Los_Angeles",
        updated=founded,
    )
    slackbot["profile"].update({"always_active": True, "email": None, "title": "", "image_original": None})
    slackbot["profile"].pop("email")
    slackbot["_presence"] = "active"
    users.put(slackbot)
    for person in w.employees:
        uid = stable_id("U", person.id)
        mapping[person.id] = uid
        users.put(
            user_record(
                ctx,
                uid,
                name=person.username,
                first=person.first_name,
                last=person.last_name,
                email=person.email,
                title=person.job_title,
                phone=person.phone_business or "",
                tz=person.timezone,
                locale=person.locale,
                updated=_epoch(person.hire_date, founded),
                person=person.id,
                deleted=not person.active,
                admin=person.is_admin,
                owner=person.id == OWNER_PERSON,
                primary=person.id == OWNER_PERSON,
                pronouns=person.pronouns,
            )
        )
    app = access.app_config(ctx)
    bot = user_record(
        ctx,
        app["bot_user_id"],
        name="halcyon_bot",
        first="Halcyon",
        last="Bot",
        email=f"halcyon-bot@{w.org.domain}",
        title="",
        updated=founded,
    )
    bot["real_name"] = "Halcyon Bot"
    bot["is_bot"] = True
    bot["profile"].update(
        {
            "real_name": "Halcyon Bot",
            "real_name_normalized": "Halcyon Bot",
            "display_name": "halcyon_bot",
            "display_name_normalized": "halcyon_bot",
            "bot_id": app["bot_id"],
            "api_app_id": app["app_id"],
            "always_active": True,
        }
    )
    bot["profile"].pop("email")
    bot["_presence"] = "active"
    users.put(bot)
    for pid, single in GUEST_PERSONS.items():
        person = w.person(pid)
        uid = stable_id("U", person.id)
        mapping[pid] = uid
        users.put(
            user_record(
                ctx,
                uid,
                name=person.username,
                first=person.first_name,
                last=person.last_name,
                email=person.email,
                title=person.job_title,
                phone=person.phone_mobile or "",
                tz=person.timezone,
                updated=founded,
                person=pid,
                restricted=True,
                ultra=single,
            )
        )
    for client_id in EXT_CLIENTS:
        client_team = stable_id("T", client_id)
        for pid in w.client(client_id).contact_ids:
            person = w.person(pid)
            uid = stable_id("U", person.id)
            mapping[pid] = uid
            users.put(
                user_record(
                    ctx,
                    uid,
                    name=person.username,
                    first=person.first_name,
                    last=person.last_name,
                    email=person.email,
                    title=person.job_title,
                    phone=person.phone_mobile or "",
                    tz=person.timezone,
                    team=client_team,
                    updated=founded,
                    person=pid,
                    stranger=True,
                )
            )
    return mapping


def seed_usergroups(ctx: ProviderContext, mapping: dict[str, str], channels: dict[str, str]) -> None:
    w = ctx.world
    handles = {"g-partners": "partners", "g-leadership": "leadership", "g-managers": "managers", "g-it-admins": "it-admins"}
    default_channel = {
        "audit": "audit-general",
        "tax": "tax-general",
        "advisory": "advisory-general",
        "growth": "bd-general",
        "it": "it-help",
    }
    owner = mapping[OWNER_PERSON]
    admin = next((mapping[p.id] for p in w.employees if p.is_admin), owner)
    created = int(datetime(2019, 4, 8, tzinfo=UTC).timestamp())
    for group in w.groups:
        if group.kind == "department" and group.department_id:
            handle = group.department_id
        elif group.id in handles:
            handle = handles[group.id]
        else:
            continue
        members = [mapping[p.id] for p in w.group_members(group.id) if p.id in mapping and p.active]
        chan = channels.get(default_channel.get(handle, ""))
        access.col(ctx, access.USERGROUPS).put(
            {
                "id": stable_id("S", group.id),
                "team_id": access.team_id(ctx),
                "is_usergroup": True,
                "is_subteam": True,
                "name": group.name,
                "description": group.description,
                "handle": handle,
                "is_external": False,
                "date_create": created,
                "date_update": created,
                "date_delete": 0,
                "auto_type": None,
                "auto_provision": False,
                "enterprise_subteam_id": "",
                "created_by": admin,
                "updated_by": admin,
                "prefs": {"channels": [chan] if chan else [], "groups": []},
                "channel_count": 1 if chan else 0,
                "_users": members,
            }
        )


EMOJI = {
    "halcyon": "halcyon",
    "halcyon-parrot": "halcyon",
    "audit-done": "halcyon",
    "ship-it": "alias:rocket",
    "lgtm": "halcyon",
    "party-parrot": "halcyon",
    "thisisfine": "halcyon",
    "shipit": "alias:ship-it",
}


def seed_emoji(ctx: ProviderContext) -> None:
    team = access.team_id(ctx)
    for name, target in EMOJI.items():
        if target.startswith("alias:"):
            url = target
        else:
            digest = stable_hash(name, length=10)
            url = f"https://emoji.slack-edge.com/{team}/{name}/{digest}.png"
        access.col(ctx, access.EMOJI).put({"id": name, "url": url})


def seed_tokens(ctx: ProviderContext, mapping: dict[str, str]) -> None:
    """Deterministic, documented tokens so tests can start without an OAuth dance."""
    app = access.app_config(ctx)
    team_digits = digits_from(access.team_id(ctx), "team", length=11)

    def issue(token: str, kind: str, uid: str, scopes: list[str], **extra: Any) -> None:
        ctx.tokens.issue_access(
            token=token,
            client_id=app["client_id"],
            user_id=uid if kind == "user" else None,
            scopes=scopes,
            ttl=None,
            app_only=kind != "user",
            extra={
                "kind": kind,
                "slack_user": uid,
                "bot_id": app["bot_id"],
                "app_id": app["app_id"],
                "seeded": True,
                **extra,
            },
        )

    def bot_token(label: str) -> str:
        return f"xoxb-{team_digits}-{digits_from(label, 'a', length=13)}-{stable_hash('xoxb', label, length=24)}"

    def user_token(person: str) -> str:
        return (
            f"xoxp-{team_digits}-{digits_from(person, 'u', length=12)}-{digits_from(person, 'n', length=13)}-"
            f"{stable_hash('xoxp', person, length=32)}"
        )

    issue(bot_token("bot"), "bot", app["bot_user_id"], BOT_SCOPES)
    issue(bot_token("bot-full"), "bot", app["bot_user_id"], BOT_SCOPES_FULL)
    issue(bot_token("bot-readonly"), "bot", app["bot_user_id"], BOT_SCOPES_READONLY)
    issue(
        f"xapp-1-{app['app_id']}-{digits_from('app', 'a', length=13)}-{stable_hash('xapp', length=40)}",
        "app",
        "",
        APP_SCOPES,
    )
    for pid in (OWNER_PERSON, "p035", "p018", "p046", "c002-1"):
        uid = mapping.get(pid)
        if uid:
            issue(user_token(pid), "user", uid, USER_SCOPES)


def seeded_tokens(ctx: ProviderContext) -> dict[str, str]:
    """``{label: token}`` of the documented tokens (for ``/_emulator/info`` and tests)."""
    found: dict[str, str] = {}
    for record in ctx.store.collection("_grants"):
        extra = record.get("extra") or {}
        if record.get("kind") != "access" or not extra.get("seeded"):
            continue
        token = record["token"]
        if token.startswith("xoxb-"):
            scopes = set(record["scopes"])
            if "chat:write.public" in scopes:
                found["bot_full"] = token
            elif scopes <= set(BOT_SCOPES_READONLY):
                found["bot_readonly"] = token
            else:
                found["bot"] = token
        elif token.startswith("xapp-"):
            found["app_level"] = token
        elif token.startswith("xoxp-"):
            user = access.user_by_id(ctx, extra.get("slack_user"))
            if user and user.get("_person"):
                found[f"user:{user['_person']}"] = token
    return found
