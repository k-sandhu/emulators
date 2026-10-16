"""Keep OAuth credentials out of the request journal without changing core middleware."""

from __future__ import annotations

from typing import Any

from emulators.core.middleware import RequestJournal

SENSITIVE = {"token", "access_token", "refresh_token", "client_secret", "code", "verification_token"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key in SENSITIVE else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class NotionJournal(RequestJournal):
    def append(self, entry: dict[str, Any]) -> None:
        entry["body"] = redact(entry.get("body"))
        super().append(entry)
