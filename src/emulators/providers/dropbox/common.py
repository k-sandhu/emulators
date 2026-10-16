"""Wire-level plumbing for the Dropbox emulator: error envelopes, argument parsing, ids,
timestamps, HTTP-header-safe JSON and content hashing.

Dropbox error conventions (all reproduced here):

* route errors  -> ``409`` ``{"error_summary": "path/not_found/...", "error": {".tag": ...}}``
* bad input     -> ``400`` ``text/plain`` ``Error in call to API function "<route>": <reason>``
* auth errors   -> ``401`` ``{"error_summary": "invalid_access_token/...", "error": {".tag": ...}}``
* rate limiting -> ``429`` ``{"error": {"reason": {".tag": "too_many_requests"}, "retry_after": N}}``
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from emulators.core.errors import EmulatorError

DBX_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
BLOCK = 4 * 1024 * 1024
MISSING: Any = object()

_P = r"(/(.|[\r\n])*)"
_NS = r"(ns:[0-9]+(/(.|[\r\n])*)?)"
PATH_R_OR_ID = rf"{_P}?|id:.*|{_NS}"  # list_folder / search path ("" = root)
READ_PATH = rf"(/(.|[\r\n])*|id:.*)|(rev:[0-9a-f]{{9,}})|{_NS}"  # get_metadata / download / export / ...
WRITE_PATH = rf"{_P}|{_NS}"  # create_folder / restore
WRITE_OR_ID = rf"{_P}|{_NS}|(id:.*)"  # delete / move / copy / upload / commit / lock
REVISIONS_PATH = rf"/(.|[\r\n])*|id:.*|{_NS}"
PATH_ONLY = r"/(.|[\r\n])*"


# ---------------------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------------------
def ts(dt: datetime) -> str:
    """``2026-10-15T09:12:44Z`` -- the only timestamp format Dropbox emits."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime(DBX_FORMAT)


def parse_ts(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# ---------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------
class BadInput(Exception):
    """Invalid request; rendered as a 400 ``text/plain`` with the route name prefixed."""


def _child_union(node: dict[str, Any], tag: str) -> dict[str, Any] | None:
    for key in (tag, "reason", "error", "failure"):
        child = node.get(key)
        if isinstance(child, dict) and ".tag" in child:
            return child
    return None


def error_summary(error: dict[str, Any]) -> str:
    """``path/not_found/...``: the chain of union tags, as Dropbox composes ``error_summary``."""
    tags: list[str] = []
    node: dict[str, Any] | None = error
    while node is not None and ".tag" in node:
        tag = node[".tag"]
        tags.append(tag)
        node = _child_union(node, tag)
    return "/".join(tags) + "/..."


def api_error(
    error: dict[str, Any],
    status: int = 409,
    *,
    user_message: str | None = None,
    headers: dict[str, str] | None = None,
) -> EmulatorError:
    body: dict[str, Any] = {"error_summary": error_summary(error), "error": error}
    if user_message:
        body["user_message"] = {"text": user_message, "locale": "en"}
    return EmulatorError(status, body, headers=headers)


def tag(name: str, /, **fields: Any) -> dict[str, Any]:
    return {".tag": name, **fields}


def path_error(kind: str, field: str = "path", **fields: Any) -> EmulatorError:
    """409 for a union like ``{".tag": "path", "path": {".tag": "not_found"}}``."""
    return api_error({".tag": field, field: {".tag": kind, **fields}})


def lookup_error(kind: str = "not_found", wrapper: str = "path", **fields: Any) -> EmulatorError:
    return path_error(kind, wrapper, **fields)


def text_error(status: int, message: str, route: str | None = None) -> EmulatorError:
    prefix = f'Error in call to API function "{route}": ' if route else ""
    return EmulatorError(status, prefix + message, media_type="text/plain")


def auth_error(kind: str, **fields: Any) -> EmulatorError:
    return api_error({".tag": kind, **fields}, 401)


def missing_scope(scope: str) -> EmulatorError:
    return api_error({".tag": "missing_scope", "required_scope": scope}, 401)


# ---------------------------------------------------------------------------------------
# Request arguments
# ---------------------------------------------------------------------------------------
class Args:
    """Typed access to a decoded JSON argument object with Dropbox-style 400 messages."""

    def __init__(self, data: Any, where: str = "request body") -> None:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise BadInput(f"{where}: expected object, got {type(data).__name__}")
        self.data: dict[str, Any] = data
        self.where = where

    def has(self, key: str) -> bool:
        return self.data.get(key) is not None

    def raw(self, key: str, default: Any = None) -> Any:
        value = self.data.get(key)
        return default if value is None else value

    def _fail(self, key: str, message: str) -> BadInput:
        return BadInput(f"{self.where}: {key}: {message}")

    def str(
        self, key: str, default: Any = MISSING, *, pattern: str | None = None, nonempty: bool = False
    ) -> Any:
        value = self.data.get(key)
        if value is None:
            if default is MISSING:
                raise self._fail(key, "missing required field")
            return default
        if not isinstance(value, str):
            raise self._fail(key, f"expected string, got {type(value).__name__}")
        if pattern is not None and re.fullmatch(pattern, value, re.DOTALL) is None:
            raise self._fail(key, f"'{value}' did not match pattern '{pattern}'")
        if nonempty and not value:
            raise self._fail(key, "string is too short")
        return value

    def bool(self, key: str, default: bool = False) -> bool:
        value = self.data.get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise self._fail(key, f"expected boolean, got {type(value).__name__}")
        return value

    def int(self, key: str, default: Any = MISSING, *, lo: int | None = None, hi: int | None = None) -> Any:
        value = self.data.get(key)
        if value is None:
            if default is MISSING:
                raise self._fail(key, "missing required field")
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            raise self._fail(key, f"expected integer, got {type(value).__name__}")
        value = int(value)
        if lo is not None and value < lo:
            raise self._fail(key, f"{value} is too small; minimum is {lo}")
        if hi is not None and value > hi:
            raise self._fail(key, f"{value} is too large; maximum is {hi}")
        return value

    def list(self, key: str, default: Any = MISSING) -> list[Any]:
        value = self.data.get(key)
        if value is None:
            if default is MISSING:
                raise self._fail(key, "missing required field")
            return default
        if not isinstance(value, list):
            raise self._fail(key, f"expected list, got {type(value).__name__}")
        return value

    def obj(self, key: str, default: Any = MISSING) -> Args | None:
        value = self.data.get(key)
        if value is None:
            if default is MISSING:
                raise self._fail(key, "missing required field")
            return default
        if not isinstance(value, dict):
            raise self._fail(key, f"expected object, got {type(value).__name__}")
        return Args(value, f"{self.where}: {key}")

    def union(
        self, key: str, allowed: tuple[str, ...] | None = None, default: Any = MISSING
    ) -> tuple[str, Any]:
        """Decode a Stone union value ``{".tag": "x", ...}`` (or a bare string for void tags)."""
        value = self.data.get(key)
        if value is None:
            if default is MISSING:
                raise self._fail(key, "missing required field")
            return default, None
        name, payload = union_tag(value)
        if not isinstance(name, str):
            raise self._fail(key, "expected union object with a '.tag' field")
        if allowed is not None and name not in allowed:
            raise self._fail(key, f"unknown tag '{name}'")
        return name, payload

    def path(self, key: str = "path", pattern: str = PATH_R_OR_ID) -> str:
        return self.str(key, pattern=pattern)

    def ts(self, key: str) -> datetime | None:
        value = self.data.get(key)
        if value is None:
            return None
        try:
            return parse_ts(str(value))
        except ValueError:
            raise self._fail(key, f"'{value}' did not match pattern '%Y-%m-%dT%H:%M:%SZ'") from None


def union_tag(value: Any) -> tuple[str | None, Any]:
    """``("tag", payload)`` for a wire union; payload is the dict (minus ``.tag``) or the
    nested value stored under the tag key."""
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict) and ".tag" in value:
        name = value[".tag"]
        if not isinstance(name, str):
            return None, None
        rest = {k: v for k, v in value.items() if k != ".tag"}
        if name in rest and len(rest) == 1:
            return name, rest[name]
        return name, rest or None
    return None, None


# ---------------------------------------------------------------------------------------
# JSON in HTTP headers
# ---------------------------------------------------------------------------------------
def header_json(value: Any) -> str:
    """Dropbox-API-Result / Dropbox-API-Arg encoding: ASCII only, DEL escaped."""
    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return text.replace("\x7f", "\\u007f")


def decode_header_json(raw: str) -> Any:
    """Parse a ``Dropbox-API-Arg`` header. HTTP headers are latin-1 on the wire, but clients
    may send raw UTF-8 bytes; repair that before ``json.loads`` resolves ``\\uXXXX`` escapes."""
    if not raw.isascii():
        try:  # raw UTF-8 bytes were read as latin-1 by the HTTP layer
            raw = raw.encode("latin-1").decode("utf-8")
        except UnicodeError:
            pass
    return json.loads(raw)


# ---------------------------------------------------------------------------------------
# Ids and hashing
# ---------------------------------------------------------------------------------------
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def digest(*parts: object) -> bytes:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).digest()


def numeric_id(key: str, digits: int = 10) -> str:
    n = int.from_bytes(digest("dbx-num", key)[:8], "big") % (9 * 10 ** (digits - 1)) + 10 ** (digits - 1)
    return str(n)


def account_id_for(key: str) -> str:
    return "dbid:AA" + b64url(digest("dbx-acct", key))[:33]


def member_id_for(key: str) -> str:
    return "dbmid:AA" + b64url(digest("dbx-member", key))[:37]


def group_id_for(key: str) -> str:
    return "g:" + digest("dbx-group", key).hex()[:32]


def revision_for(key: str) -> str:
    return digest("dbx-rev", key).hex()[:16]


def content_hash(data: bytes) -> str:
    """Dropbox's content hash: sha256 over the concatenated sha256 of every 4 MiB block."""
    outer = hashlib.sha256()
    for start in range(0, len(data), BLOCK):
        outer.update(hashlib.sha256(data[start : start + BLOCK]).digest())
    return outer.hexdigest()


class Pager:
    """Immutable opaque cursors, bound to the caller and restored with store snapshots.

    Client-supplied base64 JSON must never become trusted pagination or ACL state.
    Longpoll intentionally uses the cursor as a capability without authentication.
    """

    @staticmethod
    def encode(state: dict[str, Any], call: Any) -> str:
        cursor = "AAH" + secrets.token_urlsafe(32)
        call.ctx.store.collection("_dropbox_cursors").put(
            {"id": cursor, "owner": Pager.owner(call), "state": copy.deepcopy(state)}
        )
        return cursor

    @staticmethod
    def decode(cursor: str, call: Any) -> dict[str, Any] | None:
        record = call.ctx.store.collection("_dropbox_cursors").get(cursor)
        if record is None:
            return None
        if call.route != "files/list_folder/longpoll" and record["owner"] != Pager.owner(call):
            return None
        return copy.deepcopy(record["state"])

    @staticmethod
    def owner(call: Any) -> list[Any]:
        actor = call.actor
        return [
            actor.id,
            actor.admin,
            actor.team_scope,
            actor.grant.client_id if actor.grant else actor.app_id,
        ]


# ---------------------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------------------
_BAD_CHARS = re.compile(r'[\\<>:"|?*\x00-\x1f]')
_RESERVED = {".dropbox", ".dropbox.attr", ".dropbox.cache", "desktop.ini", "thumbs.db", ".ds_store", "icon\r"}


def disallowed_name(name: str) -> bool:
    if not name or len(name) > 255:
        return True
    if name in (".", "..") or name.endswith((" ", ".")):
        return True
    if name.lower() in _RESERVED:
        return True
    return bool(_BAD_CHARS.search(name))


def split_ext(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]


def autorename(name: str, taken: set[str]) -> str:
    """``report.docx`` -> ``report (1).docx`` -> ``report (2).docx`` (case-insensitive)."""
    stem, ext = split_ext(name)
    i = 1
    while f"{stem} ({i}){ext}".lower() in taken:
        i += 1
    return f"{stem} ({i}){ext}"
