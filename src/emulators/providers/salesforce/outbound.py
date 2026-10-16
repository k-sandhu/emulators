"""SOAP outbound messages require a positive acknowledgement even when HTTP succeeds."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from emulators.core.clock import isoformat_z
from emulators.core.webhooks import WebhookDispatcher

ACK_PATH = (
    "{http://schemas.xmlsoap.org/soap/envelope/}Body/"
    "{http://soap.sforce.com/2005/09/outbound}notificationsResponse/"
    "{http://soap.sforce.com/2005/09/outbound}Ack"
)


class OutboundDispatcher(WebhookDispatcher):
    """Keep the shared dispatcher journal/modes while applying Salesforce's SOAP success condition."""

    @classmethod
    def from_dispatcher(cls, dispatcher: WebhookDispatcher) -> OutboundDispatcher:
        return cls(
            dispatcher.provider,
            dispatcher.store,
            dispatcher.clock,
            dispatcher.ids,
            mode=dispatcher.mode,
            transport_factory=dispatcher.transport_factory,
            max_attempts=dispatcher.max_attempts,
            timeout_s=dispatcher.timeout_s,
        )

    async def _attempt_all(
        self,
        record: dict[str, Any],
        body: bytes,
        sign: Callable[[bytes, int], dict[str, str]] | None,
    ) -> None:
        if not record["event_type"].startswith("outbound."):
            await super()._attempt_all(record, body, sign)
            return
        attempts = int(record.get("max_attempts") or self.max_attempts)
        for attempt in range(1, attempts + 1):
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
                    try:
                        root = ET.fromstring(response.content)
                        ack = root.findtext(ACK_PATH)
                    except ET.ParseError:
                        ack = None
                    if ack is not None and ack.strip() in ("true", "1"):
                        record["status"] = "delivered"
                        record["delivered_at"] = isoformat_z(self.clock.now())
                        record["error"] = None
                        return
                    record["error"] = "Outbound message was not acknowledged with Ack=true"
                else:
                    record["error"] = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"[:500]
            if attempt < attempts and self.mode == "async":
                await asyncio.sleep(min(0.25 * 2**attempt, 2.0))
        record["status"] = "failed"
