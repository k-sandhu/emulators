"""Keep malformed simulation inputs from leaving half-started meetings or crashing actions."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from emulators.providers.zoom.common import invalid_field
from emulators.providers.zoom.validation import validate

TEXT = {"type": "string", "maxLength": 4096}
BOOL = {"type": "boolean"}
NUMBER = {"type": "number", "minimum": 0, "maximum": 1440}
TEXTS = {"type": "array", "items": TEXT, "maxItems": 1000}
SCOPES = {"anyOf": [TEXT, TEXTS]}
WHO = {
    "user": TEXT,
    "email": TEXT,
    "name": TEXT,
    "person_id": TEXT,
    "join_offset_minutes": NUMBER,
    "stay_minutes": NUMBER,
    "show_email": BOOL,
}
PARTICIPANT = {"anyOf": [TEXT, {"type": "object", "properties": WHO}]}
SCHEMAS: dict[str, dict[str, Any]] = {
    "mint_token": {
        "user": TEXT,
        "scopes": SCOPES,
        "app_only": BOOL,
        "client_id": TEXT,
        "expires_in": {"type": "integer", "minimum": -86400, "maximum": 7776000},
    },
    "register_app": {
        "client_id": TEXT,
        "client_secret": TEXT,
        "redirect_uris": TEXTS,
        "scopes": SCOPES,
        "name": TEXT,
        "account_id": TEXT,
    },
    "configure_webhook": {
        "url": TEXT,
        "secret_token": TEXT,
        "events": SCOPES,
        "skip_validation": BOOL,
        "verification_token": TEXT,
    },
    "remove_webhook": {"id": TEXT, "url": TEXT},
    "start_meeting": {
        "occurrence_id": TEXT,
        "participants": {
            "anyOf": [
                {"type": "string", "enum": ["all"]},
                {"type": "array", "items": PARTICIPANT, "maxItems": 300},
            ]
        },
    },
    "participant_join": WHO,
    "participant_leave": {
        "participant_id": TEXT,
        "user": TEXT,
        "email": TEXT,
        "name": TEXT,
        "after_minutes": NUMBER,
    },
    "end_meeting": {
        "record": BOOL,
        "transcribe": BOOL,
        "duration_minutes": NUMBER,
        "chat": {
            "type": "array",
            "maxItems": 1000,
            "items": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "from": TEXT,
                    "name": TEXT,
                    "email": TEXT,
                    "text": TEXT,
                    "offset_s": {"type": "number", "minimum": 0, "maximum": 86400},
                },
            },
        },
    },
    "replay_event": {"event": TEXT},
}


def action_body(name: str, payload: dict[str, Any]) -> None:
    fields = {"meeting_id": {"anyOf": [TEXT, {"type": "integer"}]}, "id": TEXT, "uuid": TEXT}
    validate(payload, {"type": "object", "properties": {**fields, **SCHEMAS.get(name, {})}})
    if name == "configure_webhook" and "url" in payload:
        try:
            parsed = urlparse(payload["url"])
        except ValueError:
            raise invalid_field("url") from None
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise invalid_field("url")
