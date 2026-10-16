"""Small, deliberately strict parser for Meet's documented list filters."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from emulators.core.clock import parse_instant
from emulators.providers.google.common import google_error

TOKEN = re.compile(r'\s*(>=|<=|!=|=|>|<|\(|\)|"(?:[^"\\]|\\.)*"|[A-Za-z_][\w.]*|\S+)')


def compile_filter(expression: str, fields: dict[str, str]) -> Callable[[dict[str, Any]], bool]:
    """Validate before evaluating, including when the candidate list is empty."""
    tokens = TOKEN.findall(expression)
    index = 0

    def take() -> str:
        nonlocal index
        if index >= len(tokens):
            raise ValueError("Incomplete filter")
        value = tokens[index]
        index += 1
        return value

    def atom() -> Callable[[dict[str, Any]], bool]:
        nonlocal index
        if index < len(tokens) and tokens[index] == "(":
            index += 1
            result = either()
            if take() != ")":
                raise ValueError("Unbalanced filter")
            return result
        field = take()
        if field not in fields:
            raise ValueError(f"Unsupported filter field: {field}")
        operator = take()
        if operator.upper() == "IS":
            if take().upper() != "NULL":
                raise ValueError("Expected NULL")
            return lambda row: row.get(fields[field]) is None
        if operator not in ("=", "!=", ">=", "<=", ">", "<"):
            raise ValueError("Unsupported comparison")
        value = take()
        if not (value.startswith('"') and value.endswith('"')):
            raise ValueError("Expected a quoted value")
        value = value[1:-1]
        temporal = field.endswith("time")
        wanted = parse_instant(value) if temporal else value

        def compare(row: dict[str, Any]) -> bool:
            actual = row.get(fields[field])
            if actual is None:
                return False
            actual = parse_instant(actual) if temporal else actual
            return {
                "=": lambda: actual == wanted,
                "!=": lambda: actual != wanted,
                ">=": lambda: actual >= wanted,
                "<=": lambda: actual <= wanted,
                ">": lambda: actual > wanted,
                "<": lambda: actual < wanted,
            }[operator]()

        return compare

    def both() -> Callable[[dict[str, Any]], bool]:
        nonlocal index
        parts = [atom()]
        while index < len(tokens) and tokens[index].upper() == "AND":
            index += 1
            parts.append(atom())
        return lambda row: all(part(row) for part in parts)

    def either() -> Callable[[dict[str, Any]], bool]:
        nonlocal index
        parts = [both()]
        while index < len(tokens) and tokens[index].upper() == "OR":
            index += 1
            parts.append(both())
        return lambda row: any(part(row) for part in parts)

    if not tokens:
        return lambda _: True
    try:
        result = either()
        if index != len(tokens):
            raise ValueError("Unexpected filter token")
        return result
    except (ValueError, TypeError) as exc:
        raise google_error(400, f"Invalid filter: {exc}", legacy=False) from exc
