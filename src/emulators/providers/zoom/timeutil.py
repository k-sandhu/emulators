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


def day_range(
    from_raw: str | None,
    to_raw: str | None,
    *,
    default_from: date,
    default_to: date,
    max_days: int = 30,
) -> tuple[date, date]:
    """Resolve Zoom's ``from``/``to`` (``yyyy-mm-dd``) with defaults, as an inclusive day range."""
    from emulators.providers.zoom.common import E_VALIDATION, invalid_param, zoom_error

    try:
        lo = parse_day(from_raw) if from_raw else default_from
    except ValueError:
        raise invalid_param("from") from None
    try:
        hi = parse_day(to_raw) if to_raw else default_to
    except ValueError:
        raise invalid_param("to") from None
    if hi < lo:
        raise zoom_error(400, E_VALIDATION, "Invalid date range: 'from' must not be after 'to'.")
    if (hi - lo).days > max_days:
        raise zoom_error(400, E_VALIDATION, f"The date range must not exceed {max_days} days.")
    return lo, hi


def in_days(moment: str, lo: date, hi: date) -> bool:
    """Is the ISO instant within the inclusive UTC day range?"""
    return lo <= parse_instant(moment).date() <= hi
