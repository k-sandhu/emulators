"""ASGI middleware shared by every provider app.

* :class:`EmulatorMiddleware` resolves the *external* base URL the client used (needed to
  build absolute ``@odata.nextLink``/``download_url`` values that work whether the
  emulator is reached as ``https://graph.microsoft.com``, ``http://localhost:44080/microsoft``
  or a dedicated port), records every request in the :class:`RequestJournal`, and applies
  :class:`FaultRule` s (status codes, latency, throttling) before the route runs.

It is pure ASGI rather than ``BaseHTTPMiddleware`` so streaming downloads and large
uploads pass through untouched.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import random
import re
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from emulators.core.clock import Clock, isoformat_z

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

ADMIN_PREFIX = "/_emulator"
MAX_BODY_CAPTURE = 64 * 1024
REDACT_HEADERS = {"authorization", "cookie", "x-api-key"}


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode()
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode("latin-1")
    return None


def external_base(scope: Scope) -> str:
    """``scheme://host[:port][root_path]`` as seen by the client."""
    proto = _header(scope, "x-forwarded-proto") or scope.get("scheme", "http")
    host = _header(scope, "x-forwarded-host") or _header(scope, "host")
    if not host:
        server = scope.get("server") or ("localhost", 80)
        host = f"{server[0]}:{server[1]}"
    root = scope.get("root_path", "") or ""
    return f"{proto}://{host}{root}"


@dataclass
class FaultRule:
    """Make matching requests fail (or slow down) — see ``POST /_emulator/faults``."""

    path: str = ".*"
    method: str | None = None
    status: int | None = 503
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    times: int | None = 1  # remaining matches; None = unlimited
    after: int = 0  # let this many matches through first
    probability: float = 1.0
    delay_ms: int = 0
    id: str = ""
    hits: int = 0

    def matches(self, method: str, path: str) -> bool:
        if self.method and self.method.upper() != method.upper():
            return False
        return re.search(self.path, path) is not None


class FaultInjector:
    def __init__(self) -> None:
        self._rules: list[FaultRule] = []
        self._lock = threading.Lock()
        self._counter = 0

    def add(self, rule: FaultRule) -> FaultRule:
        with self._lock:
            self._counter += 1
            rule.id = rule.id or f"fault_{self._counter}"
            self._rules.append(rule)
        return rule

    def clear(self) -> None:
        with self._lock:
            self._rules.clear()

    def list(self) -> list[dict[str, Any]]:
        return [asdict(r) for r in self._rules]

    def remove(self, rule_id: str) -> bool:
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r.id != rule_id]
            return len(self._rules) != before

    def pick(self, method: str, path: str) -> FaultRule | None:
        with self._lock:
            for rule in self._rules:
                if not rule.matches(method, path):
                    continue
                rule.hits += 1
                if rule.hits <= rule.after:
                    continue
                if rule.probability < 1.0 and random.random() > rule.probability:
                    continue
                if rule.times is not None:
                    if rule.times <= 0:
                        continue
                    rule.times -= 1
                return rule
        return None


class RequestJournal:
    """Bounded log of requests a provider received, newest last."""

    def __init__(self, maxlen: int = 5000) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._entries.append(entry)

    def entries(
        self, *, method: str | None = None, path: str | None = None, since: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._entries)
        result = [
            e
            for e in items
            if e["seq"] > since
            and (method is None or e["method"].upper() == method.upper())
            and (path is None or re.search(path, e["path"]))
        ]
        return result[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _redact(name: str, value: str) -> str:
    if name.lower() in REDACT_HEADERS:
        scheme, _, rest = value.partition(" ")
        tail = rest[-4:] if rest else value[-4:]
        return f"{scheme} …{tail}" if rest else f"…{tail}"
    return value


def _decode_body(body: bytes, content_type: str) -> Any:
    if not body:
        return None
    snippet = body[:MAX_BODY_CAPTURE]
    if "json" in content_type:
        try:
            return json.loads(snippet)
        except (ValueError, RecursionError):
            pass
    try:
        return snippet.decode("utf-8")
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(snippet[:4096]).decode(), "size": len(body)}


class EmulatorMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        provider: str,
        journal: RequestJournal,
        faults: FaultInjector,
        clock: Clock,
        fault_response: Callable[..., tuple[int, dict[str, str], bytes]],
    ) -> None:
        self.app = app
        self.provider = provider
        self.journal = journal
        self.faults = faults
        self.clock = clock
        self.fault_response = fault_response
        try:
            # Providers written before the scope-aware hook take only ``rule``.
            self._fault_takes_scope = len(inspect.signature(fault_response).parameters) >= 2
        except (TypeError, ValueError):
            self._fault_takes_scope = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["external_base"] = external_base(scope)
        state["provider"] = self.provider
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :] or "/"
        method = scope.get("method", "GET")
        is_admin = path.startswith(ADMIN_PREFIX)
        started = time.perf_counter()

        captured = bytearray()

        async def capture_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request" and len(captured) < MAX_BODY_CAPTURE:
                captured.extend(message.get("body", b"")[: MAX_BODY_CAPTURE - len(captured)])
            return message

        status_holder: dict[str, Any] = {"status": None, "headers": {}}

        async def capture_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["headers"] = {
                    k.decode("latin-1"): v.decode("latin-1") for k, v in message.get("headers", [])
                }
            await send(message)

        rule = None if is_admin else self.faults.pick(method, path)
        try:
            if rule is not None and rule.delay_ms:
                await asyncio.sleep(rule.delay_ms / 1000)
            if rule is not None and rule.status:
                # Drain the request body so clients don't see a reset connection.
                more = True
                while more:
                    message = await capture_receive()
                    more = message.get("more_body", False) and message["type"] == "http.request"
                if self._fault_takes_scope:
                    status, headers, body = self.fault_response(rule, scope)  # type: ignore[call-arg]
                else:
                    status, headers, body = self.fault_response(rule)
                raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
                raw_headers.append((b"content-length", str(len(body)).encode()))
                await capture_send({"type": "http.response.start", "status": status, "headers": raw_headers})
                await send({"type": "http.response.body", "body": body})
            else:
                await self.app(scope, capture_receive, capture_send)
        finally:
            if not is_admin:
                headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
                self.journal.append(
                    {
                        "provider": self.provider,
                        "time": isoformat_z(self.clock.now(), timespec="milliseconds"),
                        "method": method,
                        "host": headers.get("host", ""),
                        "path": path,
                        "query": scope.get("query_string", b"").decode("latin-1"),
                        "headers": {k: _redact(k, v) for k, v in headers.items()},
                        "body": _decode_body(bytes(captured), headers.get("content-type", "")),
                        "status": status_holder["status"],
                        "fault": rule.id if rule else None,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )
