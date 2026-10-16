"""Project the Halcyon world into Meta's data model.

WhatsApp threads come from ``world.conversations`` (channel ``whatsapp``, business
number +1 416-555-0177) and Messenger threads from channel ``messenger`` (leads and
client contacts talking to the Halcyon Page). All ids are stable hashes of world ids.

The world's last inbound messages all pre-date the 24-hour messaging window, so to
leave at least one open window per channel (as a real account would have) the seed
appends one recent inbound follow-up — from a real world contact, an hour before the
world anchor — to the first WhatsApp and first Messenger thread.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from emulators.core.ids import stable_guid
from emulators.core.provider import ProviderContext
from emulators.providers.meta.common import (
    APP_ID,
    APP_NAME,
    APP_SECRET,
    DISPLAY_PHONE,
    DISPLAY_PHONE_DIGITS,
    PAGE_ACCESS_TOKEN,
    SYSTEM_USER_TOKEN,
    USER_ACCESS_TOKEN,
    digits_only,
    graph_time,
    issue_meta_token,
    mid_for,
    numeric_id,
    unix_s,
    wamid,
)

PAGE_MANAGERS = {"p040": "ADMIN", "p042": "EDITOR"}  # Natalie Cho, Zoe Anderson
TEMPLATE_NAMESPACE = stable_guid("meta", "template-namespace").replace("-", "_")


def fb_user_id(person_id: str) -> str:
    return numeric_id("fbuser", person_id, digits=15)


def psid_for(page_id: str, person_id: str) -> str:
    return numeric_id("psid", page_id, person_id, digits=16)


def seed(ctx: ProviderContext) -> None:
    world = ctx.world
    store = ctx.store
    business_id = world.org.meta_business_id
    waba_id = numeric_id("waba", business_id, digits=15)
    phone_id = numeric_id("phone", DISPLAY_PHONE_DIGITS, digits=15)
    system_user_id = numeric_id("system_user", business_id, digits=15)
    messenger_convs = [c for c in world.conversations if c.channel == "messenger"]
    page_id = messenger_convs[0].business_number if messenger_convs else "104857600123456"
    store.meta.update(
        {
            "app_id": APP_ID,
            "app_secret": APP_SECRET,
            "app_name": APP_NAME,
            "business_id": business_id,
            "waba_id": waba_id,
            "phone_number_id": phone_id,
            "page_id": page_id,
            "system_user_id": system_user_id,
            "display_phone_number": DISPLAY_PHONE_DIGITS,
        }
    )
    ctx.tokens.register_client(APP_ID, APP_SECRET, name=APP_NAME)

    _seed_business(ctx, business_id, waba_id, phone_id)
    _seed_fb_users(ctx)
    _seed_tokens(ctx, page_id, system_user_id)
    _seed_page(ctx, page_id)
    _seed_templates(ctx, waba_id)
    _seed_whatsapp(ctx, phone_id)
    _seed_messenger(ctx, page_id)
    store.collection("subscribed_apps").put({"id": waba_id, "apps": [{"id": APP_ID, "name": APP_NAME}]})


def _seed_business(ctx: ProviderContext, business_id: str, waba_id: str, phone_id: str) -> None:
    world = ctx.world
    ctx.store.collection("businesses").put(
        {
            "id": business_id,
            "name": world.org.name,
            "created_time": graph_time(world.anchor - timedelta(days=800)),
            "_waba_id": waba_id,
            "_roles": dict(PAGE_MANAGERS),
        }
    )
    ctx.store.collection("wabas").put(
        {
            "id": waba_id,
            "name": world.org.name,
            "currency": world.org.currency,
            "timezone_id": "35",
            "message_template_namespace": TEMPLATE_NAMESPACE,
            "account_review_status": "APPROVED",
            "status": "ACTIVE",
            "owner_business_info": {"name": world.org.name, "id": business_id},
            "primary_funding_id": numeric_id("funding", business_id, digits=15),
            "is_enabled_for_insights": True,
            "_business_id": business_id,
        }
    )
    addr = world.org.address
    ctx.store.collection("phone_numbers").put(
        {
            "id": phone_id,
            "display_phone_number": DISPLAY_PHONE,
            "verified_name": world.org.name,
            "quality_rating": "GREEN",
            "code_verification_status": "VERIFIED",
            "messaging_limit_tier": "TIER_1K",
            "platform_type": "CLOUD_API",
            "throughput": {"level": "STANDARD"},
            "is_official_business_account": True,
            "name_status": "APPROVED",
            "new_name_status": "NONE",
            "status": "CONNECTED",
            "account_mode": "LIVE",
            "is_on_biz_app": False,
            "is_preverified": True,
            "certificate_expiration_time": None,
            "_waba_id": waba_id,
            "_profile": {
                "about": "Accounting, tax and advisory for growing businesses.",
                "address": addr.one_line(),
                "description": (
                    f"{world.org.name} is an accounting, tax and advisory firm serving "
                    "owner-managed businesses across Canada and the United States."
                ),
                "email": f"hello@{world.org.domain}",
                "websites": [world.org.website],
                "vertical": "PROF_SERVICES",
            },
        }
    )


def _seed_fb_users(ctx: ProviderContext) -> None:
    for person in ctx.world.active_employees:
        ctx.store.collection("fb_users").put(
            {
                "id": fb_user_id(person.id),
                "name": person.display_name,
                "first_name": person.first_name,
                "last_name": person.last_name,
                "email": person.email,
                "link": f"https://www.facebook.com/{fb_user_id(person.id)}",
                "locale": person.locale.replace("-", "_"),
                "timezone": person.timezone,
                "_person_id": person.id,
            }
        )


def _seed_tokens(ctx: ProviderContext, page_id: str, system_user_id: str) -> None:
    issue_meta_token(
        ctx,
        meta_type="SYSTEM_USER",
        fb_user_id=system_user_id,
        ttl=None,
        token=SYSTEM_USER_TOKEN,
    )
    issue_meta_token(
        ctx,
        meta_type="PAGE",
        page_id=page_id,
        ttl=None,
        token=PAGE_ACCESS_TOKEN,
    )
    natalie = "p040"
    issue_meta_token(
        ctx,
        meta_type="USER",
        fb_user_id=fb_user_id(natalie),
        person_id=natalie,
        ttl=timedelta(days=60),
        token=USER_ACCESS_TOKEN,
    )


def _seed_page(ctx: ProviderContext, page_id: str) -> None:
    world = ctx.world
    addr = world.org.address
    ctx.store.collection("pages").put(
        {
            "id": page_id,
            "name": world.org.name,
            "username": world.org.slug,
            "category": "Accountant",
            "category_list": [{"id": numeric_id("cat", "accountant", digits=15), "name": "Accountant"}],
            "about": "Accounting, tax and advisory for growing businesses.",
            "description": (
                f"{world.org.name} ({world.org.legal_name}) helps owner-managed businesses "
                "with audit, tax compliance, bookkeeping and advisory services."
            ),
            "link": f"https://www.facebook.com/{world.org.slug}",
            "website": world.org.website,
            "phone": world.org.phone,
            "emails": [f"hello@{world.org.domain}"],
            "fan_count": 1284,
            "followers_count": 1412,
            "is_published": True,
            "is_verified": True,
            "verification_status": "blue_verified",
            "can_post": True,
            "location": {
                "street": addr.street,
                "city": addr.city,
                "state": addr.state,
                "zip": addr.postal_code,
                "country": addr.country,
                "latitude": 43.6465,
                "longitude": -79.3800,
            },
            "access_token": PAGE_ACCESS_TOKEN,
            "_roles": dict(PAGE_MANAGERS),
            "_business_id": world.org.meta_business_id,
            "_subscribed_fields": ["messages", "messaging_postbacks", "message_deliveries", "message_reads"],
            "_profile": {
                "greeting": [
                    {
                        "locale": "default",
                        "text": "Hi {{user_first_name}}! Thanks for messaging Halcyon Advisory Group. "
                        "How can we help?",
                    }
                ],
                "get_started": {"payload": "GET_STARTED"},
                "persistent_menu": [
                    {
                        "locale": "default",
                        "composer_input_disabled": False,
                        "call_to_actions": [
                            {"type": "postback", "title": "Book a consultation", "payload": "BOOK"},
                            {
                                "type": "web_url",
                                "title": "Visit our website",
                                "url": world.org.website,
                            },
                        ],
                    }
                ],
                "ice_breakers": [
                    {
                        "question": "Do you work with small businesses?",
                        "payload": "ICE_SMALL_BIZ",
                        "locale": "default",
                    }
                ],
            },
        }
    )


def _template(
    waba_id: str,
    name: str,
    category: str,
    status: str,
    components: list[dict[str, Any]],
    *,
    language: str = "en_US",
    rejected_reason: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": numeric_id("template", waba_id, name, language, digits=16),
        "name": name,
        "language": language,
        "category": category,
        "status": status,
        "components": components,
        "quality_score": {"score": "GREEN" if status != "REJECTED" else "RED", "date": "1760572800"},
        "_waba_id": waba_id,
    }
    if rejected_reason is not None:
        record["rejected_reason"] = rejected_reason
    return record


def _seed_templates(ctx: ProviderContext, waba_id: str) -> None:
    templates = ctx.store.collection("templates")
    templates.put(
        _template(
            waba_id,
            "hello_world",
            "UTILITY",
            "APPROVED",
            [
                {"type": "HEADER", "format": "TEXT", "text": "Hello World"},
                {
                    "type": "BODY",
                    "text": "Welcome and congratulations!! This message demonstrates your ability "
                    "to send a WhatsApp message notification from the Cloud API, hosted by Meta. "
                    "Thank you for taking the time to test with us.",
                },
                {"type": "FOOTER", "text": "WhatsApp Business Platform sample message"},
            ],
        )
    )
    templates.put(
        _template(
            waba_id,
            "appointment_reminder",
            "UTILITY",
            "APPROVED",
            [
                {
                    "type": "BODY",
                    "text": "Hi {{1}}, this is a reminder of your appointment with Halcyon Advisory "
                    "Group on {{2}} at {{3}}. Reply C to confirm or R to reschedule.",
                    "example": {"body_text": [["Paula", "Tuesday, October 20", "10:00 am"]]},
                },
                {"type": "FOOTER", "text": "Halcyon Advisory Group"},
            ],
        )
    )
    templates.put(
        _template(
            waba_id,
            "document_request",
            "UTILITY",
            "APPROVED",
            [
                {
                    "type": "BODY",
                    "text": "Hi {{1}}, we still need {{2}} to continue your engagement. You can reply "
                    "here with a photo, or upload it to the shared folder.",
                    "example": {"body_text": [["Hamid", "the signed engagement letter"]]},
                }
            ],
        )
    )
    templates.put(
        _template(
            waba_id,
            "payment_reminder",
            "UTILITY",
            "APPROVED",
            [
                {
                    "type": "BODY",
                    "text": "Hi {{1}}, invoice {{2}} for {{3}} is now due. Please let us know if "
                    "you have any questions about it.",
                    "example": {"body_text": [["Helen", "INV-1043", "$4,250.00"]]},
                }
            ],
        )
    )
    templates.put(
        _template(
            waba_id,
            "year_end_promo",
            "MARKETING",
            "REJECTED",
            [
                {
                    "type": "BODY",
                    "text": "Year-end offer: get {{1}}% off tax planning when you book before "
                    "January 31. Reply to claim.",
                    "example": {"body_text": [["15"]]},
                }
            ],
            rejected_reason="INVALID_FORMAT",
        )
    )


def _person_phone(ctx: ProviderContext, person_id: str) -> str | None:
    try:
        person = ctx.world.person(person_id)
    except KeyError:
        return None
    return person.phone_mobile or person.phone_business


def _seed_whatsapp(ctx: ProviderContext, phone_id: str) -> None:
    world = ctx.world
    contacts = ctx.store.collection("wa_contacts")
    messages = ctx.store.collection("wa_messages")
    now = world.anchor
    open_window_done = False
    for conv in world.conversations:
        if conv.channel != "whatsapp":
            continue
        person = world.person(conv.person_id)
        phone = _person_phone(ctx, conv.person_id) or ""
        wa_id = digits_only(phone)
        last_inbound: int | None = None
        records: list[dict[str, Any]] = []
        for msg in conv.messages:
            ts = int(msg.sent.timestamp())
            if msg.direction == "inbound":
                last_inbound = ts
            record = {
                "id": wamid("wa", msg.id),
                "direction": msg.direction,
                "from": wa_id if msg.direction == "inbound" else DISPLAY_PHONE_DIGITS,
                "recipient_id": wa_id,
                "timestamp": str(ts),
                "type": "text",
                "text": {"body": msg.text},
                "status": msg.status if msg.direction == "outbound" else "received",
                "_person_id": conv.person_id,
                "_author_id": msg.author_id,
                "_phone_number_id": phone_id,
                "_conversation_id": conv.id,
            }
            if msg.media_doc_id:
                record["_media_doc_id"] = msg.media_doc_id
            records.append(record)
        if not open_window_done:
            recent = now - timedelta(hours=1)
            recent_record = {
                "id": wamid("wa", conv.id, "recent-follow-up"),
                "direction": "inbound",
                "from": wa_id,
                "recipient_id": wa_id,
                "timestamp": unix_s(recent),
                "type": "text",
                "text": {
                    "body": "Hi, following up on the engagement letter — do you need anything "
                    "else from us before the call?"
                },
                "status": "received",
                "_person_id": conv.person_id,
                "_author_id": None,
                "_phone_number_id": phone_id,
                "_conversation_id": conv.id,
            }
            records.append(recent_record)
            last_inbound = int(recent.timestamp())
            open_window_done = True
        for record in records:
            messages.put(record)
        contacts.put(
            {
                "id": wa_id,
                "wa_id": wa_id,
                "profile_name": person.display_name,
                "_person_id": conv.person_id,
                "_phone": phone,
                "_last_inbound_ts": last_inbound,
                "_conversation_origin": "service",
            }
        )


def _seed_messenger(ctx: ProviderContext, page_id: str) -> None:
    world = ctx.world
    psids = ctx.store.collection("psids")
    conversations = ctx.store.collection("ms_conversations")
    messages = ctx.store.collection("ms_messages")
    now = world.anchor
    open_window_done = False
    for conv in world.conversations:
        if conv.channel != "messenger":
            continue
        person = world.person(conv.person_id)
        psid = psid_for(page_id, conv.person_id)
        conv_id = "t_" + numeric_id("msconv", conv.id, digits=15)
        last_inbound: int | None = None
        msg_ids: list[str] = []
        all_msgs = list(conv.messages)
        extra: dict[str, Any] | None = None
        if not open_window_done:
            recent = now - timedelta(hours=1)
            extra = {
                "world_id": conv.id + "-recent",
                "direction": "inbound",
                "sent": recent,
                "text": "Hi! Quick follow-up — is next Tuesday morning still good for the consultation?",
                "author_id": None,
                "status": "received",
            }
            open_window_done = True
        entries: list[dict[str, Any]] = [
            {
                "world_id": m.id,
                "direction": m.direction,
                "sent": m.sent,
                "text": m.text,
                "author_id": m.author_id,
                "status": m.status,
            }
            for m in all_msgs
        ]
        if extra is not None:
            entries.append(extra)
        entries.sort(key=lambda e: e["sent"])
        unread = 0
        for entry in entries:
            ts = entry["sent"]
            if entry["direction"] == "inbound":
                last_inbound = int(ts.timestamp())
                unread += 1
                frm = {"id": psid, "name": person.display_name}
                to = [{"id": page_id, "name": world.org.name}]
            else:
                unread = 0
                frm = {"id": page_id, "name": world.org.name}
                to = [{"id": psid, "name": person.display_name}]
            record = {
                "id": mid_for("ms", entry["world_id"]),
                "created_time": graph_time(ts),
                "from": frm,
                "to": {"data": to},
                "message": entry["text"],
                "_conversation_id": conv_id,
                "_psid": psid,
                "_person_id": conv.person_id,
                "_direction": entry["direction"],
                "_author_id": entry["author_id"],
                "_ts": int(ts.timestamp()),
            }
            messages.put(record)
            msg_ids.append(record["id"])
        updated = entries[-1]["sent"] if entries else now
        psids.put(
            {
                "id": psid,
                "first_name": person.first_name,
                "last_name": person.last_name,
                "name": person.display_name,
                "locale": person.locale,
                "timezone_name": person.timezone,
                "_person_id": conv.person_id,
                "_page_id": page_id,
                "_last_inbound_ts": last_inbound,
                "_conversation_id": conv_id,
            }
        )
        conversations.put(
            {
                "id": conv_id,
                "link": f"/{conv_id}/",
                "updated_time": graph_time(updated),
                "message_count": len(msg_ids),
                "unread_count": unread,
                "can_reply": True,
                "is_subscribed": True,
                "snippet": (entries[-1]["text"][:100] if entries else ""),
                "participants": {
                    "data": [
                        {"id": page_id, "name": world.org.name},
                        {"id": psid, "name": person.display_name, "email": person.email},
                    ]
                },
                "_psid": psid,
                "_person_id": conv.person_id,
                "_page_id": page_id,
                "_message_ids": msg_ids,
                "_updated_ts": int(updated.timestamp()),
                "_world_conversation_id": conv.id,
            }
        )
