"""Seed Twilio state from the shared Halcyon world (deterministic, no rendering)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from emulators.core.clock import isoformat_z, utc
from emulators.providers.twilio.common import credentials_for, iso8601, stable_sid
from emulators.providers.twilio.shapes import (
    build_account,
    build_call,
    build_media,
    build_message,
    build_messaging_service,
    build_number,
    build_recording,
    build_verify_service,
)

if TYPE_CHECKING:
    from emulators.core.provider import ProviderContext

# The firm's Twilio numbers (also the business_number values in world conversations).
NUMBER_TORONTO = "+14165550188"
NUMBER_NEW_YORK = "+12125550188"
NUMBER_WHATSAPP = "+14165550177"

MESSAGING_SERVICE_NAME = "Halcyon Client Notifications"
VERIFY_SERVICE_NAME = "Halcyon Client Portal"


def _account_created(world_anchor: datetime) -> datetime:
    return utc(world_anchor) - timedelta(days=400)


def seed(ctx: ProviderContext) -> None:
    world = ctx.world
    account_sid = world.org.twilio_account_sid
    creds = credentials_for(account_sid)
    ctx.store.meta["credentials"] = creds
    ctx.store.meta["balance"] = "152.34"
    ctx.store.meta["chat_service_sid"] = stable_sid("IS", "chat-service", account_sid)
    ctx.store.meta["participant_role_sid"] = stable_sid("RL", "participant-role", account_sid)
    created = _account_created(world.anchor)

    # Accounts + API key.
    ctx.store.collection("accounts").put(
        build_account(
            sid_=account_sid,
            friendly_name=world.org.name,
            auth_token=creds["auth_token"],
            created=created,
        )
    )
    from emulators.providers.twilio.common import rfc2822

    ctx.store.collection("api_keys").put(
        {
            "id": creds["api_key_sid"],
            "sid": creds["api_key_sid"],
            "friendly_name": "Emulator API Key",
            "secret": creds["api_key_secret"],
            "date_created": rfc2822(created),
            "date_updated": rfc2822(created),
            "uri": f"/2010-04-01/Accounts/{account_sid}/Keys/{creds['api_key_sid']}.json",
        }
    )

    # Phone numbers.
    numbers = ctx.store.collection("numbers")
    specs = [
        (NUMBER_TORONTO, "Halcyon Client Line"),
        (NUMBER_NEW_YORK, "Halcyon New York Line"),
        (NUMBER_WHATSAPP, "Halcyon WhatsApp Sender"),
    ]
    number_sids: dict[str, str] = {}
    for phone, friendly in specs:
        sid_ = stable_sid("PN", "number", phone)
        number_sids[phone] = sid_
        numbers.put(
            build_number(
                sid_=sid_,
                account_sid=account_sid,
                phone_number=phone,
                friendly_name=friendly,
                created=created,
            )
        )

    # Messaging service containing the SMS numbers.
    mg_sid = stable_sid("MG", "messaging-service", MESSAGING_SERVICE_NAME)
    ctx.store.meta["messaging_service_sid"] = mg_sid
    ctx.store.collection("messaging_services").put(
        build_messaging_service(
            sid_=mg_sid,
            account_sid=account_sid,
            friendly_name=MESSAGING_SERVICE_NAME,
            created=created,
            usecase="notifications",
        )
    )
    links = ctx.store.collection("mg_numbers")
    for phone in (NUMBER_TORONTO, NUMBER_NEW_YORK):
        link_id = f"{mg_sid}:{number_sids[phone]}"
        links.put_as(
            link_id,
            {
                "id": link_id,
                "sid": number_sids[phone],
                "service_sid": mg_sid,
                "account_sid": account_sid,
                "date_created": iso8601(created),
                "date_updated": iso8601(created),
            },
        )

    # Verify service.
    va_sid = stable_sid("VA", "verify-service", VERIFY_SERVICE_NAME)
    ctx.store.meta["verify_service_sid"] = va_sid
    ctx.store.collection("verify_services").put(
        build_verify_service(
            sid_=va_sid,
            account_sid=account_sid,
            friendly_name=VERIFY_SERVICE_NAME,
            created=created,
            custom_code_enabled=True,
        )
    )

    # Message history from the world's SMS and WhatsApp conversations.
    messages = ctx.store.collection("messages")
    media_store = ctx.store.collection("media")
    for conv in world.conversations:
        if conv.channel not in ("sms", "whatsapp"):
            continue
        try:
            contact = world.person(conv.person_id)
        except KeyError:
            continue
        contact_phone = contact.phone_mobile
        if not contact_phone:
            continue
        prefix = "whatsapp:" if conv.channel == "whatsapp" else ""
        business = f"{prefix}{conv.business_number}"
        contact_addr = f"{prefix}{contact_phone}"
        for msg in conv.messages:
            outbound = msg.direction == "outbound"
            sid_ = stable_sid("MM" if msg.media_doc_id else "SM", "message", msg.id)
            status = "delivered" if outbound else "received"
            if outbound and msg.status in ("sent", "read", "delivered"):
                status = "delivered" if msg.status != "sent" else "sent"
            elif outbound and msg.status == "failed":
                status = "failed"
            record = build_message(
                sid_=sid_,
                account_sid=account_sid,
                to=contact_addr if outbound else business,
                from_=business if outbound else contact_addr,
                body=msg.text,
                direction="outbound-api" if outbound else "inbound",
                status=status,
                created=msg.sent,
                sent=msg.sent,
                num_media=1 if msg.media_doc_id else 0,
                price="-0.00790" if outbound else "-0.00750",
            )
            messages.put(record)
            if msg.media_doc_id:
                doc = world.document(msg.media_doc_id)
                ctype = {"png": "image/png", "pdf": "application/pdf"}.get(
                    doc.kind, "application/octet-stream"
                )
                media_store.put(
                    build_media(
                        sid_=stable_sid("ME", "media", msg.id),
                        account_sid=account_sid,
                        parent_sid=sid_,
                        content_type=ctype,
                        created=msg.sent,
                        document_id=doc.id,
                    )
                )

    # Appointment-style reminders sent through the messaging service.
    sms_convs = [c for c in world.conversations if c.channel == "sms"][:4]
    anchor = utc(world.anchor)
    for i, conv in enumerate(sms_convs):
        contact = world.person(conv.person_id)
        if not contact.phone_mobile:
            continue
        sent = anchor - timedelta(days=2, hours=i * 3)
        sid_ = stable_sid("SM", "reminder", conv.id)
        messages.put(
            build_message(
                sid_=sid_,
                account_sid=account_sid,
                to=contact.phone_mobile,
                from_=conv.business_number,
                body=(
                    f"Hi {contact.first_name}, this is Halcyon Advisory Group reminding you of your upcoming "
                    f"appointment. Reply C to confirm or call us to reschedule."
                ),
                direction="outbound-api",
                status="delivered",
                created=sent,
                sent=sent,
                messaging_service_sid=mg_sid,
                price="-0.00790",
            )
        )

    # A few completed calls (one recorded) to recent SMS contacts.
    calls = ctx.store.collection("calls")
    recordings = ctx.store.collection("recordings")
    for i, conv in enumerate(sms_convs[:3]):
        contact = world.person(conv.person_id)
        if not contact.phone_mobile:
            continue
        when = anchor - timedelta(days=5 - i, hours=2)
        call_sid = stable_sid("CA", "call", conv.id)
        duration = 90 + i * 75
        call = build_call(
            sid_=call_sid,
            account_sid=account_sid,
            to=contact.phone_mobile,
            from_=NUMBER_TORONTO,
            status="completed",
            created=when,
            phone_number_sid=number_sids[NUMBER_TORONTO],
            record=(i == 0),
        )
        from emulators.providers.twilio.common import rfc2822 as _rfc

        call["start_time"] = _rfc(when)
        call["end_time"] = _rfc(when + timedelta(seconds=duration))
        call["duration"] = str(duration)
        call["price"] = f"-{0.014 * max(1, (duration + 59) // 60):.5f}"
        call["answered_by"] = "human"
        call["_started"] = isoformat_z(when)
        calls.put(call)
        if i == 0:
            recordings.put(
                build_recording(
                    sid_=stable_sid("RE", "recording", conv.id),
                    account_sid=account_sid,
                    call_sid=call_sid,
                    created=when,
                    duration_s=duration,
                )
            )

    _seed_conversations(ctx, account_sid, mg_sid)


def _seed_conversations(ctx: ProviderContext, account_sid: str, mg_sid: str) -> None:
    """Project the world's SMS/WhatsApp threads into the Conversations API."""
    world = ctx.world
    convs = ctx.store.collection("conversations")
    participants = ctx.store.collection("conv_participants")
    conv_messages = ctx.store.collection("conv_messages")
    chat_sid = ctx.store.meta["chat_service_sid"]
    role_sid = ctx.store.meta["participant_role_sid"]
    for conv in world.conversations:
        if conv.channel not in ("sms", "whatsapp"):
            continue
        try:
            contact = world.person(conv.person_id)
        except KeyError:
            continue
        if not contact.phone_mobile or not conv.messages:
            continue
        prefix = "whatsapp:" if conv.channel == "whatsapp" else ""
        business = f"{prefix}{conv.business_number}"
        contact_addr = f"{prefix}{contact.phone_mobile}"
        conv_sid = stable_sid("CH", "conversation", conv.id)
        first_at = conv.messages[0].sent
        last_at = conv.messages[-1].sent
        convs.put(
            {
                "id": conv_sid,
                "sid": conv_sid,
                "account_sid": account_sid,
                "chat_service_sid": chat_sid,
                "friendly_name": f"{contact.display_name} ({conv.channel})",
                "unique_name": conv.id,
                "attributes": "{}",
                "messaging_service_sid": mg_sid if conv.channel == "sms" else None,
                "date_created": iso8601(first_at),
                "date_updated": iso8601(last_at),
                "state": "active",
                "timers": {"date_inactive": None, "date_closed": None},
                "bindings": {},
                "_account": account_sid,
            }
        )
        agent_sid = stable_sid("MB", "participant", conv.id, "agent")
        participants.put(
            {
                "id": agent_sid,
                "sid": agent_sid,
                "account_sid": account_sid,
                "conversation_sid": conv_sid,
                "identity": "halcyon-advisory",
                "attributes": "{}",
                "messaging_binding": None,
                "role_sid": role_sid,
                "date_created": iso8601(first_at),
                "date_updated": iso8601(first_at),
                "last_read_message_index": len(conv.messages) - 1,
                "last_read_timestamp": iso8601(last_at),
            }
        )
        contact_part_sid = stable_sid("MB", "participant", conv.id, "contact")
        participants.put(
            {
                "id": contact_part_sid,
                "sid": contact_part_sid,
                "account_sid": account_sid,
                "conversation_sid": conv_sid,
                "identity": None,
                "attributes": "{}",
                "messaging_binding": {
                    "type": conv.channel,
                    "address": contact_addr,
                    "proxy_address": business,
                    "projected_address": None,
                    "level": None,
                },
                "role_sid": role_sid,
                "date_created": iso8601(first_at),
                "date_updated": iso8601(first_at),
                "last_read_message_index": None,
                "last_read_timestamp": None,
            }
        )
        for index, msg in enumerate(conv.messages):
            outbound = msg.direction == "outbound"
            msg_sid = stable_sid("IM", "conv-message", msg.id)
            conv_messages.put(
                {
                    "id": msg_sid,
                    "sid": msg_sid,
                    "account_sid": account_sid,
                    "conversation_sid": conv_sid,
                    "author": "halcyon-advisory" if outbound else contact_addr,
                    "body": msg.text,
                    "attributes": "{}",
                    "participant_sid": agent_sid if outbound else contact_part_sid,
                    "media": None,
                    "content_sid": None,
                    "index": index,
                    "delivery": {
                        "total": 2,
                        "sent": 1,
                        "delivered": 1,
                        "read": 0,
                        "failed": 0,
                        "undelivered": 0,
                    },
                    "date_created": iso8601(msg.sent),
                    "date_updated": iso8601(msg.sent),
                }
            )
