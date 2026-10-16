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

from .common import Record, api_url, clean, fail, operations, site_url
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


def write_body(request: Request, body: object) -> Record:
    if not isinstance(body, dict):
        fail(request, 400, "body must be an object.")
    if "representation" not in body:
        body = body.get("storage") or body.get("atlas_doc_format") or {}
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
        if (
            not isinstance(document, dict)
            or document.get("type") != "doc"
            or document.get("version") != 1
            or not isinstance(document.get("content"), list)
        ):
            raise ValueError
    except (ValueError, TypeError):
        fail(request, 400, "Invalid Atlassian document format.")
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


def optional_field(
    request: Request, resource: str, parent_id: str, name: str, results: list[Record]
) -> Record:
    return {
        "results": results[:50],
        "meta": {"hasMore": len(results) > 50},
        "_links": {"self": api_url(request, f"/wiki/api/v2/{resource}/{parent_id}/{name}")},
    }


def page_shape(
    request: Request, ctx: ProviderContext, page: Record, grant: Grant, *, single: bool = False
) -> Record:
    result = clean(page)
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
            ("versions", [h["version"] for h in reversed(page["_history"])]),
            ("operations", operations(ctx, page, grant)),
            (
                "properties",
                [
                    clean(p)
                    for p in ctx.store["properties"]
                    if p["_resource"] == resource and p["_parent"] == page["id"]
                ],
            ),
        ):
            if request.query_params.get("include-" + name) == "true":
                result[name] = optional_field(request, resource, page["id"], name, values)
    return result


def space_shape(request: Request, ctx: ProviderContext, space: Record) -> Record:
    result = clean(space)
    result["_links"] = {"webui": "/spaces/" + space["key"], "base": site_url(request, ctx, "/wiki")}
    rep = request.query_params.get("description-format")
    if rep == "view":
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
