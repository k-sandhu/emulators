"""Zoom retries server/transport failures, while redirects and client errors are terminal."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from emulators.core.clock import isoformat_z
from emulators.core.webhooks import WebhookDispatcher


class ZoomWebhookDispatcher(WebhookDispatcher):
    async def _attempt_all(
        self, record: dict[str, Any], body: bytes, sign: Callable[[bytes, int], dict[str, str]] | None
    ) -> None:
        for attempt in range(1, self.max_attempts + 1):
            record["attempts"] = attempt
            headers = dict(record["headers"])
            if sign:
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
                    record.update(status="delivered", delivered_at=isoformat_z(self.clock.now()), error=None)
                    return
                record["error"] = f"HTTP {response.status_code}"
                if response.status_code < 500:
                    break
            except httpx.HTTPError as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"[:500]
            if attempt < self.max_attempts and self.mode == "async":
                # Keep tests fast; Zoom's production retry delays are 5, 20 and 60 minutes.
                await asyncio.sleep(min(0.25 * 2**attempt, 2.0))
        record["status"] = "failed"
