"""Binary request/response helpers: multipart/form-data uploads and blob downloads."""

from __future__ import annotations

import base64
import json
import mimetypes
from email import policy
from email.parser import BytesParser
from typing import Any

from emulators.providers.salesforce.common import bad_request


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, Any], str | None]:
    """Salesforce's multipart insert: a JSON ``entity_content`` part plus one binary part per blob field.

    The binary parts come back base64-encoded under their part name (``VersionData``, ``Body``...) so the normal
    field coercion stores them. Also returns the uploaded filename, which callers use as a default title/name.
    """
    from emulators.providers.salesforce.rest_core import _reject_constant, require_dict

    raw = b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode() + b"\r\n\r\n" + body
    message = BytesParser(policy=policy.HTTP).parsebytes(raw)
    if not message.is_multipart():
        raise bad_request("JSON_PARSER_ERROR", "Malformed multipart request")
    data: dict[str, Any] = {}
    files: dict[str, str] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if name == "entity_content" or (name is None and part.get_content_type() == "application/json"):
            try:
                parsed = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
            except (ValueError, RecursionError) as exc:
                raise bad_request("JSON_PARSER_ERROR", "entity_content is not valid JSON") from exc
            data.update(require_dict(parsed, "entity_content"))
        elif name:
            data[str(name)] = base64.b64encode(payload).decode()
            filename = part.get_filename()
            if filename:
                files[str(name)] = filename
    return data, next(iter(files.values()), None)


def guess_mime(name: str | None) -> str:
    return mimetypes.guess_type(name or "")[0] or "application/octet-stream"
