"""The QBO query language (a small SQL subset).

Grammar (keywords case-insensitive)::

    SELECT * | COUNT(*) | Field, Field FROM Entity
      [WHERE cond [AND cond]...] [ORDERBY|ORDER BY Field [ASC|DESC], ...]
      [STARTPOSITION n] [MAXRESULTS m]

QBO supports AND only — OR is a QueryParserError (4000), as is any parse error.
Name-list entities default to ``Active = true`` unless the query mentions Active.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.quickbooks.common import BY_LOWER, NAME_LIST, fail, money, parse_time, public

_TOKEN_RE = re.compile(
    r"""
    (?P<STRING>'(?:[^']|'')*')
  | (?P<NUMBER>-?\d+(?:\.\d+)?)
  | (?P<OP><=|>=|!=|<>|=|<|>)
  | (?P<LPAREN>\()
  | (?P<RPAREN>\))
  | (?P<COMMA>,)
  | (?P<STAR>\*)
  | (?P<IDENT>[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<WS>\s+)
  | (?P<MISC>.)
""",
    re.VERBOSE,
)


@dataclass
class Condition:
    field: str
    op: str
    values: list[Any]


@dataclass
class ParsedQuery:
    entity: str
    fields: list[str] | None = None  # None = *
    count: bool = False
    conditions: list[Condition] = field(default_factory=list)
    order_by: list[tuple[str, bool]] = field(default_factory=list)  # (field, desc)
    start: int = 1
    max_results: int = 100


def _error(ctx: ProviderContext, detail: str) -> Any:
    fail(
        ctx,
        400,
        "Error parsing query",
        f"QueryParserError: {detail}",
        "4000",
        fault_type="ValidationFault",
    )


def _tokenize(ctx: ProviderContext, text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for match in _TOKEN_RE.finditer(text):
        kind = match.lastgroup or "MISC"
        if kind == "WS":
            continue
        if kind == "MISC":
            _error(ctx, f"unexpected character {match.group()!r}")
        tokens.append((kind, match.group()))
    return tokens


class _Parser:
    def __init__(self, ctx: ProviderContext, text: str) -> None:
        self.ctx = ctx
        self.tokens = _tokenize(ctx, text)
        self.pos = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            _error(self.ctx, "unexpected end of query")
        self.pos += 1
        return token  # type: ignore[return-value]

    def at_keyword(self, word: str) -> bool:
        token = self.peek()
        return token is not None and token[0] == "IDENT" and token[1].upper() == word

    def eat_keyword(self, word: str) -> bool:
        if self.at_keyword(word):
            self.pos += 1
            return True
        return False

    def expect_keyword(self, word: str) -> None:
        if not self.eat_keyword(word):
            _error(self.ctx, f"expected {word}")

    def expect(self, kind: str) -> str:
        token = self.next()
        if token[0] != kind:
            _error(self.ctx, f"expected {kind}, found {token[1]!r}")
        return token[1]

    def literal(self) -> Any:
        kind, value = self.next()
        if kind == "STRING":
            return value[1:-1].replace("''", "'").replace("\\'", "'")
        if kind == "NUMBER":
            return float(value) if "." in value else int(value)
        if kind == "IDENT" and value.lower() in ("true", "false"):
            return value.lower() == "true"
        _error(self.ctx, f"expected a literal, found {value!r}")

    def condition(self) -> Condition:
        field_name = self.expect("IDENT")
        if self.eat_keyword("IN"):
            self.expect("LPAREN")
            values = [self.literal()]
            while self.peek() and self.peek()[0] == "COMMA":  # type: ignore[index]
                self.next()
                values.append(self.literal())
            self.expect("RPAREN")
            return Condition(field_name, "IN", values)
        if self.eat_keyword("LIKE"):
            return Condition(field_name, "LIKE", [self.literal()])
        op = self.expect("OP")
        return Condition(field_name, op, [self.literal()])


def parse_query(ctx: ProviderContext, text: str) -> ParsedQuery:
    if not text or not text.strip():
        _error(ctx, "empty query")
    parser = _Parser(ctx, text.strip())
    parser.expect_keyword("SELECT")
    fields: list[str] | None = None
    count = False
    if parser.at_keyword("COUNT"):
        parser.next()
        parser.expect("LPAREN")
        parser.expect("STAR")
        parser.expect("RPAREN")
        count = True
    elif parser.peek() and parser.peek()[0] == "STAR":  # type: ignore[index]
        parser.next()
        # python-quickbooks sends "SELECT *, Sku FROM Item"
        if parser.peek() and parser.peek()[0] == "COMMA":  # type: ignore[index]
            while parser.peek() and parser.peek()[0] == "COMMA":  # type: ignore[index]
                parser.next()
                parser.expect("IDENT")
    else:
        fields = [parser.expect("IDENT")]
        while parser.peek() and parser.peek()[0] == "COMMA":  # type: ignore[index]
            parser.next()
            fields.append(parser.expect("IDENT"))
    parser.expect_keyword("FROM")
    entity_raw = parser.expect("IDENT")
    entity = BY_LOWER.get(entity_raw.lower())
    if entity is None:
        _error(ctx, f"entity {entity_raw!r} is not queryable")
    parsed = ParsedQuery(entity=entity, fields=fields, count=count)  # type: ignore[arg-type]
    if parser.eat_keyword("WHERE"):
        parsed.conditions.append(parser.condition())
        while True:
            if parser.eat_keyword("AND"):
                parsed.conditions.append(parser.condition())
            elif parser.at_keyword("OR"):
                _error(ctx, "OR is not supported; QBO queries support AND only")
            else:
                break
    if parser.eat_keyword("ORDERBY") or (parser.eat_keyword("ORDER") and parser.eat_keyword("BY")):
        while True:
            field_name = parser.expect("IDENT")
            desc = False
            if parser.eat_keyword("DESC"):
                desc = True
            else:
                parser.eat_keyword("ASC")
            parsed.order_by.append((field_name, desc))
            if parser.peek() and parser.peek()[0] == "COMMA":  # type: ignore[index]
                parser.next()
                continue
            break
    while parser.peek() is not None:
        if parser.eat_keyword("STARTPOSITION"):
            parsed.start = int(parser.expect("NUMBER"))
        elif parser.eat_keyword("MAXRESULTS"):
            parsed.max_results = int(parser.expect("NUMBER"))
        else:
            _error(ctx, f"unexpected token {parser.peek()[1]!r}")  # type: ignore[index]
    if parsed.start < 1:
        parsed.start = 1
    parsed.max_results = max(1, min(parsed.max_results, 1000))
    return parsed


def field_value(record: dict[str, Any], dotted: str) -> Any:
    current: Any = record
    parts = dotted.split(".")
    for part in parts:
        if not isinstance(current, dict):
            return None
        if part in current:
            current = current[part]
            continue
        lowered = {k.lower(): k for k in current}
        key = lowered.get(part.lower())
        if key is None:
            return None
        current = current[key]
    # References compare by their value (CustomerRef = '12').
    if isinstance(current, dict) and "value" in current and len(parts) == 1:
        return current.get("value")
    return current


def _comparable(a: Any, b: Any) -> tuple[Any, Any]:
    """Coerce a record value and a literal into comparable types."""
    if a is None or b is None:
        return a, b
    if isinstance(b, bool):
        if isinstance(a, str):
            return a.lower() == "true", b
        return bool(a), b
    if isinstance(b, (int, float)) and isinstance(a, (int, float)):
        return a, b
    if isinstance(b, (int, float)) and isinstance(a, str):
        try:
            return float(a), float(b)
        except ValueError:
            return a, b
    if isinstance(b, str) and isinstance(a, (int, float)) and not isinstance(a, bool):
        try:
            return a, float(b)
        except ValueError:
            return str(a), b
    if isinstance(a, str) and isinstance(b, str):
        ta, tb = parse_time(a), parse_time(b)
        if ta is not None and tb is not None and ("T" in a or "T" in b):
            return ta, tb
    return a, b


def _matches(record: dict[str, Any], cond: Condition) -> bool:
    raw = field_value(record, cond.field)
    if cond.op == "IN":
        return any(_comparable(raw, v)[0] == _comparable(raw, v)[1] for v in cond.values)
    if cond.op == "LIKE":
        if raw is None:
            return False
        pattern = "^" + re.escape(str(cond.values[0])).replace("%", ".*") + "$"
        return re.match(pattern, str(raw), re.IGNORECASE) is not None
    left, right = _comparable(raw, cond.values[0])
    try:
        if cond.op == "=":
            return left == right
        if cond.op in ("!=", "<>"):
            return left != right
        if left is None or right is None:
            return False
        if cond.op == "<":
            return left < right
        if cond.op == ">":
            return left > right
        if cond.op == "<=":
            return left <= right
        if cond.op == ">=":
            return left >= right
    except TypeError:
        return str(left) == str(right) if cond.op == "=" else False
    return False


def run_query(ctx: ProviderContext, text: str) -> dict[str, Any]:
    """Execute a query and return the ``QueryResponse`` body fragment."""
    parsed = parse_query(ctx, text)
    records = [r for r in ctx.store.collection(parsed.entity) if not r.get("_deleted")]
    if parsed.entity in NAME_LIST and not any(c.field.lower() == "active" for c in parsed.conditions):
        records = [r for r in records if r.get("Active", True) is not False]
    for cond in parsed.conditions:
        records = [r for r in records if _matches(r, cond)]
    if parsed.count:
        return {"totalCount": len(records)}
    total = len(records)
    for field_name, desc in reversed(parsed.order_by):

        def keyfn(r: dict[str, Any], f: str = field_name) -> tuple[int, str]:
            value = field_value(r, f)
            return (0, "") if value is None else (1, str(value))

        records.sort(key=keyfn, reverse=desc)
    start = parsed.start
    page = records[start - 1 : start - 1 + parsed.max_results]
    if not page:
        return {}
    if parsed.fields is None:
        items = [public(r) for r in page]
    else:
        items = []
        for record in page:
            projected: dict[str, Any] = {"Id": record.get("Id")}
            if "SyncToken" in record:
                projected["SyncToken"] = record["SyncToken"]
            for name in parsed.fields:
                value = field_value(record, name)
                # keep the canonical key casing from the record
                canonical = next((k for k in record if k.lower() == name.split(".")[0].lower()), name)
                projected[canonical] = public(value)
            items.append(projected)
    _ = money  # keep import used by callers expecting money here
    _ = total
    return {
        parsed.entity: items,
        "startPosition": start,
        "maxResults": len(items),
    }
