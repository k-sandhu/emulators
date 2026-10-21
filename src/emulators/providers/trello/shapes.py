"""Request-aware links, computed badges and Trello's nested resource parameters."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import Request

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext, url_for_host
from emulators.world.documents import render

from .common import Record, can_view, clean, fail, flag, integer, names, page, project


def member_shape(
    ctx: ProviderContext, request: Request, record: Record, grant: Grant | None, fields: Any = "all"
) -> Record:
    value = clean(record)
    value["url"] = url_for_host(request, ctx, "trello.com", "/" + record["username"])
    if grant and grant.user_id == record["id"]:
        if "account" in grant.scopes or "*" in grant.scopes:
            value["email"] = record["_email"]
        value["idBoards"] = [b["id"] for b in ctx.store["boards"] if record["id"] in b["_members"]]
        value["idOrganizations"] = [
            o["id"] for o in ctx.store["organizations"] if record["id"] in o["_members"]
        ]
    if "email" in names(fields) and "email" not in value:
        fail(401, "unauthorized permission requested")
    return project(value, fields)


def organization_shape(
    ctx: ProviderContext, request: Request, record: Record, fields: Any = "all", grant: Grant | None = None
) -> Record:
    value = clean(record)
    value["url"] = url_for_host(request, ctx, "trello.com", "/" + record["name"])
    value["idBoards"] = [
        b["id"]
        for b in ctx.store["boards"]
        if b["idOrganization"] == record["id"] and can_view(ctx, b, grant)
    ]
    return project(value, fields)


def attachment_content(ctx: ProviderContext, record: Record) -> bytes:
    if not record["_blob"]:
        doc = ctx.world.document(record["_document"])
        content, mime = render(
            doc.kind,
            doc.title,
            list(doc.paragraphs),
            author=ctx.world.person(doc.owner_id).display_name,
            created=doc.created,
            table=[list(row) for row in doc.table] if doc.table else None,
        )
        record.update(_blob=ctx.store.blobs.put(content), bytes=len(content), mimeType=mime)
    return ctx.store.blobs.get(record["_blob"])


def attachment_shape(ctx: ProviderContext, request: Request, record: Record, fields: Any = "all") -> Record:
    if record["isUpload"]:
        attachment_content(ctx, record)
    value = clean(record)
    value["fileName"] = record["name"]
    value.pop("idCard", None)
    value.pop("idBoard", None)
    if record["isUpload"]:
        value["url"] = url_for_host(
            request,
            ctx,
            "api.trello.com",
            f"/1/cards/{record['idCard']}/attachments/{record['id']}/download/{quote(record['name'], safe='')}",
        )
    return project(value, fields)


def action_shape(
    ctx: ProviderContext, request: Request, record: Record, grant: Grant | None, fields: Any = "all"
) -> Record:
    value = project(record, fields)
    creator = ctx.store["members"].get(record["idMemberCreator"])
    if creator:
        value["memberCreator"] = member_shape(
            ctx, request, creator, grant, "avatarHash,fullName,initials,username"
        )
    return value


def action_list(
    ctx: ProviderContext, request: Request, grant: Grant | None, records: list[Record], params: Record
) -> list[Record]:
    wanted = params.get("filter", "all")
    if wanted != "all":
        filters = names(wanted, "filter")
        records = [
            a
            for a in records
            if any(
                a["type"] == f
                or (
                    f.startswith("updateCard:")
                    and a["type"] == "updateCard"
                    and f.split(":", 1)[1] in a["data"].get("old", {})
                )
                for f in filters
            )
        ]
    offset = integer(params.get("page"), "page", 0, 1000000, 0)
    limit = integer(params.get("limit"), "limit", 50)
    records = page(
        records, {**params, "limit": max(1, len(records))}, maximum=max(1000, len(records)), default=1000
    )
    return [
        action_shape(ctx, request, a, grant, params.get("fields", "all"))
        for a in records[offset * limit : (offset + 1) * limit]
    ]


def card_shape(
    ctx: ProviderContext, request: Request, record: Record, grant: Grant | None, params: Record | None = None
) -> Record:
    params = params or {}
    value = clean(record)
    value["shortUrl"] = url_for_host(request, ctx, "trello.com", "/c/" + record["shortLink"])
    value["url"] = (
        value["shortUrl"]
        + "/"
        + str(record["idShort"])
        + "-"
        + quote(record["name"].replace(" ", "-"), safe="-")
    )
    value["labels"] = [clean(r) for rid in record["idLabels"] if (r := ctx.store["labels"].get(rid))]
    attachments = ctx.store["attachments"].where(idCard=record["id"])
    checklists = [r for rid in record["idChecklists"] if (r := ctx.store["checklists"].get(rid))]
    checkitems = [i for c in checklists for i in c["checkItems"]]
    value["checkItemStates"] = [
        {"idCheckItem": i["id"], "state": i["state"]} for i in checkitems if i["state"] == "complete"
    ]
    value["badges"] = {
        "attachments": len(attachments),
        "comments": len(
            ctx.store["actions"].filter(
                lambda a: a.get("_card") == record["id"] and a["type"] == "commentCard"
            )
        ),
        "checkItems": len(checkitems),
        "checkItemsChecked": sum(i["state"] == "complete" for i in checkitems),
        "description": bool(record["desc"]),
        "due": record["due"],
        "dueComplete": record["dueComplete"],
        "start": record["start"],
        "votes": 0,
        "subscribed": False,
        "viewingMemberVoted": False,
        "location": False,
        "attachmentsByType": {"trello": {"board": 0, "card": 0}},
    }
    value = project(value, params.get("fields", "all"))
    if "attachments" in params and params["attachments"] not in (False, "false"):
        mode = params["attachments"]
        if mode != "cover":
            flag(mode, "attachments")
        value["attachments"] = [
            attachment_shape(ctx, request, a, params.get("attachment_fields", "all"))
            for a in attachments
            if mode != "cover" or a["id"] == record["idAttachmentCover"]
        ]
    for key, collection in (("board", "boards"), ("list", "lists")):
        if flag(params.get(key, False), key):
            parent = ctx.store[collection].get(record["id" + key.title()])
            value[key] = (
                board_shape(ctx, request, parent, grant, {"fields": params.get("board_fields", "all")})
                if key == "board"
                else project(parent, params.get("list_fields", "all"))
            )
    if flag(params.get("members", False), "members"):
        value["members"] = [
            member_shape(
                ctx,
                request,
                ctx.store["members"].get(uid),
                grant,
                params.get("member_fields", "avatarHash,fullName,initials,username"),
            )
            for uid in record["idMembers"]
        ]
    if params.get("checklists", "none") != "none":
        if params["checklists"] != "all":
            from .common import fail

            fail(400, "invalid value for checklists")
        value["checklists"] = [project(c, params.get("checklist_fields", "all")) for c in checklists]
    if params.get("actions") and params["actions"] != "none":
        action_params = {
            "filter": params["actions"],
            "limit": params.get("actions_limit", 50),
            "fields": params.get("action_fields", "all"),
        }
        value["actions"] = action_list(
            ctx, request, grant, ctx.store["actions"].where(_card=record["id"]), action_params
        )
    return value


def board_shape(
    ctx: ProviderContext, request: Request, record: Record, grant: Grant | None, params: Record | None = None
) -> Record:
    params = params or {}
    for key in ("lists", "cards"):
        if params.get(key, "none") not in ("none", "all", "open", "closed", "visible"):
            fail(400, f"invalid value for {key}")
    for key in ("members", "memberships"):
        if params.get(key, "none") not in ("none", "all", "normal", "admin", "active", "me"):
            fail(400, f"invalid value for {key}")
    value = clean(record)
    value["url"] = url_for_host(
        request,
        ctx,
        "trello.com",
        "/b/" + record["shortLink"] + "/" + quote(record["name"].replace(" ", "-")),
    )
    value["shortUrl"] = url_for_host(request, ctx, "trello.com", "/b/" + record["shortLink"])
    value = project(value, params.get("fields", "all"))
    if params.get("memberships", "none") != "none":
        value["memberships"] = [
            {
                "id": rid[:16] + record["id"][-8:],
                "idMember": rid,
                "memberType": role,
                "unconfirmed": False,
                "deactivated": False,
            }
            for rid, role in record["_members"].items()
            if params["memberships"] in ("all", "active")
            or role == params["memberships"]
            or (params["memberships"] == "me" and grant and rid == grant.user_id)
        ]
    if params.get("lists", "none") != "none":
        filter_ = params["lists"]
        value["lists"] = [
            project(r, params.get("list_fields", "all"))
            for r in sorted(ctx.store["lists"].where(idBoard=record["id"]), key=lambda r: r["pos"])
            if filter_ == "all" or r["closed"] == (filter_ == "closed")
        ]
    if params.get("cards", "none") != "none":
        filter_ = params["cards"]
        cards = [
            r
            for r in ctx.store["cards"].where(idBoard=record["id"])
            if filter_ == "all" or r["closed"] == (filter_ == "closed")
        ]
        if filter_ == "visible":
            cards = [
                r for r in cards if not ctx.store["lists"].get(r["idList"])["closed"] and not record["closed"]
            ]
        value["cards"] = [
            card_shape(ctx, request, r, grant, {"fields": params.get("card_fields", "all")})
            for r in sorted(cards, key=lambda r: (r["idList"], r["pos"]))
        ]
    if params.get("members", "none") != "none":
        value["members"] = [
            member_shape(
                ctx,
                request,
                ctx.store["members"].get(uid),
                grant,
                params.get("member_fields", "avatarHash,fullName,initials,username"),
            )
            for uid in record["_members"]
            if params["members"] in ("all", "active")
            or record["_members"][uid] == params["members"]
            or (params["members"] == "me" and grant and uid == grant.user_id)
        ]
    if flag(params.get("organization", False), "organization") and record["idOrganization"]:
        value["organization"] = organization_shape(
            ctx,
            request,
            ctx.store["organizations"].get(record["idOrganization"]),
            params.get("organization_fields", "all"),
            grant,
        )
    if params.get("actions") and params["actions"] != "none":
        value["actions"] = action_list(
            ctx,
            request,
            grant,
            ctx.store["actions"].where(_board=record["id"]),
            {"filter": params["actions"], "limit": integer(params.get("actions_limit"), "actions_limit", 50)},
        )
    return value
