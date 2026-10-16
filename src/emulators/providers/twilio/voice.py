"""Voice: Calls and Recordings (2010 API), plus the inbound-call simulation."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import isoformat_z, parse_instant, utc
from emulators.providers.twilio.common import (
    API_VERSION,
    auto_advance,
    bare_number,
    check_account,
    deliver_form,
    fail,
    form_params,
    geo_for,
    get_param,
    newest_first,
    not_found,
    page_2010,
    parse_bool,
    payload_text,
    require_auth,
    rfc2822,
    spawn,
    valid_e164,
    valid_url,
)
from emulators.providers.twilio.messages import numbers_by_phone
from emulators.providers.twilio.payloads import silent_mp3
from emulators.providers.twilio.shapes import (
    build_call,
    build_recording,
    render_call,
    render_recording,
    touch,
)

if TYPE_CHECKING:
    from emulators.core.provider import ProviderContext

Record = dict[str, Any]

_CHAIN = ["queued", "ringing", "in-progress", "completed"]
_TERMINAL = ("completed", "busy", "no-answer", "canceled", "failed")
_ADVANCE_TARGETS = ("ringing", "in-progress", *_TERMINAL)
_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "queued": ("ringing", "in-progress", "completed", "busy", "no-answer", "canceled", "failed"),
    "ringing": ("in-progress", "completed", "busy", "no-answer", "canceled", "failed"),
    "in-progress": ("completed", "failed"),
}
_EVENT_FOR = {
    "queued": "initiated",
    "ringing": "ringing",
    "in-progress": "answered",
    # Every way a call can end is reported under the "completed" event.
    "completed": "completed",
    "busy": "completed",
    "no-answer": "completed",
    "failed": "completed",
    "canceled": "completed",
}


def _talk_time(ctx: ProviderContext, record: Record, now: datetime) -> int:
    """Seconds a call lasted: the real elapsed time when a test held it in progress on the
    emulator clock, otherwise the deterministic simulated duration."""
    started = record.get("_started")
    elapsed = int((now - parse_instant(started)).total_seconds()) if started else 0
    return elapsed if elapsed >= 1 else _duration_for(record["sid"])


def _duration_for(sid_: str) -> int:
    return 20 + int(hashlib.sha256(sid_.encode()).hexdigest()[:4], 16) % 180


async def _fire_call_callback(ctx: ProviderContext, record: Record) -> None:
    if record.get("_test"):
        return
    url = record.get("_status_callback")
    if not url:
        return
    event = _EVENT_FOR.get(record["status"], record["status"])
    if event not in (record.get("_status_callback_events") or ["completed"]):
        return
    record["_sequence"] = int(record.get("_sequence") or 0) + 1
    caller, called = record["from"], record["to"]
    from_city, from_state, from_country = geo_for(record["from"])
    to_city, to_state, to_country = geo_for(record["to"])
    params = {
        "AccountSid": record["account_sid"],
        "ApiVersion": API_VERSION,
        "CallSid": record["sid"],
        # The "initiated" progress event reports CallStatus=initiated (the REST status is "queued").
        "CallStatus": "initiated" if record["status"] == "queued" else record["status"],
        "CallbackSource": "call-progress-events",
        "CallerName": "",
        "Called": called,
        "CalledCity": to_city,
        "CalledCountry": to_country,
        "CalledState": to_state,
        "CalledZip": "",
        "Caller": caller,
        "CallerCity": from_city,
        "CallerCountry": from_country,
        "CallerState": from_state,
        "CallerZip": "",
        "Direction": record["direction"],
        "From": record["from"],
        "FromCity": from_city,
        "FromCountry": from_country,
        "FromState": from_state,
        "FromZip": "",
        "SequenceNumber": str(record["_sequence"]),
        "Timestamp": rfc2822(ctx.clock.now()) or "",
        "To": record["to"],
        "ToCity": to_city,
        "ToCountry": to_country,
        "ToState": to_state,
        "ToZip": "",
    }
    if record.get("parent_call_sid"):
        params["ParentCallSid"] = record["parent_call_sid"]
    if record["status"] == "completed":
        params["CallDuration"] = str(record.get("duration") or "0")
        params["Duration"] = str(max(1, (int(record.get("duration") or 0) + 59) // 60))
        params["SipResponseCode"] = "200"
    await deliver_form(
        ctx, url, params, event_type="call.status", method=record.get("_status_callback_method") or "POST"
    )


async def advance_call(ctx: ProviderContext, record: Record, to_status: str | None = None) -> Record:
    if record["status"] in _TERMINAL:
        return record
    try:
        start = _CHAIN.index(record["status"])
    except ValueError:
        start = -1
    steps = _CHAIN[start + 1 :]
    if to_status:
        steps = steps[: steps.index(to_status) + 1] if to_status in steps else [to_status]
    now = ctx.clock.now()
    for step in steps:
        if ctx.store.collection("calls").get(record["sid"]) is None:
            break
        record["status"] = step
        touch(record, ctx.clock.now())
        if step == "in-progress" and not record.get("start_time"):
            record["start_time"] = rfc2822(now)
            record["_started"] = isoformat_z(utc(now))
            await _fire_call_callback(ctx, record)
            await _fetch_twiml(ctx, record)
            continue
        if step in ("completed", "busy", "no-answer", "canceled", "failed"):
            duration = _talk_time(ctx, record, now) if step == "completed" else 0
            record["duration"] = str(duration)
            began = parse_instant(record["_started"]) if record.get("_started") else now
            record["end_time"] = rfc2822(began + timedelta(seconds=duration))
            record["price"] = (
                f"-{0.014 * max(1, (duration + 59) // 60):.5f}" if step == "completed" else "0.00000"
            )
            record["answered_by"] = "human" if step == "completed" else record.get("answered_by")
            if step == "completed" and record.get("_record"):
                _make_recording(ctx, record, duration)
        await _fire_call_callback(ctx, record)
    return record


async def _fetch_twiml(ctx: ProviderContext, record: Record) -> None:
    """Twilio fetches the call's Url when answered; the emulator POSTs and ignores TwiML."""
    url = record.get("_url")
    if not url or record.get("_test"):
        return
    params = {
        "AccountSid": record["account_sid"],
        "ApiVersion": API_VERSION,
        "CallSid": record["sid"],
        "CallStatus": "in-progress",
        "Called": record["to"],
        "Caller": record["from"],
        "Direction": record["direction"],
        "From": record["from"],
        "To": record["to"],
    }
    await deliver_form(ctx, url, params, event_type="call.twiml")


def _make_recording(ctx: ProviderContext, call: Record, duration: int) -> Record:
    store = ctx.store.collection("recordings")
    for existing in store.values():
        if existing["call_sid"] == call["sid"]:
            return existing
    record = build_recording(
        sid_="RE" + ctx.ids.hex(32),
        account_sid=call["account_sid"],
        call_sid=call["sid"],
        created=ctx.clock.now(),
        duration_s=duration,
    )
    store.put(record)
    return record


async def maybe_advance_call(ctx: ProviderContext, record: Record) -> None:
    if not auto_advance(ctx):
        return  # EMULATORS_TWILIO_AUTO_ADVANCE=0: tests drive the call with the advance_call action
    if ctx.webhooks.mode in ("sync", "manual"):
        await advance_call(ctx, record)
    else:

        async def _delayed() -> None:
            await asyncio.sleep(0.05)
            await advance_call(ctx, record)

        spawn(ctx, _delayed())


def recording_bytes(ctx: ProviderContext, record: Record) -> bytes:
    blob = record.get("_blob")
    if blob and ctx.store.blobs.has(blob):
        return ctx.store.blobs.get(blob)
    from emulators.world.media import make_wav

    duration = min(int(record.get("_duration_s") or 5), 30)
    seed = int(record["sid"][-6:], 16) % 100000
    content = make_wav(float(max(duration, 2)), seed=seed)
    record["_blob"] = ctx.store.blobs.put(content)
    return content


def recording_mp3_bytes(ctx: ProviderContext, record: Record) -> bytes:
    blob = record.get("_blob_mp3")
    if blob and ctx.store.blobs.has(blob):
        return ctx.store.blobs.get(blob)
    content = silent_mp3(float(min(int(record.get("_duration_s") or 5), 30)))
    record["_blob_mp3"] = ctx.store.blobs.put(content)
    return content


def _call_or_404(ctx: ProviderContext, account: str, sid_: str) -> Record:
    record = ctx.store.collection("calls").get(sid_)
    if record is None or record.get("_account") != account:
        raise not_found(f"/{API_VERSION}/Accounts/{account}/Calls/{sid_}.json")
    return record


def _recording_or_404(ctx: ProviderContext, account: str, sid_: str) -> Record:
    record = ctx.store.collection("recordings").get(sid_)
    if record is None or record.get("_account") != account:
        raise not_found(f"/{API_VERSION}/Accounts/{account}/Recordings/{sid_}.json")
    return record


async def create_call_record(
    ctx: ProviderContext,
    *,
    account: str,
    to: str,
    from_: str,
    url: str | None,
    twiml: str | None,
    status_callback: str | None,
    status_callback_method: str,
    events: list[str],
    record_flag: bool,
    direction: str = "outbound-api",
    is_test: bool = False,
) -> tuple[Record, Record]:
    sid_ = "CA" + ctx.ids.hex(32)
    number = (
        numbers_by_phone(ctx).get(bare_number(from_))
        if direction == "outbound-api"
        else numbers_by_phone(ctx).get(bare_number(to))
    )
    record = build_call(
        sid_=sid_,
        account_sid=account,
        to=to,
        from_=from_,
        status="queued",
        created=ctx.clock.now(),
        direction=direction,
        phone_number_sid=number["sid"] if number else None,
        status_callback=status_callback,
        status_callback_method=status_callback_method,
        status_callback_events=events or ["completed"],
        record=record_flag,
        url=url,
        twiml=twiml,
    )
    record["_test"] = is_test
    ctx.store.collection("calls").put(record)
    snapshot = render_call(record)
    await _fire_call_callback(ctx, record)  # the "initiated" progress event, when subscribed to
    await maybe_advance_call(ctx, record)
    return record, snapshot


def register(app: FastAPI, ctx: ProviderContext) -> None:
    calls_base = f"/{API_VERSION}/Accounts/{{account_sid}}/Calls"

    @app.post(calls_base + ".json")
    async def create_call(request: Request, account_sid: str) -> Response:
        auth = require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        params, multi = await form_params(request)
        to = get_param(params, "To")
        from_ = get_param(params, "From")
        if not to:
            raise fail(400, 21201, "No 'To' number is specified")
        if not valid_e164(bare_number(to)):
            raise fail(400, 21211, f"The 'To' number {to} is not a valid phone number.")
        if not from_:
            raise fail(400, 21213, "No 'From' number is specified")
        owned = numbers_by_phone(ctx)
        if bare_number(from_) not in owned and bare_number(from_) != "+15005550006":
            raise fail(
                400,
                21210,
                f"The source phone number provided, {from_}, is not yet verified for your account. "
                "You may only make calls from phone numbers that you've verified or purchased from Twilio.",
            )
        url = get_param(params, "Url")
        twiml = get_param(params, "Twiml")
        if not (url or twiml or get_param(params, "ApplicationSid")):
            raise fail(400, 21205, "Url parameter is required")
        if url and not valid_url(url):
            raise fail(400, 21205, f"Invalid Url: {url}")
        status_callback = get_param(params, "StatusCallback")
        if status_callback and not valid_url(status_callback):
            raise fail(400, 21609, f"StatusCallback URL {status_callback} is not a valid URL.")
        method = (get_param(params, "StatusCallbackMethod") or "POST").upper()
        if method not in ("GET", "POST"):
            raise fail(400, 21403, f"Invalid StatusCallbackMethod: {method}")
        events = multi.get("StatusCallbackEvent") or [get_param(params, "StatusCallbackEvent") or "completed"]
        # Twilio takes the events as a space separated list as well as repeated parameters.
        normalized = [e.lower() for raw in events for e in raw.split() if e]
        _record, snapshot = await create_call_record(
            ctx,
            account=account,
            to=to,
            from_=from_,
            url=url,
            twiml=twiml,
            status_callback=status_callback,
            status_callback_method=method,
            events=normalized,
            record_flag=parse_bool(get_param(params, "Record")),
            is_test=auth.is_test,
        )
        return JSONResponse(snapshot, status_code=201)

    @app.get(calls_base + ".json")
    async def list_calls(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        records = [c for c in ctx.store.collection("calls").values() if c.get("_account") == account]
        query = request.query_params
        if query.get("To"):
            records = [c for c in records if c["to"] == query["To"]]
        if query.get("From"):
            records = [c for c in records if c["from"] == query["From"]]
        if query.get("Status"):
            records = [c for c in records if c["status"] == query["Status"]]
        if query.get("ParentCallSid"):
            records = [c for c in records if c.get("parent_call_sid") == query["ParentCallSid"]]
        for key, op in (("StartTime", "eq"), ("StartTime<", "lte"), ("StartTime>", "gte")):
            raw = query.get(key)
            if not raw:
                continue
            try:
                bound = parse_instant(raw)
            except ValueError:
                raise fail(
                    400, 20001, f"Invalid {key}: {raw}. Use YYYY-MM-DD or an ISO 8601 timestamp."
                ) from None

            def _match(c: Record, bound: datetime = bound, op: str = op) -> bool:
                started = c.get("_started") or c.get("_created")
                if not started:
                    return False
                instant = parse_instant(started)
                if op == "eq":  # an equality filter is a UTC day filter, as with DateSent
                    return instant.date() == bound.date()
                return instant <= bound if op == "lte" else instant >= bound

            records = [c for c in records if _match(c)]
        records = newest_first(records, "_created")
        uri = f"/{API_VERSION}/Accounts/{account_sid}/Calls.json"
        return page_2010(request, "calls", [render_call(c) for c in records], uri)

    @app.get(calls_base + "/{sid}.json")
    async def get_call(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        return render_call(_call_or_404(ctx, account, sid))

    @app.post(calls_base + "/{sid}.json")
    async def update_call(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        record = _call_or_404(ctx, account, sid)
        params, _ = await form_params(request)
        status = (get_param(params, "Status") or "").lower()
        url = get_param(params, "Url")
        twiml = get_param(params, "Twiml")
        if (status or url or twiml) and record["status"] in _TERMINAL:
            raise fail(400, 21220, "Call is not in-progress. Cannot redirect.")
        if status:
            if status not in ("completed", "canceled"):
                raise fail(400, 21220, f"Call status '{status}' is not a valid update status.")
            if status == "canceled" and record["status"] == "in-progress":
                raise fail(400, 21220, "Call is in-progress. Cannot cancel; use Status=completed to hang up.")
            if record["status"] in ("queued", "ringing"):
                # Ending a call that was never answered cancels it.
                await advance_call(ctx, record, "canceled")
            else:
                await advance_call(ctx, record, "completed")
        if url:
            if not valid_url(url):
                raise fail(400, 21205, f"Invalid Url: {url}")
            record["_url"] = url
        if twiml:
            record["_twiml"] = twiml
        touch(record, ctx.clock.now())
        return render_call(record)

    @app.get(calls_base + "/{sid}/Recordings.json")
    async def call_recordings(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        _call_or_404(ctx, account, sid)
        rows = [
            render_recording(r, request, ctx)
            for r in ctx.store.collection("recordings").values()
            if r["call_sid"] == sid
        ]
        uri = f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid}/Recordings.json"
        return page_2010(request, "recordings", rows, uri)

    @app.post(calls_base + "/{sid}/Recordings.json")
    async def start_recording(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        call = _call_or_404(ctx, account, sid)
        record = _make_recording(ctx, call, int(call.get("duration") or 30))
        return JSONResponse(render_recording(record, request, ctx), status_code=201)

    def call_recording_or_404(account: str, account_sid: str, sid: str, rec_sid: str) -> Record:
        _call_or_404(ctx, account, sid)
        record = ctx.store.collection("recordings").get(rec_sid)
        if record is None or record["call_sid"] != sid or record.get("_account") != account:
            raise not_found(f"/{API_VERSION}/Accounts/{account_sid}/Calls/{sid}/Recordings/{rec_sid}.json")
        return record

    @app.get(calls_base + "/{sid}/Recordings/{rec_sid}.json")
    async def get_call_recording(request: Request, account_sid: str, sid: str, rec_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        return render_recording(call_recording_or_404(account, account_sid, sid, rec_sid), request, ctx)

    @app.delete(calls_base + "/{sid}/Recordings/{rec_sid}.json")
    async def delete_call_recording(request: Request, account_sid: str, sid: str, rec_sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        call_recording_or_404(account, account_sid, sid, rec_sid)
        ctx.store.collection("recordings").delete(rec_sid)
        return Response(status_code=204)

    rec_base = f"/{API_VERSION}/Accounts/{{account_sid}}/Recordings"

    @app.get(rec_base + ".json")
    async def list_recordings(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        records = [r for r in ctx.store.collection("recordings").values() if r.get("_account") == account]
        if request.query_params.get("CallSid"):
            records = [r for r in records if r["call_sid"] == request.query_params["CallSid"]]
        records = newest_first(records, "_created")
        uri = f"/{API_VERSION}/Accounts/{account_sid}/Recordings.json"
        return page_2010(request, "recordings", [render_recording(r, request, ctx) for r in records], uri)

    @app.get(rec_base + "/{sid}.json")
    async def get_recording(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        return render_recording(_recording_or_404(ctx, account, sid), request, ctx)

    @app.delete(rec_base + "/{sid}.json")
    async def delete_recording(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        _recording_or_404(ctx, account, sid)
        ctx.store.collection("recordings").delete(sid)
        return Response(status_code=204)

    @app.get(rec_base + "/{sid}.wav")
    async def recording_wav(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        return Response(recording_bytes(ctx, _recording_or_404(ctx, account, sid)), media_type="audio/wav")

    @app.get(rec_base + "/{sid}.mp3")
    async def recording_mp3(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        return Response(
            recording_mp3_bytes(ctx, _recording_or_404(ctx, account, sid)), media_type="audio/mpeg"
        )

    @app.get(rec_base + "/{sid}")
    async def recording_media(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid, request)
        return Response(recording_bytes(ctx, _recording_or_404(ctx, account, sid)), media_type="audio/x-wav")


# -- actions -----------------------------------------------------------------------


async def action_advance_call(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Advance a call's status (optionally to a named status)."""
    sid_ = payload_text(payload, "sid", "Sid")
    record = ctx.store.collection("calls").get(sid_) if sid_ else None
    if record is None:
        raise fail(404, 20404, f"Call {sid_} not found.")
    target = payload_text(payload, "status", "Status")
    if target and target not in _ADVANCE_TARGETS:
        raise fail(400, 20001, f"Unknown status '{target}'. Use one of {', '.join(_ADVANCE_TARGETS)}.")
    if target and target not in _TRANSITIONS.get(record["status"], ()):
        raise fail(400, 20001, f"A call cannot move from '{record['status']}' to '{target}'.")
    await advance_call(ctx, record, target)
    return {"call": render_call(record)}


async def action_inbound_call(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Simulate an inbound call: creates the Call and POSTs the number's VoiceUrl."""
    from_ = payload_text(payload, "from", "From")
    to = payload_text(payload, "to", "To")
    if not from_ or not to:
        raise fail(400, 20001, "inbound_call requires 'from' and 'to'.")
    from emulators.providers.twilio.common import creds as _creds

    account = _creds(ctx)["account_sid"]
    number = numbers_by_phone(ctx).get(bare_number(to))
    record, _snapshot = await create_call_record(
        ctx,
        account=account,
        to=to,
        from_=from_,
        url=number.get("voice_url") if number else None,
        twiml=None,
        status_callback=number.get("status_callback") if number else None,
        status_callback_method="POST",
        events=["initiated", "ringing", "answered", "completed"],
        record_flag=False,
        direction="inbound",
    )
    return {"call": render_call(record)}


__all__ = [
    "action_advance_call",
    "action_inbound_call",
    "advance_call",
    "create_call_record",
    "recording_bytes",
    "register",
]
