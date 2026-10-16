"""Graph Outlook mail folders, messages, actions and attachments."""

from __future__ import annotations

import base64
import copy
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common as c
from emulators.providers.microsoft import directory_support as s
from emulators.providers.microsoft import mail_helpers as h
from emulators.providers.microsoft.mail_seed import ensure_folder, seed  # noqa: F401


def register(app: FastAPI, ctx: ProviderContext) -> None:
    router = APIRouter()

    async def folders(request: Request, identifier: str = "me", fid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        uid = owner["id"]
        if fid and not request.url.path.endswith("childFolders"):
            return s.single(request, h.folder_shape(ctx, h.folder(ctx, uid, fid)), "mailFolders/$entity")
        parent = h.folder(ctx, uid, fid)["id"] if fid else ensure_folder(ctx, uid, "msgfolderroot")
        rows = [
            h.folder_shape(ctx, r)
            for r in ctx.store["mail_folders"].where(_owner=uid, parentFolderId=parent)
            if not r["isHidden"] or request.query_params.get("includeHiddenFolders") == "true"
        ]
        return s.collection(request, rows, "mailFolders")

    async def messages(request: Request, identifier: str = "me", fid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        rows = ctx.store["mail_messages"].where(_owner=owner["id"])
        if fid:
            parent = h.folder(ctx, owner["id"], fid)
            rows = [r for r in rows if r["parentFolderId"] == parent["id"]]
        if request.url.path.endswith("/delta"):
            resource = f"users/{owner['id']}/mailFolders/{parent['id']}/messages"
            result = s.delta(request, ctx, [h.shape(request, r) for r in rows], resource, "messages")
            result["value"] = [h.shape(request, r) for r in result["value"]]
            return result
        return s.collection(
            request,
            [h.shape(request, r) for r in rows],
            "messages",
            size=10,
            maximum=1000,
            search=("subject", "bodyPreview", "from", "toRecipients"),
        )

    async def get_message(request: Request, mid: str, identifier: str = "me", fid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        record = h.message(ctx, owner["id"], mid)
        if fid and record["parentFolderId"] != h.folder(ctx, owner["id"], fid)["id"]:
            raise c.not_found(mid)
        if request.url.path.endswith("/$value"):
            return Response(h.mime(ctx, record), media_type="text/plain")
        result = s.single(request, h.shape(request, record), "messages/$entity")
        if "attachments" in request.query_params.get("$expand", ""):
            result["attachments"] = h.attachments(ctx, record)
        return result

    async def attachments(
        request: Request, mid: str, identifier: str = "me", aid: str | None = None, fid: str | None = None
    ) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        record = h.message(ctx, owner["id"], mid)
        items = h.attachments(ctx, record)
        if aid:
            item = next((a for a in items if a["id"] == aid), None)
            if not item:
                raise c.not_found(aid)
            if request.url.path.endswith("/$value"):
                return Response(
                    base64.b64decode(item["contentBytes"]),
                    media_type=item.get("contentType", "application/octet-stream"),
                )
            return s.single(request, item, "attachments/$entity")
        return s.collection(request, items, "attachments")

    async def create(request: Request, identifier: str = "me", fid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        record = h.draft(ctx, owner, await request.json())
        if fid:
            record["parentFolderId"] = h.folder(ctx, owner["id"], fid)["id"]
        await h.changed(ctx, record, "created")
        return JSONResponse(s.single(request, record, "messages/$entity"), status_code=201)

    async def send_mail(request: Request, identifier: str = "me") -> Response:
        owner = s.mailbox(request, ctx, identifier)
        if "text/plain" in request.headers.get("content-type", ""):
            try:
                parsed = BytesParser(policy=policy.default).parsebytes(
                    base64.b64decode(await request.body(), validate=True)
                )
            except ValueError:
                raise c.graph_error(
                    400, "ErrorMimeContentInvalidBase64String", "Invalid base64 MIME content."
                ) from None
            body = {
                "subject": parsed.get("Subject", ""),
                "body": {
                    "contentType": "text",
                    "content": parsed.get_body(preferencelist=("plain", "html")).get_content(),
                },
                "toRecipients": [
                    {"emailAddress": {"name": name, "address": addr}}
                    for name, addr in getaddresses(parsed.get_all("To", []))
                ],
            }
            save = True
        else:
            payload = await request.json()
            body, save = payload.get("message", {}), payload.get("saveToSentItems", True)
        record = h.draft(ctx, owner, body)
        await h.deliver(ctx, record, save)
        return Response(status_code=202)

    async def patch(request: Request, mid: str, identifier: str = "me") -> Response:
        owner = s.mailbox(request, ctx, identifier)
        record = h.message(ctx, owner["id"], mid)
        data = await request.json()
        allowed = {"isRead", "flag", "categories", "importance", "inferenceClassification"}
        if record["isDraft"]:
            allowed.update({"subject", "body", "toRecipients", "ccRecipients", "bccRecipients", "replyTo"})
        record.update({k: copy.deepcopy(v) for k, v in data.items() if k in allowed})
        record.update({"lastModifiedDateTime": s.now(ctx), "changeKey": ctx.ids.urlsafe(24)})
        record["@odata.etag"] = f'W/"{record["changeKey"]}"'
        record["bodyPreview"] = s.text_body(record["body"]["content"])[:255]
        await h.changed(ctx, record, "updated")
        return JSONResponse(s.single(request, h.shape(request, record), "messages/$entity"))

    async def delete(request: Request, mid: str, identifier: str = "me") -> Response:
        owner = s.mailbox(request, ctx, identifier)
        record = h.message(ctx, owner["id"], mid)
        previous = copy.deepcopy(record)
        deleted = ensure_folder(ctx, owner["id"], "deleteditems")
        if record["parentFolderId"] == deleted:
            ctx.store["mail_messages"].delete(mid)
        else:
            ctx.store["mail_messages"].delete(mid)
            record.update(
                {"id": s.outlook_id(ctx), "parentFolderId": deleted, "lastModifiedDateTime": s.now(ctx)}
            )
            ctx.store["mail_messages"].put(record)
            await h.changed(ctx, record, "created")
        await h.changed(ctx, previous, "deleted")
        return Response(status_code=204)

    async def action(request: Request, mid: str, identifier: str = "me") -> Any:
        owner = s.mailbox(request, ctx, identifier)
        record = h.message(ctx, owner["id"], mid)
        operation = request.url.path.rsplit("/", 1)[-1]
        payload = await request.json() if await request.body() else {}
        if operation in {"move", "copy"}:
            target = h.folder(ctx, owner["id"], payload.get("destinationId", ""))
            copied = copy.deepcopy(record)
            copied.update({"id": s.outlook_id(ctx), "parentFolderId": target["id"]})
            ctx.store["mail_messages"].put(copied)
            if operation == "move":
                ctx.store["mail_messages"].delete(mid)
                await h.changed(ctx, record, "deleted")
            await h.changed(ctx, copied, "created")
            return JSONResponse(s.single(request, copied, "messages/$entity"), status_code=201)
        if operation == "send":
            if not record["isDraft"]:
                raise c.graph_error(400, "ErrorInvalidOperation", "The message is not a draft.")
            await h.deliver(ctx, record)
            return Response(status_code=202)
        reply = operation != "forward"
        body = payload.get("message", {})
        prefix = "RE: " if reply else "FW: "
        to = (record["replyTo"] or [record["from"]]) if reply else payload.get("toRecipients", [])
        if operation == "replyAll":
            to = to + [
                r
                for r in record["toRecipients"] + record["ccRecipients"]
                if r["emailAddress"]["address"].lower() != str(owner["mail"]).lower()
            ]
        body.setdefault("subject", prefix + record["subject"])
        body.setdefault("toRecipients", to)
        body.setdefault(
            "body",
            {
                "contentType": "text",
                "content": payload.get("comment", "") + "\n\n" + s.text_body(record["body"]["content"]),
            },
        )
        new = h.draft(ctx, owner, body)
        new["conversationId"] = record["conversationId"]
        if operation == "forward":
            new["_attachment_docs"] = list(record["_attachment_docs"])
            new["_attachments"] = copy.deepcopy(record["_attachments"])
            new["hasAttachments"] = record["hasAttachments"]
        if operation == "createReply":
            await h.changed(ctx, new, "created")
            return JSONResponse(s.single(request, new, "messages/$entity"), status_code=201)
        await h.deliver(ctx, new)
        return Response(status_code=202)

    async def settings(request: Request, identifier: str = "me") -> Any:
        owner = s.mailbox(request, ctx, identifier)
        record = ctx.store["mailbox_settings"].get(owner["id"])
        if request.method == "PATCH":
            record.update({k: v for k, v in (await request.json()).items() if k != "id"})
        result = s.single(request, record, "microsoft.graph.mailboxSettings")
        result.pop("id", None)
        return result

    for base in ("/me", "/users/{identifier}"):
        for suffix in ("mailFolders", "mailFolders/{fid}", "mailFolders/{fid}/childFolders"):
            router.add_api_route(f"{base}/{suffix}", folders, methods=["GET"])
        for prefix in ("messages", "mailFolders/{fid}/messages"):
            router.add_api_route(f"{base}/{prefix}", messages, methods=["GET"])
            router.add_api_route(f"{base}/{prefix}", create, methods=["POST"])
            if "mailFolders" in prefix:
                router.add_api_route(f"{base}/{prefix}/delta", messages, methods=["GET"])
            for suffix in ("{mid}", "{mid}/$value"):
                router.add_api_route(f"{base}/{prefix}/{suffix}", get_message, methods=["GET"])
            for suffix in ("{mid}/attachments", "{mid}/attachments/{aid}", "{mid}/attachments/{aid}/$value"):
                router.add_api_route(f"{base}/{prefix}/{suffix}", attachments, methods=["GET"])
        router.add_api_route(f"{base}/sendMail", send_mail, methods=["POST"])
        router.add_api_route(f"{base}/messages/{{mid}}", patch, methods=["PATCH"])
        router.add_api_route(f"{base}/messages/{{mid}}", delete, methods=["DELETE"])
        for operation in ("send", "createReply", "reply", "replyAll", "forward", "move", "copy"):
            router.add_api_route(f"{base}/messages/{{mid}}/{operation}", action, methods=["POST"])
        router.add_api_route(f"{base}/mailboxSettings", settings, methods=["GET", "PATCH"])
    for version in c.GRAPH_VERSIONS:
        app.include_router(router, prefix=f"/{version}")
