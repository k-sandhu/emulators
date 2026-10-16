"""Shared Graph API machinery for the Meta emulator.

Constants (app id/secret, well-known tokens), deterministic numeric ids, the Graph
error envelope, bearer/app token resolution with ``appsecret_proof`` checking, the
``fields=`` expansion parser and cursor paging. Everything here is vendor-shaped:
errors are ``{"error": {"message", "type", "code", "error_subcode", "fbtrace_id"}}``
and object ids are numeric strings.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, NoReturn
from urllib.parse import urlencode

from fastapi import Request

from emulators.core.auth import Grant, bearer_token
from emulators.core.clock import utc
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext, url_for_host
from emulators.core.webhooks import hmac_sha256_hex

# -- well-known test app / credentials (documented in docs/providers/meta.md) -----------------
APP_ID = "1876543210987654"
APP_SECRET = "0123456789abcdeffedcba9876543210"
APP_NAME = "Halcyon Integrations"
VERIFY_TOKEN = "halcyon-verify-token"
SYSTEM_USER_TOKEN = "EAACEdEose0cBOZCHalcyonSystemUser0123456789abcdefghij"
PAGE_ACCESS_TOKEN = "EAACEdEose0cBOZCHalcyonPageAccess0123456789abcdefghij"
USER_ACCESS_TOKEN = "EAACEdEose0cBOZCHalcyonUserNatalie0123456789abcdefgh"
DISPLAY_PHONE = "+14165550177"
DISPLAY_PHONE_DIGITS = "14165550177"
GRAPH_HOST = "graph.facebook.com"
DEFAULT_VERSION = "v21.0"
OLDEST_VERSION = "v17.0"
SUPPORTED_VERSIONS = tuple(f"v{n}.0" for n in range(17, 25))

USER_SCOPES = [
    "public_profile",
    "email",
    "pages_show_list",
    "pages_messaging",
    "pages_manage_metadata",
    "pages_read_engagement",
    "whatsapp_business_messaging",
    "whatsapp_business_management",
    "business_management",
]
PAGE_SCOPES = [
    "pages_messaging",
    "pages_manage_metadata",
    "pages_read_engagement",
    "pages_show_list",
]
SYSTEM_SCOPES = [
    "whatsapp_business_messaging",
    "whatsapp_business_management",
    "business_management",
    "pages_show_list",
]


def numeric_id(*parts: object, digits: int = 15) -> str:
    """A stable numeric Graph object id (15-17 digits) derived from ``parts``."""
    raw = int(stable_hash("meta", *parts, length=16), 16)
    value = raw % (10**digits)
    if value < 10 ** (digits - 1):
        value += 10 ** (digits - 1)
    return str(value)


def wamid(*parts: object) -> str:
    """A WhatsApp message id (``wamid.<base64>``) derived from ``parts``."""
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).digest()
    return "wamid." + base64.b64encode(digest).decode().rstrip("=")[:44]


def new_wamid(ctx: ProviderContext) -> str:
    return "wamid." + ctx.ids.urlsafe(44)


def new_mid(ctx: ProviderContext) -> str:
    """A Messenger message id (``m_...``)."""
    return "m_" + ctx.ids.urlsafe(32)


def mid_for(*parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).digest()
    return "m_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")[:32]


def graph_time(dt: datetime) -> str:
    """Graph's ISO-8601 with ``+0000`` offset (Messenger ``created_time`` etc.)."""
    return utc(dt).strftime("%Y-%m-%dT%H:%M:%S+0000")


def unix_s(dt: datetime) -> str:
    return str(int(utc(dt).timestamp()))


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def trace_id(ctx: ProviderContext) -> str:
    return ctx.ids.base62(1) + ctx.ids.urlsafe(20)


# -- errors ------------------------------------------------------------------------------------
def error_dict(
    ctx: ProviderContext,
    code: int,
    message: str,
    *,
    subcode: int | None = None,
    err_type: str = "OAuthException",
    error_data: dict[str, Any] | None = None,
    user_title: str | None = None,
    user_msg: str | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"message": message, "type": err_type, "code": code}
    if subcode is not None:
        error["error_subcode"] = subcode
    if error_data is not None:
        error["error_data"] = error_data
    if user_title is not None:
        error["error_user_title"] = user_title
    if user_msg is not None:
        error["error_user_msg"] = user_msg
    error["fbtrace_id"] = trace_id(ctx)
    return {"error": error}


def fail(
    ctx: ProviderContext,
    status: int,
    code: int,
    message: str,
    **kwargs: Any,
) -> NoReturn:
    raise EmulatorError(status, error_dict(ctx, code, message, **kwargs))


def wa_fail(ctx: ProviderContext, code: int, title: str, details: str, *, status: int = 400) -> NoReturn:
    """A WhatsApp Cloud API business error (13xxxx) with ``error_data.details``."""
    fail(
        ctx,
        status,
        code,
        f"(#{code}) {title}",
        error_data={"messaging_product": "whatsapp", "details": details},
        user_title=title,
        user_msg=details,
    )


def not_found(ctx: ProviderContext, object_id: str) -> NoReturn:
    if object_id.isascii() and object_id.isdigit():
        fail(
            ctx,
            400,
            100,
            f"Unsupported get request. Object with ID '{object_id}' does not exist, "
            "cannot be loaded due to missing permissions, or does not support this operation.",
            subcode=33,
            err_type="GraphMethodException",
        )
    fail(
        ctx,
        400,
        803,
        f"(#803) Some of the aliases you requested do not exist: {object_id}",
    )


def invalid_param(ctx: ProviderContext, message: str, *, subcode: int | None = None) -> NoReturn:
    fail(ctx, 400, 100, f"(#100) {message}" if not message.startswith("(#") else message, subcode=subcode)


def permission_fail(
    ctx: ProviderContext, message: str, *, code: int = 200, subcode: int | None = None
) -> NoReturn:
    fail(ctx, 403, code, f"(#{code}) {message}" if not message.startswith("(#") else message, subcode=subcode)


# -- auth ---------------------------------------------------------------------------------------
@dataclass
class Auth:
    """A resolved Graph access token."""

    token: str
    meta_type: str  # USER | PAGE | SYSTEM_USER | APP
    grant: Grant | None = None
    fb_user_id: str | None = None
    page_id: str | None = None
    person_id: str | None = None
    scopes: list[str] = field(default_factory=list)
    app_id: str = APP_ID

    @property
    def client_id(self) -> str:
        return self.grant.client_id if self.grant else self.app_id


def extract_token(request: Request, params: dict[str, Any]) -> str | None:
    return bearer_token(request) or (str(params["access_token"]) if params.get("access_token") else None)


def app_secret_for(ctx: ProviderContext, client_id: str | None) -> str:
    if client_id:
        record = ctx.tokens.client(client_id)
        if record and record.get("client_secret"):
            return str(record["client_secret"])
    return APP_SECRET


def resolve_auth(ctx: ProviderContext, request: Request, params: dict[str, Any]) -> Auth | None:
    """Resolve a token without raising (``None`` = missing/invalid/expired)."""
    token = extract_token(request, params)
    if not token:
        return None
    if "|" in token:  # app access token: "<app-id>|<app-secret>"
        client_id, _, secret = token.partition("|")
        record = ctx.tokens.client(client_id)
        expected = (record or {}).get("client_secret") or (APP_SECRET if client_id == APP_ID else None)
        if expected is not None and hmac.compare_digest(
            str(expected).encode(), secret.encode("utf-8", "replace")
        ):
            return Auth(token=token, meta_type="APP", grant=None, scopes=[], app_id=client_id)
        return None
    grant = ctx.tokens.resolve(token)
    if grant is None:
        return None
    extra = grant.extra or {}
    return Auth(
        token=token,
        meta_type=str(extra.get("meta_type") or ("APP" if grant.app_only else "USER")),
        grant=grant,
        fb_user_id=extra.get("fb_user_id") or (grant.user_id if not extra.get("page_id") else None),
        page_id=extra.get("page_id"),
        person_id=extra.get("person_id"),
        scopes=list(grant.scopes or []),
    )


def require_auth(
    ctx: ProviderContext,
    request: Request,
    params: dict[str, Any],
    *,
    types: set[str] | None = None,
    perm_message: str | None = None,
    perm_code: int = 200,
    perm_subcode: int | None = None,
) -> Auth:
    token = extract_token(request, params)
    if not token:
        fail(
            ctx,
            401,
            190,
            "An active access token must be used to query information about the current user.",
        )
    auth = resolve_auth(ctx, request, params)
    if auth is None:
        if ctx.tokens.is_expired(token):
            fail(
                ctx,
                401,
                190,
                "Error validating access token: Session has expired.",
                subcode=463,
            )
        fail(
            ctx,
            401,
            190,
            "Error validating access token: Session is invalid.",
            subcode=467,
        )
    proof = params.get("appsecret_proof")
    if proof:
        expected = hmac_sha256_hex(app_secret_for(ctx, auth.client_id), token)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(proof)) or not hmac.compare_digest(
            expected.lower(), str(proof).lower()
        ):
            invalid_param(ctx, "Invalid appsecret_proof provided in the API argument")
    if types is not None and auth.meta_type not in types:
        permission_fail(
            ctx,
            perm_message or "Permissions error: this endpoint requires a different type of access token.",
            code=perm_code,
            subcode=perm_subcode,
        )
    return auth


def require_scopes(ctx: ProviderContext, auth: Auth, *scopes: str) -> None:
    for scope in scopes:
        if scope not in auth.scopes:
            permission_fail(ctx, f"Requires {scope} permission.")


def require_business_access(ctx: ProviderContext, auth: Auth) -> None:
    """Scopes alone do not assign a business asset to a user."""
    business = ctx.store.collection("businesses").get(ctx.store.meta["business_id"]) or {}
    if auth.meta_type == "SYSTEM_USER" and auth.fb_user_id == ctx.store.meta["system_user_id"]:
        return
    person_id = auth.person_id
    if not person_id and auth.fb_user_id:
        person_id = (ctx.store.collection("fb_users").get(auth.fb_user_id) or {}).get("_person_id")
    if auth.meta_type != "USER" or person_id not in business.get("_roles", {}):
        permission_fail(ctx, "Permissions error: the token does not have access to this business asset.")


def issue_meta_token(
    ctx: ProviderContext,
    *,
    meta_type: str,
    fb_user_id: str | None = None,
    page_id: str | None = None,
    person_id: str | None = None,
    scopes: list[str] | None = None,
    ttl: timedelta | None = timedelta(hours=1),
    token: str | None = None,
    client_id: str = APP_ID,
) -> Grant:
    if scopes is None:
        scopes = {"USER": USER_SCOPES, "PAGE": PAGE_SCOPES, "SYSTEM_USER": SYSTEM_SCOPES}.get(meta_type, [])
    return ctx.tokens.issue_access(
        client_id=client_id,
        user_id=page_id or fb_user_id,
        scopes=list(scopes),
        ttl=ttl,
        token=token,
        prefix="EAA",
        length=60,
        app_only=meta_type in ("SYSTEM_USER", "APP"),
        extra={
            "meta_type": meta_type,
            "fb_user_id": fb_user_id,
            "page_id": page_id,
            "person_id": person_id,
        },
    )


# -- fields= expansion ---------------------------------------------------------------------------
@dataclass
class FieldSpec:
    name: str
    modifiers: dict[str, str] = field(default_factory=dict)
    children: list[FieldSpec] = field(default_factory=list)


class FieldSelectionError(ValueError):
    """Keep field errors inside a batch's individual response envelope."""


def parse_fields(raw: str | None) -> list[FieldSpec] | None:
    """Parse ``fields=a,b.limit(5){c,d},e{f}`` into specs (``None`` when absent).

    Bound depth so hostile field expansions cannot exhaust the Python stack.
    """
    if raw is None or not str(raw).strip():
        return None
    text = str(raw)
    if len(text) > 16384 or text.count("{") > 32 or text.count(".") + text.count("{") > 64:
        raise ValueError("fields expansion is too complex")
    pos = 0
    end = len(text)

    def read_name() -> str:
        nonlocal pos
        start = pos
        while pos < end and (text[pos].isalnum() or text[pos] in "_:@-"):
            pos += 1
        return text[start:pos]

    def read_parens() -> str:
        """Consume a balanced ``(...)`` group at ``pos``; return its contents."""
        nonlocal pos
        depth = 0
        start = pos + 1
        while pos < end:
            if text[pos] == "(":
                depth += 1
            elif text[pos] == ")":
                depth -= 1
                if depth == 0:
                    inside = text[start:pos]
                    pos += 1
                    return inside
            pos += 1
        raise ValueError("Unclosed field modifier")

    def parse_list(stop: str | None) -> list[FieldSpec]:
        nonlocal pos
        specs: list[FieldSpec] = []
        while pos < end:
            if stop and text[pos] == stop:
                pos += 1
                break
            if text[pos] in ", \t":
                pos += 1
                continue
            before = pos
            spec = parse_field()
            if spec.name:
                specs.append(spec)
            if pos == before or (pos < end and text[pos] not in ", \t" and text[pos] != stop):
                raise ValueError("Invalid fields syntax")
        if stop and (pos == 0 or text[pos - 1] != stop):
            raise ValueError("Unclosed field expansion")
        return specs

    def parse_field() -> FieldSpec:
        nonlocal pos
        root = FieldSpec(name=read_name())
        if not root.name:
            raise ValueError("A field name is required")
        leaf = root
        while pos < end:
            char = text[pos]
            if char == "." and pos + 1 < end and (text[pos + 1].isalpha() or text[pos + 1] == "_"):
                pos += 1
                name = read_name()
                if pos < end and text[pos] == "(":
                    leaf.modifiers[name] = read_parens()
                else:
                    # Dotted path ``a.b.c``: nest each segment as a child.
                    child = FieldSpec(name=name)
                    leaf.children = [child]
                    leaf = child
                continue
            if char == "{":
                pos += 1
                leaf.children = parse_list("}")
                continue
            if char == "(":
                read_parens()
                continue
            break
        return root

    result = parse_list(None)
    if not result:
        raise ValueError("A field name is required")
    return result


def format_fields(specs: list[FieldSpec]) -> str:
    return ",".join(
        s.name
        + "".join(f".{k}({v})" for k, v in s.modifiers.items())
        + ("{" + format_fields(s.children) + "}" if s.children else "")
        for s in specs
    )


def _limit_of(spec: FieldSpec) -> int | None:
    raw = spec.modifiers.get("limit")
    if raw is None:
        return None
    try:
        limit = int(raw)
        if limit < 0:
            raise ValueError
        return limit
    except ValueError:
        raise FieldSelectionError("Invalid limit modifier in fields.") from None


def _shape(value: Any, specs: list[FieldSpec] | None, spec: FieldSpec | None = None) -> Any:
    if isinstance(value, dict):
        if (
            "data" in value
            and isinstance(value["data"], list)
            and (specs is not None or (spec is not None and (spec.children or spec.modifiers)))
        ):
            # A connection: apply the requested subfields (or defaults) to each item.
            children = specs
            items = value["data"]
            limit = _limit_of(spec) if spec else None
            if limit is not None:
                items = items[:limit]
            out: dict[str, Any] = {"data": [_shape(item, children) for item in items]}
            if spec is not None and spec.modifiers.get("summary", "").lower() == "true":
                out["summary"] = (
                    value.get("summary") or value.get("_summary") or {"total_count": len(value["data"])}
                )
            for key in ("paging", "summary"):
                if key in value and key not in out:
                    out[key] = value[key]
            return out
        if specs is None:
            defaults = value.get("_defaults")
            keys = [k for k in value if not k.startswith("_")]
            if isinstance(defaults, list):
                wanted = ["id", *defaults] if "id" in value else list(defaults)
                keys = [k for k in keys if k in wanted]
            return {k: _shape(value[k], None) for k in keys}
        out = {}
        if "id" in value:
            out["id"] = value["id"]
        for child in specs:
            if child.name not in value or child.name.startswith("_"):
                if "_defaults" in value and child.name not in value.get("_known_fields", []):
                    raise FieldSelectionError(
                        f"Tried accessing nonexisting field ({child.name}) on this node."
                    )
                continue
            key = child.modifiers.get("as", child.name)
            out[key] = _shape(value[child.name], child.children or None, child)
        return out
    if isinstance(value, list):
        limit = _limit_of(spec) if spec else None
        items = value[:limit] if limit is not None else value
        return [_shape(item, specs) for item in items]
    return value


def apply_fields(value: Any, fields_param: str | None) -> Any:
    """Filter a built wire dict by ``fields=`` (or to its ``_defaults`` when absent)."""
    return _shape(value, parse_fields(fields_param))


def strip_internal(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_internal(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [strip_internal(v) for v in value]
    return value


# -- cursor paging --------------------------------------------------------------------------------
def _encode_cursor(index: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": index}).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))["o"]
        if type(value) is not int or value < 0:
            return None
        return value
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        return None


def paged(
    ctx: ProviderContext,
    request: Request,
    version: str,
    path: str,
    items: list[dict[str, Any]],
    params: dict[str, Any],
    *,
    default_limit: int = 25,
    max_limit: int = 100,
) -> dict[str, Any]:
    """Graph cursor paging over an in-memory list (``limit``/``after``/``before``)."""
    try:
        limit = int(params.get("limit", default_limit))
    except (TypeError, ValueError):
        invalid_param(ctx, "Invalid limit parameter.")
    if limit < 0 or isinstance(params.get("limit"), bool):
        invalid_param(ctx, "limit must be a non-negative integer.")
    limit = min(limit, max_limit)
    total = len(items)
    after = _decode_cursor(str(params["after"])) if params.get("after") else None
    before = _decode_cursor(str(params["before"])) if params.get("before") else None
    if (params.get("after") and after is None) or (params.get("before") and before is None):
        invalid_param(ctx, "Invalid paging cursor.")
    if params.get("after") and params.get("before"):
        invalid_param(ctx, "Specify either after or before, not both.")
    if limit == 0:
        out: dict[str, Any] = {"data": []}
        if str(params.get("summary", "")).lower() in ("true", "1"):
            out["summary"] = {"total_count": total}
        return out
    if after is not None:
        start = min(after + 1, total)
        page = items[start : start + limit]
        end = start + len(page)
    elif before is not None:
        end = min(before, total)
        start = max(0, end - limit)
        page = items[start:end]
    else:
        start, page = 0, items[:limit]
        end = len(page)
    out: dict[str, Any] = {"data": page}
    paging: dict[str, Any] = {}
    if page:
        paging["cursors"] = {"before": _encode_cursor(start), "after": _encode_cursor(end - 1)}

    def link(cursor_param: str, cursor: str) -> str:
        query = {k: str(v) for k, v in params.items() if k not in ("after", "before") and v is not None}
        query["limit"] = str(limit)
        query[cursor_param] = cursor
        return url_for_host(request, ctx, GRAPH_HOST, f"/{version}/{path}?{urlencode(query)}")

    if end < total:
        paging["next"] = link("after", _encode_cursor(end - 1))
    if start > 0:
        paging["previous"] = link("before", _encode_cursor(start))
    if paging:
        out["paging"] = paging
    if str(params.get("summary", "")).lower() in ("true", "1"):
        out["summary"] = {"total_count": total}
    return out


def picture_url(
    ctx: ProviderContext, request: Request, object_id: str, *, width: int = 50, height: int = 50
) -> str:
    path = f"/v/t39.30808-1/{object_id}.png"
    if (width, height) != (50, 50):
        path += "?" + urlencode({"width": width, "height": height})
    return url_for_host(request, ctx, "scontent-iad3-2.xx.fbcdn.net", path)


def to_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return utc(value)
    return utc(datetime.fromisoformat(str(value)))


def offset_hours(tz_name: str, at: datetime) -> int:
    try:
        from zoneinfo import ZoneInfo

        offset = at.astimezone(ZoneInfo(tz_name)).utcoffset()
        return int(offset.total_seconds() // 3600) if offset else 0
    except Exception:
        return -5


# Re-exported so call sites import clock helpers from one place.
__all__ = [
    "APP_ID",
    "APP_NAME",
    "APP_SECRET",
    "DEFAULT_VERSION",
    "DISPLAY_PHONE",
    "DISPLAY_PHONE_DIGITS",
    "GRAPH_HOST",
    "OLDEST_VERSION",
    "PAGE_ACCESS_TOKEN",
    "PAGE_SCOPES",
    "SUPPORTED_VERSIONS",
    "SYSTEM_SCOPES",
    "SYSTEM_USER_TOKEN",
    "USER_ACCESS_TOKEN",
    "USER_SCOPES",
    "VERIFY_TOKEN",
    "Auth",
    "FieldSpec",
    "app_secret_for",
    "apply_fields",
    "digits_only",
    "error_dict",
    "extract_token",
    "fail",
    "graph_time",
    "invalid_param",
    "issue_meta_token",
    "mid_for",
    "new_mid",
    "new_wamid",
    "not_found",
    "numeric_id",
    "offset_hours",
    "paged",
    "parse_fields",
    "permission_fail",
    "picture_url",
    "require_auth",
    "require_business_access",
    "require_scopes",
    "resolve_auth",
    "strip_internal",
    "to_dt",
    "trace_id",
    "unix_s",
    "wa_fail",
    "wamid",
]
