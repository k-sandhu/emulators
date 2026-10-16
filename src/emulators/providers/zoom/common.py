"""Shared Zoom helpers: error envelope, token format, principals and scopes, paging, URLs.

Zoom's REST API v2 is unusual in a few ways the helpers below reproduce:

* errors are ``{"code": <zoom code>, "message": "..."}`` and *every* response (including
  errors and 404s) carries an ``x-zm-trackingid`` header;
* OAuth scopes come in two dialects (classic ``meeting:read:admin`` and granular
  ``meeting:read:meeting:admin``) and a ``:admin`` scope only has account-wide reach when the
  token's user (or app) is an account admin;
* paging uses an opaque ``next_page_token`` that is the empty string on the last page;
* ids are heterogeneous: 22-char base64url user ids, 10-11 digit meeting ids and 24-char
  base64 meeting UUIDs that can contain ``/``, ``+`` and ``//``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from emulators.core.auth import Grant, bearer_token
from emulators.core.errors import EmulatorError
from emulators.core.ids import b64url, stable_hash
from emulators.core.provider import ProviderContext, url_for_host
from emulators.world.model import Person, World

JSON_TYPE = "application/json;charset=UTF-8"
API_HOST = "api.zoom.us"
WEB_HOST = "us06web.zoom.us"
SSR_HOST = "ssrweb.zoom.us"
OAUTH_HOST = "zoom.us"
CLUSTER = "us06"

DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 300

# Zoom error codes used across modules.
E_INVALID_TOKEN = 124
E_NO_PERMISSION = 200
E_VALIDATION = 300
E_MISSING_SCOPE = 4700
E_USER_MISSING = 1001
E_MEETING_MISSING = 3001
E_NOT_HOST = 3003
E_RECORDING_MISSING = 3301


# -- responses ----------------------------------------------------------------------------


class ZoomJSONResponse(JSONResponse):
    media_type = JSON_TYPE


def zoom_error(
    status: int,
    code: int,
    message: str,
    *,
    errors: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> EmulatorError:
    body: dict[str, Any] = {"code": code, "message": message}
    if errors:
        body["errors"] = errors
    return EmulatorError(status, body, headers=headers, media_type=JSON_TYPE)


def invalid_field(field: str, message: str = "Invalid field.") -> EmulatorError:
    return zoom_error(400, E_VALIDATION, "Validation Failed.", errors=[{"field": field, "message": message}])


def invalid_param(name: str) -> EmulatorError:
    return zoom_error(400, E_VALIDATION, f"Invalid parameter: {name}.")


def no_permission() -> EmulatorError:
    return zoom_error(403, E_NO_PERMISSION, "No permission.")


def user_missing(key: str) -> EmulatorError:
    return zoom_error(404, E_USER_MISSING, f"User does not exist: {key}.")


def meeting_missing(key: object) -> EmulatorError:
    return zoom_error(404, E_MEETING_MISSING, f"Meeting does not exist: {key}.")


def recording_missing() -> EmulatorError:
    return zoom_error(404, E_RECORDING_MISSING, "This recording does not exist.")


def tracking_id() -> str:
    return f"v=2.0;clid=us;rid=WEB_{secrets.token_hex(16)}"


class TrackingMiddleware:
    """Adds ``x-zm-trackingid`` to every response, including errors the core renders."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(k.lower() == b"x-zm-trackingid" for k, _ in headers):
                    headers.append((b"x-zm-trackingid", tracking_id().encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


# -- tokens -------------------------------------------------------------------------------


def _b64j(value: dict[str, Any]) -> str:
    return b64url(json.dumps(value, separators=(",", ":")).encode())


def zoom_token(ctx: ProviderContext, *, kind: str, uid: str, client_id: str, ttl_s: int) -> str:
    """A JWT-looking opaque token (``eyJzdiI6IjAwMDAwMSIs...``) like the ones Zoom issues.

    Clients that peek at the payload (``exp``, ``uid``, ``aid``) find sensible values, but the
    token is only ever validated by looking it up in the grant store."""
    now = ctx.clock.timestamp()
    header = {"sv": "000001", "alg": "HS512", "v": "2.0", "kid": secrets.token_hex(16)}
    payload = {
        "ver": 10,
        "auth_id": secrets.token_hex(16),
        "iss": "marketplace.zoom.us",
        "aud": "marketplace.zoom.us" if kind != "refresh" else "refresh",
        "uid": uid,
        "aid": ctx.world.org.zoom_account_id,
        "cid": client_id,
        "iat": now,
        "exp": now + ttl_s,
    }
    return f"{_b64j(header)}.{_b64j(payload)}.{b64url(secrets.token_bytes(64))}"


# -- ids ----------------------------------------------------------------------------------


def digest(world: World, *parts: object) -> bytes:
    return hashlib.sha256("\x1f".join(("zoom", world.seed, *(str(p) for p in parts))).encode()).digest()


def b64_22(world: World, *parts: object) -> str:
    """22-character base64url id (Zoom user, group, participant and registrant ids)."""
    return b64url(digest(world, *parts)[:16])


def number(world: World, *parts: object, digits: int = 10) -> int:
    value = int.from_bytes(digest(world, *parts)[:8], "big")
    low = 10 ** (digits - 1)
    return low + value % (9 * low)


def meeting_uuid(world: World, *parts: object, variant: str | None = None) -> str:
    """24-char base64 UUID. ``variant`` forces the awkward shapes real Zoom produces:
    ``"leading_slash"`` (starts with ``/``) or ``"double_slash"`` (contains ``//``) -- clients
    must double-URL-encode those when they put them in a path. Unforced UUIDs avoid both
    shapes, so exactly the forced ones exercise the encoding rule."""
    for salt in range(500_000):
        text = base64.b64encode(digest(world, "uuid", *parts, salt)[:16]).decode()
        if variant == "leading_slash":
            ok = text.startswith("/")
        elif variant == "double_slash":
            ok = "//" in text and not text.startswith("/")
        else:
            ok = not text.startswith("/") and "//" not in text
        if ok:
            return text
    raise RuntimeError("unreachable: uuid search exhausted")


def runtime_uuid(ctx: ProviderContext) -> str:
    return base64.b64encode(bytes.fromhex(ctx.ids.hex(32))).decode()


def runtime_b64_22(ctx: ProviderContext) -> str:
    return b64url(bytes.fromhex(ctx.ids.hex(32)))


def digits_str(world: World, *parts: object, digits: int) -> str:
    return str(number(world, *parts, digits=digits))


def decode_path_key(raw: str) -> str:
    """Undo single or double URL-encoding of a meeting UUID taken from a path.

    Zoom asks clients to double-encode UUIDs that start with ``/`` or contain ``//``; the
    HTTP server decodes once and the application decodes again. Base64 never contains ``%``,
    so decoding until no ``%`` is left accepts both encodings (and unencoded values)."""
    value = raw
    for _ in range(3):
        if "%" not in value:
            break
        value = unquote(value)
    return value


def is_meeting_id(key: str) -> bool:
    return key.replace(" ", "").isdigit()


def now_ms(ctx: ProviderContext) -> int:
    return int(ctx.clock.now().timestamp() * 1000)


# -- principals and scopes ----------------------------------------------------------------

DEFAULT_SCOPES = (
    "meeting:read:admin meeting:write:admin recording:read:admin recording:write:admin "
    "user:read:admin user:write:admin report:read:admin group:read:admin group:write:admin "
    "dashboard_meetings:read:admin meeting_summary:read:admin"
)


@dataclass(frozen=True)
class Need:
    """A required scope in both dialects. ``admin`` variants carry account-wide reach."""

    classic: str
    granular: str
    admin_only: bool = False
    classic_aliases: tuple[str, ...] = ()

    def names(self) -> list[str]:
        base = [self.classic, self.granular, *self.classic_aliases]
        admin = [f"{n}:admin" for n in base]
        return admin if self.admin_only else [*base, *admin]

    def hint(self, *, granular: bool) -> list[str]:
        """The scope names Zoom lists in a 4700 error: an app uses one dialect or the other."""
        base = self.granular if granular else self.classic
        if self.admin_only:
            return [f"{base}:admin"]
        return [base, f"{base}:admin"] if granular else [base]


@dataclass
class Principal:
    """Who is calling: a delegated user (user-level OAuth) or an account-level app."""

    grant: Grant
    person: Person | None
    owner_id: str | None
    scopes: frozenset[str]

    @property
    def app_only(self) -> bool:
        return self.person is None

    @property
    def is_account_admin(self) -> bool:
        if self.person is None:
            return True
        return self.person.is_admin or self.person.id == self.owner_id

    def authorize(self, need: Need) -> bool:
        """Check the token's scopes for ``need``; return whether it may act on other users.

        Tokens minted without scopes (``/_emulator/tokens``) are unrestricted: reach then depends
        only on the caller's identity."""
        if not self.scopes and not self.grant.extra.get("enforce_scopes"):
            return self.is_account_admin
        acceptable = need.names()
        if not any(name in self.scopes for name in acceptable):
            granular = any(sc.removesuffix(":admin").count(":") >= 2 for sc in self.scopes)
            raise zoom_error(
                401,
                E_MISSING_SCOPE,
                f"Invalid access token, does not contain scopes:[{', '.join(need.hint(granular=granular))}].",
            )
        has_admin = any(
            f"{n}:admin" in self.scopes for n in (need.classic, need.granular, *need.classic_aliases)
        )
        return has_admin and self.is_account_admin


def owner_person(world: World) -> Person:
    return next((p for p in world.employees if p.manager_id is None), world.employees[0])


def resolve_grant_person(ctx: ProviderContext, grant: Grant) -> Person | None:
    key = grant.user_id
    if not key:
        return None
    for person in ctx.world.employees:
        if key in (person.id, person.email) or key == zoom_user_id(ctx.world, person.id):
            return person
    return None


def principal_from_grant(ctx: ProviderContext, grant: Grant) -> Principal:
    person = resolve_grant_person(ctx, grant)
    if grant.user_id:
        rec = ctx.store.collection("users").get(zoom_user_id(ctx.world, person.id)) if person else None
        if person is None or rec is None or rec.get("status") != "active":
            raise zoom_error(401, E_INVALID_TOKEN, "Invalid access token.")
    return Principal(grant, person, owner_person(ctx.world).id, frozenset(grant.scopes))


def authenticate(ctx: ProviderContext, request: Request) -> Principal:
    token = bearer_token(request)
    grant = ctx.tokens.resolve(token) if token else None
    if grant is None:
        expired = bool(token) and ctx.tokens.is_expired(token)
        raise zoom_error(
            401, E_INVALID_TOKEN, "Access token is expired." if expired else "Invalid access token."
        )
    return principal_from_grant(ctx, grant)


def zoom_user_id(world: World, person_id: str) -> str:
    return b64_22(world, "user", person_id)


# -- request helpers ----------------------------------------------------------------------


async def json_body(request: Request) -> dict[str, Any]:
    from emulators.providers.zoom.validation import request_body

    raw = await request.body()
    if not raw.strip():
        raw = b"{}"

    def non_finite(_: str) -> None:
        raise ValueError("Non-finite JSON number")

    try:
        data = json.loads(raw, parse_constant=non_finite)
        json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        raise zoom_error(400, E_VALIDATION, "Request Body should be a valid JSON object.")
    request_body(request, data)
    return data


def query_int(
    request: Request, name: str, default: int, *, minimum: int = 1, maximum: int | None = None
) -> int:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)  # type: ignore[arg-type]
    except ValueError:
        raise invalid_param(name) from None
    if value < minimum:
        raise invalid_param(name)
    return min(value, maximum) if maximum else value


def query_bool(request: Request, name: str, default: bool = False) -> bool:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    if raw.lower() not in ("true", "false", "1", "0"):
        raise invalid_param(name)
    return raw.lower() in ("true", "1")


# -- paging -------------------------------------------------------------------------------


def _fingerprint(request: Request) -> str:
    pairs = sorted(
        (k, v)
        for k, v in request.query_params.multi_items()
        if k not in ("next_page_token", "page_number", "page_size")
    )
    return stable_hash(request.url.path, pairs, length=8)


def paginate(request: Request, items: list[Any]) -> tuple[list[Any], dict[str, Any]]:
    """Slice ``items`` by ``page_size``/``next_page_token`` and return the paging envelope.

    Unknown or stale tokens fail the way Zoom's do (code 300)."""
    size = query_int(request, "page_size", DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE)
    token = request.query_params.get("next_page_token")
    ctx: ProviderContext = request.app.state.ctx
    cursors = ctx.store.collection("_zoom_cursors")
    grant = ctx.tokens.resolve(bearer_token(request))
    identity = (grant.client_id, grant.user_id, grant.app_only, sorted(grant.scopes)) if grant else None
    offset = 0
    if token:
        cursor = cursors.get(token)
        if (
            cursor is None
            or cursor["fingerprint"] != _fingerprint(request)
            or cursor["identity"] != identity
            or cursor["expires"] <= ctx.clock.timestamp()
        ):
            raise zoom_error(400, E_VALIDATION, "The next page token is invalid or has expired.")
        offset = cursor["offset"]
    elif request.query_params.get("page_number"):
        offset = (query_int(request, "page_number", 1) - 1) * size
    page = items[offset : offset + size]
    more = offset + size < len(items)
    next_token = ""
    if more:
        next_token = secrets.token_urlsafe(32)
        cursors.put(
            {
                "id": next_token,
                "offset": offset + size,
                "fingerprint": _fingerprint(request),
                "identity": identity,
                "expires": ctx.clock.timestamp() + 900,
            }
        )
    meta = {
        "page_count": math.ceil(len(items) / size),
        "page_number": offset // size + 1,
        "page_size": size,
        "total_records": len(items),
        "next_page_token": next_token,
    }
    return page, meta


# -- rendering ----------------------------------------------------------------------------

_URL_RE = re.compile(r"https://((?:[a-z0-9-]+\.)*zoom\.us)((?:/[^\s\"'<>]*)?)")


def clean(value: Any) -> Any:
    """Drop ``_private`` keys (internal bookkeeping stored next to the wire fields)."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def externalize(request: Request, ctx: ProviderContext, value: Any) -> Any:
    """Rewrite stored ``https://*.zoom.us`` URLs to the base the client is using."""
    if isinstance(value, str):
        if "zoom.us" not in value:
            return value

        def swap(match: re.Match[str]) -> str:
            host, path = match.group(1), match.group(2)
            if not path:
                return url_for_host(request, ctx, host, "/").rstrip("/")
            return url_for_host(request, ctx, host, path)

        return _URL_RE.sub(swap, value)
    if isinstance(value, dict):
        return {k: externalize(request, ctx, v) for k, v in value.items()}
    if isinstance(value, list):
        return [externalize(request, ctx, v) for v in value]
    return value


def render(request: Request, ctx: ProviderContext, data: Any) -> Any:
    return externalize(request, ctx, clean(data))


def respond(
    request: Request,
    ctx: ProviderContext,
    data: Any,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> ZoomJSONResponse:
    return ZoomJSONResponse(render(request, ctx, data), status_code=status, headers=headers)


# -- generic errors and faults ------------------------------------------------------------

RATE_LIMIT_MESSAGE = "You have reached the maximum per-second rate limit for this API. Try again later."


def generic_error_body(status: int, message: str = "") -> dict[str, Any]:
    """Envelope for errors the core raises on our behalf (unknown routes, validation, faults)."""
    if status == 400:
        return {"code": E_VALIDATION, "message": message or "Validation Failed."}
    if status == 401:
        return {"code": E_INVALID_TOKEN, "message": "Invalid access token."}
    if status == 403:
        return {"code": E_NO_PERMISSION, "message": "No permission."}
    if status == 404:
        return {"code": 404, "message": "Resource not found."}
    if status == 405:
        return {"code": 405, "message": "Method not allowed."}
    if status == 429:
        return {"code": 429, "message": RATE_LIMIT_MESSAGE}
    if status >= 500:
        return {"code": status, "message": "Internal Server Error."}
    return {"code": status, "message": message or "Error."}


def fault_response(rule: Any) -> tuple[int, dict[str, str], bytes]:
    """Injected faults look like Zoom's: JSON envelope, tracking id, and for 429 the rate-limit headers."""
    status = rule.status or 500
    headers = {"content-type": JSON_TYPE, "x-zm-trackingid": tracking_id()}
    if status == 429:
        headers.update(
            {
                "retry-after": "1",
                "x-ratelimit-category": "Light",
                "x-ratelimit-type": "QPS",
                "x-ratelimit-limit": "30",
                "x-ratelimit-remaining": "0",
            }
        )
    headers.update({k.lower(): v for k, v in rule.headers.items()})
    if rule.body is not None:
        body = rule.body if isinstance(rule.body, (bytes, str)) else json.dumps(rule.body)
    else:
        body = json.dumps(generic_error_body(status))
    return status, headers, body.encode() if isinstance(body, str) else body


class LiveCollection:
    """A named store collection resolved on every access.

    ``Store.restore`` (used by ``reset``) swaps in fresh ``Collection`` objects, so route modules
    must not hold on to the object ``store.collection(name)`` returned at registration time."""

    def __init__(self, ctx: ProviderContext, name: str) -> None:
        self._ctx = ctx
        self._name = name

    def _coll(self) -> Any:
        return self._ctx.store.collection(self._name)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._coll(), attr)

    def __iter__(self) -> Any:
        return iter(self._coll())

    def __len__(self) -> int:
        return len(self._coll())

    def __contains__(self, item: object) -> bool:
        return item in self._coll()
