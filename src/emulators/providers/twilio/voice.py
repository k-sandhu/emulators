"""Voice: Calls and Recordings (2010 API), plus the inbound-call simulation."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import isoformat_z, parse_instant, utc
from emulators.providers.twilio.common import (
    API_VERSION,
    bare_number,
    check_account,
    deliver_form,
    fail,
    form_params,
    get_param,
    not_found,
    page_2010,
    parse_bool,
    require_auth,
    rfc2822,
    valid_e164,
)
from emulators.providers.twilio.messages import numbers_by_phone
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
_EVENT_FOR = {
    "queued": "initiated",
    "ringing": "ringing",
    "in-progress": "answered",
    "completed": "completed",
}


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
    params = {
        "AccountSid": record["account_sid"],
        "ApiVersion": API_VERSION,
        "CallSid": record["sid"],
        "CallStatus": record["status"],
        "CallbackSource": "call-progress-events",
        "CallerName": "",
        "Direction": record["direction"],
        "From": record["from"],
        "SequenceNumber": str(record["_sequence"]),
        "Timestamp": rfc2822(ctx.clock.now()) or "",
        "To": record["to"],
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
    if record["status"] in ("completed", "busy", "no-answer", "canceled", "failed"):
        return record
    try:
        start = _CHAIN.index(record["status"])
    except ValueError:
        start = -1
    steps = _CHAIN[start + 1 :]
    if record["status"] == "queued" and "initiated" in (record.get("_status_callback_events") or []):
        await _fire_call_callback(ctx, record)  # the "initiated" progress event
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
            duration = _duration_for(record["sid"]) if step == "completed" else 0
            record["duration"] = str(duration)
            record["end_time"] = rfc2822(now + timedelta(seconds=duration))
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
    if ctx.webhooks.mode in ("sync", "manual"):
        await advance_call(ctx, record)
    else:

        async def _delayed() -> None:
            await asyncio.sleep(0.05)
            await advance_call(ctx, record)

        asyncio.get_running_loop().create_task(_delayed())


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
    await maybe_advance_call(ctx, record)
    return record, snapshot


def register(app: FastAPI, ctx: ProviderContext) -> None:
    calls_base = f"/{API_VERSION}/Accounts/{{account_sid}}/Calls"

    @app.post(calls_base + ".json")
    async def create_call(request: Request, account_sid: str) -> Response:
        auth = require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        params, multi = await form_params(request)
        to = get_param(params, "To")
        from_ = get_param(params, "From")
        if not to:
            raise fail(400, 21201, "'To' is required.")
        if not valid_e164(bare_number(to)):
            raise fail(400, 21211, f"The 'To' number {to} is not a valid phone number.")
        if not from_:
            raise fail(400, 21203, "'From' is required.")
        owned = numbers_by_phone(ctx)
        if bare_number(from_) not in owned and bare_number(from_) != "+15005550006":
            raise fail(400, 21210, f"The 'From' number {from_} is not a valid Caller ID.")
        if not (
            get_param(params, "Url") or get_param(params, "Twiml") or get_param(params, "ApplicationSid")
        ):
            raise fail(400, 21205, "No 'Url', 'Twiml', or 'ApplicationSid' provided.")
        events = multi.get("StatusCallbackEvent") or [get_param(params, "StatusCallbackEvent") or "completed"]
        normalized = [{"initiated": "initiated"}.get(e.lower(), e.lower()) for e in events if e]
        _record, snapshot = await create_call_record(
            ctx,
            account=account,
            to=to,
            from_=from_,
            url=get_param(params, "Url"),
            twiml=get_param(params, "Twiml"),
            status_callback=get_param(params, "StatusCallback"),
            status_callback_method=get_param(params, "StatusCallbackMethod") or "POST",
            events=normalized,
            record_flag=parse_bool(get_param(params, "Record")),
            is_test=auth.is_test,
        )
        return JSONResponse(snapshot, status_code=201)

    @app.get(calls_base + ".json")
    async def list_calls(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
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
        for key, field, op in (
            ("StartTime", "_created", "eq"),
            ("StartTime<", "_created", "lt"),
            ("StartTime>", "_created", "gt"),
        ):
            raw = query.get(key)
            if raw:
                try:
                    bound = parse_instant(raw)
                except ValueError:
                    continue
                records = [
                    c
                    for c in records
                    if c.get(field)
                    and (
                        parse_instant(c[field]) == bound
                        if op == "eq"
                        else parse_instant(c[field]) < bound
                        if op == "lt"
                        else parse_instant(c[field]) > bound
                    )
                ]
        records.sort(key=lambda c: c.get("_created") or "", reverse=True)
        uri = f"/{API_VERSION}/Accounts/{account_sid}/Calls.json"
        return page_2010(request, "calls", [render_call(c) for c in records], uri)

    @app.get(calls_base + "/{sid}.json")
    async def get_call(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        return render_call(_call_or_404(ctx, account, sid))

    @app.post(calls_base + "/{sid}.json")
    async def update_call(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        record = _call_or_404(ctx, account, sid)
        params, _ = await form_params(request)
        status = get_param(params, "Status")
        if status:
            if status not in ("completed", "canceled"):
                raise fail(400, 21220, f"Call status '{status}' is not a valid update status.")
            if record["status"] in ("queued", "ringing") and status == "canceled":
                record["status"] = "canceled"
                record["duration"] = "0"
                record["end_time"] = rfc2822(ctx.clock.now())
                await _fire_call_callback(ctx, record)
            elif record["status"] == "in-progress" and status == "completed":
                await advance_call(ctx, record, "completed")
            elif record["status"] not in ("completed", "canceled"):
                await advance_call(ctx, record, status)
        url = get_param(params, "Url")
        if url:
            record["_url"] = url
        twiml = get_param(params, "Twiml")
        if twiml:
            record["_twiml"] = twiml
        touch(record, ctx.clock.now())
        return render_call(record)

    @app.get(calls_base + "/{sid}/Recordings.json")
    async def call_recordings(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
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
        account = check_account(ctx, account_sid)
        call = _call_or_404(ctx, account, sid)
        record = _make_recording(ctx, call, int(call.get("duration") or 30))
        return JSONResponse(render_recording(record, request, ctx), status_code=201)

    rec_base = f"/{API_VERSION}/Accounts/{{account_sid}}/Recordings"

    @app.get(rec_base + ".json")
    async def list_recordings(request: Request, account_sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        records = [r for r in ctx.store.collection("recordings").values() if r.get("_account") == account]
        if request.query_params.get("CallSid"):
            records = [r for r in records if r["call_sid"] == request.query_params["CallSid"]]
        records.sort(key=lambda r: r.get("_created") or "", reverse=True)
        uri = f"/{API_VERSION}/Accounts/{account_sid}/Recordings.json"
        return page_2010(request, "recordings", [render_recording(r, request, ctx) for r in records], uri)

    @app.get(rec_base + "/{sid}.json")
    async def get_recording(request: Request, account_sid: str, sid: str) -> Any:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        return render_recording(_recording_or_404(ctx, account, sid), request, ctx)

    @app.delete(rec_base + "/{sid}.json")
    async def delete_recording(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        _recording_or_404(ctx, account, sid)
        ctx.store.collection("recordings").delete(sid)
        return Response(status_code=204)

    @app.get(rec_base + "/{sid}.wav")
    async def recording_wav(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        return Response(recording_bytes(ctx, _recording_or_404(ctx, account, sid)), media_type="audio/wav")

    @app.get(rec_base + "/{sid}.mp3")
    async def recording_mp3(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        return Response(recording_bytes(ctx, _recording_or_404(ctx, account, sid)), media_type="audio/mpeg")

    @app.get(rec_base + "/{sid}")
    async def recording_media(request: Request, account_sid: str, sid: str) -> Response:
        require_auth(request, ctx)
        account = check_account(ctx, account_sid)
        return Response(recording_bytes(ctx, _recording_or_404(ctx, account, sid)), media_type="audio/x-wav")


# -- actions -----------------------------------------------------------------------


async def action_advance_call(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Advance a call's status (optionally to a named status)."""
    sid_ = payload.get("sid") or payload.get("Sid")
    record = ctx.store.collection("calls").get(sid_) if sid_ else None
    if record is None:
        raise fail(404, 20404, f"Call {sid_} not found.")
    await advance_call(ctx, record, payload.get("status") or payload.get("Status"))
    return {"call": render_call(record)}


async def action_inbound_call(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Simulate an inbound call: creates the Call and POSTs the number's VoiceUrl."""
    from_ = payload.get("from") or payload.get("From")
    to = payload.get("to") or payload.get("To")
    if not from_ or not to:
        raise fail(400, 21200, "inbound_call requires 'from' and 'to'.")
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
