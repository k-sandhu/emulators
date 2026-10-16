"""Programmable Messaging: Messages, Media, delivery progression and inbound SMS.

Outbound messages progress ``queued → sending → sent → delivered`` (``accepted``
first when sent via a Messaging Service, ``scheduled`` when ``SendAt`` is given),
POSTing the ``StatusCallback`` at each step. In ``sync`` webhook mode the whole
progression (and its callbacks) completes before the create response returns, but
the create response itself still shows the initial status, exactly like Twilio.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import isoformat_z, parse_instant, utc
from emulators.core.provider import ctx_url_for_host
from emulators.providers.twilio.common import (
    API_HOST,
    API_VERSION,
    HELP_WORDS,
    MAGIC_FROM_ERRORS,
    MAGIC_FROM_OK,
    MAGIC_TO_ERRORS,
    OPT_IN_WORDS,
    OPT_OUT_WORDS,
    WHATSAPP_SANDBOX,
    absolute,
    auto_advance,
    bare_number,
    check_account,
    creds,
    deliver_form,
    fail,
    form_params,
    geo_for,
    get_param,
    newest_first,
    not_found,
    num_segments,
    page_2010,
    param_value,
    payload_text,
    require_auth,
    rfc2822,
    spawn,
    split_address,
    valid_e164,
    valid_url,
)
from emulators.providers.twilio.payloads import render_for_content_type
from emulators.providers.twilio.shapes import build_media, build_message, render_media, render_message, touch

if TYPE_CHECKING:
    from emulators.core.provider import ProviderContext

Record = dict[str, Any]

_CHAIN = ["queued", "sending", "sent", "delivered"]
_ADVANCE_TARGETS = ("queued", "sending", "sent", "delivered", "undelivered", "failed", "canceled", "read")
# Which statuses a message may be pushed to from each status (Twilio never moves backwards).
_TRANSITIONS: dict[str, tuple[str, ...]] = {
    # "Send now" for a scheduled message: release it, or push it straight through.
    "scheduled": ("queued", "sending", "sent", "delivered", "undelivered", "failed", "canceled"),
    "accepted": ("sending", "sent", "delivered", "undelivered", "failed"),
    "queued": ("sending", "sent", "delivered", "undelivered", "failed"),
    "sending": ("sent", "delivered", "undelivered", "failed"),
    "sent": ("delivered", "undelivered", "failed", "read"),
    "delivered": ("read",),
}
_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".vcf": "text/vcard",
}


def _now(ctx: ProviderContext) -> datetime:
    return ctx.clock.now()


def _content_type_for(url: str) -> str:
    path = urlsplit(url).path.lower()
    for ext, ctype in _CONTENT_TYPES.items():
        if path.endswith(ext):
            return ctype
    return "image/jpeg"  # Twilio MMS media is overwhelmingly images; default like an upload.


# -- lookups over the store --------------------------------------------------------


def numbers_by_phone(ctx: ProviderContext) -> dict[str, Record]:
    return {n["phone_number"]: n for n in ctx.store.collection("numbers").values()}


def service_for_number(ctx: ProviderContext, phone_number: str) -> Record | None:
    number = numbers_by_phone(ctx).get(phone_number)
    if number is None:
        return None
    for link in ctx.store.collection("mg_numbers").values():
        if link["sid"] == number["sid"]:
            return ctx.store.collection("messaging_services").get(link["service_sid"])
    return None


def opted_out(ctx: ProviderContext, senders: list[str], recipient: str) -> bool:
    store = ctx.store.collection("optouts")
    return any(store.get(f"{sender}|{recipient}") is not None for sender in senders if sender)


def _set_optout(ctx: ProviderContext, business: str, contact: str, out: bool) -> None:
    store = ctx.store.collection("optouts")
    keys = [f"{business}|{contact}"]
    service = service_for_number(ctx, bare_number(business))
    if service is not None:
        keys.append(f"{service['sid']}|{contact}")
    for key in keys:
        if out:
            store.put_as(key, {"id": key, "business": business, "contact": contact})
        else:
            store.delete(key)


# -- delivery progression ----------------------------------------------------------


def _failure_for(record: Record) -> tuple[int, str] | None:
    bare = bare_number(record["to"])
    if bare.endswith("0199"):
        return 30003, "Unreachable destination handset"
    if bare.endswith("0198"):
        return 30005, "Unknown destination handset"
    return None


async def _fire_status_callback(ctx: ProviderContext, record: Record) -> None:
    if record.get("_test"):
        return  # Test credentials never trigger real delivery webhooks.
    url = record.get("_status_callback")
    if not url and record.get("messaging_service_sid"):
        service = ctx.store.collection("messaging_services").get(record["messaging_service_sid"])
        url = service.get("status_callback") if service else None
    if not url:
        # An IncomingPhoneNumber's StatusCallback is for *voice* calls, so it never applies here.
        return
    params = {
        "MessageSid": record["sid"],
        "SmsSid": record["sid"],
        "AccountSid": record["account_sid"],
        "From": record["from"],
        "To": record["to"],
        "MessageStatus": record["status"],
        "SmsStatus": record["status"],
        "ApiVersion": API_VERSION,
    }
    if record.get("messaging_service_sid"):
        params["MessagingServiceSid"] = record["messaging_service_sid"]
    if split_address(record["to"])[0] == "whatsapp":
        params["ChannelToAddress"] = bare_number(record["to"])
        params["ChannelPrefix"] = "whatsapp"
    if record["status"] in ("delivered", "undelivered"):
        params["RawDlrDoneDate"] = utc(_now(ctx)).strftime("%y%m%d%H%M")
    if record.get("error_code") is not None:
        params["ErrorCode"] = str(record["error_code"])
    await deliver_form(
        ctx,
        url,
        params,
        event_type="message.status",  # Message callbacks are always POSTs
    )


def _apply_status(ctx: ProviderContext, record: Record, status: str) -> None:
    now = _now(ctx)
    record["status"] = status
    touch(record, now)
    if status in ("sent", "delivered", "undelivered", "failed") and not record.get("_sent"):
        record["_sent"] = isoformat_z(now)
        record["date_sent"] = rfc2822(now)
    if status in ("delivered", "undelivered", "read"):  # failed messages are not billed
        segments = max(1, int(record.get("num_segments") or 1))
        record["price"] = f"-{0.0079 * segments:.5f}"
    if status in ("undelivered", "failed") and record.get("error_code") is None:
        # Twilio always attaches an error code to messages that did not reach the handset.
        record["error_code"], record["error_message"] = _failure_for(record) or (30008, "Unknown error")


async def advance_message(ctx: ProviderContext, record: Record, to_status: str | None = None) -> Record:
    """Move a message along the delivery chain, firing its StatusCallback per step."""
    if record["status"] in ("delivered", "undelivered", "failed", "received", "read", "canceled"):
        if to_status and to_status != record["status"]:
            _apply_status(ctx, record, to_status)
            await _fire_status_callback(ctx, record)
        return record
    if record["status"] == "scheduled" and to_status is None:
        return record
    failure = _failure_for(record)
    final = "undelivered" if failure else "delivered"
    chain = ["accepted", *_CHAIN[:-1], final] if record["status"] == "accepted" else [*_CHAIN[:-1], final]
    try:
        start = chain.index(record["status"])
    except ValueError:
        start = -1
    steps = chain[start + 1 :]
    if to_status:
        if to_status in steps:
            steps = steps[: steps.index(to_status) + 1]
        elif to_status in ("canceled", "failed", "undelivered", "delivered"):
            steps = [to_status]
    for step in steps:
        if ctx.store.collection("messages").get(record["sid"]) is None:
            break  # Deleted (or reset) while advancing.
        if step == final and failure:
            record["error_code"], record["error_message"] = failure
        _apply_status(ctx, record, step)
        await _fire_status_callback(ctx, record)
    return record


async def maybe_advance(ctx: ProviderContext, record: Record) -> None:
    if not auto_advance(ctx):
        return  # EMULATORS_TWILIO_AUTO_ADVANCE=0: tests drive the message with advance_message
    mode = ctx.webhooks.mode
    if mode in ("sync", "manual"):
        await advance_message(ctx, record)
    else:

        async def _delayed() -> None:
            await asyncio.sleep(0.05)
            await advance_message(ctx, record)

        spawn(ctx, _delayed())


# -- outbound creation -------------------------------------------------------------


_SEND_AT_MIN = timedelta(minutes=15)
_SEND_AT_MAX = timedelta(days=35)
_CONTENT_SID_RE = re.compile(r"^HX[0-9a-f]{32}$")
_SUPPORTED_CHANNELS = ("sms", "whatsapp")


def parse_send_at(ctx: ProviderContext, send_at: str, schedule_type: str | None) -> datetime:
    """``SendAt`` must be ISO 8601, 15 minutes to 35 days ahead, with ``ScheduleType=fixed``."""
    if schedule_type and schedule_type.lower() != "fixed":
        raise fail(400, 35115, f"Invalid ScheduleType: {schedule_type}. The only supported value is 'fixed'.")
    try:
        when = parse_instant(send_at)
    except ValueError:
        raise fail(400, 35114, f"Invalid SendAt: {send_at}. SendAt must be an ISO 8601 timestamp.") from None
    now = _now(ctx)
    if when < now + _SEND_AT_MIN or when > now + _SEND_AT_MAX:
        raise fail(
            400,
            35114,
            "Invalid SendAt: the scheduled time must be more than 15 minutes and fewer than 35 days "
            "in the future.",
        )
    return when


def validate_send(
    ctx: ProviderContext,
    *,
    account_sid: str,
    to: str | None,
    from_: str | None,
    body: str | None,
    media_urls: list[str],
    messaging_service_sid: str | None,
    send_at: str | None,
    schedule_type: str | None = None,
    content_sid: str | None = None,
    status_callback: str | None = None,
) -> tuple[str | None, Record | None, datetime | None]:
    """Twilio's create-time validation; returns (resolved From, messaging service, send time)."""
    if not to:
        raise fail(400, 21604, "A 'To' phone number is required.")
    to_channel, to_bare = split_address(to)
    magic_to = MAGIC_TO_ERRORS.get(to_bare)
    if magic_to:
        code, template = magic_to
        raise fail(400, code, template.format(to=to_bare, from_=from_ or ""))
    if to_channel not in _SUPPORTED_CHANNELS or not valid_e164(to_bare):
        raise fail(400, 21211, f"The 'To' number {to} is not a valid phone number.")
    if body is not None and len(body) > 1600:
        raise fail(400, 21617, "The concatenated message body exceeds the 1600 character limit.")
    if len(media_urls) > 10:
        raise fail(400, 21623, "The message has more than the maximum allowed 10 media attachments.")
    for url in media_urls:
        if not valid_url(url):
            raise fail(400, 21620, f"Invalid media URL(s): {url}")
    if status_callback and not valid_url(status_callback):
        raise fail(400, 21609, f"StatusCallback URL {status_callback} is not a valid URL.")
    if content_sid and not _CONTENT_SID_RE.match(content_sid):
        raise fail(400, 21655, f"Invalid ContentSid: {content_sid}")
    if not body and not media_urls and not content_sid:
        raise fail(400, 21602, "A text message body or media urls are required.")

    service = None
    if messaging_service_sid:
        service = ctx.store.collection("messaging_services").get(messaging_service_sid)
        if service is None or service["account_sid"] != account_sid:
            raise not_found(f"/v1/Services/{messaging_service_sid}")
    resolved_from = from_
    if from_:
        from_channel, from_bare = split_address(from_)
        magic_from = MAGIC_FROM_ERRORS.get(from_bare)
        if magic_from:
            code, template = magic_from
            raise fail(400, code, template.format(from_=from_bare, to=to_bare))
        owned = numbers_by_phone(ctx)
        is_alphanumeric = bool(re.fullmatch(r"[A-Za-z0-9 ]{1,11}", from_)) and not from_.startswith("+")
        if from_channel not in _SUPPORTED_CHANNELS:
            raise fail(400, 21212, f"The 'From' number {from_} is not a valid phone number.")
        if from_channel != to_channel:
            raise fail(400, 21910, "Invalid From and To pair. From and To should be of the same channel.")
        if from_channel == "whatsapp":
            sender = owned.get(from_bare)
            if from_bare != WHATSAPP_SANDBOX and (sender is None or not sender.get("_whatsapp")):
                raise fail(400, 63007, "Twilio could not find a Channel with the specified From address.")
        elif from_bare != MAGIC_FROM_OK and from_bare not in owned and not is_alphanumeric:
            raise fail(
                400,
                21606,
                "The 'From' phone number provided is not a valid message-capable Twilio phone number "
                "for this destination/account. It must be a Twilio phone number or a valid shortcode "
                "for this destination/account.",
            )
        number = owned.get(from_bare)
        if number is not None and number.get("_account") not in (None, account_sid):
            raise fail(400, 21606, "The 'From' phone number provided is not owned by this account.")
    elif service is not None:
        links = [
            lnk for lnk in ctx.store.collection("mg_numbers").values() if lnk["service_sid"] == service["sid"]
        ]
        if not links:
            raise fail(400, 21701, "The Messaging Service does not have any senders in its Sender Pool.")
        # Sticky sender: pick the pool number that most recently messaged this recipient.
        prior = [
            m
            for m in ctx.store.collection("messages").values()
            if m["to"] == to and m.get("direction", "").startswith("outbound")
        ]
        pool = {ctx.store.collection("numbers").get(lnk["sid"])["phone_number"] for lnk in links}  # type: ignore[index]
        sticky = next((m["from"] for m in reversed(prior) if bare_number(m["from"]) in pool), None)
        resolved_from = sticky or sorted(pool)[0]
        if to_channel == "whatsapp" and not resolved_from.startswith("whatsapp:"):
            resolved_from = f"whatsapp:{resolved_from}"
    else:
        raise fail(400, 21603, "A 'From' phone number is required to send a message.")
    scheduled_for: datetime | None = None
    if send_at:
        if not messaging_service_sid:
            raise fail(400, 21704, "Scheduling is only available when sending via a Messaging Service.")
        scheduled_for = parse_send_at(ctx, send_at, schedule_type)
    senders = [resolved_from or "", messaging_service_sid or ""]
    if opted_out(ctx, senders, to):
        raise fail(400, 21610, "The recipient has blocked messages from this number or has opted out.")
    return resolved_from, service, scheduled_for


async def create_outbound(
    ctx: ProviderContext,
    *,
    account_sid: str,
    to: str,
    from_: str | None,
    body: str | None,
    media_urls: list[str] | None = None,
    messaging_service_sid: str | None = None,
    status_callback: str | None = None,
    status_callback_method: str = "POST",
    send_at: str | None = None,
    schedule_type: str | None = None,
    content_sid: str | None = None,
    direction: str = "outbound-api",
    is_test: bool = False,
    validate: bool = True,
    advance: bool = True,
) -> tuple[Record, Record]:
    """Create (and normally advance) an outbound message; returns (record, snapshot)."""
    media_urls = media_urls or []
    scheduled_for: datetime | None = None
    if validate:
        from_, _service, scheduled_for = validate_send(
            ctx,
            account_sid=account_sid,
            to=to,
            from_=from_,
            body=body,
            media_urls=media_urls,
            messaging_service_sid=messaging_service_sid,
            send_at=send_at,
            schedule_type=schedule_type,
            content_sid=content_sid,
            status_callback=status_callback,
        )
    prefix = "MM" if media_urls else "SM"
    sid_ = prefix + ctx.ids.hex(32)
    now = _now(ctx)
    if scheduled_for:
        status = "scheduled"
    elif messaging_service_sid:
        status = "accepted"
    else:
        status = "queued"
    record = build_message(
        sid_=sid_,
        account_sid=account_sid,
        to=to,
        from_=from_ or "",
        body=body,
        direction=direction,
        status=status,
        created=now,
        messaging_service_sid=messaging_service_sid,
        num_media=len(media_urls),
        status_callback=status_callback,
        status_callback_method=status_callback_method,
    )
    record["_test"] = is_test
    record["_send_at"] = isoformat_z(scheduled_for) if scheduled_for else None
    if content_sid:
        record["_content_sid"] = content_sid
        record["num_segments"] = "1"
    ctx.store.collection("messages").put(record)
    for url in media_urls:
        media = build_media(
            sid_="ME" + ctx.ids.hex(32),
            account_sid=account_sid,
            parent_sid=sid_,
            content_type=_content_type_for(url),
            created=now,
            source_url=url,
        )
        ctx.store.collection("media").put(media)
    snapshot = render_message(record)  # pre-advancement wire snapshot
    if advance and status != "scheduled":
        await maybe_advance(ctx, record)
    return record, snapshot


async def tick_scheduled(ctx: ProviderContext) -> None:
    """Release scheduled messages whose ``SendAt`` has passed on the emulator clock.

    Twilio sends them at that time (``scheduled`` → ``queued`` → ``sent`` → ``delivered``); the
    emulator has no background timer, so it checks whenever messages are read or listed, which is
    what tests that advance ``POST /_emulator/clock`` observe.
    """
    now = _now(ctx)
    for record in ctx.store.collection("messages").values():
        send_at = record.get("_send_at")
        if record["status"] == "scheduled" and send_at and parse_instant(send_at) <= now:
            record["status"] = "queued"
            touch(record, now)
            await maybe_advance(ctx, record)


# -- media content -----------------------------------------------------------------


def media_bytes(ctx: ProviderContext, media: Record) -> bytes:
    """Render (once, then blob-cache) the bytes behind a Media resource."""
    blob = media.get("_blob")
    if blob and ctx.store.blobs.has(blob):
        return ctx.store.blobs.get(blob)
    from emulators.world import documents

    if media.get("_document_id"):
        doc = ctx.world.document(media["_document_id"])
        table = [list(row) for row in doc.table] if doc.table else None
        content, _mime = documents.render(doc.kind, doc.title, list(doc.paragraphs), table=table)
    else:
        seed = int(media["sid"][-6:], 16) % 100000
        content = render_for_content_type(
            media.get("content_type") or "", media.get("_source_url") or "media", seed
        )
    media["_blob"] = ctx.store.blobs.put(content)
    return content


def media_content_url(ctx: ProviderContext, media: Record, request: Request | None = None) -> str:
    path = (
        f"/{API_VERSION}/Accounts/{media['account_sid']}/Messages/{media['parent_sid']}/Media/{media['sid']}"
    )
    if request is not None:
        return absolute(request, ctx, API_HOST, path)
    # No request (webhook parameters): follow however the emulator was last reached, so the URL
    # works in host, prefix and per-port mode alike.
    return ctx_url_for_host(ctx, API_HOST, path)


# -- inbound simulation ------------------------------------------------------------


async def _system_reply(ctx: ProviderContext, account_sid: str, to: str, from_: str, body: str) -> Record:
    record, _ = await create_outbound(
        ctx,
        account_sid=account_sid,
        to=to,
        from_=from_,
        body=body,
        direction="outbound-reply",
        validate=False,
    )
    return record


def _parse_twiml_replies(xml_text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    replies = []
    for message in root.iter("Message"):
        media = [node.text.strip() for node in message.findall("Media") if node.text]
        body_node = message.find("Body")
        text = (body_node.text if body_node is not None else message.text) or ""
        replies.append(
            {"to": message.get("to"), "from": message.get("from"), "body": text.strip(), "media_urls": media}
        )
    return replies


def _profile_name(ctx: ProviderContext, number: str) -> str:
    for person in ctx.world.people:
        if person.phone_mobile == number:
            return person.display_name
    return ""


def _inbound_params(
    ctx: ProviderContext,
    payload: dict[str, Any],
    sid_: str,
    account_sid: str,
    from_: str,
    to: str,
    body: str | None,
    service: Record | None,
    media_records: list[Record],
) -> dict[str, str]:
    """The form parameters Twilio POSTs to an SMS/WhatsApp webhook (the two channels differ)."""
    channel, from_bare = split_address(from_)
    if channel == "whatsapp":
        params = {
            "SmsMessageSid": sid_,
            "NumMedia": str(len(media_records)),
            "ProfileName": str(payload.get("profile_name") or _profile_name(ctx, from_bare)),
            "MessageType": "image" if media_records else "text",
            "SmsSid": sid_,
            "WaId": from_bare.lstrip("+"),
            "SmsStatus": "received",
            "Body": body or "",
            "To": to,
            "NumSegments": "1",
            "ReferralNumMedia": "0",
            "MessageSid": sid_,
            "AccountSid": account_sid,
            "From": from_,
            "ApiVersion": API_VERSION,
        }
    else:
        from_city, from_state, from_country = geo_for(from_)
        to_city, to_state, to_country = geo_for(to)
        params = {
            "ToCountry": to_country,
            "ToState": to_state,
            "SmsMessageSid": sid_,
            "NumMedia": str(len(media_records)),
            "ToCity": to_city,
            "FromZip": "",
            "SmsSid": sid_,
            "FromState": from_state,
            "SmsStatus": "received",
            "FromCity": from_city,
            "Body": body or "",
            "FromCountry": from_country,
            "To": to,
            "ToZip": "",
            "NumSegments": str(num_segments(body or "")),
            "MessageSid": sid_,
            "AccountSid": account_sid,
            "From": from_,
            "ApiVersion": API_VERSION,
        }
    if service is not None:
        params["MessagingServiceSid"] = service["sid"]
    for i, media in enumerate(media_records):
        params[f"MediaContentType{i}"] = media["content_type"]
        params[f"MediaSid{i}"] = media["sid"]
        params[f"MediaUrl{i}"] = media_content_url(ctx, media)
    return params


async def simulate_inbound(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """``POST /_emulator/actions/inbound_sms`` — see docs/providers/twilio.md."""
    c = creds(ctx)
    account_sid = c["account_sid"]
    from_ = payload_text(payload, "from", "From", "from_")
    to = payload_text(payload, "to", "To")
    body = payload_text(payload, "body", "Body") or ""
    raw_media = payload.get("media_urls") or payload.get("MediaUrls") or []
    media_urls = [str(u) for u in raw_media] if isinstance(raw_media, list) else [str(raw_media)]
    if not from_ or not to:
        raise fail(400, 20001, "inbound_sms requires 'from' and 'to'.")
    now = _now(ctx)
    sid_ = ("MM" if media_urls else "SM") + ctx.ids.hex(32)
    record = build_message(
        sid_=sid_,
        account_sid=account_sid,
        to=to,
        from_=from_,
        body=body,
        direction="inbound",
        status="received",
        created=now,
        sent=now,
        num_media=len(media_urls),
        price="-0.00750",
    )
    service = service_for_number(ctx, bare_number(to))
    if service is not None:
        record["messaging_service_sid"] = service["sid"]
    ctx.store.collection("messages").put(record)
    media_records = []
    for url in media_urls:
        media = build_media(
            sid_="ME" + ctx.ids.hex(32),
            account_sid=account_sid,
            parent_sid=sid_,
            content_type=_content_type_for(url),
            created=now,
            source_url=url,
        )
        ctx.store.collection("media").put(media)
        media_records.append(media)

    result: dict[str, Any] = {"message": render_message(record), "replies": [], "webhook": None}
    keyword = (body or "").strip().upper()
    if keyword in OPT_OUT_WORDS:
        _set_optout(ctx, to, from_, True)
        reply = await _system_reply(
            ctx,
            account_sid,
            from_,
            to,
            "You have successfully been unsubscribed. You will not receive any more messages from this "
            "number. Reply START to resubscribe.",
        )
        result["replies"] = [render_message(reply)]
        result["opted_out"] = True
        return result
    if keyword in OPT_IN_WORDS:
        _set_optout(ctx, to, from_, False)
        reply = await _system_reply(
            ctx,
            account_sid,
            from_,
            to,
            "You have successfully been resubscribed to messages from this number. Reply HELP for help. "
            "Reply STOP to unsubscribe. Msg&Data Rates May Apply.",
        )
        result["replies"] = [render_message(reply)]
        result["opted_in"] = True
        return result
    if keyword in HELP_WORDS:
        reply = await _system_reply(
            ctx,
            account_sid,
            from_,
            to,
            "Halcyon Advisory Group: Reply STOP to unsubscribe. Msg&Data Rates May Apply.",
        )
        result["replies"] = [render_message(reply)]
        return result

    number = numbers_by_phone(ctx).get(bare_number(to))
    webhook_url = None
    method = "POST"
    if (
        service is not None
        and service.get("inbound_request_url")
        and not service.get("use_inbound_webhook_on_number")
    ):
        webhook_url = service["inbound_request_url"]
        method = service.get("inbound_method") or "POST"
    elif number is not None and number.get("sms_url"):
        webhook_url = number["sms_url"]
        method = number.get("sms_method") or "POST"
    if not webhook_url:
        return result

    params = _inbound_params(ctx, payload, sid_, account_sid, from_, to, body, service, media_records)
    delivery = await deliver_form(ctx, webhook_url, params, event_type="message.inbound", method=method)
    if ctx.webhooks.mode == "async":
        await ctx.webhooks.drain()
        delivery = ctx.webhooks.deliveries.get(delivery["id"]) or delivery
    fallback_url = number.get("sms_fallback_url") if number is not None else None
    if delivery.get("status") == "failed" and fallback_url and ctx.webhooks.mode != "manual":
        # Twilio retries the request against the SmsFallbackUrl when the primary URL errors.
        fallback_params = {**params, "ErrorCode": "11200", "ErrorUrl": webhook_url}
        delivery = await deliver_form(
            ctx,
            fallback_url,
            fallback_params,
            event_type="message.inbound",
            method=number.get("sms_fallback_method") or "POST",
        )
        if ctx.webhooks.mode == "async":
            await ctx.webhooks.drain()
            delivery = ctx.webhooks.deliveries.get(delivery["id"]) or delivery
        webhook_url = fallback_url
    result["webhook"] = {
        "url": webhook_url,
        "status": delivery.get("status"),
        "response_status": delivery.get("response_status"),
    }
    for reply_spec in _parse_twiml_replies(delivery.get("response_body") or ""):
        reply, _snap = await create_outbound(
            ctx,
            account_sid=account_sid,
            to=reply_spec["to"] or from_,
            from_=reply_spec["from"] or to,
            body=reply_spec["body"] or None,
            media_urls=reply_spec["media_urls"],
            direction="outbound-reply",
            validate=False,
        )
        result["replies"].append(render_message(reply))
    return result


# -- routes ------------------------------------------------------------------------


def _message_or_404(ctx: ProviderContext, account: str, sid_: str) -> Record:
    record = ctx.store.collection("messages").get(sid_)
    if record is None or record.get("_account") != account:
        raise not_found(f"/{API_VERSION}/Accounts/{account}/Messages/{sid_}.json")
    return record


def register(app: FastAPI, ctx: ProviderContext) -> None:
    base = f"/{API_VERSION}/Accounts/{{account_sid}}/Messages"

    @app.post(base + ".json")
    async def create_message(request: Request, account_sid: str) -> Response:
        auth = require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        params, multi = await form_params(request)
        media_urls = [
            u for u in (multi.get("MediaUrl") or multi.get("mediaurl") or [params.get("MediaUrl", "")]) if u
        ]
        _record, snapshot = await create_outbound(
            ctx,
            account_sid=account,
            to=get_param(params, "To") or "",
            from_=get_param(params, "From"),
            body=param_value(params, "Body"),
            media_urls=media_urls,
            messaging_service_sid=get_param(params, "MessagingServiceSid"),
            status_callback=get_param(params, "StatusCallback"),
            send_at=get_param(params, "SendAt"),
            schedule_type=get_param(params, "ScheduleType"),
            content_sid=get_param(params, "ContentSid"),
            is_test=auth.is_test,
        )
        if account_sid != account:
            # Alias (test-credentials) SID: re-stamp the pre-advancement snapshot.
            snapshot = {**snapshot, "account_sid": account_sid}
            snapshot["uri"] = snapshot["uri"].replace(f"/Accounts/{account}/", f"/Accounts/{account_sid}/")
            snapshot["subresource_uris"] = {
                k: v.replace(f"/Accounts/{account}/", f"/Accounts/{account_sid}/")
                for k, v in snapshot["subresource_uris"].items()
            }
        return JSONResponse(snapshot, status_code=201)

    @app.get(base + ".json")
    async def list_messages(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        await tick_scheduled(ctx)
        records = [m for m in ctx.store.collection("messages").values() if m.get("_account") == account]
        query = request.query_params
        if query.get("To"):
            records = [m for m in records if m["to"] == query["To"]]
        if query.get("From"):
            records = [m for m in records if m["from"] == query["From"]]
        for key, op in (("DateSent", "eq"), ("DateSent<", "lte"), ("DateSent>", "gte")):
            raw = query.get(key)
            if not raw:
                continue
            try:
                bound = parse_instant(raw)
            except ValueError:
                raise fail(
                    400, 20001, f"Invalid {key}: {raw}. Use YYYY-MM-DD or an ISO 8601 timestamp."
                ) from None

            def _match(m: Record, bound: datetime = bound, op: str = op) -> bool:
                sent = m.get("_sent")  # Unsent messages have no date_sent and never match.
                if not sent:
                    return False
                instant = parse_instant(sent)
                if op == "eq":  # DateSent is a UTC day filter even when given a full timestamp
                    return instant.date() == bound.date()
                return instant <= bound if op == "lte" else instant >= bound

            records = [m for m in records if _match(m)]
        records = newest_first(records, "_sent", "_created")
        rendered = [render_message(m, account_sid) for m in records]
        return page_2010(
            request, "messages", rendered, f"/{API_VERSION}/Accounts/{account_sid}/Messages.json"
        )

    @app.get(base + "/{sid}.json")
    async def get_message(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        await tick_scheduled(ctx)
        return render_message(_message_or_404(ctx, account, sid), account_sid)

    @app.post(base + "/{sid}.json")
    async def update_message(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        await tick_scheduled(ctx)
        record = _message_or_404(ctx, account, sid)
        params, _multi = await form_params(request)
        body = param_value(params, "Body")
        if body is not None:
            if body != "":
                raise fail(
                    400, 20001, "The Body of a message can only be updated to an empty string (redact)."
                )
            record["body"] = ""
        status = get_param(params, "Status")
        if status:
            if status != "canceled":
                raise fail(400, 20001, f"Status '{status}' is not a valid update status. Use 'canceled'.")
            if record["status"] != "scheduled":
                raise fail(400, 20001, "Only scheduled messages can be canceled.")
            record["status"] = "canceled"
        touch(record, _now(ctx))
        return render_message(record, account_sid)

    @app.delete(base + "/{sid}.json")
    async def delete_message(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        _message_or_404(ctx, account, sid)
        ctx.store.collection("messages").delete(sid)
        media_store = ctx.store.collection("media")
        for media in [m for m in media_store.values() if m["parent_sid"] == sid]:
            media_store.delete(media["sid"])
        return Response(status_code=204)

    @app.post(base + "/{sid}/Feedback.json")
    async def create_feedback(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        _message_or_404(ctx, account, sid)
        params, _multi = await form_params(request)
        outcome = get_param(params, "Outcome") or "unconfirmed"
        if outcome not in ("confirmed", "unconfirmed"):
            raise fail(400, 20001, f"Invalid Outcome: {outcome}. Must be confirmed or unconfirmed.")
        now = _now(ctx)
        body = {
            "account_sid": account_sid,
            "date_created": rfc2822(now),
            "date_updated": rfc2822(now),
            "message_sid": sid,
            "outcome": outcome,
            "uri": f"/{API_VERSION}/Accounts/{account_sid}/Messages/{sid}/Feedback.json",
        }
        ctx.store.collection("feedback").put({"id": sid, **body})
        return JSONResponse(body, status_code=201)

    @app.get(base + "/{sid}/Media.json")
    async def list_media(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        _message_or_404(ctx, account, sid)
        records = [render_media(m) for m in ctx.store.collection("media").values() if m["parent_sid"] == sid]
        return page_2010(
            request, "media_list", records, f"/{API_VERSION}/Accounts/{account_sid}/Messages/{sid}/Media.json"
        )

    @app.get(base + "/{sid}/Media/{media_sid}.json")
    async def get_media(request: Request, account_sid: str, sid: str, media_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        _message_or_404(ctx, account, sid)
        media = ctx.store.collection("media").get(media_sid)
        if media is None or media["parent_sid"] != sid:
            raise not_found(f"/{API_VERSION}/Accounts/{account_sid}/Messages/{sid}/Media/{media_sid}.json")
        return render_media(media)

    @app.get(base + "/{sid}/Media/{media_sid}")
    async def get_media_content(request: Request, account_sid: str, sid: str, media_sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        _message_or_404(ctx, account, sid)
        media = ctx.store.collection("media").get(media_sid)
        if media is None or media["parent_sid"] != sid:
            raise not_found(f"/{API_VERSION}/Accounts/{account_sid}/Messages/{sid}/Media/{media_sid}")
        return Response(media_bytes(ctx, media), media_type=media["content_type"])

    @app.delete(base + "/{sid}/Media/{media_sid}.json")
    async def delete_media(request: Request, account_sid: str, sid: str, media_sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        record = _message_or_404(ctx, account, sid)
        media = ctx.store.collection("media").get(media_sid)
        if media is None or media["parent_sid"] != sid:
            raise not_found(f"/{API_VERSION}/Accounts/{account_sid}/Messages/{sid}/Media/{media_sid}.json")
        ctx.store.collection("media").delete(media_sid)
        remaining = [m for m in ctx.store.collection("media").values() if m["parent_sid"] == sid]
        record["num_media"] = str(len(remaining))
        return Response(status_code=204)


# -- actions -----------------------------------------------------------------------


async def action_inbound_sms(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Simulate an inbound SMS/MMS/WhatsApp message (fires the number's SmsUrl)."""
    return await simulate_inbound(ctx, payload)


async def action_advance_message(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Advance a message's delivery status (optionally to a named status)."""
    sid_ = payload_text(payload, "sid", "Sid")
    record = ctx.store.collection("messages").get(sid_) if sid_ else None
    if record is None:
        raise fail(404, 20404, f"Message {sid_} not found.")
    target = payload_text(payload, "status", "Status")
    if target and target not in _ADVANCE_TARGETS:
        raise fail(400, 20001, f"Unknown status '{target}'. Use one of {', '.join(_ADVANCE_TARGETS)}.")
    if target and target not in _TRANSITIONS.get(record["status"], ()):
        raise fail(400, 20001, f"A message cannot move from '{record['status']}' to '{target}'.")
    await advance_message(ctx, record, target)
    return {"message": render_message(record)}


# Re-exported for other modules (Verify sends its code as a message).
__all__ = [
    "action_advance_message",
    "action_inbound_sms",
    "advance_message",
    "create_outbound",
    "media_bytes",
    "media_content_url",
    "numbers_by_phone",
    "register",
    "simulate_inbound",
]
