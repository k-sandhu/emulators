"""Workspace, board, list, label and checklist resource operations."""

from __future__ import annotations

import copy
from typing import Any

from fastapi import Request

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext

from .common import (
    COLORS,
    Record,
    board_access,
    can_view,
    clean,
    fail,
    flag,
    get_board,
    integer,
    lookup,
    member,
    new_id,
    position,
    project,
    resource,
    text,
)
from .hooks import emit
from .seed import board_record
from .shapes import action_list, board_shape, card_shape, member_shape, organization_shape


def selected(
    records: list[Record], filter_: Any, default: str = "open", *, ctx: ProviderContext | None = None
) -> list[Record]:
    filter_ = default if filter_ is None else filter_
    if filter_ not in ("all", "open", "closed", "visible", "none"):
        fail(400, "invalid filter")
    if filter_ == "none":
        return []
    result = [r for r in records if filter_ == "all" or r["closed"] == (filter_ == "closed")]
    if filter_ == "visible" and ctx:
        result = [
            r
            for r in result
            if "idList" not in r
            or (
                not ctx.store["lists"].get(r["idList"])["closed"]
                and not ctx.store["boards"].get(r["idBoard"])["closed"]
            )
        ]
    return result


def color_value(value: Any) -> str | None:
    if value in (None, "null"):
        return None
    allowed = COLORS | {color + suffix for color in COLORS for suffix in ("_light", "_dark")}
    if not isinstance(value, str) or value not in allowed:
        fail(400, "invalid value for color")
    return value


class ResourcesMixin:
    ctx: ProviderContext
    request: Request
    params: Record
    grant: Grant | None
    method: str

    async def members(self, parts: list[str]) -> Any:
        ctx, p, grant = self.ctx, self.params, self.grant
        if not parts or self.method != "GET":
            fail(404, "not found")
        if parts[0] == "me" and not grant:
            fail(401, "invalid token")
        record = member(ctx, grant.user_id if parts[0] == "me" else parts[0])
        if len(parts) == 1:
            return member_shape(ctx, self.request, record, grant, p.get("fields", "all"))
        edge = parts[1]
        if edge == "boards":
            rows = [
                b for b in ctx.store["boards"] if record["id"] in b["_members"] and can_view(ctx, b, grant)
            ]
            filter_ = parts[2] if len(parts) == 3 else p.get("filter", "all")
            return [board_shape(ctx, self.request, b, grant, p) for b in selected(rows, filter_)]
        if edge == "organizations":
            return [
                organization_shape(ctx, self.request, o, p.get("fields", "all"), grant)
                for o in ctx.store["organizations"]
                if record["id"] in o["_members"] and grant and grant.user_id in o["_members"]
            ]
        if edge == "cards":
            rows = [
                c
                for c in ctx.store["cards"]
                if record["id"] in c["idMembers"]
                and can_view(ctx, ctx.store["boards"].get(c["idBoard"]), grant)
            ]
            from .common import page

            return [
                card_shape(ctx, self.request, c, grant, p)
                for c in page(selected(rows, p.get("filter", "visible"), ctx=ctx), p)
            ]
        if edge == "actions":
            rows = [
                a
                for a in ctx.store["actions"]
                if a["idMemberCreator"] == record["id"]
                and (b := ctx.store["boards"].get(a["_board"]))
                and can_view(ctx, b, grant)
            ]
            return action_list(ctx, self.request, grant, rows, p)
        value = member_shape(ctx, self.request, record, grant)
        if edge in value and len(parts) == 2:
            return {"_value": value[edge]}
        fail(404, "not found")

    async def organizations(self, parts: list[str]) -> Any:
        ctx, p, grant, method = self.ctx, self.params, self.grant, self.method
        if not parts and method == "POST":
            assert grant
            display = text(p.get("displayName"), "displayName", 16384)
            slug = text(p.get("name", "workspace" + new_id(ctx, "slug")[-8:]), "name", 255)
            if not slug.replace("_", "").isalnum() or not slug.isascii():
                fail(400, "invalid value for name")
            if ctx.store["organizations"].first(name=slug):
                fail(400, "name already taken")
            record = {
                "id": new_id(ctx, "organization"),
                "name": slug.lower(),
                "displayName": display,
                "desc": text(p.get("desc", ""), "desc", empty=True),
                "descData": {"emoji": {}},
                "website": text(p.get("website", ""), "website", 4096, empty=True),
                "logoHash": None,
                "logoUrl": None,
                "products": [],
                "premiumFeatures": [],
                "prefs": {"permissionLevel": "private"},
                "_members": {grant.user_id: "admin"},
            }
            ctx.store["organizations"].put(record)
            return organization_shape(ctx, self.request, record, grant=grant)
        if not parts:
            fail(404, "not found")
        record = lookup(ctx, "organizations", parts[0])
        if not grant or grant.user_id not in record["_members"]:
            fail(401, "unauthorized permission requested")
        admin = record["_members"][grant.user_id] == "admin"
        if len(parts) == 1:
            if method == "GET":
                return organization_shape(ctx, self.request, record, p.get("fields", "all"), grant)
            if method == "PUT":
                if not admin:
                    fail(401, "unauthorized permission requested")
                changes = {
                    key: text(p[key], key, empty=key in ("desc", "website"))
                    for key in ("displayName", "desc", "website")
                    if key in p
                }
                record.update(changes)
                return organization_shape(ctx, self.request, record, grant=grant)
            if method == "DELETE":
                if not admin:
                    fail(401, "unauthorized permission requested")
                for board in ctx.store["boards"].where(idOrganization=record["id"]):
                    board["idOrganization"] = None
                ctx.store["organizations"].delete(record["id"])
                return {}
            fail(404, "not found")
        edge = parts[1]
        if edge == "boards" and method == "GET":
            rows = [
                b for b in ctx.store["boards"].where(idOrganization=record["id"]) if can_view(ctx, b, grant)
            ]
            return [
                board_shape(ctx, self.request, b, grant, p) for b in selected(rows, p.get("filter", "all"))
            ]
        if edge == "members" and method == "GET":
            return [
                member_shape(
                    ctx,
                    self.request,
                    ctx.store["members"].get(uid),
                    grant,
                    p.get("fields", "avatarHash,fullName,initials,username"),
                )
                for uid in record["_members"]
            ]
        if edge == "memberships" and method == "GET":
            return [
                {
                    "id": uid[:16] + record["id"][-8:],
                    "idMember": uid,
                    "memberType": role,
                    "unconfirmed": False,
                    "deactivated": False,
                }
                for uid, role in record["_members"].items()
            ]
        if edge == "members" and len(parts) == 3 and method in ("PUT", "DELETE"):
            if not admin:
                fail(401, "unauthorized permission requested")
            who = member(ctx, parts[2])
            role = p.get("type", "normal")
            if method == "PUT" and role not in ("normal", "admin"):
                fail(400, "invalid value for type")
            if (
                record["_members"].get(who["id"]) == "admin"
                and (method == "DELETE" or role != "admin")
                and list(record["_members"].values()).count("admin") == 1
            ):
                fail(400, "cannot remove last admin")
            if method == "PUT":
                record["_members"][who["id"]] = role
                return organization_shape(ctx, self.request, record, grant=grant)
            record["_members"].pop(who["id"], None)
            return {}
        fail(404, "not found")

    async def boards(self, parts: list[str]) -> Any:
        ctx, p, grant, method = self.ctx, self.params, self.grant, self.method
        if not parts:
            if method != "POST":
                fail(404, "not found")
            assert grant
            name = text(p.get("name"), "name")
            desc = text(p.get("desc", ""), "desc", empty=True)
            default_lists = flag(p.get("defaultLists", True), "defaultLists")
            default_labels = flag(p.get("defaultLabels", True), "defaultLabels")
            if "idBoardSource" in p:
                fail(400, "board copying is not supported")
            oid = p.get("idOrganization")
            if oid:
                org = lookup(ctx, "organizations", oid)
                if grant.user_id not in org["_members"]:
                    fail(401, "unauthorized permission requested")
                oid = org["id"]
            visibility = p.get("prefs_permissionLevel", "private")
            if visibility not in ("private", "public", "org") or (visibility == "org" and not oid):
                fail(400, "invalid value for prefs_permissionLevel")
            board = board_record(
                ctx, new_id(ctx, "board"), name, oid, {grant.user_id: "admin"}, desc, visibility
            )
            ctx.store["boards"].put(board)
            if default_lists:
                for i, title in enumerate(("To Do", "Doing", "Done")):
                    ctx.store["lists"].put(
                        {
                            "id": new_id(ctx, "list"),
                            "name": title,
                            "closed": False,
                            "idBoard": board["id"],
                            "pos": float((i + 1) * 16384),
                            "softLimit": None,
                            "subscribed": False,
                        }
                    )
            if default_labels:
                for color in sorted(COLORS):
                    ctx.store["labels"].put(
                        {
                            "id": new_id(ctx, "label"),
                            "idBoard": board["id"],
                            "name": "",
                            "color": color,
                            "uses": 0,
                        }
                    )
            await emit(ctx, self.request, grant, "createBoard", board)
            return board_shape(ctx, self.request, board, grant)
        board = get_board(ctx, parts[0], grant)
        if len(parts) == 1:
            if method == "GET":
                return board_shape(ctx, self.request, board, grant, p)
            if method == "PUT":
                board_access(ctx, board, grant, admin=True)
                changes = {
                    key: text(p[key], key, empty=key == "desc") for key in ("name", "desc") if key in p
                }
                if "closed" in p:
                    changes["closed"] = flag(p["closed"], "closed")
                prefs = copy.deepcopy(board["prefs"])
                if "prefs/permissionLevel" in p or "prefs_permissionLevel" in p:
                    value = p.get("prefs/permissionLevel", p.get("prefs_permissionLevel"))
                    if value not in ("private", "org", "public") or (
                        value == "org" and not board["idOrganization"]
                    ):
                        fail(400, "invalid value for permissionLevel")
                    prefs["permissionLevel"] = value
                changes["prefs"] = prefs
                old = {key: copy.deepcopy(board[key]) for key in changes if changes[key] != board[key]}
                board.update(changes)
                await emit(ctx, self.request, grant, "updateBoard", board, data={"old": old})
                return board_shape(ctx, self.request, board, grant)
            if method == "DELETE":
                board_access(ctx, board, grant, admin=True)
                from .cards import delete_card

                await emit(ctx, self.request, grant, "deleteBoard", board)
                for card in ctx.store["cards"].where(idBoard=board["id"]):
                    delete_card(ctx, card)
                for collection in ("lists", "labels"):
                    for row in ctx.store[collection].where(idBoard=board["id"]):
                        ctx.store[collection].delete(row["id"])
                ctx.store["boards"].delete(board["id"])
                return {}
            fail(404, "not found")
        edge = parts[1]
        if method != "GET":
            board_access(ctx, board, grant, write=True)
        if edge == "actions" and method == "GET":
            return action_list(ctx, self.request, grant, ctx.store["actions"].where(_board=board["id"]), p)
        if edge == "cards" and method == "GET":
            rows = selected(
                ctx.store["cards"].where(idBoard=board["id"]),
                parts[2] if len(parts) == 3 else p.get("filter"),
                ctx=ctx,
            )
            if "before" in p or "since" in p or "limit" in p:
                from .common import page

                rows = page(rows, p)
            else:
                rows.sort(key=lambda c: (c["idList"], c["pos"]))
                rows = rows[:1000]
            return [card_shape(ctx, self.request, c, grant, p) for c in rows]
        if edge == "lists":
            if method == "GET":
                return [
                    project(r, p.get("fields", "all"))
                    for r in sorted(
                        selected(
                            ctx.store["lists"].where(idBoard=board["id"]),
                            parts[2] if len(parts) == 3 else p.get("filter"),
                        ),
                        key=lambda r: r["pos"],
                    )
                ]
            if method == "POST" and len(parts) == 2:
                return await self.create_list(board)
        if edge == "labels":
            if method == "GET":
                rows = ctx.store["labels"].where(idBoard=board["id"])
                return [
                    project(r, p.get("fields", "all")) for r in rows[: integer(p.get("limit"), "limit", 50)]
                ]
            if method == "POST":
                return await self.create_label(board)
        if edge == "checklists" and method == "GET":
            return [
                project(r, p.get("fields", "all")) for r in ctx.store["checklists"].where(idBoard=board["id"])
            ]
        if edge == "memberships" and method == "GET":
            rows = board_shape(ctx, self.request, board, grant, {"memberships": p.get("filter", "all")}).get(
                "memberships", []
            )
            if flag(p.get("member", False), "member"):
                for row in rows:
                    row["member"] = member_shape(
                        ctx,
                        self.request,
                        ctx.store["members"].get(row["idMember"]),
                        grant,
                        p.get("member_fields", "fullName,username"),
                    )
            return rows
        if edge == "members":
            if method == "GET":
                return [
                    member_shape(
                        ctx,
                        self.request,
                        ctx.store["members"].get(uid),
                        grant,
                        p.get("fields", "avatarHash,fullName,initials,username"),
                    )
                    for uid in board["_members"]
                ]
            if len(parts) == 3 and method in ("PUT", "DELETE"):
                board_access(ctx, board, grant, admin=True)
                who = member(ctx, parts[2])
                role = p.get("type", "normal")
                if method == "PUT" and role not in ("admin", "normal", "observer"):
                    fail(400, "invalid value for type")
                if (
                    board["_members"].get(who["id"]) == "admin"
                    and (method == "DELETE" or role != "admin")
                    and list(board["_members"].values()).count("admin") == 1
                ):
                    fail(400, "cannot remove last admin")
                if method == "PUT":
                    board["_members"][who["id"]] = role
                else:
                    board["_members"].pop(who["id"], None)
                    for c in ctx.store["cards"].where(idBoard=board["id"]):
                        c["idMembers"] = [uid for uid in c["idMembers"] if uid != who["id"]]
                await emit(
                    ctx,
                    self.request,
                    grant,
                    "addMemberToBoard" if method == "PUT" else "removeMemberFromBoard",
                    board,
                    data={"member": {"id": who["id"], "name": who["fullName"]}},
                )
                return member_shape(ctx, self.request, who, grant) if method == "PUT" else {}
        if method == "GET" and len(parts) == 2:
            value = board_shape(ctx, self.request, board, grant)
            if edge in value:
                return {"_value": value[edge]}
        fail(404, "not found")

    async def create_list(self, board: Record) -> Record:
        p, ctx = self.params, self.ctx
        name = text(p.get("name"), "name")
        pos = position(p.get("pos", "top"), ctx.store["lists"].where(idBoard=board["id"]))
        record = {
            "id": new_id(ctx, "list"),
            "name": name,
            "closed": False,
            "idBoard": board["id"],
            "pos": pos,
            "softLimit": None,
            "subscribed": False,
        }
        ctx.store["lists"].put(record)
        await emit(
            ctx,
            self.request,
            self.grant,
            "createList",
            board,
            data={"list": {"id": record["id"], "name": name}},
        )
        return clean(record)

    async def lists(self, parts: list[str]) -> Any:
        ctx, p, method, grant = self.ctx, self.params, self.method, self.grant
        if not parts:
            if method != "POST":
                fail(404, "not found")
            board = get_board(ctx, p.get("idBoard"), grant, write=True)
            return await self.create_list(board)
        record = resource(ctx, "lists", parts[0], grant, write=method != "GET")
        board = ctx.store["boards"].get(record["idBoard"])
        edge = parts[1] if len(parts) > 1 else None
        if method == "GET":
            if edge is None:
                return project(record, p.get("fields", "all"))
            if edge == "cards":
                rows = selected(ctx.store["cards"].where(idList=record["id"]), p.get("filter"), ctx=ctx)
                if "before" in p or "since" in p or "limit" in p:
                    from .common import page

                    rows = page(rows, p)
                else:
                    rows.sort(key=lambda c: c["pos"])
                    rows = rows[:1000]
                return [card_shape(ctx, self.request, c, grant, p) for c in rows]
            if edge == "board":
                return board_shape(ctx, self.request, board, grant, p)
            if edge == "actions":
                rows = [
                    a
                    for a in ctx.store["actions"].where(_board=board["id"])
                    if a["data"].get("list", {}).get("id") == record["id"]
                    or ((c := ctx.store["cards"].get(a.get("_card"))) and c["idList"] == record["id"])
                ]
                return action_list(ctx, self.request, grant, rows, p)
            if edge in clean(record):
                return {"_value": record[edge]}
        if method == "PUT" and edge in (None, "closed", "name", "pos"):
            updates = {edge: p.get("value")} if edge else p
            changes: Record = {}
            if "name" in updates:
                changes["name"] = text(updates["name"], "name")
            if "closed" in updates:
                changes["closed"] = flag(updates["closed"], "closed")
            if "pos" in updates:
                changes["pos"] = position(
                    updates["pos"],
                    [r for r in ctx.store["lists"].where(idBoard=board["id"]) if r["id"] != record["id"]],
                )
            if "idBoard" in updates and updates["idBoard"] != board["id"]:
                fail(400, "list moves between boards are not supported")
            record.update(changes)
            await emit(
                ctx,
                self.request,
                grant,
                "updateList",
                board,
                data={"list": {"id": record["id"], "name": record["name"]}},
            )
            return clean(record)
        if method == "POST" and edge == "archiveAllCards":
            for card in ctx.store["cards"].where(idList=record["id"]):
                card["closed"] = True
                await emit(
                    ctx, self.request, grant, "updateCard", board, card, data={"old": {"closed": False}}
                )
            return {}
        fail(404, "not found")

    async def create_label(self, board: Record) -> Record:
        p, ctx = self.params, self.ctx
        name, color = text(p.get("name", ""), "name", empty=True), color_value(p.get("color"))
        record = {"id": new_id(ctx, "label"), "idBoard": board["id"], "name": name, "color": color, "uses": 0}
        ctx.store["labels"].put(record)
        await emit(ctx, self.request, self.grant, "createLabel", board, data={"label": clean(record)})
        return clean(record)

    async def labels(self, parts: list[str]) -> Any:
        ctx, p, grant, method = self.ctx, self.params, self.grant, self.method
        if not parts and method == "POST":
            return await self.create_label(get_board(ctx, p.get("idBoard"), grant, write=True))
        if not parts:
            fail(404, "not found")
        record = resource(ctx, "labels", parts[0], grant, write=method != "GET")
        board = ctx.store["boards"].get(record["idBoard"])
        if method == "GET" and len(parts) == 1:
            return project(record, p.get("fields", "all"))
        if method == "PUT" and len(parts) <= 2:
            values = {parts[1]: p.get("value")} if len(parts) == 2 else p
            changes: Record = {}
            if "name" in values:
                changes["name"] = text(values["name"], "name", empty=True)
            if "color" in values:
                changes["color"] = color_value(values["color"])
            record.update(changes)
            await emit(ctx, self.request, grant, "updateLabel", board, data={"label": clean(record)})
            return clean(record)
        if method == "DELETE" and len(parts) == 1:
            for card in ctx.store["cards"].where(idBoard=board["id"]):
                card["idLabels"] = [rid for rid in card["idLabels"] if rid != record["id"]]
            ctx.store["labels"].delete(record["id"])
            await emit(ctx, self.request, grant, "deleteLabel", board, data={"label": {"id": record["id"]}})
            return {}
        fail(404, "not found")

    async def checklists(self, parts: list[str]) -> Any:
        ctx, p, grant, method = self.ctx, self.params, self.grant, self.method
        if not parts and method == "POST":
            return await self.create_checklist(resource(ctx, "cards", p.get("idCard"), grant, write=True))
        if not parts:
            fail(404, "not found")
        record = resource(ctx, "checklists", parts[0], grant, write=method != "GET")
        card = ctx.store["cards"].get(record["idCard"])
        board = ctx.store["boards"].get(record["idBoard"])
        if len(parts) == 1:
            if method == "GET":
                return project(record, p.get("fields", "all"))
            if method == "PUT":
                changes: Record = {}
                if "name" in p:
                    changes["name"] = text(p["name"], "name")
                if "pos" in p:
                    changes["pos"] = position(
                        p["pos"],
                        [
                            c
                            for c in ctx.store["checklists"].where(idCard=card["id"])
                            if c["id"] != record["id"]
                        ],
                    )
                record.update(changes)
                await emit(ctx, self.request, grant, "updateChecklist", board, card)
                return clean(record)
            if method == "DELETE":
                card["idChecklists"].remove(record["id"])
                ctx.store["checklists"].delete(record["id"])
                await emit(ctx, self.request, grant, "removeChecklistFromCard", board, card)
                return {}
            fail(404, "not found")
        edge = parts[1]
        if edge == "board" and method == "GET":
            return board_shape(ctx, self.request, board, grant, p)
        if edge == "cards" and method == "GET":
            return [card_shape(ctx, self.request, card, grant, p)]
        if edge == "checkItems":
            if len(parts) == 3:
                if not any(i["id"] == parts[2] for i in record["checkItems"]):
                    fail(404, "checkItem not found")
                return await self.check_item(card, parts[2])
            if method == "GET":
                rows = sorted(record["checkItems"], key=lambda i: i["pos"])
                filter_ = p.get("filter", "all")
                if filter_ not in ("all", "complete", "incomplete"):
                    fail(400, "invalid value for filter")
                return [
                    project(i, p.get("fields", "all"))
                    for i in rows
                    if filter_ == "all" or i["state"] == filter_
                ]
            if method == "POST":
                name = text(p.get("name"), "name")
                checked = flag(p.get("checked", False), "checked")
                pos = position(p.get("pos", "bottom"), record["checkItems"])
                item = {
                    "id": new_id(ctx, "checkitem"),
                    "idChecklist": record["id"],
                    "name": name,
                    "nameData": {"emoji": {}},
                    "state": "complete" if checked else "incomplete",
                    "pos": pos,
                    "due": None,
                    "idMember": None,
                }
                record["checkItems"].append(item)
                await emit(
                    ctx, self.request, grant, "createCheckItem", board, card, data={"checkItem": clean(item)}
                )
                return clean(item)
        if method == "GET" and edge in clean(record):
            return {"_value": record[edge]}
        fail(404, "not found")
