"""Exceptions that render as provider-shaped HTTP responses.

Each vendor has its own error envelope (Graph ``{"error": {"code", "message"}}``,
Stripe ``{"error": {"type", "code", "message", "param"}}``, Dropbox
``{"error_summary", "error": {".tag"}}``, Salesforce ``[{"errorCode", "message"}]``...).
Providers build those bodies themselves and raise :class:`EmulatorError`; the app
factory renders the body verbatim.
"""

from __future__ import annotations

from typing import Any


class EmulatorError(Exception):
    """Raise from a route to return ``body`` with ``status`` exactly as given."""

    def __init__(
        self,
        status: int,
        body: Any,
        *,
        headers: dict[str, str] | None = None,
        media_type: str = "application/json",
    ) -> None:
        super().__init__(f"{status}: {body!r}"[:300])
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.media_type = media_type
