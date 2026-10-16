"""Keep Notion response request IDs consistent across successes and errors."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from emulators.providers.notion.common import REQUEST_ID


class NotionHeaders:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        token = REQUEST_ID.set(request_id)

        async def response(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                if not any(key.lower() == b"x-request-id" for key, _ in headers):
                    headers.append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, response)
        finally:
            REQUEST_ID.reset(token)
