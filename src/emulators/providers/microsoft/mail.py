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
from emulators.providers.microsoft import outlook_attachments as attachments_api
from emulators.providers.microsoft import outlook_payloads as payloads
from emulators.providers.microsoft.mail_seed import ensure_folder, ensure_mailbox, seed  # noqa: F401


def register(app: FastAPI, ctx: ProviderContext) -> None:
    router = APIRouter()

    async def folders(request: Request, identifier: str = "me", fid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        uid = owner["id"]
        if request.url.path.endswith("/delta"):
            rows = [
                h.folder_shape(ctx, r)
                for r in ctx.store["mail_folders"].where(_owner=uid)
                if r["parentFolderId"] is not None
            ]
            return s.delta(request, ctx, rows, f"users/{uid}/mailFolders", "mailFolders")
        if request.method == "POST":
            body = await c.json_body(request)
            if not isinstance(body.get("displayName"), str) or not body["displayName"].strip():
                raise c.graph_error(400, "ErrorInvalidParameter", "displayName is required.")
            if "isHidden" in body and not isinstance(body["isHidden"], bool):
                raise c.invalid_json()
            parent = h.folder(ctx, uid, fid)["id"] if fid else ensure_folder(ctx, uid, "msgfolderroot")
            record = {
                "id": s.outlook_id(ctx),
                "displayName": body["displayName"],
                "parentFolderId": parent,
                "childFolderCount": 0,
                "unreadItemCount": 0,
                "totalItemCount": 0,
                "isHidden": body.get("isHidden", False),
                "_owner": uid,
            }
            ctx.store["mail_folders"].put(record)
            await c.publish_change(ctx, f"users/{uid}/mailFolders/{record['id']}", "created", record)
            return JSONResponse(s.single(request, record, "mailFolders/$entity"), status_code=201)
        if fid and not request.url.path.endswith("childFolders"):
            return s.single(request, h.folder_shape(ctx, h.folder(ctx, uid, fid)), "mailFolders/$entity")
        parent = h.folder(ctx, uid, fid)["id"] if fid else ensure_folder(ctx, uid, "msgfolderroot")
        rows = [
            h.folder_shape(ctx, r)
            for r in ctx.store["mail_folders"].where(_owner=uid, parentFolderId=parent)
            if not r["isHidden"] or request.query_params.get("includeHiddenFolders") == "true"
        ]
        return s.collection(request, rows, "mailFolders", size=10)

    async def messages(request: Request, identifier: str = "me", fid: str | None = None) -> Any:
        owner = s.mailbox(request, ctx, identifier)
        rows = ctx.store["mail_messages"].where(_owner=owner["id"])
        if fid:
            parent = h.folder(ctx, owner["id"], fid)
            rows = [r for r in rows if r["parentFolderId"] == parent["id"]]
        if request.url.path.endswith("/$count"):
            return c.count_response(request, rows)
        if not request.query_params.get("$orderby"):
            rows = sorted(rows, key=lambda row: row["receivedDateTime"], reverse=True)
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
        if fid and record["parentFolderId"] != h.folder(ctx, owner["id"], fid)["id"]:
            raise c.not_found(mid)
        if request.method == "POST":
            item = attachments_api.create(ctx, await c.json_body(request))
            record["_attachments"].append(item)
            record["hasAttachments"] = record["hasAttachments"] or not item["isInline"]
            attachments_api.touch(ctx, record)
            await h.changed(ctx, record, "updated")
            return attachments_api.created(request, ctx, item)
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
        parent_id = h.folder(ctx, owner["id"], fid)["id"] if fid else None
        record = h.draft(ctx, owner, await c.json_body(request))
        if parent_id:
            record["parentFolderId"] = parent_id
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
                    "content": (parsed.get_body(preferencelist=("plain", "html")) or parsed).get_content(),
                },
                "toRecipients": [
                    {"emailAddress": {"name": name, "address": addr}}
                    for name, addr in getaddresses(parsed.get_all("To", []))
                ],
            }
            save = True
        else:
            payload = await c.json_body(request)
            body, save = c.json_object(payload.get("message")), payload.get("saveToSentItems", True)
            if not isinstance(save, bool):
                raise c.invalid_json()
        body = payloads.message(body, sending=True)
        record = h.draft(ctx, owner, body)
        await h.deliver(ctx, record, save)
        return Response(status_code=202)

    async def patch(request: Request, mid: str, identifier: str = "me", fid: str | None = None) -> Response:
        owner = s.mailbox(request, ctx, identifier)
        record = h.message(ctx, owner["id"], mid)
        if fid and record["parentFolderId"] != h.folder(ctx, owner["id"], fid)["id"]:
            raise c.not_found(mid)
        data = payloads.message(await c.json_body(request))
        allowed = {"isRead", "flag", "categories", "importance", "inferenceClassification"}
        if record["isDraft"]:
            allowed.update({"subject", "body", "toRecipients", "ccRecipients", "bccRecipients", "replyTo"})
        record.update({k: copy.deepcopy(v) for k, v in data.items() if k in allowed})
        record.update({"lastModifiedDateTime": s.now(ctx), "changeKey": ctx.ids.urlsafe(24)})
        record["@odata.etag"] = f'W/"{record["changeKey"]}"'
        record["bodyPreview"] = s.text_body(record["body"]["content"])[:255]
        await h.changed(ctx, record, "updated")
        return JSONResponse(s.single(request, h.shape(request, record), "messages/$entity"))

    async def delete(request: Request, mid: str, identifier: str = "me", fid: str | None = None) -> Response:
        owner = s.mailbox(request, ctx, identifier)
        record = h.message(ctx, owner["id"], mid)
        if fid and record["parentFolderId"] != h.folder(ctx, owner["id"], fid)["id"]:
            raise c.not_found(mid)
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
        payload = await c.json_body(request, optional=True)
        if "comment" in payload and not isinstance(payload["comment"], str):
            raise c.invalid_json()
        if operation in {"move", "copy"}:
            if not isinstance(payload.get("destinationId"), str):
                raise c.invalid_json()
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
        reply = operation not in ("forward", "createForward")
        body = c.json_object(payload.get("message", {}))
        prefix = "RE: " if reply else "FW: "
        to = (record["replyTo"] or [record["from"]]) if reply else payload.get("toRecipients", [])
        if operation in ("replyAll", "createReplyAll"):
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
        body = payloads.message(body, sending=not operation.startswith("create"))
        new = h.draft(ctx, owner, body)
        new["conversationId"] = record["conversationId"]
        if not reply:
            new["_attachment_docs"] = list(record["_attachment_docs"])
            new["_attachments"] = copy.deepcopy(record["_attachments"])
            new["hasAttachments"] = record["hasAttachments"]
        if operation.startswith("create"):
            await h.changed(ctx, new, "created")
            return JSONResponse(s.single(request, new, "messages/$entity"), status_code=201)
        await h.deliver(ctx, new)
        return Response(status_code=202)

    async def settings(request: Request, identifier: str = "me") -> Any:
        owner = s.mailbox(request, ctx, identifier)
        record = ensure_mailbox(ctx, owner["id"])
        if request.method == "PATCH":
            body = await c.json_body(request)
            for key in ("automaticRepliesSetting", "workingHours", "language"):
                if key in body:
                    c.json_object(body[key])
            for key in ("timeZone", "timeFormat", "dateFormat"):
                if key in body and not isinstance(body[key], str):
                    raise c.invalid_json()
            record.update({k: v for k, v in body.items() if k != "id" and not k.startswith(("_", "@"))})
        result = s.single(request, record, "microsoft.graph.mailboxSettings")
        result.pop("id", None)
        return result

    for base in ("/me", "/users/{identifier}"):
        for suffix in (
            "mailFolders",
            "mailFolders/delta",
            "mailFolders/{fid}",
            "mailFolders/{fid}/childFolders",
        ):
            router.add_api_route(f"{base}/{suffix}", folders, methods=["GET"])
        for suffix in ("mailFolders", "mailFolders/{fid}/childFolders"):
            router.add_api_route(f"{base}/{suffix}", folders, methods=["POST"])
        for prefix in ("messages", "mailFolders/{fid}/messages"):
            router.add_api_route(f"{base}/{prefix}", messages, methods=["GET"])
            router.add_api_route(f"{base}/{prefix}", create, methods=["POST"])
            if "mailFolders" in prefix:
                router.add_api_route(f"{base}/{prefix}/delta", messages, methods=["GET"])
            router.add_api_route(f"{base}/{prefix}/$count", messages, methods=["GET"])
            for suffix in ("{mid}", "{mid}/$value"):
                router.add_api_route(f"{base}/{prefix}/{suffix}", get_message, methods=["GET"])
            for suffix in ("{mid}/attachments", "{mid}/attachments/{aid}", "{mid}/attachments/{aid}/$value"):
                router.add_api_route(f"{base}/{prefix}/{suffix}", attachments, methods=["GET"])
            router.add_api_route(f"{base}/{prefix}/{{mid}}/attachments", attachments, methods=["POST"])
            router.add_api_route(f"{base}/{prefix}/{{mid}}", patch, methods=["PATCH"])
            router.add_api_route(f"{base}/{prefix}/{{mid}}", delete, methods=["DELETE"])
        router.add_api_route(f"{base}/sendMail", send_mail, methods=["POST"])
        router.add_api_route(f"{base}/messages/{{mid}}", patch, methods=["PATCH"])
        router.add_api_route(f"{base}/messages/{{mid}}", delete, methods=["DELETE"])
        for operation in (
            "send",
            "createReply",
            "createReplyAll",
            "createForward",
            "reply",
            "replyAll",
            "forward",
            "move",
            "copy",
        ):
            router.add_api_route(f"{base}/messages/{{mid}}/{operation}", action, methods=["POST"])
        router.add_api_route(f"{base}/mailboxSettings", settings, methods=["GET", "PATCH"])
    for version in c.GRAPH_VERSIONS:
        app.include_router(router, prefix=f"/{version}")
