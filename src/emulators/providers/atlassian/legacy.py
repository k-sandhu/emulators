"""The Confluence v1 read surface still used by knowledge connectors."""

from __future__ import annotations

import copy
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Request

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext

from .auth import confluence_auth
from .common import (
    Record,
    absolute,
    ancestors,
    api_url,
    can_view,
    fail,
    get_page,
    integer,
    local_path,
    person_record,
    principal,
    site_url,
    space_role,
)
from .query import QueryError, parse_query
from .seed import account_id
from .shapes import attachment_shape, body_shape, plain_text


def user_shape(request: Request, ctx: ProviderContext, user: Record) -> Record:
    return {
        "type": "known",
        "accountId": user["id"],
        "accountType": "atlassian",
        "email": user["email"],
        "publicName": user["publicName"],
        "displayName": user["displayName"],
        "isExternalCollaborator": False,
        "profilePicture": {
            "path": "/wiki/aa-avatar/" + user["id"],
            "width": 48,
            "height": 48,
            "isDefault": True,
        },
        "_links": {"self": api_url(request, "/wiki/rest/api/user?accountId=" + user["id"])},
        "_expandable": {"personalSpace": ""},
    }


def v1_space(request: Request, ctx: ProviderContext, space: Record) -> Record:
    return {
        "id": int(space["id"]),
        "key": space["key"],
        "name": space["name"],
        "type": space["type"],
        "status": space["status"],
        "_links": {
            "webui": "/spaces/" + space["key"],
            "self": api_url(request, "/wiki/rest/api/space/" + space["key"]),
        },
        "_expandable": {"description": "", "homepage": "/rest/api/content/" + space["homepageId"]},
    }


def v1_version(request: Request, ctx: ProviderContext, version: Record) -> Record:
    user = ctx.store["users"].get(version["authorId"])
    return {
        "by": user_shape(request, ctx, user) if user else {"accountId": version["authorId"], "type": "known"},
        "when": version["createdAt"],
        "friendlyWhen": version["createdAt"],
        "message": version["message"],
        "number": version["number"],
        "minorEdit": version["minorEdit"],
    }


def restrictions(request: Request, ctx: ProviderContext, page: Record) -> Record:
    result: Record = {}
    for operation, field in (("read", "_read"), ("update", "_update")):
        allowed = page.get(field) or []
        users = [
            user_shape(request, ctx, user)
            for pid in allowed
            if (user := ctx.store["users"].get(account_id(pid)))
        ]
        result[operation] = {
            "operation": operation,
            "restrictions": {
                "user": {"results": users, "start": 0, "limit": 100, "size": len(users)},
                "group": {"results": [], "start": 0, "limit": 100, "size": 0},
                "_expandable": {"user": "", "group": ""},
            },
            "_expandable": {"content": "/rest/api/content/" + page["id"]},
        }
    result["_links"] = {
        "self": api_url(request, "/wiki/rest/api/content/" + page["id"] + "/restriction/byOperation")
    }
    return result


def v1_content(
    request: Request,
    ctx: ProviderContext,
    page: Record,
    grant: Grant,
    expand: str = "",
    *,
    shallow: bool = False,
) -> Record:
    space = ctx.store["spaces"].get(page["spaceId"])
    result: Record = {
        "id": page["id"],
        "type": page.get("_type", "page"),
        "status": page["status"],
        "title": page["title"],
        "_links": {
            "webui": f"/spaces/{space['key']}/pages/{page['id']}",
            "editui": f"/pages/resumedraft.action?draftId={page['id']}",
            "tinyui": "/x/" + page["id"],
            "self": api_url(request, "/wiki/rest/api/content/" + page["id"]),
        },
        "_expandable": {
            "body": "",
            "version": "",
            "history": "/rest/api/content/" + page["id"] + "/history",
            "ancestors": "",
            "space": "/rest/api/space/" + space["key"],
            "restrictions": "/rest/api/content/" + page["id"] + "/restriction/byOperation",
        },
    }
    if shallow:
        return result
    expansions = expand.split(",")

    def has(name: str) -> bool:
        return any(e == name or e.startswith(name + ".") for e in expansions)

    if has("body"):
        result["body"] = {}
        for representation in ("storage", "view", "atlas_doc_format"):
            if has("body." + representation):
                result["body"].update(body_shape(request, page, representation))
    if has("version"):
        result["version"] = v1_version(request, ctx, page["version"])
    if has("space"):
        result["space"] = v1_space(request, ctx, space)
    if has("ancestors"):
        result["ancestors"] = [
            v1_content(request, ctx, parent, grant, shallow=True)
            for parent in ancestors(ctx, page)
            if can_view(ctx, parent, grant)
        ]
    if has("history"):
        result["history"] = {
            "latest": True,
            "createdDate": page["createdAt"],
            "createdBy": user_shape(request, ctx, ctx.store["users"].get(page["authorId"])),
            "lastUpdated": v1_version(request, ctx, page["version"]),
        }
    if has("restrictions"):
        result["restrictions"] = restrictions(request, ctx, page)
    if has("metadata.labels"):
        result["metadata"] = {
            "labels": {"results": page["_labels"], "start": 0, "limit": 200, "size": len(page["_labels"])}
        }
    for key in list(result["_expandable"]):
        if key in result:
            del result["_expandable"][key]
    return result


def offset_page(
    request: Request, ctx: ProviderContext, records: list[Record], *, total: bool = False
) -> Record:
    start = integer(request, request.query_params.get("start"), "start", 0, 2147483647, 0)
    limit = integer(request, request.query_params.get("limit"), "limit", 25)
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k not in ("start", "limit")]
    links = {
        "base": site_url(request, ctx, "/wiki"),
        "context": "/wiki",
        "self": absolute(request, local_path(request)),
    }
    if start + limit < len(records):
        links["next"] = (
            absolute(request, local_path(request))
            + "?"
            + urlencode([*pairs, ("start", start + limit), ("limit", limit)])
        )
    if start:
        links["prev"] = (
            absolute(request, local_path(request))
            + "?"
            + urlencode([*pairs, ("start", max(0, start - limit)), ("limit", limit)])
        )
    result = {
        "results": records[start : start + limit],
        "start": start,
        "limit": limit,
        "size": len(records[start : start + limit]),
        "_links": links,
    }
    if total:
        result["totalSize"] = len(records)
    return result


def search_record(ctx: ProviderContext, page: Record) -> Record:
    space = ctx.store["spaces"].get(page["spaceId"])
    return {
        "id": page["id"],
        "content": page["id"],
        "type": page["_type"],
        "space": [space["key"], space["id"]],
        "space.key": space["key"],
        "space.id": space["id"],
        "title": page["title"],
        "text": page["title"] + " " + plain_text(page["body"]["storage"]["value"]),
        "ancestor": [a["id"] for a in ancestors(ctx, page)],
        "parent": page.get("parentId"),
        "label": [label["name"] for label in page["_labels"]],
        "creator": page["authorId"],
        "lastmodifier": page["version"]["authorId"],
        "created": page["createdAt"],
        "lastmodified": page["version"]["createdAt"],
        "_page": page,
    }


def build_v1(ctx: ProviderContext) -> APIRouter:
    router = APIRouter()

    @router.get("/content/search")
    @router.get("/search")
    async def search(request: Request) -> Record:
        grant = confluence_auth(request, ctx, "search")
        cql = request.query_params.get("cql")
        if not cql:
            fail(request, 400, "cql is required.")
        user = person_record(ctx, principal(ctx, grant))
        try:
            query = parse_query(cql, "cql", now=ctx.clock.now(), user=user["id"] if user else None)
            pages = [
                search_record(ctx, p)
                for p in ctx.store["pages"]
                if p["status"] == "current" and can_view(ctx, p, grant)
            ]
            matches = query.apply(pages)
        except QueryError as exc:
            fail(request, 400, str(exc))
        expand = request.query_params.get("expand", "")
        if request.url.path.endswith("/content/search"):
            items = [v1_content(request, ctx, r["_page"], grant, expand) for r in matches]
        else:
            items = []
            for record in matches:
                page = record["_page"]
                content_expand = ",".join(part.removeprefix("content.") for part in expand.split(","))
                content = v1_content(request, ctx, page, grant, content_expand)
                excerpt = plain_text(page["body"]["storage"]["value"])[:400]
                for term in query.text_terms:
                    if term:
                        excerpt = re.sub(
                            re.escape(term),
                            lambda m: "@@@hl@@@" + m[0] + "@@@endhl@@@",
                            excerpt,
                            flags=re.IGNORECASE,
                        )
                space = ctx.store["spaces"].get(page["spaceId"])
                items.append(
                    {
                        "content": content,
                        "title": page["title"],
                        "excerpt": excerpt,
                        "url": content["_links"]["webui"],
                        "resultGlobalContainer": {
                            "title": space["name"],
                            "displayUrl": "/spaces/" + space["key"],
                        },
                        "breadcrumbs": [],
                        "entityType": "content",
                        "lastModified": page["version"]["createdAt"],
                        "score": 1.0,
                    }
                )
        result = offset_page(request, ctx, items, total=True)
        if not request.url.path.endswith("/content/search"):
            result.update({"cqlQuery": cql, "searchDuration": 1})
        return result

    @router.get("/content/{page_id}")
    async def content(request: Request, page_id: str) -> Record:
        grant = confluence_auth(request, ctx)
        raw = ctx.store["pages"].get(page_id)
        page = get_page(
            request, ctx, grant, page_id, content_type=raw.get("_type", "page") if raw else "page"
        )
        if page["status"] != request.query_params.get("status", "current"):
            fail(request, 404, f"No content found with id: {page_id}")
        if "version" in request.query_params:
            number = integer(request, request.query_params["version"], "version", 1, 1000000)
            history = next((h for h in page["_history"] if h["version"]["number"] == number), None)
            if not history:
                fail(request, 404, "Version not found.")
            page = {**page, **copy.deepcopy(history)}
        return v1_content(request, ctx, page, grant, request.query_params.get("expand", ""))

    @router.get("/content/{page_id}/restriction/byOperation")
    async def page_restrictions(request: Request, page_id: str) -> Record:
        grant = confluence_auth(request, ctx)
        page = get_page(request, ctx, grant, page_id)
        return restrictions(request, ctx, page)

    @router.get("/content/{page_id}/restriction/byOperation/{operation}")
    async def operation_restrictions(request: Request, page_id: str, operation: str) -> Record:
        grant = confluence_auth(request, ctx)
        page = get_page(request, ctx, grant, page_id)
        if operation not in ("read", "update"):
            fail(request, 400, "Invalid restriction operation.")
        return restrictions(request, ctx, page)[operation]

    @router.get("/content/{page_id}/child/attachment")
    async def attachments(request: Request, page_id: str) -> Record:
        grant = confluence_auth(request, ctx, "attachment")
        get_page(request, ctx, grant, page_id)
        items = []
        for attachment in ctx.store["attachments"].where(pageId=page_id):
            shaped = attachment_shape(request, ctx, attachment)
            items.append(
                {
                    "id": attachment["id"],
                    "type": "attachment",
                    "status": attachment["status"],
                    "title": attachment["title"],
                    "metadata": {"mediaType": shaped["mediaType"]},
                    "extensions": {"mediaType": shaped["mediaType"], "fileSize": shaped["fileSize"]},
                    "_links": shaped["_links"],
                }
            )
        return offset_page(request, ctx, items)

    @router.get("/space")
    async def spaces(request: Request) -> Record:
        grant = confluence_auth(request, ctx, "space")
        keys = request.query_params.getlist("spaceKey")
        items = [
            v1_space(request, ctx, s)
            for s in ctx.store["spaces"]
            if space_role(ctx, s, grant)
            and (not keys or s["key"] in keys)
            and ("type" not in request.query_params or s["type"] == request.query_params["type"])
        ]
        return offset_page(request, ctx, items)

    @router.get("/user/current")
    async def current_user(request: Request) -> Record:
        grant = confluence_auth(request, ctx, "user")
        user = person_record(ctx, grant.user_id)
        if not user or grant.app_only:
            fail(request, 403, "This resource requires a user grant.")
        return user_shape(request, ctx, user)

    return router
