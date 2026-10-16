"""Validate and evaluate Notion database filters without evaluating user code."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from emulators.core.clock import parse_instant
from emulators.providers.notion.common import invalid, property_by_key, text_of, uid

OPERATORS = {
    "title": {
        "equals",
        "does_not_equal",
        "contains",
        "does_not_contain",
        "starts_with",
        "ends_with",
        "is_empty",
        "is_not_empty",
    },
    "rich_text": {
        "equals",
        "does_not_equal",
        "contains",
        "does_not_contain",
        "starts_with",
        "ends_with",
        "is_empty",
        "is_not_empty",
    },
    "url": {
        "equals",
        "does_not_equal",
        "contains",
        "does_not_contain",
        "starts_with",
        "ends_with",
        "is_empty",
        "is_not_empty",
    },
    "email": {
        "equals",
        "does_not_equal",
        "contains",
        "does_not_contain",
        "starts_with",
        "ends_with",
        "is_empty",
        "is_not_empty",
    },
    "phone_number": {
        "equals",
        "does_not_equal",
        "contains",
        "does_not_contain",
        "starts_with",
        "ends_with",
        "is_empty",
        "is_not_empty",
    },
    "number": {
        "equals",
        "does_not_equal",
        "greater_than",
        "less_than",
        "greater_than_or_equal_to",
        "less_than_or_equal_to",
        "is_empty",
        "is_not_empty",
    },
    "checkbox": {"equals", "does_not_equal"},
    "select": {"equals", "does_not_equal", "is_empty", "is_not_empty"},
    "status": {"equals", "does_not_equal", "is_empty", "is_not_empty"},
    "multi_select": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
    "people": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
    "relation": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
    "date": {
        "equals",
        "before",
        "after",
        "on_or_before",
        "on_or_after",
        "is_empty",
        "is_not_empty",
        "past_week",
        "past_month",
        "past_year",
        "next_week",
        "next_month",
        "next_year",
        "this_week",
    },
    "files": {"is_empty", "is_not_empty"},
    "created_time": {
        "equals",
        "before",
        "after",
        "on_or_before",
        "on_or_after",
        "is_empty",
        "is_not_empty",
        "past_week",
        "past_month",
        "past_year",
        "next_week",
        "next_month",
        "next_year",
        "this_week",
    },
    "last_edited_time": {
        "equals",
        "before",
        "after",
        "on_or_before",
        "on_or_after",
        "is_empty",
        "is_not_empty",
        "past_week",
        "past_month",
        "past_year",
        "next_week",
        "next_month",
        "next_year",
        "this_week",
    },
}


def compile_filter(raw: Any, specs: dict[str, Any], depth: int = 0) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        invalid("filter must be a non-empty object.")
    compounds = [k for k in ("and", "or") if k in raw]
    if compounds:
        key = compounds[0]
        if len(raw) != 1 or depth >= 2 or not isinstance(raw[key], list) or not raw[key]:
            invalid("Compound filters support at most two levels and require a non-empty array.")
        return (key, [compile_filter(v, specs, depth + 1) for v in raw[key]])
    if "timestamp" in raw:
        name = raw["timestamp"]
        if name not in ("created_time", "last_edited_time"):
            invalid("timestamp must be created_time or last_edited_time.")
        kind = name
    elif "property" in raw:
        name, spec = property_by_key(specs, raw["property"])
        kind = spec["type"]
    else:
        invalid("A filter requires a property or timestamp.")
    condition = raw.get(kind)
    if kind == "formula":
        if not isinstance(condition, dict) or len(condition) != 1:
            invalid("formula filter requires string, number, checkbox, or date.")
        subtype, condition = next(iter(condition.items()))
        if subtype not in ("string", "number", "checkbox", "date"):
            invalid("Unsupported formula filter type.")
        comparison = "rich_text" if subtype == "string" else subtype
    else:
        comparison = kind
    if comparison not in OPERATORS or not isinstance(condition, dict) or len(condition) != 1:
        invalid(f"Invalid {kind} filter condition.")
    operator, target = next(iter(condition.items()))
    if operator not in OPERATORS[comparison]:
        invalid(f"Unsupported {comparison} filter operator: {operator}.")
    if operator in ("is_empty", "is_not_empty"):
        if not isinstance(target, bool):
            invalid(f"{operator} must be boolean.")
    elif operator in (
        "past_week",
        "past_month",
        "past_year",
        "next_week",
        "next_month",
        "next_year",
        "this_week",
    ):
        if target != {}:
            invalid(f"{operator} requires an empty object.")
    elif comparison in ("date", "created_time", "last_edited_time"):
        try:
            parse_instant(target)
        except (ValueError, TypeError, AttributeError):
            invalid("Date filter requires an ISO 8601 date.")
    elif comparison == "number":
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            invalid("Number filter requires a number.")
    elif comparison == "checkbox":
        if not isinstance(target, bool):
            invalid("Checkbox filter requires a boolean.")
    elif not isinstance(target, str):
        invalid("Filter comparison requires a string.")
    if comparison in ("people", "relation") and operator in ("contains", "does_not_contain"):
        target = uid(target)
    return ("leaf", name, kind, comparison, operator, target)


def evaluate(node: Any, row: dict[str, Any], now: datetime) -> bool:
    if node is None:
        return True
    if node[0] == "and":
        return all(evaluate(n, row, now) for n in node[1])
    if node[0] == "or":
        return any(evaluate(n, row, now) for n in node[1])
    _, name, kind, comparison, operator, target = node
    if name in ("created_time", "last_edited_time") and name not in row["properties"]:
        value = row[name]
    else:
        value = row["properties"].get(name, {}).get(kind)
    if kind == "formula":
        value = (value or {}).get((value or {}).get("type"))
    if kind in ("title", "rich_text"):
        value = text_of(value or [])
    elif kind in ("select", "status"):
        value = (value or {}).get("name")
    elif kind in ("people", "relation"):
        value = [v["id"] for v in (value or [])]
    elif kind == "multi_select":
        value = [v["name"] for v in (value or [])]
    if operator in ("is_empty", "is_not_empty"):
        empty = value is None or value == "" or value == []
        return (empty if operator == "is_empty" else not empty) == target
    if comparison in ("date", "created_time", "last_edited_time"):
        value = value.get("start") if isinstance(value, dict) else value
        if not value:
            return False
        at = parse_instant(value)
        if operator.startswith(("past_", "next_")):
            # Notion's relative month/year windows are calendar intervals.
            unit = operator.split("_")[1]
            sign = -1 if operator.startswith("past") else 1
            if unit == "week":
                edge = now + timedelta(days=7 * sign)
            else:
                import calendar

                month_index = now.year * 12 + now.month - 1 + sign * (12 if unit == "year" else 1)
                year, month = divmod(month_index, 12)
                month += 1
                edge = now.replace(
                    year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1])
                )
            return min(now, edge) <= at <= max(now, edge)
        if operator == "this_week":
            beginning = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return beginning <= at < beginning + timedelta(days=7)
        expected = parse_instant(target)
        if len(target) == 10:
            at, expected = at.date(), expected.date()
        return {
            "equals": at == expected,
            "before": at < expected,
            "after": at > expected,
            "on_or_before": at <= expected,
            "on_or_after": at >= expected,
        }[operator]
    if operator == "equals":
        return value == target
    if operator == "does_not_equal":
        return value != target
    if operator in ("contains", "does_not_contain"):
        includes = (
            target.casefold() in value.casefold() if isinstance(value, str) else target in (value or [])
        )
        return includes if operator == "contains" else not includes
    if operator == "starts_with":
        return isinstance(value, str) and value.casefold().startswith(target.casefold())
    if operator == "ends_with":
        return isinstance(value, str) and value.casefold().endswith(target.casefold())
    if value is None:
        return False
    return {
        "greater_than": value > target,
        "less_than": value < target,
        "greater_than_or_equal_to": value >= target,
        "less_than_or_equal_to": value <= target,
    }[operator]


def sorted_rows(rows: list[dict[str, Any]], sorts: Any, specs: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(sorts, list):
        invalid("sorts must be an array.")
    compiled = []
    for sort in sorts:
        if not isinstance(sort, dict) or sort.get("direction", "ascending") not in (
            "ascending",
            "descending",
        ):
            invalid("Invalid sort direction.")
        if "timestamp" in sort and sort["timestamp"] in ("created_time", "last_edited_time"):
            compiled.append((sort["timestamp"], None, sort.get("direction") == "descending"))
        elif "property" in sort:
            name, spec = property_by_key(specs, sort["property"])
            compiled.append((name, spec["type"], sort.get("direction") == "descending"))
        else:
            invalid("Sort must specify property or timestamp.")
    for name, kind, reverse in reversed(compiled):

        def key(row: dict[str, Any], name: str = name, kind: str | None = kind) -> Any:
            value = row[name] if kind is None else row["properties"].get(name, {}).get(kind)
            if kind in ("rich_text", "title"):
                value = text_of(value or []).casefold()
            elif kind in ("select", "status"):
                value = (value or {}).get("name")
            elif kind == "date":
                value = (value or {}).get("start")
            elif kind == "formula":
                value = (value or {}).get((value or {}).get("type"))
            if isinstance(value, (list, dict)):
                value = str(value)
            return (value is None, value if value is not None else "")

        rows = sorted(rows, key=key, reverse=reverse)
    return rows
