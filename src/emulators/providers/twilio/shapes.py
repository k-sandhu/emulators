"""Wire shapes: builders that create stored records and renderers that emit them.

Stored records keep Twilio's snake_case wire fields plus ``_``-prefixed internal
fields (ISO datetimes for filtering, callback URLs, blob keys). Renderers strip the
internal fields and recompute mode-dependent absolute URLs, so the same record works
in host mode, prefix mode and per-port mode.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import Request

from emulators.core.clock import isoformat_z, parse_instant, utc
from emulators.providers.twilio.common import (
    API_HOST,
    API_VERSION,
    CONVERSATIONS_HOST,
    MESSAGING_HOST,
    VERIFY_HOST,
    absolute,
    format_from_formatted,
    iso8601,
    national_format,
    num_segments,
    rfc2822,
)

if TYPE_CHECKING:
    from emulators.core.provider import ProviderContext

Record = dict[str, Any]


def _dt(record: Record, key: str) -> datetime | None:
    value = record.get(key)
    return parse_instant(value) if value else None


# -- messages ----------------------------------------------------------------------


def message_uri(account_sid: str, sid_: str) -> str:
    return f"/{API_VERSION}/Accounts/{account_sid}/Messages/{sid_}.json"


def build_message(
    *,
    sid_: str,
    account_sid: str,
    to: str,
    from_: str,
    body: str | None,
    direction: str,
    status: str,
    created: datetime,
    sent: datetime | None = None,
    messaging_service_sid: str | None = None,
    num_media: int = 0,
    error_code: int | None = None,
    error_message: str | None = None,
    price: str | None = None,
    status_callback: str | None = None,
    status_callback_method: str = "POST",
) -> Record:
    return {
        "id": sid_,
        "sid": sid_,
        "account_sid": account_sid,
        "api_version": API_VERSION,
        "body": body if body is not None else "",  # media-only messages carry an empty string
        "date_created": rfc2822(created),
        "date_sent": rfc2822(sent),
        "date_updated": rfc2822(created),
        "direction": direction,
        "error_code": error_code,
        "error_message": error_message,
        "from": from_,
        "messaging_service_sid": messaging_service_sid,
        "num_media": str(num_media),
        "num_segments": str(max(num_segments(body or ""), 1 if num_media else 0)),
        "price": price,
        "price_unit": "USD",
        "status": status,
        "subresource_uris": {
            "media": f"/{API_VERSION}/Accounts/{account_sid}/Messages/{sid_}/Media.json",
            "feedback": f"/{API_VERSION}/Accounts/{account_sid}/Messages/{sid_}/Feedback.json",
        },
        "to": to,
        "uri": message_uri(account_sid, sid_),
        "_created": isoformat_z(utc(created)),
        "_sent": isoformat_z(utc(sent)) if sent else None,
        "_account": account_sid,
        "_status_callback": status_callback,
        "_status_callback_method": status_callback_method,
    }


def render_message(record: Record, account_sid: str | None = None) -> Record:
    out = {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}
    if account_sid and account_sid != out["account_sid"]:
        out["account_sid"] = account_sid
        out["uri"] = message_uri(account_sid, out["sid"])
        out["subresource_uris"] = {
            "media": f"/{API_VERSION}/Accounts/{account_sid}/Messages/{out['sid']}/Media.json",
            "feedback": f"/{API_VERSION}/Accounts/{account_sid}/Messages/{out['sid']}/Feedback.json",
        }
    return out


def touch(record: Record, when: datetime, rfc: bool = True) -> None:
    record["date_updated"] = rfc2822(when) if rfc else iso8601(when)


# -- media -------------------------------------------------------------------------


def build_media(
    *,
    sid_: str,
    account_sid: str,
    parent_sid: str,
    content_type: str,
    created: datetime,
    source_url: str | None = None,
    document_id: str | None = None,
) -> Record:
    return {
        "id": sid_,
        "sid": sid_,
        "account_sid": account_sid,
        "content_type": content_type,
        "date_created": rfc2822(created),
        "date_updated": rfc2822(created),
        "parent_sid": parent_sid,
        "uri": f"/{API_VERSION}/Accounts/{account_sid}/Messages/{parent_sid}/Media/{sid_}.json",
        "_created": isoformat_z(utc(created)),
        "_source_url": source_url,
        "_document_id": document_id,
        "_blob": None,
    }


def render_media(record: Record) -> Record:
    return {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}


# -- accounts ----------------------------------------------------------------------


def account_subresources(account_sid: str) -> dict[str, str]:
    base = f"/{API_VERSION}/Accounts/{account_sid}"
    return {
        "available_phone_numbers": f"{base}/AvailablePhoneNumbers.json",
        "balance": f"{base}/Balance.json",
        "calls": f"{base}/Calls.json",
        "conferences": f"{base}/Conferences.json",
        "connect_apps": f"{base}/ConnectApps.json",
        "incoming_phone_numbers": f"{base}/IncomingPhoneNumbers.json",
        "keys": f"{base}/Keys.json",
        "messages": f"{base}/Messages.json",
        "notifications": f"{base}/Notifications.json",
        "outgoing_caller_ids": f"{base}/OutgoingCallerIds.json",
        "queues": f"{base}/Queues.json",
        "recordings": f"{base}/Recordings.json",
        "signing_keys": f"{base}/SigningKeys.json",
        "sip": f"{base}/SIP.json",
        "applications": f"{base}/Applications.json",
        "short_codes": f"{base}/SMS/ShortCodes.json",
        "transcriptions": f"{base}/Transcriptions.json",
        "usage": f"{base}/Usage.json",
        "addresses": f"{base}/Addresses.json",
        "authorized_connect_apps": f"{base}/AuthorizedConnectApps.json",
        "tokens": f"{base}/Tokens.json",
        "validation_requests": f"{base}/OutgoingCallerIds.json",
    }


def build_account(
    *,
    sid_: str,
    friendly_name: str,
    auth_token: str,
    created: datetime,
    owner_sid: str | None = None,
    type_: str = "Full",
) -> Record:
    return {
        "id": sid_,
        "sid": sid_,
        "auth_token": auth_token,
        "date_created": rfc2822(created),
        "date_updated": rfc2822(created),
        "friendly_name": friendly_name,
        "owner_account_sid": owner_sid or sid_,
        "status": "active",
        "subresource_uris": account_subresources(sid_),
        "type": type_,
        "uri": f"/{API_VERSION}/Accounts/{sid_}.json",
        "_created": isoformat_z(utc(created)),
    }


def render_account(record: Record) -> Record:
    return {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}


# -- phone numbers -----------------------------------------------------------------


def build_number(
    *,
    sid_: str,
    account_sid: str,
    phone_number: str,
    friendly_name: str,
    created: datetime,
    sms: bool = True,
    mms: bool = True,
    voice: bool = True,
    origin: str = "twilio",
) -> Record:
    return {
        "id": sid_,
        "sid": sid_,
        "account_sid": account_sid,
        "address_requirements": "none",
        "address_sid": None,
        "api_version": API_VERSION,
        "beta": False,
        "bundle_sid": None,
        "capabilities": {"voice": voice, "sms": sms, "mms": mms, "fax": False},
        "date_created": rfc2822(created),
        "date_updated": rfc2822(created),
        "emergency_address_sid": None,
        "emergency_address_status": "unregistered",
        "emergency_status": "Inactive",
        "friendly_name": friendly_name,
        "identity_sid": None,
        "origin": origin,
        "phone_number": phone_number,
        "sms_application_sid": None,
        "sms_fallback_method": "POST",
        "sms_fallback_url": None,
        "sms_method": "POST",
        "sms_url": None,
        "status": "in-use",
        "status_callback": None,
        "status_callback_method": "POST",
        "trunk_sid": None,
        "uri": f"/{API_VERSION}/Accounts/{account_sid}/IncomingPhoneNumbers/{sid_}.json",
        "voice_application_sid": None,
        "voice_caller_id_lookup": False,
        "voice_fallback_method": "POST",
        "voice_fallback_url": None,
        "voice_method": "POST",
        "voice_receive_mode": "voice",
        "voice_url": None,
        "_created": isoformat_z(utc(created)),
        "_account": account_sid,
        "_type": number_type(phone_number),
    }


def number_type(phone_number: str) -> str:
    """``local`` / ``toll-free`` / ``mobile``: which ``IncomingPhoneNumbers/<Type>`` list it is in."""
    if phone_number.startswith("+1") and phone_number[2:5] in {
        "800",
        "833",
        "844",
        "855",
        "866",
        "877",
        "888",
    }:
        return "toll-free"
    if phone_number.startswith("+447"):
        return "mobile"
    return "local"


def render_number(record: Record) -> Record:
    return {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}


def available_number(phone_number: str, locality: str, region: str, country: str) -> Record:
    return {
        "friendly_name": national_format(phone_number),
        "phone_number": phone_number,
        "lata": None,
        "locality": locality or None,
        "rate_center": (locality or "").upper() or None,
        "latitude": None,
        "longitude": None,
        "region": region or None,
        "postal_code": None,
        "iso_country": country,
        "address_requirements": "none",
        "beta": False,
        "capabilities": {"voice": True, "SMS": True, "MMS": True, "fax": False},
    }


# -- calls & recordings ------------------------------------------------------------


def build_call(
    *,
    sid_: str,
    account_sid: str,
    to: str,
    from_: str,
    status: str,
    created: datetime,
    direction: str = "outbound-api",
    phone_number_sid: str | None = None,
    status_callback: str | None = None,
    status_callback_method: str = "POST",
    status_callback_events: list[str] | None = None,
    record: bool = False,
    url: str | None = None,
    twiml: str | None = None,
) -> Record:
    return {
        "id": sid_,
        "sid": sid_,
        "account_sid": account_sid,
        "annotation": None,
        "answered_by": None,
        "api_version": API_VERSION,
        "caller_name": None,
        "date_created": rfc2822(created),
        "date_updated": rfc2822(created),
        "direction": direction,
        "duration": None,
        "end_time": None,
        "forwarded_from": None,
        "from": from_,
        "from_formatted": format_from_formatted(from_),
        "group_sid": None,
        "parent_call_sid": None,
        "phone_number_sid": phone_number_sid,
        "price": None,
        "price_unit": "USD",
        "queue_time": "0",
        "start_time": None,
        "status": status,
        "subresource_uris": {
            "notifications": f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid_}/Notifications.json",
            "recordings": f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid_}/Recordings.json",
            "transcriptions": f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid_}/Transcriptions.json",
            "payments": f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid_}/Payments.json",
            "events": f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid_}/Events.json",
            "siprec": f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid_}/Siprec.json",
            "streams": f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid_}/Streams.json",
            "user_defined_message_subscriptions": f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid_}/UserDefinedMessageSubscriptions.json",
            "user_defined_messages": f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid_}/UserDefinedMessages.json",
        },
        "to": to,
        "to_formatted": national_format(to),
        "trunk_sid": None,
        "uri": f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid_}.json",
        "_created": isoformat_z(utc(created)),
        "_account": account_sid,
        "_status_callback": status_callback,
        "_status_callback_method": status_callback_method,
        "_status_callback_events": status_callback_events or ["completed"],
        "_record": record,
        "_url": url,
        "_twiml": twiml,
        "_sequence": 0,
    }


def render_call(record: Record) -> Record:
    return {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}


def build_recording(
    *,
    sid_: str,
    account_sid: str,
    call_sid: str,
    created: datetime,
    duration_s: int,
    status: str = "completed",
) -> Record:
    return {
        "id": sid_,
        "sid": sid_,
        "account_sid": account_sid,
        "api_version": API_VERSION,
        "call_sid": call_sid,
        "channels": 1,
        "conference_sid": None,
        "date_created": rfc2822(created),
        "date_updated": rfc2822(created),
        "duration": str(duration_s),
        "error_code": None,
        "price": "-0.00250",
        "price_unit": "USD",
        "source": "RecordVerb",
        "start_time": rfc2822(created),
        "status": status,
        "subresource_uris": {
            "add_on_results": f"/{API_VERSION}/Accounts/{account_sid}/Recordings/{sid_}/AddOnResults.json",
            "transcriptions": f"/{API_VERSION}/Accounts/{account_sid}/Recordings/{sid_}/Transcriptions.json",
        },
        "uri": f"/{API_VERSION}/Accounts/{account_sid}/Recordings/{sid_}.json",
        "_created": isoformat_z(utc(created)),
        "_account": account_sid,
        "_duration_s": duration_s,
        "_blob": None,
    }


def render_recording(record: Record, request: Request, ctx: ProviderContext) -> Record:
    out = {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}
    out["media_url"] = absolute(
        request, ctx, API_HOST, f"/{API_VERSION}/Accounts/{record['account_sid']}/Recordings/{record['sid']}"
    )
    out["encryption_details"] = None
    out["track"] = "both"
    return out


# -- messaging services ------------------------------------------------------------


def build_messaging_service(
    *, sid_: str, account_sid: str, friendly_name: str, created: datetime, **overrides: Any
) -> Record:
    record: Record = {
        "id": sid_,
        "sid": sid_,
        "account_sid": account_sid,
        "friendly_name": friendly_name,
        "date_created": iso8601(created),
        "date_updated": iso8601(created),
        "inbound_request_url": None,
        "inbound_method": "POST",
        "fallback_url": None,
        "fallback_method": "POST",
        "status_callback": None,
        "sticky_sender": True,
        "mms_converter": True,
        "smart_encoding": False,
        "scan_message_content": "inherit",
        "fallback_to_long_code": False,
        "area_code_geomatch": True,
        "validity_period": 14400,
        "synchronous_validation": False,
        "usecase": None,
        "us_app_to_person_registered": False,
        "use_inbound_webhook_on_number": False,
        "_created": isoformat_z(utc(created)),
        "_account": account_sid,
    }
    record.update({k: v for k, v in overrides.items() if v is not None})
    return record


def render_messaging_service(record: Record, request: Request, ctx: ProviderContext) -> Record:
    out = {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}
    base = f"/v1/Services/{record['sid']}"
    out["url"] = absolute(request, ctx, MESSAGING_HOST, base)
    out["links"] = {
        "phone_numbers": absolute(request, ctx, MESSAGING_HOST, f"{base}/PhoneNumbers"),
        "short_codes": absolute(request, ctx, MESSAGING_HOST, f"{base}/ShortCodes"),
        "alpha_senders": absolute(request, ctx, MESSAGING_HOST, f"{base}/AlphaSenders"),
        "us_app_to_person": absolute(request, ctx, MESSAGING_HOST, f"{base}/Compliance/Usa2p"),
        "us_app_to_person_usecases": absolute(
            request, ctx, MESSAGING_HOST, f"{base}/Compliance/Usa2p/Usecases"
        ),
        "channel_senders": absolute(request, ctx, MESSAGING_HOST, f"{base}/ChannelSenders"),
        "destination_alpha_senders": absolute(
            request, ctx, MESSAGING_HOST, f"{base}/DestinationAlphaSenders"
        ),
    }
    return out


def render_mg_number(link: Record, number: Record, request: Request, ctx: ProviderContext) -> Record:
    base = f"/v1/Services/{link['service_sid']}/PhoneNumbers/{link['sid']}"
    return {
        "sid": link["sid"],
        "service_sid": link["service_sid"],
        "account_sid": link["account_sid"],
        "phone_number": number["phone_number"],
        "country_code": "CA" if number["phone_number"].startswith(("+1416", "+1647")) else "US",
        "capabilities": sorted(
            {"sms": "SMS", "mms": "MMS", "voice": "Voice", "fax": "Fax"}[k]
            for k, v in number["capabilities"].items()
            if v and k in ("sms", "mms", "voice", "fax")
        ),
        "date_created": link["date_created"],
        "date_updated": link["date_updated"],
        "url": absolute(request, ctx, MESSAGING_HOST, base),
    }


# -- verify ------------------------------------------------------------------------


def build_verify_service(
    *, sid_: str, account_sid: str, friendly_name: str, created: datetime, **overrides: Any
) -> Record:
    record: Record = {
        "id": sid_,
        "sid": sid_,
        "account_sid": account_sid,
        "friendly_name": friendly_name,
        "code_length": 6,
        "lookup_enabled": False,
        "skip_sms_to_landlines": False,
        "dtmf_input_required": False,
        "tts_name": None,
        "psd2_enabled": False,
        "do_not_share_warning_enabled": False,
        "custom_code_enabled": False,
        "push": {"include_date": False, "apn_credential_sid": None, "fcm_credential_sid": None},
        "totp": {"issuer": None, "time_step": 30, "code_length": 6, "skew": 1},
        "default_template_sid": None,
        "whatsapp": {"msg_service_sid": None, "from": None},
        "passkeys": {
            "relying_party_id": None,
            "relying_party_name": None,
            "relying_party_origins": [],
            "authenticator_attachment": None,
            "discoverable_credentials": None,
            "user_verification": None,
        },
        "verify_event_subscription_enabled": False,
        "date_created": iso8601(created),
        "date_updated": iso8601(created),
        "_created": isoformat_z(utc(created)),
        "_account": account_sid,
    }
    record.update({k: v for k, v in overrides.items() if v is not None})
    return record


def render_verify_service(record: Record, request: Request, ctx: ProviderContext) -> Record:
    out = {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}
    base = f"/v2/Services/{record['sid']}"
    out["url"] = absolute(request, ctx, VERIFY_HOST, base)
    out["links"] = {
        "verifications": absolute(request, ctx, VERIFY_HOST, f"{base}/Verifications"),
        "verification_checks": absolute(request, ctx, VERIFY_HOST, f"{base}/VerificationCheck"),
        "rate_limits": absolute(request, ctx, VERIFY_HOST, f"{base}/RateLimits"),
        "messaging_configurations": absolute(request, ctx, VERIFY_HOST, f"{base}/MessagingConfigurations"),
        "entities": absolute(request, ctx, VERIFY_HOST, f"{base}/Entities"),
        "access_tokens": absolute(request, ctx, VERIFY_HOST, f"{base}/AccessTokens"),
        "webhooks": absolute(request, ctx, VERIFY_HOST, f"{base}/Webhooks"),
    }
    return out


_VERIFICATION_WIRE = (
    "sid",
    "service_sid",
    "account_sid",
    "to",
    "channel",
    "status",
    "valid",
    "lookup",
    "amount",
    "payee",
    "send_code_attempts",
    "date_created",
    "date_updated",
    "sna",
)


def render_verification(record: Record, request: Request, ctx: ProviderContext) -> Record:
    out = {k: record.get(k) for k in _VERIFICATION_WIRE}
    out["url"] = absolute(
        request, ctx, VERIFY_HOST, f"/v2/Services/{record['service_sid']}/Verifications/{record['sid']}"
    )
    return out


def render_verification_check(record: Record) -> Record:
    return {
        "sid": record["sid"],
        "service_sid": record["service_sid"],
        "account_sid": record["account_sid"],
        "to": record["to"],
        "channel": record.get("channel"),
        "status": record["status"],
        "valid": record["valid"],
        "amount": record.get("amount"),
        "payee": record.get("payee"),
        "date_created": record["date_created"],
        "date_updated": record["date_updated"],
        "sna_attempts_error_codes": [],
    }


# -- conversations -----------------------------------------------------------------


def render_conversation(record: Record, request: Request, ctx: ProviderContext) -> Record:
    out = {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}
    base = f"/v1/Conversations/{record['sid']}"
    out["url"] = absolute(request, ctx, CONVERSATIONS_HOST, base)
    out["links"] = {
        "participants": absolute(request, ctx, CONVERSATIONS_HOST, f"{base}/Participants"),
        "messages": absolute(request, ctx, CONVERSATIONS_HOST, f"{base}/Messages"),
        "webhooks": absolute(request, ctx, CONVERSATIONS_HOST, f"{base}/Webhooks"),
    }
    return out


def render_participant(record: Record, request: Request, ctx: ProviderContext) -> Record:
    out = {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}
    out["url"] = absolute(
        request,
        ctx,
        CONVERSATIONS_HOST,
        f"/v1/Conversations/{record['conversation_sid']}/Participants/{record['sid']}",
    )
    return out


def render_conv_message(record: Record, request: Request, ctx: ProviderContext) -> Record:
    out = {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}
    base = f"/v1/Conversations/{record['conversation_sid']}/Messages/{record['sid']}"
    out["url"] = absolute(request, ctx, CONVERSATIONS_HOST, base)
    out["links"] = {
        "delivery_receipts": absolute(request, ctx, CONVERSATIONS_HOST, f"{base}/DeliveryReceipts")
    }
    return out
