"""Card writes, labels, comments, checklists and authenticated attachments."""

from __future__ import annotations

import copy
import mimetypes
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile

from emulators.core.auth import Grant
from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext

from .common import (
    Record,
    clean,
    date_value,
    fail,
    flag,
    get_board,
    http_url,
    lookup,
    member,
    names,
    new_id,
    position,
    project,
    resource,
    text,
)
from .hooks import emit
from .seed import card_record
from .shapes import (
    action_list,
    action_shape,
    attachment_content,
    attachment_shape,
    board_shape,
    card_shape,
    member_shape,
)


def validate_card(
    ctx: ProviderContext, grant: Grant, params: Record, original: Record | None = None
) -> tuple[Record, Record]:
    if original:
        original = copy.deepcopy(original)
        old_board = get_board(ctx, original["idBoard"], grant, write=True)
    else:
        old_board = None
    list_id = params.get("idList", original["idList"] if original else None)
    if (
        original
        and "idBoard" in params
        and "idList" not in params
        and params["idBoard"] != original["idBoard"]
    ):
        destination = get_board(ctx, params["idBoard"], grant, write=True)
        open_lists = sorted(
            [r for r in ctx.store["lists"].where(idBoard=destination["id"]) if not r["closed"]],
            key=lambda r: r["pos"],
        )
        if not open_lists:
            fail(400, "destination board has no open list")
        list_id = open_lists[0]["id"]
    target_list = resource(ctx, "lists", list_id, grant, write=True)
    board = get_board(ctx, params.get("idBoard", target_list["idBoard"]), grant, write=True)
    if target_list["idBoard"] != board["id"] or target_list["closed"]:
        fail(400, "invalid value for idList")
    values: Record = {"idBoard": board["id"], "idList": target_list["id"]}
    for field in ("name", "desc"):
        if field in params:
            values[field] = text(params[field], field, empty=field == "desc")
    for field in ("closed", "dueComplete"):
        if field in params:
            values[field] = flag(params[field], field)
    for field in ("due", "start"):
        if field in params:
            values[field] = date_value(params[field], field)
    moved = old_board is not None and old_board["id"] != board["id"]
    for field, collection in (("idMembers", "members"), ("idLabels", "labels")):
        if field not in params:
            if moved:
                values[field] = (
                    [rid for rid in original[field] if rid in board["_members"]]
                    if field == "idMembers"
                    else []
                )
            continue
        selected = names(params[field], field)
        for rid in selected:
            found = lookup(ctx, collection, rid)
            if (field == "idMembers" and found["id"] not in board["_members"]) or (
                field == "idLabels" and found["idBoard"] != board["id"]
            ):
                fail(400, f"invalid value for {field}")
        values[field] = list(dict.fromkeys(selected))
    if "idAttachmentCover" in params:
        aid = params["idAttachmentCover"]
        if aid in (None, "null", ""):
            values["idAttachmentCover"] = None
        else:
            att = lookup(ctx, "attachments", aid)
            if not original or att["idCard"] != original["id"]:
                fail(400, "invalid value for idAttachmentCover")
            values["idAttachmentCover"] = att["id"]
    if "pos" in params or not original or original["idList"] != target_list["id"]:
        peers = [
            c
            for c in ctx.store["cards"].where(idList=target_list["id"])
            if not original or c["id"] != original["id"]
        ]
        values["pos"] = position(params.get("pos", "bottom"), peers)
    if original:
        original.update(values)
        return original, board
    return values, board


def delete_card(ctx: ProviderContext, card: Record) -> None:
    for collection, field in (("checklists", "idCard"), ("attachments", "idCard")):
        for item in ctx.store[collection].where(**{field: card["id"]}):
            ctx.store[collection].delete(item["id"])
    ctx.store["cards"].delete(card["id"])


class CardsMixin:
    ctx: ProviderContext
    request: Request
    params: Record
    grant: Grant | None
    method: str

    async def cards(self, parts: list[str]) -> Any:
        ctx, p, grant, method = self.ctx, self.params, self.grant, self.method
        if not parts:
            if method != "POST":
                fail(404, "not found")
            assert grant is not None
            for unsupported in ("idCardSource", "fileSource", "cover", "coordinates"):
                if unsupported in p:
                    fail(400, f"unsupported parameter: {unsupported}")
            values, board = validate_card(ctx, grant, p)
            source_url = http_url(p["urlSource"], "urlSource") if "urlSource" in p else None
            rid = new_id(ctx, "card")
            card = card_record(
                ctx, rid, board, values["idList"], values.get("name", "Untitled"), values.get("desc", "")
            )
            card.update(values, _created=isoformat_z(ctx.clock.now()))
            ctx.store["cards"].put(card)
            if source_url:
                self.add_attachment(card, {"url": source_url, "name": source_url[:256]}, None)
            await emit(
                ctx,
                self.request,
                grant,
                "createCard",
                board,
                card,
                data={"list": {"id": card["idList"], "name": ctx.store["lists"].get(card["idList"])["name"]}},
            )
            return card_shape(ctx, self.request, card, grant)
        card = resource(ctx, "cards", parts[0], grant, write=method != "GET")
        board = get_board(ctx, card["idBoard"], grant)
        if len(parts) == 1:
            if method == "GET":
                return card_shape(ctx, self.request, card, grant, p)
            assert grant is not None
            if method == "PUT":
                for unsupported in ("cover", "coordinates", "idCardSource"):
                    if unsupported in p:
                        fail(400, f"unsupported parameter: {unsupported}")
                changed, target = validate_card(ctx, grant, p, card)
                old = {
                    k: copy.deepcopy(card[k])
                    for k in changed
                    if k in card and changed[k] != card[k] and not k.startswith("_")
                }
                moved = card["idBoard"] != target["id"]
                if moved:
                    if "idLabels" not in p:
                        changed["idLabels"] = []
                        for old_id in card["idLabels"]:
                            label = ctx.store["labels"].get(old_id)
                            match = ctx.store["labels"].first(
                                idBoard=target["id"], name=label["name"], color=label["color"]
                            )
                            if match is None:
                                match = ctx.store["labels"].put(
                                    {
                                        **clean(label),
                                        "id": new_id(ctx, "label"),
                                        "idBoard": target["id"],
                                        "uses": 0,
                                    }
                                )
                            changed["idLabels"].append(match["id"])
                    target["_nextShort"] += 1
                    changed["idShort"] = target["_nextShort"]
                    await emit(
                        ctx,
                        self.request,
                        grant,
                        "moveCardFromBoard",
                        board,
                        card,
                        data={
                            "boardTarget": {
                                "id": target["id"],
                                "name": target["name"],
                                "shortLink": target["shortLink"],
                            }
                        },
                    )
                card.update(changed)
                if moved:
                    for collection in ("checklists", "attachments"):
                        for item in ctx.store[collection].where(idCard=card["id"]):
                            item["idBoard"] = target["id"]
                data: Record = {"old": old}
                if "idList" in old:
                    data.update(
                        listBefore={
                            "id": old["idList"],
                            "name": ctx.store["lists"].get(old["idList"])["name"],
                        },
                        listAfter={
                            "id": card["idList"],
                            "name": ctx.store["lists"].get(card["idList"])["name"],
                        },
                    )
                if moved:
                    data["boardSource"] = {
                        "id": board["id"],
                        "name": board["name"],
                        "shortLink": board["shortLink"],
                    }
                await emit(
                    ctx,
                    self.request,
                    grant,
                    "moveCardToBoard" if moved else "updateCard",
                    target,
                    card,
                    data=data,
                )
                return card_shape(ctx, self.request, card, grant)
            if method == "DELETE":
                await emit(ctx, self.request, grant, "deleteCard", board, card)
                delete_card(ctx, card)
                return {}
            fail(404, "not found")
        edge = parts[1]
        if edge == "board" and method == "GET":
            return board_shape(ctx, self.request, board, grant, p)
        if edge == "list" and method == "GET":
            return project(ctx.store["lists"].get(card["idList"]), p.get("fields", "all"))
        if edge == "members" and method == "GET":
            return [
                member_shape(
                    ctx,
                    self.request,
                    ctx.store["members"].get(uid),
                    grant,
                    p.get("fields", "avatarHash,fullName,initials,username"),
                )
                for uid in card["idMembers"]
            ]
        if edge == "actions":
            return await self.comments(card, board, parts[2:])
        if edge == "attachments":
            return await self.attachments(card, board, parts[2:])
        if edge == "checklists":
            if method == "GET" and len(parts) == 2:
                return [
                    project(ctx.store["checklists"].get(cid), p.get("fields", "all"))
                    for cid in card["idChecklists"]
                ]
            if method == "POST" and len(parts) == 2:
                return await self.create_checklist(card)
            if method == "DELETE" and len(parts) == 3 and parts[2] in card["idChecklists"]:
                card["idChecklists"].remove(parts[2])
                ctx.store["checklists"].delete(parts[2])
                await emit(ctx, self.request, grant, "removeChecklistFromCard", board, card)
                return {}
        if edge == "checkItemStates" and method == "GET":
            return card_shape(ctx, self.request, card, grant)["checkItemStates"]
        if edge == "checkItem" and len(parts) == 3:
            return await self.check_item(card, parts[2])
        if edge in ("idLabels", "idMembers"):
            if not grant:
                fail(401, "invalid token")
            field = edge
            collection = "labels" if edge == "idLabels" else "members"
            if method == "POST" and len(parts) == 2:
                rid = text(p.get("value"), "value", 24)
                record = lookup(ctx, collection, rid)
                if (collection == "labels" and record["idBoard"] != board["id"]) or (
                    collection == "members" and rid not in board["_members"]
                ):
                    fail(400, f"invalid value for {edge}")
                if rid not in card[field]:
                    card[field].append(rid)
                await emit(
                    ctx,
                    self.request,
                    grant,
                    "addLabelToCard" if collection == "labels" else "addMemberToCard",
                    board,
                    card,
                )
                return (
                    clean(record)
                    if collection == "labels"
                    else member_shape(ctx, self.request, record, grant)
                )
            if method == "DELETE" and len(parts) == 3:
                if parts[2] not in card[field]:
                    fail(404, "not found")
                card[field].remove(parts[2])
                await emit(
                    ctx,
                    self.request,
                    grant,
                    "removeLabelFromCard" if collection == "labels" else "removeMemberFromCard",
                    board,
                    card,
                )
                return {}
        if method == "GET" and len(parts) == 2:
            shaped = card_shape(ctx, self.request, card, grant)
            if edge in shaped:
                return {"_value": shaped[edge]}
        fail(404, "not found")

    async def comments(self, card: Record, board: Record, parts: list[str]) -> Any:
        ctx, p, grant = self.ctx, self.params, self.grant
        if not parts and self.method == "GET":
            return action_list(ctx, self.request, grant, ctx.store["actions"].where(_card=card["id"]), p)
        if not grant:
            fail(404, "not found")
        assert grant is not None
        if parts == ["comments"] and self.method == "POST":
            value = text(p.get("text"), "text")
            action = await emit(ctx, self.request, grant, "commentCard", board, card, data={"text": value})
            return action_shape(ctx, self.request, action, grant)
        if len(parts) == 2 and parts[1] == "comments":
            action = lookup(ctx, "actions", parts[0])
            if action.get("_card") != card["id"] or action["type"] != "commentCard":
                fail(404, "action not found")
            if action["idMemberCreator"] != grant.user_id and board["_members"].get(grant.user_id) != "admin":
                fail(401, "unauthorized permission requested")
            if self.method == "PUT":
                value = text(p.get("text"), "text")
                action["data"]["text"] = value
                await emit(
                    ctx,
                    self.request,
                    grant,
                    "updateComment",
                    board,
                    card,
                    data={"action": {"id": action["id"]}, "text": value},
                )
                return action_shape(ctx, self.request, action, grant)
            if self.method == "DELETE":
                ctx.store["actions"].delete(action["id"])
                await emit(
                    ctx,
                    self.request,
                    grant,
                    "deleteComment",
                    board,
                    card,
                    data={"action": {"id": action["id"]}},
                )
                return {}
        fail(404, "not found")

    def add_attachment(self, card: Record, params: Record, uploaded: tuple[bytes, str, str] | None) -> Record:
        ctx, grant = self.ctx, self.grant
        assert grant is not None
        name = text(params.get("name", uploaded[1] if uploaded else params.get("url", "")), "name", 256)
        if uploaded:
            content, _, mime = uploaded
            if not content or len(content) > 10 * 1024 * 1024:
                fail(400, "file too large or empty")
            url = None
        else:
            url, mime = http_url(params.get("url"), "url"), ""
        record = {
            "id": new_id(ctx, "attachment"),
            "idCard": card["id"],
            "idBoard": card["idBoard"],
            "idMember": grant.user_id,
            "bytes": len(uploaded[0]) if uploaded else None,
            "date": isoformat_z(ctx.clock.now()),
            "edgeColor": None,
            "isUpload": uploaded is not None,
            "mimeType": mime,
            "name": name,
            "previews": [],
            "pos": position("bottom", ctx.store["attachments"].where(idCard=card["id"])),
            "url": url,
            "_blob": ctx.store.blobs.put(uploaded[0]) if uploaded else None,
        }
        return ctx.store["attachments"].put(record)

    async def attachments(self, card: Record, board: Record, parts: list[str]) -> Any:
        ctx, p, grant, method = self.ctx, self.params, self.grant, self.method
        if not parts:
            if method == "GET":
                return [
                    attachment_shape(ctx, self.request, r, p.get("fields", "all"))
                    for r in ctx.store["attachments"].where(idCard=card["id"])
                ]
            if method == "POST":
                upload = p.get("file")
                data = None
                if upload is not None:
                    if not isinstance(upload, UploadFile):
                        fail(400, "invalid file")
                    content = await upload.read(10 * 1024 * 1024 + 1)
                    data = (
                        content,
                        upload.filename or "attachment",
                        upload.content_type or "application/octet-stream",
                    )
                if upload is not None and "url" in p:
                    fail(400, "specify file or url")
                attachment = self.add_attachment(card, p, data)
                await emit(
                    ctx,
                    self.request,
                    grant,
                    "addAttachmentToCard",
                    board,
                    card,
                    data={
                        "attachment": {
                            "id": attachment["id"],
                            "name": attachment["name"],
                            "url": attachment_shape(ctx, self.request, attachment)["url"],
                        }
                    },
                )
                return attachment_shape(ctx, self.request, attachment)
            fail(404, "not found")
        attachment = lookup(ctx, "attachments", parts[0])
        if attachment["idCard"] != card["id"]:
            fail(404, "attachment not found")
        if method == "DELETE" and len(parts) == 1:
            ctx.store["attachments"].delete(attachment["id"])
            if card["idAttachmentCover"] == attachment["id"]:
                card["idAttachmentCover"] = None
            await emit(ctx, self.request, grant, "deleteAttachmentFromCard", board, card)
            return {}
        if method == "GET" and len(parts) == 1:
            return attachment_shape(ctx, self.request, attachment, p.get("fields", "all"))
        if method == "GET" and len(parts) >= 3 and parts[1] == "download" and attachment["isUpload"]:
            if not grant or not self.request.headers.get("authorization", "").lower().startswith(
                ("oauth ", "bearer ")
            ):
                fail(401, "invalid token")
            content = attachment_content(ctx, attachment)
            return Response(
                content,
                media_type=attachment["mimeType"]
                or mimetypes.guess_type(attachment["name"])[0]
                or "application/octet-stream",
                headers={
                    "Content-Disposition": "attachment; filename*=UTF-8''"
                    + quote(attachment["name"], safe=""),
                    "Content-Length": str(len(content)),
                    "Cache-Control": "private",
                },
            )
        fail(404, "not found")

    async def create_checklist(self, card: Record) -> Record:
        ctx, p, grant = self.ctx, self.params, self.grant
        if "idChecklistSource" in p:
            fail(400, "checklist copying is not supported")
        name = text(p.get("name", "Checklist"), "name")
        pos = position(p.get("pos", "bottom"), ctx.store["checklists"].where(idCard=card["id"]))
        record = {
            "id": new_id(ctx, "checklist"),
            "idBoard": card["idBoard"],
            "idCard": card["id"],
            "name": name,
            "pos": pos,
            "checkItems": [],
        }
        ctx.store["checklists"].put(record)
        card["idChecklists"].append(record["id"])
        await emit(
            ctx,
            self.request,
            grant,
            "addChecklistToCard",
            ctx.store["boards"].get(card["idBoard"]),
            card,
            data={"checklist": {"id": record["id"], "name": name}},
        )
        return clean(record)

    async def check_item(self, card: Record, item_id: str) -> Record:
        ctx, p, grant = self.ctx, self.params, self.grant
        checklist = next(
            (
                c
                for c in ctx.store["checklists"].where(idCard=card["id"])
                if any(i["id"] == item_id for i in c["checkItems"])
            ),
            None,
        )
        if not checklist:
            fail(404, "checkItem not found")
        item = next(i for i in checklist["checkItems"] if i["id"] == item_id)
        if self.method == "GET":
            return clean(item)
        if self.method == "DELETE":
            checklist["checkItems"].remove(item)
        elif self.method == "PUT":
            changes: Record = {}
            if "name" in p:
                changes["name"] = text(p["name"], "name")
            if "state" in p:
                if p["state"] not in ("complete", "incomplete"):
                    fail(400, "invalid value for state")
                changes["state"] = p["state"]
            if "pos" in p:
                changes["pos"] = position(
                    p["pos"], [i for i in checklist["checkItems"] if i["id"] != item_id]
                )
            if "due" in p:
                changes["due"] = date_value(p["due"], "due")
            if "idMember" in p:
                changes["idMember"] = (
                    member(ctx, p["idMember"])["id"] if p["idMember"] not in (None, "null", "") else None
                )
                if (
                    changes["idMember"]
                    and changes["idMember"] not in ctx.store["boards"].get(card["idBoard"])["_members"]
                ):
                    fail(400, "invalid value for idMember")
            item.update(changes)
        else:
            fail(404, "not found")
        await emit(
            ctx,
            self.request,
            grant,
            "updateCheckItemStateOnCard" if self.method == "PUT" else "deleteCheckItem",
            ctx.store["boards"].get(card["idBoard"]),
            card,
            data={"checkItem": clean(item), "checklist": {"id": checklist["id"], "name": checklist["name"]}},
        )
        return clean(item) if self.method == "PUT" else {}
