"""Confluence representations, including lazily rendered attachment bytes."""

from __future__ import annotations

import copy
import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import quote

from fastapi import Request

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext
from emulators.world.documents import render

from .common import (
    Record,
    api_url,
    clean,
    cursor_binding,
    cursor_token,
    fail,
    gateway_prefix,
    operations,
    site_url,
)
from .seed import adf, storage_body


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: str) -> str:
    parser = TextParser()
    parser.feed(value)
    return " ".join(parser.parts)


def adf_text(node: object) -> list[str]:
    if isinstance(node, dict):
        return ([node["text"]] if isinstance(node.get("text"), str) else []) + adf_text(
            node.get("content", [])
        )
    if isinstance(node, list):
        return [text for child in node for text in adf_text(child)]
    return []


def validate_adf(request: Request, value: object) -> None:
    if (
        not isinstance(value, dict)
        or value.get("type") != "doc"
        or type(value.get("version")) is not int
        or value.get("version") != 1
        or not isinstance(value.get("content"), list)
    ):
        fail(request, 400, "Operation value must be an Atlassian Document (ADF).")
    pending = [(value, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if not isinstance(node, dict) or not isinstance(node.get("type"), str) or depth > 64 or count > 10000:
            fail(request, 400, "Invalid or overly complex ADF node.")
        if node["type"] == "text" and (not isinstance(node.get("text"), str) or not node["text"]):
            fail(request, 400, "ADF text nodes require non-empty text.")
        if "attrs" in node and not isinstance(node["attrs"], dict):
            fail(request, 400, "ADF attrs must be an object.")
        if "content" in node:
            if not isinstance(node["content"], list):
                fail(request, 400, "ADF content must be an array.")
            pending.extend((child, depth + 1) for child in node["content"])


def write_body(request: Request, body: object) -> Record:
    if not isinstance(body, dict):
        fail(request, 400, "body must be an object.")
    if "representation" not in body:
        nested = [body[key] for key in ("storage", "atlas_doc_format") if key in body]
        if len(nested) != 1 or not isinstance(nested[0], dict):
            fail(request, 400, "Exactly one body representation object is required.")
        body = nested[0]
    representation, value = body.get("representation"), body.get("value")
    if not isinstance(value, str) or representation not in ("storage", "atlas_doc_format"):
        fail(request, 400, "body requires a storage or atlas_doc_format representation and string value.")
    if representation == "storage":
        return {
            "storage": {"representation": "storage", "value": value},
            "atlas_doc_format": {
                "representation": "atlas_doc_format",
                "value": json.dumps(adf([plain_text(value)])),
            },
        }
    try:
        document = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        fail(request, 400, "Invalid Atlassian document format.")
    validate_adf(request, document)
    return {
        "atlas_doc_format": {"representation": "atlas_doc_format", "value": value},
        "storage": {"representation": "storage", "value": storage_body("", adf_text(document))},
    }


def body_shape(request: Request, page: Record, representation: str | None = None) -> Record:
    rep = representation if representation is not None else request.query_params.get("body-format")
    if not rep:
        return {}
    if rep not in ("storage", "atlas_doc_format", "view"):
        fail(request, 400, "Invalid body-format.")
    if rep == "view":
        markup = page["body"]["storage"]["value"]
        markup = re.sub(r"</?ac:[^>]*>", "", markup)
        return {"view": {"value": markup, "representation": "view"}}
    return {rep: copy.deepcopy(page["body"][rep])}


def validate_body_format(request: Request) -> None:
    """Validate even when the requested collection has no results."""
    rep = request.query_params.get("body-format")
    if rep is not None and rep not in ("storage", "atlas_doc_format", "view"):
        fail(request, 400, "Invalid body-format.")


def optional_field(
    request: Request, resource: str, parent_id: str, name: str, results: list[Record]
) -> Record:
    path = gateway_prefix(request) + f"/wiki/api/v2/{resource}/{parent_id}/{name}"
    meta: Record = {"hasMore": len(results) > 50}
    if meta["hasMore"]:
        meta["cursor"] = cursor_token(50, cursor_binding(request, path, []))
    return {
        "results": results[:50],
        "meta": meta,
        "_links": {"self": api_url(request, f"/wiki/api/v2/{resource}/{parent_id}/{name}")},
    }


def page_shape(
    request: Request, ctx: ProviderContext, page: Record, grant: Grant, *, single: bool = False
) -> Record:
    result = clean(page)
    if page.get("_type") == "blogpost":
        for key in ("ownerId", "lastOwnerId", "parentId", "parentType", "position", "subtype"):
            result.pop(key, None)
    result["body"] = body_shape(request, page)
    space = ctx.store["spaces"].get(page["spaceId"])
    result["_links"] = {
        "webui": f"/spaces/{space['key']}/pages/{page['id']}/{quote(page['title'], safe='')}",
        "editui": f"/pages/resumedraft.action?draftId={page['id']}",
        "tinyui": f"/x/{page['id']}",
    }
    if single:
        result["_links"]["base"] = site_url(request, ctx, "/wiki")
        if request.query_params.get("include-version") == "false":
            result.pop("version", None)
        resource = "blogposts" if page.get("_type") == "blogpost" else "pages"
        for name, values in (
            ("labels", page["_labels"]),
            ("versions", [h["version"] for h in page["_history"]]),
            ("operations", operations(ctx, page, grant)),
            (
                "properties",
                sorted(
                    [
                        clean(p)
                        for p in ctx.store["properties"]
                        if p["_resource"] == resource and p["_parent"] == page["id"]
                    ],
                    key=lambda p: p["key"],
                ),
            ),
        ):
            if request.query_params.get("include-" + name) == "true":
                result[name] = optional_field(request, resource, page["id"], name, values)
    return result


def space_shape(request: Request, ctx: ProviderContext, space: Record) -> Record:
    result = clean(space)
    result["_links"] = {"webui": "/spaces/" + space["key"], "base": site_url(request, ctx, "/wiki")}
    rep = request.query_params.get("description-format")
    if rep is None:
        result.pop("description", None)
    elif rep == "view":
        result["description"] = {
            "view": {
                "value": "<p>" + html.escape(space["description"]["plain"]["value"]) + "</p>",
                "representation": "view",
            }
        }
    elif rep not in (None, "plain"):
        fail(request, 400, "Invalid description-format.")
    if request.query_params.get("include-icon") == "true":
        result["icon"] = {"path": "/wiki/images/logo/default-space-logo.svg", "apiDownloadLink": ""}
    if request.query_params.get("include-permissions") == "true":
        result["permissions"] = optional_field(
            request, "spaces", space["id"], "permissions", space["_permissions"]
        )
    return result


def attachment_bytes(ctx: ProviderContext, attachment: Record) -> bytes:
    if not attachment.get("_blob"):
        doc = ctx.world.document(attachment["_world_id"])
        content, mime = render(
            doc.kind,
            doc.title,
            list(doc.paragraphs),
            author=ctx.world.person(doc.owner_id).display_name,
            created=doc.created,
            table=[list(row) for row in doc.table] if doc.table else None,
        )
        attachment["_blob"] = ctx.store.blobs.put(content)
        attachment["mediaType"] = mime
        attachment["fileSize"] = len(content)
    return ctx.store.blobs.get(attachment["_blob"])


def attachment_shape(request: Request, ctx: ProviderContext, attachment: Record) -> Record:
    attachment_bytes(ctx, attachment)
    result = clean(attachment)
    path = f"/wiki/download/attachments/{attachment['pageId']}/{quote(attachment['title'], safe='')}"
    result["downloadLink"] = api_url(request, path)
    result["webuiLink"] = site_url(
        request, ctx, f"/wiki/pages/viewpageattachments.action?pageId={attachment['pageId']}"
    )
    result["_links"] = {
        "download": result["downloadLink"],
        "webui": result["webuiLink"],
        "base": site_url(request, ctx, "/wiki"),
    }
    return result
