"""Lazy, deterministic RFC 2822 and Gmail MessagePart serialization."""

from __future__ import annotations

import copy
import html
import re
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import format_datetime, formataddr, getaddresses
from typing import Any

from emulators.core.ids import b64url, b64url_decode, stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.google.common import google_error, json_object
from emulators.world.documents import EXTENSION_TO_MIME, render


def attachment_bytes(ctx: ProviderContext, attachment: dict[str, Any]) -> bytes:
    if not attachment.get("_blob"):
        document = ctx.world.document(attachment["_document"])
        data, _ = render(
            document.kind, document.title, document.paragraphs, table=document.table, created=document.created
        )
        attachment["_blob"] = ctx.store.blobs.put(data)
    return ctx.store.blobs.get(attachment["_blob"])


def raw_bytes(ctx: ProviderContext, record: dict[str, Any]) -> bytes:
    if record.get("_rawBlob"):
        return ctx.store.blobs.get(record["_rawBlob"])
    message = EmailMessage(policy=policy.SMTP)
    for name, value in record["_headers"]:
        message[name] = value
    message.set_content(record["_body"])
    message.add_alternative(
        "<html><body><p>" + html.escape(record["_body"]).replace("\n", "<br>\n") + "</p></body></html>",
        subtype="html",
    )
    for attachment in record.get("_attachments", []):
        maintype, subtype = attachment["mimeType"].split("/", 1)
        message.add_attachment(
            attachment_bytes(ctx, attachment),
            maintype=maintype,
            subtype=subtype,
            filename=attachment["filename"],
        )
    for index, part in enumerate(message.walk()):
        if part.is_multipart():
            part.set_boundary("=_emulator_" + stable_hash(record["id"], index))
    data = message.as_bytes()
    record["_rawBlob"] = ctx.store.blobs.put(data)
    record["sizeEstimate"] = len(data)
    return data


def _payload(
    ctx: ProviderContext, part: Message, attachments: list[dict[str, Any]], part_id: str = ""
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "partId": part_id,
        "mimeType": part.get_content_type(),
        "filename": part.get_filename() or "",
        "headers": [{"name": k, "value": v} for k, v in part.items()],
        "body": {"size": 0},
    }
    if part.is_multipart():
        result["parts"] = [
            _payload(ctx, child, attachments, f"{part_id}.{i}".lstrip("."))
            for i, child in enumerate(part.get_payload())
        ]
    else:
        data = part.get_payload(decode=True) or b""
        result["body"] = {"size": len(data)}
        if part.get_filename() or part.get_content_disposition() == "attachment":
            matching = next(
                (a for a in attachments if a["filename"] == part.get_filename() and not a.get("_used")), None
            )
            if matching:
                matching["_used"] = True
                result["body"]["attachmentId"] = matching["attachmentId"]
            else:
                result["body"]["data"] = b64url(data)
        else:
            result["body"]["data"] = b64url(data)
    return result


def message_output(
    ctx: ProviderContext, record: dict[str, Any], fmt: str = "full", headers: list[str] | None = None
) -> dict[str, Any]:
    fmt = fmt.lower()
    if fmt not in ("full", "metadata", "minimal", "raw"):
        raise google_error(400, "Invalid format", reason="invalidArgument")
    result = {k: copy.deepcopy(v) for k, v in record.items() if not k.startswith("_")}
    if fmt == "raw":
        result["raw"] = b64url(raw_bytes(ctx, record))
    elif fmt == "full":
        parsed = BytesParser(policy=policy.default).parsebytes(raw_bytes(ctx, record))
        result["payload"] = _payload(ctx, parsed, copy.deepcopy(record.get("_attachments", [])))
    elif fmt == "metadata":
        wanted = {h.lower() for h in headers or []}
        parsed = BytesParser(policy=policy.default).parsebytes(raw_bytes(ctx, record), headersonly=True)
        result["payload"] = {
            "mimeType": parsed.get_content_type(),
            "headers": [
                {"name": k, "value": str(v)} for k, v in parsed.items() if not wanted or k.lower() in wanted
            ],
        }
    result["sizeEstimate"] = record["sizeEstimate"]
    return result


def seed_message(
    ctx: ProviderContext, message: Any, thread_id: str, owner_email: str, labels: list[str]
) -> dict[str, Any]:
    sender = ctx.world.person(message.from_id)
    from_text = formataddr((sender.display_name, sender.email))
    to_text = ", ".join(
        formataddr((ctx.world.person(p).display_name, ctx.world.person(p).email)) for p in message.to_ids
    )
    cc_text = ", ".join(
        formataddr((ctx.world.person(p).display_name, ctx.world.person(p).email)) for p in message.cc_ids
    )
    rfc_id = f"<{message.id}@{ctx.world.org.domain}>"
    headers = [
        ("Delivered-To", owner_email),
        ("Received", f"by mail.{ctx.world.org.domain} with SMTP; {format_datetime(message.sent)}"),
        ("Date", format_datetime(message.sent)),
        ("From", from_text),
        ("To", to_text),
        ("Subject", message.subject),
        ("Message-ID", rfc_id),
    ]
    if cc_text:
        headers.append(("Cc", cc_text))
    if message.in_reply_to:
        reference = f"<{message.in_reply_to}@{ctx.world.org.domain}>"
        headers += [("In-Reply-To", reference), ("References", reference)]
    attachments = []
    for doc_id in message.attachment_doc_ids:
        document = ctx.world.document(doc_id)
        attachments.append(
            {
                "attachmentId": b64url(f"{message.id}:{doc_id}".encode()),
                "filename": document.name,
                "mimeType": EXTENSION_TO_MIME.get(document.kind, "application/octet-stream"),
                "_document": doc_id,
            }
        )
    return {
        "id": stable_hash(ctx.world.seed, "gmail-message", owner_email, message.id),
        "threadId": thread_id,
        "labelIds": labels,
        "snippet": snippet(message.body),
        "historyId": "1000",
        "internalDate": str(int(message.sent.timestamp() * 1000)),
        "sizeEstimate": len(message.body.encode()) + 700,
        "_headers": headers,
        "_subject": message.subject,
        "_from": from_text,
        "_to": to_text,
        "_cc": cc_text,
        "_bcc": "",
        "_body": message.body,
        "_rfcId": rfc_id,
        "_attachments": attachments,
    }


def parse_raw(
    ctx: ProviderContext, data: bytes, message_id: str, thread_id: str, labels: list[str]
) -> dict[str, Any]:
    try:
        parsed = BytesParser(policy=policy.default).parsebytes(data)
        if not parsed.get("From") and not parsed.get("To") and not parsed.get("Subject"):
            raise ValueError
        body, attachments = [], []
        for index, part in enumerate(parsed.walk()):
            if part.is_multipart():
                continue
            content = part.get_payload(decode=True) or b""
            if part.get_filename() or part.get_content_disposition() == "attachment":
                attachments.append(
                    {
                        "attachmentId": b64url(f"{message_id}:{index}".encode()),
                        "filename": part.get_filename() or "attachment",
                        "mimeType": part.get_content_type(),
                        "_blob": ctx.store.blobs.put(content),
                    }
                )
            elif part.get_content_type() == "text/plain":
                body.append(content.decode(part.get_content_charset() or "utf-8", errors="replace"))
        text = "\n".join(body)
        if not text:
            text = re_strip_html(parsed)
        return {
            "id": message_id,
            "threadId": thread_id,
            "labelIds": list(labels),
            "snippet": snippet(text),
            "historyId": "1000",
            "internalDate": str(int(ctx.clock.now().timestamp() * 1000)),
            "sizeEstimate": len(data),
            "_headers": [(k, str(v)) for k, v in parsed.items()],
            "_subject": str(parsed.get("Subject", "")),
            "_from": str(parsed.get("From", "")),
            "_to": str(parsed.get("To", "")),
            "_cc": str(parsed.get("Cc", "")),
            "_bcc": str(parsed.get("Bcc", "")),
            "_rfcId": str(parsed.get("Message-ID", f"<{message_id}@{ctx.world.org.domain}>")),
            "_body": text,
            "_attachments": attachments,
            "_rawBlob": ctx.store.blobs.put(data),
        }
    except (ValueError, TypeError, LookupError) as exc:
        raise google_error(400, "Invalid RFC 2822 message", reason="invalidArgument") from exc


def re_strip_html(parsed: Message) -> str:

    return "\n".join(
        html.unescape(
            re.sub(
                "<[^>]+>",
                " ",
                (p.get_payload(decode=True) or b"").decode(
                    p.get_content_charset() or "utf-8", errors="replace"
                ),
            )
        )
        for p in parsed.walk()
        if p.get_content_type() == "text/html"
    )


def snippet(text: str) -> str:
    return html.escape(" ".join(text.split())[:200]).replace("&#x27;", "&#39;")


async def read_upload(request: Any) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("message/rfc822"):
        return {"raw": b64url(await request.body())}
    if content_type.startswith("multipart/"):
        import json

        parsed = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + await request.body()
        )
        body: dict[str, Any] = {}
        for part in parsed.iter_parts():
            if part.get_content_type() == "application/json":
                body.update(json.loads(part.get_payload(decode=True)))
            else:
                raw = part.get_payload(decode=True)
                if raw is None and part.get_content_type() == "message/rfc822":
                    raw = part.get_payload(0).as_bytes(policy=policy.SMTP)
                body["raw"] = b64url(raw or b"")
        return body
    return await json_object(request)


def decode_raw(body: dict[str, Any]) -> bytes:
    try:
        raw = body["raw"]
        if not isinstance(raw, str):
            raise ValueError
        return b64url_decode(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise google_error(400, "'raw' RFC 2822 message is required", reason="invalidArgument") from exc


def addresses(record: dict[str, Any]) -> list[str]:
    # Parse each non-empty header on its own: newer Pythons' strict getaddresses() rejects the
    # whole list when an empty Cc/Bcc is joined in, which would hide valid To recipients.
    found: list[str] = []
    for field in ("_to", "_cc", "_bcc"):
        value = str(record.get(field) or "").strip()
        if value:
            found += [email.lower() for _, email in getaddresses([value]) if email]
    return found
