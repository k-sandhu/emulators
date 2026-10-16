"""Reroute vendor hostnames to a running :class:`EmulatorServer` — no code changes needed.

Official SDKs (stripe, twilio, dropbox, box, googleapiclient, simple-salesforce,
slack_sdk, python-quickbooks...) hard-code ``https://api.vendor.com`` and build their own
HTTP sessions, so a transport seam isn't available. ``intercept(server)`` patches the
HTTP stacks they use:

* ``httpx`` (sync and async transports)
* ``requests`` (``HTTPAdapter.send``)
* ``urllib.request`` (``OpenerDirector.open`` — slack_sdk's sync client)
* ``httplib2`` (google-api-python-client)
* ``aiohttp`` (slack_sdk's async client), when installed

A request to a hostname some provider claims is rewritten to
``http://127.0.0.1:<port>`` with the original ``Host`` and ``X-Forwarded-Proto``/
``X-Forwarded-Host`` headers, so the emulator routes it by hostname and generates
absolute URLs on the *real* hostname (which are intercepted again when followed).
Everything else passes through untouched.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from emulators.testing.server import EmulatorServer

_lock = threading.RLock()
_active: list[EmulatorServer] = []
_originals: dict[str, Any] = {}


def _target(url: str) -> tuple[str, dict[str, str]] | None:
    """New URL and extra headers if ``url`` points at an emulated host."""
    if not _active:
        return None
    server = _active[-1]
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host or server.hub.provider_for_host(host) is None:
        return None
    new = urlunsplit(("http", f"{server.host}:{server.port}", parts.path or "/", parts.query, ""))
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return new, {"Host": netloc, "X-Forwarded-Host": netloc, "X-Forwarded-Proto": parts.scheme or "https"}


def _patch_httpx() -> None:
    import httpx

    sync_orig = httpx.HTTPTransport.handle_request
    async_orig = httpx.AsyncHTTPTransport.handle_async_request
    _originals["httpx.sync"] = sync_orig
    _originals["httpx.async"] = async_orig

    def rewrite(request: httpx.Request) -> None:
        target = _target(str(request.url))
        if target is not None:
            new_url, headers = target
            request.url = httpx.URL(new_url)
            for key, value in headers.items():
                request.headers[key] = value

    def handle_request(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        rewrite(request)
        return sync_orig(self, request)

    async def handle_async_request(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        rewrite(request)
        return await async_orig(self, request)

    httpx.HTTPTransport.handle_request = handle_request  # type: ignore[method-assign]
    httpx.AsyncHTTPTransport.handle_async_request = handle_async_request  # type: ignore[method-assign]


def _patch_requests() -> None:
    try:
        import requests.adapters
    except ImportError:
        return
    orig = requests.adapters.HTTPAdapter.send
    _originals["requests"] = orig

    def send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        target = _target(request.url)
        if target is not None:
            request.url, headers = target
            request.headers.update(headers)
        return orig(self, request, *args, **kwargs)

    requests.adapters.HTTPAdapter.send = send  # type: ignore[method-assign]


def _patch_urllib() -> None:
    import urllib.request

    orig = urllib.request.OpenerDirector.open
    _originals["urllib"] = orig

    def open_(self: Any, fullurl: Any, data: Any = None, timeout: Any = None, **kwargs: Any) -> Any:
        req = (
            fullurl if isinstance(fullurl, urllib.request.Request) else urllib.request.Request(fullurl, data)
        )
        target = _target(req.full_url)
        if target is not None:
            new_url, headers = target
            req.full_url = new_url
            for key, value in headers.items():
                req.add_unredirected_header(key, value) if key == "Host" else req.add_header(key, value)
        if timeout is None:
            return orig(self, req, data, **kwargs)
        return orig(self, req, data, timeout, **kwargs)

    urllib.request.OpenerDirector.open = open_  # type: ignore[method-assign]


def _patch_httplib2() -> None:
    try:
        import httplib2
    except ImportError:
        return
    orig = httplib2.Http.request
    _originals["httplib2"] = orig

    def request(
        self: Any,
        uri: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        target = _target(uri)
        if target is not None:
            uri, extra = target
            headers = {**(headers or {}), **{k.lower(): v for k, v in extra.items()}}
        return orig(self, uri, method, body, headers, *args, **kwargs)

    httplib2.Http.request = request  # type: ignore[method-assign]


def _patch_aiohttp() -> None:
    try:
        import aiohttp
    except ImportError:
        return
    orig = aiohttp.ClientSession._request
    _originals["aiohttp"] = orig

    async def _request(self: Any, method: str, str_or_url: Any, **kwargs: Any) -> Any:
        target = _target(str(str_or_url))
        if target is not None:
            str_or_url, extra = target
            kwargs["headers"] = {**(kwargs.get("headers") or {}), **extra}
            kwargs.pop("ssl", None)
        return await orig(self, method, str_or_url, **kwargs)

    aiohttp.ClientSession._request = _request  # type: ignore[method-assign]


_PATCHERS: list[Callable[[], None]] = [
    _patch_httpx,
    _patch_requests,
    _patch_urllib,
    _patch_httplib2,
    _patch_aiohttp,
]


def _install() -> None:
    for patch in _PATCHERS:
        patch()


def _uninstall() -> None:
    import httpx

    if "httpx.sync" in _originals:
        httpx.HTTPTransport.handle_request = _originals.pop("httpx.sync")  # type: ignore[method-assign]
        httpx.AsyncHTTPTransport.handle_async_request = _originals.pop("httpx.async")  # type: ignore[method-assign]
    if "requests" in _originals:
        import requests.adapters

        requests.adapters.HTTPAdapter.send = _originals.pop("requests")  # type: ignore[method-assign]
    if "urllib" in _originals:
        import urllib.request

        urllib.request.OpenerDirector.open = _originals.pop("urllib")  # type: ignore[method-assign]
    if "httplib2" in _originals:
        import httplib2

        httplib2.Http.request = _originals.pop("httplib2")  # type: ignore[method-assign]
    if "aiohttp" in _originals:
        import aiohttp

        aiohttp.ClientSession._request = _originals.pop("aiohttp")  # type: ignore[method-assign]


@contextlib.contextmanager
def intercept(server: EmulatorServer) -> Iterator[EmulatorServer]:
    """Route vendor hostnames to ``server`` for the duration of the block (re-entrant)."""
    with _lock:
        if not _active:
            _install()
        _active.append(server)
    try:
        yield server
    finally:
        with _lock:
            _active.remove(server)
            if not _active:
                _uninstall()
