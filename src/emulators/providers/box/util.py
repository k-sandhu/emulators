"""Small shared helpers: Box timestamps, ids, ``fields=`` parsing, paging markers, request bodies."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import string
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request

from emulators.core.provider import ProviderContext
from emulators.providers.box import errors

BOX_TZ = ZoneInfo("America/Los_Angeles")
ALNUM = string.ascii_letters + string.digits
LOWER_ALNUM = string.ascii_lowercase + string.digits

ENTERPRISE_TYPE = "enterprise"


# -- time ------------------------------------------------------------------------------
def to_iso(dt: datetime) -> str:
    """Canonical stored form: aware UTC, whole seconds, ``+00:00``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_ts(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def try_parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return parse_ts(value)
    except ValueError:
        return None


def box_ts(value: str | None) -> str | None:
    """Render a stored UTC timestamp the way Box does: Pacific time with an offset."""
    if not value:
        return None
    return parse_ts(value).astimezone(BOX_TZ).isoformat(timespec="seconds")


def now_iso(ctx: ProviderContext) -> str:
    return to_iso(ctx.clock.now())


def input_ts(value: Any, name: str) -> str:
    """Validate a client-supplied RFC 3339 timestamp and return the stored form."""
    if not isinstance(value, str):
        raise errors.invalid_parameter(name, f"'{name}' must be an RFC 3339 timestamp")
    try:
        return to_iso(parse_ts(value))
    except ValueError:
        raise errors.invalid_parameter(name, f"Invalid date format for '{name}'") from None


# -- ids and tokens --------------------------------------------------------------------
def random_alnum(n: int) -> str:
    return "".join(secrets.choice(ALNUM) for _ in range(n))


def random_lower(n: int) -> str:
    return "".join(secrets.choice(LOWER_ALNUM) for _ in range(n))


def derived_number(seed: str, kind: str, key: str, low: int, high: int) -> int:
    digest = hashlib.sha256(f"box/{seed}/{kind}/{key}".encode()).hexdigest()
    return low + int(digest[:15], 16) % (high - low)


def next_id(ctx: ProviderContext, kind: str, existing: Any, *, start: int) -> str:
    """Mint a runtime id from a counter kept in ``store.meta`` (restored by ``reset``)."""
    key = f"seq:{kind}"
    while True:
        value = ctx.store.meta.get(key, start) + 1
        ctx.store.meta[key] = value
        if str(value) not in existing:
            return str(value)


def sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def sha1_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data, usedforsecurity=False).digest()).decode()


def etag_of(seq: int) -> str:
    return str(seq)


# -- query helpers ---------------------------------------------------------------------
def parse_fields(request: Request) -> list[str] | None:
    raw = request.query_params.get("fields")
    if raw is None:
        return None
    fields = [f.strip() for f in raw.split(",") if f.strip()]
    return fields or None


def query_int(request: Request, name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise errors.invalid_parameter(name, f"'{name}' must be an integer") from None
    if value < minimum:
        raise errors.invalid_parameter(name, f"'{name}' must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise errors.invalid_parameter(name, f"'{name}' must be at most {maximum}")
    return value


def query_bool(request: Request, name: str, default: bool = False) -> bool:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes")


def query_list(request: Request, name: str) -> list[str]:
    values: list[str] = []
    for raw in request.query_params.getlist(name):
        values.extend(part.strip() for part in raw.split(",") if part.strip())
    return values


MARKER_PREFIX = "box-marker:"


def encode_marker(offset: int) -> str:
    return base64.urlsafe_b64encode(f"{MARKER_PREFIX}{offset}".encode()).decode().rstrip("=")


def decode_marker(marker: str | None) -> int:
    if not marker:
        return 0
    try:
        text = base64.urlsafe_b64decode(marker + "=" * (-len(marker) % 4)).decode()
        if text.startswith(MARKER_PREFIX):
            return int(text[len(MARKER_PREFIX) :])
    except (ValueError, UnicodeDecodeError):
        pass
    raise errors.invalid_parameter("marker", "Invalid marker")


def page_window(request: Request, *, default_limit: int, max_limit: int) -> tuple[int, int, bool]:
    """``(offset, limit, use_marker)`` from ``offset``/``limit``/``usemarker``/``marker``."""
    limit = query_int(request, "limit", default_limit, minimum=0, maximum=max_limit)
    use_marker = query_bool(request, "usemarker")
    if use_marker:
        return decode_marker(request.query_params.get("marker")), limit, True
    return query_int(request, "offset", 0), limit, False


def paginate(
    entries: list[Any],
    offset: int,
    limit: int,
    use_marker: bool,
    *,
    order: list[dict[str, str]] | None = None,
    with_total: bool = True,
) -> dict[str, Any]:
    window = entries[offset : offset + limit]
    body: dict[str, Any] = {}
    if use_marker:
        more = offset + limit < len(entries)
        body["limit"] = limit
        body["next_marker"] = encode_marker(offset + limit) if more else None
        body["prev_marker"] = encode_marker(max(offset - limit, 0)) if offset > 0 else None
        if order is not None:
            body["order"] = order
        body["entries"] = window
        return body
    if with_total:
        body["total_count"] = len(entries)
    body["entries"] = window
    body["offset"] = offset
    body["limit"] = limit
    if order is not None:
        body["order"] = order
    return body


# -- request bodies --------------------------------------------------------------------
async def json_body(request: Request, *, required: bool = True) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        if required:
            raise errors.bad_request("Bad Request", reason="invalid_request_body", name="body")
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise errors.bad_request("Invalid JSON in request body", reason="invalid_request_body") from None
    if not isinstance(data, dict):
        raise errors.bad_request("Request body must be a JSON object", reason="invalid_request_body")
    return data


def require_str(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if value is None or value == "":
        raise errors.missing_parameter(name)
    if not isinstance(value, str):
        raise errors.invalid_parameter(name, f"'{name}' must be a string")
    return value


def ref_id(body: dict[str, Any], name: str) -> str:
    """``{"parent": {"id": "123"}}`` -> ``"123"`` with Box's validation errors."""
    value = body.get(name)
    if not isinstance(value, dict) or value.get("id") in (None, ""):
        raise errors.missing_parameter(f"{name}.id" if isinstance(value, dict) else name)
    return str(value["id"])


# -- names -----------------------------------------------------------------------------
_RESERVED = re.compile(r"[\\/]")


def validate_name(name: Any, field: str = "name") -> str:
    if name is None or name == "":
        raise errors.missing_parameter(field)
    if not isinstance(name, str):
        raise errors.invalid_parameter(field, f"'{field}' must be a string")
    if len(name) > 255:
        raise errors.invalid_parameter(field, "Item name is too long (maximum 255 characters)")
    if name != name.strip() or name in (".", "..") or _RESERVED.search(name) or not name.strip():
        raise errors.invalid_parameter(field, f"Invalid value '{name}'. Item names must not contain / or \\, or start or end with whitespace.")
    return name


def extension_of(name: str) -> str:
    return name.rsplit(".", 1)[1].lower() if "." in name.strip(".") else ""


def unique_copy_name(name: str, taken: set[str]) -> str:
    """Name for an auto-renamed duplicate: ``a.docx`` -> ``a (1).docx``."""
    if name.lower() not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 1
    while True:
        candidate = f"{stem} ({n}){'.' + ext if ext else ''}"
        if candidate.lower() not in taken:
            return candidate
        n += 1
