"""Controllable clock shared by every emulator in a hub.

Tests need to reason about token expiry, webhook timestamps and "recent" data, so all
providers read time from this object instead of ``datetime.now()``. The clock can run
live, be frozen at an instant, or be advanced (``POST /_emulator/clock``).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta


def utc(dt: datetime) -> datetime:
    """Return ``dt`` as an aware UTC datetime (naive values are assumed to be UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_instant(value: str) -> datetime:
    """Parse an ISO-8601 instant or date (``2026-10-01``) into aware UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) == 10:
        text += "T00:00:00+00:00"
    return utc(datetime.fromisoformat(text))


class Clock:
    """A wall clock with an adjustable offset and an optional frozen instant."""

    def __init__(self, frozen_at: datetime | None = None) -> None:
        self._lock = threading.Lock()
        self._offset = timedelta(0)
        self._frozen: datetime | None = utc(frozen_at) if frozen_at else None

    def now(self) -> datetime:
        with self._lock:
            if self._frozen is not None:
                return self._frozen
            return datetime.now(UTC) + self._offset

    def timestamp(self) -> int:
        return int(self.now().timestamp())

    def advance(self, delta: timedelta) -> datetime:
        with self._lock:
            if self._frozen is not None:
                self._frozen += delta
            else:
                self._offset += delta
        return self.now()

    def freeze(self, at: datetime | None = None) -> datetime:
        instant = utc(at) if at else self.now()
        with self._lock:
            self._frozen = instant
        return instant

    def unfreeze(self) -> datetime:
        with self._lock:
            if self._frozen is not None:
                self._offset = self._frozen - datetime.now(UTC)
            self._frozen = None
        return self.now()

    @property
    def frozen(self) -> bool:
        return self._frozen is not None


def isoformat_z(dt: datetime, *, timespec: str = "seconds") -> str:
    """``2026-10-16T09:30:00Z`` — the most common wire format across these APIs."""
    return utc(dt).isoformat(timespec=timespec).replace("+00:00", "Z")


def graph_datetime(dt: datetime) -> str:
    """Graph's 7-digit fractional form used in ``dateTimeTimeZone`` values (no offset)."""
    return utc(dt).strftime("%Y-%m-%dT%H:%M:%S.0000000")
