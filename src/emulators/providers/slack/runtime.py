"""Web API plumbing: parameter parsing, token resolution, scope checks and the method registry.

Every Web API method is ``async def handler(call: Call) -> dict`` registered with
:func:`api`. The dispatcher turns the returned dict into ``{"ok": true, ...}`` and a raised
:class:`SlackError` into ``{"ok": false, "error": "..."}`` with HTTP 200, exactly as Slack
does (only rate limiting uses a non-200 status).
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.message import Message
from typing import Any
from urllib.parse import parse_qsl

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from emulators.core.auth import Grant, bearer_token
from emulators.core.clock import utc
from emulators.core.provider import ProviderContext

JSON_TYPE = "application/json; charset=utf-8"
BOT = "bot"
USER = "user"
APP = "app"


class SlackError(Exception):
    """A Slack ``ok: false`` response. ``extra`` keys are merged into the body."""

    def __init__(self, error: str, **extra: Any) -> None:
        super().__init__(error)
        self.error = error
        self.extra = extra


@dataclass
class UploadedFile:
    name: str
    content_type: str
    data: bytes


class Params:
    """Request parameters merged from the query string and the form/JSON/multipart body.

    Form-encoded values arrive as strings, JSON values keep their type; the accessors
    normalise both so handlers don't care how the client encoded the call.
    """

    def __init__(
        self,
        data: dict[str, Any],
        files: dict[str, UploadedFile],
        *,
        json_body: bool,
        missing_charset: bool = False,
    ) -> None:
        self.data = data
        self.files = files
        self.json_body = json_body
        self.missing_charset = missing_charset

    def has(self, name: str) -> bool:
        return self.data.get(name) is not None

    def raw(self, name: str, default: Any = None) -> Any:
        value = self.data.get(name)
        return default if value is None else value

    def str(self, name: str, default: str | None = None) -> str | None:
        value = self.data.get(name)
        if value is None or value == "":
            return default
        if not isinstance(value, str):
            raise SlackError("invalid_arguments")
        return value

    def need(self, name: str) -> str:
        value = self.str(name)
        if value is None:
            raise SlackError(
                "invalid_arguments",
                response_metadata={"messages": [f"[ERROR] missing required field: {name}"]},
            )
        return value

    def int(
        self, name: str, default: int | None = None, *, lo: int | None = None, hi: int | None = None
    ) -> int | None:
        """Parse an integer; out-of-range values are clamped to ``hi`` and rejected below ``lo``."""
        value = self.data.get(name)
        if value is None or value == "":
            return default
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError
            numeric = float(value)
            if not math.isfinite(numeric) or not numeric.is_integer() or abs(numeric) > 2**63 - 1:
                raise ValueError
            number = int(value) if isinstance(value, (str, int)) else int(numeric)
        except (TypeError, ValueError, OverflowError):
            raise SlackError("invalid_arguments") from None
        if lo is not None and number < lo:
            raise SlackError("invalid_limit" if name == "limit" else "invalid_arguments")
        if hi is not None and number > hi:
            number = hi
        return number

    def bool(self, name: str, default: bool = False) -> bool:
        value = self.data.get(name)
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() not in ("0", "1", "true", "false"):
            raise SlackError("invalid_arguments")
        return str(value).strip().lower() in ("1", "true")

    def csv(self, name: str) -> list[str]:
        value = self.data.get(name)
        if value is None or value == "":
            return []
        if isinstance(value, list):
            if any(not isinstance(v, str) for v in value):
                raise SlackError("invalid_arguments")
            return [v.strip() for v in value if v.strip()]
        if not isinstance(value, str):
            raise SlackError("invalid_arguments")
        return [part.strip() for part in value.split(",") if part.strip()]

    def obj(self, name: str, error: str) -> Any:
        """A structured parameter (``blocks``, ``attachments``, ``metadata``...) in either encoding."""
        value = self.data.get(name)
        if value is None or value == "":
            return None
        if isinstance(value, (list, dict)):
            return value
        try:
            return load_json(str(value))
        except (ValueError, RecursionError, UnicodeError):
            raise SlackError(error) from None


@dataclass
class Actor:
    """The authenticated caller: a bot token (the app), a user token or an app-level token."""

    token: str
    kind: str
    uid: str
    scopes: tuple[str, ...]
    grant: Grant
    bot_id: str | None = None
    app_id: str | None = None

    def has(self, *wanted: str) -> bool:
        return any(w in self.scopes for w in wanted)

    @property
    def is_bot(self) -> bool:
        return self.kind == BOT


@dataclass
class Call:
    ctx: ProviderContext
    request: Request
    name: str
    params: Params
    actor: Actor | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def me(self) -> Actor:
        assert self.actor is not None
        return self.actor

    def need_scope(self, *alternatives: str) -> None:
        actor = self.me
        if actor.has(*alternatives):
            return
        needed = alternatives[0] if actor.kind == BOT or len(alternatives) == 1 else alternatives[-1]
        raise SlackError("missing_scope", needed=needed, provided=",".join(actor.scopes))


Handler = Callable[[Call], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Method:
    name: str
    fn: Handler
    scopes: tuple[str, ...]
    kinds: tuple[str, ...]
    auth: bool


METHODS: dict[str, Method] = {}
#: Coroutines run before each Web API call (the scheduled-message sweep registers itself here).
BEFORE_CALL: list[Callable[[ProviderContext], Awaitable[None]]] = []


def api(
    name: str, *, scopes: tuple[str, ...] = (), kinds: tuple[str, ...] = (BOT, USER), auth: bool = True
) -> Callable[[Handler], Handler]:
    """Register a Web API method. ``scopes`` are alternatives: holding any one is enough."""

    def decorator(fn: Handler) -> Handler:
        METHODS[name] = Method(name, fn, scopes, kinds, auth)
        return fn

    return decorator


# -- request parsing ---------------------------------------------------------------------------


def load_json(raw: str | bytes) -> Any:
    """Reject non-JSON numbers and unpaired surrogates before they reach the store or response encoder."""

    def invalid_constant(value: str) -> Any:
        raise ValueError(value)

    parsed = json.loads(raw, parse_constant=invalid_constant)
    json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return parsed


async def read_params(request: Request) -> Params:
    data: dict[str, Any] = {k: v for k, v in request.query_params.items()}
    files: dict[str, UploadedFile] = {}
    json_body = False
    missing_charset = False
    if request.method in ("POST", "PUT", "PATCH"):
        content_type = request.headers.get("content-type", "")
        mime = content_type.split(";")[0].strip().lower()
        header = Message()
        header["content-type"] = content_type
        charset = header.get_content_charset() or "utf-8"
        if charset not in ("utf-8", "iso-8859-1"):
            raise SlackError("invalid_charset")
        if mime == "application/x-www-form-urlencoded":
            body = (await request.body()).decode(charset, errors="replace")
            data.update(dict(parse_qsl(body, keep_blank_values=True, encoding=charset)))
        elif mime == "application/json":
            json_body = True
            raw = await request.body()
            if raw.strip():
                try:
                    parsed = load_json(raw.decode(charset))
                except (ValueError, RecursionError, UnicodeError):
                    raise SlackError("invalid_json") from None
                if not isinstance(parsed, dict):
                    raise SlackError("invalid_json")
                data.update(parsed)
            missing_charset = "charset" not in content_type.lower()
        elif mime.startswith("multipart/"):
            try:
                form = await request.form()
            except (HTTPException, ValueError):
                raise SlackError("invalid_form_data") from None
            for key, value in form.multi_items():
                if hasattr(value, "read"):
                    payload = await value.read()
                    files[key] = UploadedFile(
                        value.filename or key,
                        value.content_type or "application/octet-stream",
                        payload,
                    )
                else:
                    data[key] = value
        elif not mime:
            if (await request.body()).strip():
                raise SlackError("missing_post_type")
        else:
            raise SlackError("invalid_post_type")
    return Params(data, files, json_body=json_body, missing_charset=missing_charset)


# -- authentication ----------------------------------------------------------------------------


def authenticate(ctx: ProviderContext, request: Request, params: Params) -> Actor:
    from emulators.providers.slack import access

    token = bearer_token(request)
    if token is None and not params.json_body:
        token = params.str("token")
    if not token:
        raise SlackError("not_authed")
    grant = ctx.tokens.get(token)
    if grant is None or grant.kind != "access":
        raise SlackError("invalid_auth")
    if grant.revoked:
        raise SlackError("token_revoked")
    if grant.expires_at is not None and utc(grant.expires_at) <= ctx.clock.now():
        raise SlackError("token_expired")
    extra = grant.extra or {}
    kind = extra.get("kind") or (USER if grant.user_id else BOT)
    uid = extra.get("slack_user")
    if kind != APP:
        user = access.user_by_id(ctx, uid) if uid else access.resolve_user_ref(ctx, grant.user_id)
        if user is None:
            raise SlackError("invalid_auth")
        if user.get("deleted"):
            raise SlackError("account_inactive")
        uid = user["id"]
    return Actor(
        token=token,
        kind=kind,
        uid=uid or "",
        scopes=tuple(grant.scopes),
        grant=grant,
        bot_id=extra.get("bot_id"),
        app_id=extra.get("app_id"),
    )


# -- dispatch ----------------------------------------------------------------------------------


def _headers(spec: Method | None, actor: Actor | None, request_id: str) -> dict[str, str]:
    headers = {"x-slack-req-id": request_id, "cache-control": "private, no-cache, no-store, must-revalidate"}
    if actor is not None:
        headers["x-oauth-scopes"] = ",".join(actor.scopes)
    if spec is not None and spec.scopes:
        headers["x-accepted-oauth-scopes"] = ",".join(spec.scopes)
    return headers


def failure(error: SlackError, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": error.error, **error.extra}, headers=headers, media_type=JSON_TYPE
    )


async def dispatch(ctx: ProviderContext, request: Request, method_name: str) -> Response:
    request_id = "req-" + ctx.ids.hex(12)
    spec = METHODS.get(method_name)
    actor: Actor | None = None
    try:
        params = await read_params(request)
        if spec is None:
            raise SlackError("unknown_method", req_method=method_name)
        call = Call(ctx, request, method_name, params)
        if spec.auth:
            actor = call.actor = authenticate(ctx, request, params)
            if actor.kind not in spec.kinds:
                raise SlackError("not_allowed_token_type")
            if spec.scopes:
                call.need_scope(*spec.scopes)
        for hook in BEFORE_CALL:
            await hook(ctx)
        if params.missing_charset:
            call.warnings.append("missing_charset")
        result = await spec.fn(call)
    except SlackError as exc:
        return failure(exc, _headers(spec, actor, request_id))
    body: dict[str, Any] = {"ok": True, **result}
    if call.warnings:
        body["warning"] = ",".join(call.warnings)
        meta = dict(body.get("response_metadata") or {})
        meta["warnings"] = list(dict.fromkeys([*meta.get("warnings", []), *call.warnings]))
        body["response_metadata"] = meta
    return JSONResponse(body, headers=_headers(spec, actor, request_id), media_type=JSON_TYPE)
