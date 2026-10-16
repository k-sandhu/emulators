"""``/_emulator/actions/*`` handlers for the Meta emulator.

``mint_token`` (user|page|system_user|app), ``configure_webhook`` (runs Meta's
``hub.challenge`` verification), ``inbound_whatsapp``, ``deliver_statuses``,
``approve_template``/``reject_template`` and ``inbound_messenger``.
"""

from __future__ import annotations

import base64
from datetime import timedelta
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.meta.common import (
    APP_ID,
    DISPLAY_PHONE_DIGITS,
    digits_only,
    graph_time,
    issue_meta_token,
    new_mid,
    new_wamid,
    numeric_id,
)
from emulators.providers.meta.seed import fb_user_id, psid_for
from emulators.providers.meta.webhooks_out import (
    configure_webhook_verified,
    deliver_ms_delivery_and_read,
    deliver_ms_inbound,
    deliver_wa_inbound,
    deliver_wa_status,
    failed_errors,
)


def _find_person(ctx: ProviderContext, ref: str):
    ref = str(ref)
    for person in ctx.world.people:
        if ref in (person.id, person.email, person.username) or fb_user_id(person.id) == ref:
            return person
    for lead in ctx.world.leads:
        if ref in (lead.id, lead.email):
            return lead
    return None


def _display_name(person: Any) -> str:
    return getattr(person, "display_name", None) or f"{person.first_name} {person.last_name}"


def _parse_scopes(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(s) for s in raw]
    return [s for s in str(raw).replace(",", " ").split() if s]


def _ensure_fb_user(ctx: ProviderContext, person: Any) -> str:
    fb_id = fb_user_id(person.id)
    coll = ctx.store.collection("fb_users")
    if coll.get(fb_id) is None:
        coll.put(
            {
                "id": fb_id,
                "name": _display_name(person),
                "first_name": person.first_name,
                "last_name": person.last_name,
                "email": getattr(person, "email", ""),
                "link": f"https://www.facebook.com/{fb_id}",
                "locale": str(getattr(person, "locale", "en-US")).replace("-", "_"),
                "timezone": getattr(person, "timezone", "America/Toronto"),
                "_person_id": person.id,
            }
        )
    return fb_id


def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Mint a Meta token: {"type": user|page|system_user|app, "user": id|email, "scopes", "expires_in"}."""
    raw_type = str(
        payload.get("type")
        or payload.get("token_type")
        or payload.get("kind")
        or payload.get("meta_type")
        or ""
    ).lower()
    mapping = {
        "user": "USER",
        "page": "PAGE",
        "system_user": "SYSTEM_USER",
        "system": "SYSTEM_USER",
        "app": "APP",
    }
    meta_type = mapping.get(raw_type)
    user_ref = payload.get("user") or payload.get("user_id") or payload.get("person")
    if meta_type is None:
        meta_type = "USER" if user_ref else "SYSTEM_USER"
    scopes = _parse_scopes(payload.get("scopes") or payload.get("scope"))
    client_id = str(payload.get("client_id") or APP_ID)
    if "expires_in" in payload and payload["expires_in"] is not None:
        ttl: timedelta | None = timedelta(seconds=int(payload["expires_in"]))
        expires_in: int | None = int(payload["expires_in"])
    elif meta_type == "USER":
        ttl, expires_in = timedelta(hours=1), 3600
    else:
        ttl, expires_in = None, None
    meta = ctx.store.meta
    if meta_type == "APP":
        client = ctx.tokens.client(client_id)
        secret = str(
            payload.get("client_secret") or (client or {}).get("client_secret") or meta["app_secret"]
        )
        if client is None:
            ctx.tokens.register_client(client_id, secret)
        return {
            "access_token": f"{client_id}|{secret}",
            "token_type": "bearer",
            "type": "APP",
            "app_id": client_id,
        }
    if meta_type == "PAGE":
        page_id = str(payload.get("page_id") or payload.get("page") or meta["page_id"])
        grant = issue_meta_token(
            ctx, meta_type="PAGE", page_id=page_id, scopes=scopes, ttl=ttl, client_id=client_id
        )
        result: dict[str, Any] = {
            "access_token": grant.token,
            "token_type": "bearer",
            "type": "PAGE",
            "page_id": page_id,
        }
    elif meta_type == "SYSTEM_USER":
        grant = issue_meta_token(
            ctx,
            meta_type="SYSTEM_USER",
            fb_user_id=meta["system_user_id"],
            scopes=scopes,
            ttl=ttl,
            client_id=client_id,
        )
        result = {
            "access_token": grant.token,
            "token_type": "bearer",
            "type": "SYSTEM_USER",
            "user_id": meta["system_user_id"],
        }
    else:
        person = _find_person(ctx, str(user_ref)) if user_ref else ctx.world.person("p040")
        if person is None:
            return {"error": f"unknown user '{user_ref}'"}
        fb_id = _ensure_fb_user(ctx, person)
        grant = issue_meta_token(
            ctx,
            meta_type="USER",
            fb_user_id=fb_id,
            person_id=person.id,
            scopes=scopes,
            ttl=ttl,
            client_id=client_id,
        )
        result = {
            "access_token": grant.token,
            "token_type": "bearer",
            "type": "USER",
            "user_id": fb_id,
            "person_id": person.id,
        }
    if expires_in is not None:
        result["expires_in"] = expires_in
    return result


async def configure_webhook(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Verify (hub.challenge) and store a webhook subscription for an object type."""
    obj = str(payload.get("object") or "")
    callback_url = str(payload.get("callback_url") or payload.get("url") or "")
    verify_token = str(payload.get("verify_token") or "")
    if not obj or not callback_url or not verify_token:
        return {"success": False, "error": "object, callback_url and verify_token are required"}
    raw_fields = payload.get("fields") or ["messages"]
    fields = (
        raw_fields
        if isinstance(raw_fields, list)
        else [f.strip() for f in str(raw_fields).split(",") if f.strip()]
    )
    ok, info = await configure_webhook_verified(
        ctx,
        obj=obj,
        callback_url=callback_url,
        verify_token=verify_token,
        app_secret=str(payload["app_secret"]) if payload.get("app_secret") else None,
        fields=[str(f) for f in fields],
    )
    return {"success": ok, **info}


def _make_media(ctx: ProviderContext, media: dict[str, Any], kind: str, wa_id: str) -> dict[str, Any]:
    """Create a media record from an action payload (doc_id, base64, or synthesized)."""
    content: bytes | None = None
    mime = str(media.get("mime_type") or "")
    filename = str(media.get("filename") or "")
    if media.get("content_base64"):
        content = base64.b64decode(str(media["content_base64"]))
    elif media.get("doc_id"):
        from emulators.world.documents import render

        doc = ctx.world.document(str(media["doc_id"]))
        content, mime = render(doc.kind, doc.title, list(doc.paragraphs), created=doc.created)
        filename = filename or doc.name
        if doc.kind in ("png",):
            kind = "image"
    if content is None:
        if kind == "image":
            from emulators.world.documents import make_png

            content, mime = make_png(320, 240, seed=int(wa_id[-4:])), "image/png"
            filename = filename or "photo.png"
        elif kind == "audio":
            from emulators.world.media import make_wav

            content, mime = make_wav(2.0, seed=int(wa_id[-4:])), "audio/wav"
            filename = filename or "voice-message.wav"
        elif kind == "video":
            from emulators.world.media import make_mp4

            content, mime = make_mp4(2.0, seed=int(wa_id[-4:])), "video/mp4"
            filename = filename or "video.mp4"
        else:
            from emulators.world.documents import render

            content, mime = render("pdf", "Document", ["Document shared over WhatsApp."])
            filename = filename or "document.pdf"
    blob_key = ctx.store.blobs.put(content)
    media_id = numeric_id("media", blob_key, ctx.ids.sequence("media"), digits=16)
    record = {
        "id": media_id,
        "messaging_product": "whatsapp",
        "mime_type": mime or "application/octet-stream",
        "sha256": blob_key,
        "file_size": len(content),
        "_blob_key": blob_key,
        "_filename": filename or "download",
        "_phone_number_id": ctx.store.meta["phone_number_id"],
    }
    ctx.store.collection("media").put(record)
    return record


async def inbound_whatsapp(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Simulate a customer WhatsApp message (opens the 24h window, fires the webhook)."""
    ref = str(
        payload.get("from") or payload.get("wa_id") or payload.get("phone") or payload.get("person") or ""
    )
    if not ref:
        return {"success": False, "error": "'from' (E.164 number or world person id) is required"}
    person = _find_person(ctx, ref)
    profile_name: str | None = None
    person_id: str | None = None
    if person is not None:
        profile_name = _display_name(person)
        person_id = person.id
        phone = getattr(person, "phone_mobile", None) or getattr(person, "phone", "")
        wa_id = digits_only(str(phone))
    else:
        wa_id = digits_only(ref)
    if not wa_id:
        return {"success": False, "error": f"could not resolve '{ref}' to a phone number"}
    now = ctx.clock.now()
    now_ts = int(now.timestamp())
    contacts = ctx.store.collection("wa_contacts")
    contact = contacts.get(wa_id)
    if contact is None:
        contact = {
            "id": wa_id,
            "wa_id": wa_id,
            "profile_name": profile_name or str(payload.get("profile_name") or wa_id),
            "_person_id": person_id,
            "_phone": "+" + wa_id,
            "_last_inbound_ts": None,
            "_conversation_origin": "service",
        }
        contacts.put(contact)
    elif profile_name:
        contact["profile_name"] = profile_name
    contact["_last_inbound_ts"] = now_ts
    contact["_conversation_origin"] = "service"
    msg_type = str(payload.get("type") or "text")
    message_id = new_wamid(ctx)
    record: dict[str, Any] = {
        "id": message_id,
        "direction": "inbound",
        "from": wa_id,
        "recipient_id": wa_id,
        "timestamp": str(now_ts),
        "type": msg_type,
        "status": "received",
        "_person_id": person_id or contact.get("_person_id"),
        "_phone_number_id": ctx.store.meta["phone_number_id"],
    }
    media_payload = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    if msg_type == "text":
        record["text"] = {"body": str(payload.get("text") or payload.get("body") or "")}
    elif msg_type in ("image", "document", "audio", "video", "sticker"):
        media_record = _make_media(ctx, media_payload, msg_type, wa_id)
        item: dict[str, Any] = {
            "id": media_record["id"],
            "mime_type": media_record["mime_type"],
            "sha256": media_record["sha256"],
        }
        caption = payload.get("caption") or media_payload.get("caption")
        if caption and msg_type in ("image", "document", "video"):
            item["caption"] = str(caption)
        if msg_type == "document":
            item["filename"] = media_record["_filename"]
        if msg_type == "audio":
            item["voice"] = bool(media_payload.get("voice", True))
        if msg_type == "sticker":
            item["animated"] = False
        record[msg_type] = item
    elif msg_type == "location":
        location = payload.get("location") or {}
        record["location"] = {
            "latitude": location.get("latitude", 43.6465),
            "longitude": location.get("longitude", -79.38),
            "name": location.get("name", ""),
            "address": location.get("address", ""),
        }
    elif msg_type == "reaction":
        reaction = payload.get("reaction") or {}
        record["reaction"] = {
            "message_id": reaction.get("message_id") or payload.get("context_message_id") or "",
            "emoji": reaction.get("emoji") or payload.get("emoji") or "",
        }
    elif msg_type == "interactive":
        record["interactive"] = payload.get("interactive") or {
            "type": "button_reply",
            "button_reply": {"id": "confirm", "title": str(payload.get("text") or "Confirm")},
        }
    elif payload.get(msg_type) is not None:
        record[msg_type] = payload[msg_type]
    else:
        record[msg_type] = {}
    context_message_id = payload.get("context_message_id") or (payload.get("context") or {}).get("message_id")
    if context_message_id:
        original = ctx.store.collection("wa_messages").get(str(context_message_id))
        record["context"] = {
            "from": (original or {}).get("from", DISPLAY_PHONE_DIGITS),
            "id": str(context_message_id),
        }
    ctx.store.collection("wa_messages").put(record)
    delivered = await deliver_wa_inbound(ctx, record)
    return {
        "success": True,
        "message_id": message_id,
        "id": message_id,
        "wa_id": wa_id,
        "from": wa_id,
        "timestamp": str(now_ts),
        "type": msg_type,
        "webhook_delivered": delivered,
    }


async def deliver_statuses(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Emit one outbound status webhook (sent|delivered|read|failed) for a message."""
    message_id = str(payload.get("message_id") or "")
    if not message_id:
        return {"success": False, "error": "'message_id' is required"}
    statuses = payload.get("statuses") or [str(payload.get("status") or "delivered")]
    record = ctx.store.collection("wa_messages").get(message_id)
    if record is not None:
        delivered_any = False
        for status in statuses:
            status = str(status)
            if status not in ("sent", "delivered", "read", "failed"):
                return {"success": False, "error": f"unknown status '{status}'"}
            errors = None
            if status == "failed":
                errors = payload.get("errors") or failed_errors(int(payload.get("error_code", 131026)))
            record["status"] = status
            delivered_any = await deliver_wa_status(ctx, record, status, errors=errors) or delivered_any
        return {
            "success": True,
            "message_id": message_id,
            "status": str(statuses[-1]),
            "webhook_delivered": delivered_any,
            "channel": "whatsapp",
        }
    ms_record = ctx.store.collection("ms_messages").get(message_id)
    if ms_record is not None:
        psid = str(ms_record.get("_psid") or "")
        watermark = int(ctx.clock.now().timestamp() * 1000)
        await deliver_ms_delivery_and_read(ctx, psid, message_id, watermark)
        return {
            "success": True,
            "message_id": message_id,
            "status": str(statuses[-1]),
            "channel": "messenger",
        }
    return {"success": False, "error": f"unknown message_id '{message_id}'"}


def _set_template_status(ctx: ProviderContext, payload: dict[str, Any], status: str) -> dict[str, Any]:
    name = str(payload.get("name") or "")
    if not name:
        return {"success": False, "error": "'name' is required"}
    language = payload.get("language")
    updated = []
    for template in ctx.store.collection("templates"):
        if template["name"] == name and (language is None or template["language"] == language):
            template["status"] = status
            if status == "APPROVED":
                template.pop("rejected_reason", None)
            else:
                template["rejected_reason"] = str(payload.get("rejected_reason") or "INVALID_FORMAT")
            updated.append(
                {"id": template["id"], "name": name, "language": template["language"], "status": status}
            )
    if not updated:
        return {"success": False, "error": f"no template named '{name}'"}
    first = updated[0]
    return {"success": True, "count": len(updated), "templates": updated, **first}


def approve_template(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Move a template to APPROVED (Meta's review step, compressed)."""
    return _set_template_status(ctx, payload, "APPROVED")


def reject_template(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Move a template to REJECTED with a rejected_reason."""
    return _set_template_status(ctx, payload, "REJECTED")


def _ensure_psid_and_conversation(
    ctx: ProviderContext, psid: str, person: Any | None, name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    page_id = ctx.store.meta["page_id"]
    psids = ctx.store.collection("psids")
    record = psids.get(psid)
    if record is None:
        first = getattr(person, "first_name", "Facebook") if person else "Facebook"
        last = getattr(person, "last_name", "User") if person else "User"
        conv_id = "t_" + numeric_id("msconv", page_id, psid, digits=15)
        record = {
            "id": psid,
            "first_name": first,
            "last_name": last,
            "name": name or f"{first} {last}",
            "locale": getattr(person, "locale", "en-US") if person else "en-US",
            "timezone_name": getattr(person, "timezone", "America/Toronto") if person else "America/Toronto",
            "_person_id": person.id if person else None,
            "_page_id": page_id,
            "_last_inbound_ts": None,
            "_conversation_id": conv_id,
        }
        psids.put(record)
    conversations = ctx.store.collection("ms_conversations")
    conv = conversations.get(record["_conversation_id"])
    if conv is None:
        conv = {
            "id": record["_conversation_id"],
            "link": f"/{record['_conversation_id']}/",
            "updated_time": graph_time(ctx.clock.now()),
            "message_count": 0,
            "unread_count": 0,
            "can_reply": True,
            "is_subscribed": True,
            "snippet": "",
            "participants": {
                "data": [
                    {"id": page_id, "name": ctx.world.org.name},
                    {"id": psid, "name": record["name"]},
                ]
            },
            "_psid": psid,
            "_person_id": record.get("_person_id"),
            "_page_id": page_id,
            "_message_ids": [],
            "_updated_ts": int(ctx.clock.now().timestamp()),
        }
        conversations.put(conv)
    return record, conv


async def inbound_messenger(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Simulate a user messaging the Page (text and/or postback; opens the window)."""
    page_id = ctx.store.meta["page_id"]
    psid: str | None = str(payload["psid"]) if payload.get("psid") else None
    person_ref = (
        payload.get("person") or payload.get("person_id") or payload.get("user") or payload.get("from")
    )
    person = _find_person(ctx, str(person_ref)) if person_ref else None
    if person is not None and psid is None:
        psid = psid_for(page_id, person.id)
    if not psid:
        return {"success": False, "error": "'psid' or 'person' (world person id/email) is required"}
    name = _display_name(person) if person else str(payload.get("name") or "Facebook User")
    psid_record, conv = _ensure_psid_and_conversation(ctx, psid, person, name)
    now = ctx.clock.now()
    now_ts = int(now.timestamp())
    psid_record["_last_inbound_ts"] = now_ts
    mid = new_mid(ctx)
    postback_raw = payload.get("postback")
    if postback_raw is not None:
        if isinstance(postback_raw, dict):
            postback = {
                "mid": mid,
                "payload": str(postback_raw.get("payload") or ""),
                "title": str(postback_raw.get("title") or ""),
            }
        else:
            postback = {"mid": mid, "payload": str(postback_raw), "title": str(payload.get("title") or "")}
        delivered = await deliver_ms_inbound(ctx, psid, postback=postback)
        conv["unread_count"] = int(conv.get("unread_count", 0)) + 1
        conv["updated_time"] = graph_time(now)
        conv["_updated_ts"] = now_ts
        return {
            "success": True,
            "message_id": mid,
            "mid": mid,
            "psid": psid,
            "conversation_id": conv["id"],
            "postback": postback,
            "webhook_delivered": delivered,
        }
    text = str(payload.get("text") or payload.get("message") or "")
    if not text and not payload.get("attachments") and not payload.get("quick_reply_payload"):
        return {"success": False, "error": "'text' (or 'postback') is required"}
    page = ctx.store.collection("pages").get(page_id)
    record: dict[str, Any] = {
        "id": mid,
        "created_time": graph_time(now),
        "from": {"id": psid, "name": psid_record["name"]},
        "to": {"data": [{"id": page_id, "name": (page or {}).get("name", "Page")}]},
        "message": text,
        "_conversation_id": conv["id"],
        "_psid": psid,
        "_person_id": psid_record.get("_person_id"),
        "_direction": "inbound",
        "_author_id": None,
        "_ts": now_ts,
    }
    ctx.store.collection("ms_messages").put(record)
    conv["_message_ids"] = [*conv.get("_message_ids", []), mid]
    conv["message_count"] = len(conv["_message_ids"])
    conv["unread_count"] = int(conv.get("unread_count", 0)) + 1
    conv["updated_time"] = graph_time(now)
    conv["_updated_ts"] = now_ts
    conv["snippet"] = text[:100]
    webhook_message: dict[str, Any] = {"mid": mid, "text": text}
    quick_payload = payload.get("quick_reply_payload") or (payload.get("quick_reply") or {}).get("payload")
    if quick_payload:
        webhook_message["quick_reply"] = {"payload": str(quick_payload)}
    if payload.get("attachments"):
        webhook_message["attachments"] = payload["attachments"]
        record["_attachments"] = {"data": payload["attachments"]}
    delivered = await deliver_ms_inbound(ctx, psid, message=webhook_message)
    return {
        "success": True,
        "message_id": mid,
        "mid": mid,
        "psid": psid,
        "conversation_id": conv["id"],
        "webhook_delivered": delivered,
    }
