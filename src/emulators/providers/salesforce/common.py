"""Shared Salesforce helpers: ids, wire timestamps, the ``[{message, errorCode}]`` error envelope.

Salesforce errors are always a JSON *array* of ``{"message", "errorCode"[, "fields"]}``
objects (REST) or a SOAP fault (SOAP login); OAuth token errors use the RFC 6749
``{"error", "error_description"}`` shape. :class:`SfError` carries the code/message/fields so
batch endpoints (composite, collections, bulk) can re-render them per record.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

from emulators.core.errors import EmulatorError

DEFAULT_VERSION = "62.0"
MIN_VERSION = 31
MAX_VERSION = 66
PASSWORD = "Halcyon2026!"
SECURITY_TOKEN = "EMULATORTOKEN"
INSTANCE_HOST = "halcyonadvisory.my.salesforce.com"
LOGIN_HOST = "login.salesforce.com"
DAILY_API_LIMIT = 15000
STANDARD_PRICEBOOK_NAME = "Standard Price Book"
MASTER_RECORD_TYPE_ID = "012000000000000AAA"

ID_RE = re.compile(r"^[A-Za-z0-9]{15}$|^[A-Za-z0-9]{18}$")
_CHECKSUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


# -- errors ----------------------------------------------------------------------------


class SfError(EmulatorError):
    """A Salesforce REST error: HTTP ``status`` plus one ``{message, errorCode}`` entry."""

    def __init__(self, status: int, code: str, message: str, fields: list[str] | None = None) -> None:
        entry: dict[str, Any] = {"message": message, "errorCode": code}
        if fields is not None:
            entry["fields"] = fields
        super().__init__(status, [entry])
        self.code = code
        self.message = message
        self.fields = fields

    def entry(self) -> dict[str, Any]:
        return self.body[0]

    def dml_entry(self) -> dict[str, Any]:
        """The ``{statusCode, message, fields}`` shape used inside ``errors`` of save results."""
        return {"statusCode": self.code, "message": self.message, "fields": self.fields or []}


def not_found(message: str = "The requested resource does not exist") -> SfError:
    return SfError(404, "NOT_FOUND", message)


def invalid_session() -> SfError:
    return SfError(401, "INVALID_SESSION_ID", "Session expired or invalid")


def insufficient_access(message: str = "insufficient access rights on object id") -> SfError:
    return SfError(403, "INSUFFICIENT_ACCESS_OR_READONLY", message)


def bad_request(code: str, message: str, fields: list[str] | None = None) -> SfError:
    return SfError(400, code, message, fields)


# -- ids -------------------------------------------------------------------------------


def to_18(id15: str) -> str:
    """Append the 3-character case-safe checksum to a 15-character id."""
    suffix = ""
    for chunk in range(3):
        bits = 0
        for i, ch in enumerate(id15[chunk * 5 : chunk * 5 + 5]):
            if "A" <= ch <= "Z":
                bits |= 1 << i
        suffix += _CHECKSUM[bits]
    return id15 + suffix


def norm_id(text: Any) -> str | None:
    """Canonical 18-char form of a 15- or 18-char id; ``None`` when it is not a valid id."""
    if not isinstance(text, str) or not ID_RE.match(text):
        return None
    if len(text) == 15:
        return to_18(text)
    suffix = text[15:].upper()
    if any(ch not in _CHECKSUM for ch in suffix):
        return None
    body = []
    for chunk, checksum in enumerate(suffix):
        bits = _CHECKSUM.index(checksum)
        for i, ch in enumerate(text[chunk * 5 : chunk * 5 + 5]):
            upper = bool(bits & (1 << i))
            if upper and ch.isdigit():
                return None
            body.append(ch.upper() if upper else ch.lower())
    return "".join(body) + suffix


def id_prefix(text: str) -> str:
    return text[:3]


# -- timestamps ------------------------------------------------------------------------


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def fmt_dt(dt: datetime) -> str:
    """``2026-10-15T14:22:05.000+0000`` — Salesforce's datetime wire format."""
    dt = utc(dt)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}+0000"


def fmt_date(d: date | datetime) -> str:
    return d.strftime("%Y-%m-%d") if isinstance(d, datetime) else d.isoformat()


_DT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_dt(text: str) -> datetime | None:
    """Parse the ISO-8601 flavours Salesforce accepts into aware UTC; ``None`` if malformed."""
    match = _DT_RE.match(text.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, frac, zone = match.groups()
    try:
        dt = datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            int((frac or "0").ljust(6, "0")[:6]),
            tzinfo=UTC,
        )
    except ValueError:
        return None
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        from datetime import timedelta

        hours, minutes = int(digits[:2]), int(digits[2:])
        if hours > 23 or minutes > 59:
            return None
        try:
            dt -= sign * timedelta(hours=hours, minutes=minutes)
        except OverflowError:
            return None
    return dt


def parse_date_only(text: str) -> date | None:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text.strip())
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


# -- versions --------------------------------------------------------------------------


def parse_version(segment: str) -> float | None:
    """``v62.0`` -> 62.0 when inside the supported range."""
    match = re.fullmatch(r"v(\d{2})\.0", segment)
    if not match:
        return None
    number = int(match.group(1))
    return float(number) if MIN_VERSION <= number <= MAX_VERSION else None


def version_label(number: int) -> str:
    """Release name for an API version: v62.0 is "Winter '25"; v66.0 is "Spring '26"."""
    index = number - 29  # v29.0 = Winter '14
    return f"{('Winter', 'Spring', 'Summer')[index % 3]} '{14 + index // 3}"


def versions_list() -> list[dict[str, str]]:
    return [
        {"label": version_label(n), "url": f"/services/data/v{n}.0", "version": f"{n}.0"}
        for n in range(MIN_VERSION, MAX_VERSION + 1)
    ]


def camel_to_label(name: str) -> str:
    """``BillingStreet`` -> ``Billing Street``; ``Start_Date__c`` -> ``Start Date``."""
    base = name[:-3] if name.endswith("__c") else name
    base = base.replace("_", " ")
    base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", base)
    base = re.sub(r"\bId$", "ID", base)
    return base.strip()


def get_ci(mapping: dict[str, Any], key: str) -> Any:
    """Case-insensitive dict lookup (Salesforce field names are case-insensitive)."""
    if key in mapping:
        return mapping[key]
    lowered = key.lower()
    for k, v in mapping.items():
        if k.lower() == lowered:
            return v
    return None
