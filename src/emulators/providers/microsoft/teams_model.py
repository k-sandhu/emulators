"""Teams, channel and chat projections with conversation-scoped message identifiers."""

from __future__ import annotations

import copy
import html
import re
from datetime import timedelta
from typing import Any

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_guid
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common
from emulators.providers.microsoft import drive_model as dm
from emulators.world.model import ChatMessage

TEAMS = "ms_teams"
CHANNELS = "ms_teams_channels"
CHATS = "ms_teams_chats"
MESSAGES = "ms_teams_messages"


def teamwork_user(ctx: ProviderContext, pid: str) -> dict[str, Any]:
    person = ctx.world.person(pid)
    return {
        "@odata.type": "#microsoft.graph.teamworkUserIdentity",
        "id": common.user_id(ctx.world, pid),
        "displayName": person.display_name,
        "userIdentityType": "aadUser",
        "tenantId": common.tenant_id(ctx.world),
    }


def member(ctx: ProviderContext, pid: str, owners: list[str]) -> dict[str, Any]:
    person = ctx.world.person(pid)
    return {
        "@odata.type": "#microsoft.graph.aadUserConversationMember",
        "id": stable_guid(ctx.world.seed, "conversation-member", pid),
        "roles": ["owner"] if pid in owners else (["guest"] if person.is_external else []),
        "displayName": person.display_name,
        "visibleHistoryStartDateTime": "0001-01-01T00:00:00Z",
        "userId": common.user_id(ctx.world, pid),
        "email": person.email,
        "tenantId": common.tenant_id(ctx.world),
    }


def message_key(scope: str, mid: str) -> str:
    return scope + "/" + mid


def new_message(
    ctx: ProviderContext,
    scope: str,
    pid: str,
    mid: str,
    timestamp: str,
    *,
    channel: dict[str, Any] | None = None,
    chat: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    reply: str | None = None,
) -> dict[str, Any]:
    location = (
        {"chatId": chat["id"], "channelIdentity": None}
        if chat
        else {"chatId": None, "channelIdentity": {"teamId": channel["_team"], "channelId": channel["id"]}}
    )
    base = chat["webUrl"] if chat else channel["webUrl"]
    return {
        "id": mid,
        "replyToId": reply,
        "etag": mid,
        "messageType": "message",
        "createdDateTime": timestamp,
        "lastModifiedDateTime": timestamp,
        "lastEditedDateTime": None,
        "deletedDateTime": None,
        "subject": None,
        "summary": None,
        **location,
        "importance": "normal",
        "locale": "en-us",
        "webUrl": base + "&messageId=" + mid,
        "policyViolation": None,
        "eventDetail": None,
        "from": {"application": None, "device": None, "user": teamwork_user(ctx, pid)},
        "body": body or {"contentType": "html", "content": ""},
        "attachments": [],
        "mentions": [],
        "reactions": [],
        "messageHistory": [],
        "_scope": scope,
        "_author": pid,
        "_revision": 0,
    }


def seed_messages(
    ctx: ProviderContext,
    messages: tuple[ChatMessage, ...],
    scope: str,
    *,
    channel: dict[str, Any] | None = None,
    chat: dict[str, Any] | None = None,
) -> None:
    ids: dict[str, str] = {}
    used: set[int] = set()
    for message in messages:
        value = int(message.created.timestamp() * 1000)
        while value in used:
            value += 1
        used.add(value)
        ids[message.id] = str(value)
    for message in messages:
        text = html.escape(message.text)
        mentions = []
        for index, pid in enumerate(message.mentions):
            person = ctx.world.person(pid)
            tag = f'<at id="{index}">{html.escape(person.display_name)}</at>'
            pattern = "@" + html.escape(person.display_name)
            if pattern in text:
                text = text.replace(pattern, tag, 1)
            elif "@" + person.first_name in text:
                text = text.replace("@" + person.first_name, tag, 1)
            else:
                text += " " + tag
            mentions.append(
                {
                    "id": index,
                    "mentionText": person.display_name,
                    "mentioned": {"user": teamwork_user(ctx, pid)},
                }
            )
        record = new_message(
            ctx,
            scope,
            message.author_id,
            ids[message.id],
            isoformat_z(message.created),
            channel=channel,
            chat=chat,
            body={"contentType": "html", "content": "<p>" + text.replace("\n", "</p><p>") + "</p>"},
            reply=ids.get(message.reply_to_id),
        )
        record["mentions"] = mentions
        record["reactions"] = [
            {
                "reactionType": {
                    "thumbsup": "like",
                    "+1": "like",
                    "heart": "heart",
                    "tada": "celebrate",
                    "eyes": "surprised",
                }.get(emoji, emoji),
                "displayName": emoji,
                "reactionContentUrl": None,
                "createdDateTime": record["createdDateTime"],
                "user": {"application": None, "device": None, "user": teamwork_user(ctx, pid)},
            }
            for emoji, pid in message.reactions
        ]
        for docid in message.attachment_doc_ids:
            doc = ctx.world.document(docid)
            item = ctx.store[dm.ITEMS].first(_doc=docid)
            record["attachments"].append(
                {
                    "id": stable_guid(record["id"], docid),
                    "contentType": "reference",
                    "contentUrl": item["webUrl"] if item else None,
                    "content": None,
                    "name": doc.name,
                    "thumbnailUrl": None,
                }
            )
        if message.edited:
            record["lastEditedDateTime"] = record["lastModifiedDateTime"] = isoformat_z(message.edited)
        ctx.store[MESSAGES].put_as(message_key(scope, record["id"]), record)


def seed(ctx: ProviderContext) -> None:
    for team in ctx.world.teams:
        tid = common.group_id(ctx.world, team.group_id or f"team-{team.id}")
        created = min(
            (m.created for channel in team.channels for m in channel.messages),
            default=ctx.world.anchor,
        ) - timedelta(days=1)
        ctx.store[TEAMS].put(
            {
                "id": tid,
                "displayName": team.name,
                "description": team.description,
                "visibility": team.visibility.lower(),
                "webUrl": f"https://teams.microsoft.com/l/team/{tid}/conversations?groupId={tid}&tenantId={common.tenant_id(ctx.world)}",
                "createdDateTime": isoformat_z(created),
                "isArchived": False,
                "classification": None,
                "specialization": "none",
                "internalId": stable_guid(tid, "internal"),
                "memberSettings": {
                    "allowCreateUpdateChannels": True,
                    "allowCreatePrivateChannels": True,
                    "allowDeleteChannels": True,
                    "allowAddRemoveApps": True,
                },
                "guestSettings": {"allowCreateUpdateChannels": False, "allowDeleteChannels": False},
                "messagingSettings": {
                    "allowUserEditMessages": True,
                    "allowUserDeleteMessages": True,
                    "allowOwnerDeleteMessages": True,
                    "allowTeamMentions": True,
                    "allowChannelMentions": True,
                },
                "funSettings": {
                    "allowGiphy": True,
                    "giphyContentRating": "moderate",
                    "allowStickersAndMemes": True,
                    "allowCustomMemes": True,
                },
                "_members": list(dict.fromkeys((*team.owner_ids, *team.member_ids))),
                "_owners": list(team.owner_ids),
                "_world": team.id,
            }
        )
        for channel in team.channels:
            cid = common.channel_id(ctx.world, channel.id)
            record = ctx.store[CHANNELS].put(
                {
                    "id": cid,
                    "displayName": channel.name,
                    "description": channel.description,
                    "isFavoriteByDefault": channel.name == "General",
                    "email": f"{re.sub(r'[^a-z0-9]', '', channel.name.lower())}.{stable_guid(cid)[:8]}@halcyonadvisory.example",
                    "tenantId": common.tenant_id(ctx.world),
                    "webUrl": f"https://teams.microsoft.com/l/channel/{dm.quote(cid, safe='')}/{dm.quote(channel.name)}?groupId={tid}&tenantId={common.tenant_id(ctx.world)}",
                    "membershipType": "private" if channel.is_private else "standard",
                    "createdDateTime": isoformat_z(created),
                    "_team": tid,
                    "_members": list(channel.member_ids)
                    if channel.is_private
                    else list(dict.fromkeys((*team.owner_ids, *team.member_ids))),
                }
            )
            seed_messages(ctx, channel.messages, cid, channel=record)
    for chat in ctx.world.chats:
        cid = common.chat_id(ctx.world, chat)
        timestamps = [isoformat_z(m.created) for m in chat.messages]
        record = ctx.store[CHATS].put(
            {
                "id": cid,
                "topic": chat.topic,
                "createdDateTime": min(timestamps) if timestamps else isoformat_z(ctx.world.anchor),
                "lastUpdatedDateTime": max(timestamps) if timestamps else isoformat_z(ctx.world.anchor),
                "chatType": "oneOnOne" if len(chat.member_ids) == 2 and not chat.topic else "group",
                "webUrl": f"https://teams.microsoft.com/l/chat/{dm.quote(cid, safe='')}/0?tenantId={common.tenant_id(ctx.world)}",
                "tenantId": common.tenant_id(ctx.world),
                "onlineMeetingInfo": None,
                "viewpoint": None,
                "_members": list(chat.member_ids),
            }
        )
        seed_messages(ctx, chat.messages, cid, chat=record)


def require_team(ctx: ProviderContext, tid: str, p: common.Principal) -> dict[str, Any]:
    team = ctx.store[TEAMS].get(tid)
    if team is None or (p.delegated and p.person_id not in team["_members"]):
        raise common.item_not_found()
    return team


def require_channel(ctx: ProviderContext, tid: str, cid: str, p: common.Principal) -> dict[str, Any]:
    require_team(ctx, tid, p)
    channel = ctx.store[CHANNELS].get(cid)
    if channel is None or channel["_team"] != tid or (p.delegated and p.person_id not in channel["_members"]):
        raise common.item_not_found()
    return channel


def require_chat(ctx: ProviderContext, cid: str, p: common.Principal) -> dict[str, Any]:
    chat = ctx.store[CHATS].get(cid)
    if chat is None or (p.delegated and p.person_id not in chat["_members"]):
        raise common.item_not_found()
    return chat


async def changed(ctx: ProviderContext, message: dict[str, Any], change_type: str) -> None:
    sequence = ctx.store.meta.get("ms_teams_sequence", 0) + 1
    ctx.store.meta["ms_teams_sequence"] = sequence
    message["_revision"] += 1
    ctx.store["ms_teams_changes"].put(
        {"id": str(sequence), "scope": message["_scope"], "message": copy.deepcopy(message)}
    )
    channel = message.get("channelIdentity")
    resource = (
        f"teams/{channel['teamId']}/channels/{channel['channelId']}/messages/{message['id']}"
        if channel
        else f"chats/{message['chatId']}/messages/{message['id']}"
    )
    if message["replyToId"]:
        resource = resource.rsplit("/", 1)[0] + f"/{message['replyToId']}/replies/{message['id']}"
    await common.publish_change(ctx, resource, change_type, dm.public(message))
