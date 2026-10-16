"""A bounded CQL/JQL subset: unsupported clauses fail instead of widening searches."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from emulators.core.clock import parse_instant

from .common import Record

CQL_FIELDS = {
    "type",
    "space",
    "space.key",
    "space.id",
    "title",
    "text",
    "id",
    "content",
    "ancestor",
    "parent",
    "label",
    "creator",
    "lastmodifier",
    "created",
    "lastmodified",
}
JQL_FIELDS = {
    "project",
    "status",
    "statuscategory",
    "assignee",
    "reporter",
    "priority",
    "key",
    "issuekey",
    "id",
    "type",
    "issuetype",
    "labels",
    "created",
    "updated",
    "due",
    "duedate",
    "parent",
    "summary",
    "description",
    "text",
}
TEMPORAL = {"created", "lastmodified", "updated", "due", "duedate"}
TOKEN = re.compile(
    r"""\s*(?:("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(!=|>=|<=|!~|=|>|<|~)|([(),])|([^\s(),=!<>~]+))"""
)
Node = tuple[Any, ...]


class QueryError(ValueError):
    pass


@dataclass
class Query:
    node: Node | None
    order: list[tuple[str, bool]]
    text_terms: list[str]
    now: datetime
    user: str | None

    def matches(self, record: Record) -> bool:
        return self.node is None or self._eval(self.node, record)

    def _eval(self, node: Node, record: Record) -> bool:
        if node[0] == "and":
            return self._eval(node[1], record) and self._eval(node[2], record)
        if node[0] == "or":
            return self._eval(node[1], record) or self._eval(node[2], record)
        if node[0] == "not":
            return not self._eval(node[1], record)
        _, field, op, rhs = node
        lhs = record.get(field)
        if op in ("is", "is not"):
            empty = lhs is None or lhs == [] or lhs == ""
            return empty if op == "is" else not empty
        left = lhs if isinstance(lhs, list) else [lhs]
        right = rhs if isinstance(rhs, list) else [rhs]
        if op in ("!=", "not in", "!~"):
            if lhs is None or lhs == []:
                return False
            positive = "~" if op == "!~" else "="
            return not any(self._compare(field, a, positive, b) for a in left for b in right)
        return any(self._compare(field, a, "=" if op == "in" else op, b) for a in left for b in right)

    def _value(self, value: Any) -> Any:
        if isinstance(value, tuple):
            name, argument = value
            if name == "currentuser":
                return self.user
            base = self.now if name == "now" else self.now.replace(hour=0, minute=0, second=0, microsecond=0)
            if name == "endofday":
                base += timedelta(days=1, microseconds=-1)
            if argument:
                base += duration(argument)
            return base
        return value

    def _compare(self, field: str, a: Any, op: str, b: Any) -> bool:
        b = self._value(b)
        if a is None or b is None:
            return False
        if field in TEMPORAL:
            a = date_value(a, self.now)
            b = date_value(b, self.now)
            if a is None or b is None:
                return False
        else:
            a, b = str(a).casefold(), str(b).casefold()
        if op == "=":
            return a == b
        if op == "~":
            pattern = re.escape(str(b).strip('"')).replace(r"\*", ".*").replace(r"\?", ".")
            return bool(re.search(pattern, str(a), re.IGNORECASE))
        return {">": lambda: a > b, ">=": lambda: a >= b, "<": lambda: a < b, "<=": lambda: a <= b}[op]()

    def apply(self, records: list[Record]) -> list[Record]:
        result = [r for r in records if self.matches(r)]
        for field, descending in reversed(self.order):

            def key(record: Record, field: str = field) -> tuple[bool, Any]:
                value = record.get(field)
                if isinstance(value, list):
                    value = value[0] if value else None
                if field in TEMPORAL:
                    value = date_value(value, self.now)
                return value is not None, value if isinstance(value, datetime) else str(
                    value or ""
                ).casefold()

            result.sort(key=key, reverse=descending)
        return result


def duration(value: str) -> timedelta:
    match = re.fullmatch(r"([+-]?\d+)([mhdw]?)", value, re.IGNORECASE)
    if not match:
        raise QueryError("Invalid relative date.")
    number = int(match[1])
    return timedelta(
        seconds=number * {"m": 60, "h": 3600, "d": 86400, "w": 604800, "": 86400}[match[2].lower()]
    )


def date_value(value: Any, now: datetime) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    text = str(value)
    try:
        if re.fullmatch(r"[+-]?\d+[mhdw]", text, re.IGNORECASE):
            return now + duration(text)
        return parse_instant(text.replace("/", "-"))
    except ValueError:
        return None


class Parser:
    def __init__(self, expression: str, dialect: str, now: datetime, user: str | None) -> None:
        if len(expression) > 20000:
            raise QueryError("Query is too long.")
        self.tokens: list[tuple[str, bool]] = []
        position = 0
        while position < len(expression.rstrip()):
            match = TOKEN.match(expression, position)
            if not match:
                raise QueryError(f"Invalid query at character {position}.")
            quoted, operator, punctuation, word = match.groups()
            if word and ('"' in word or "'" in word):
                raise QueryError("Unterminated or misplaced quote.")
            value = re.sub(r"\\(.)", r"\1", quoted[1:-1]) if quoted else operator or punctuation or word
            self.tokens.append((value, quoted is not None))
            position = match.end()
        self.index = 0
        self.depth = 0
        self.fields = CQL_FIELDS if dialect == "cql" else JQL_FIELDS
        self.query = Query(None, [], [], now, user)

    def peek(self, value: str | None = None) -> bool:
        if self.index >= len(self.tokens):
            return value is None
        return (
            value is not None
            and not self.tokens[self.index][1]
            and self.tokens[self.index][0].casefold() == value
        )

    def take(self) -> tuple[str, bool]:
        if self.index >= len(self.tokens):
            raise QueryError("Unexpected end of query.")
        result = self.tokens[self.index]
        self.index += 1
        return result

    def expect(self, value: str) -> None:
        if not self.peek(value):
            raise QueryError(f"Expected {value}.")
        self.take()

    def field(self) -> str:
        name = self.take()[0].casefold()
        if name not in self.fields:
            raise QueryError(f"Unsupported field: {name}.")
        return name

    def parse(self) -> Query:
        if not self.peek() and not self.peek("order"):
            self.query.node = self.logical_or()
        if self.peek("order"):
            self.take()
            self.expect("by")
            while True:
                field = self.field()
                descending = self.peek("desc")
                if self.peek("asc") or descending:
                    self.take()
                self.query.order.append((field, descending))
                if not self.peek(","):
                    break
                self.take()
        if not self.peek():
            raise QueryError(f"Unexpected token: {self.take()[0]}.")
        return self.query

    def logical_or(self) -> Node:
        node = self.logical_and()
        while self.peek("or"):
            self.take()
            node = ("or", node, self.logical_and())
        return node

    def logical_and(self) -> Node:
        node = self.unary()
        while self.peek("and"):
            self.take()
            node = ("and", node, self.unary())
        return node

    def unary(self) -> Node:
        self.depth += 1
        if self.depth > 32:
            raise QueryError("Query nesting is too deep.")
        try:
            if self.peek("not"):
                self.take()
                return ("not", self.unary())
            if self.peek("("):
                self.take()
                node = self.logical_or()
                self.expect(")")
                return node
            field = self.field()
            op = self.take()[0].casefold()
            if op == "not":
                self.expect("in")
                op = "not in"
            if op == "is":
                if self.peek("not"):
                    self.take()
                    op = "is not"
                value = self.take()[0].casefold()
                if value not in ("empty", "null"):
                    raise QueryError("IS supports EMPTY or NULL.")
                return ("compare", field, op, None)
            if op not in ("=", "!=", ">", ">=", "<", "<=", "~", "!~", "in", "not in"):
                raise QueryError(f"Unsupported operator: {op}.")
            if op in ("in", "not in"):
                self.expect("(")
                values = [self.value(field)]
                while self.peek(","):
                    self.take()
                    values.append(self.value(field))
                self.expect(")")
                return ("compare", field, op, values)
            value = self.value(field)
            if op == "~" and field in ("text", "title", "summary") and isinstance(value, str):
                self.query.text_terms.append(value.strip('"'))
            return ("compare", field, op, value)
        finally:
            self.depth -= 1

    def value(self, field: str) -> Any:
        value, quoted = self.take()
        if not quoted and value.casefold() in ("and", "or", "order", ")", "(", ","):
            raise QueryError("A field value is required.")
        if not quoted and self.peek("("):
            name = value.casefold()
            if name not in ("currentuser", "now", "startofday", "endofday"):
                raise QueryError(f"Unsupported function: {name}.")
            self.take()
            argument = None if self.peek(")") else self.take()[0]
            self.expect(")")
            if name == "currentuser" and argument:
                raise QueryError("currentUser() takes no arguments.")
            if name != "currentuser" and argument:
                duration(argument)
            return name, argument
        if field in TEMPORAL and date_value(value, self.query.now) is None:
            raise QueryError("Invalid date literal.")
        return value


def parse_query(
    expression: str, dialect: str, *, now: datetime | None = None, user: str | None = None
) -> Query:
    try:
        return Parser(expression, dialect, now or datetime(2026, 10, 16, tzinfo=UTC), user).parse()
    except RecursionError:
        raise QueryError("Query is too complex.") from None
