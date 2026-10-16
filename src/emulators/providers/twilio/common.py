"""Shared helpers for the Twilio emulator: auth, errors, dates, paging, phones, signing.

Twilio conventions implemented here (see the public API reference):

* 2010 API (``api.twilio.com``) uses RFC 2822 GMT timestamps, relative ``uri``
  fields, form-encoded PascalCase request parameters, snake_case JSON responses and
  the ``first_page_uri``/``next_page_uri`` paging envelope.
* The v1/v2 APIs (Messaging, Verify, Lookups, Conversations) use ISO-8601
  ``YYYY-MM-DDTHH:MM:SSZ`` timestamps, absolute ``url`` fields and the ``meta``
  paging envelope.
* Errors are ``{"code", "message", "more_info", "status"}`` everywhere.
* Webhooks are signed with ``X-Twilio-Signature`` = base64(HMAC-SHA1(auth_token,
  url + concat(sorted(key + value)))) — exactly what
  ``twilio.request_validator.RequestValidator`` recomputes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac as hmac_mod
import math
import re
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import Request

from emulators.core.auth import basic_credentials, bearer_token
from emulators.core.clock import isoformat_z, utc
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_hash
from emulators.core.provider import url_for_host

if TYPE_CHECKING:
    from emulators.core.provider import ProviderContext

API_VERSION = "2010-04-01"
API_HOST = "api.twilio.com"
MESSAGING_HOST = "messaging.twilio.com"
VERIFY_HOST = "verify.twilio.com"
LOOKUPS_HOST = "lookups.twilio.com"
CONVERSATIONS_HOST = "conversations.twilio.com"

# Twilio "magic" numbers used with test credentials; in this emulator they also act
# as deterministic failure simulators with the live credentials.
MAGIC_TO_ERRORS: dict[str, tuple[int, str]] = {
    "+15005550001": (21211, "The 'To' number {to} is not a valid phone number."),
    "+15005550002": (
        21612,
        "The 'To' phone number: {to}, is not currently reachable using the 'From' phone number: "
        "{from_} via SMS.",
    ),
    "+15005550003": (
        21408,
        "Permission to send an SMS has not been enabled for the region indicated by the 'To' number: {to}.",
    ),
    "+15005550004": (21610, "The recipient has blocked messages from this number or has opted out."),
    "+15005550009": (21614, "'To' number: {to} is not a mobile number"),
}
MAGIC_FROM_ERRORS: dict[str, tuple[int, str]] = {
    "+15005550001": (21212, "The 'From' number {from_} is not a valid phone number."),
    "+15005550007": (
        21606,
        "The 'From' phone number provided is not a valid message-capable Twilio phone number for this destination/account.",
    ),
    "+15005550008": (21611, "This 'From' number has exceeded the maximum MPS for this phone number."),
}
MAGIC_FROM_OK = "+15005550006"
# Twilio's published WhatsApp sandbox sender: every developer's first ``whatsapp:`` From.
WHATSAPP_SANDBOX = "+14155238886"

OPT_OUT_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
OPT_IN_WORDS = {"START", "YES", "UNSTOP"}
HELP_WORDS = {"HELP", "INFO"}

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


# -- credentials -------------------------------------------------------------------


def credentials_for(account_sid: str) -> dict[str, str]:
    """Deterministic credential set derived from the account SID (never sequence-based)."""
    return {
        "account_sid": account_sid,
        "auth_token": stable_hash("emulators/twilio/auth-token", account_sid, length=32),
        "test_account_sid": "AC" + stable_hash("emulators/twilio/test-sid", account_sid, length=32),
        "test_auth_token": stable_hash("emulators/twilio/test-token", account_sid, length=32),
        "api_key_sid": "SK" + stable_hash("emulators/twilio/api-key", account_sid, length=32),
        "api_key_secret": stable_hash("emulators/twilio/api-key-secret", account_sid, length=32),
    }


def creds(ctx: ProviderContext) -> dict[str, str]:
    return ctx.store.meta["credentials"]  # set by seed()


def auth_token(ctx: ProviderContext) -> str:
    return creds(ctx)["auth_token"]


def strict_auth(ctx: ProviderContext) -> bool:
    return (ctx.setting("STRICT_AUTH") or "").lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class AuthInfo:
    """Who a request authenticated as."""

    account_sid: str  # canonical account the request operates on (main account)
    is_test: bool = False
    via_api_key: bool = False
    via_token: bool = False  # /_emulator/tokens grant presented as Bearer
    subaccount: bool = False  # authenticated with a subaccount's own SID/token


_AUTH_DETAIL = "Your AccountSid or AuthToken was incorrect."


def _unauthorized() -> EmulatorError:
    return EmulatorError(
        401,
        twilio_error_body(401, 20003, "Authenticate", detail=_AUTH_DETAIL),
        headers={"WWW-Authenticate": 'Basic realm="Twilio API"'},
    )


def require_auth(request: Request, ctx: ProviderContext) -> AuthInfo:
    """Twilio HTTP Basic auth: AccountSid:AuthToken, test credentials, or SK...:secret.

    Lenient by default (any password for a known username); set
    ``EMULATORS_TWILIO_STRICT_AUTH=1`` to require the exact token/secret. A Bearer
    grant minted via ``POST /_emulator/tokens`` is also accepted. Subaccounts created
    through the API authenticate with their own SID and auth token and can only act on
    themselves (see :func:`check_account`). The result is remembered on the request so
    ``check_account`` can enforce that scoping.
    """
    auth = _authenticate(request, ctx)
    request.state.twilio_auth = auth
    return auth


def _authenticate(request: Request, ctx: ProviderContext) -> AuthInfo:
    c = creds(ctx)
    bearer = bearer_token(request)
    if bearer:
        grant = ctx.tokens.resolve(bearer)
        if grant is not None:
            return AuthInfo(c["account_sid"], via_token=True)
        if bearer == c["auth_token"]:
            return AuthInfo(c["account_sid"])
        if bearer == c["test_auth_token"]:
            return AuthInfo(c["account_sid"], is_test=True)
        raise _unauthorized()
    pair = basic_credentials(request)
    if pair is None:
        raise _unauthorized()
    user, password = pair
    known: dict[str, tuple[str, bool, bool]] = {
        c["account_sid"]: (c["auth_token"], False, False),
        c["test_account_sid"]: (c["test_auth_token"], True, False),
    }
    # API keys (the seeded one and any created via /Keys) authenticate for the main account for
    # as long as they exist, so deleting a key revokes it.
    extra = ctx.store.collection("api_keys").get(user)
    if extra is not None:
        owner = extra.get("_account") or c["account_sid"]
        if owner != c["account_sid"]:  # a subaccount's key acts as that subaccount only
            if strict_auth(ctx) and not hmac_mod.compare_digest(password, extra.get("secret") or ""):
                raise _unauthorized()
            return AuthInfo(owner, via_api_key=True, subaccount=True)
        known[user] = (extra.get("secret") or "", False, True)
    sub = ctx.store.collection("accounts").get(user) if user.startswith("AC") else None
    if sub is not None and user != c["account_sid"]:
        # A subaccount authenticates as itself, with its own token.
        if strict_auth(ctx) and not hmac_mod.compare_digest(password, sub.get("auth_token") or ""):
            raise _unauthorized()
        return AuthInfo(user, subaccount=True)
    if user not in known:
        raise _unauthorized()
    expected, is_test, via_key = known[user]
    if strict_auth(ctx) and not hmac_mod.compare_digest(password, expected):
        raise _unauthorized()
    return AuthInfo(c["account_sid"], is_test=is_test, via_api_key=via_key)


def check_account(
    ctx: ProviderContext, path_sid: str, request: Request | None = None, *, allow_inactive: bool = False
) -> str:
    """Validate an ``{AccountSid}`` path segment; return the canonical account SID.

    The test-credentials SID aliases the main account (same resources, test mode).
    Subaccounts created via ``POST /Accounts.json`` resolve to themselves, and a
    request authenticated *as* a subaccount may only address that subaccount (403
    otherwise), while the main account's credentials reach every subaccount.
    """
    c = creds(ctx)
    auth: AuthInfo | None = getattr(request.state, "twilio_auth", None) if request is not None else None
    if path_sid in (c["account_sid"], c["test_account_sid"]):
        canonical = c["account_sid"]
    elif ctx.store.collection("accounts").get(path_sid) is not None:
        canonical = path_sid
    else:
        raise not_found(f"/{API_VERSION}/Accounts/{path_sid}.json")
    if auth is not None and auth.subaccount and canonical != auth.account_sid:
        raise fail(403, 20003, "Permission Denied")
    record = ctx.store.collection("accounts").get(canonical)
    if (
        request is not None
        and not allow_inactive
        and request.method not in ("GET", "HEAD")
        and record is not None
        and record.get("status") != "active"
    ):
        raise fail(403, 20005, "Account not active")
    return canonical


# -- errors ------------------------------------------------------------------------


def twilio_error_body(status: int, code: int, message: str, detail: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code}
    if detail:
        body["detail"] = detail
    body.update(
        {"message": message, "more_info": f"https://www.twilio.com/docs/errors/{code}", "status": status}
    )
    return body


def fail(status: int, code: int, message: str, detail: str | None = None) -> EmulatorError:
    return EmulatorError(status, twilio_error_body(status, code, message, detail))


def missing_param(name: str) -> EmulatorError:
    """Platform-level "required parameter absent" error used by the v1/v2 APIs."""
    return fail(400, 20001, f"Missing required parameter {name} in the post body")


def not_found(uri: str) -> EmulatorError:
    return fail(404, 20404, f"The requested resource {uri} was not found")


_STATUS_CODES = {
    400: 20001,
    401: 20003,
    403: 20003,
    404: 20404,
    405: 20004,
    409: 20001,
    429: 20429,
    500: 20500,
    502: 20500,
    503: 20503,
    504: 20504,
}


def error_body(status: int, code: str, message: str) -> dict[str, Any]:
    """Provider.error_body: map core status codes onto Twilio's numeric codes."""
    numeric = int(code) if code.isdigit() else _STATUS_CODES.get(status, 20500)
    if status == 401:
        return twilio_error_body(status, numeric, "Authenticate", detail=_AUTH_DETAIL)
    return twilio_error_body(status, numeric, message)


def request_id() -> str:
    """``Twilio-Request-Id`` value: ``RQ`` + 32 hex (not deterministic, like the real one)."""
    return "RQ" + uuid.uuid4().hex


# -- dates -------------------------------------------------------------------------


def rfc2822(dt: datetime | None) -> str | None:
    """``Thu, 16 Oct 2026 14:00:00 +0000`` — the 2010 API timestamp format."""
    if dt is None:
        return None
    return format_datetime(utc(dt))


def iso8601(dt: datetime | None) -> str | None:
    """``2026-10-16T14:00:00Z`` — the v1/v2 API timestamp format."""
    if dt is None:
        return None
    return isoformat_z(utc(dt))


# -- ids ---------------------------------------------------------------------------


def sid(ctx: ProviderContext, prefix: str, namespace: str) -> str:
    # NOTE: IdFactory.fork() restarts its stream, so runtime ids must come from the
    # shared ctx.ids stream to stay unique; `namespace` is kept for call-site clarity.
    _ = namespace
    return prefix + ctx.ids.hex(32)


def stable_sid(prefix: str, *parts: object) -> str:
    """A SID derived only from ``parts`` so seeded records are identical every run."""
    return prefix + stable_hash("twilio", *parts, length=32)


# -- request parsing ---------------------------------------------------------------


async def form_params(request: Request) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Parse a Twilio request body (form-encoded in production; JSON tolerated).

    Returns ``(single, multi)`` where ``single`` keeps the first value of each key and
    ``multi`` keeps every value (``MediaUrl``, ``StatusCallbackEvent`` repeat).
    """
    content_type = request.headers.get("content-type", "")
    multi: dict[str, list[str]] = {}
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, list):
                    multi[str(key)] = [str(v) for v in value]
                elif value is not None:
                    multi[str(key)] = [_form_scalar(value)]
    else:
        form = await request.form()
        for key in dict.fromkeys(form):
            multi[key] = [str(v) for v in form.getlist(key)]
    single = {k: v[0] for k, v in multi.items() if v}
    return single, multi


def _form_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def get_param(params: dict[str, str], *names: str) -> str | None:
    for name in names:
        if name in params and params[name] != "":
            return params[name]
    # Twilio parameters are PascalCase, but hand-written clients sometimes vary case.
    lowered = {k.lower(): v for k, v in params.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def has_param(params: dict[str, str], *names: str) -> bool:
    lowered = {k.lower() for k in params}
    return any(name.lower() in lowered for name in names)


def param_value(params: dict[str, str], *names: str) -> str | None:
    """Like get_param but distinguishes "" (present) from absent (for redaction)."""
    for name in names:
        if name in params:
            return params[name]
    lowered = {k.lower(): v for k, v in params.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes")


def parse_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


# -- phones ------------------------------------------------------------------------


def split_address(address: str) -> tuple[str, str]:
    """``whatsapp:+1416...`` → ``("whatsapp", "+1416...")``; plain numbers → sms."""
    text = address.strip()
    if ":" in text:
        channel, _, number = text.partition(":")
        return channel.lower(), number.strip()
    return "sms", text


def bare_number(address: str) -> str:
    return split_address(address)[1]


def valid_e164(number: str) -> bool:
    """Syntactic E.164 plus the NANP rules Twilio enforces (``+1`` + 10 digits, NPA 2-9)."""
    if not _E164_RE.match(number):
        return False
    if number.startswith("+1"):
        return bool(re.fullmatch(r"\+1[2-9]\d{9}", number))
    return True


def national_format(number: str) -> str:
    digits = re.sub(r"\D", "", number)
    if number.startswith("+1") and len(digits) == 11:
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    if number.startswith("+44") and len(digits) == 12:
        national = digits[2:]
        if national.startswith("20"):  # London: 020 7946 0100
            return f"0{national[:2]} {national[2:6]} {national[6:]}"
        if national.startswith("7"):  # Mobile: 07700 900123
            return f"0{national[:4]} {national[4:]}"
        return f"0{national[:3]} {national[3:6]} {national[6:]}"
    return number


def format_from_formatted(number: str) -> str:
    """Twilio's ``from_formatted`` for calls: NANP numbers in national format."""
    channel, bare = split_address(number)
    formatted = national_format(bare)
    return f"{channel}:{formatted}" if ":" in number else formatted


# GSM 03.38 basic character set (each counts 1 septet) and extension set (counts 2).
_GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXT = "^{}\\[~]|€\f"


def num_segments(body: str) -> int:
    """Twilio segment counting: GSM-7 160/153, UCS-2 70/67 (UTF-16 code units)."""
    if not body:
        return 0
    if all(ch in _GSM7_BASIC or ch in _GSM7_EXT for ch in body):
        units = sum(2 if ch in _GSM7_EXT else 1 for ch in body)
        return 1 if units <= 160 else math.ceil(units / 153)
    units = len(body.encode("utf-16-le")) // 2
    return 1 if units <= 70 else math.ceil(units / 67)


# Area-code geography for inbound webhook fields and Lookup (fictional-safe subset).
AREA_GEO: dict[str, tuple[str, str, str]] = {
    "416": ("Toronto", "ON", "CA"),
    "647": ("Toronto", "ON", "CA"),
    "437": ("Toronto", "ON", "CA"),
    "905": ("Hamilton", "ON", "CA"),
    "403": ("Calgary", "AB", "CA"),
    "604": ("Vancouver", "BC", "CA"),
    "514": ("Montreal", "QC", "CA"),
    "613": ("Ottawa", "ON", "CA"),
    "519": ("London", "ON", "CA"),
    "902": ("Halifax", "NS", "CA"),
    "617": ("Boston", "MA", "US"),
    "212": ("New York", "NY", "US"),
    "917": ("New York", "NY", "US"),
    "312": ("Chicago", "IL", "US"),
    "313": ("Detroit", "MI", "US"),
    "303": ("Denver", "CO", "US"),
    "305": ("Miami", "FL", "US"),
    "415": ("San Francisco", "CA", "US"),
    "503": ("Portland", "OR", "US"),
    "512": ("Austin", "TX", "US"),
    "603": ("Manchester", "NH", "US"),
    "608": ("Madison", "WI", "US"),
    "720": ("Denver", "CO", "US"),
    "718": ("New York", "NY", "US"),
    "828": ("Asheville", "NC", "US"),
}
CA_AREAS = {area for area, geo in AREA_GEO.items() if geo[2] == "CA"} | {
    "204",
    "226",
    "236",
    "249",
    "250",
    "289",
    "306",
    "343",
    "365",
    "367",
    "418",
    "431",
    "438",
    "450",
    "506",
    "548",
    "579",
    "581",
    "587",
    "639",
    "672",
    "705",
    "709",
    "742",
    "753",
    "778",
    "780",
    "782",
    "807",
    "819",
    "825",
    "867",
    "873",
    "879",
}


def geo_for(number: str) -> tuple[str, str, str]:
    """(city, state/province, ISO country) for an E.164 number."""
    bare = bare_number(number)
    if bare.startswith("+44"):
        return ("London", "ENG", "GB")
    if bare.startswith("+1") and len(bare) >= 5:
        area = bare[2:5]
        if area in AREA_GEO:
            return AREA_GEO[area]
        return ("", "", "CA" if area in CA_AREAS else "US")
    return ("", "", "")


# -- signing & webhook bodies ------------------------------------------------------


def twilio_signature(token: str, url: str, params: dict[str, str]) -> str:
    """``X-Twilio-Signature`` exactly as ``RequestValidator.compute_signature`` does."""
    signed = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac_mod.new(token.encode(), signed.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def form_body(params: dict[str, str]) -> bytes:
    return urlencode(params).encode()


async def deliver_form(
    ctx: ProviderContext,
    url: str,
    params: dict[str, str],
    *,
    event_type: str,
    method: str = "POST",
) -> dict[str, Any]:
    """Send a signed Twilio webhook through the core dispatcher.

    ``POST`` webhooks carry the params as a form body; ``GET`` webhooks (a number configured
    with ``SmsMethod=GET``) carry them in the query string, and Twilio signs the full URL
    including that query string (the validator is then called with no POST params).
    """
    method = (method or "POST").upper()
    headers = {"User-Agent": "TwilioProxy/1.1", "I-Twilio-Idempotency-Token": str(uuid.uuid4())}
    if method == "GET":
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
        full_url = parts._replace(query=urlencode(query)).geturl()
        headers["X-Twilio-Signature"] = twilio_signature(auth_token(ctx), full_url, {})
        return await ctx.webhooks.deliver(
            full_url,
            b"",
            headers=headers,
            event_type=event_type,
            method="GET",
            content_type="application/x-www-form-urlencoded; charset=UTF-8",
        )
    headers["X-Twilio-Signature"] = twilio_signature(auth_token(ctx), url, params)
    return await ctx.webhooks.deliver(
        url,
        form_body(params),
        headers=headers,
        event_type=event_type,
        method=method,
        content_type="application/x-www-form-urlencoded; charset=UTF-8",
    )


# -- paging ------------------------------------------------------------------------


_PAGING_KEYS = {"page", "pagesize", "pagetoken"}


def _page_args(request: Request) -> tuple[int, int]:
    query = request.query_params
    page_size = parse_int(query.get("PageSize"), 50)
    page_size = max(1, min(page_size, 1000))
    page = parse_int(query.get("Page"), 0)
    token = query.get("PageToken")
    if token:
        try:
            page = int(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode())
        except (ValueError, UnicodeDecodeError):
            page = parse_int(token, page)
    return max(page, 0), page_size


def _page_token(page: int) -> str:
    return base64.urlsafe_b64encode(str(page).encode()).rstrip(b"=").decode()


def _carried_query(request: Request, extra: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Filters from the original request, repeated on every paging link (as Twilio does)."""
    carried = [(k, v) for k, v in request.query_params.multi_items() if k.lower() not in _PAGING_KEYS]
    if extra:
        carried.extend((k, v) for k, v in extra.items() if k not in {c[0] for c in carried})
    return carried


def page_2010(
    request: Request,
    key: str,
    records: list[dict[str, Any]],
    uri_path: str,
    *,
    extra_query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The 2010 list envelope with relative ``*_page_uri`` values, as Twilio returns."""
    page, page_size = _page_args(request)
    start = page * page_size
    chunk = records[start : start + page_size]
    end = start + len(chunk) - 1 if chunk else start
    carried = _carried_query(request, extra_query)

    def link(p: int) -> str:
        query = [*carried, ("PageSize", str(page_size)), ("Page", str(p))]
        if p > 0:
            query.append(("PageToken", _page_token(p)))
        return f"{uri_path}?{urlencode(query)}"

    return {
        key: chunk,
        "end": end,
        "first_page_uri": link(0),
        "next_page_uri": link(page + 1) if start + page_size < len(records) else None,
        "page": page,
        "page_size": page_size,
        "previous_page_uri": link(page - 1) if page > 0 else None,
        "start": start,
        "uri": link(page),
    }


def page_meta(
    request: Request,
    ctx: ProviderContext,
    host: str,
    key: str,
    records: list[dict[str, Any]],
    path: str,
) -> dict[str, Any]:
    """The v1/v2 list envelope: ``{key: [...], meta: {...}}`` with absolute page URLs."""
    page, page_size = _page_args(request)
    start = page * page_size
    chunk = records[start : start + page_size]
    carried = _carried_query(request)

    def link(p: int) -> str:
        pairs: list[tuple[str, Any]] = [*carried, ("PageSize", page_size), ("Page", p)]
        if p > 0:
            pairs.append(("PageToken", _page_token(p)))
        return url_for_host(request, ctx, host, f"{path}?{urlencode(pairs)}")

    return {
        key: chunk,
        "meta": {
            "page": page,
            "page_size": page_size,
            "first_page_url": link(0),
            "previous_page_url": link(page - 1) if page > 0 else None,
            "url": link(page),
            "next_page_url": link(page + 1) if start + page_size < len(records) else None,
            "key": key,
        },
    }


def absolute(request: Request, ctx: ProviderContext, host: str, path: str) -> str:
    return url_for_host(request, ctx, host, path)


def strip_internal(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if not k.startswith("_") and k != "id"}


def valid_url(value: str) -> bool:
    """An absolute http(s) URL, as Twilio requires for webhook and callback parameters."""
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def auto_advance(ctx: ProviderContext) -> bool:
    """``EMULATORS_TWILIO_AUTO_ADVANCE=0`` stops messages/calls moving past their first status,
    so tests can drive each transition with the ``advance_message`` / ``advance_call`` actions."""
    return (ctx.setting("AUTO_ADVANCE") or "1").lower() not in ("0", "false", "no", "off")


_ORPHAN_TASKS: set[asyncio.Task[Any]] = set()


def spawn(ctx: ProviderContext, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Run ``coro`` in the background but keep a strong reference to it.

    The task joins the webhook dispatcher's task set when available, so ``hub.drain_webhooks()``
    (and ``POST /_emulator/webhooks/flush``) also wait for delayed status progression.
    """
    task = asyncio.get_running_loop().create_task(coro)
    tasks = getattr(ctx.webhooks, "_tasks", None)
    keep = tasks if isinstance(tasks, set) else _ORPHAN_TASKS
    keep.add(task)
    task.add_done_callback(keep.discard)
    return task


def payload_text(payload: dict[str, Any], *names: str) -> str | None:
    """First non-null value among ``names`` coerced to ``str`` (control-plane payloads are JSON,
    so a test may send numbers or lists where text is expected)."""
    for name in names:
        value = payload.get(name)
        if value is not None:
            return value if isinstance(value, str) else str(value)
    return None


def newest_first(records: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    """Sort newest first by the first non-empty ISO timestamp key; ties (the emulator clock may be
    frozen, and Twilio's resolution is one second) resolve to the most recently *created* record."""
    stamped = [(next((r[k] for k in keys if r.get(k)), ""), i, r) for i, r in enumerate(records)]
    return [r for _, _, r in sorted(stamped, key=lambda t: (t[0], t[1]), reverse=True)]


def route_path(scope: dict[str, Any]) -> str:
    """The request path as the vendor sees it: without the gateway's ``/twilio`` mount prefix."""
    path = scope.get("path", "")
    root = scope.get("root_path", "") or ""
    return path[len(root) :] if root and path.startswith(root) else path
