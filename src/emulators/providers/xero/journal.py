"""Redact OAuth form credentials before the shared request journal retains them."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode

from emulators.core.middleware import RequestJournal

SENSITIVE = {"client_secret", "code", "code_verifier", "refresh_token", "access_token", "id_token", "token"}


def redact_form(value: str) -> str:
    return urlencode(
        [
            (key, "[REDACTED]" if key.lower() in SENSITIVE else item)
            for key, item in parse_qsl(value, keep_blank_values=True)
        ]
    )


class XeroJournal(RequestJournal):
    def append(self, entry: dict[str, Any]) -> None:
        entry = dict(entry)
        if entry.get("path", "").lower() in {"/connect/token", "/connect/revocation"}:
            body = entry.get("body")
            if isinstance(body, str):
                entry["body"] = redact_form(body)
            elif isinstance(body, dict):
                entry["body"] = {k: "[REDACTED]" if k.lower() in SENSITIVE else v for k, v in body.items()}
        if entry.get("query"):
            entry["query"] = redact_form(entry["query"])
        super().append(entry)
