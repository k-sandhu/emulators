"""Convert JSON request values into stored field values, raising Salesforce's DML errors."""

from __future__ import annotations

import base64
import binascii
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from emulators.providers.salesforce.common import SfError, fmt_dt, parse_dt
from emulators.providers.salesforce.schema import Field

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?Z?$")
_SOURCE = "\n at [Source: (org.apache.catalina.connector.CoyoteInputStream); line: 1, column: 2]"


def _json_error(f: Field, value: Any, wanted: str) -> SfError:
    kind = (
        "VALUE_STRING"
        if isinstance(value, str)
        else "VALUE_NUMBER_INT"
        if isinstance(value, int)
        else "VALUE_NUMBER_FLOAT"
    )
    if isinstance(value, bool):
        kind = "VALUE_TRUE" if value else "VALUE_FALSE"
    return SfError(
        400,
        "JSON_PARSER_ERROR",
        f"Cannot deserialize instance of {wanted} from {kind} value {value} or request may be missing a "
        f"required field{_SOURCE}",
    )


def _range_error(f: Field, value: Any) -> SfError:
    return SfError(
        400,
        "NUMBER_OUTSIDE_VALID_RANGE",
        f"{f.label}: value outside of valid range on numeric field: {value}",
        [f.name],
    )


# Geocodes and probabilities have hard ranges regardless of the declared precision.
_BOUNDS: dict[str, tuple[float, float]] = {"Probability": (0.0, 100.0)}
_SUFFIX_BOUNDS: tuple[tuple[str, tuple[float, float]], ...] = (
    ("Latitude", (-90.0, 90.0)),
    ("Longitude", (-180.0, 180.0)),
)


def coerce(f: Field, value: Any) -> Any:
    """Validate/convert ``value`` for field ``f`` (``None`` clears the field)."""
    if value is None:
        return None
    t = f.type
    if value == "" and t in (
        "string",
        "textarea",
        "picklist",
        "multipicklist",
        "email",
        "phone",
        "url",
        "combobox",
    ):
        return None  # Salesforce stores an empty string as a cleared field
    if t == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise _json_error(f, value, "boolean")
    if t == "int":
        if isinstance(value, bool):
            raise _json_error(f, value, "int")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        elif isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            value = int(value.strip())
        if not isinstance(value, int):
            raise _json_error(f, value, "int")
        if abs(value) >= 10**9:
            raise _range_error(f, value)
        return value
    if t in ("double", "currency", "percent"):
        if isinstance(value, bool):
            raise _json_error(f, value, "double")
        try:
            number = Decimal(str(value).strip())
        except Exception as exc:
            raise _json_error(f, value, "double") from exc
        if not number.is_finite():
            raise _json_error(f, value, "double")
        if f.precision:
            digits = f.precision - f.scale
            if abs(number) >= Decimal(10) ** digits:
                raise _range_error(f, value)
        quantum = Decimal(1).scaleb(-f.scale)
        result = float(number.quantize(quantum, rounding=ROUND_HALF_UP))
        bound = _BOUNDS.get(f.name) or next((b for s, b in _SUFFIX_BOUNDS if f.name.endswith(s)), None)
        if bound is not None and not bound[0] <= result <= bound[1]:
            raise _range_error(f, value)
        return result
    if t == "date":
        if isinstance(value, str):
            dt = parse_dt(value) if len(value) >= 10 else None
            if dt is not None:
                return dt.date().isoformat()
        raise _json_error(f, value, "date")
    if t == "datetime":
        if isinstance(value, str):
            dt = parse_dt(value)
            if dt is not None:
                return fmt_dt(dt)
        raise _json_error(f, value, "dateTime")
    if t == "time":
        if isinstance(value, str) and TIME_RE.match(value):
            return value
        raise _json_error(f, value, "time")
    if t in ("id", "reference"):
        if isinstance(value, str):
            return value
        raise _json_error(f, value, "id")
    if t == "base64":
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str):
            try:
                return base64.b64decode(value, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise _json_error(f, "<binary>", "base64") from exc
        raise _json_error(f, value, "base64")
    if t == "address":
        raise SfError(
            400, "INVALID_FIELD", f"Cannot set compound field {f.name}; set its components instead."
        )
    # string-like types
    if isinstance(value, (bool, dict, list)):
        raise _json_error(f, value, "string")
    text = str(value) if not isinstance(value, str) else value
    if t == "multipicklist":
        return _multipicklist(f, text)
    if t == "picklist":
        text = _picklist(f, text)
    if f.length and len(text) > f.length and t != "textarea":
        raise _too_long(f, text)
    if t == "textarea" and f.length and len(text) > f.length:
        raise _too_long(f, text)
    if t == "email" and text and not EMAIL_RE.match(text):
        raise SfError(400, "INVALID_EMAIL_ADDRESS", f"{f.label}: invalid email address: {text}", [f.name])
    return text


def _too_long(f: Field, text: str) -> SfError:
    return SfError(
        400,
        "STRING_TOO_LONG",
        f"{f.label}: data value too large: {text[:100]}{'...' if len(text) > 100 else ''} (max length={f.length})",
        [f.name],
    )


def _picklist(f: Field, text: str) -> str:
    if text == "":
        return text
    for candidate in f.picklist_values():
        if candidate.casefold() == text.casefold():
            return candidate
    if f.restricted:
        raise SfError(
            400,
            "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST",
            f"{f.label}: bad value for restricted picklist field: {text}",
            [f.name],
        )
    return text


def _multipicklist(f: Field, text: str) -> str:
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if f.restricted:
        allowed = {v.casefold(): v for v in f.picklist_values()}
        for p in parts:
            if p.casefold() not in allowed:
                raise SfError(
                    400,
                    "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST",
                    f"{f.label}: bad value for restricted picklist field: {p}",
                    [f.name],
                )
        parts = [allowed[p.casefold()] for p in parts]
    return ";".join(parts)
