"""Box error envelopes.

API errors (``/2.0/...``) use ``{"type": "error", "status", "code", "message", "request_id"...}``;
the OAuth token endpoint uses RFC 6749 ``{"error", "error_description"}``.
"""

from __future__ import annotations

import secrets
from typing import Any

from emulators.core.errors import EmulatorError

HELP_URL = "http://developers.box.com/docs/#errors"

#: HTTP status -> (code, message) used for generic framework errors and injected faults.
STATUS_DEFAULTS: dict[int, tuple[str, str]] = {
    400: ("bad_request", "Bad Request"),
    401: ("unauthorized", "Unauthorized"),
    403: ("access_denied_insufficient_permissions", "Access denied - insufficient permission"),
    404: ("not_found", "Not Found"),
    405: ("method_not_allowed", "Method Not Allowed"),
    409: ("conflict", "Conflict"),
    410: ("gone", "Gone"),
    412: ("precondition_failed", "Precondition Failed"),
    413: ("request_entity_too_large", "Request Entity Too Large"),
    415: ("unsupported_media_type", "Unsupported Media Type"),
    429: ("rate_limit_exceeded", "Request rate limit exceeded, please try again later"),
    500: ("internal_server_error", "Internal Server Error"),
    502: ("bad_gateway", "Bad Gateway"),
    503: ("unavailable", "Service Unavailable"),
    504: ("gateway_timeout", "Gateway Timeout"),
}

WWW_AUTHENTICATE = 'Bearer realm="Service", error="invalid_token", error_description="The access token provided is invalid."'


def request_id() -> str:
    return secrets.token_hex(8)


def error_body(
    status: int, code: str, message: str, *, context_info: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"type": "error", "status": status, "code": code}
    if context_info:
        body["context_info"] = context_info
    body["help_url"] = HELP_URL
    body["message"] = message
    body["request_id"] = request_id()
    return body


def box_error(
    status: int,
    code: str | None = None,
    message: str | None = None,
    *,
    context_info: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> EmulatorError:
    default_code, default_message = STATUS_DEFAULTS.get(status, ("error", "Error"))
    body = error_body(status, code or default_code, message or default_message, context_info=context_info)
    return EmulatorError(status, body, headers=headers)


def unauthorized(description: str | None = None) -> EmulatorError:
    header = WWW_AUTHENTICATE
    if description:
        header = f'Bearer realm="Service", error="invalid_token", error_description="{description}"'
    return box_error(401, headers={"WWW-Authenticate": header})


def unauthorized_missing() -> EmulatorError:
    return box_error(
        401,
        headers={
            "WWW-Authenticate": 'Bearer realm="Service", error="invalid_request", '
            'error_description="The access token is missing or malformed."'
        },
    )


def not_found(message: str = "Not Found", *, context_info: dict[str, Any] | None = None) -> EmulatorError:
    return box_error(404, "not_found", message, context_info=context_info)


def trashed(message: str = "Item is trashed") -> EmulatorError:
    return box_error(404, "trashed", message)


def forbidden(message: str = "Access denied - insufficient permission") -> EmulatorError:
    return box_error(403, "access_denied_insufficient_permissions", message)


def bad_request(
    message: str = "Bad Request", *, reason: str | None = None, name: str | None = None, code: str | None = None
) -> EmulatorError:
    context: dict[str, Any] | None = None
    if reason or name:
        context = {"errors": [{"reason": reason or "invalid_parameter", "name": name, "message": message}]}
    return box_error(400, code or "bad_request", message, context_info=context)


def missing_parameter(name: str) -> EmulatorError:
    return box_error(
        400,
        "bad_request",
        "Bad Request",
        context_info={
            "errors": [{"reason": "missing_parameter", "name": name, "message": f"'{name}' is required"}]
        },
    )


def invalid_parameter(name: str, message: str | None = None) -> EmulatorError:
    return box_error(
        400,
        "bad_request",
        "Bad Request",
        context_info={
            "errors": [
                {"reason": "invalid_parameter", "name": name, "message": message or f"Invalid value for '{name}'"}
            ]
        },
    )


def name_in_use(conflicts: Any) -> EmulatorError:
    return box_error(
        409,
        "item_name_in_use",
        "Item with the same name already exists",
        context_info={"conflicts": conflicts},
    )


def precondition_failed() -> EmulatorError:
    return box_error(412, "precondition_failed", "The resource has been changed since the If-Match etag.")


def oauth_error(error: str, description: str, status: int = 400) -> EmulatorError:
    return EmulatorError(
        status,
        {"error": error, "error_description": description},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
