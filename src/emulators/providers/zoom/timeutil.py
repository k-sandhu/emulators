"""Date and time parsing for Zoom's request formats."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_instant(value: str, tz_name: str | None = None) -> datetime:
    """``yyyy-MM-ddTHH:mm:ssZ`` is GMT; a bare ``yyyy-MM-ddTHH:mm:ss`` is local time in ``tz_name``."""
    text = str(value).strip()
    if len(text) == 10:
        text += "T00:00:00"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone(tz_name))
    return parsed.astimezone(UTC)


def parse_day(value: str) -> date:
    return date.fromisoformat(str(value).strip()[:10])


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True
