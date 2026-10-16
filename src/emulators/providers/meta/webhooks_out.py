"""Outgoing Meta webhook payloads (WhatsApp ``messages`` field, Messenger ``page``).

Deliveries go through ``ctx.webhooks.deliver`` signed with
``X-Hub-Signature-256: sha256=<hmac_sha256(app_secret, body)>`` plus the legacy
``X-Hub-Signature: sha1=<hmac_sha1(app_secret, body)>``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.meta.shapes import wa_message_webhook_value
from emulators.providers.meta.validation import http_url

PAGE_FIELDS = ["messages", "messaging_postbacks", "message_deliveries", "message_reads"]


def webhook_config(ctx: ProviderContext, obj: str) -> dict[str, Any] | None:
    return ctx.store.collection("webhook_configs").get(obj)


def sign_headers(secret: str, body: bytes) -> dict[str, str]:
    return {
        "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
        "X-Hub-Signature": "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest(),
    }


async def configure_webhook_verified(
    ctx: ProviderContext,
    *,
    obj: str,
    callback_url: str,
    verify_token: str,
    app_secret: str | None = None,
    fields: list[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Run Meta's verification handshake and, on success, store the subscription.

    Issues ``GET callback_url?hub.mode=subscribe&hub.verify_token=...&hub.challenge=<int>``
    through ``ctx.webhooks.request`` and requires the challenge echoed verbatim.
    """
    import httpx

    if obj not in ("page", "whatsapp_business_account"):
        return False, {"error": "unsupported_object"}
    http_url(ctx, callback_url, "callback_url")
    if fields is not None and any(not isinstance(f, str) or not f for f in fields):
        return False, {"error": "invalid_fields"}
    challenge = str(ctx.ids.randint(10**9, 10**10))
    info: dict[str, Any] = {"challenge": int(challenge), "callback_url": callback_url, "object": obj}
    try:
        response = await ctx.webhooks.request(
            "GET",
            callback_url,
            params={
                **dict(parse_qsl(urlsplit(callback_url).query)),
                "hub.mode": "subscribe",
                "hub.verify_token": verify_token,
                "hub.challenge": challenge,
            },
        )
    except httpx.HTTPError as exc:
        return False, {**info, "error": f"{type(exc).__name__}: {exc}"}
    info["response_status"] = response.status_code
    if response.status_code != 200 or response.text != challenge:
        info["response_body"] = response.text[:500]
        info["error"] = "challenge_mismatch"
        return False, info
    ctx.store.collection("webhook_configs").put_as(
        obj,
        {
            "id": obj,
            "object": obj,
            "callback_url": callback_url,
            "verify_token": verify_token,
            "app_secret": app_secret or ctx.store.meta.get("app_secret"),
            "fields": fields if fields is not None else ["messages"],
        },
    )
    return True, info


async def deliver_payload(ctx: ProviderContext, obj: str, payload: dict[str, Any], event_type: str) -> bool:
    config = webhook_config(ctx, obj)
    if not config or not config.get("callback_url"):
        return False
    field = {
        "whatsapp.messages": "messages",
        "whatsapp.statuses": "messages",
        "messenger.messages": "messages",
        "messenger.postbacks": "messaging_postbacks",
        "messenger.deliveries": "message_deliveries",
        "messenger.reads": "message_reads",
    }.get(event_type)
    if field not in config.get("fields", []):
        return False
    if obj == "whatsapp_business_account":
        apps = ctx.store.collection("subscribed_apps").get(ctx.store.meta["waba_id"]) or {}
        if not any(a.get("id") == ctx.store.meta["app_id"] for a in apps.get("apps", [])):
            return False
    else:
        page = ctx.store.collection("pages").get(ctx.store.meta["page_id"]) or {}
        if field not in page.get("_subscribed_fields", []):
            return False
    body = json.dumps(payload).encode()
    secret = str(config.get("app_secret") or ctx.store.meta.get("app_secret") or "")
    await ctx.webhooks.deliver(
        config["callback_url"],
        body,
        headers=sign_headers(secret, body),
        event_type=event_type,
        metadata={"object": obj},
    )
    return True


def _wa_metadata(ctx: ProviderContext) -> dict[str, Any]:
    return {
        "display_phone_number": ctx.store.meta["display_phone_number"],
        "phone_number_id": ctx.store.meta["phone_number_id"],
    }


async def deliver_wa_inbound(ctx: ProviderContext, record: dict[str, Any]) -> bool:
    contact = ctx.store.collection("wa_contacts").get(record["from"]) or {}
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": ctx.store.meta["waba_id"],
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": _wa_metadata(ctx),
                            "contacts": [
                                {
                                    "profile": {"name": contact.get("profile_name") or record["from"]},
                                    "wa_id": record["from"],
                                }
                            ],
                            "messages": [wa_message_webhook_value(ctx, record)],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    return await deliver_payload(ctx, "whatsapp_business_account", payload, "whatsapp.messages")


def wa_conversation_obj(ctx: ProviderContext, record: dict[str, Any], status: str) -> dict[str, Any]:
    contact = ctx.store.collection("wa_contacts").get(record.get("recipient_id") or "") or {}
    origin = record.get("_origin") or contact.get("_conversation_origin") or "service"
    last_inbound = contact.get("_last_inbound_ts")
    now_ts = int(ctx.clock.now().timestamp())
    expiration = str((int(last_inbound) + 86400) if last_inbound else now_ts + 86400)
    conversation_id = record.get("_conversation_ref") or stable_hash(
        "wa-conversation", record.get("recipient_id"), last_inbound or "none", length=32
    )
    return {
        "id": conversation_id,
        "expiration_timestamp": expiration,
        "origin": {"type": origin},
    }


async def deliver_wa_status(
    ctx: ProviderContext,
    record: dict[str, Any],
    status: str,
    *,
    errors: list[dict[str, Any]] | None = None,
) -> bool:
    origin = record.get("_origin") or "service"
    category = origin if origin in ("marketing", "utility", "authentication", "service") else "service"
    status_obj: dict[str, Any] = {
        "id": record["id"],
        "status": status,
        "timestamp": str(int(ctx.clock.now().timestamp())),
        "recipient_id": record.get("recipient_id") or record.get("to"),
        "conversation": wa_conversation_obj(ctx, record, status),
        "pricing": {
            "billable": bool(record.get("_billable")),
            "pricing_model": "PMP",
            "category": category,
        },
    }
    if errors:
        status_obj["errors"] = errors
    if status == "failed":
        status_obj.pop("pricing", None)
        status_obj.pop("conversation", None)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": ctx.store.meta["waba_id"],
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": _wa_metadata(ctx),
                            "statuses": [status_obj],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    return await deliver_payload(ctx, "whatsapp_business_account", payload, "whatsapp.statuses")


async def deliver_wa_statuses(ctx: ProviderContext, record: dict[str, Any]) -> None:
    """The normal sent -> delivered -> read progression after a successful send."""
    for status in ("sent", "delivered", "read"):
        record["status"] = status
        await deliver_wa_status(ctx, record, status)


def failed_errors(code: int = 131026) -> list[dict[str, Any]]:
    titles = {
        131026: ("Message undeliverable", "Receiver is not a valid WhatsApp user."),
        131047: ("Re-engagement message", "More than 24 hours have passed since the recipient last replied."),
        131051: ("Unsupported message type", "The message type is not supported."),
    }
    title, details = titles.get(code, ("Unknown error", "The message could not be delivered."))
    return [
        {
            "code": code,
            "title": title,
            "message": f"(#{code}) {title}",
            "error_data": {"details": details},
            "href": "https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes",
        }
    ]


# -- Messenger ------------------------------------------------------------------------------------
async def deliver_ms_messaging(
    ctx: ProviderContext, messaging: list[dict[str, Any]], event_type: str
) -> bool:
    page_id = ctx.store.meta["page_id"]
    payload = {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": int(ctx.clock.now().timestamp() * 1000),
                "messaging": messaging,
            }
        ],
    }
    return await deliver_payload(ctx, "page", payload, event_type)


async def deliver_ms_inbound(
    ctx: ProviderContext,
    psid: str,
    message: dict[str, Any] | None = None,
    postback: dict[str, Any] | None = None,
) -> bool:
    page_id = ctx.store.meta["page_id"]
    ts_ms = int(ctx.clock.now().timestamp() * 1000)
    item: dict[str, Any] = {
        "sender": {"id": psid},
        "recipient": {"id": page_id},
        "timestamp": ts_ms,
    }
    if postback is not None:
        item["postback"] = postback
        return await deliver_ms_messaging(ctx, [item], "messenger.postbacks")
    item["message"] = message or {}
    return await deliver_ms_messaging(ctx, [item], "messenger.messages")


async def deliver_ms_delivery_and_read(
    ctx: ProviderContext, psid: str, mid: str, watermark_ms: int, *, status: str | None = None
) -> None:
    page_id = ctx.store.meta["page_id"]
    base = {"sender": {"id": psid}, "recipient": {"id": page_id}, "timestamp": watermark_ms}
    if status in (None, "delivered"):
        await deliver_ms_messaging(
            ctx, [{**base, "delivery": {"mids": [mid], "watermark": watermark_ms}}], "messenger.deliveries"
        )
    if status in (None, "read"):
        await deliver_ms_messaging(ctx, [{**base, "read": {"watermark": watermark_ms}}], "messenger.reads")
