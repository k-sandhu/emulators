"""Accounts, IncomingPhoneNumbers, AvailablePhoneNumbers, Balance, Usage and API Keys."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.providers.twilio.common import (
    API_VERSION,
    check_account,
    creds,
    fail,
    form_params,
    geo_for,
    get_param,
    not_found,
    page_2010,
    param_value,
    parse_bool,
    require_auth,
    rfc2822,
    valid_e164,
)
from emulators.providers.twilio.shapes import (
    available_number,
    build_account,
    build_number,
    render_account,
    render_number,
    touch,
)

if TYPE_CHECKING:
    from emulators.core.provider import ProviderContext

Record = dict[str, Any]

# Area codes offered for purchase per country (all numbers are fictional 555-01XX).
_AREA_CITIES: dict[str, list[tuple[str, str, str]]] = {
    "US": [
        ("212", "New York", "NY"),
        ("415", "San Francisco", "CA"),
        ("312", "Chicago", "IL"),
        ("512", "Austin", "TX"),
    ],
    "CA": [
        ("416", "Toronto", "ON"),
        ("604", "Vancouver", "BC"),
        ("403", "Calgary", "AB"),
        ("902", "Halifax", "NS"),
    ],
    "GB": [("20", "London", "ENG")],
}

_NUMBER_FIELDS = {
    "FriendlyName": "friendly_name",
    "PhoneNumber": "phone_number",
    "SmsUrl": "sms_url",
    "SmsMethod": "sms_method",
    "SmsFallbackUrl": "sms_fallback_url",
    "SmsFallbackMethod": "sms_fallback_method",
    "SmsApplicationSid": "sms_application_sid",
    "VoiceUrl": "voice_url",
    "VoiceMethod": "voice_method",
    "VoiceFallbackUrl": "voice_fallback_url",
    "VoiceFallbackMethod": "voice_fallback_method",
    "VoiceApplicationSid": "voice_application_sid",
    "VoiceReceiveMode": "voice_receive_mode",
    "StatusCallback": "status_callback",
    "StatusCallbackMethod": "status_callback_method",
    "ApiVersion": "api_version",
    "AddressSid": "address_sid",
    "BundleSid": "bundle_sid",
    "IdentitySid": "identity_sid",
    "TrunkSid": "trunk_sid",
    "EmergencyAddressSid": "emergency_address_sid",
    "EmergencyStatus": "emergency_status",
}


def _number_or_404(ctx: ProviderContext, account: str, sid_: str) -> Record:
    record = ctx.store.collection("numbers").get(sid_)
    if record is None or record.get("_account") != account:
        raise not_found(f"/{API_VERSION}/Accounts/{account}/IncomingPhoneNumbers/{sid_}.json")
    return record


def _apply_number_params(record: Record, params: dict[str, str]) -> None:
    for pascal, snake in _NUMBER_FIELDS.items():
        value = param_value(params, pascal)
        if value is not None:
            record[snake] = value or None
    caller_lookup = param_value(params, "VoiceCallerIdLookup")
    if caller_lookup is not None:
        record["voice_caller_id_lookup"] = parse_bool(caller_lookup)


def available_numbers(ctx: ProviderContext, country: str, query: Any) -> list[Record]:
    country = country.upper()
    areas = _AREA_CITIES.get(country, _AREA_CITIES["US"])
    wanted_area = query.get("AreaCode")
    contains = (query.get("Contains") or "").replace("*", "")
    sms_only = parse_bool(query.get("SmsEnabled")) if query.get("SmsEnabled") else False
    out: list[Record] = []
    for area, city, region in areas:
        if wanted_area and area != wanted_area:
            continue
        for i in range(10, 30):
            phone = f"+4420794601{i:02d}" if country == "GB" else f"+1{area}55501{i:02d}"
            if contains and contains not in phone:
                continue
            out.append(available_number(phone, city, region, country))
    if sms_only:  # Every generated number is SMS-capable.
        pass
    return out


def register(app: FastAPI, ctx: ProviderContext) -> None:
    accounts_base = f"/{API_VERSION}/Accounts"

    # -- accounts ------------------------------------------------------------
    @app.get(accounts_base + ".json")
    async def list_accounts(request: Request) -> Any:
        require_auth(request, ctx)
        records = ctx.store.collection("accounts").values()
        query = request.query_params
        if query.get("FriendlyName"):
            records = [a for a in records if a["friendly_name"] == query["FriendlyName"]]
        if query.get("Status"):
            records = [a for a in records if a["status"] == query["Status"].lower()]
        return page_2010(request, "accounts", [render_account(a) for a in records], f"{accounts_base}.json")

    @app.post(accounts_base + ".json")
    async def create_account(request: Request) -> Response:
        require_auth(request, ctx)
        params, _ = await form_params(request)
        c = creds(ctx)
        sid_ = "AC" + ctx.ids.hex(32)
        record = build_account(
            sid_=sid_,
            friendly_name=get_param(params, "FriendlyName") or "",
            auth_token=ctx.ids.hex(32),
            created=ctx.clock.now(),
            owner_sid=c["account_sid"],
        )
        ctx.store.collection("accounts").put(record)
        return JSONResponse(render_account(record), status_code=201)

    @app.get(accounts_base + "/{sid}.json")
    async def get_account(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, sid)
        record = ctx.store.collection("accounts").get(account)
        if record is None:
            raise not_found(f"{accounts_base}/{sid}.json")
        rendered = render_account(record)
        if sid == creds(ctx)["test_account_sid"]:
            rendered = {**rendered, "sid": sid, "auth_token": creds(ctx)["test_auth_token"]}
        return rendered

    @app.post(accounts_base + "/{sid}.json")
    async def update_account(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, sid)
        record = ctx.store.collection("accounts").get(account)
        if record is None:
            raise not_found(f"{accounts_base}/{sid}.json")
        params, _ = await form_params(request)
        friendly = param_value(params, "FriendlyName")
        if friendly is not None:
            record["friendly_name"] = friendly
        status = get_param(params, "Status")
        if status:
            record["status"] = status.lower()
        touch(record, ctx.clock.now())
        return render_account(record)

    @app.get(accounts_base + "/{account_sid}/Balance.json")
    async def get_balance(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        check_account(ctx, account_sid)
        return {
            "account_sid": account_sid,
            "balance": ctx.store.meta.get("balance", "152.34"),
            "currency": "USD",
        }

    @app.get(accounts_base + "/{account_sid}/Usage/Records.json")
    async def usage_records(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        now = ctx.clock.now()
        start = request.query_params.get("StartDate") or now.strftime("%Y-%m-01")
        end = request.query_params.get("EndDate") or now.strftime("%Y-%m-%d")
        messages = [m for m in ctx.store.collection("messages").values() if m.get("_account") == account]
        calls = [c for c in ctx.store.collection("calls").values() if c.get("_account") == account]
        segments = sum(int(m.get("num_segments") or 0) for m in messages)
        minutes = sum(int(c.get("duration") or 0) for c in calls) // 60
        base = f"{accounts_base}/{account_sid}/Usage/Records.json"
        records = [
            {
                "account_sid": account_sid,
                "api_version": API_VERSION,
                "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "category": "sms",
                "count": str(len(messages)),
                "count_unit": "messages",
                "description": "Total SMS",
                "end_date": end,
                "price": f"{0.0079 * segments:.5f}",
                "price_unit": "usd",
                "start_date": start,
                "subresource_uris": {},
                "uri": base,
                "usage": str(segments),
                "usage_unit": "segments",
            },
            {
                "account_sid": account_sid,
                "api_version": API_VERSION,
                "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "category": "calls",
                "count": str(len(calls)),
                "count_unit": "calls",
                "description": "Total Calls",
                "end_date": end,
                "price": f"{0.014 * max(minutes, 0):.5f}",
                "price_unit": "usd",
                "start_date": start,
                "subresource_uris": {},
                "uri": base,
                "usage": str(minutes),
                "usage_unit": "minutes",
            },
            {
                "account_sid": account_sid,
                "api_version": API_VERSION,
                "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "category": "totalprice",
                "count": None,
                "count_unit": "",
                "description": "Total Price",
                "end_date": end,
                "price": f"{0.0079 * segments + 0.014 * max(minutes, 0):.5f}",
                "price_unit": "usd",
                "start_date": start,
                "subresource_uris": {},
                "uri": base,
                "usage": f"{0.0079 * segments + 0.014 * max(minutes, 0):.5f}",
                "usage_unit": "dollars",
            },
        ]
        category = request.query_params.get("Category")
        if category:
            records = [r for r in records if r["category"] == category]
        return page_2010(request, "usage_records", records, base)

    # -- API keys --------------------------------------------------------------
    @app.post(accounts_base + "/{account_sid}/Keys.json")
    async def create_key(request: Request, account_sid: str) -> Response:
        require_auth(request, ctx)
        check_account(ctx, account_sid)
        params, _ = await form_params(request)
        sid_ = "SK" + ctx.ids.hex(32)
        secret = ctx.ids.hex(32)
        now = ctx.clock.now()
        record = {
            "id": sid_,
            "sid": sid_,
            "friendly_name": get_param(params, "FriendlyName") or "",
            "secret": secret,
            "date_created": rfc2822(now),
            "date_updated": rfc2822(now),
            "uri": f"{accounts_base}/{account_sid}/Keys/{sid_}.json",
        }
        ctx.store.collection("api_keys").put(record)
        return JSONResponse(record, status_code=201)

    @app.get(accounts_base + "/{account_sid}/Keys.json")
    async def list_keys(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        check_account(ctx, account_sid)
        rows = [
            {k: v for k, v in k_.items() if k not in ("id", "secret")}
            for k_ in ctx.store.collection("api_keys").values()
        ]
        return page_2010(request, "keys", rows, f"{accounts_base}/{account_sid}/Keys.json")

    @app.get(accounts_base + "/{account_sid}/Keys/{sid}.json")
    async def get_key(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        check_account(ctx, account_sid)
        record = ctx.store.collection("api_keys").get(sid)
        if record is None:
            raise not_found(f"{accounts_base}/{account_sid}/Keys/{sid}.json")
        return {k: v for k, v in record.items() if k not in ("id", "secret")}

    @app.delete(accounts_base + "/{account_sid}/Keys/{sid}.json")
    async def delete_key(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        check_account(ctx, account_sid)
        if ctx.store.collection("api_keys").delete(sid) is None:
            raise not_found(f"{accounts_base}/{account_sid}/Keys/{sid}.json")
        return Response(status_code=204)

    # NewKeys / SigningKeys aliases used by newer SDKs for key creation.
    @app.post(accounts_base + "/{account_sid}/SigningKeys.json")
    async def create_signing_key(request: Request, account_sid: str) -> Response:
        return await create_key(request, account_sid)

    # -- incoming phone numbers --------------------------------------------------
    numbers_base = accounts_base + "/{account_sid}/IncomingPhoneNumbers"

    @app.get(numbers_base + ".json")
    async def list_numbers(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        records = [n for n in ctx.store.collection("numbers").values() if n.get("_account") == account]
        query = request.query_params
        if query.get("PhoneNumber"):
            records = [n for n in records if n["phone_number"] == query["PhoneNumber"]]
        if query.get("FriendlyName"):
            records = [n for n in records if n["friendly_name"] == query["FriendlyName"]]
        if query.get("Beta") is not None:
            records = [n for n in records if n["beta"] == parse_bool(query["Beta"])]
        uri = f"{accounts_base}/{account_sid}/IncomingPhoneNumbers.json"
        return page_2010(request, "incoming_phone_numbers", [render_number(n) for n in records], uri)

    @app.post(numbers_base + ".json")
    async def purchase_number(request: Request, account_sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        params, _ = await form_params(request)
        phone = get_param(params, "PhoneNumber")
        area = get_param(params, "AreaCode")
        if not phone and area:
            country = "CA" if geo_for(f"+1{area}5550100")[2] == "CA" else "US"
            candidates = available_numbers(ctx, country, {"AreaCode": area})
            owned = {n["phone_number"] for n in ctx.store.collection("numbers").values()}
            phone = next((c["phone_number"] for c in candidates if c["phone_number"] not in owned), None)
        if not phone:
            raise fail(400, 21200, "PhoneNumber or AreaCode is required to purchase a number.")
        if not valid_e164(phone):
            raise fail(400, 21211, f"The 'PhoneNumber' number {phone} is not a valid phone number.")
        existing = ctx.store.collection("numbers").first(phone_number=phone)
        if existing is not None:
            raise fail(400, 21452, f"Phone number {phone} is already in use on this account.")
        record = build_number(
            sid_="PN" + ctx.ids.hex(32),
            account_sid=account,
            phone_number=phone,
            friendly_name=get_param(params, "FriendlyName") or phone,
            created=ctx.clock.now(),
        )
        _apply_number_params(record, params)
        ctx.store.collection("numbers").put(record)
        return JSONResponse(render_number(record), status_code=201)

    @app.get(numbers_base + "/{sid}.json")
    async def get_number(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        return render_number(_number_or_404(ctx, account, sid))

    @app.post(numbers_base + "/{sid}.json")
    async def update_number(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        record = _number_or_404(ctx, account, sid)
        params, _ = await form_params(request)
        _apply_number_params(record, params)
        status = get_param(params, "Status")
        if status:
            record["status"] = status
        touch(record, ctx.clock.now())
        return render_number(record)

    @app.delete(numbers_base + "/{sid}.json")
    async def delete_number(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        _number_or_404(ctx, account, sid)
        ctx.store.collection("numbers").delete(sid)
        links = ctx.store.collection("mg_numbers")
        for link in [lnk for lnk in links.values() if lnk["sid"] == sid]:
            links.delete(link["id"])
        return Response(status_code=204)

    # Typed sub-collections (Local/TollFree/Mobile) behave like the flat list.
    for kind in ("Local", "TollFree", "Mobile"):

        @app.get(numbers_base + f"/{kind}.json")
        async def list_typed_numbers(request: Request, account_sid: str, _kind: str = kind) -> Any:
            return await list_numbers(request, account_sid)

    # -- available phone numbers -------------------------------------------------
    avail_base = accounts_base + "/{account_sid}/AvailablePhoneNumbers"

    @app.get(avail_base + ".json")
    async def list_countries(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        check_account(ctx, account_sid)
        rows = [
            {
                "country_code": code,
                "country": name,
                "uri": f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{code}.json",
                "beta": False,
                "subresource_uris": {
                    "local": f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{code}/Local.json",
                    "toll_free": f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{code}/TollFree.json",
                    "mobile": f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{code}/Mobile.json",
                },
            }
            for code, name in (("US", "United States"), ("CA", "Canada"), ("GB", "United Kingdom"))
        ]
        return page_2010(
            request, "countries", rows, f"{accounts_base}/{account_sid}/AvailablePhoneNumbers.json"
        )

    @app.get(avail_base + "/{country}.json")
    async def get_country(request: Request, account_sid: str, country: str) -> Any:
        require_auth(request, ctx)
        check_account(ctx, account_sid)
        names = {"US": "United States", "CA": "Canada", "GB": "United Kingdom"}
        code = country.upper()
        if code not in names:
            raise not_found(f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{country}.json")
        return {
            "country_code": code,
            "country": names[code],
            "uri": f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{code}.json",
            "beta": False,
            "subresource_uris": {
                "local": f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{code}/Local.json",
                "toll_free": f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{code}/TollFree.json",
                "mobile": f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{code}/Mobile.json",
            },
        }

    for kind in ("Local", "TollFree", "Mobile", "National", "Voip", "MachineToMachine", "SharedCost"):

        @app.get(avail_base + "/{country}/" + kind + ".json")
        async def list_available(request: Request, account_sid: str, country: str, _kind: str = kind) -> Any:
            require_auth(request, ctx)
            check_account(ctx, account_sid)
            rows = available_numbers(ctx, country, request.query_params)
            if _kind == "TollFree" and country.upper() in ("US", "CA"):
                rows = [
                    available_number(f"+188855501{i:02d}", "", "", country.upper()) for i in range(10, 20)
                ]
            uri = f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{country}/{_kind}.json"
            return page_2010(request, "available_phone_numbers", rows, uri)


__all__ = ["available_numbers", "register"]
