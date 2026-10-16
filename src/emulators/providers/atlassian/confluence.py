"""Confluence v2 spaces, pages, hierarchy and attached resources."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.auth import Grant
from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.provider import ProviderContext

from .auth import confluence_auth
from .common import (
    Record,
    absolute,
    ancestors,
    can_view,
    clean,
    fail,
    get_page,
    integer,
    local_path,
    numeric_id,
    operations,
    page_response,
    person_record,
    principal,
    query_values,
    space_role,
)
from .hooks import emit
from .seed import new_page
from .shapes import (
    attachment_bytes,
    attachment_shape,
    body_shape,
    page_shape,
    space_shape,
    validate_body_format,
    write_body,
)


def get_space(
    request: Request, ctx: ProviderContext, grant: Grant, space_id: str, *, edit: bool = False
) -> Record:
    space = ctx.store["spaces"].get(space_id)
    if not space or not space_role(ctx, space, grant):
        fail(request, 404, "Space not found.")
    if edit and space_role(ctx, space, grant) not in ("owner", "editor"):
        fail(request, 403, "You do not have permission to create content in this space.")
    return space


def sort_records(
    request: Request, records: list[Record], fields: dict[str, Any], default: str = "id"
) -> list[Record]:
    sort = request.query_params.get("sort", default)
    name = sort.removeprefix("-")
    if name not in fields:
        fail(request, 400, "Invalid sort field.")
    return sorted(records, key=fields[name], reverse=sort.startswith("-"))


def filtered_pages(
    request: Request, ctx: ProviderContext, grant: Grant, content_type: str = "page"
) -> list[Record]:
    validate_body_format(request)
    statuses = query_values(request, "status") or (
        ["current"] if content_type == "blogpost" else ["current", "archived"]
    )
    allowed = (
        ("current", "deleted", "trashed")
        if content_type == "blogpost"
        else ("current", "archived", "deleted", "trashed")
    )
    if any(s not in allowed for s in statuses):
        fail(request, 400, "Invalid content status.")
    space_ids = query_values(request, "space-id")
    ids = query_values(request, "id")
    title = request.query_params.get("title")
    subtype = request.query_params.get("subtype")
    if content_type == "page" and subtype not in (None, "page", "live"):
        fail(request, 400, "Invalid subtype.")
    pages = [
        p
        for p in ctx.store["pages"]
        if p.get("_type") == content_type
        and p["status"] in statuses
        and (not space_ids or p["spaceId"] in space_ids)
        and (not ids or p["id"] in ids)
        and (title is None or p["title"] == title)
        and (subtype is None or p.get("subtype", "page") == subtype)
        and can_view(ctx, p, grant)
    ]
    return sort_records(
        request,
        pages,
        {
            "id": lambda p: int(p["id"]),
            "created-date": lambda p: p["createdAt"],
            "modified-date": lambda p: p["version"]["createdAt"],
            "title": lambda p: p["title"].casefold(),
        },
    )


def validate_parent(
    request: Request,
    ctx: ProviderContext,
    grant: Grant,
    parent_id: str | None,
    space_id: str,
    page_id: str | None = None,
) -> None:
    if not parent_id:
        return
    parent = get_page(request, ctx, grant, parent_id)
    if parent["status"] != "current" or parent["spaceId"] != space_id:
        fail(request, 400, "The parent must be a current page in the same space.")
    if page_id and page_id in [parent["id"], *(a["id"] for a in ancestors(ctx, parent))]:
        fail(request, 400, "A page cannot be its own ancestor.")


def unique_title(
    request: Request, ctx: ProviderContext, space_id: str, title: str, except_id: str | None = None
) -> None:
    if any(
        p["spaceId"] == space_id
        and p.get("_type") == "page"
        and p["title"].casefold() == title.casefold()
        and p["id"] != except_id
        and p["status"] in ("current", "archived")
        for p in ctx.store["pages"]
    ):
        fail(request, 400, "A page with this title already exists in this space.")


def blog_created_at(request: Request, value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}(?:Z|[+-]\d{2}:?\d{2})", value
    ):
        fail(request, 400, "createdAt must use yyyy-MM-ddTHH:mm:ss.SSSZ.")
    try:
        return parse_instant(value)
    except ValueError:
        fail(request, 400, "Invalid createdAt.")


def property_payload(request: Request, payload: Record, *, updating: bool = False) -> None:
    if (not updating or "key" in payload) and (
        not isinstance(payload.get("key"), str) or not 1 <= len(payload["key"]) <= 255
    ):
        fail(request, 400, "key must contain between 1 and 255 characters.")
    if "value" not in payload:
        fail(request, 400, "value is required.")
    try:
        value = json.dumps(payload["value"], ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail(request, 400, "value must be valid JSON.")
    if len(value) > 32768:
        fail(request, 400, "Property value exceeds 32 KB.")
    if updating:
        version = payload.get("version")
        if (
            not isinstance(version, dict)
            or type(version.get("number")) is not int
            or not 1 <= version["number"] <= 2147483647
            or not isinstance(version.get("message", ""), str)
        ):
            fail(request, 400, "version requires an integer number and optional string message.")


def build_content(router: APIRouter, ctx: ProviderContext, resource: str, content_type: str) -> None:
    @router.get("/" + resource)
    async def list_content(request: Request) -> JSONResponse:
        grant = confluence_auth(request, ctx)
        pages = filtered_pages(request, ctx, grant, content_type)
        return page_response(request, ctx, [page_shape(request, ctx, p, grant) for p in pages])

    @router.post("/" + resource)
    async def create_content(request: Request, payload: dict[str, Any]) -> Record:
        grant = confluence_auth(request, ctx)
        user = person_record(ctx, principal(ctx, grant))
        sid = payload.get("spaceId")
        if not isinstance(sid, str) or not sid:
            fail(request, 400, "spaceId must be a non-empty string.")
        space = get_space(request, ctx, grant, sid, edit=True)
        status, title = payload.get("status", "current"), payload.get("title", "")
        if content_type == "page" and payload.get("subtype") not in (None, "live"):
            fail(request, 400, "Invalid subtype.")
        if (
            status not in ("current", "draft")
            or not isinstance(title, str)
            or (status == "current" and not title.strip())
            or len(title) > 255
        ):
            fail(request, 400, "A title up to 255 characters and status current or draft are required.")
        body = write_body(request, payload.get("body", {"representation": "storage", "value": ""}))
        parent_id = payload.get("parentId")
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id.isdecimal()):
            fail(request, 400, "parentId must be a numeric string.")
        if request.query_params.get("root-level") == "true" and parent_id is not None:
            fail(request, 400, "root-level and parentId cannot be supplied together.")
        if content_type == "page" and not parent_id and request.query_params.get("root-level") != "true":
            parent_id = space["homepageId"]
        validate_parent(request, ctx, grant, parent_id, sid)
        created = ctx.clock.now()
        if content_type == "blogpost" and "createdAt" in payload:
            created = blog_created_at(request, payload["createdAt"])
        with ctx.store.lock:
            if content_type == "page" and status == "current":
                unique_title(request, ctx, sid, title)
            sequence = ctx.store.meta.get("page_sequence", 0) + 1
            ctx.store.meta["page_sequence"] = sequence
            page_id = numeric_id("created-page", sequence)
            page = new_page(
                ctx,
                page_id=page_id,
                space_id=sid,
                title=title,
                owner_id=user["_world_id"],
                created=created,
                modified=ctx.clock.now(),
                paragraphs=[],
                parent_id=parent_id,
                content_type=content_type,
            )
            page["status"], page["body"] = status, body
            if content_type == "page" and payload.get("subtype") == "live":
                page["subtype"] = "live"
            if request.query_params.get("private") == "true":
                page["_read"] = [user["_world_id"]]
                page["_update"] = [user["_world_id"]]
            page["_history"][0]["body"] = copy.deepcopy(body)
        if content_type == "page":
            await emit(ctx, "page_created", page, user["id"], request=request)
        return page_shape(request, ctx, page, grant, single=True)

    @router.get("/" + resource + "/{page_id}")
    async def get_content(request: Request, page_id: str) -> Record:
        grant = confluence_auth(request, ctx)
        page = get_page(request, ctx, grant, page_id, content_type=content_type)
        statuses = query_values(request, "status") or (
            ["draft"] if request.query_params.get("get-draft") == "true" else ["current", "archived"]
        )
        if any(
            s not in ("current", "archived", "trashed", "deleted", "historical", "draft") for s in statuses
        ):
            fail(request, 400, "Invalid content status.")
        if page["status"] not in statuses:
            fail(request, 404, f"No content found with id: {page_id}")
        if "version" in request.query_params:
            number = integer(request, request.query_params["version"], "version", 1, maximum=1000000)
            historical = next((h for h in page["_history"] if h["version"]["number"] == number), None)
            if not historical:
                fail(request, 404, "Version not found.")
            page = {
                **page,
                **copy.deepcopy(historical),
                "status": "historical" if number != page["version"]["number"] else page["status"],
            }
        return page_shape(request, ctx, page, grant, single=True)

    @router.put("/" + resource + "/{page_id}")
    async def update_content(request: Request, page_id: str, payload: dict[str, Any]) -> Record:
        grant = confluence_auth(request, ctx)
        user = person_record(ctx, principal(ctx, grant))
        page = get_page(request, ctx, grant, page_id, edit=True, content_type=content_type)
        if (
            payload.get("id") != page_id
            or not isinstance(payload.get("title"), str)
            or not payload["title"].strip()
            or len(payload["title"]) > 255
        ):
            fail(request, 400, "Matching id and non-empty title are required.")
        if payload.get("status") not in ("current", "draft"):
            fail(request, 400, "A valid status is required.")
        body = write_body(request, payload.get("body"))
        version = payload.get("version")
        if not isinstance(version, dict) or type(version.get("number")) is not int:
            fail(request, 400, "version.number is required.")
        if not isinstance(version.get("message", ""), str) or not isinstance(
            version.get("minorEdit", False), bool
        ):
            fail(request, 400, "Invalid version message or minorEdit.")
        sid = payload.get("spaceId", page["spaceId"])
        if not isinstance(sid, str) or sid != page["spaceId"]:
            fail(request, 400, "Moving content to another space is not supported by this endpoint.")
        get_space(request, ctx, grant, sid, edit=True)
        parent_id = payload.get("parentId", page.get("parentId"))
        if parent_id is not None and not isinstance(parent_id, str):
            fail(request, 400, "parentId must be a string.")
        validate_parent(request, ctx, grant, parent_id, sid, page_id)
        new_owner = payload.get("ownerId", page["ownerId"])
        if not isinstance(new_owner, str) or not ctx.store["users"].get(new_owner):
            fail(request, 400, "Invalid ownerId.")
        created = (
            blog_created_at(request, payload["createdAt"])
            if content_type == "blogpost" and "createdAt" in payload
            else None
        )
        with ctx.store.lock:
            if page["status"] in ("trashed", "deleted") and payload["status"] == "current":
                page["status"] = "current"
                return page_shape(request, ctx, page, grant, single=True)
            expected = 1 if payload["status"] == "draft" else page["version"]["number"] + 1
            if version["number"] != expected:
                fail(request, 409, "Version must be incremented by one.")
            if content_type == "page" and payload["status"] == "current":
                unique_title(request, ctx, sid, payload["title"], page_id)
            updated = {
                "number": version["number"],
                "message": str(version.get("message", "")),
                "minorEdit": bool(version.get("minorEdit", False)),
                "authorId": user["id"],
                "createdAt": isoformat_z(ctx.clock.now(), timespec="milliseconds"),
            }
            page.update(
                {
                    "title": payload["title"],
                    "status": payload["status"],
                    "spaceId": sid,
                    "body": body,
                    "version": updated,
                    "lastOwnerId": page["ownerId"] if new_owner != page["ownerId"] else page["lastOwnerId"],
                    "ownerId": new_owner,
                }
            )
            if created is not None:
                page["createdAt"] = isoformat_z(created, timespec="milliseconds")
            if parent_id:
                page.update({"parentId": parent_id, "parentType": "page"})
            else:
                page.pop("parentId", None)
                page.pop("parentType", None)
            if payload["status"] == "draft":
                page["_history"] = []
            page["_history"].append(
                {"version": copy.deepcopy(updated), "title": page["title"], "body": copy.deepcopy(body)}
            )
        if content_type == "page":
            await emit(ctx, "page_updated", page, user["id"], request=request)
        return page_shape(request, ctx, page, grant, single=True)

    @router.delete("/" + resource + "/{page_id}")
    async def delete_content(request: Request, page_id: str) -> Response:
        grant = confluence_auth(request, ctx)
        page = get_page(request, ctx, grant, page_id, edit=True, content_type=content_type)
        if request.query_params.get("purge") == "true" or request.query_params.get("draft") == "true":
            if page["status"] not in ("trashed", "draft"):
                fail(request, 400, "Only trashed pages or drafts can be purged.")
            ctx.store["pages"].delete(page_id)
        else:
            page["status"] = "trashed"
        return Response(status_code=204)


def build_properties(router: APIRouter, ctx: ProviderContext, resource: str) -> None:
    def authorize(request: Request, parent_id: str, edit: bool = False) -> Grant:
        grant = confluence_auth(request, ctx, "space" if resource == "spaces" else "page")
        if resource == "spaces":
            space = get_space(request, ctx, grant, parent_id)
            if edit and space_role(ctx, space, grant) != "owner":
                fail(request, 403, "Space administration permission is required.")
        else:
            get_page(
                request,
                ctx,
                grant,
                parent_id,
                edit=edit,
                content_type="blogpost" if resource == "blogposts" else "page",
            )
        return grant

    def find(request: Request, parent_id: str, property_id: str) -> Record:
        prop = ctx.store["properties"].get(property_id)
        if not prop or prop["_parent"] != parent_id or prop["_resource"] != resource:
            fail(request, 404, "Property not found.")
        return prop

    @router.get("/" + resource + "/{parent_id}/properties")
    async def list_properties(request: Request, parent_id: str) -> JSONResponse:
        authorize(request, parent_id)
        records = [
            clean(p)
            for p in ctx.store["properties"]
            if p["_resource"] == resource
            and p["_parent"] == parent_id
            and ("key" not in request.query_params or p["key"] == request.query_params["key"])
        ]
        if resource != "spaces":
            records = sort_records(
                request,
                records,
                {
                    "key": lambda p: p["key"],
                },
                "key",
            )
        return page_response(request, ctx, records)

    @router.post("/" + resource + "/{parent_id}/properties", status_code=201 if resource == "spaces" else 200)
    async def create_property(
        request: Request, parent_id: str, payload: dict[str, Any], response: Response
    ) -> Record:
        grant = authorize(request, parent_id, True)
        property_payload(request, payload)
        key = payload.get("key")
        if any(
            p["_resource"] == resource and p["_parent"] == parent_id and p["key"] == key
            for p in ctx.store["properties"]
        ):
            fail(request, 400, "Property key already exists.")
        sequence = ctx.store.meta.get("property_sequence", 0) + 1
        ctx.store.meta["property_sequence"] = sequence
        prop = {
            "id": numeric_id("created-property", sequence),
            "key": key,
            "value": payload["value"],
            "version": {
                "number": 1,
                "createdAt": isoformat_z(ctx.clock.now()),
                "createdBy" if resource == "spaces" else "authorId": person_record(
                    ctx, principal(ctx, grant)
                )["id"],
                "message": "",
            },
            "_resource": resource,
            "_parent": parent_id,
        }
        if resource == "spaces":
            prop.update(
                {"createdAt": prop["version"]["createdAt"], "createdBy": prop["version"]["createdBy"]}
            )
            response.headers["Location"] = absolute(request, local_path(request)) + "/" + prop["id"]
        ctx.store["properties"].put(prop)
        return clean(prop)

    @router.get("/" + resource + "/{parent_id}/properties/{property_id}")
    async def get_property(request: Request, parent_id: str, property_id: str) -> Record:
        authorize(request, parent_id)
        return clean(find(request, parent_id, property_id))

    @router.put("/" + resource + "/{parent_id}/properties/{property_id}")
    async def update_property(
        request: Request, parent_id: str, property_id: str, payload: dict[str, Any], response: Response
    ) -> Record:
        grant = authorize(request, parent_id, True)
        prop = find(request, parent_id, property_id)
        property_payload(request, payload, updating=True)
        with ctx.store.lock:
            if payload["version"].get("number") != prop["version"]["number"] + 1:
                fail(request, 409, "Version must be incremented by one.")
            key = payload.get("key", prop["key"])
            if any(
                p["id"] != property_id
                and p["_parent"] == parent_id
                and p["_resource"] == resource
                and p["key"] == key
                for p in ctx.store["properties"]
            ):
                fail(request, 400, "Property key already exists.")
            prop.update(
                {
                    "key": key,
                    "value": payload["value"],
                    "version": {
                        "number": prop["version"]["number"] + 1,
                        "createdAt": isoformat_z(ctx.clock.now()),
                        "createdBy" if resource == "spaces" else "authorId": person_record(
                            ctx, principal(ctx, grant)
                        )["id"],
                        "message": payload["version"].get("message", ""),
                    },
                }
            )
        if resource == "spaces":
            response.headers["Location"] = absolute(request, local_path(request))
        return clean(prop)

    @router.delete("/" + resource + "/{parent_id}/properties/{property_id}")
    async def delete_property(request: Request, parent_id: str, property_id: str) -> Response:
        authorize(request, parent_id, True)
        find(request, parent_id, property_id)
        ctx.store["properties"].delete(property_id)
        return Response(status_code=204)


def build_v2(ctx: ProviderContext) -> APIRouter:
    router = APIRouter()
    build_content(router, ctx, "pages", "page")
    build_content(router, ctx, "blogposts", "blogpost")
    for resource in ("pages", "spaces", "blogposts"):
        build_properties(router, ctx, resource)

    @router.get("/spaces")
    async def list_spaces(request: Request) -> JSONResponse:
        grant = confluence_auth(request, ctx, "space")
        if request.query_params.get("description-format") not in (None, "plain", "view"):
            fail(request, 400, "Invalid description-format.")
        keys, ids = query_values(request, "keys"), query_values(request, "ids")
        space_type, status = request.query_params.get("type"), request.query_params.get("status")
        if space_type not in (None, "global", "personal") or status not in (
            None,
            "current",
            "archived",
            "trashed",
        ):
            fail(request, 400, "Invalid space type or status.")
        spaces = [
            s
            for s in ctx.store["spaces"]
            if space_role(ctx, s, grant)
            and (not keys or s["key"] in keys)
            and (not ids or s["id"] in ids)
            and (space_type is None or s["type"] == space_type)
            and s["status"] == (status or "current")
        ]
        spaces = sort_records(
            request,
            spaces,
            {"id": lambda s: int(s["id"]), "key": lambda s: s["key"], "name": lambda s: s["name"]},
        )
        return page_response(request, ctx, [space_shape(request, ctx, s) for s in spaces])

    @router.get("/spaces/{space_id}")
    async def get_space_route(request: Request, space_id: str) -> Record:
        grant = confluence_auth(request, ctx, "space")
        return space_shape(request, ctx, get_space(request, ctx, grant, space_id))

    @router.get("/spaces/{space_id}/pages")
    async def space_pages(request: Request, space_id: str) -> JSONResponse:
        grant = confluence_auth(request, ctx)
        get_space(request, ctx, grant, space_id)
        depth = request.query_params.get("depth", "all")
        if depth not in ("all", "root"):
            fail(request, 400, "depth must be root or all.")
        pages = [
            p
            for p in filtered_pages(request, ctx, grant)
            if p["spaceId"] == space_id and (depth == "all" or not p.get("parentId"))
        ]
        return page_response(request, ctx, [page_shape(request, ctx, p, grant) for p in pages])

    @router.get("/spaces/{space_id}/permissions")
    async def space_permissions(request: Request, space_id: str) -> JSONResponse:
        grant = confluence_auth(request, ctx, "space")
        space = get_space(request, ctx, grant, space_id)
        return page_response(request, ctx, space["_permissions"])

    @router.get("/pages/{page_id}/children")
    @router.get("/pages/{page_id}/direct-children")
    async def children(request: Request, page_id: str) -> JSONResponse:
        grant = confluence_auth(request, ctx)
        get_page(request, ctx, grant, page_id)
        pages = [
            p
            for p in ctx.store["pages"]
            if p.get("parentId") == page_id
            and p["status"] in ("current", "archived")
            and can_view(ctx, p, grant)
        ]
        pages = sort_records(
            request,
            pages,
            {"id": lambda p: int(p["id"]), "child-position": lambda p: p["position"]},
            "child-position",
        )
        result = [
            {
                "id": p["id"],
                "status": p["status"],
                "title": p["title"],
                "spaceId": p["spaceId"],
                "childPosition": p["position"],
            }
            for p in pages
        ]
        if request.url.path.endswith("/direct-children"):
            for item in result:
                item["type"] = "page"
        return page_response(request, ctx, result)

    @router.get("/pages/{page_id}/descendants")
    async def descendants(request: Request, page_id: str) -> JSONResponse:
        grant = confluence_auth(request, ctx, "hierarchy")
        get_page(request, ctx, grant, page_id)
        depth = integer(request, request.query_params.get("depth"), "depth", 2, 10)
        result: list[Record] = []

        def walk(parent_id: str, level: int) -> None:
            if level > depth:
                return
            for child in sorted(ctx.store["pages"].where(parentId=parent_id), key=lambda p: p["position"]):
                if child["status"] in ("current", "archived") and can_view(ctx, child, grant):
                    result.append(
                        {
                            "id": child["id"],
                            "status": child["status"],
                            "title": child["title"],
                            "type": "page",
                            "parentId": parent_id,
                            "depth": level,
                            "childPosition": child["position"],
                        }
                    )
                    walk(child["id"], level + 1)

        walk(page_id, 1)
        return page_response(request, ctx, result)

    @router.get("/pages/{page_id}/ancestors")
    async def page_ancestors(request: Request, page_id: str) -> JSONResponse:
        grant = confluence_auth(request, ctx)
        page = get_page(request, ctx, grant, page_id)
        limit = integer(request, request.query_params.get("limit"), "limit", 25)
        result = [{"id": p["id"], "type": "page"} for p in ancestors(ctx, page) if can_view(ctx, p, grant)]
        return JSONResponse({"results": result[-limit:], "_links": {}})

    @router.get("/pages/{page_id}/versions")
    async def versions(request: Request, page_id: str) -> JSONResponse:
        grant = confluence_auth(request, ctx)
        validate_body_format(request)
        page = get_page(request, ctx, grant, page_id)
        histories = sort_records(
            request,
            page["_history"],
            {
                "modified-date": lambda h: h["version"]["createdAt"],
            },
            "modified-date",
        )
        return page_response(
            request,
            ctx,
            [
                {**h["version"], "page": {"id": page_id, "title": h["title"], "body": body_shape(request, h)}}
                for h in histories
            ],
        )

    @router.get("/pages/{page_id}/labels")
    async def labels(request: Request, page_id: str) -> JSONResponse:
        grant = confluence_auth(request, ctx)
        page = get_page(request, ctx, grant, page_id)
        labels = [
            label
            for label in page["_labels"]
            if "prefix" not in request.query_params or label["prefix"] == request.query_params["prefix"]
        ]
        return page_response(request, ctx, labels)

    @router.get("/labels/{label_id}/pages")
    async def label_pages(request: Request, label_id: str) -> JSONResponse:
        grant = confluence_auth(request, ctx)
        pages = [
            p
            for p in filtered_pages(request, ctx, grant)
            if any(label["id"] == label_id for label in p["_labels"])
        ]
        return page_response(request, ctx, [page_shape(request, ctx, p, grant) for p in pages])

    @router.get("/pages/{page_id}/operations")
    async def page_operations(request: Request, page_id: str) -> Record:
        grant = confluence_auth(request, ctx)
        page = get_page(request, ctx, grant, page_id)
        return {"operations": operations(ctx, page, grant)}

    @router.get("/pages/{page_id}/attachments")
    async def attachments(request: Request, page_id: str) -> JSONResponse:
        grant = confluence_auth(request, ctx, "attachment")
        get_page(request, ctx, grant, page_id)
        items = [a for a in ctx.store["attachments"] if a["pageId"] == page_id and a["status"] == "current"]
        return page_response(request, ctx, [attachment_shape(request, ctx, a) for a in items])

    @router.get("/attachments/{attachment_id}")
    async def attachment(request: Request, attachment_id: str) -> Record:
        grant = confluence_auth(request, ctx, "attachment")
        item = ctx.store["attachments"].get("att" + attachment_id.removeprefix("att"))
        if not item:
            fail(request, 404, "Attachment not found.")
        get_page(request, ctx, grant, item["pageId"])
        return attachment_shape(request, ctx, item)

    @router.get("/pages/{page_id}/footer-comments")
    async def comments(request: Request, page_id: str) -> JSONResponse:
        grant = confluence_auth(request, ctx)
        get_page(request, ctx, grant, page_id)
        items = [clean(c) for c in ctx.store["comments"] if c["pageId"] == page_id]
        for item in items:
            item["body"] = body_shape(request, item)
            item["_links"] = {"webui": "/pages/viewpage.action?pageId=" + page_id + "#comment-" + item["id"]}
        return page_response(request, ctx, items)

    @router.post("/users-bulk")
    async def users(request: Request, payload: dict[str, Any]) -> JSONResponse:
        confluence_auth(request, ctx, "user")
        ids = payload.get("accountIds")
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 250
            or any(not isinstance(uid, str) for uid in ids)
        ):
            fail(request, 400, "accountIds must contain between 1 and 250 account ids.")
        items = []
        for uid in dict.fromkeys(ids):
            user = ctx.store["users"].get(uid)
            if user:
                item = clean(user)
                item.pop("id")
                item["profilePicture"] = {
                    "path": "/wiki/aa-avatar/" + quote(uid),
                    "width": 48,
                    "height": 48,
                    "isDefault": True,
                }
                items.append(item)
        return JSONResponse({"results": items, "_links": {}})

    return router


def build_downloads(ctx: ProviderContext) -> APIRouter:
    router = APIRouter()

    @router.get("/wiki/download/attachments/{page_id}/{filename}")
    async def download(request: Request, page_id: str, filename: str) -> Response:
        grant = confluence_auth(request, ctx, "attachment")
        get_page(request, ctx, grant, page_id)
        attachment = ctx.store["attachments"].first(pageId=page_id, title=filename, status="current")
        if not attachment:
            fail(request, 404, "Attachment not found.")
        content = attachment_bytes(ctx, attachment)
        return Response(
            content,
            media_type=attachment["mediaType"],
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""),
                "ETag": '"' + attachment["_blob"] + '"',
            },
        )

    return router
