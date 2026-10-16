"""Validate writes before mutating store records, using Zoom's published constraints."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from fastapi import Request

from emulators.core.errors import EmulatorError
from emulators.providers.zoom.common import invalid_field
from emulators.providers.zoom.request_meetings import SCHEMAS as MEETINGS
from emulators.providers.zoom.request_settings import SCHEMA as SETTINGS
from emulators.providers.zoom.request_users import SCHEMAS as USERS
from emulators.providers.zoom.timeutil import parse_instant, valid_timezone

SCHEMAS = {**MEETINGS, **USERS}
SCHEMAS[("PATCH", "/v2/users/{user_id}/settings")] = SETTINGS
JSON_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate(value: Any, schema: dict[str, Any], field: str = "body") -> None:
    if "anyOf" in schema:
        for part in schema["anyOf"]:
            try:
                validate(value, part, field)
                break
            except EmulatorError:
                continue
        else:
            raise invalid_field(field)
    for part in schema.get("allOf", []):
        validate(value, part, field)
    kind = schema.get("type")
    if kind in JSON_TYPES and type(value) is not JSON_TYPES[kind]:
        raise invalid_field(field)
    if kind == "number" and (type(value) not in (int, float)):
        raise invalid_field(field)
    if "enum" in schema and value not in schema["enum"]:
        raise invalid_field(field)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise invalid_field(key if field == "body" else f"{field}.{key}", "Required field.")
        for key, item in value.items():
            if key in schema.get("properties", {}):
                validate(item, schema["properties"][key], key if field == "body" else f"{field}.{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise invalid_field(field)
        for index, item in enumerate(value):
            validate(item, schema.get("items", {}), f"{field}[{index}]")
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise invalid_field(field) from None
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise invalid_field(field)
        if schema.get("format") == "email" and value and not EMAIL.fullmatch(value):
            raise invalid_field(field)
        if schema.get("format") == "date-time":
            try:
                if "T" not in value:
                    raise ValueError
                parse_instant(value)
            except (ValueError, OverflowError):
                raise invalid_field(field) from None
    elif type(value) in (int, float):
        if type(value) is float and not math.isfinite(value):
            raise invalid_field(field)
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise invalid_field(field)


def request_body(request: Request, data: dict[str, Any]) -> None:
    route = request.scope.get("route")
    schema = SCHEMAS.get((request.method, getattr(route, "path", "")))
    if schema:
        validate(data, schema)
    if "timezone" in data and not valid_timezone(data["timezone"]):
        raise invalid_field("timezone")
    if "recurrence" in data:
        recurrence(data["recurrence"])


def recurrence(data: dict[str, Any]) -> None:
    kind = data["type"]
    interval = data.get("repeat_interval", 1)
    if not 1 <= interval <= {1: 99, 2: 50, 3: 10}[kind]:
        raise invalid_field("recurrence.repeat_interval")
    if not 0 <= data.get("end_times", 1) <= 60:
        raise invalid_field("recurrence.end_times")
    if "end_times" in data and "end_date_time" in data:
        raise invalid_field("recurrence.end_date_time", "Cannot be used with end_times.")
    if (
        "end_date_time" in data
        and datetime.fromisoformat(data["end_date_time"].replace("Z", "+00:00")).tzinfo is None
    ):
        raise invalid_field("recurrence.end_date_time")
    if kind == 2 and not re.fullmatch(r"[1-7](?:,[1-7])*", data.get("weekly_days", "")):
        raise invalid_field("recurrence.weekly_days")
    if "monthly_day" in data and not 1 <= data["monthly_day"] <= 31:
        raise invalid_field("recurrence.monthly_day")
    if ("monthly_week" in data) != ("monthly_week_day" in data):
        raise invalid_field("recurrence.monthly_week_day")
