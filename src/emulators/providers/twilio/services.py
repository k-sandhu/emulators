"""Messaging Services (v1), Verify (v2) and Lookup (v2)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import isoformat_z, utc
from emulators.providers.twilio.common import (
    LOOKUPS_HOST,
    MESSAGING_HOST,
    VERIFY_HOST,
    absolute,
    creds,
    fail,
    form_params,
    geo_for,
    get_param,
    national_format,
    not_found,
    page_meta,
    param_value,
    parse_bool,
    parse_int,
    require_auth,
    valid_e164,
)
from emulators.providers.twilio.common import (
    sid as new_sid,
)
from emulators.providers.twilio.messages import create_outbound
from emulators.providers.twilio.shapes import (
    build_messaging_service,
    build_verify_service,
    render_messaging_service,
    render_mg_number,
    render_verification,
    render_verification_check,
    render_verify_service,
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
VERIFICATION_TTL = timedelta(minutes=10)


def _touch_iso(ctx: ProviderContext, record: Record) -> None:
    from emulators.providers.twilio.shapes import touch

    touch(record, ctx.clock.now(), rfc=False)


def _mg_or_404(ctx: ProviderContext, sid_: str) -> Record:
    record = ctx.store.collection("messaging_services").get(sid_)
    if record is None:
        raise not_found(f"/v1/Services/{sid_}")
    return record


def _vs_or_404(ctx: ProviderContext, sid_: str) -> Record:
    record = ctx.store.collection("verify_services").get(sid_)
    if record is None:
        raise not_found(f"/v2/Services/{sid_}")
    return record


# -- Lookup helpers ----------------------------------------------------------------


def _people_by_phone(ctx: ProviderContext) -> dict[str, Any]:
    book: dict[str, Any] = {}
    for person in ctx.world.people:
        if person.phone_mobile:
            book[person.phone_mobile] = (person.display_name, "mobile")
        if person.phone_business:
            book.setdefault(person.phone_business, (person.display_name, "landline"))
    for lead in ctx.world.leads:
        book.setdefault(lead.phone, (f"{lead.first_name} {lead.last_name}", "mobile"))
    for office in ctx.world.offices:
        book.setdefault(office.phone, (ctx.world.org.name, "landline"))
    book.setdefault(ctx.world.org.phone, (ctx.world.org.name, "landline"))
    return book


def lookup_phone(ctx: ProviderContext, request: Request, raw_number: str) -> Record:
    number = raw_number.strip()
    if number and not number.startswith("+") and number.isdigit():
        number = "+" + number
    fields = {f.strip().lower() for f in (request.query_params.get("Fields") or "").split(",") if f.strip()}
    valid = valid_e164(number) and number != "+15005550001"
    _, _, country = geo_for(number)
    if number.startswith("+1"):
        calling, alpha2 = "1", country or "US"
    elif number.startswith("+44"):
        calling, alpha2 = "44", "GB"
    elif number.startswith("+"):
        calling, alpha2 = number[1:3].rstrip("0") or number[1:2], country
    else:
        calling, alpha2 = "", country
    errors: list[str] | None = None
    if not valid:
        errors = ["INVALID_PHONE_NUMBER"] if number.startswith("+") else ["MISSING_COUNTRY_CODE"]
        if number == "+15005550001":
            errors = ["INVALID_PHONE_NUMBER"]
    book = _people_by_phone(ctx)
    owned = {n["phone_number"] for n in ctx.store.collection("numbers").values()}
    known_name, known_type = book.get(number, (None, None))
    if number in owned:
        known_name, known_type = (known_name or ctx.world.org.name), "nonFixedVoip"
    line_type = known_type or ("mobile" if valid else None)
    carriers = {
        "mobile": (
            "Rogers Communications Canada Inc." if alpha2 == "CA" else "T-Mobile USA, Inc.",
            "302" if alpha2 == "CA" else "310",
            "720" if alpha2 == "CA" else "260",
        ),
        "landline": ("Bell Canada" if alpha2 == "CA" else "Verizon", None, None),
        "nonFixedVoip": ("Twilio Inc.", None, None),
    }
    result: Record = {
        "calling_country_code": calling,
        "country_code": alpha2 or None,
        "phone_number": number,
        "national_format": national_format(number) if valid else number,
        "valid": valid,
        "validation_errors": errors,
        "caller_name": None,
        "sim_swap": None,
        "call_forwarding": None,
        "line_type_intelligence": None,
        "line_status": None,
        "identity_match": None,
        "reassigned_number": None,
        "sms_pumping_risk": None,
        "phone_number_quality_score": None,
        "pre_fill": None,
        "url": absolute(request, ctx, LOOKUPS_HOST, f"/v2/PhoneNumbers/{number}"),
    }
    if "caller_name" in fields:
        result["caller_name"] = {
            "caller_name": known_name.upper() if known_name else None,
            "caller_type": "CONSUMER"
            if known_name and known_name != ctx.world.org.name
            else ("BUSINESS" if known_name else None),
            "error_code": None,
        }
    if "line_type_intelligence" in fields:
        carrier, mcc, mnc = carriers.get(line_type or "mobile", carriers["mobile"])
        result["line_type_intelligence"] = {
            "carrier_name": carrier if valid else None,
            "type": line_type if valid else None,
            "mobile_country_code": mcc if line_type == "mobile" and valid else None,
            "mobile_network_code": mnc if line_type == "mobile" and valid else None,
            "error_code": None,
        }
    if "line_status" in fields:
        result["line_status"] = {"status": "active" if valid else "unknown", "error_code": None}
    if "sms_pumping_risk" in fields:
        result["sms_pumping_risk"] = {
            "sms_pumping_risk_score": 5 if valid else 100,
            "carrier_risk_category": "low" if valid else "high",
            "blocked": False,
            "error_code": None,
        }
    if "sim_swap" in fields:
        result["sim_swap"] = {"last_sim_swap": {"last_swap_date": None}, "error_code": None}
    return result


def register(app: FastAPI, ctx: ProviderContext) -> None:
    account_sid = creds(ctx)["account_sid"]

    # -- Messaging Services ------------------------------------------------------
    @app.get("/v1/Services")
    async def list_messaging_services(request: Request) -> Any:
        require_auth(request, ctx)
        rows = [
            render_messaging_service(s, request, ctx)
            for s in ctx.store.collection("messaging_services").values()
        ]
        return page_meta(request, ctx, MESSAGING_HOST, "services", rows, "/v1/Services")

    @app.post("/v1/Services")
    async def create_messaging_service(request: Request) -> Response:
        require_auth(request, ctx)
        params, _ = await form_params(request)
        friendly = get_param(params, "FriendlyName")
        if not friendly:
            raise fail(400, 21200, "FriendlyName is required.")
        record = build_messaging_service(
            sid_=new_sid(ctx, "MG", "messaging_services"),
            account_sid=account_sid,
            friendly_name=friendly,
            created=ctx.clock.now(),
        )
        _apply_mg(record, params)
        ctx.store.collection("messaging_services").put(record)
        return JSONResponse(render_messaging_service(record, request, ctx), status_code=201)

    @app.get("/v1/Services/{sid}")
    async def get_messaging_service(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        return render_messaging_service(_mg_or_404(ctx, sid), request, ctx)

    @app.post("/v1/Services/{sid}")
    async def update_messaging_service(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        record = _mg_or_404(ctx, sid)
        params, _ = await form_params(request)
        _apply_mg(record, params)
        _touch_iso(ctx, record)
        return render_messaging_service(record, request, ctx)

    @app.delete("/v1/Services/{sid}")
    async def delete_messaging_service(request: Request, sid: str) -> Response:
        require_auth(request, ctx)
        _mg_or_404(ctx, sid)
        ctx.store.collection("messaging_services").delete(sid)
        links = ctx.store.collection("mg_numbers")
        for link in [lnk for lnk in links.values() if lnk["service_sid"] == sid]:
            links.delete(link["id"])
        return Response(status_code=204)

    @app.get("/v1/Services/{sid}/PhoneNumbers")
    async def list_mg_numbers(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        _mg_or_404(ctx, sid)
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
        require_auth(request, ctx)
        _mg_or_404(ctx, sid)
        params, _ = await form_params(request)
        pn_sid = get_param(params, "PhoneNumberSid")
        number = ctx.store.collection("numbers").get(pn_sid) if pn_sid else None
        if number is None:
            raise not_found(f"/2010-04-01/Accounts/{account_sid}/IncomingPhoneNumbers/{pn_sid}.json")
        link_id = f"{sid}:{number['sid']}"
        if ctx.store.collection("mg_numbers").get(link_id) is not None:
            raise fail(400, 21200, "Phone number is already in the Messaging Service sender pool.")
        now = ctx.clock.now()
        from emulators.providers.twilio.common import iso8601 as _iso

        link = {
            "id": link_id,
            "sid": number["sid"],
            "service_sid": sid,
            "account_sid": account_sid,
            "date_created": _iso(now),
            "date_updated": _iso(now),
        }
        ctx.store.collection("mg_numbers").put(link)
        return JSONResponse(render_mg_number(link, number, request, ctx), status_code=201)

    @app.get("/v1/Services/{sid}/PhoneNumbers/{pn_sid}")
    async def get_mg_number(request: Request, sid: str, pn_sid: str) -> Any:
        require_auth(request, ctx)
        _mg_or_404(ctx, sid)
        link = ctx.store.collection("mg_numbers").get(f"{sid}:{pn_sid}")
        number = ctx.store.collection("numbers").get(pn_sid)
        if link is None or number is None:
            raise not_found(f"/v1/Services/{sid}/PhoneNumbers/{pn_sid}")
        return render_mg_number(link, number, request, ctx)

    @app.delete("/v1/Services/{sid}/PhoneNumbers/{pn_sid}")
    async def delete_mg_number(request: Request, sid: str, pn_sid: str) -> Response:
        require_auth(request, ctx)
        _mg_or_404(ctx, sid)
        if ctx.store.collection("mg_numbers").delete(f"{sid}:{pn_sid}") is None:
            raise not_found(f"/v1/Services/{sid}/PhoneNumbers/{pn_sid}")
        return Response(status_code=204)

    # -- Verify services -----------------------------------------------------------
    @app.get("/v2/Services")
    async def list_verify_services(request: Request) -> Any:
        require_auth(request, ctx)
        rows = [
            render_verify_service(s, request, ctx) for s in ctx.store.collection("verify_services").values()
        ]
        return page_meta(request, ctx, VERIFY_HOST, "services", rows, "/v2/Services")

    @app.post("/v2/Services")
    async def create_verify_service(request: Request) -> Response:
        require_auth(request, ctx)
        params, _ = await form_params(request)
        friendly = get_param(params, "FriendlyName")
        if not friendly:
            raise fail(400, 60200, "FriendlyName is required.")
        record = build_verify_service(
            sid_=new_sid(ctx, "VA", "verify_services"),
            account_sid=account_sid,
            friendly_name=friendly,
            created=ctx.clock.now(),
        )
        _apply_verify_service(record, params)
        ctx.store.collection("verify_services").put(record)
        return JSONResponse(render_verify_service(record, request, ctx), status_code=201)

    @app.get("/v2/Services/{sid}")
    async def get_verify_service(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        return render_verify_service(_vs_or_404(ctx, sid), request, ctx)

    @app.post("/v2/Services/{sid}")
    async def update_verify_service(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        record = _vs_or_404(ctx, sid)
        params, _ = await form_params(request)
        _apply_verify_service(record, params)
        _touch_iso(ctx, record)
        return render_verify_service(record, request, ctx)

    @app.delete("/v2/Services/{sid}")
    async def delete_verify_service(request: Request, sid: str) -> Response:
        require_auth(request, ctx)
        _vs_or_404(ctx, sid)
        ctx.store.collection("verify_services").delete(sid)
        return Response(status_code=204)

    # -- Verifications ---------------------------------------------------------------
    @app.post("/v2/Services/{sid}/Verifications")
    async def create_verification(request: Request, sid: str) -> Response:
        auth = require_auth(request, ctx)
        service = _vs_or_404(ctx, sid)
        params, _ = await form_params(request)
        to = get_param(params, "To")
        if not to:
            raise fail(400, 60200, "To is required.")
        channel = (get_param(params, "Channel") or "sms").lower()
        if channel not in ("sms", "call", "email", "whatsapp", "sna"):
            raise fail(400, 60200, f"Channel '{channel}' is not valid.")
        custom = get_param(params, "CustomCode")
        if custom and not service.get("custom_code_enabled"):
            raise fail(400, 60200, "Custom codes are not enabled for this Service.")
        code_length = int(service.get("code_length") or 6)
        code = custom or ctx.ids.digits(code_length, leading_nonzero=False).rjust(code_length, "0")
        now = ctx.clock.now()
        from emulators.providers.twilio.common import iso8601 as _iso

        attempt_sid = "VL" + ctx.ids.hex(32)
        record: Record = {
            "id": new_sid(ctx, "VE", "verifications"),
            "sid": "",
            "service_sid": sid,
            "account_sid": account_sid,
            "to": to,
            "channel": channel,
            "status": "pending",
            "valid": False,
            "lookup": lookup_phone(ctx, request, to)
            if service.get("lookup_enabled") and channel != "email"
            else {},
            "amount": get_param(params, "Amount"),
            "payee": get_param(params, "Payee"),
            "send_code_attempts": [{"time": _iso(now), "channel": channel, "attempt_sid": attempt_sid}],
            "date_created": _iso(now),
            "date_updated": _iso(now),
            "sna": None,
            "code": code,
            "check_attempts": 0,
            "expires_at": isoformat_z(utc(now) + VERIFICATION_TTL),
        }
        record["sid"] = record["id"]
        # Do not leak the full lookup URL internals Twilio wouldn't nest; keep lookup compact.
        if isinstance(record["lookup"], dict) and record["lookup"]:
            record["lookup"] = {
                k: record["lookup"].get(k)
                for k in (
                    "calling_country_code",
                    "country_code",
                    "phone_number",
                    "national_format",
                    "valid",
                    "validation_errors",
                )
            }
        ctx.store.collection("verifications").put(record)
        if channel in ("sms", "whatsapp") and valid_e164(to.split(":")[-1]):
            numbers = ctx.store.collection("numbers").values()
            sender = next(
                (n["phone_number"] for n in numbers if n["capabilities"].get("sms")), "+14165550188"
            )
            address = f"whatsapp:{to.split(':')[-1]}" if channel == "whatsapp" else to
            from_addr = f"whatsapp:{sender}" if channel == "whatsapp" else sender
            try:
                await create_outbound(
                    ctx,
                    account_sid=account_sid,
                    to=address,
                    from_=from_addr,
                    body=f"Your {service['friendly_name']} verification code is: {code}",
                    is_test=auth.is_test,
                )
            except Exception:
                pass  # Verification still succeeds if the SMS leg cannot be simulated.
        return JSONResponse(render_verification(record, request, ctx), status_code=201)

    @app.get("/v2/Services/{sid}/Verifications/{verification_sid}")
    async def get_verification(request: Request, sid: str, verification_sid: str) -> Any:
        require_auth(request, ctx)
        _vs_or_404(ctx, sid)
        record = _verification_or_404(ctx, sid, verification_sid)
        return render_verification(record, request, ctx)

    @app.post("/v2/Services/{sid}/Verifications/{verification_sid}")
    async def update_verification(request: Request, sid: str, verification_sid: str) -> Any:
        require_auth(request, ctx)
        _vs_or_404(ctx, sid)
        record = _verification_or_404(ctx, sid, verification_sid)
        params, _ = await form_params(request)
        status = (get_param(params, "Status") or "").lower()
        if status not in ("canceled", "approved"):
            raise fail(400, 60200, "Status must be 'canceled' or 'approved'.")
        record["status"] = status
        record["valid"] = status == "approved"
        _touch_iso(ctx, record)
        return render_verification(record, request, ctx)

    @app.post("/v2/Services/{sid}/VerificationCheck")
    async def check_verification(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        _vs_or_404(ctx, sid)
        params, _ = await form_params(request)
        code = get_param(params, "Code")
        if not code:
            raise fail(400, 60200, "Code is required.")
        verification_sid = get_param(params, "VerificationSid")
        to = get_param(params, "To")
        record = None
        if verification_sid:
            candidate = ctx.store.collection("verifications").get(verification_sid)
            if candidate is not None and candidate["service_sid"] == sid:
                record = candidate
        elif to:
            pending = [
                v
                for v in ctx.store.collection("verifications").values()
                if v["service_sid"] == sid and v["to"] == to and v["status"] == "pending"
            ]
            record = pending[-1] if pending else None
        else:
            raise fail(400, 60200, "To or VerificationSid is required.")
        if record is None or _expired(ctx, record):
            raise not_found(f"/v2/Services/{sid}/VerificationCheck")
        if record["status"] != "pending":
            return render_verification_check(record)
        if code == record["code"] or code == UNIVERSAL_CODE:
            record["status"] = "approved"
            record["valid"] = True
        else:
            record["check_attempts"] = int(record.get("check_attempts") or 0) + 1
            if record["check_attempts"] >= MAX_CHECK_ATTEMPTS:
                record["status"] = "canceled"
                _touch_iso(ctx, record)
                raise fail(
                    404, 20404, "The requested resource was not found. Max check attempts reached (60203)."
                )
        _touch_iso(ctx, record)
        return render_verification_check(record)

    # -- Lookup ----------------------------------------------------------------------
    @app.get("/v2/PhoneNumbers/{phone_number:path}")
    async def lookup(request: Request, phone_number: str) -> Any:
        require_auth(request, ctx)
        return lookup_phone(ctx, request, phone_number)


def _apply_mg(record: Record, params: dict[str, str]) -> None:
    for pascal, snake in MG_FIELDS.items():
        value = param_value(params, pascal)
        if value is not None:
            record[snake] = value or None
    for pascal, snake in MG_BOOL_FIELDS.items():
        value = param_value(params, pascal)
        if value is not None:
            record[snake] = parse_bool(value)
    validity = param_value(params, "ValidityPeriod")
    if validity is not None:
        record["validity_period"] = parse_int(validity, record["validity_period"])
    scan = param_value(params, "ScanMessageContent")
    if scan is not None:
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
        record["code_length"] = parse_int(length, record["code_length"])


def _verification_or_404(ctx: ProviderContext, service_sid: str, sid_: str) -> Record:
    record = ctx.store.collection("verifications").get(sid_)
    if record is None or record["service_sid"] != service_sid:
        raise not_found(f"/v2/Services/{service_sid}/Verifications/{sid_}")
    return record


def _expired(ctx: ProviderContext, record: Record) -> bool:
    from emulators.core.clock import parse_instant

    return bool(record.get("expires_at")) and parse_instant(record["expires_at"]) <= ctx.clock.now()


__all__ = ["lookup_phone", "register"]
