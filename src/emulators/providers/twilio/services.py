"""Messaging Services (v1) and Verify (v2); Lookup lives in :mod:`.lookup`."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import isoformat_z, parse_instant, utc
from emulators.core.errors import EmulatorError
from emulators.providers.twilio.common import (
    MESSAGING_HOST,
    VERIFY_HOST,
    AuthInfo,
    fail,
    form_params,
    get_param,
    iso8601,
    missing_param,
    not_found,
    page_meta,
    param_value,
    parse_bool,
    parse_int,
    require_auth,
    valid_e164,
    valid_url,
)
from emulators.providers.twilio.common import (
    sid as new_sid,
)
from emulators.providers.twilio.lookup import carrier_info
from emulators.providers.twilio.messages import create_outbound
from emulators.providers.twilio.shapes import (
    build_messaging_service,
    build_verify_service,
    render_messaging_service,
    render_mg_number,
    render_verification,
    render_verification_check,
    render_verify_service,
    touch,
)

if TYPE_CHECKING:
    from emulators.core.provider import ProviderContext

Record = dict[str, Any]

MG_FIELDS = {
    "FriendlyName": "friendly_name",
    "InboundRequestUrl": "inbound_request_url",
    "InboundMethod": "inbound_method",
    "FallbackUrl": "fallback_url",
    "FallbackMethod": "fallback_method",
    "StatusCallback": "status_callback",
    "Usecase": "usecase",
}
MG_BOOL_FIELDS = {
    "StickySender": "sticky_sender",
    "MmsConverter": "mms_converter",
    "SmartEncoding": "smart_encoding",
    "FallbackToLongCode": "fallback_to_long_code",
    "AreaCodeGeomatch": "area_code_geomatch",
    "SynchronousValidation": "synchronous_validation",
    "UseInboundWebhookOnNumber": "use_inbound_webhook_on_number",
}

VERIFY_FIELDS = {
    "FriendlyName": "friendly_name",
    "TtsName": "tts_name",
    "DefaultTemplateSid": "default_template_sid",
}
VERIFY_BOOL_FIELDS = {
    "LookupEnabled": "lookup_enabled",
    "SkipSmsToLandlines": "skip_sms_to_landlines",
    "DtmfInputRequired": "dtmf_input_required",
    "Psd2Enabled": "psd2_enabled",
    "DoNotShareWarningEnabled": "do_not_share_warning_enabled",
    "CustomCodeEnabled": "custom_code_enabled",
    "VerifyEventSubscriptionEnabled": "verify_event_subscription_enabled",
}

UNIVERSAL_CODE = "123456"
MAX_CHECK_ATTEMPTS = 5
MAX_SEND_ATTEMPTS = 5
VERIFICATION_TTL = timedelta(minutes=10)
VERIFY_CHANNELS = ("sms", "call", "email", "whatsapp", "sna")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _touch_iso(ctx: ProviderContext, record: Record) -> None:
    touch(record, ctx.clock.now(), rfc=False)


def _mg_or_404(ctx: ProviderContext, auth: AuthInfo, sid_: str) -> Record:
    record = ctx.store.collection("messaging_services").get(sid_)
    if record is None or record["account_sid"] != auth.account_sid:
        raise not_found(f"/v1/Services/{sid_}")
    return record


def _vs_or_404(ctx: ProviderContext, auth: AuthInfo, sid_: str) -> Record:
    record = ctx.store.collection("verify_services").get(sid_)
    if record is None or record["account_sid"] != auth.account_sid:
        raise not_found(f"/v2/Services/{sid_}")
    return record


def invalid_param(name: str, value: object, *, code: int = 20001) -> EmulatorError:
    """Verify (60200) / platform (20001) "this parameter is wrong" error."""
    return fail(400, code, f"Invalid parameter `{name}`: {value}")


def register(app: FastAPI, ctx: ProviderContext) -> None:
    # -- Messaging Services ------------------------------------------------------
    @app.get("/v1/Services")
    async def list_messaging_services(request: Request) -> Any:
        auth = require_auth(request, ctx)
        rows = [
            render_messaging_service(s, request, ctx)
            for s in ctx.store.collection("messaging_services").values()
            if s["account_sid"] == auth.account_sid
        ]
        return page_meta(request, ctx, MESSAGING_HOST, "services", rows, "/v1/Services")

    @app.post("/v1/Services")
    async def create_messaging_service(request: Request) -> Response:
        auth = require_auth(request, ctx)
        params, _ = await form_params(request)
        friendly = get_param(params, "FriendlyName")
        if not friendly:
            raise missing_param("FriendlyName")
        record = build_messaging_service(
            sid_=new_sid(ctx, "MG", "messaging_services"),
            account_sid=auth.account_sid,
            friendly_name=friendly,
            created=ctx.clock.now(),
        )
        _apply_mg(record, params)
        ctx.store.collection("messaging_services").put(record)
        return JSONResponse(render_messaging_service(record, request, ctx), status_code=201)

    @app.get("/v1/Services/{sid}")
    async def get_messaging_service(request: Request, sid: str) -> Any:
        auth = require_auth(request, ctx)
        return render_messaging_service(_mg_or_404(ctx, auth, sid), request, ctx)

    @app.post("/v1/Services/{sid}")
    async def update_messaging_service(request: Request, sid: str) -> Any:
        auth = require_auth(request, ctx)
        record = _mg_or_404(ctx, auth, sid)
        params, _ = await form_params(request)
        _apply_mg(record, params)
        _touch_iso(ctx, record)
        return render_messaging_service(record, request, ctx)

    @app.delete("/v1/Services/{sid}")
    async def delete_messaging_service(request: Request, sid: str) -> Response:
        auth = require_auth(request, ctx)
        _mg_or_404(ctx, auth, sid)
        ctx.store.collection("messaging_services").delete(sid)
        links = ctx.store.collection("mg_numbers")
        for link in [lnk for lnk in links.values() if lnk["service_sid"] == sid]:
            links.delete(link["id"])
        return Response(status_code=204)

    @app.get("/v1/Services/{sid}/PhoneNumbers")
    async def list_mg_numbers(request: Request, sid: str) -> Any:
        auth = require_auth(request, ctx)
        _mg_or_404(ctx, auth, sid)
        rows = []
        for link in ctx.store.collection("mg_numbers").values():
            if link["service_sid"] != sid:
                continue
            number = ctx.store.collection("numbers").get(link["sid"])
            if number is not None:
                rows.append(render_mg_number(link, number, request, ctx))
        return page_meta(
            request, ctx, MESSAGING_HOST, "phone_numbers", rows, f"/v1/Services/{sid}/PhoneNumbers"
        )

    @app.post("/v1/Services/{sid}/PhoneNumbers")
    async def add_mg_number(request: Request, sid: str) -> Response:
        auth = require_auth(request, ctx)
        _mg_or_404(ctx, auth, sid)
        params, _ = await form_params(request)
        pn_sid = get_param(params, "PhoneNumberSid")
        if not pn_sid:
            raise missing_param("PhoneNumberSid")
        number = ctx.store.collection("numbers").get(pn_sid)
        if number is None or number.get("_account") != auth.account_sid:
            raise not_found(f"/2010-04-01/Accounts/{auth.account_sid}/IncomingPhoneNumbers/{pn_sid}.json")
        links = ctx.store.collection("mg_numbers")
        if links.get(f"{sid}:{number['sid']}") is not None:
            raise fail(409, 21710, "Phone Number is already in the Messaging Service.")
        if any(lnk["sid"] == number["sid"] for lnk in links.values()):
            raise fail(409, 21712, "Phone Number is already in another Messaging Service.")
        now = iso8601(ctx.clock.now())
        link = {
            "id": f"{sid}:{number['sid']}",
            "sid": number["sid"],
            "service_sid": sid,
            "account_sid": auth.account_sid,
            "date_created": now,
            "date_updated": now,
        }
        links.put(link)
        return JSONResponse(render_mg_number(link, number, request, ctx), status_code=201)

    @app.get("/v1/Services/{sid}/PhoneNumbers/{pn_sid}")
    async def get_mg_number(request: Request, sid: str, pn_sid: str) -> Any:
        auth = require_auth(request, ctx)
        _mg_or_404(ctx, auth, sid)
        link = ctx.store.collection("mg_numbers").get(f"{sid}:{pn_sid}")
        number = ctx.store.collection("numbers").get(pn_sid)
        if link is None or number is None:
            raise not_found(f"/v1/Services/{sid}/PhoneNumbers/{pn_sid}")
        return render_mg_number(link, number, request, ctx)

    @app.delete("/v1/Services/{sid}/PhoneNumbers/{pn_sid}")
    async def delete_mg_number(request: Request, sid: str, pn_sid: str) -> Response:
        auth = require_auth(request, ctx)
        _mg_or_404(ctx, auth, sid)
        if ctx.store.collection("mg_numbers").delete(f"{sid}:{pn_sid}") is None:
            raise not_found(f"/v1/Services/{sid}/PhoneNumbers/{pn_sid}")
        return Response(status_code=204)

    # -- Verify services -----------------------------------------------------------
    @app.get("/v2/Services")
    async def list_verify_services(request: Request) -> Any:
        auth = require_auth(request, ctx)
        rows = [
            render_verify_service(s, request, ctx)
            for s in ctx.store.collection("verify_services").values()
            if s["account_sid"] == auth.account_sid
        ]
        return page_meta(request, ctx, VERIFY_HOST, "services", rows, "/v2/Services")

    @app.post("/v2/Services")
    async def create_verify_service(request: Request) -> Response:
        auth = require_auth(request, ctx)
        params, _ = await form_params(request)
        friendly = get_param(params, "FriendlyName")
        if not friendly:
            raise missing_param("FriendlyName")
        record = build_verify_service(
            sid_=new_sid(ctx, "VA", "verify_services"),
            account_sid=auth.account_sid,
            friendly_name=friendly,
            created=ctx.clock.now(),
        )
        _apply_verify_service(record, params)
        ctx.store.collection("verify_services").put(record)
        return JSONResponse(render_verify_service(record, request, ctx), status_code=201)

    @app.get("/v2/Services/{sid}")
    async def get_verify_service(request: Request, sid: str) -> Any:
        auth = require_auth(request, ctx)
        return render_verify_service(_vs_or_404(ctx, auth, sid), request, ctx)

    @app.post("/v2/Services/{sid}")
    async def update_verify_service(request: Request, sid: str) -> Any:
        auth = require_auth(request, ctx)
        record = _vs_or_404(ctx, auth, sid)
        params, _ = await form_params(request)
        _apply_verify_service(record, params)
        _touch_iso(ctx, record)
        return render_verify_service(record, request, ctx)

    @app.delete("/v2/Services/{sid}")
    async def delete_verify_service(request: Request, sid: str) -> Response:
        auth = require_auth(request, ctx)
        _vs_or_404(ctx, auth, sid)
        ctx.store.collection("verify_services").delete(sid)
        return Response(status_code=204)

    # -- Verifications ---------------------------------------------------------------
    @app.post("/v2/Services/{sid}/Verifications")
    async def create_verification(request: Request, sid: str) -> Response:
        auth = require_auth(request, ctx)
        service = _vs_or_404(ctx, auth, sid)
        params, _ = await form_params(request)
        to = get_param(params, "To")
        if not to:
            raise missing_param("To")
        channel_raw = get_param(params, "Channel")
        if not channel_raw:
            raise missing_param("Channel")
        channel = channel_raw.lower()
        if channel not in VERIFY_CHANNELS:
            raise invalid_param("Channel", channel_raw, code=60200)
        if channel == "email":
            if not _EMAIL_RE.match(to):
                raise invalid_param("To", to, code=60200)
        elif not valid_e164(to.removeprefix("whatsapp:")):
            raise invalid_param("To", to, code=60200)
        custom = get_param(params, "CustomCode")
        if custom:
            if not service.get("custom_code_enabled"):
                raise invalid_param("CustomCode", custom, code=60200)
            if not re.fullmatch(r"\d{4,10}", custom):
                raise invalid_param("CustomCode", custom, code=60200)
        code_length = int(service.get("code_length") or 6)
        code = custom or ctx.ids.digits(code_length, leading_nonzero=False).rjust(code_length, "0")
        now = ctx.clock.now()
        key = to.removeprefix("whatsapp:")
        attempt = {"time": iso8601(now), "channel": channel, "attempt_sid": "VL" + ctx.ids.hex(32)}

        # A second start for the same recipient while one is pending re-sends on the SAME
        # verification (new code, one more send attempt) until the send limit is hit.
        record = next(
            (
                v
                for v in reversed(ctx.store.collection("verifications").values())
                if v["service_sid"] == sid and v["_to"] == key and _live(ctx, v)
            ),
            None,
        )
        if record is not None:
            if len(record["send_code_attempts"]) >= MAX_SEND_ATTEMPTS:
                raise fail(429, 60203, "Max send attempts reached")
            record["send_code_attempts"].append(attempt)
            record["channel"] = channel
            record["code"] = code
            record["expires_at"] = isoformat_z(utc(now) + VERIFICATION_TTL)
            record["date_updated"] = iso8601(now)
        else:
            sid_ = new_sid(ctx, "VE", "verifications")
            record = {
                "id": sid_,
                "sid": sid_,
                "service_sid": sid,
                "account_sid": auth.account_sid,
                "to": to,
                "channel": channel,
                "status": "pending",
                "valid": False,
                "lookup": carrier_info(ctx, key) if channel != "email" else {},
                "amount": get_param(params, "Amount"),
                "payee": get_param(params, "Payee"),
                "send_code_attempts": [attempt],
                "date_created": iso8601(now),
                "date_updated": iso8601(now),
                "sna": None,
                "code": code,
                "check_attempts": 0,
                "expires_at": isoformat_z(utc(now) + VERIFICATION_TTL),
                "_to": key,
            }
            ctx.store.collection("verifications").put(record)
        if channel in ("sms", "whatsapp"):
            await _send_code_message(
                ctx, service, key, channel, code, account_sid=auth.account_sid, is_test=auth.is_test
            )
        return JSONResponse(render_verification(record, request, ctx), status_code=201)

    @app.get("/v2/Services/{sid}/Verifications/{verification_sid}")
    async def get_verification(request: Request, sid: str, verification_sid: str) -> Any:
        auth = require_auth(request, ctx)
        _vs_or_404(ctx, auth, sid)
        record = _verification_or_404(ctx, sid, verification_sid)
        _expire(ctx, record)
        return render_verification(record, request, ctx)

    @app.post("/v2/Services/{sid}/Verifications/{verification_sid}")
    async def update_verification(request: Request, sid: str, verification_sid: str) -> Any:
        auth = require_auth(request, ctx)
        _vs_or_404(ctx, auth, sid)
        record = _verification_or_404(ctx, sid, verification_sid)
        params, _ = await form_params(request)
        status_raw = get_param(params, "Status")
        if not status_raw:
            raise missing_param("Status")
        status = status_raw.lower()
        if status not in ("canceled", "approved"):
            raise invalid_param("Status", status_raw, code=60200)
        _expire(ctx, record)
        if record["status"] != "pending":
            raise not_found(f"/v2/Services/{sid}/Verifications/{verification_sid}")
        record["status"] = status
        record["valid"] = status == "approved"
        _touch_iso(ctx, record)
        return render_verification(record, request, ctx)

    @app.post("/v2/Services/{sid}/VerificationCheck")
    async def check_verification(request: Request, sid: str) -> Any:
        auth = require_auth(request, ctx)
        _vs_or_404(ctx, auth, sid)
        params, _ = await form_params(request)
        code = get_param(params, "Code")
        if not code:
            raise missing_param("Code")
        if not re.fullmatch(r"\d{4,10}", code):
            raise invalid_param("Code", code, code=60200)
        verification_sid = get_param(params, "VerificationSid")
        to = get_param(params, "To")
        record = None
        if verification_sid:
            candidate = ctx.store.collection("verifications").get(verification_sid)
            if candidate is not None and candidate["service_sid"] == sid:
                record = candidate
        elif to:
            key = to.removeprefix("whatsapp:")
            pending = [
                v
                for v in ctx.store.collection("verifications").values()
                if v["service_sid"] == sid and v["_to"] == key and _live(ctx, v)
            ]
            record = pending[-1] if pending else None
        else:
            raise fail(400, 60200, "Parameter `To` or `VerificationSid` is required")
        if record is not None:
            _expire(ctx, record)
        if record is None or record["status"] != "pending":
            # Unknown, expired, canceled and already-approved verifications all 404 for checks.
            raise not_found(f"/v2/Services/{sid}/VerificationCheck")
        if int(record.get("check_attempts") or 0) >= MAX_CHECK_ATTEMPTS:
            record["status"] = "max_attempts_reached"
            _touch_iso(ctx, record)
            raise fail(429, 60202, "Max check attempts reached")
        if code == record["code"] or code == UNIVERSAL_CODE:
            record["status"] = "approved"
            record["valid"] = True
        else:
            record["check_attempts"] = int(record.get("check_attempts") or 0) + 1
        _touch_iso(ctx, record)
        return render_verification_check(record)


async def _send_code_message(
    ctx: ProviderContext,
    service: Record,
    to: str,
    channel: str,
    code: str,
    *,
    account_sid: str,
    is_test: bool,
) -> None:
    """The SMS/WhatsApp leg of a verification, visible in Messages like any other send."""
    numbers = [n for n in ctx.store.collection("numbers").values() if n.get("_account") == account_sid]
    if channel == "whatsapp":
        sender = next((n["phone_number"] for n in numbers if n.get("_whatsapp")), None)
    else:
        sender = next(
            (n["phone_number"] for n in numbers if n["capabilities"].get("sms") and not n.get("_whatsapp")),
            None,
        )
    if sender is None:
        return
    prefix = "whatsapp:" if channel == "whatsapp" else ""
    try:
        await create_outbound(
            ctx,
            account_sid=account_sid,
            to=f"{prefix}{to}",
            from_=f"{prefix}{sender}",
            body=f"Your {service['friendly_name']} verification code is: {code}",
            is_test=is_test,
        )
    except EmulatorError:
        pass  # The verification itself still exists if the message leg is rejected (e.g. opted out).


def _apply_mg(record: Record, params: dict[str, str]) -> None:
    for pascal, snake in MG_FIELDS.items():
        value = param_value(params, pascal)
        if value is None:
            continue
        is_url = snake in ("inbound_request_url", "fallback_url", "status_callback")
        if is_url and value and not valid_url(value):
            raise invalid_param(pascal, value)
        if snake.endswith("_method"):
            if value and value.upper() not in ("GET", "POST"):
                raise invalid_param(pascal, value)
            record[snake] = value.upper() or "POST"
            continue
        record[snake] = value or None
    for pascal, snake in MG_BOOL_FIELDS.items():
        value = param_value(params, pascal)
        if value is not None:
            record[snake] = parse_bool(value)
    validity = param_value(params, "ValidityPeriod")
    if validity is not None:
        seconds = parse_int(validity, 0)
        if not 1 <= seconds <= 14400:
            raise invalid_param("ValidityPeriod", validity)
        record["validity_period"] = seconds
    scan = param_value(params, "ScanMessageContent")
    if scan is not None:
        if scan not in ("inherit", "enable", "disable"):
            raise invalid_param("ScanMessageContent", scan)
        record["scan_message_content"] = scan


def _apply_verify_service(record: Record, params: dict[str, str]) -> None:
    for pascal, snake in VERIFY_FIELDS.items():
        value = param_value(params, pascal)
        if value is not None:
            record[snake] = value or None
    for pascal, snake in VERIFY_BOOL_FIELDS.items():
        value = param_value(params, pascal)
        if value is not None:
            record[snake] = parse_bool(value)
    length = param_value(params, "CodeLength")
    if length is not None:
        digits = parse_int(length, 0)
        if not 4 <= digits <= 10:
            raise invalid_param("CodeLength", length, code=60200)
        record["code_length"] = digits


def _verification_or_404(ctx: ProviderContext, service_sid: str, sid_: str) -> Record:
    record = ctx.store.collection("verifications").get(sid_)
    if record is None or record["service_sid"] != service_sid:
        raise not_found(f"/v2/Services/{service_sid}/Verifications/{sid_}")
    return record


def _expired(ctx: ProviderContext, record: Record) -> bool:
    return bool(record.get("expires_at")) and parse_instant(record["expires_at"]) <= ctx.clock.now()


def _expire(ctx: ProviderContext, record: Record) -> None:
    """Pending verifications become ``expired`` once their 10 minutes are up."""
    if record["status"] == "pending" and _expired(ctx, record):
        record["status"] = "expired"
        _touch_iso(ctx, record)


def _live(ctx: ProviderContext, record: Record) -> bool:
    return record["status"] == "pending" and not _expired(ctx, record)


__all__ = ["register"]
