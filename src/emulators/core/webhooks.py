"""Outgoing webhook delivery with a journal tests can assert on.

Providers build the payload and the vendor-specific signature headers (Stripe
``Stripe-Signature``, Zoom ``x-zm-signature``, Twilio ``X-Twilio-Signature``, Meta
``X-Hub-Signature-256``, Dropbox ``X-Dropbox-Signature``, Box ``BOX-SIGNATURE-PRIMARY``,
Slack ``X-Slack-Signature``, QuickBooks ``intuit-signature``...) and hand the bytes to
:meth:`WebhookDispatcher.deliver`.

Delivery modes (``EMULATORS_WEBHOOK_MODE``):

* ``async`` (default) — fire-and-forget background task with retries, like the real thing;
* ``sync`` — the triggering request waits for the delivery, which makes tests deterministic;
* ``manual`` — deliveries queue up until ``POST /_emulator/webhooks/flush``.

Tests running a consumer app in-process can set ``hub.webhook_transport`` to an
``httpx.ASGITransport`` so deliveries never touch the network.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
from collections.abc import Callable
from typing import Any

import httpx

from emulators.core.clock import Clock, isoformat_z
from emulators.core.ids import IdFactory
from emulators.core.store import Collection, Store

DELIVERIES = "_webhook_deliveries"


def hmac_sha256_hex(secret: str | bytes, message: str | bytes) -> str:
    key = secret.encode() if isinstance(secret, str) else secret
    msg = message.encode() if isinstance(message, str) else message
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def hmac_sha256_b64(secret: str | bytes, message: str | bytes) -> str:
    key = secret.encode() if isinstance(secret, str) else secret
    msg = message.encode() if isinstance(message, str) else message
    return base64.b64encode(hmac.new(key, msg, hashlib.sha256).digest()).decode()


def hmac_sha1_b64(secret: str | bytes, message: str | bytes) -> str:
    key = secret.encode() if isinstance(secret, str) else secret
    msg = message.encode() if isinstance(message, str) else message
    return base64.b64encode(hmac.new(key, msg, hashlib.sha1).digest()).decode()


class WebhookDispatcher:
    def __init__(
        self,
        provider: str,
        store: Store,
        clock: Clock,
        ids: IdFactory,
        *,
        mode: str = "async",
        transport_factory: Callable[[], httpx.AsyncBaseTransport | None] | None = None,
        max_attempts: int = 3,
        timeout_s: float = 10.0,
    ) -> None:
        self.provider = provider
        self.store = store
        self.clock = clock
        self.ids = ids
        self.mode = mode
        self.transport_factory = transport_factory or (lambda: None)
        self.max_attempts = max_attempts
        self.timeout_s = timeout_s
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending: dict[str, tuple[bytes, Callable[[bytes, int], dict[str, str]] | None]] = {}

    @property
    def deliveries(self) -> Collection:
        return self.store.collection(DELIVERIES)

    def _client(self) -> httpx.AsyncClient:
        transport = self.transport_factory()
        return httpx.AsyncClient(transport=transport, timeout=self.timeout_s, follow_redirects=False)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """A synchronous call to a subscriber, e.g. a subscription validation handshake."""
        async with self._client() as client:
            return await client.request(method, url, headers=headers, content=content, params=params)

    async def deliver(
        self,
        url: str,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        event_type: str = "",
        method: str = "POST",
        content_type: str = "application/json",
        metadata: dict[str, Any] | None = None,
        sign: Callable[[bytes, int], dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Record and dispatch one delivery according to ``self.mode``.

        ``sign(body, attempt)`` may return per-attempt headers (timestamps change between
        retries for Stripe/Slack/Zoom, as they do in production).
        """
        record = {
            "id": self.ids.prefixed("whd", 20),
            "provider": self.provider,
            "url": url,
            "method": method,
            "event_type": event_type,
            "headers": {"Content-Type": content_type, **(headers or {})},
            "body": body.decode("utf-8", errors="replace"),
            "status": "pending",
            "attempts": 0,
            "response_status": None,
            "response_body": None,
            "error": None,
            "created_at": isoformat_z(self.clock.now()),
            "delivered_at": None,
            "metadata": metadata or {},
        }
        self.deliveries.put(record)
        if self.mode == "manual":
            self._pending[record["id"]] = (body, sign)
            return record
        if self.mode == "sync":
            await self._attempt_all(record, body, sign)
            return record
        task = asyncio.get_running_loop().create_task(self._attempt_all(record, body, sign))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record

    async def flush(self) -> list[dict[str, Any]]:
        """Deliver everything queued in ``manual`` mode."""
        flushed = []
        for record in self.deliveries:
            if record["status"] == "pending" and record["id"] in self._pending:
                body, sign = self._pending.pop(record["id"])
                await self._attempt_all(record, body, sign)
                flushed.append(record)
        return flushed

    async def drain(self) -> None:
        """Wait for in-flight async deliveries (used by tests and graceful shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _attempt_all(
        self, record: dict[str, Any], body: bytes, sign: Callable[[bytes, int], dict[str, str]] | None
    ) -> None:
        for attempt in range(1, self.max_attempts + 1):
            record["attempts"] = attempt
            headers = dict(record["headers"])
            if sign is not None:
                headers.update(sign(body, attempt))
                record["headers"] = headers
            try:
                async with self._client() as client:
                    response = await client.request(
                        record["method"], record["url"], headers=headers, content=body
                    )
                record["response_status"] = response.status_code
                record["response_body"] = response.text[:2000]
                if 200 <= response.status_code < 300:
                    record["status"] = "delivered"
                    record["delivered_at"] = isoformat_z(self.clock.now())
                    record["error"] = None
                    return
                record["error"] = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"[:500]
            if attempt < self.max_attempts and self.mode == "async":
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(min(0.25 * 2**attempt, 2.0))
        record["status"] = "failed"
