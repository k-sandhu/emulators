"""Validate wire values before they can mutate stored Notion objects."""

from __future__ import annotations

import math
import re
import sys
from datetime import datetime
from typing import Any, NoReturn
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from emulators.core.clock import parse_instant
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext

COLORS = {
    "default",
    "gray",
    "brown",
    "orange",
    "yellow",
    "green",
    "blue",
    "purple",
    "pink",
    "red",
    "gray_background",
    "brown_background",
    "orange_background",
    "yellow_background",
    "green_background",
    "blue_background",
    "purple_background",
    "pink_background",
    "red_background",
}


def invalid(message: str) -> NoReturn:
    # Imported lazily because common's rich text normalization uses these validators.
    from emulators.providers.notion.common import invalid as reject

    reject(message)


def string(value: Any, name: str, *, maximum: int | None = None, empty: bool = True) -> str:
    if not isinstance(value, str) or (not empty and not value) or (maximum and len(value) > maximum):
        invalid(f"{name} must be a string" + (f" of at most {maximum} characters." if maximum else "."))
    return value


def url(value: Any, name: str = "url") -> str:
    value = string(value, name, maximum=2000, empty=False)
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or any(c.isspace() for c in value):
            invalid(f"{name} must be a valid URL.")
        if parsed.scheme in ("http", "https") and not parsed.netloc:
            invalid(f"{name} must be a valid URL.")
    except ValueError:
        invalid(f"{name} must be a valid URL.")
    return value


def color(value: Any) -> str:
    if not isinstance(value, str) or value not in COLORS:
        invalid("color must be a supported Notion color.")
    return value


def json_values(value: Any, depth: int = 0) -> None:
    """Reject values Python's JSON decoder accepts but JSON responses cannot encode."""
    if depth > 64:
        invalid("Request JSON is nested too deeply.")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            invalid("Strings must contain valid Unicode characters.")
    elif isinstance(value, float) and not math.isfinite(value):
        invalid("Numbers must be finite.")
    elif isinstance(value, int) and abs(value) > sys.float_info.max:
        invalid("Numbers must be within the supported numeric range.")
    elif isinstance(value, dict):
        for key, item in value.items():
            json_values(key, depth + 1)
            json_values(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            json_values(item, depth + 1)


def date_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("start"), str):
        invalid("date must contain a start date.")
    start = iso_date(value["start"])
    end = value.get("end")
    if end is not None and iso_date(end) < start:
        invalid("date.end must be on or after date.start.")
    zone = value.get("time_zone")
    if zone is not None:
        string(zone, "date.time_zone", empty=False)
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            invalid("date.time_zone must be an IANA time zone.")
        if any(len(v) <= 10 or re.search(r"(?:Z|[+-]\d{2}:\d{2})$", v) for v in (value["start"], end) if v):
            invalid("Dates with time_zone must include a time without a UTC offset.")
    return {"start": value["start"], "end": end, "time_zone": zone}


def iso_date(value: Any) -> datetime:
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T)", value):
        invalid("Expected an ISO 8601 date.")
    try:
        return parse_instant(value)
    except (ValueError, TypeError, OverflowError):
        invalid("Expected an ISO 8601 date.")


def archive_value(payload: dict[str, Any]) -> bool | None:
    for field in ("archived", "in_trash"):
        if field in payload and not isinstance(payload[field], bool):
            invalid("archived/in_trash must be a boolean.")
    if "archived" in payload and "in_trash" in payload and payload["archived"] != payload["in_trash"]:
        invalid("archived and in_trash must have the same value.")
    return payload.get("in_trash", payload.get("archived"))


def editable(ctx: ProviderContext, record: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
    from emulators.providers.notion.common import ancestors

    chain = ancestors(ctx, record)
    if any(r.get("archived") for r in chain[1:]) or (
        record.get("archived") and (payload is None or archive_value(payload) is not False)
    ):
        invalid("Cannot edit archived content. Restore it and its ancestors first.")


def not_json(_: str) -> NoReturn:
    from emulators.providers.notion.common import error_body

    raise EmulatorError(400, error_body(400, "invalid_json", "Error parsing JSON body."))
