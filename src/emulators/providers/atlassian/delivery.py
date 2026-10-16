"""Jira retries differ from the shared dispatcher's generic retry policy."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

from emulators.core.clock import isoformat_z
from emulators.core.webhooks import WebhookDispatcher

from .common import Record


class AtlassianWebhooks(WebhookDispatcher):
    async def _attempt_all(
        self, record: Record, body: bytes, sign: Callable[[bytes, int], dict[str, str]] | None
    ) -> None:
        if not record["event_type"].startswith("jira:"):
            await super()._attempt_all(record, body, sign)
            return
        for attempt in range(1, 7):
            record["attempts"] = attempt
            headers = dict(record["headers"])
            if attempt > 1:
                headers["X-Atlassian-Webhook-Retry"] = str(attempt - 1)
            if sign is not None:
                headers.update(sign(body, attempt))
            record["headers"] = headers
            retry = True
            try:
                async with self._client() as client:
                    response = await client.request(
                        record["method"], record["url"], headers=headers, content=body
                    )
                record["response_status"] = response.status_code
                record["response_body"] = response.text[:2000]
                if 200 <= response.status_code < 300:
                    record.update(
                        {"status": "delivered", "delivered_at": isoformat_z(self.clock.now()), "error": None}
                    )
                    return
                record["error"] = f"HTTP {response.status_code}"
                retry = response.status_code in (408, 409, 425, 429) or 500 <= response.status_code < 600
            except httpx.HTTPError as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"[:500]
            if not retry:
                break
            if attempt < 6 and self.mode == "async":
                # Keep integration tests quick while preserving attempt and header semantics.
                await asyncio.sleep(min(0.25 * 2**attempt, 2.0))
        record["status"] = "failed"
