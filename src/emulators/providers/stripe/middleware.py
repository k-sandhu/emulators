"""Response headers and atomic, account-scoped idempotency for Stripe POST requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.providers.stripe.common import BaseAPI
from emulators.providers.stripe.wire import VERSION, decode


class StripeMiddleware:
    def __init__(self, app: ASGIApp, ctx: ProviderContext) -> None:
        self.app, self.ctx = app, ctx
        self.locks: dict[str, asyncio.Lock] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        req_id = self.ctx.ids.prefixed("req", 24)
        scope.setdefault("state", {})["stripe_request_id"] = req_id
        version = request.headers.get("stripe-version", VERSION)

        async def headers_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"request-id", req_id.encode()),
                    (b"stripe-version", version.encode()),
                ]
            await send(message)

        key = request.headers.get("idempotency-key")
        if scope["method"] != "POST" or not key or "/_emulator/" in scope["path"]:
            await self.app(scope, receive, headers_send)
            return
        if len(key) > 255:
            await JSONResponse(
                {
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Idempotency keys must be no longer than 255 characters.",
                        "param": "Idempotency-Key",
                    }
                },
                status_code=400,
            )(scope, receive, headers_send)
            return
        body = await request.body()
        try:
            parsed: Any = (
                decode(body)
                if "x-www-form-urlencoded" in request.headers.get("content-type", "")
                else body.hex()
            )
        except Exception:
            parsed = body.hex()
        fingerprint = hashlib.sha256(
            json.dumps(
                [scope["path"], scope.get("query_string", b"").decode(), parsed], sort_keys=True
            ).encode()
        ).hexdigest()
        namespace = hashlib.sha256(
            (
                request.headers.get("authorization", "") + "|" + request.headers.get("stripe-account", "")
            ).encode()
        ).hexdigest()
        storage_key = namespace + ":" + key
        lock = self.locks.setdefault(storage_key, asyncio.Lock())
        async with lock:
            cached = self.ctx.store.collection("_idempotency").get(storage_key)
            if cached and cached["created"] + 86400 <= self.ctx.clock.timestamp():
                cached = None
            if cached:
                collection = request.url.path.split("/v1/", 1)[-1].split("/")[0]
                if collection in {"checkout", "billing_portal", "test_helpers"}:
                    collection = "/".join(request.url.path.split("/v1/", 1)[-1].split("/")[:2])
                try:
                    BaseAPI(self.ctx, request, parsed if isinstance(parsed, dict) else {}).authorize(
                        collection, "write"
                    )
                except EmulatorError as exc:
                    await JSONResponse(exc.body, status_code=exc.status)(scope, receive, headers_send)
                    return
                if cached["fingerprint"] != fingerprint:
                    await JSONResponse(
                        {
                            "error": {
                                "type": "idempotency_error",
                                "message": "Keys for idempotent requests can only be used with the same parameters they were first used with. Try using a key other than '"
                                + key
                                + "' if you meant to execute a different request.",
                            }
                        },
                        status_code=400,
                    )(scope, receive, headers_send)
                    return
                await send(
                    {
                        "type": "http.response.start",
                        "status": cached["status"],
                        "headers": [
                            *(tuple(h) for h in cached["headers"]),
                            (b"idempotent-replayed", b"true"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": cached["body"]})
                return
            messages: list[Message] = []

            async def capture(message: Message) -> None:
                if message["type"] == "http.response.start":
                    message["headers"] = [
                        *message.get("headers", []),
                        (b"request-id", req_id.encode()),
                        (b"stripe-version", version.encode()),
                    ]
                messages.append(message)

            sent = False

            async def replay_receive() -> Message:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            await self.app(scope, replay_receive, capture)
            start = next(m for m in messages if m["type"] == "http.response.start")
            if 200 <= start["status"] < 300 or start["status"] in {402, 500}:
                self.ctx.store.collection("_idempotency").put(
                    {
                        "id": storage_key,
                        "fingerprint": fingerprint,
                        "created": self.ctx.clock.timestamp(),
                        "status": start["status"],
                        "headers": start["headers"],
                        "body": b"".join(
                            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
                        ),
                    }
                )
            for message in messages:
                await send(message)
