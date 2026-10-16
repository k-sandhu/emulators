"""Cursor pagination in Slack's style (``limit`` + opaque base64 ``cursor``)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from emulators.providers.slack.runtime import Call, SlackError
from emulators.providers.slack.util import decode_cursor, encode_cursor


def paginate(
    items: list[Any],
    call: Call,
    *,
    kind: str,
    key: Callable[[Any], str],
    default_limit: int = 100,
    max_limit: int = 1000,
) -> tuple[list[Any], dict[str, str]]:
    """Slice ``items`` by ``limit``/``cursor``; returns the page and ``response_metadata``.

    The cursor names the first item of the next page, so concurrent inserts never repeat or
    skip entries. ``next_cursor`` is ``""`` on the last page, as Slack does.
    """
    limit = call.params.int("limit", default_limit, lo=1, hi=max_limit) or default_limit
    start = 0
    cursor = call.params.str("cursor")
    if cursor:
        value = decode_cursor(cursor, kind)
        keys = [key(item) for item in items]
        if value is None or value not in keys:
            raise SlackError("invalid_cursor")
        start = keys.index(value)
    page = items[start : start + limit]
    more = start + limit < len(items)
    return page, {"next_cursor": encode_cursor(kind, key(items[start + limit])) if more else ""}
