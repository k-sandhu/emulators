"""Deterministic per-mailbox message copies and folder trees."""

from __future__ import annotations

import base64
import html
from typing import Any

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common as c

FOLDERS = {
    "msgfolderroot": "Top of Information Store",
    "inbox": "Inbox",
    "drafts": "Drafts",
    "sentitems": "Sent Items",
    "deleteditems": "Deleted Items",
    "junkemail": "Junk Email",
    "archive": "Archive",
    "outbox": "Outbox",
    "conversationhistory": "Conversation History",
}


def folder_id(ctx: ProviderContext, uid: str, name: str) -> str:
    return "AAMkA" + base64.urlsafe_b64encode(
        bytes.fromhex(stable_hash(ctx.world.seed, "folder", uid, name, length=64))
    ).decode().rstrip("=")


def ensure_folder(ctx: ProviderContext, uid: str, path: str) -> str:
    fid = folder_id(ctx, uid, path)
    if fid not in ctx.store["mail_folders"]:
        parent = path.rsplit("/", 1)[0] if "/" in path else "msgfolderroot"
        parent_id = ensure_folder(ctx, uid, parent) if path != "msgfolderroot" else None
        ctx.store["mail_folders"].put(
            {
                "id": fid,
                "displayName": FOLDERS.get(path, path.rsplit("/", 1)[-1]),
                "parentFolderId": parent_id,
                "childFolderCount": 0,
                "unreadItemCount": 0,
                "totalItemCount": 0,
                "isHidden": False,
                "_owner": uid,
                "_path": path,
            }
        )
    return fid


def message_shape(
    ctx: ProviderContext,
    uid: str,
    mid: str,
    subject: str,
    body: dict[str, str],
    sender: dict[str, Any],
    to: list[dict[str, Any]],
    *,
    draft: bool = False,
    sent: str | None = None,
) -> dict[str, Any]:
    from emulators.providers.microsoft.directory_support import now, text_body

    stamp = sent or now(ctx)
    ck = stable_hash(mid, stamp)
    return {
        "id": mid,
        "createdDateTime": stamp,
        "lastModifiedDateTime": stamp,
        "changeKey": ck,
        "categories": [],
        "receivedDateTime": stamp,
        "sentDateTime": stamp,
        "hasAttachments": False,
        "internetMessageId": f"<{stable_hash(mid, length=40)}@{ctx.world.org.domain}>",
        "subject": subject,
        "bodyPreview": text_body(body["content"])[:255],
        "importance": "normal",
        "parentFolderId": ensure_folder(ctx, uid, "drafts" if draft else "inbox"),
        "conversationId": "AAQkA" + stable_hash(mid, length=32),
        "conversationIndex": base64.b64encode(bytes.fromhex(stable_hash(mid, length=44))).decode(),
        "isDeliveryReceiptRequested": False,
        "isReadReceiptRequested": False,
        "isRead": draft,
        "isDraft": draft,
        "webLink": f"https://outlook.office365.com/owa/?ItemID={mid}&exvsurl=1&viewmodel=ReadMessageItem",
        "inferenceClassification": "focused",
        "body": body,
        "sender": sender,
        "from": sender,
        "toRecipients": to,
        "ccRecipients": [],
        "bccRecipients": [],
        "replyTo": [],
        "flag": {"flagStatus": "notFlagged"},
        "@odata.etag": f'W/"{ck}"',
        "_owner": uid,
        "_attachment_docs": [],
        "_attachments": [],
    }


def seed(ctx: ProviderContext) -> None:
    world = ctx.world
    for person in world.employees:
        uid = c.user_id(world, person.id)
        for name in FOLDERS:
            ensure_folder(ctx, uid, name)
        office = world.office(person.office_id)
        ctx.store["mailbox_settings"].put(
            {
                "id": uid,
                "timeZone": office.windows_timezone,
                "language": {"locale": person.locale, "displayName": "English (United States)"},
                "dateFormat": "MM/dd/yyyy",
                "timeFormat": "h:mm tt",
                "userPurpose": "user",
                "workingHours": {
                    "daysOfWeek": ["monday", "tuesday", "wednesday", "thursday", "friday"],
                    "startTime": "09:00:00.0000000",
                    "endTime": "17:00:00.0000000",
                    "timeZone": {"name": office.windows_timezone},
                },
                "automaticRepliesSetting": {
                    "status": "disabled",
                    "externalAudience": "all",
                    "scheduledStartDateTime": {"dateTime": "0001-01-01T00:00:00.0000000", "timeZone": "UTC"},
                    "scheduledEndDateTime": {"dateTime": "0001-01-01T00:00:00.0000000", "timeZone": "UTC"},
                    "internalReplyMessage": "",
                    "externalReplyMessage": "",
                },
            }
        )
    for thread in world.email_threads:
        for msg in thread.messages:
            for pid in dict.fromkeys((msg.from_id, *msg.to_ids, *msg.cc_ids)):
                person = world.person(pid)
                if person.is_external:
                    continue
                uid = c.user_id(world, pid)
                mid = "AAMkA" + base64.urlsafe_b64encode(
                    bytes.fromhex(stable_hash(world.seed, "message", msg.id, pid, length=64))
                ).decode().rstrip("=")
                record = message_shape(
                    ctx,
                    uid,
                    mid,
                    msg.subject,
                    {
                        "contentType": "html",
                        "content": "<html><body>"
                        + html.escape(msg.body).replace("\n", "<br>\n")
                        + "</body></html>",
                    },
                    c.email_address(world.person(msg.from_id)),
                    [c.email_address(world.person(p)) for p in msg.to_ids],
                    sent=isoformat_z(msg.sent),
                )
                path = "sentitems" if pid == msg.from_id else "inbox"
                if path == "inbox" and thread.labels:
                    path += "/" + thread.labels[0]
                record.update(
                    {
                        "parentFolderId": ensure_folder(ctx, uid, path),
                        "ccRecipients": [c.email_address(world.person(p)) for p in msg.cc_ids],
                        "isRead": pid in msg.read_by or pid == msg.from_id,
                        "importance": msg.importance,
                        "conversationId": "AAQkA" + stable_hash(world.seed, thread.id, pid, length=32),
                        "categories": list(thread.labels),
                        "flag": {"flagStatus": "flagged" if pid in msg.flagged_by else "notFlagged"},
                        "hasAttachments": bool(msg.attachment_doc_ids),
                        "_attachment_docs": list(msg.attachment_doc_ids),
                    }
                )
                ctx.store["mail_messages"].put(record)
