"""Validate Graph parameters before handlers mutate vendor records."""

from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request

from emulators.core.provider import ProviderContext
from emulators.providers.meta.common import invalid_param


def object_value(ctx: ProviderContext, value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        invalid_param(ctx, f"{name} must be a JSON object.")
    return value


def string_value(
    ctx: ProviderContext, value: Any, name: str, *, maximum: int | None = None, empty: bool = False
) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        invalid_param(ctx, f"{name} must be a {'non-empty ' if not empty else ''}string.")
    if maximum is not None and len(value) > maximum:
        invalid_param(ctx, f"{name} exceeds the maximum length of {maximum} characters.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        invalid_param(ctx, f"{name} contains invalid Unicode.")
    return value


def list_value(ctx: ProviderContext, value: Any, name: str, *, maximum: int = 100) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        invalid_param(ctx, f"{name} must be an array with at most {maximum} entries.")
    return value


def number_value(ctx: ProviderContext, value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        invalid_param(ctx, f"{name} must be a number.")
    if not math.isfinite(value) or not low <= value <= high:
        invalid_param(ctx, f"{name} must be between {low} and {high}.")
    return float(value)


def http_url(ctx: ProviderContext, value: Any, name: str) -> str:
    url = string_value(ctx, value, name, maximum=4096)
    try:
        parts = urlsplit(url)
    except ValueError:
        invalid_param(ctx, f"{name} must be an HTTP(S) URL.")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        invalid_param(ctx, f"{name} must be an HTTP(S) URL.")
    return url


def decode_parameter(ctx: ProviderContext, value: Any, name: str) -> Any:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            invalid_param(ctx, f"{name} must contain valid JSON.")
        validate_json(ctx, parsed)
        return parsed
    return value


def validate_json(ctx: ProviderContext, value: Any) -> None:
    """Reject values that would poison a later JSON response from the store."""
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            invalid_param(ctx, "JSON nesting exceeds the supported depth.")
        if isinstance(item, str):
            string_value(ctx, item, "JSON string", empty=True)
        elif isinstance(item, float) and not math.isfinite(item):
            invalid_param(ctx, "Non-finite JSON numbers are not supported.")
        elif isinstance(item, dict):
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif isinstance(item, list):
            pending.extend((part, depth + 1) for part in item)


def integer_value(ctx: ProviderContext, value: Any, name: str, *, maximum: int = 315360000) -> int:
    if isinstance(value, str) and value.isascii() and value.removeprefix("-").isdigit() and len(value) <= 10:
        value = int(value)
    if type(value) is not int or not -maximum <= value <= maximum:
        invalid_param(ctx, f"{name} must be an integer between {-maximum} and {maximum}.")
    return value


async def multipart(ctx: ProviderContext, request: Request) -> Any:
    try:
        return await request.form()
    except (ValueError, UnicodeError) as exc:
        invalid_param(ctx, f"Invalid multipart form: {type(exc).__name__}.")
