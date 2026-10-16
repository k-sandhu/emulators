"""Teams memberships, channel threads and private chat message HTTP semantics."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext, external_base
from emulators.providers.microsoft import common
from emulators.providers.microsoft import drive_model as dm
from emulators.providers.microsoft import teams_model as tm

seed = tm.seed


def message_wire(ctx: ProviderContext, message: dict[str, Any], expand: str = "") -> dict[str, Any]:
    result = dm.public(message)
    if "replies" in expand:
        result["replies"] = [
            dm.public(r)
            for r in ctx.store[tm.MESSAGES].where(_scope=message["_scope"], replyToId=message["id"])
        ]
    return result


def list_messages(request: Request, ctx: ProviderContext, records: list[dict[str, Any]]) -> dict[str, Any]:
    records = sorted(records, key=lambda r: r["lastModifiedDateTime"], reverse=True)
    return dm.collection(
        request,
        [message_wire(ctx, r, request.query_params.get("$expand", "")) for r in records],
        context="Collection(microsoft.graph.chatMessage)",
        default_page_size=20,
        max_page_size=50,
        search_fields=("body/content", "subject"),
    )


def delta_messages(request: Request, ctx: ProviderContext, scope: str, p: common.Principal) -> dict[str, Any]:
    token = request.query_params.get("$deltatoken") or request.query_params.get("token")
    sequence = ctx.store.meta.get("ms_teams_sequence", 0)
    if token and token != "latest":
        cursor = ctx.store["ms_files_cursors"].get(token)
        if not cursor or cursor["resource"] != "channel/" + scope or cursor["user"] != p.user_id:
            raise common.graph_error(410, "resyncRequired", "The delta token is invalid.")
        if cursor["records"]:
            records = cursor["records"][cursor["offset"] :]
            sequence = cursor["sequence"]
        else:
            records = list(
                {
                    c["message"]["id"]: c["message"]
                    for c in ctx.store["ms_teams_changes"]
                    if c["scope"] == scope and int(c["id"]) > cursor["sequence"]
                }.values()
            )
    else:
        records = (
            []
            if token == "latest"
            else [copy.deepcopy(r) for r in ctx.store[tm.MESSAGES].where(_scope=scope)]
        )
    size = min(common.query_options(request).top or 20, 50)
    more = len(records) > size
    cursor_id = dm.delta_token(
        ctx, "channel/" + scope, p, records if more else [], sequence, size if more else 0
    )
    params = {k: v for k, v in request.query_params.items() if k not in ("token", "$deltatoken")}
    params["$deltatoken"] = cursor_id
    return {
        "@odata.context": common.context_url(request, "Collection(microsoft.graph.chatMessage)"),
        "value": [dm.public(r) for r in records[:size]],
        "@odata.nextLink" if more else "@odata.deltaLink": external_base(request)
        + dm.path_of(request)
        + "?"
        + urlencode(params),
    }


async def messages_dispatch(
    request: Request,
    ctx: ProviderContext,
    p: common.Principal,
    scope: str,
    tail: str,
    *,
    channel: dict[str, Any] | None = None,
    chat: dict[str, Any] | None = None,
) -> Any:
    parts = tail.strip("/").split("/") if tail else []
    records = ctx.store[tm.MESSAGES].where(_scope=scope)
    reply_to: str | None = None
    if parts and parts[0] == "delta" and request.method == "GET":
        return delta_messages(request, ctx, scope, p)
    if len(parts) > 1 and parts[1] == "replies":
        parent = ctx.store[tm.MESSAGES].get(tm.message_key(scope, parts[0]))
        if not parent or parent["replyToId"] or parent["deletedDateTime"]:
            raise common.item_not_found()
        reply_to = parent["id"]
        parts = parts[2:]
    if not parts:
        if request.method == "GET":
            return list_messages(request, ctx, [r for r in records if r["replyToId"] == reply_to])
        if request.method == "POST":
            if p.app_only:
                raise common.access_denied(
                    "Sending messages requires delegated authentication outside migration mode."
                )
            payload = await dm.json_body(request)
            body = payload.get("body")
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("content"), str)
                or body.get("contentType", "text") not in ("html", "text")
            ):
                raise common.graph_error(400, "BadRequest", "A valid message body is required.")
            if not body["content"].strip() and not payload.get("attachments"):
                raise common.graph_error(400, "BadRequest", "Message content cannot be empty.")
            timestamp = isoformat_z(ctx.clock.now(), timespec="milliseconds")
            mid = str(max(ctx.clock.timestamp() * 1000, max((int(r["id"]) for r in records), default=0) + 1))
            record = tm.new_message(
                ctx,
                scope,
                p.person_id,
                mid,
                timestamp,
                channel=channel,
                chat=chat,
                body={"contentType": body.get("contentType", "text"), "content": body["content"]},
                reply=reply_to,
            )
            for key in ("subject", "summary", "importance", "locale", "attachments", "mentions"):
                if key in payload:
                    record[key] = payload[key]
            ctx.store[tm.MESSAGES].put_as(tm.message_key(scope, mid), record)
            if chat:
                chat["lastUpdatedDateTime"] = timestamp
            await tm.changed(ctx, record, "created")
            return JSONResponse(dm.public(record), status_code=201)
    message = ctx.store[tm.MESSAGES].get(tm.message_key(scope, parts[0]))
    if message is None or message["replyToId"] != reply_to:
        raise common.item_not_found()
    op = "/".join(parts[1:])
    if not op and request.method == "GET":
        return dm.single(
            request,
            message_wire(ctx, message, request.query_params.get("$expand", "")),
            "microsoft.graph.chatMessage",
        )
    if request.method == "PATCH" and not op:
        if p.app_only or p.person_id != message["_author"]:
            raise common.access_denied("Only the author can edit this message.")
        if message["deletedDateTime"]:
            raise common.graph_error(400, "BadRequest", "A deleted message cannot be edited.")
        payload = await dm.json_body(request)
        body = payload.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("content"), str):
            raise common.graph_error(400, "BadRequest", "A message body is required.")
        timestamp = isoformat_z(ctx.clock.now(), timespec="milliseconds")
        message["messageHistory"].append(
            {"modifiedDateTime": timestamp, "actions": "contentEdited", "reaction": None}
        )
        message["body"] = {
            "contentType": body.get("contentType", message["body"]["contentType"]),
            "content": body["content"],
        }
        message["lastEditedDateTime"] = message["lastModifiedDateTime"] = timestamp
        message["etag"] = str(int(message["etag"]) + 1)
        await tm.changed(ctx, message, "updated")
        return Response(status_code=204)
    if op in ("softDelete", "undoSoftDelete") and request.method == "POST":
        owner = channel and p.person_id in ctx.store[tm.TEAMS].get(channel["_team"])["_owners"]
        if p.delegated and p.person_id != message["_author"] and not owner:
            raise common.access_denied()
        if op == "softDelete":
            message["_prior_body"] = copy.deepcopy(message["body"])
            message["deletedDateTime"] = isoformat_z(ctx.clock.now(), timespec="milliseconds")
            message["body"] = {"contentType": "html", "content": ""}
        else:
            message["deletedDateTime"] = None
            message["body"] = message.pop("_prior_body", {"contentType": "html", "content": ""})
        message["lastModifiedDateTime"] = isoformat_z(ctx.clock.now(), timespec="milliseconds")
        message["etag"] = str(int(message["etag"]) + 1)
        await tm.changed(ctx, message, "updated")
        return Response(status_code=204)
    raise common.graph_error(405, "methodNotAllowed", "The HTTP method is not allowed on this resource.")


def chat_wire(request: Request, ctx: ProviderContext, chat: dict[str, Any]) -> dict[str, Any]:
    record = dm.public(chat)
    expand = request.query_params.get("$expand", "")
    if "members" in expand:
        record["members"] = [tm.member(ctx, pid, []) for pid in chat["_members"]]
    if "lastMessagePreview" in expand:
        messages = ctx.store[tm.MESSAGES].where(_scope=chat["id"])
        last = max(messages, key=lambda m: m["createdDateTime"]) if messages else None
        record["lastMessagePreview"] = (
            {
                "id": last["id"],
                "createdDateTime": last["createdDateTime"],
                "isDeleted": bool(last["deletedDateTime"]),
                "messageType": last["messageType"],
                "from": last["from"],
                "body": last["body"],
                "attachments": last["attachments"],
            }
            if last
            else None
        )
    return record


def register(app: FastAPI, ctx: ProviderContext) -> None:
    def router() -> APIRouter:
        routes = APIRouter()

        @routes.get("/me/joinedTeams")
        async def joined(request: Request) -> dict[str, Any]:
            p = common.principal(request, ctx)
            common.me_user_id(p)
            return dm.collection(
                request,
                [t for t in ctx.store[tm.TEAMS] if p.person_id in t["_members"]],
                context="Collection(team)",
            )

        @routes.get("/users/{uid}/joinedTeams")
        async def user_joined(request: Request, uid: str) -> dict[str, Any]:
            p = common.principal(request, ctx)
            pid = dm.person_id(ctx, uid)
            if p.delegated and p.person_id != pid:
                raise common.access_denied()
            return dm.collection(
                request, [t for t in ctx.store[tm.TEAMS] if pid in t["_members"]], context="Collection(team)"
            )

        @routes.api_route("/teams/{tid}", methods=dm.METHODS)
        @routes.api_route("/teams/{tid}/{tail:path}", methods=dm.METHODS)
        async def teams(request: Request, tid: str, tail: str = "") -> Any:
            p = common.principal(request, ctx)
            team = tm.require_team(ctx, tid, p)
            if not tail and request.method == "GET":
                return dm.single(request, team, "team")
            if tail == "members" and request.method == "GET":
                return dm.collection(
                    request,
                    [tm.member(ctx, pid, team["_owners"]) for pid in team["_members"]],
                    context="Collection(conversationMember)",
                )
            channels = ctx.store[tm.CHANNELS].where(_team=tid)
            if tail in ("channels", "primaryChannel") and request.method == "GET":
                accessible = [c for c in channels if p.app_only or p.person_id in c["_members"]]
                if tail == "primaryChannel":
                    channel = next((c for c in accessible if c["displayName"] == "General"), None)
                    if not channel:
                        raise common.item_not_found()
                    return dm.single(request, channel, "channel")
                return dm.collection(request, accessible, context="Collection(channel)")
            if tail == "channels/getAllMessages" and request.method == "GET":
                if not p.app_only:
                    raise common.access_denied("This API requires application permissions.")
                scopes = {c["id"] for c in channels}
                return list_messages(
                    request, ctx, [m for m in ctx.store[tm.MESSAGES] if m["_scope"] in scopes]
                )
            parts = tail.split("/")
            if len(parts) < 2 or parts[0] != "channels":
                raise common.item_not_found()
            channel = tm.require_channel(ctx, tid, parts[1], p)
            if len(parts) == 2 and request.method == "GET":
                return dm.single(request, channel, "channel")
            if len(parts) < 3:
                raise common.graph_error(
                    405, "methodNotAllowed", "The HTTP method is not allowed on this resource."
                )
            if parts[2] == "members" and request.method == "GET":
                return dm.collection(
                    request,
                    [tm.member(ctx, pid, team["_owners"]) for pid in channel["_members"]],
                    context="Collection(conversationMember)",
                )
            if parts[2] == "messages":
                return await messages_dispatch(
                    request, ctx, p, channel["id"], "/".join(parts[3:]), channel=channel
                )
            raise common.item_not_found()

        @routes.get("/me/chats")
        async def my_chats(request: Request) -> dict[str, Any]:
            p = common.principal(request, ctx)
            common.me_user_id(p)
            return dm.collection(
                request,
                [chat_wire(request, ctx, c) for c in ctx.store[tm.CHATS] if p.person_id in c["_members"]],
                context="Collection(chat)",
                search_fields=("topic",),
            )

        @routes.get("/users/{uid}/chats/getAllMessages")
        async def all_chats(request: Request, uid: str) -> dict[str, Any]:
            p = common.principal(request, ctx)
            if not p.app_only:
                raise common.access_denied("This API requires application permissions.")
            pid = dm.person_id(ctx, uid)
            scopes = {c["id"] for c in ctx.store[tm.CHATS] if pid in c["_members"]}
            return list_messages(request, ctx, [m for m in ctx.store[tm.MESSAGES] if m["_scope"] in scopes])

        @routes.get("/users/{uid}/chats")
        async def user_chats(request: Request, uid: str) -> dict[str, Any]:
            p = common.principal(request, ctx)
            pid = dm.person_id(ctx, uid)
            if p.delegated and p.person_id != pid:
                raise common.access_denied()
            return dm.collection(
                request,
                [chat_wire(request, ctx, c) for c in ctx.store[tm.CHATS] if pid in c["_members"]],
                context="Collection(chat)",
                search_fields=("topic",),
            )

        @routes.api_route("/chats/{cid}", methods=dm.METHODS)
        @routes.api_route("/chats/{cid}/{tail:path}", methods=dm.METHODS)
        async def chats(request: Request, cid: str, tail: str = "") -> Any:
            p = common.principal(request, ctx)
            chat = tm.require_chat(ctx, cid, p)
            if not tail and request.method == "GET":
                return dm.single(request, chat_wire(request, ctx, chat), "chat")
            if tail == "members" and request.method == "GET":
                return dm.collection(
                    request,
                    [tm.member(ctx, pid, []) for pid in chat["_members"]],
                    context="Collection(conversationMember)",
                )
            if tail == "messages" or tail.startswith("messages/"):
                return await messages_dispatch(
                    request, ctx, p, cid, tail.removeprefix("messages").lstrip("/"), chat=chat
                )
            raise common.item_not_found()

        return routes

    for version in common.GRAPH_VERSIONS:
        app.include_router(router(), prefix="/" + version, tags=["Teams / chats"])
