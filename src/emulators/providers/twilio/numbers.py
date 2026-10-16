"""Accounts, IncomingPhoneNumbers, AvailablePhoneNumbers, Balance, Usage and API Keys."""

from __future__ import annotations

import fnmatch
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import parse_instant
from emulators.providers.twilio.common import (
    API_VERSION,
    check_account,
    creds,
    fail,
    form_params,
    geo_for,
    get_param,
    missing_param,
    not_found,
    page_2010,
    param_value,
    parse_bool,
    require_auth,
    rfc2822,
    valid_e164,
    valid_url,
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

COUNTRIES = {"US": "United States", "CA": "Canada", "GB": "United Kingdom"}
_ACCOUNT_STATUSES = ("active", "suspended", "closed")
_USAGE_PERIODS = ("Daily", "Monthly", "Yearly", "AllTime", "LastMonth", "ThisMonth", "Today", "Yesterday")
_AVAILABLE_KINDS = ("Local", "TollFree", "Mobile", "National", "Voip", "MachineToMachine", "SharedCost")
_TYPED_NUMBER_LISTS = (("Local", "local"), ("TollFree", "toll-free"), ("Mobile", "mobile"))

_NUMBER_FIELDS = {
    "FriendlyName": "friendly_name",
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
    """Apply the configurable IncomingPhoneNumber parameters, validating URLs and methods."""
    for pascal, snake in _NUMBER_FIELDS.items():
        value = param_value(params, pascal)
        if value is None:
            continue
        if (snake.endswith("_url") or snake == "status_callback") and value and not valid_url(value):
            raise fail(400, 21402, f"Invalid Url: {value}. Url must be a valid http(s) URL.")
        if snake.endswith("_method"):
            if value and value.upper() not in ("GET", "POST"):
                raise fail(400, 21403, f"Invalid Method: {value}. Method must be GET or POST.")
            record[snake] = value.upper() or "POST"
            continue
        record[snake] = value or None
    caller_lookup = param_value(params, "VoiceCallerIdLookup")
    if caller_lookup is not None:
        record["voice_caller_id_lookup"] = parse_bool(caller_lookup)


def available_numbers(ctx: ProviderContext, country: str, query: Any) -> list[Record]:
    """Fictional ``555-01XX`` inventory for a supported country, filtered like Twilio's search."""
    country = country.upper()
    areas = _AREA_CITIES[country]
    wanted_area = query.get("AreaCode")
    contains = (query.get("Contains") or "").replace("*", "")
    owned = {n["phone_number"] for n in ctx.store.collection("numbers").values()}
    out: list[Record] = []
    for area, city, region in areas:
        if wanted_area and area != wanted_area:
            continue
        for i in range(10, 30):
            phone = f"+4420794601{i:02d}" if country == "GB" else f"+1{area}55501{i:02d}"
            if phone in owned or (contains and contains not in phone):
                continue
            out.append(available_number(phone, city, region, country))
    return out


# -- usage ------------------------------------------------------------------------


def _usage_window(period: str, query: Any, now: datetime, since: date) -> tuple[date, date]:
    today = now.date()
    first_of_month = today.replace(day=1)
    if period == "Today":
        return today, today
    if period == "Yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if period == "ThisMonth":
        return first_of_month, today
    if period == "LastMonth":
        last = first_of_month - timedelta(days=1)
        return last.replace(day=1), last
    start = since
    end = today
    for key, setter in (("StartDate", "start"), ("EndDate", "end")):
        raw = query.get(key)
        if not raw:
            continue
        try:
            parsed = parse_instant(raw).date()
        except ValueError:
            raise fail(400, 20001, f"Invalid {key}: {raw}. Use YYYY-MM-DD.") from None
        if setter == "start":
            start = parsed
        else:
            end = parsed
    return start, end


def _usage_records(
    ctx: ProviderContext, account: str, account_sid: str, start: date, end: date, now: datetime
) -> list[Record]:
    """The sms / calls / totalprice usage categories for one window, from live message/call state."""
    messages = [
        m
        for m in ctx.store.collection("messages").values()
        if m.get("_account") == account and start <= parse_instant(m["_created"]).date() <= end
    ]
    calls = [
        c
        for c in ctx.store.collection("calls").values()
        if c.get("_account") == account and start <= parse_instant(c["_created"]).date() <= end
    ]
    segments = sum(int(m.get("num_segments") or 0) for m in messages)
    minutes = sum((int(c.get("duration") or 0) + 59) // 60 for c in calls)
    sms_price = sum(abs(float(m["price"])) for m in messages if m.get("price"))
    call_price = sum(abs(float(c["price"])) for c in calls if c.get("price"))
    base = f"/{API_VERSION}/Accounts/{account_sid}/Usage/Records.json"
    common = {
        "account_sid": account_sid,
        "api_version": API_VERSION,
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date": end.isoformat(),
        "price_unit": "usd",
        "start_date": start.isoformat(),
        "subresource_uris": {},
        "uri": base,
    }
    return [
        {
            **common,
            "category": "sms",
            "count": str(len(messages)),
            "count_unit": "messages",
            "description": "Total SMS",
            "price": f"{sms_price:.5f}",
            "usage": str(segments),
            "usage_unit": "segments",
        },
        {
            **common,
            "category": "calls",
            "count": str(len(calls)),
            "count_unit": "calls",
            "description": "Total Calls",
            "price": f"{call_price:.5f}",
            "usage": str(minutes),
            "usage_unit": "minutes",
        },
        {
            **common,
            "category": "totalprice",
            "count": None,
            "count_unit": "",
            "description": "Total Price",
            "price": f"{sms_price + call_price:.5f}",
            "usage": f"{sms_price + call_price:.5f}",
            "usage_unit": "usd",
        },
    ]


def _usage_page(
    ctx: ProviderContext, request: Request, account: str, account_sid: str, period: str | None
) -> Any:
    """``Usage/Records`` and its ``Daily``/``Monthly``/``Yearly``/``Today``/... subresources."""
    now = ctx.clock.now()
    account_record = ctx.store.collection("accounts").get(account) or {}
    since = parse_instant(account_record.get("_created") or now.isoformat()).date()
    start, end = _usage_window(period or "AllTime", request.query_params, now, since)
    if period in ("Daily", "Monthly", "Yearly"):
        # One record set per calendar bucket that has activity inside the window.
        buckets: dict[tuple[date, date], None] = {}
        stamps = [
            parse_instant(r["_created"]).date()
            for coll in ("messages", "calls")
            for r in ctx.store.collection(coll).values()
            if r.get("_account") == account
        ]
        for day in sorted({d for d in stamps if start <= d <= end}):
            if period == "Daily":
                buckets[(day, day)] = None
            elif period == "Monthly":
                first = day.replace(day=1)
                nxt = (first + timedelta(days=32)).replace(day=1)
                buckets[(max(first, start), min(nxt - timedelta(days=1), end))] = None
            else:
                year_start, year_end = day.replace(month=1, day=1), day.replace(month=12, day=31)
                buckets[(max(year_start, start), min(year_end, end))] = None
        records = [r for lo, hi in buckets for r in _usage_records(ctx, account, account_sid, lo, hi, now)]
    else:
        records = _usage_records(ctx, account, account_sid, start, end, now)
    category = request.query_params.get("Category")
    if category:
        records = [r for r in records if r["category"] == category]
    suffix = f"/{period}" if period else ""
    base = f"/{API_VERSION}/Accounts/{account_sid}/Usage/Records{suffix}.json"
    return page_2010(request, "usage_records", records, base)


def register(app: FastAPI, ctx: ProviderContext) -> None:
    accounts_base = f"/{API_VERSION}/Accounts"

    # -- accounts ------------------------------------------------------------
    @app.get(accounts_base + ".json")
    async def list_accounts(request: Request) -> Any:
        auth = require_auth(request, ctx)
        records = ctx.store.collection("accounts").values()
        if auth.subaccount:  # a subaccount only ever sees itself
            records = [a for a in records if a["sid"] == auth.account_sid]
        query = request.query_params
        if query.get("FriendlyName"):
            records = [a for a in records if a["friendly_name"] == query["FriendlyName"]]
        if query.get("Status"):
            records = [a for a in records if a["status"] == query["Status"].lower()]
        return page_2010(request, "accounts", [render_account(a) for a in records], f"{accounts_base}.json")

    @app.post(accounts_base + ".json")
    async def create_account(request: Request) -> Response:
        auth = require_auth(request, ctx)
        if auth.subaccount:
            raise fail(403, 20003, "Permission Denied")
        params, _ = await form_params(request)
        c = creds(ctx)
        now = ctx.clock.now()
        sid_ = "AC" + ctx.ids.hex(32)
        record = build_account(
            sid_=sid_,
            friendly_name=get_param(params, "FriendlyName")
            or f"SubAccount Created at {now:%Y-%m-%d} {now.hour % 12 or 12}:{now:%M} {now:%p}",
            auth_token=ctx.ids.hex(32),
            created=now,
            owner_sid=c["account_sid"],
        )
        ctx.store.collection("accounts").put(record)
        return JSONResponse(render_account(record), status_code=201)

    @app.get(accounts_base + "/{sid}.json")
    async def get_account(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, sid, request)
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
        account = check_account(ctx, sid, request, allow_inactive=True)
        record = ctx.store.collection("accounts").get(account)
        if record is None:
            raise not_found(f"{accounts_base}/{sid}.json")
        params, _ = await form_params(request)
        friendly = param_value(params, "FriendlyName")
        status = (get_param(params, "Status") or "").lower()
        if status:
            if status not in _ACCOUNT_STATUSES:
                raise fail(400, 20001, f"Invalid Status: {status}. Must be one of active, suspended, closed.")
            if record["status"] == "closed" and status != "closed":
                raise fail(400, 20001, "A closed account cannot be reopened.")
            if account == creds(ctx)["account_sid"] and status != "active":
                raise fail(403, 20003, "Permission Denied")
        if friendly is not None:
            record["friendly_name"] = friendly
        if status:
            record["status"] = status
        touch(record, ctx.clock.now())
        return render_account(record)

    @app.get(accounts_base + "/{account_sid}/Balance.json")
    async def get_balance(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        check_account(ctx, account_sid, request)
        return {
            "account_sid": account_sid,
            "balance": ctx.store.meta.get("balance", "152.34"),
            "currency": "USD",
        }

    @app.get(accounts_base + "/{account_sid}/Usage/Records.json")
    async def usage_records(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        return _usage_page(ctx, request, account, account_sid, None)

    @app.get(accounts_base + "/{account_sid}/Usage/Records/{period}.json")
    async def usage_records_period(request: Request, account_sid: str, period: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        if period not in _USAGE_PERIODS:
            raise not_found(f"{accounts_base}/{account_sid}/Usage/Records/{period}.json")
        return _usage_page(ctx, request, account, account_sid, period)

    # -- API keys --------------------------------------------------------------
    def key_wire(record: Record, *, with_secret: bool = False) -> Record:
        out = {
            "sid": record["sid"],
            "friendly_name": record["friendly_name"],
            "date_created": record["date_created"],
            "date_updated": record["date_updated"],
        }
        if with_secret:
            out["secret"] = record["secret"]
        return out

    def key_or_404(account: str, account_sid: str, sid: str) -> Record:
        record = ctx.store.collection("api_keys").get(sid)
        if record is None or record.get("_account") != account:
            raise not_found(f"{accounts_base}/{account_sid}/Keys/{sid}.json")
        return record

    def keys_of(account: str) -> list[Record]:
        return [k for k in ctx.store.collection("api_keys").values() if k.get("_account") == account]

    @app.post(accounts_base + "/{account_sid}/Keys.json")
    async def create_key(request: Request, account_sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
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
            "_account": account,  # a key authenticates as the account that owns it
        }
        ctx.store.collection("api_keys").put(record)
        return JSONResponse(key_wire(record, with_secret=True), status_code=201)

    @app.get(accounts_base + "/{account_sid}/Keys.json")
    async def list_keys(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        rows = [key_wire(k_) for k_ in keys_of(account)]
        return page_2010(request, "keys", rows, f"{accounts_base}/{account_sid}/Keys.json")

    @app.get(accounts_base + "/{account_sid}/Keys/{sid}.json")
    async def get_key(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        return key_wire(key_or_404(account, account_sid, sid))

    @app.post(accounts_base + "/{account_sid}/Keys/{sid}.json")
    async def update_key(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        record = key_or_404(account, account_sid, sid)
        params, _ = await form_params(request)
        friendly = param_value(params, "FriendlyName")
        if friendly is not None:
            record["friendly_name"] = friendly
        touch(record, ctx.clock.now())
        return key_wire(record)

    @app.delete(accounts_base + "/{account_sid}/Keys/{sid}.json")
    async def delete_key(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        key_or_404(account, account_sid, sid)
        ctx.store.collection("api_keys").delete(sid)
        return Response(status_code=204)

    # SigningKeys are the same credentials under another name (newer SDKs create keys this way).
    @app.post(accounts_base + "/{account_sid}/SigningKeys.json")
    async def create_signing_key(request: Request, account_sid: str) -> Response:
        return await create_key(request, account_sid)

    @app.get(accounts_base + "/{account_sid}/SigningKeys.json")
    async def list_signing_keys(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        rows = [key_wire(k_) for k_ in keys_of(account)]
        return page_2010(request, "signing_keys", rows, f"{accounts_base}/{account_sid}/SigningKeys.json")

    @app.get(accounts_base + "/{account_sid}/SigningKeys/{sid}.json")
    async def get_signing_key(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        return key_wire(key_or_404(account, account_sid, sid))

    @app.delete(accounts_base + "/{account_sid}/SigningKeys/{sid}.json")
    async def delete_signing_key(request: Request, account_sid: str, sid: str) -> Response:
        return await delete_key(request, account_sid, sid)

    # -- incoming phone numbers --------------------------------------------------
    numbers_base = accounts_base + "/{account_sid}/IncomingPhoneNumbers"

    def numbers_of(request: Request, account: str, kind: str | None) -> list[Record]:
        records = [n for n in ctx.store.collection("numbers").values() if n.get("_account") == account]
        if kind is not None:
            records = [n for n in records if n.get("_type") == kind]
        query = request.query_params
        if query.get("PhoneNumber"):
            # Partial numbers and ``*`` wildcards are accepted, as in the real list filter.
            raw = query["PhoneNumber"]
            pattern = "+" + raw[1:] if raw.startswith(" ") else raw  # an unencoded "+" decodes to " "
            records = [
                n
                for n in records
                if fnmatch.fnmatchcase(n["phone_number"], pattern)
                or ("*" not in pattern and pattern in n["phone_number"])
            ]
        if query.get("FriendlyName"):
            records = [n for n in records if n["friendly_name"] == query["FriendlyName"]]
        if query.get("Beta") is not None:
            records = [n for n in records if n["beta"] == parse_bool(query["Beta"])]
        return records

    async def purchase(request: Request, account_sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        params, _ = await form_params(request)
        phone = get_param(params, "PhoneNumber")
        area = get_param(params, "AreaCode")
        if not phone and not area:
            raise missing_param("PhoneNumber or AreaCode")
        if not phone:
            country = "CA" if geo_for(f"+1{area}5550100")[2] == "CA" else "US"
            candidates = available_numbers(ctx, country, {"AreaCode": area})
            if not candidates:
                raise fail(400, 21452, f"No phone numbers found in area code {area}.")
            phone = candidates[0]["phone_number"]
        if not valid_e164(phone):
            raise fail(400, 21421, f"PhoneNumber {phone} is invalid.")
        if ctx.store.collection("numbers").first(phone_number=phone) is not None:
            raise fail(400, 21422, f"PhoneNumber {phone} is not available to be purchased.")
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

    def make_typed_list(slug: str, kind: str) -> Any:
        async def list_typed(request: Request, account_sid: str) -> Any:
            require_auth(request, ctx)
            account = check_account(ctx, account_sid, request)
            rows = [render_number(n) for n in numbers_of(request, account, kind)]
            uri = f"{accounts_base}/{account_sid}/IncomingPhoneNumbers/{slug}.json"
            return page_2010(request, "incoming_phone_numbers", rows, uri)

        return list_typed

    # The typed sub-collections must be registered before ``/{sid}.json``, or Starlette would
    # route ``/Local.json`` to the fetch-by-SID handler.
    for slug, kind in _TYPED_NUMBER_LISTS:
        app.get(numbers_base + f"/{slug}.json")(make_typed_list(slug, kind))
        app.post(numbers_base + f"/{slug}.json")(purchase)

    @app.get(numbers_base + ".json")
    async def list_numbers(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        rows = [render_number(n) for n in numbers_of(request, account, None)]
        return page_2010(
            request,
            "incoming_phone_numbers",
            rows,
            f"{accounts_base}/{account_sid}/IncomingPhoneNumbers.json",
        )

    app.post(numbers_base + ".json")(purchase)

    @app.get(numbers_base + "/{sid}.json")
    async def get_number(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        return render_number(_number_or_404(ctx, account, sid))

    @app.post(numbers_base + "/{sid}.json")
    async def update_number(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        record = _number_or_404(ctx, account, sid)
        params, _ = await form_params(request)
        _apply_number_params(record, params)
        touch(record, ctx.clock.now())
        return render_number(record)

    @app.delete(numbers_base + "/{sid}.json")
    async def delete_number(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        _number_or_404(ctx, account, sid)
        ctx.store.collection("numbers").delete(sid)
        links = ctx.store.collection("mg_numbers")
        for link in [lnk for lnk in links.values() if lnk["sid"] == sid]:
            links.delete(link["id"])
        return Response(status_code=204)

    # -- available phone numbers -------------------------------------------------
    avail_base = accounts_base + "/{account_sid}/AvailablePhoneNumbers"

    def country_wire(account_sid: str, code: str) -> Record:
        base = f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{code}"
        return {
            "country_code": code,
            "country": COUNTRIES[code],
            "uri": f"{base}.json",
            "beta": False,
            "subresource_uris": {
                "local": f"{base}/Local.json",
                "toll_free": f"{base}/TollFree.json",
                "mobile": f"{base}/Mobile.json",
            },
        }

    @app.get(avail_base + ".json")
    async def list_countries(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        check_account(ctx, account_sid, request)
        rows = [country_wire(account_sid, code) for code in COUNTRIES]
        return page_2010(
            request, "countries", rows, f"{accounts_base}/{account_sid}/AvailablePhoneNumbers.json"
        )

    @app.get(avail_base + "/{country}.json")
    async def get_country(request: Request, account_sid: str, country: str) -> Any:
        require_auth(request, ctx)
        check_account(ctx, account_sid, request)
        code = country.upper()
        if code not in COUNTRIES:
            raise not_found(f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{country}.json")
        return country_wire(account_sid, code)

    def make_available_list(kind: str) -> Any:
        async def list_available(request: Request, account_sid: str, country: str) -> Any:
            require_auth(request, ctx)
            check_account(ctx, account_sid, request)
            code = country.upper()
            uri = f"{accounts_base}/{account_sid}/AvailablePhoneNumbers/{country}/{kind}.json"
            if code not in COUNTRIES:
                raise not_found(uri)
            if kind == "TollFree" and code in ("US", "CA"):
                rows = [available_number(f"+188855501{i:02d}", "", "", code) for i in range(10, 20)]
            else:
                rows = available_numbers(ctx, code, request.query_params)
            return page_2010(request, "available_phone_numbers", rows, uri)

        return list_available

    for kind in _AVAILABLE_KINDS:
        app.get(avail_base + "/{country}/" + kind + ".json")(make_available_list(kind))


__all__ = ["COUNTRIES", "available_numbers", "register"]
