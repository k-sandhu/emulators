"""Small file attachments use the same blob storage in messages and events."""

from __future__ import annotations

import base64
import copy
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common as c
from emulators.providers.microsoft import directory_support as s


def create(ctx: ProviderContext, payload: Any) -> dict[str, Any]:
    item = copy.deepcopy(c.json_object(payload))
    if item.get("@odata.type", "#microsoft.graph.fileAttachment") != "#microsoft.graph.fileAttachment":
        raise c.graph_error(400, "ErrorInvalidAttachment", "Only file attachments are supported.")
    if not isinstance(item.get("name"), str) or not isinstance(item.get("contentBytes"), str):
        raise c.invalid_json()
    if not isinstance(item.get("contentType", "application/octet-stream"), str) or not isinstance(
        item.get("isInline", False), bool
    ):
        raise c.invalid_json()
    try:
        content = base64.b64decode(item["contentBytes"], validate=True)
    except (ValueError, UnicodeEncodeError):
        raise c.invalid_json() from None
    if len(content) >= 3 * 1024 * 1024:
        raise c.graph_error(413, "ErrorMessageSizeExceeded", "Use an upload session for large attachments.")
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": s.outlook_id(ctx),
        "name": item["name"],
        "contentType": item.get("contentType", "application/octet-stream"),
        "size": len(content),
        "isInline": item.get("isInline", False),
        "lastModifiedDateTime": s.now(ctx),
        "contentId": item.get("contentId"),
        "contentLocation": None,
        "_blob": ctx.store.blobs.put(content),
    }


def wire(ctx: ProviderContext, record: dict[str, Any]) -> dict[str, Any]:
    return {
        **s.clean(record),
        "contentBytes": base64.b64encode(ctx.store.blobs.get(record["_blob"])).decode(),
    }


def touch(ctx: ProviderContext, owner: dict[str, Any]) -> None:
    owner["lastModifiedDateTime"] = s.now(ctx)
    owner["changeKey"] = ctx.ids.urlsafe(24)
    owner["@odata.etag"] = f'W/"{owner["changeKey"]}"'


def read(request: Request, ctx: ProviderContext, items: list[dict[str, Any]], aid: str | None) -> Any:
    if aid is None:
        return s.collection(request, items, "attachments")
    item = next((item for item in items if item["id"] == aid), None)
    if item is None:
        raise c.graph_error(404, "ErrorItemNotFound", "The specified attachment was not found.")
    if request.url.path.endswith("/$value"):
        return Response(base64.b64decode(item["contentBytes"]), media_type=item["contentType"])
    return s.single(request, item, "attachments/$entity")


def created(request: Request, ctx: ProviderContext, item: dict[str, Any]) -> JSONResponse:
    return JSONResponse(s.single(request, wire(ctx, item), "attachments/$entity"), status_code=201)
