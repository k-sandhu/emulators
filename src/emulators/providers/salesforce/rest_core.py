"""REST plumbing shared by the sObject, query, composite and bulk handlers.

A tiny router maps ``(method, path-below-the-version)`` to handlers that take an
:class:`ApiRequest` and return an :class:`ApiResponse`. The same router serves real HTTP
requests and the sub-requests of ``/composite`` and ``/composite/batch``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

from emulators.providers.salesforce.common import SfError, bad_request, not_found
from emulators.providers.salesforce.dml import Dml, Txn
from emulators.providers.salesforce.org import Org, Session
from emulators.providers.salesforce.sharing import Access

if TYPE_CHECKING:
    from starlette.requests import Request

    from emulators.core.provider import ProviderContext

JSON = "application/json;charset=UTF-8"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"Non-standard token '{name}'")


def json_error(message: str) -> SfError:
    return bad_request("JSON_PARSER_ERROR", message)


def require_dict(value: Any, what: str = "request") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise json_error(f"Cannot deserialize instance of object from the {what}: expected a JSON object")
    return value


def require_list(value: Any, what: str = "request") -> list[Any]:
    if not isinstance(value, list):
        raise json_error(f"Cannot deserialize instance of array from the {what}: expected a JSON array")
    return value


def require_str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise json_error(f"Cannot deserialize instance of string for {what}")
    return value


def bool_field(body: dict[str, Any], name: str, default: bool = False) -> bool:
    value = body.get(name, default)
    if not isinstance(value, bool):
        raise json_error(f"Cannot deserialize instance of boolean for {name}")
    return value


def int_param(value: Any, name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """An integer query parameter / body value, or 400 INVALID_PARAMETER_VALUE."""
    try:
        if isinstance(value, bool):
            raise ValueError
        number = int(str(value).strip()) if not isinstance(value, int) else value
    except (TypeError, ValueError):
        raise bad_request(
            "INVALID_PARAMETER_VALUE", f"Invalid value for parameter '{name}': {value}"
        ) from None
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        raise bad_request("INVALID_PARAMETER_VALUE", f"Parameter '{name}' is out of range: {number}")
    return number


@dataclass
class ApiResponse:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    media_type: str = JSON


@dataclass
class ApiRequest:
    method: str
    path: str  # below /services/data/vNN.N, no leading/trailing slash
    session: Session
    api: Api
    txn: Txn
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    params: dict[str, str] = field(default_factory=dict)
    request: Request | None = None
    top_level: bool = True
    force_all_or_none: bool = False

    @property
    def version(self) -> str:
        return self.session.version

    @property
    def org(self) -> Org:
        return self.api.org

    @property
    def access(self) -> Access:
        return self.api.access_for(self.session)

    def dml(self) -> Dml:
        return Dml(self.org, self.access, self.txn)

    def q(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def json(self, *, required: bool = True) -> Any:
        if not self.body.strip():
            if required:
                raise bad_request("JSON_PARSER_ERROR", "No content to map to Object due to end of input")
            return None
        try:
            return json.loads(self.body.decode("utf-8-sig"), parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise bad_request(
                "JSON_PARSER_ERROR",
                f"Unexpected character ('{self.body.decode('utf-8', 'replace')[:1]}'): was expecting double-quote "
                f"to start field name\n at [Source: (org.apache.catalina.connector.CoyoteInputStream); line: 1, column: 3]",
            ) from exc

    def obj_json(self) -> dict[str, Any]:
        data = self.json()
        if not isinstance(data, dict):
            raise bad_request(
                "JSON_PARSER_ERROR", "Cannot deserialize instance of object from the request body"
            )
        return data


Handler = Callable[[ApiRequest], "ApiResponse | Awaitable[ApiResponse]"]


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        regex = re.sub(r"\{(\w+):path\}", r"(?P<\1>.+)", pattern)
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", regex)
        self.routes.append((method.upper(), re.compile(f"^{regex}$"), handler))

    def route(self, method: str, *patterns: str) -> Callable[[Handler], Handler]:
        def decorator(fn: Handler) -> Handler:
            for pattern in patterns:
                self.add(method, pattern, fn)
            return fn

        return decorator

    def match(self, method: str, path: str) -> tuple[Handler, dict[str, str]]:
        allowed: list[str] = []
        for m, regex, handler in self.routes:
            hit = regex.match(path)
            if hit:
                if m == method.upper():
                    return handler, hit.groupdict()
                if m not in allowed:
                    allowed.append(m)
        if allowed:
            listing = ",".join(["HEAD", *allowed] if "GET" in allowed else allowed)
            error = SfError(
                405,
                "METHOD_NOT_ALLOWED",
                f"HTTP Method '{method.upper()}' not allowed. Allowed are {listing}",
            )
            error.headers["Allow"] = listing
            raise error
        raise not_found()


class Api:
    """Per-provider REST state: the router, the org and per-user access caches."""

    def __init__(self, ctx: ProviderContext) -> None:
        from emulators.providers.salesforce.notify import Notifier

        self.ctx = ctx
        self.org = Org(ctx)
        self.router = Router()
        self.notifier = Notifier(ctx, self.org)

    def access_for(self, session: Session) -> Access:
        cache = getattr(session, "_access", None)
        if cache is None:
            cache = Access(self.org, session.user)
            session._access = cache  # type: ignore[attr-defined]
        return cache

    async def dispatch(self, req: ApiRequest) -> ApiResponse:
        """Route and run one request; ``SfError`` becomes the error-array response (and undoes its writes)."""
        savepoint = req.txn.savepoint()
        try:
            handler, params = self.router.match(req.method, req.path)
            req.params = params
            result = handler(req)
            if isinstance(result, Awaitable):
                result = await result
            return result
        except SfError as exc:
            req.txn.rollback(savepoint)
            return ApiResponse(exc.status, exc.body, dict(exc.headers))

    # -- sub-requests (composite, batch) -----------------------------------------------
    def sub_request(self, parent: ApiRequest, method: str, url: str, body: Any = None) -> ApiRequest:
        """Build a request for ``url`` (``/services/data/vNN.N/...`` or ``vNN.N/...``) under the parent's session."""
        parts = urlsplit(url)
        path = unquote(parts.path)  # sub-request URLs arrive percent-encoded inside JSON bodies
        if path.startswith("/services/data/"):
            path = path[len("/services/data/") :]
        path = path.lstrip("/")
        segments = path.split("/", 1)
        version = parent.session.version
        if re.fullmatch(r"v\d{2}\.0", segments[0]):
            version = segments[0][1:]
            path = segments[1] if len(segments) > 1 else ""
        session = parent.session
        if version != session.version:
            session = Session(session.token, session.grant, session.user, version)
            session._access = getattr(parent.session, "_access", None)  # type: ignore[attr-defined]
        raw = b"" if body is None else json.dumps(body).encode()
        return ApiRequest(
            method=method.upper(),
            path=path.strip("/"),
            session=session,
            api=self,
            txn=parent.txn,
            query=parse_qs(parts.query, keep_blank_values=True),
            headers=parent.headers,
            body=raw,
            request=parent.request,
            top_level=False,
        )


def json_response(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> ApiResponse:
    return ApiResponse(status, body, headers or {})


def no_content() -> ApiResponse:
    return ApiResponse(204, None)
