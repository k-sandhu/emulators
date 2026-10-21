"""Validate and evaluate HubSpot's OR-of-AND CRM search language."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from emulators.core.clock import parse_instant
from emulators.core.provider import ProviderContext

from .common import fail

OPERATORS = {
    "EQ",
    "NEQ",
    "LT",
    "LTE",
    "GT",
    "GTE",
    "BETWEEN",
    "IN",
    "NOT_IN",
    "HAS_PROPERTY",
    "NOT_HAS_PROPERTY",
    "CONTAINS_TOKEN",
    "NOT_CONTAINS_TOKEN",
}
QUERY_FIELDS = {
    "contacts": ["firstname", "lastname", "email", "phone", "company"],
    "companies": ["name", "domain", "phone", "website"],
    "deals": ["dealname"],
    "tickets": ["subject", "content"],
}


def comparable(ctx: ProviderContext, kind: str, name: str, value: Any) -> Any:
    definition = ctx.store[f"properties:{kind}"].get(name)
    if definition is None:
        fail(400, f"Property {name} does not exist.")
    text = str(value)
    if definition["type"] == "number":
        try:
            number = Decimal(text)
            if not number.is_finite():
                raise InvalidOperation
            return number
        except InvalidOperation:
            fail(400, f"Invalid numeric value for {name}.")
    if definition["type"] in ("date", "datetime"):
        try:
            return (
                Decimal(text)
                if re.fullmatch(r"-?\d+", text)
                else Decimal(str(parse_instant(text).timestamp() * 1000))
            )
        except (ValueError, InvalidOperation):
            fail(400, f"Invalid date value for {name}.")
    return text if definition["type"] == "enumeration" else text.casefold()


def validate(ctx: ProviderContext, kind: str, payload: dict[str, Any]) -> None:
    groups = payload.get("filterGroups", [])
    if not isinstance(groups, list) or len(groups) > 5:
        fail(400, "At most five filterGroups are supported.")
    count = 0
    for group in groups:
        if (
            not isinstance(group, dict)
            or not isinstance(group.get("filters"), list)
            or not group["filters"]
            or len(group["filters"]) > 6
        ):
            fail(400, "Each filterGroup requires between one and six filters.")
        for item in group["filters"]:
            if not isinstance(item, dict):
                fail(400, "Invalid search filter.")
            name, operator = item.get("propertyName"), item.get("operator")
            if (
                not isinstance(operator, str)
                or operator not in OPERATORS
                or not isinstance(name, str)
                or not ctx.store[f"properties:{kind}"].get(name)
            ):
                fail(400, "Invalid property or search operator.")
            if operator in ("IN", "NOT_IN"):
                if not isinstance(item.get("values"), list) or any(
                    isinstance(v, bool) or not isinstance(v, (str, int, float)) for v in item["values"]
                ):
                    fail(400, "IN and NOT_IN require an array of string values.")
                for value in item["values"]:
                    comparable(ctx, kind, name, value)
            elif operator not in ("HAS_PROPERTY", "NOT_HAS_PROPERTY"):
                if "value" not in item or (operator == "BETWEEN" and "highValue" not in item):
                    fail(400, "Filter value or highValue is missing.")
                if any(
                    isinstance(item[key], bool) or not isinstance(item[key], (str, int, float))
                    for key in ("value", "highValue")
                    if key in item
                ):
                    fail(400, "Filter values must be strings.")
                comparable(ctx, kind, name, item["value"])
                if operator == "BETWEEN":
                    comparable(ctx, kind, name, item["highValue"])
            count += 1
    if count > 18:
        fail(400, "At most 18 search filters are allowed.")
    sorts = payload.get("sorts", [])
    if not isinstance(sorts, list) or len(sorts) > 1:
        fail(400, "Only one sorting rule is allowed.")
    for sort in sorts:
        if isinstance(sort, str):
            name, direction = sort.removeprefix("-"), "DESCENDING" if sort.startswith("-") else "ASCENDING"
        elif isinstance(sort, dict):
            name, direction = sort.get("propertyName"), sort.get("direction", "ASCENDING")
        else:
            fail(400, "Invalid sort rule.")
        if (
            not isinstance(name, str)
            or not ctx.store[f"properties:{kind}"].get(name)
            or direction not in ("ASCENDING", "DESCENDING")
        ):
            fail(400, "Invalid sort property or direction.")
    if not isinstance(payload.get("query", ""), str) or len(payload.get("query", "")) > 3000:
        fail(400, "Invalid search query.")


def matches_filter(ctx: ProviderContext, kind: str, props: dict[str, str], item: dict[str, Any]) -> bool:
    name, op = item["propertyName"], item["operator"]
    raw = props.get(name)
    present = raw not in (None, "")
    if op == "HAS_PROPERTY":
        return present
    if op == "NOT_HAS_PROPERTY":
        return not present
    if not present:
        return False
    value = comparable(ctx, kind, name, raw)
    if op in ("IN", "NOT_IN"):
        found = value in [comparable(ctx, kind, name, v) for v in item["values"]]
        return found if op == "IN" else not found
    wanted = comparable(ctx, kind, name, item["value"])
    if op in ("CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN"):
        pattern = "^" + re.escape(str(wanted)).replace(r"\*", ".*") + "$"
        definition = ctx.store[f"properties:{kind}"].get(name)
        flags = 0 if definition["type"] == "enumeration" else re.IGNORECASE
        found = any(re.fullmatch(pattern, token, flags) for token in re.findall(r"[\w@.\-]+", str(raw)))
        return found if op == "CONTAINS_TOKEN" else not found
    if op == "BETWEEN":
        return wanted <= value <= comparable(ctx, kind, name, item["highValue"])
    return {
        "EQ": lambda: value == wanted,
        "NEQ": lambda: value != wanted,
        "LT": lambda: value < wanted,
        "LTE": lambda: value <= wanted,
        "GT": lambda: value > wanted,
        "GTE": lambda: value >= wanted,
    }[op]()


def apply(
    ctx: ProviderContext, kind: str, records: list[dict[str, Any]], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    validate(ctx, kind, payload)
    groups = payload.get("filterGroups", [])
    query = payload.get("query", "").casefold()
    fields = QUERY_FIELDS.get(
        kind,
        list({p for r in records for p in r["properties"] if p not in ("hubspot_owner_id", "hs_object_id")}),
    )
    result = [
        r
        for r in records
        if (
            not groups
            or any(all(matches_filter(ctx, kind, r["properties"], f) for f in g["filters"]) for g in groups)
        )
        and (not query or any(query in r["properties"].get(p, "").casefold() for p in fields))
    ]
    sorts = payload.get("sorts", [])
    if sorts:
        sort = sorts[0]
        name = sort.removeprefix("-") if isinstance(sort, str) else sort["propertyName"]
        descending = sort.startswith("-") if isinstance(sort, str) else sort.get("direction") == "DESCENDING"
        present = [r for r in result if r["properties"].get(name) not in (None, "")]
        missing = [r for r in result if r["properties"].get(name) in (None, "")]
        result = (
            sorted(
                present, key=lambda r: comparable(ctx, kind, name, r["properties"][name]), reverse=descending
            )
            + missing
        )
    return result
