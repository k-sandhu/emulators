"""Keep OAuth credentials out of the shared diagnostic request journal."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode

from emulators.core.middleware import RequestJournal

SENSITIVE = {"client_secret", "access_token", "refresh_token", "code", "token"}


class HubSpotJournal(RequestJournal):
    def append(self, entry: dict[str, Any]) -> None:
        entry = dict(entry)
        for prefix in ("/oauth/v1/access-tokens/", "/oauth/v1/refresh-tokens/"):
            if entry["path"].startswith(prefix):
                entry["path"] = prefix + "[redacted]"
        if entry["path"].startswith("/oauth/"):
            entry["query"] = urlencode(
                [
                    (k, "[redacted]" if k in SENSITIVE else v)
                    for k, v in parse_qsl(entry.get("query", ""), keep_blank_values=True)
                ]
            )
            content = entry.get("body")
            if isinstance(content, str):
                entry["body"] = urlencode(
                    [
                        (k, "[redacted]" if k in SENSITIVE else v)
                        for k, v in parse_qsl(content, keep_blank_values=True)
                    ]
                )
            elif isinstance(content, dict):
                entry["body"] = {k: "[redacted]" if k in SENSITIVE else v for k, v in content.items()}
        super().append(entry)
