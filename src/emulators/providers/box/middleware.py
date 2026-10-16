"""Per-response behaviour shared by every Box route: the ``Box-Request-Id`` header, CORS and a JSON 500.

Box stamps each response with ``Box-Request-Id`` and repeats it as ``request_id`` in error bodies.
It never answers with a bare text 500 either: an unexpected failure is Box's JSON
``internal_server_error`` envelope, so clients keep parsing errors the same way. Browser clients
(Box UI Elements, hand-written fetch code) need CORS: preflights are answered here and every
response to a request carrying an ``Origin`` header allows it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from emulators.providers.box import errors

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

log = logging.getLogger(__name__)
HEADER = b"box-request-id"
ALLOW_METHODS = "GET, POST, PUT, DELETE, OPTIONS, HEAD"
ALLOW_HEADERS = (
    "Authorization, Content-Type, BoxApi, As-User, If-Match, If-None-Match, Digest, Content-Range, "
    "Content-MD5, Range, X-Rep-Hints, X-Box-Client-Name, X-Box-Client-Version"
)
EXPOSE_HEADERS = (
    "Box-Request-Id, Retry-After, Location, ETag, Content-Range, Accept-Ranges, Content-Disposition"
)


def _header(scope: Scope, name: str) -> str | None:
    wanted = name.lower().encode()
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


class BoxResponseMiddleware:
    """Pure ASGI (no ``BaseHTTPMiddleware``) so streaming downloads and uploads pass through."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = errors.new_request_id()
        token = errors.CURRENT_REQUEST_ID.set(rid)
        origin = _header(scope, "origin")
        started = False

        async def send_with_id(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                if not any(name.lower() == HEADER for name, _ in headers):
                    headers.append((HEADER, rid.encode()))
                if origin and not any(name.lower() == b"access-control-allow-origin" for name, _ in headers):
                    headers.append((b"access-control-allow-origin", b"*"))
                    headers.append((b"access-control-expose-headers", EXPOSE_HEADERS.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            if scope["method"] == "OPTIONS" and _header(scope, "access-control-request-method"):
                # CORS preflight (not Box's ``OPTIONS /files/content`` upload pre-check).
                requested = _header(scope, "access-control-request-headers") or ALLOW_HEADERS
                headers = [
                    (b"access-control-allow-origin", b"*"),
                    (b"access-control-allow-methods", ALLOW_METHODS.encode()),
                    (b"access-control-allow-headers", requested.encode()),
                    (b"access-control-max-age", b"600"),
                    (b"content-length", b"0"),
                ]
                await send_with_id({"type": "http.response.start", "status": 204, "headers": headers})
                await send({"type": "http.response.body", "body": b""})
                return
            await self.app(scope, receive, send_with_id)
        except Exception:
            log.exception("unhandled error serving %s %s", scope.get("method"), scope.get("path"))
            if started:
                raise
            body = json.dumps(
                errors.error_body(500, "internal_server_error", "Internal Server Error")
            ).encode()
            headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
            await send_with_id({"type": "http.response.start", "status": 500, "headers": headers})
            await send({"type": "http.response.body", "body": body})
        finally:
            errors.CURRENT_REQUEST_ID.reset(token)
