"""Slack identifier, timestamp, cursor and text helpers (no store access)."""

from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime
from typing import Any

from emulators.core.ids import stable_hash

WORKSPACE_DOMAIN = "halcyonadvisory"
WORKSPACE_URL = f"https://{WORKSPACE_DOMAIN}.slack.com"

_TS_RE = re.compile(r"^(\d{1,12})(?:\.(\d{1,6}))?$")
Ts = tuple[int, int]


def stable_id(prefix: str, *parts: object, length: int = 10) -> str:
    """``U07CC5F644F``-style id: stable for the same inputs, uppercase alphanumeric."""
    return prefix + stable_hash("slack", prefix, *parts, length=length).upper()


def parse_ts(value: object) -> Ts | None:
    """``"1697040000.000100"`` -> ``(1697040000, 100)``; ``None`` when malformed."""
    if value is None or isinstance(value, bool):
        return None
    match = _TS_RE.match(str(value).strip())
    if not match:
        return None
    return int(match.group(1)), int((match.group(2) or "").ljust(6, "0"))


def format_ts(seconds: int, micro: int) -> str:
    return f"{seconds}.{micro:06d}"


def ts_key(value: object) -> Ts:
    return parse_ts(value) or (0, 0)


def ts_from_datetime(moment: datetime) -> str:
    return format_ts(int(moment.timestamp()), moment.microsecond)


def ts_to_float(value: object) -> float:
    sec, micro = ts_key(value)
    return sec + micro / 1_000_000


def bump_ts(value: object, step: int = 1) -> str:
    """The smallest legal ts strictly after ``value`` (by at least ``step`` microseconds)."""
    sec, micro = ts_key(value)
    micro += step
    sec += micro // 1_000_000
    return format_ts(sec, micro % 1_000_000)


def permalink_ts(ts: str) -> str:
    return "p" + ts.replace(".", "")


def message_permalink(channel_id: str, ts: str, thread_ts: str | None = None) -> str:
    link = f"{WORKSPACE_URL}/archives/{channel_id}/{permalink_ts(ts)}"
    if thread_ts and thread_ts != ts:
        link += f"?thread_ts={thread_ts}&cid={channel_id}"
    return link


def encode_cursor(kind: str, value: str) -> str:
    return base64.b64encode(f"{kind}:{value}".encode()).decode()


def decode_cursor(cursor: str, kind: str) -> str | None:
    """Value inside a cursor of ``kind``; ``None`` when it is malformed or of another kind."""
    try:
        text = base64.b64decode(cursor.encode(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    prefix, sep, value = text.partition(":")
    if not sep or prefix != kind or not value:
        return None
    return value


def slugify(text: str) -> str:
    """``"Fees & Staffing"`` -> ``"fees-staffing"``."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug


def escape_text(text: str) -> str:
    """Slack's only mandatory escapes in message text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape_text(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def clean(record: dict[str, Any], *, keep_id: bool = True) -> dict[str, Any]:
    """Drop emulator-internal (underscore-prefixed) keys from a stored record.

    Messages have no wire ``id`` (the store key is ``<channel>:<ts>``), so they pass
    ``keep_id=False``.
    """
    return {k: v for k, v in record.items() if not k.startswith("_") and (keep_id or k != "id")}


def digits_from(*parts: object, length: int = 12) -> str:
    """Stable numeric string, used for the numeric segments of ``xoxb-`` style tokens."""
    return str(int(stable_hash("slack-digits", *parts, length=16), 16))[:length].rjust(length, "1")
