"""Typed Teams message payloads and message-scoped hosted binary content."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common
from emulators.providers.microsoft import drive_model as dm

CONTENTS = "ms_teams_hosted_contents"


def validate(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("attachments", "mentions", "hostedContents"):
        if key in payload and (
            not isinstance(payload[key], list) or any(not isinstance(v, dict) for v in payload[key])
        ):
            raise common.graph_error(400, "BadRequest", f"{key} must be an array of objects.")
    for key in ("subject", "summary", "locale"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], str):
            raise common.graph_error(400, "BadRequest", f"{key} must be a string.")
    if "importance" in payload and payload["importance"] not in ("normal", "high", "urgent"):
        raise common.graph_error(400, "BadRequest", "Invalid importance.")
    for attachment in payload.get("attachments", []):
        if not isinstance(attachment.get("contentType"), str) or not attachment["contentType"]:
            raise common.graph_error(400, "BadRequest", "An attachment requires contentType.")
        for key in ("id", "contentUrl", "content", "name", "thumbnailUrl"):
            if attachment.get(key) is not None and not isinstance(attachment[key], str):
                raise common.graph_error(400, "BadRequest", f"Attachment {key} must be a string.")
    for mention in payload.get("mentions", []):
        identity = mention.get("mentioned")
        if (
            type(mention.get("id")) is not int
            or mention["id"] < 0
            or not isinstance(mention.get("mentionText"), str)
            or not isinstance(identity, dict)
            or not identity
            or any(
                not isinstance(v, dict) or not isinstance(v.get("id"), str)
                for key, v in identity.items()
                if not key.startswith("@")
            )
        ):
            raise common.graph_error(400, "BadRequest", "Invalid message mention.")
    decoded = []
    ids: set[str] = set()
    for content in payload.get("hostedContents", []):
        temporary = content.get("@microsoft.graph.temporaryId")
        mime = content.get("contentType")
        if (
            not isinstance(temporary, str)
            or not temporary
            or temporary in ids
            or not isinstance(mime, str)
            or "/" not in mime
            or not mime.isascii()
            or any(c in mime for c in "\r\n")
            or not isinstance(content.get("contentBytes"), str)
        ):
            raise common.graph_error(400, "BadRequest", "Invalid hosted content.")
        try:
            data = base64.b64decode(content["contentBytes"], validate=True)
        except (binascii.Error, ValueError):
            raise common.graph_error(400, "BadRequest", "contentBytes must be base64 encoded.") from None
        ids.add(temporary)
        decoded.append({"temporary": temporary, "mime": mime, "data": data})
    return decoded


def store(
    request: Request, ctx: ProviderContext, message: dict[str, Any], contents: list[dict[str, Any]]
) -> None:
    for content in contents:
        cid = ctx.ids.urlsafe(96)
        ctx.store[CONTENTS].put_as(
            message["_scope"] + "/" + message["id"] + "/" + cid,
            {
                "id": cid,
                "_scope": message["_scope"],
                "_message": message["id"],
                "_blob": ctx.store.blobs.put(content["data"]),
                "_mime": content["mime"],
            },
        )
        path = dm.path_of(request).rstrip("/") + f"/{message['id']}/hostedContents/{cid}/$value"
        message["body"]["content"] = message["body"]["content"].replace(
            f"../hostedContents/{content['temporary']}/$value", dm.graph_url(request, ctx, path)
        )


def handle(request: Request, ctx: ProviderContext, message: dict[str, Any], operation: str) -> Any:
    if request.method != "GET":
        raise common.graph_error(405, "methodNotAllowed", "Hosted content is read only.")
    parts = operation.split("/")
    records = ctx.store[CONTENTS].where(_scope=message["_scope"], _message=message["id"])
    if len(parts) == 1:
        return dm.collection(
            request,
            [{"id": r["id"], "contentBytes": None, "contentType": None} for r in records],
            context="Collection(chatMessageHostedContent)",
        )
    record = next((r for r in records if r["id"] == parts[1]), None)
    if record is None:
        raise common.item_not_found()
    if len(parts) == 2:
        return dm.single(
            request,
            {"id": record["id"], "contentBytes": None, "contentType": None},
            "chatMessageHostedContent",
        )
    if len(parts) == 3 and parts[2] == "$value":
        return Response(ctx.store.blobs.get(record["_blob"]), media_type=record["_mime"])
    raise common.item_not_found()
