"""Drive q is its own language; a strict parser prevents silent query broadening."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from emulators.core.clock import parse_instant
from emulators.providers.google.common import google_error

Predicate = Callable[[dict[str, Any]], bool]
STRING_FIELDS = {"name", "mimeType", "fullText", "visibility"}
DATE_FIELDS = {"modifiedTime", "createdTime", "viewedByMeTime"}
BOOL_FIELDS = {"trashed", "starred", "sharedWithMe"}
MEMBER_FIELDS = {"parents", "owners", "writers", "readers"}
LEX = re.compile(r"\s*(?:('(?:\\.|[^'\\])*')|([A-Za-z_][A-Za-z_0-9]*)|(!=|<=|>=|=|<|>|[(){}]))")


class Parser:
    def __init__(self, text: str) -> None:
        self.tokens: list[tuple[str, str]] = []
        self.pos = 0
        offset = 0
        while offset < len(text):
            if not text[offset:].strip():
                break
            match = LEX.match(text, offset)
            if not match:
                raise ValueError
            literal, word, symbol = match.groups()
            if literal is not None:
                self.tokens.append(("string", re.sub(r"\\(['\\])", r"\1", literal[1:-1])))
            else:
                self.tokens.append(("token", word or symbol))
            offset = match.end()

    def take(self, expected: str | None = None) -> tuple[str, str]:
        if self.pos >= len(self.tokens):
            raise ValueError
        token = self.tokens[self.pos]
        if expected and token != ("token", expected):
            raise ValueError
        self.pos += 1
        return token

    def peek(self, value: str) -> bool:
        return self.pos < len(self.tokens) and self.tokens[self.pos] == ("token", value)

    def parse(self) -> Predicate:
        result = self.logical_or()
        if self.pos != len(self.tokens):
            raise ValueError
        return result

    def logical_or(self) -> Predicate:
        result = self.logical_and()
        while self.peek("or"):
            self.take()
            left, right = result, self.logical_and()

            def result(f: dict[str, Any], a: Predicate = left, b: Predicate = right) -> bool:
                return a(f) or b(f)

        return result

    def logical_and(self) -> Predicate:
        result = self.atom()
        while self.peek("and"):
            self.take()
            left, right = result, self.atom()

            def result(f: dict[str, Any], a: Predicate = left, b: Predicate = right) -> bool:
                return a(f) and b(f)

        return result

    def atom(self) -> Predicate:
        if self.peek("not"):
            self.take()
            inner = self.atom()
            return lambda f: not inner(f)
        if self.peek("("):
            self.take()
            inner = self.logical_or()
            self.take(")")
            return inner
        kind, field = self.take()
        if kind == "string":
            self.take("in")
            _, container = self.take()
            if container not in MEMBER_FIELDS:
                raise ValueError
            return lambda f: field in f.get("_" + container, f.get(container, []))
        if field in ("properties", "appProperties"):
            self.take("has")
            self.take("{")
            pairs: dict[str, str] = {}
            while True:
                _, key = self.take()
                if key not in ("key", "value") or key in pairs:
                    raise ValueError
                self.take("=")
                value_kind, value = self.take()
                if value_kind != "string":
                    raise ValueError
                pairs[key] = value
                if not self.peek("and"):
                    break
                self.take()
            self.take("}")
            if "key" not in pairs:
                raise ValueError
            return lambda f: (
                pairs["key"] in f.get(field, {})
                and ("value" not in pairs or f[field][pairs["key"]] == pairs["value"])
            )
        if field in BOOL_FIELDS:
            if field == "sharedWithMe" and not (self.peek("=") or self.peek("!=")):
                return lambda f: bool(f.get("sharedWithMe", False))
            _, operator = self.take()
            if operator not in ("=", "!="):
                raise ValueError
            boolean_kind, boolean = self.take()
            if boolean_kind != "token" or boolean not in ("true", "false"):
                raise ValueError
            return lambda f: (bool(f.get(field, False)) == (boolean == "true")) == (operator == "=")
        if field not in STRING_FIELDS | DATE_FIELDS:
            raise ValueError
        _, operator = self.take()
        value_kind, value = self.take()
        if value_kind != "string":
            raise ValueError
        if field in DATE_FIELDS:
            if operator not in ("=", "!=", ">", "<", ">=", "<="):
                raise ValueError
            stamp = parse_instant(value)

            def compare_date(f: dict[str, Any]) -> bool:
                if not f.get(field):
                    return False
                current = parse_instant(f[field])
                return {
                    "=": current == stamp,
                    "!=": current != stamp,
                    ">": current > stamp,
                    "<": current < stamp,
                    ">=": current >= stamp,
                    "<=": current <= stamp,
                }[operator]

            return compare_date
        if operator not in ("=", "!=", "contains") or (
            operator == "contains" and field not in ("name", "fullText", "mimeType")
        ):
            raise ValueError

        def compare_string(f: dict[str, Any]) -> bool:
            text = str(f.get("_text", "") if field == "fullText" else f.get(field, ""))
            if operator == "contains":
                if field == "name":
                    return any(
                        text[m.start() :].casefold().startswith(value.casefold())
                        for m in re.finditer(r"(?<!\w)\w", text)
                    )
                if field == "mimeType":
                    return value.casefold() in text.casefold()
                if value.startswith('"') and value.endswith('"'):

                    def normalize(s: str) -> str:
                        return " ".join(re.findall(r"[^\W_]+", s.casefold()))

                    return " " + normalize(value[1:-1]) + " " in " " + normalize(text) + " "
                return set(re.findall(r"[^\W_]+", value.casefold())) <= set(
                    re.findall(r"[^\W_]+", text.casefold())
                )
            equal = text.casefold() == value.casefold()
            return equal if operator == "=" else not equal

        return compare_string


def compile_query(text: str) -> Predicate:
    if not text.strip():
        return lambda _: True
    try:
        return Parser(text).parse()
    except (ValueError, TypeError, OverflowError):
        raise google_error(400, "Invalid Value", reason="invalid", location="q") from None


def order_files(rows: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    keys = [k.strip().split() for k in order.split(",") if k.strip()]
    valid = {
        "folder",
        "modifiedTime",
        "name",
        "createdTime",
        "quotaBytesUsed",
        "recency",
        "starred",
        "viewedByMeTime",
        "name_natural",
        "modifiedByMeTime",
        "sharedWithMeTime",
    }
    for parts in reversed(keys):
        if len(parts) > 2 or parts[0] not in valid or (len(parts) == 2 and parts[1] not in ("asc", "desc")):
            raise google_error(400, "Invalid orderBy parameter", reason="invalid", location="orderBy")
        key = parts[0]

        def sort_key(f: dict[str, Any], key: str = key) -> Any:
            if key == "folder":
                return f.get("parents", [""])[0] if f.get("parents") else ""
            if key == "quotaBytesUsed":
                return int(f.get(key, 0))
            if key == "recency":
                return max(f.get(k, "") for k in ("modifiedTime", "viewedByMeTime", "createdTime"))
            if key == "starred":
                return bool(f.get(key))
            if key == "name_natural":
                return [
                    (0, int(x)) if x.isdigit() else (1, x.casefold()) for x in re.split(r"(\d+)", f["name"])
                ]
            return str(f.get(key, "")).casefold()

        rows = sorted(rows, key=sort_key, reverse=len(parts) == 2 and parts[1] == "desc")
    return rows
