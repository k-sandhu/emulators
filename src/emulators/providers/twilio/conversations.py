"""Conversations API (conversations.twilio.com/v1): Conversations, Participants, Messages."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.providers.twilio.common import (
    CONVERSATIONS_HOST,
    creds,
    fail,
    form_params,
    get_param,
    iso8601,
    not_found,
    page_meta,
    param_value,
    parse_int,
    require_auth,
)
from emulators.providers.twilio.common import (
    sid as new_sid,
)
from emulators.providers.twilio.shapes import (
    render_conv_message,
    render_conversation,
    render_participant,
    touch,
)

if TYPE_CHECKING:
    from emulators.core.provider import ProviderContext

Record = dict[str, Any]


def _conv_or_404(ctx: ProviderContext, key: str) -> Record:
    store = ctx.store.collection("conversations")
    record = store.get(key)
    if record is None:
        record = store.first(unique_name=key)
    if record is None:
        raise not_found(f"/v1/Conversations/{key}")
    return record


def build_conversation(ctx: ProviderContext, params: dict[str, str], sid_: str | None = None) -> Record:
    now = ctx.clock.now()
    account = creds(ctx)["account_sid"]
    return {
        "id": sid_ or new_sid(ctx, "CH", "conversations"),
        "sid": sid_ or "",
        "account_sid": account,
        "chat_service_sid": ctx.store.meta.get("chat_service_sid", ""),
        "friendly_name": get_param(params, "FriendlyName"),
        "unique_name": get_param(params, "UniqueName"),
        "attributes": get_param(params, "Attributes") or "{}",
        "messaging_service_sid": get_param(params, "MessagingServiceSid"),
        "date_created": iso8601(now),
        "date_updated": iso8601(now),
        "state": (get_param(params, "State") or "active").lower(),
        "timers": {"date_inactive": None, "date_closed": None},
        "bindings": {},
        "_account": account,
    }


def register(app: FastAPI, ctx: ProviderContext) -> None:
    account_sid = creds(ctx)["account_sid"]

    @app.get("/v1/Conversations")
    async def list_conversations(request: Request) -> Any:
        require_auth(request, ctx)
        rows = [render_conversation(c, request, ctx) for c in ctx.store.collection("conversations").values()]
        return page_meta(request, ctx, CONVERSATIONS_HOST, "conversations", rows, "/v1/Conversations")

    @app.post("/v1/Conversations")
    async def create_conversation(request: Request) -> Response:
        require_auth(request, ctx)
        params, _ = await form_params(request)
        unique = get_param(params, "UniqueName")
        if unique and ctx.store.collection("conversations").first(unique_name=unique) is not None:
            raise fail(400, 50412, "A Conversation with this UniqueName already exists.")
        record = build_conversation(ctx, params)
        record["sid"] = record["id"]
        ctx.store.collection("conversations").put(record)
        return JSONResponse(render_conversation(record, request, ctx), status_code=201)

    @app.get("/v1/Conversations/{sid}")
    async def get_conversation(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        return render_conversation(_conv_or_404(ctx, sid), request, ctx)

    @app.post("/v1/Conversations/{sid}")
    async def update_conversation(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        record = _conv_or_404(ctx, sid)
        params, _ = await form_params(request)
        for pascal, snake in (
            ("FriendlyName", "friendly_name"),
            ("UniqueName", "unique_name"),
            ("Attributes", "attributes"),
            ("MessagingServiceSid", "messaging_service_sid"),
            ("State", "state"),
        ):
            value = param_value(params, pascal)
            if value is not None:
                record[snake] = value or None
        touch(record, ctx.clock.now(), rfc=False)
        return render_conversation(record, request, ctx)

    @app.delete("/v1/Conversations/{sid}")
    async def delete_conversation(request: Request, sid: str) -> Response:
        require_auth(request, ctx)
        record = _conv_or_404(ctx, sid)
        ctx.store.collection("conversations").delete(record["sid"])
        for coll in ("conv_participants", "conv_messages"):
            store = ctx.store.collection(coll)
            for item in [i for i in store.values() if i["conversation_sid"] == record["sid"]]:
                store.delete(item["id"])
        return Response(status_code=204)

    # -- participants ------------------------------------------------------------
    @app.get("/v1/Conversations/{sid}/Participants")
    async def list_participants(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        conv = _conv_or_404(ctx, sid)
        rows = [
            render_participant(p, request, ctx)
            for p in ctx.store.collection("conv_participants").values()
            if p["conversation_sid"] == conv["sid"]
        ]
        return page_meta(
            request,
            ctx,
            CONVERSATIONS_HOST,
            "participants",
            rows,
            f"/v1/Conversations/{conv['sid']}/Participants",
        )

    @app.post("/v1/Conversations/{sid}/Participants")
    async def add_participant(request: Request, sid: str) -> Response:
        require_auth(request, ctx)
        conv = _conv_or_404(ctx, sid)
        params, _ = await form_params(request)
        identity = get_param(params, "Identity")
        address = get_param(params, "MessagingBinding.Address")
        proxy = get_param(params, "MessagingBinding.ProxyAddress")
        if not identity and not address:
            raise fail(400, 50400, "Identity or MessagingBinding.Address is required.")
        now = ctx.clock.now()
        record: Record = {
            "id": new_sid(ctx, "MB", "participants"),
            "sid": "",
            "account_sid": account_sid,
            "conversation_sid": conv["sid"],
            "identity": identity,
            "attributes": get_param(params, "Attributes") or "{}",
            "messaging_binding": _binding(address, proxy) if address else None,
            "role_sid": get_param(params, "RoleSid") or ctx.store.meta.get("participant_role_sid", ""),
            "date_created": iso8601(now),
            "date_updated": iso8601(now),
            "last_read_message_index": None,
            "last_read_timestamp": None,
        }
        record["sid"] = record["id"]
        ctx.store.collection("conv_participants").put(record)
        return JSONResponse(render_participant(record, request, ctx), status_code=201)

    @app.get("/v1/Conversations/{sid}/Participants/{participant_sid}")
    async def get_participant(request: Request, sid: str, participant_sid: str) -> Any:
        require_auth(request, ctx)
        conv = _conv_or_404(ctx, sid)
        record = ctx.store.collection("conv_participants").get(participant_sid)
        if record is None or record["conversation_sid"] != conv["sid"]:
            raise not_found(f"/v1/Conversations/{conv['sid']}/Participants/{participant_sid}")
        return render_participant(record, request, ctx)

    @app.post("/v1/Conversations/{sid}/Participants/{participant_sid}")
    async def update_participant(request: Request, sid: str, participant_sid: str) -> Any:
        require_auth(request, ctx)
        conv = _conv_or_404(ctx, sid)
        record = ctx.store.collection("conv_participants").get(participant_sid)
        if record is None or record["conversation_sid"] != conv["sid"]:
            raise not_found(f"/v1/Conversations/{conv['sid']}/Participants/{participant_sid}")
        params, _ = await form_params(request)
        index = param_value(params, "LastReadMessageIndex")
        if index is not None:
            record["last_read_message_index"] = parse_int(index, 0)
        attrs = param_value(params, "Attributes")
        if attrs is not None:
            record["attributes"] = attrs
        role = param_value(params, "RoleSid")
        if role is not None:
            record["role_sid"] = role
        touch(record, ctx.clock.now(), rfc=False)
        return render_participant(record, request, ctx)

    @app.delete("/v1/Conversations/{sid}/Participants/{participant_sid}")
    async def delete_participant(request: Request, sid: str, participant_sid: str) -> Response:
        require_auth(request, ctx)
        conv = _conv_or_404(ctx, sid)
        record = ctx.store.collection("conv_participants").get(participant_sid)
        if record is None or record["conversation_sid"] != conv["sid"]:
            raise not_found(f"/v1/Conversations/{conv['sid']}/Participants/{participant_sid}")
        ctx.store.collection("conv_participants").delete(participant_sid)
        return Response(status_code=204)

    # -- messages ------------------------------------------------------------------
    @app.get("/v1/Conversations/{sid}/Messages")
    async def list_conv_messages(request: Request, sid: str) -> Any:
        require_auth(request, ctx)
        conv = _conv_or_404(ctx, sid)
        records = [
            m for m in ctx.store.collection("conv_messages").values() if m["conversation_sid"] == conv["sid"]
        ]
        records.sort(key=lambda m: m["index"])
        rows = [render_conv_message(m, request, ctx) for m in records]
        return page_meta(
            request, ctx, CONVERSATIONS_HOST, "messages", rows, f"/v1/Conversations/{conv['sid']}/Messages"
        )

    @app.post("/v1/Conversations/{sid}/Messages")
    async def create_conv_message(request: Request, sid: str) -> Response:
        require_auth(request, ctx)
        conv = _conv_or_404(ctx, sid)
        params, _ = await form_params(request)
        body = param_value(params, "Body")
        author = get_param(params, "Author") or "system"
        participants = [
            p
            for p in ctx.store.collection("conv_participants").values()
            if p["conversation_sid"] == conv["sid"]
        ]
        participant = next(
            (
                p
                for p in participants
                if p.get("identity") == author or (p.get("messaging_binding") or {}).get("address") == author
            ),
            None,
        )
        existing = [
            m for m in ctx.store.collection("conv_messages").values() if m["conversation_sid"] == conv["sid"]
        ]
        index = max((m["index"] for m in existing), default=-1) + 1
        now = ctx.clock.now()
        record: Record = {
            "id": new_sid(ctx, "IM", "conv_messages"),
            "sid": "",
            "account_sid": account_sid,
            "conversation_sid": conv["sid"],
            "author": author,
            "body": body,
            "attributes": get_param(params, "Attributes") or "{}",
            "participant_sid": participant["sid"] if participant else None,
            "media": None,
            "content_sid": get_param(params, "ContentSid"),
            "index": index,
            "delivery": {
                "total": len(participants),
                "sent": 0,
                "delivered": 0,
                "read": 0,
                "failed": 0,
                "undelivered": 0,
            },
            "date_created": iso8601(now),
            "date_updated": iso8601(now),
        }
        record["sid"] = record["id"]
        ctx.store.collection("conv_messages").put(record)
        touch(conv, now, rfc=False)
        return JSONResponse(render_conv_message(record, request, ctx), status_code=201)

    @app.get("/v1/Conversations/{sid}/Messages/{message_sid}")
    async def get_conv_message(request: Request, sid: str, message_sid: str) -> Any:
        require_auth(request, ctx)
        conv = _conv_or_404(ctx, sid)
        record = ctx.store.collection("conv_messages").get(message_sid)
        if record is None or record["conversation_sid"] != conv["sid"]:
            raise not_found(f"/v1/Conversations/{conv['sid']}/Messages/{message_sid}")
        return render_conv_message(record, request, ctx)

    @app.post("/v1/Conversations/{sid}/Messages/{message_sid}")
    async def update_conv_message(request: Request, sid: str, message_sid: str) -> Any:
        require_auth(request, ctx)
        conv = _conv_or_404(ctx, sid)
        record = ctx.store.collection("conv_messages").get(message_sid)
        if record is None or record["conversation_sid"] != conv["sid"]:
            raise not_found(f"/v1/Conversations/{conv['sid']}/Messages/{message_sid}")
        params, _ = await form_params(request)
        body = param_value(params, "Body")
        if body is not None:
            record["body"] = body
        attrs = param_value(params, "Attributes")
        if attrs is not None:
            try:
                json.loads(attrs)
            except ValueError:
                raise fail(400, 50400, "Attributes must be valid JSON.") from None
            record["attributes"] = attrs
        touch(record, ctx.clock.now(), rfc=False)
        return render_conv_message(record, request, ctx)

    @app.delete("/v1/Conversations/{sid}/Messages/{message_sid}")
    async def delete_conv_message(request: Request, sid: str, message_sid: str) -> Response:
        require_auth(request, ctx)
        conv = _conv_or_404(ctx, sid)
        record = ctx.store.collection("conv_messages").get(message_sid)
        if record is None or record["conversation_sid"] != conv["sid"]:
            raise not_found(f"/v1/Conversations/{conv['sid']}/Messages/{message_sid}")
        ctx.store.collection("conv_messages").delete(message_sid)
        return Response(status_code=204)


def _binding(address: str, proxy: str | None) -> dict[str, Any]:
    kind = "whatsapp" if address.startswith("whatsapp:") else "sms"
    return {
        "type": kind,
        "address": address,
        "proxy_address": proxy,
        "projected_address": None,
        "level": None,
    }


__all__ = ["register"]
