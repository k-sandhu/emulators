"""Conversations API (conversations.twilio.com/v1): Conversations, Participants, Messages."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.providers.twilio.common import (
    CONVERSATIONS_HOST,
    AuthInfo,
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


def _conv_or_404(ctx: ProviderContext, auth: AuthInfo, key: str) -> Record:
    """A conversation by SID or UniqueName, visible only to the account that owns it."""
    store = ctx.store.collection("conversations")
    record = store.get(key)
    if record is None:
        record = store.first(unique_name=key, account_sid=auth.account_sid)
    if record is None or record["account_sid"] != auth.account_sid:
        raise not_found(f"/v1/Conversations/{key}")
    return record


_STATES = ("active", "inactive", "closed")


def _json_attributes(value: str | None) -> str:
    """``Attributes`` is a JSON *string*; Twilio rejects anything that is not a JSON object."""
    if value is None or value == "":
        return "{}"
    try:
        parsed = json.loads(value)
    except ValueError:
        raise fail(400, 50057, "Attributes must be a valid JSON string.") from None
    if not isinstance(parsed, dict):
        raise fail(400, 50057, "Attributes must be a JSON object.")
    return value


def _state(value: str | None, default: str = "active") -> str:
    state = (value or default).lower()
    if state not in _STATES:
        raise fail(400, 50057, f"Invalid State: {value}. Must be one of active, inactive, closed.")
    return state


def _delivery_summary(non_chat: int, sent: bool = True) -> dict[str, Any] | None:
    """Twilio's per-message delivery summary: counts for *non-chat* recipients, ``all/some/none``."""
    if non_chat == 0:
        return None
    mark = "all" if sent else "none"
    return {
        "total": non_chat,
        "sent": mark,
        "delivered": mark,
        "read": "none",
        "failed": "none",
        "undelivered": "none",
    }


def build_conversation(
    ctx: ProviderContext, params: dict[str, str], account: str, sid_: str | None = None
) -> Record:
    now = ctx.clock.now()
    return {
        "id": sid_ or new_sid(ctx, "CH", "conversations"),
        "sid": sid_ or "",
        "account_sid": account,
        "chat_service_sid": ctx.store.meta.get("chat_service_sid", ""),
        "friendly_name": get_param(params, "FriendlyName"),
        "unique_name": get_param(params, "UniqueName"),
        "attributes": _json_attributes(param_value(params, "Attributes")),
        "messaging_service_sid": get_param(params, "MessagingServiceSid"),
        "date_created": iso8601(now),
        "date_updated": iso8601(now),
        "state": _state(get_param(params, "State")),
        "timers": {"date_inactive": None, "date_closed": None},
        "bindings": {},
        "_account": account,
    }


def register(app: FastAPI, ctx: ProviderContext) -> None:

    @app.get("/v1/Conversations")
    async def list_conversations(request: Request) -> Any:
        auth = require_auth(request, ctx)
        records = [
            c for c in ctx.store.collection("conversations").values() if c["account_sid"] == auth.account_sid
        ]
        wanted = request.query_params.get("State")
        if wanted:
            records = [c for c in records if c["state"] == wanted.lower()]
        rows = [render_conversation(c, request, ctx) for c in records]
        return page_meta(request, ctx, CONVERSATIONS_HOST, "conversations", rows, "/v1/Conversations")

    @app.post("/v1/Conversations")
    async def create_conversation(request: Request) -> Response:
        auth = require_auth(request, ctx)
        params, _ = await form_params(request)
        unique = get_param(params, "UniqueName")
        if unique and ctx.store.collection("conversations").first(unique_name=unique) is not None:
            raise fail(409, 50353, "Conversation with provided unique name already exists")
        record = build_conversation(ctx, params, auth.account_sid)
        record["sid"] = record["id"]
        ctx.store.collection("conversations").put(record)
        return JSONResponse(render_conversation(record, request, ctx), status_code=201)

    @app.get("/v1/Conversations/{sid}")
    async def get_conversation(request: Request, sid: str) -> Any:
        auth = require_auth(request, ctx)
        return render_conversation(_conv_or_404(ctx, auth, sid), request, ctx)

    @app.post("/v1/Conversations/{sid}")
    async def update_conversation(request: Request, sid: str) -> Any:
        auth = require_auth(request, ctx)
        record = _conv_or_404(ctx, auth, sid)
        params, _ = await form_params(request)
        unique = get_param(params, "UniqueName")
        taken = unique and ctx.store.collection("conversations").first(unique_name=unique)
        if unique and unique != record.get("unique_name") and taken is not None:
            raise fail(409, 50353, "Conversation with provided unique name already exists")
        updates: dict[str, Any] = {}
        for pascal, snake in (
            ("FriendlyName", "friendly_name"),
            ("UniqueName", "unique_name"),
            ("MessagingServiceSid", "messaging_service_sid"),
        ):
            value = param_value(params, pascal)
            if value is not None:
                updates[snake] = value or None
        attrs = param_value(params, "Attributes")
        if attrs is not None:
            updates["attributes"] = _json_attributes(attrs)
        state = param_value(params, "State")
        if state:
            updates["state"] = _state(state)
        record.update(updates)
        touch(record, ctx.clock.now(), rfc=False)
        return render_conversation(record, request, ctx)

    @app.delete("/v1/Conversations/{sid}")
    async def delete_conversation(request: Request, sid: str) -> Response:
        auth = require_auth(request, ctx)
        record = _conv_or_404(ctx, auth, sid)
        ctx.store.collection("conversations").delete(record["sid"])
        for coll in ("conv_participants", "conv_messages"):
            store = ctx.store.collection(coll)
            for item in [i for i in store.values() if i["conversation_sid"] == record["sid"]]:
                store.delete(item["id"])
        return Response(status_code=204)

    # -- participants ------------------------------------------------------------
    @app.get("/v1/Conversations/{sid}/Participants")
    async def list_participants(request: Request, sid: str) -> Any:
        auth = require_auth(request, ctx)
        conv = _conv_or_404(ctx, auth, sid)
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
        auth = require_auth(request, ctx)
        conv = _conv_or_404(ctx, auth, sid)
        params, _ = await form_params(request)
        identity = get_param(params, "Identity")
        address = get_param(params, "MessagingBinding.Address")
        proxy = get_param(params, "MessagingBinding.ProxyAddress")
        if not identity and not address:
            raise fail(400, 50400, "Identity or MessagingBinding.Address is required.")
        siblings = [
            p
            for p in ctx.store.collection("conv_participants").values()
            if p["conversation_sid"] == conv["sid"]
        ]
        if address and any((p.get("messaging_binding") or {}).get("address") == address for p in siblings):
            raise fail(
                409,
                50416,
                "A participant with the same messaging binding already exists in this Conversation.",
            )
        if identity and any(p.get("identity") == identity for p in siblings):
            raise fail(409, 50433, "A participant with this Identity already exists in this Conversation.")
        now = ctx.clock.now()
        record: Record = {
            "id": new_sid(ctx, "MB", "participants"),
            "sid": "",
            "account_sid": auth.account_sid,
            "chat_service_sid": conv["chat_service_sid"],
            "conversation_sid": conv["sid"],
            "identity": identity,
            "attributes": _json_attributes(param_value(params, "Attributes")),
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
        auth = require_auth(request, ctx)
        conv = _conv_or_404(ctx, auth, sid)
        record = ctx.store.collection("conv_participants").get(participant_sid)
        if record is None or record["conversation_sid"] != conv["sid"]:
            raise not_found(f"/v1/Conversations/{conv['sid']}/Participants/{participant_sid}")
        return render_participant(record, request, ctx)

    @app.post("/v1/Conversations/{sid}/Participants/{participant_sid}")
    async def update_participant(request: Request, sid: str, participant_sid: str) -> Any:
        auth = require_auth(request, ctx)
        conv = _conv_or_404(ctx, auth, sid)
        record = ctx.store.collection("conv_participants").get(participant_sid)
        if record is None or record["conversation_sid"] != conv["sid"]:
            raise not_found(f"/v1/Conversations/{conv['sid']}/Participants/{participant_sid}")
        params, _ = await form_params(request)
        index = param_value(params, "LastReadMessageIndex")
        if index is not None:
            record["last_read_message_index"] = parse_int(index, 0)
        attrs = param_value(params, "Attributes")
        if attrs is not None:
            record["attributes"] = _json_attributes(attrs)
        role = param_value(params, "RoleSid")
        if role is not None:
            record["role_sid"] = role
        touch(record, ctx.clock.now(), rfc=False)
        return render_participant(record, request, ctx)

    @app.delete("/v1/Conversations/{sid}/Participants/{participant_sid}")
    async def delete_participant(request: Request, sid: str, participant_sid: str) -> Response:
        auth = require_auth(request, ctx)
        conv = _conv_or_404(ctx, auth, sid)
        record = ctx.store.collection("conv_participants").get(participant_sid)
        if record is None or record["conversation_sid"] != conv["sid"]:
            raise not_found(f"/v1/Conversations/{conv['sid']}/Participants/{participant_sid}")
        ctx.store.collection("conv_participants").delete(participant_sid)
        return Response(status_code=204)

    # -- messages ------------------------------------------------------------------
    @app.get("/v1/Conversations/{sid}/Messages")
    async def list_conv_messages(request: Request, sid: str) -> Any:
        auth = require_auth(request, ctx)
        conv = _conv_or_404(ctx, auth, sid)
        records = [
            m for m in ctx.store.collection("conv_messages").values() if m["conversation_sid"] == conv["sid"]
        ]
        descending = (request.query_params.get("Order") or "asc").lower() == "desc"
        records.sort(key=lambda m: m["index"], reverse=descending)
        rows = [render_conv_message(m, request, ctx) for m in records]
        return page_meta(
            request, ctx, CONVERSATIONS_HOST, "messages", rows, f"/v1/Conversations/{conv['sid']}/Messages"
        )

    @app.post("/v1/Conversations/{sid}/Messages")
    async def create_conv_message(request: Request, sid: str) -> Response:
        auth = require_auth(request, ctx)
        conv = _conv_or_404(ctx, auth, sid)
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
        # Indexes are never reused, even after the newest message was deleted.
        index = max(conv.get("_next_index", 0), max((m["index"] for m in existing), default=-1) + 1)
        conv["_next_index"] = index + 1
        non_chat = sum(1 for p in participants if p.get("messaging_binding"))
        now = ctx.clock.now()
        record: Record = {
            "id": new_sid(ctx, "IM", "conv_messages"),
            "sid": "",
            "account_sid": auth.account_sid,
            "chat_service_sid": conv["chat_service_sid"],
            "conversation_sid": conv["sid"],
            "author": author,
            "body": body,
            "attributes": _json_attributes(param_value(params, "Attributes")),
            "participant_sid": participant["sid"] if participant else None,
            "media": None,
            "content_sid": get_param(params, "ContentSid"),
            "index": index,
            "delivery": _delivery_summary(non_chat),
            "date_created": iso8601(now),
            "date_updated": iso8601(now),
        }
        record["sid"] = record["id"]
        ctx.store.collection("conv_messages").put(record)
        touch(conv, now, rfc=False)
        return JSONResponse(render_conv_message(record, request, ctx), status_code=201)

    @app.get("/v1/Conversations/{sid}/Messages/{message_sid}")
    async def get_conv_message(request: Request, sid: str, message_sid: str) -> Any:
        auth = require_auth(request, ctx)
        conv = _conv_or_404(ctx, auth, sid)
        record = ctx.store.collection("conv_messages").get(message_sid)
        if record is None or record["conversation_sid"] != conv["sid"]:
            raise not_found(f"/v1/Conversations/{conv['sid']}/Messages/{message_sid}")
        return render_conv_message(record, request, ctx)

    @app.post("/v1/Conversations/{sid}/Messages/{message_sid}")
    async def update_conv_message(request: Request, sid: str, message_sid: str) -> Any:
        auth = require_auth(request, ctx)
        conv = _conv_or_404(ctx, auth, sid)
        record = ctx.store.collection("conv_messages").get(message_sid)
        if record is None or record["conversation_sid"] != conv["sid"]:
            raise not_found(f"/v1/Conversations/{conv['sid']}/Messages/{message_sid}")
        params, _ = await form_params(request)
        body = param_value(params, "Body")
        if body is not None:
            record["body"] = body
        attrs = param_value(params, "Attributes")
        if attrs is not None:
            record["attributes"] = _json_attributes(attrs)
        touch(record, ctx.clock.now(), rfc=False)
        return render_conv_message(record, request, ctx)

    @app.delete("/v1/Conversations/{sid}/Messages/{message_sid}")
    async def delete_conv_message(request: Request, sid: str, message_sid: str) -> Response:
        auth = require_auth(request, ctx)
        conv = _conv_or_404(ctx, auth, sid)
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
