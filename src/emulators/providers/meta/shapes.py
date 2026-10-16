"""Wire-shape builders: turn store records into Graph API objects.

Each builder returns the *full* object plus a ``_defaults`` list naming the fields
Graph returns when no ``fields=`` is given; :func:`common.apply_fields` then filters.
Internal keys (``_person_id`` etc.) never survive shaping.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.providers.meta.common import offset_hours, picture_url, to_dt


def picture(ctx: ProviderContext, request: Request, object_id: str) -> dict[str, Any]:
    return {
        "data": {
            "url": picture_url(ctx, request, object_id),
            "width": 50,
            "height": 50,
            "is_silhouette": False,
        }
    }


def business_shape(record: dict[str, Any]) -> dict[str, Any]:
    return clean(
        {
            "id": record["id"],
            "name": record["name"],
            "created_time": record.get("created_time"),
            "_defaults": ["id", "name"],
        }
    )


def clean(shape: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is ``None`` (Graph omits unset fields) except explicit nulls."""
    return {k: v for k, v in shape.items() if v is not None or k.startswith("_")}


def waba_shape(record: dict[str, Any]) -> dict[str, Any]:
    return clean(
        {
            "id": record["id"],
            "name": record["name"],
            "currency": record["currency"],
            "timezone_id": record["timezone_id"],
            "message_template_namespace": record["message_template_namespace"],
            "account_review_status": record["account_review_status"],
            "status": record.get("status"),
            "owner_business_info": record.get("owner_business_info"),
            "primary_funding_id": record.get("primary_funding_id"),
            "is_enabled_for_insights": record.get("is_enabled_for_insights"),
            "_defaults": ["id", "name"],
        }
    )


def phone_shape(record: dict[str, Any]) -> dict[str, Any]:
    return clean(
        {
            "id": record["id"],
            "display_phone_number": record["display_phone_number"],
            "verified_name": record["verified_name"],
            "quality_rating": record["quality_rating"],
            "code_verification_status": record["code_verification_status"],
            "messaging_limit_tier": record["messaging_limit_tier"],
            "platform_type": record["platform_type"],
            "throughput": record["throughput"],
            "is_official_business_account": record["is_official_business_account"],
            "name_status": record.get("name_status"),
            "new_name_status": record.get("new_name_status"),
            "status": record.get("status"),
            "account_mode": record.get("account_mode"),
            "is_on_biz_app": record.get("is_on_biz_app"),
            "is_preverified": record.get("is_preverified"),
            "certificate_expiration_time": record.get("certificate_expiration_time"),
            "_defaults": ["id", "display_phone_number", "verified_name", "quality_rating"],
        }
    )


def profile_shape(ctx: ProviderContext, request: Request, phone: dict[str, Any]) -> dict[str, Any]:
    profile = dict(phone.get("_profile") or {})
    shape: dict[str, Any] = {
        "about": profile.get("about"),
        "address": profile.get("address"),
        "description": profile.get("description"),
        "email": profile.get("email"),
        "profile_picture_url": profile.get("profile_picture_url") or picture_url(ctx, request, phone["id"]),
        "websites": profile.get("websites") or [],
        "vertical": profile.get("vertical"),
        "industry": profile.get("vertical"),
        "messaging_product": "whatsapp",
    }
    return clean(shape)


def template_shape(record: dict[str, Any]) -> dict[str, Any]:
    return clean(
        {
            "id": record["id"],
            "name": record["name"],
            "language": record["language"],
            "category": record["category"],
            "status": record["status"],
            "components": record["components"],
            "quality_score": record.get("quality_score"),
            "rejected_reason": record.get("rejected_reason"),
            "previous_category": record.get("previous_category"),
            "_defaults": [
                "id",
                "name",
                "language",
                "category",
                "status",
                "components",
                "quality_score",
                "rejected_reason",
            ],
        }
    )


def media_shape(
    ctx: ProviderContext, request: Request, record: dict[str, Any], version: str
) -> dict[str, Any]:
    from emulators.core.ids import stable_hash
    from emulators.core.provider import url_for_host

    ext = int(ctx.clock.now().timestamp()) + 300
    url = url_for_host(
        request,
        ctx,
        "lookaside.fbsbx.com",
        f"/whatsapp_business/attachments/?mid={record['id']}&ext={ext}"
        f"&hash={stable_hash('media', record['id'], length=32)}",
    )
    return {
        "messaging_product": "whatsapp",
        "url": url,
        "mime_type": record["mime_type"],
        "sha256": record["sha256"],
        "file_size": record["file_size"],
        "id": record["id"],
        "_defaults": ["messaging_product", "url", "mime_type", "sha256", "file_size", "id"],
    }


def fb_user_shape(
    ctx: ProviderContext, request: Request, record: dict[str, Any], *, with_email: bool = True
) -> dict[str, Any]:
    shape: dict[str, Any] = {
        "id": record["id"],
        "name": record["name"],
        "first_name": record["first_name"],
        "last_name": record["last_name"],
        "link": record.get("link"),
        "locale": record.get("locale"),
        "picture": picture(ctx, request, record["id"]),
        "email": record.get("email") if with_email else None,
        "_defaults": ["id", "name"],
    }
    return clean(shape)


def system_user_shape(ctx: ProviderContext) -> dict[str, Any]:
    return {
        "id": ctx.store.meta["system_user_id"],
        "name": "Halcyon System User",
        "role": "ADMIN",
        "_defaults": ["id", "name"],
    }


def app_shape(ctx: ProviderContext) -> dict[str, Any]:
    return {
        "id": ctx.store.meta["app_id"],
        "name": ctx.store.meta["app_name"],
        "namespace": "halcyon_integrations",
        "category": "Business",
        "link": f"https://www.facebook.com/games/{ctx.store.meta['app_id']}/",
        "_defaults": ["id", "name"],
    }


def page_shape(
    ctx: ProviderContext,
    request: Request,
    record: dict[str, Any],
    *,
    include_token: bool = False,
    tasks: list[str] | None = None,
) -> dict[str, Any]:
    shape: dict[str, Any] = {
        "id": record["id"],
        "name": record["name"],
        "username": record["username"],
        "category": record["category"],
        "category_list": record["category_list"],
        "about": record["about"],
        "description": record["description"],
        "link": record["link"],
        "website": record["website"],
        "phone": record["phone"],
        "emails": record["emails"],
        "fan_count": record["fan_count"],
        "followers_count": record["followers_count"],
        "is_published": record["is_published"],
        "is_verified": record["is_verified"],
        "verification_status": record["verification_status"],
        "can_post": record["can_post"],
        "location": record["location"],
        "picture": picture(ctx, request, record["id"]),
        "cover": {"source": picture_url(ctx, request, record["id"] + "-cover"), "id": record["id"]},
        "_defaults": ["id", "name", "category", "fan_count", "link", "username"],
    }
    if include_token:
        shape["access_token"] = record["access_token"]
        shape["_defaults"] = [*shape["_defaults"], "access_token"]
    if tasks is not None:
        shape["tasks"] = tasks
        shape["_defaults"] = [*shape["_defaults"], "tasks"]
    return shape


def psid_shape(ctx: ProviderContext, request: Request, record: dict[str, Any]) -> dict[str, Any]:
    """Messenger User Profile API shape."""
    anchor = ctx.clock.now()
    locale = str(record.get("locale") or "en-US").replace("-", "_")
    return clean(
        {
            "id": record["id"],
            "first_name": record["first_name"],
            "last_name": record["last_name"],
            "name": record["name"],
            "profile_pic": picture_url(ctx, request, record["id"]),
            "locale": locale,
            "timezone": offset_hours(str(record.get("timezone_name") or "America/Toronto"), anchor),
            "_defaults": ["id", "first_name", "last_name", "profile_pic"],
        }
    )


def ms_message_shape(_ctx: ProviderContext | None, record: dict[str, Any]) -> dict[str, Any]:
    return _ms(record)


def ms_message_stub(record: dict[str, Any]) -> dict[str, Any]:
    """The ``/{conversation}/messages`` edge item (id + created_time by default)."""
    shape = _ms(record)
    shape["_defaults"] = ["id", "created_time"]
    return shape


def _ms(record: dict[str, Any]) -> dict[str, Any]:
    shape: dict[str, Any] = {
        "id": record["id"],
        "created_time": record["created_time"],
        "from": record["from"],
        "to": record["to"],
        "message": record.get("message", ""),
        "tags": {"data": [{"name": "source:messenger"}]},
        "_defaults": ["id", "created_time", "from", "to", "message"],
    }
    attachments = record.get("_attachments") or record.get("attachments")
    if attachments:
        shape["attachments"] = attachments
    if record.get("_sticker"):
        shape["sticker"] = record["_sticker"]
    return shape


def ms_conversation_shape(ctx: ProviderContext, record: dict[str, Any]) -> dict[str, Any]:
    messages = []
    coll = ctx.store.collection("ms_messages")
    for mid in record.get("_message_ids") or []:
        item = coll.get(mid)
        if item is not None:
            messages.append(_ms(item))
    messages.sort(key=lambda m: m["created_time"])
    return {
        "id": record["id"],
        "link": record["link"],
        "updated_time": record["updated_time"],
        "message_count": record["message_count"],
        "unread_count": record["unread_count"],
        "can_reply": record["can_reply"],
        "is_subscribed": record["is_subscribed"],
        "snippet": record.get("snippet", ""),
        "participants": record["participants"],
        "senders": record["participants"],
        "messages": {"data": messages, "_summary": {"total_count": len(messages)}},
        "former_participants": {"data": []},
        "blocked": {"data": []},
        "_defaults": [
            "id",
            "link",
            "updated_time",
            "message_count",
            "unread_count",
            "participants",
            "snippet",
            "can_reply",
        ],
    }


def wa_message_webhook_value(ctx: ProviderContext, record: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the webhook ``messages[]`` item for a stored inbound message."""
    item: dict[str, Any] = {
        "from": record["from"],
        "id": record["id"],
        "timestamp": record["timestamp"],
        "type": record["type"],
    }
    payload_key = record["type"]
    if payload_key in record and isinstance(record[payload_key], dict):
        item[payload_key] = record[payload_key]
    for key in ("context", "identity", "referral", "button", "interactive", "order", "system", "errors"):
        if key in record and record[key] is not None and key != payload_key:
            item[key] = record[key]
    return item


def iso_z_from_graph(value: str) -> str:
    return isoformat_z(to_dt(value.replace("+0000", "+00:00")))
