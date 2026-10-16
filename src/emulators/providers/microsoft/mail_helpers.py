"""Mailbox rendering and delivery; attachment bytes are generated only when read."""

from __future__ import annotations

import base64
import copy
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any

from fastapi import Request

from emulators.core.clock import parse_instant
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common as c
from emulators.providers.microsoft import directory_support as s
from emulators.providers.microsoft.mail_seed import ensure_folder, folder_id, message_shape
from emulators.world import documents


def folder(ctx: ProviderContext, uid: str, identifier: str) -> dict[str, Any]:
    record = ctx.store["mail_folders"].get(identifier) or ctx.store["mail_folders"].get(
        folder_id(ctx, uid, identifier.lower())
    )
    if not record or record["_owner"] != uid:
        raise c.graph_error(404, "ErrorItemNotFound", "The specified object was not found in the store.")
    return record


def message(ctx: ProviderContext, uid: str, mid: str) -> dict[str, Any]:
    record = ctx.store["mail_messages"].get(mid)
    if not record or record["_owner"] != uid:
        raise c.graph_error(404, "ErrorItemNotFound", "The specified object was not found in the store.")
    return record


def folder_shape(ctx: ProviderContext, record: dict[str, Any]) -> dict[str, Any]:
    result = s.clean(record)
    messages = ctx.store["mail_messages"].where(parentFolderId=record["id"])
    result.update(
        {
            "totalItemCount": len(messages),
            "unreadItemCount": sum(not m["isRead"] for m in messages),
            "childFolderCount": len(ctx.store["mail_folders"].where(parentFolderId=record["id"])),
        }
    )
    return result


def shape(request: Request, record: dict[str, Any]) -> dict[str, Any]:
    result = s.clean(record)
    if "body" in result and 'outlook.body-content-type="text"' in request.headers.get("prefer", "").lower():
        result["body"] = {"contentType": "text", "content": s.text_body(result["body"]["content"])}
    return result


def attachments(ctx: ProviderContext, record: dict[str, Any]) -> list[dict[str, Any]]:
    result = copy.deepcopy(record.get("_attachments", []))
    for attachment in result:
        if "_blob" in attachment:
            attachment["contentBytes"] = base64.b64encode(
                ctx.store.blobs.get(attachment.pop("_blob"))
            ).decode()
    for doc_id in record.get("_attachment_docs", []):
        doc = ctx.world.document(doc_id)
        cache = ctx.store.meta.setdefault("ms_mail_document_blobs", {})
        if doc_id not in cache:
            content, mime = documents.render(
                doc.kind,
                doc.title,
                list(doc.paragraphs),
                author=ctx.world.person(doc.owner_id).display_name,
                created=doc.created,
                table=[list(row) for row in doc.table] if doc.table else None,
            )
            cache[doc_id] = {"blob": ctx.store.blobs.put(content), "mime": mime}
        cached = cache[doc_id]
        content = ctx.store.blobs.get(cached["blob"])
        result.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "id": "AAMkA" + stable_hash(record["id"], doc_id, length=48),
                "lastModifiedDateTime": record["lastModifiedDateTime"],
                "name": doc.name,
                "contentType": cached["mime"],
                "size": len(content),
                "isInline": False,
                "contentId": None,
                "contentLocation": None,
                "contentBytes": base64.b64encode(content).decode(),
            }
        )
    return result


def mime(ctx: ProviderContext, record: dict[str, Any]) -> bytes:
    result = EmailMessage(policy=policy.SMTP)
    for key, wire in (
        ("From", "from"),
        ("To", "toRecipients"),
        ("Cc", "ccRecipients"),
        ("Reply-To", "replyTo"),
    ):
        recipients = record[wire] if isinstance(record[wire], list) else [record[wire]]
        if recipients:
            result[key] = ", ".join(
                f"{r['emailAddress'].get('name', '')} <{r['emailAddress']['address']}>" for r in recipients
            )
    result["Subject"], result["Message-ID"] = record["subject"], record["internetMessageId"]
    result["Date"] = format_datetime(parse_instant(record["sentDateTime"]))
    body = record["body"]
    result.set_content(s.text_body(body["content"]))
    if body["contentType"].lower() == "html":
        result.add_alternative(body["content"], subtype="html")
    for attachment in attachments(ctx, record):
        main, sub = attachment.get("contentType", "application/octet-stream").split("/", 1)
        result.add_attachment(
            base64.b64decode(attachment["contentBytes"]),
            maintype=main,
            subtype=sub,
            filename=attachment["name"],
        )
    return result.as_bytes()


async def changed(ctx: ProviderContext, record: dict[str, Any], change_type: str) -> None:
    if change_type in {"created", "updated"}:
        record["_published"] = True
    uid, mid = record["_owner"], record["id"]
    await c.publish_change(ctx, f"users/{uid}/messages/{mid}", change_type, record)
    await c.publish_change(
        ctx, f"users/{uid}/mailFolders/{record['parentFolderId']}/messages/{mid}", change_type, record
    )


def draft(ctx: ProviderContext, owner: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    uid = owner["id"]
    sender = {
        "emailAddress": {"name": owner["displayName"], "address": owner["mail"] or owner["userPrincipalName"]}
    }
    record = message_shape(
        ctx,
        uid,
        s.outlook_id(ctx),
        body.get("subject", ""),
        body.get("body", {"contentType": "text", "content": ""}),
        sender,
        body.get("toRecipients", []),
        draft=True,
    )
    for key in (
        "ccRecipients",
        "bccRecipients",
        "replyTo",
        "importance",
        "categories",
        "isReadReceiptRequested",
        "isDeliveryReceiptRequested",
    ):
        if key in body:
            record[key] = copy.deepcopy(body[key])
    for attachment in body.get("attachments", []):
        item = copy.deepcopy(attachment)
        content = base64.b64decode(item.get("contentBytes", ""))
        item.update(
            {
                "id": s.outlook_id(ctx),
                "@odata.type": "#microsoft.graph.fileAttachment",
                "size": len(content),
                "isInline": item.get("isInline", False),
                "lastModifiedDateTime": record["lastModifiedDateTime"],
            }
        )
        item["_blob"] = ctx.store.blobs.put(content)
        item.pop("contentBytes", None)
        record["_attachments"].append(item)
    record["hasAttachments"] = bool(record["_attachments"])
    ctx.store["mail_messages"].put(record)
    return record


async def deliver(ctx: ProviderContext, record: dict[str, Any], save: bool = True) -> None:
    if record["isDraft"] and record.get("_published"):
        await changed(ctx, copy.deepcopy(record), "deleted")
    stamp = s.now(ctx)
    record.update(
        {
            "isDraft": False,
            "isRead": True,
            "sentDateTime": stamp,
            "receivedDateTime": stamp,
            "lastModifiedDateTime": stamp,
            "parentFolderId": ensure_folder(ctx, record["_owner"], "sentitems"),
        }
    )
    seen = set()
    for recipient in record["toRecipients"] + record["ccRecipients"] + record["bccRecipients"]:
        address = recipient["emailAddress"]["address"].casefold()
        target = next(
            (
                u
                for u in ctx.store["users"]
                if str(u.get("mail", "")).casefold() == address and u.get("userType") == "Member"
            ),
            None,
        )
        if target and target["id"] not in seen:
            seen.add(target["id"])
            copied = copy.deepcopy(record)
            copied.update(
                {
                    "id": s.outlook_id(ctx),
                    "_owner": target["id"],
                    "isRead": False,
                    "parentFolderId": ensure_folder(ctx, target["id"], "inbox"),
                    "bccRecipients": [],
                }
            )
            ctx.store["mail_messages"].put(copied)
            await changed(ctx, copied, "created")
    if save:
        await changed(ctx, record, "created")
    else:
        ctx.store["mail_messages"].delete(record["id"])
