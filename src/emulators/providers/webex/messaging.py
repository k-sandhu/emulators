"""Membership-enforced rooms, messages and direct conversations."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext

from .common import (
    authenticate,
    body,
    bot_message_visible,
    fail,
    instant,
    opaque,
    page,
    person,
    public,
    require_scope,
    required,
    stamp,
    strings,
    subject,
)
from .hooks import emit


def identity(
    request: Request, ctx: ProviderContext, resource: str, *, write: bool = False
) -> tuple[Grant, dict[str, Any]]:
    grant = authenticate(request, ctx)
    require_scope(grant, "spark:all", f"spark:{resource}_{'write' if write else 'read'}")
    return grant, subject(ctx, grant)


def room_access(
    ctx: ProviderContext, key: str, user: dict[str, Any], *, moderate: bool = False
) -> dict[str, Any]:
    room = ctx.store.collection("rooms").get(key)
    member = ctx.store.collection("memberships").first(roomId=key, personId=user["id"])
    if not room or not member:
        fail(404)
    if moderate and not member["isModerator"]:
        fail(403, "Only a moderator may manage this room.")
    return room


def new_membership(
    ctx: ProviderContext, room_id: str, p: dict[str, Any], *, moderator: bool = False
) -> dict[str, Any]:
    return {
        "id": opaque("MEMBERSHIP", f"{room_id}/{p['id']}"),
        "roomId": room_id,
        "personId": p["id"],
        "personEmail": p["emails"][0],
        "personDisplayName": p["displayName"],
        "isModerator": moderator,
        "isMonitor": False,
        "created": stamp(ctx.clock.now()),
    }


def register(app: FastAPI, ctx: ProviderContext) -> None:
    def room_wire(row: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        result = public(row)
        if row["type"] == "direct":
            other = next(
                (
                    person(ctx, m["personId"])
                    for m in ctx.store.collection("memberships").where(roomId=row["id"])
                    if m["personId"] != user["id"]
                ),
                None,
            )
            if other:
                result["title"] = other["displayName"]
        return result

    @app.get("/v1/rooms")
    async def rooms(request: Request) -> JSONResponse:
        _, user = identity(request, ctx, "rooms")
        ids = {m["roomId"] for m in ctx.store.collection("memberships").where(personId=user["id"])}
        rows = [r for r in ctx.store.collection("rooms") if r["id"] in ids]
        for key in ("type", "teamId"):
            if key in request.query_params:
                if key == "type" and request.query_params[key] not in ("group", "direct"):
                    fail(400, "Invalid room type.")
                rows = [r for r in rows if r.get(key) == request.query_params[key]]
        rows.sort(key=lambda r: (r["lastActivity"], r["id"]), reverse=True)
        return page(request, [room_wire(r, user) for r in rows])

    @app.post("/v1/rooms")
    async def create_room(request: Request) -> dict[str, Any]:
        _, user = identity(request, ctx, "rooms", write=True)
        payload = await body(request)
        required(payload, "title")
        strings(payload, "teamId")
        if "isLocked" in payload and not isinstance(payload["isLocked"], bool):
            fail(400, "isLocked must be a boolean.")
        room = {
            "id": opaque("ROOM", ctx.ids.guid()),
            "title": payload["title"],
            "type": "group",
            "isLocked": payload.get("isLocked", False),
            "lastActivity": stamp(ctx.clock.now()),
            "created": stamp(ctx.clock.now()),
            "creatorId": user["id"],
        }
        if payload.get("teamId"):
            if not any(
                r.get("teamId") == payload["teamId"]
                and ctx.store.collection("memberships").first(roomId=r["id"], personId=user["id"])
                for r in ctx.store.collection("rooms")
            ):
                fail(404)
            room["teamId"] = payload["teamId"]
        ctx.store.collection("rooms").put(room)
        ctx.store.collection("memberships").put(new_membership(ctx, room["id"], user, moderator=True))
        await emit(ctx, "rooms", "created", room, actor=user["id"])
        return public(room)

    @app.get("/v1/rooms/{room_id}")
    async def get_room(room_id: str, request: Request) -> dict[str, Any]:
        _, user = identity(request, ctx, "rooms")
        return room_wire(room_access(ctx, room_id, user), user)

    @app.put("/v1/rooms/{room_id}")
    async def update_room(room_id: str, request: Request) -> dict[str, Any]:
        _, user = identity(request, ctx, "rooms", write=True)
        room = room_access(ctx, room_id, user)
        payload = await body(request)
        required(payload, "title")
        if room["type"] == "direct":
            fail(400, "Direct rooms cannot be renamed.")
        if room["isLocked"] or "isLocked" in payload:
            room_access(ctx, room_id, user, moderate=True)
        if "isLocked" in payload and not isinstance(payload["isLocked"], bool):
            fail(400, "isLocked must be a boolean.")
        room.update({k: payload[k] for k in ("title", "isLocked") if k in payload})
        await emit(ctx, "rooms", "updated", room, actor=user["id"])
        return public(room)

    @app.delete("/v1/rooms/{room_id}")
    async def delete_room(room_id: str, request: Request) -> Response:
        _, user = identity(request, ctx, "rooms", write=True)
        room = room_access(ctx, room_id, user)
        member = ctx.store.collection("memberships").first(roomId=room_id, personId=user["id"])
        assert member is not None
        if not member["isModerator"]:
            await emit(ctx, "memberships", "deleted", member, actor=user["id"])
            ctx.store.collection("memberships").delete(member["id"])
            return Response(status_code=204)
        await emit(ctx, "rooms", "deleted", room, actor=user["id"])
        ctx.store.collection("rooms").delete(room_id)
        for collection in ("memberships", "messages"):
            for row in ctx.store.collection(collection).where(roomId=room_id):
                ctx.store.collection(collection).delete(row["id"])
        return Response(status_code=204)

    @app.get("/v1/memberships")
    async def memberships(request: Request) -> JSONResponse:
        _, user = identity(request, ctx, "memberships")
        q = request.query_params
        if q.get("roomId"):
            room_access(ctx, q["roomId"], user)
            rows = ctx.store.collection("memberships").where(roomId=q["roomId"])
        else:
            rows = ctx.store.collection("memberships").where(personId=user["id"])
        for key in ("personId", "personEmail"):
            if q.get(key):
                rows = [m for m in rows if m[key].lower() == q[key].lower()]
        return page(request, rows)

    @app.post("/v1/memberships")
    async def add_member(request: Request) -> dict[str, Any]:
        _, user = identity(request, ctx, "memberships", write=True)
        payload = await body(request)
        required(payload, "roomId")
        strings(payload, "personId", "personEmail")
        if sum(bool(payload.get(k)) for k in ("personId", "personEmail")) != 1:
            fail(400, "Specify exactly one of personId or personEmail.")
        room = room_access(ctx, payload["roomId"], user)
        if room["type"] == "direct":
            fail(400, "Direct room membership cannot be changed.")
        if room["isLocked"] or payload.get("isModerator"):
            room_access(ctx, room["id"], user, moderate=True)
        if "isModerator" in payload and not isinstance(payload["isModerator"], bool):
            fail(400, "isModerator must be a boolean.")
        p = person(ctx, payload.get("personId") or payload.get("personEmail"))
        if not p:
            fail(400, "Unknown personId or personEmail.")
        if ctx.store.collection("memberships").first(roomId=room["id"], personId=p["id"]):
            fail(409, "The person is already a member of the room.")
        row = new_membership(ctx, room["id"], p, moderator=bool(payload.get("isModerator", False)))
        ctx.store.collection("memberships").put(row)
        await emit(ctx, "memberships", "created", row, actor=user["id"])
        return row

    def membership(key: str, user: dict[str, Any]) -> dict[str, Any]:
        row = ctx.store.collection("memberships").get(key)
        if not row:
            fail(404)
        room_access(ctx, row["roomId"], user)
        return row

    @app.get("/v1/memberships/{membership_id}")
    async def get_member(membership_id: str, request: Request) -> dict[str, Any]:
        _, user = identity(request, ctx, "memberships")
        return public(membership(membership_id, user))

    @app.put("/v1/memberships/{membership_id}")
    async def update_member(membership_id: str, request: Request) -> dict[str, Any]:
        _, user = identity(request, ctx, "memberships", write=True)
        row = membership(membership_id, user)
        room = room_access(ctx, row["roomId"], user, moderate=True)
        if room["type"] == "direct":
            fail(400, "Direct room membership cannot be changed.")
        payload = await body(request)
        if not isinstance(payload.get("isModerator"), bool):
            fail(400, "isModerator must be a boolean.")
        row["isModerator"] = payload["isModerator"]
        await emit(ctx, "memberships", "updated", row, actor=user["id"])
        return public(row)

    @app.delete("/v1/memberships/{membership_id}")
    async def remove_member(membership_id: str, request: Request) -> Response:
        _, user = identity(request, ctx, "memberships", write=True)
        row = membership(membership_id, user)
        room = room_access(ctx, row["roomId"], user)
        if room["type"] == "direct":
            fail(400, "Direct room membership cannot be changed.")
        if row["personId"] != user["id"]:
            room_access(ctx, row["roomId"], user, moderate=True)
        await emit(ctx, "memberships", "deleted", row, actor=user["id"])
        ctx.store.collection("memberships").delete(membership_id)
        return Response(status_code=204)

    @app.get("/v1/messages")
    async def messages(request: Request) -> JSONResponse:
        _, user = identity(request, ctx, "messages")
        q = request.query_params
        if not q.get("roomId"):
            fail(400, "roomId is required.")
        room = room_access(ctx, q["roomId"], user)
        if user["type"] == "bot" and room["type"] == "group" and not q.get("mentionedPeople"):
            fail(400, "Bots must specify mentionedPeople for group-room message lists.")
        if q.get("mentionedPeople") and q["mentionedPeople"] not in ("me", user["id"]):
            fail(400, "mentionedPeople must identify the current user.")
        rows = ctx.store.collection("messages").where(roomId=q["roomId"])
        rows = [m for m in rows if bot_message_visible(user, m)]
        if q.get("before"):
            before = instant(q["before"], "before")
            rows = [m for m in rows if instant(m["created"], "created") < before]
        rows.sort(key=lambda m: (m["created"], m["id"]), reverse=True)
        if q.get("beforeMessage"):
            before_message = next((m for m in rows if m["id"] == q["beforeMessage"]), None)
            if not before_message:
                fail(404)
            rows = rows[rows.index(before_message) + 1 :]
        if q.get("mentionedPeople"):
            target = user["id"] if q["mentionedPeople"] == "me" else q["mentionedPeople"]
            rows = [m for m in rows if target in m.get("mentionedPeople", [])]
        if q.get("parentId"):
            rows = [m for m in rows if m.get("parentId") == q["parentId"]]
        return page(request, rows, default=50, maximum=100 if q.get("mentionedPeople") else 1000)

    @app.post("/v1/messages")
    async def send_message(request: Request) -> dict[str, Any]:
        _, user = identity(request, ctx, "messages", write=True)
        payload = await body(request)
        strings(payload, "roomId", "toPersonId", "toPersonEmail", "parentId")
        targets = [k for k in ("roomId", "toPersonId", "toPersonEmail") if payload.get(k)]
        if len(targets) != 1:
            fail(400, "Specify exactly one of roomId, toPersonId or toPersonEmail.")
        if not any(payload.get(k) for k in ("text", "markdown", "files")):
            fail(400, "A message requires text, markdown or files.")
        for key in ("text", "markdown"):
            if key in payload and not isinstance(payload[key], str):
                fail(400, f"{key} must be a string.")
            if key in payload and len(payload[key].encode("utf-8")) > 7439:
                fail(400, f"{key} must be at most 7439 UTF-8 bytes.")
        if "files" in payload and (
            not isinstance(payload["files"], list)
            or len(payload["files"]) > 1
            or not all(
                isinstance(url, str) and url.startswith(("https://", "http://")) for url in payload["files"]
            )
        ):
            fail(400, "files must be an array of HTTP URLs.")
        for url in payload.get("files", []):
            try:
                parsed = urlparse(url)
                if not parsed.hostname:
                    raise ValueError
                _ = parsed.port
            except ValueError:
                fail(400, "files must contain valid HTTP URLs.")
        new_room = False
        if payload.get("roomId"):
            room = room_access(ctx, payload["roomId"], user)
        else:
            recipient = person(ctx, payload.get("toPersonId") or payload.get("toPersonEmail"))
            if not recipient or recipient["id"] == user["id"]:
                fail(400, "Unknown or invalid message recipient.")
            key = opaque("ROOM", "/".join(sorted([user["id"], recipient["id"]])))
            room = ctx.store.collection("rooms").get(key)
            if not room:
                new_room = True
                room = {
                    "id": key,
                    "type": "direct",
                    "title": recipient["displayName"],
                    "isLocked": False,
                    "creatorId": user["id"],
                    "created": stamp(ctx.clock.now()),
                    "lastActivity": stamp(ctx.clock.now()),
                }
        if payload.get("parentId"):
            parent = ctx.store.collection("messages").get(payload["parentId"])
            if not parent or parent["roomId"] != room["id"]:
                fail(400, "parentId must identify a message in this room.")
            if not bot_message_visible(user, parent):
                fail(404)
        if new_room:
            ctx.store.collection("rooms").put(room)
            for p in (user, recipient):
                ctx.store.collection("memberships").put(new_membership(ctx, room["id"], p))
        row = {
            "id": opaque("MESSAGE", ctx.ids.guid()),
            "roomId": room["id"],
            "roomType": room["type"],
            "personId": user["id"],
            "personEmail": user["emails"][0],
            "created": stamp(ctx.clock.now()),
        }
        row.update({k: payload[k] for k in ("text", "markdown", "files", "parentId") if k in payload})
        if payload.get("markdown"):
            row.setdefault("text", re.sub(r"[*_`#]", "", payload["markdown"]))
            mentions = re.findall(r"<@personId:([^>]+)>", payload["markdown"])
            for email in re.findall(r"<@personEmail:([^>]+)>", payload["markdown"]):
                mentioned = person(ctx, email)
                if mentioned:
                    mentions.append(mentioned["id"])
            if mentions:
                row["mentionedPeople"] = list(dict.fromkeys(mentions))
        ctx.store.collection("messages").put(row)
        room["lastActivity"] = row["created"]
        await emit(ctx, "messages", "created", row, actor=user["id"])
        return public(row)

    @app.get("/v1/messages/{message_id}")
    async def get_message(message_id: str, request: Request) -> dict[str, Any]:
        _, user = identity(request, ctx, "messages")
        row = ctx.store.collection("messages").get(message_id)
        if not row:
            fail(404)
        room_access(ctx, row["roomId"], user)
        if not bot_message_visible(user, row):
            fail(404)
        return public(row)

    @app.delete("/v1/messages/{message_id}")
    async def delete_message(message_id: str, request: Request) -> Response:
        _, user = identity(request, ctx, "messages", write=True)
        row = ctx.store.collection("messages").get(message_id)
        if not row:
            fail(404)
        room_access(ctx, row["roomId"], user)
        if row["personId"] != user["id"]:
            room_access(ctx, row["roomId"], user, moderate=True)
        await emit(ctx, "messages", "deleted", row, actor=user["id"])
        ctx.store.collection("messages").delete(message_id)
        return Response(status_code=204)
