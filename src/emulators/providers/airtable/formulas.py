"""Parse a bounded Airtable formula subset without executing request text."""

from __future__ import annotations

import json
import math
import operator
import re
from typing import Any

from .common import invalid

TOKEN = re.compile(
    r"""\s*(\{[^{}]+\}|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|!=|>=|<=|[=><&+*/(),!-]|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z_0-9]*)"""
)
PRECEDENCE = {"=": 1, "!=": 1, ">": 1, "<": 1, ">=": 1, "<=": 1, "&": 2, "+": 3, "-": 3, "*": 4, "/": 4}
ARITY = {
    "AND": (1, 32),
    "OR": (1, 32),
    "NOT": (1, 1),
    "IF": (3, 3),
    "FIND": (2, 3),
    "SEARCH": (2, 3),
    "LOWER": (1, 1),
    "UPPER": (1, 1),
    "LEN": (1, 1),
    "RECORD_ID": (0, 0),
    "TRUE": (0, 0),
    "FALSE": (0, 0),
    "BLANK": (0, 0),
    "CONCATENATE": (1, 32),
    "IS_AFTER": (2, 2),
    "IS_BEFORE": (2, 2),
}


def error() -> Any:
    invalid(
        "The formula for filtering records is invalid or uses an unsupported expression.",
        "INVALID_FILTER_BY_FORMULA",
    )


class Formula:
    def __init__(self, source: str, fields: dict[str, str]) -> None:
        if not isinstance(source, str) or not source.strip() or len(source) > 16000:
            error()
        self.tokens: list[str] = []
        self.index = 0
        pos = 0
        while pos < len(source.rstrip()):
            match = TOKEN.match(source, pos)
            if not match or len(self.tokens) >= 512:
                error()
            self.tokens.append(match[1])
            pos = match.end()
        self.fields = fields
        self.tree = self.expression()
        if self.index != len(self.tokens):
            error()

    def take(self) -> str:
        if self.index >= len(self.tokens):
            error()
        value = self.tokens[self.index]
        self.index += 1
        return value

    def peek(self) -> str:
        return self.tokens[self.index] if self.index < len(self.tokens) else ""

    def expression(self, minimum: int = 0, depth: int = 0) -> Any:
        if depth > 32:
            error()
        token = self.take()
        if token in ("-", "+", "!"):
            left = ("unary", token, self.expression(5, depth + 1))
        elif token == "(":
            left = self.expression(0, depth + 1)
            if self.take() != ")":
                error()
        elif token.startswith("{"):
            key = token[1:-1]
            if key not in self.fields:
                error()
            left = ("field", self.fields[key])
        elif token.startswith(('"', "'")):
            try:
                value = json.loads(token) if token[0] == '"' else re.sub(r"\\(['\\])", r"\1", token[1:-1])
                value.encode("utf-8")
            except (ValueError, UnicodeError):
                error()
            left = ("literal", value)
        elif token[0].isdigit():
            number = float(token)
            if not math.isfinite(number):
                error()
            left = ("literal", number)
        else:
            function = token.upper()
            if function not in ARITY or self.take() != "(":
                error()
            args = []
            while self.peek() != ")":
                args.append(self.expression(0, depth + 1))
                if self.peek() != ",":
                    break
                self.take()
            if self.take() != ")" or not ARITY[function][0] <= len(args) <= ARITY[function][1]:
                error()
            left = ("call", function, args)
        while self.peek() in PRECEDENCE and PRECEDENCE[self.peek()] >= minimum:
            op = self.take()
            left = ("binary", op, left, self.expression(PRECEDENCE[op] + 1, depth + 1))
        return left

    def evaluate(self, record_id: str, values: dict[str, Any]) -> Any:
        def string(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, bool):
                return "1" if value else "0"
            return str(value)

        def calc(node: Any) -> Any:
            kind = node[0]
            if kind == "literal":
                return node[1]
            if kind == "field":
                return values.get(node[1])
            if kind == "unary":
                value = calc(node[2])
                return (
                    not value
                    if node[1] == "!"
                    else (-float(value or 0) if node[1] == "-" else float(value or 0))
                )
            if kind == "binary":
                left, right = calc(node[2]), calc(node[3])
                op = node[1]
                if op == "&":
                    return string(left) + string(right)
                if op in ("+", "-", "*", "/"):
                    return {"+": operator.add, "-": operator.sub, "*": operator.mul, "/": operator.truediv}[
                        op
                    ](float(left or 0), float(right or 0))
                if left is None:
                    left = "" if isinstance(right, str) else 0
                if right is None:
                    right = "" if isinstance(left, str) else 0
                return {
                    "=": operator.eq,
                    "!=": operator.ne,
                    ">": operator.gt,
                    "<": operator.lt,
                    ">=": operator.ge,
                    "<=": operator.le,
                }[op](left, right)
            function, args = node[1], node[2]
            if function == "IF":
                return calc(args[1]) if calc(args[0]) else calc(args[2])
            if function in ("AND", "OR"):
                return all(calc(a) for a in args) if function == "AND" else any(calc(a) for a in args)
            args = [calc(a) for a in args]
            if function in ("TRUE", "FALSE", "BLANK", "RECORD_ID"):
                return {"TRUE": True, "FALSE": False, "BLANK": None, "RECORD_ID": record_id}[function]
            if function == "NOT":
                return not args[0]
            if function in ("LOWER", "UPPER", "LEN"):
                return {"LOWER": str.lower, "UPPER": str.upper, "LEN": len}[function](string(args[0]))
            if function == "CONCATENATE":
                return "".join(string(a) for a in args)
            if function in ("FIND", "SEARCH"):
                needle, haystack = string(args[0]), string(args[1])
                if function == "SEARCH":
                    needle, haystack = needle.lower(), haystack.lower()
                return haystack.find(needle, max(0, int(args[2]) - 1) if len(args) == 3 else 0) + 1
            return (args[0] > args[1]) if function == "IS_AFTER" else (args[0] < args[1])

        try:
            result = calc(self.tree)
            return None if isinstance(result, float) and not math.isfinite(result) else result
        except (ValueError, TypeError, ArithmeticError, RecursionError):
            return None  # Airtable excludes per-cell #Error! values from filter results.


def field_map(table: dict[str, Any]) -> dict[str, str]:
    return {key: f["id"] for f in table["fields"] for key in (f["name"], f["id"])}


def values(table: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record["fields"])
    for field in table["fields"]:
        if field["type"] == "formula":
            result[field["id"]] = Formula(field["options"]["formula"], field_map(table)).evaluate(
                record["id"], result
            )
    return result
