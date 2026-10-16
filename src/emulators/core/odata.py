"""OData v4 query engine for the Microsoft Graph emulator (stdlib only).

Applies Graph-style query options (``$filter``, ``$orderby``, ``$select``, ``$expand``, ``$top``,
``$skip``, ``$count``, ``$search``) to in-memory records, i.e. plain nested dicts/lists with the
camelCase keys Graph returns.

Design notes (why it behaves the way it does):

* ``$filter`` is tokenized and parsed by a Pratt parser into a frozen AST, then evaluated per record.
  Parsed ASTs are cached by expression text, so validating a query up front and applying it later
  costs a single parse.
* Values are compared by *type*, not by spelling: ISO-8601 strings on either side are compared as
  datetimes (naive values, including Graph's ``2026-10-15T14:00:00.0000000``, are UTC), numbers
  numerically, GUIDs case-insensitively, other strings case-insensitively unless
  ``case_sensitive=True``.
* Missing properties evaluate to ``null``. ``x eq null`` is true for a missing ``x``; ordering
  comparisons (``gt``/``ge``/``lt``/``le``) with a null operand are false.
* Boolean evaluation is two-valued: only a result that is exactly ``True`` selects a record, so
  ``not`` of an "unknown" (e.g. a bare missing property) is true.
* ``not`` binds looser than comparisons here (``not a eq b`` means ``not (a eq b)``), which is what
  callers mean and matches how Graph's backends behave in practice.
* Every malformed clause raises :class:`ODataError`, which the Graph layer maps to HTTP 400
  ``{"error": {"code": "BadRequest", "message": ...}}``.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any

__all__ = [
    "DEFAULT_SELECT_ALWAYS",
    "Call",
    "Compare",
    "ExpandItem",
    "FilterNode",
    "InList",
    "Lambda",
    "Literal",
    "Logical",
    "Not",
    "ODataError",
    "OrderItem",
    "Path",
    "QueryOptions",
    "QueryResult",
    "apply_orderby",
    "apply_query",
    "apply_select",
    "compile_filter",
    "compile_search",
    "parse_expand",
    "parse_filter",
    "parse_orderby",
    "parse_query_options",
    "parse_select",
    "select_record",
]

DEFAULT_SELECT_ALWAYS: tuple[str, ...] = ("id", "@odata.type", "@odata.etag")

_MAX_DEPTH = 64  # parenthesis/lambda nesting limit; keeps hostile input from blowing the stack


# --------------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------------


class ODataError(ValueError):
    """A malformed or unsupported query option; the HTTP layer renders it as a 400 ``BadRequest``."""

    code: str = "BadRequest"
    status_code: int = 400

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        """Graph's error envelope, ready to serialize as the response body."""
        return {"error": {"code": self.code, "message": self.message}}


def _fail(clause: str, text: str, pos: int, detail: str) -> ODataError:
    """Build a Graph-style syntax error pointing at a character position."""
    return ODataError(f"Invalid {clause} clause: {detail} at position {pos} in '{text}'.")


# --------------------------------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------------------------------


class _Guid(str):
    """A GUID literal: a string that always compares case-insensitively, even in case-sensitive mode."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Literal:
    """A constant. ``kind`` is string/int/decimal/bool/null/guid/datetime/date."""

    value: Any
    kind: str


@dataclass(frozen=True, slots=True)
class Path:
    """A property path such as ``from/emailAddress/address`` (or a lambda variable plus a sub-path)."""

    segments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Not:
    """Logical negation."""

    operand: FilterNode


@dataclass(frozen=True, slots=True)
class Compare:
    """Binary comparison; ``op`` is one of eq/ne/gt/ge/lt/le."""

    op: str
    left: FilterNode
    right: FilterNode


@dataclass(frozen=True, slots=True)
class Logical:
    """N-ary ``and``/``or``. Chains are flattened so long ``or`` lists never deepen the tree."""

    op: str
    operands: tuple[FilterNode, ...]


@dataclass(frozen=True, slots=True)
class InList:
    """``left in (a, b, c)``."""

    left: FilterNode
    items: tuple[FilterNode, ...]


@dataclass(frozen=True, slots=True)
class Call:
    """A built-in function call; ``name`` is lower-cased."""

    name: str
    args: tuple[FilterNode, ...]


@dataclass(frozen=True, slots=True)
class Lambda:
    """``source/any(var: body)`` or ``source/all(var: body)``; ``any()`` has no variable or body."""

    kind: str
    source: Path
    var: str | None
    body: FilterNode | None


FilterNode = Literal | Path | Not | Compare | Logical | InList | Call | Lambda


# --------------------------------------------------------------------------------------------------
# ISO-8601 handling
# --------------------------------------------------------------------------------------------------

_ISO_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})"
    r"(?:[Tt]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:[.,]([0-9]+))?)?([Zz]|[+-][0-9]{2}(?::?[0-9]{2})?)?)?$"
)


@lru_cache(maxsize=8192)
def _parse_iso_cached(text: str) -> datetime | None:
    m = _ISO_RE.match(text)
    if m is None:
        return None
    year, month, day, hour, minute, second, frac, tz_text = m.groups()
    try:
        micro = int((frac + "000000")[:6]) if frac else 0
        if tz_text is None or tz_text in ("Z", "z"):
            tz = UTC
        else:
            digits = tz_text[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
            tz = timezone(-offset if tz_text[0] == "-" else offset)
        return datetime(
            int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0), micro, tz
        )
    except ValueError:
        return None


def _parse_iso(text: str) -> datetime | None:
    """Parse an ISO-8601 date/datetime string; naive values are UTC (Graph's convention)."""
    if len(text) < 10 or text[4] != "-" or not text[:4].isdigit():  # cheap reject before the cache
        return None
    return _parse_iso_cached(text)


def _to_datetime(value: Any) -> datetime | None:
    """Coerce a datetime/date/ISO string to an aware datetime, or None when it is not temporal."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        return _parse_iso(value)
    return None


# --------------------------------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------------------------------

_GUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![\w-])")
_DATETIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?"
    r"(?:[Zz]|[+-][0-9]{2}:[0-9]{2})?(?!\w)"
)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?!\w)")
_NUMBER_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?(?!\w)")
_IDENT_RE = re.compile(r"[^\W\d][\w@$.]*|[@$][\w@$.]*")
_STRING_RE = re.compile(r"'((?:[^']|'')*)'")  # one way to match each char: no backtracking blow-up
_DIGITS = "0123456789"
_PUNCT = {"(": "LPAREN", ")": "RPAREN", ",": "COMMA", "/": "SLASH", ":": "COLON"}


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    pos: int
    value: Any = None


def _literal_token(text: str, i: int, clause: str) -> _Token | None:
    """Try GUID, datetime, date, then number at ``i``; None when nothing matches."""
    for kind, rx in (
        ("GUID", _GUID_RE),
        ("DATETIME", _DATETIME_RE),
        ("DATE", _DATE_RE),
        ("NUMBER", _NUMBER_RE),
    ):
        m = rx.match(text, i)
        if m is None:
            continue
        raw = m.group(0)
        if kind == "GUID":
            return _Token("GUID", raw, i, raw.lower())
        if kind == "NUMBER":
            is_float = "." in raw or "e" in raw or "E" in raw
            return _Token("NUMBER", raw, i, float(raw) if is_float else int(raw))
        parsed = _parse_iso(raw)
        if parsed is None:
            raise _fail(clause, text, i, f"Invalid {kind.lower()} literal '{raw}'")
        return _Token(kind, raw, i, parsed.date() if kind == "DATE" else parsed)
    return None


def _tokenize(text: str, clause: str) -> list[_Token]:
    tokens: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == "'":
            m = _STRING_RE.match(text, i)
            if m is None:
                raise _fail(clause, text, i, "Unterminated string literal")
            tokens.append(_Token("STRING", m.group(0), i, m.group(1).replace("''", "'")))
            i = m.end()
        elif ch in _PUNCT:
            tokens.append(_Token(_PUNCT[ch], ch, i))
            i += 1
        elif ch in _DIGITS or (ch == "-" and i + 1 < n and text[i + 1] in _DIGITS):
            tok = _literal_token(text, i, clause)
            if tok is None:
                raise _fail(clause, text, i, "Syntax error")
            tokens.append(tok)
            i += len(tok.text)
        elif ch.isalpha() or ch in "_@$":
            tok = _literal_token(text, i, clause) if ch in "abcdefABCDEF" else None
            if tok is not None and tok.kind == "GUID":  # a GUID that happens to start with a hex letter
                tokens.append(tok)
                i += len(tok.text)
                continue
            m = _IDENT_RE.match(text, i)
            if m is None:
                raise _fail(clause, text, i, "Syntax error")
            ident = m.group(0)
            end = m.end()
            if end < n and text[end] == "'" and "." in ident:  # enum literal: Namespace.Type'member'
                sm = _STRING_RE.match(text, end)
                if sm is None:
                    raise _fail(clause, text, end, "Unterminated string literal")
                tokens.append(_Token("STRING", text[i : sm.end()], i, sm.group(1).replace("''", "'")))
                i = sm.end()
                continue
            tokens.append(_Token("IDENT", ident, i))
            i = end
        else:
            raise _fail(clause, text, i, "Syntax error")
    tokens.append(_Token("EOF", "", n))
    return tokens


# --------------------------------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------------------------------

# Binding powers (higher binds tighter): or < and < not < eq/ne < relational/in.
_BP_NOT = 30
_INFIX_BP: dict[str, int] = {
    "or": 10,
    "and": 20,
    "eq": 40,
    "ne": 40,
    "gt": 50,
    "ge": 50,
    "lt": 50,
    "le": 50,
    "in": 50,
}
_COMPARE_OPS = frozenset({"eq", "ne", "gt", "ge", "lt", "le"})

_FUNCTION_ARITY: dict[str, tuple[int, int]] = {
    "startswith": (2, 2),
    "endswith": (2, 2),
    "contains": (2, 2),
    "tolower": (1, 1),
    "toupper": (1, 1),
    "trim": (1, 1),
    "length": (1, 1),
    "indexof": (2, 2),
    "substring": (2, 3),
    "concat": (2, 2),
    "year": (1, 1),
    "month": (1, 1),
    "day": (1, 1),
    "hour": (1, 1),
    "minute": (1, 1),
    "second": (1, 1),
    "date": (1, 1),
    "now": (0, 0),
}
_BOOLEAN_FUNCTIONS = frozenset({"startswith", "endswith", "contains"})


def _is_non_boolean(node: FilterNode) -> bool:
    """True when ``node`` can never be a boolean (so it cannot stand alone as a condition)."""
    if isinstance(node, Literal):
        return node.kind != "bool"
    if isinstance(node, Call):
        return node.name not in _BOOLEAN_FUNCTIONS
    return False


class _Parser:
    """Pratt parser shared by ``$filter`` and ``$orderby`` (orderby keys are expressions too)."""

    def __init__(self, text: str, clause: str) -> None:
        self.text = text
        self.clause = clause
        self.tokens = _tokenize(text, clause)
        self.i = 0
        self.depth = 0

    # -- token helpers ------------------------------------------------------------------------
    def _peek(self) -> _Token:
        return self.tokens[self.i]

    def _next(self) -> _Token:
        tok = self.tokens[self.i]
        if tok.kind != "EOF":
            self.i += 1
        return tok

    def _error(self, tok: _Token, detail: str = "Syntax error") -> ODataError:
        return _fail(self.clause, self.text, tok.pos, detail)

    def _expect(self, kind: str, detail: str) -> _Token:
        tok = self._peek()
        if tok.kind != kind:
            raise self._error(tok, detail)
        return self._next()

    def _require_boolean(self, node: FilterNode) -> FilterNode:
        if _is_non_boolean(node):
            raise ODataError(
                f"Invalid {self.clause} clause: A non-boolean expression was used where a boolean "
                f"condition is required in '{self.text}'."
            )
        return node

    # -- entry points -------------------------------------------------------------------------
    def parse_filter(self) -> FilterNode:
        node = self._expr(0)
        tok = self._peek()
        if tok.kind != "EOF":
            raise self._error(tok)
        return self._require_boolean(node)

    def parse_orderby(self) -> list[OrderItem]:
        items: list[OrderItem] = []
        while True:
            expr = self._expr(0)
            descending = False
            tok = self._peek()
            if tok.kind == "IDENT" and tok.text.lower() in ("asc", "desc"):
                descending = tok.text.lower() == "desc"
                self._next()
            items.append(OrderItem(expr, descending))
            tok = self._peek()
            if tok.kind == "COMMA":
                self._next()
                continue
            if tok.kind != "EOF":
                raise self._error(tok)
            return items

    # -- grammar ------------------------------------------------------------------------------
    def _expr(self, min_bp: int) -> FilterNode:
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise self._error(self._peek(), "Expression is nested too deeply")
        try:
            left = self._prefix()
            while True:
                tok = self._peek()
                if tok.kind != "IDENT":
                    break
                op = tok.text.lower()
                bp = _INFIX_BP.get(op)
                if bp is None or bp <= min_bp:
                    break
                self._next()
                if op in ("and", "or"):
                    right = self._expr(bp)
                    left = self._logical(op, left, right)
                elif op == "in":
                    left = InList(left, self._in_list())
                else:
                    left = Compare(op, left, self._expr(bp))
            return left
        finally:
            self.depth -= 1

    def _logical(self, op: str, left: FilterNode, right: FilterNode) -> Logical:
        self._require_boolean(left)
        self._require_boolean(right)
        if isinstance(left, Logical) and left.op == op:
            return Logical(op, (*left.operands, right))
        return Logical(op, (left, right))

    def _in_list(self) -> tuple[FilterNode, ...]:
        self._expect("LPAREN", "'(' expected after 'in'")
        items: list[FilterNode] = []
        if self._peek().kind == "RPAREN":
            self._next()
            return ()
        while True:
            items.append(self._expr(0))
            tok = self._next()
            if tok.kind == "RPAREN":
                return tuple(items)
            if tok.kind != "COMMA":
                raise self._error(tok, "',' or ')' expected")

    def _prefix(self) -> FilterNode:
        tok = self._next()
        match tok.kind:
            case "STRING":
                return Literal(tok.value, "string")
            case "NUMBER":
                return Literal(tok.value, "int" if isinstance(tok.value, int) else "decimal")
            case "GUID":
                return Literal(_Guid(tok.value), "guid")
            case "DATETIME":
                return Literal(tok.value, "datetime")
            case "DATE":
                return Literal(tok.value, "date")
            case "LPAREN":
                inner = self._expr(0)
                self._expect("RPAREN", "')' expected")
                return inner
            case "IDENT":
                return self._ident(tok)
            case "EOF":
                raise self._error(tok, "Expression expected")
            case _:
                raise self._error(tok)

    def _ident(self, tok: _Token) -> FilterNode:
        low = tok.text.lower()
        nxt = self._peek()
        if low == "not" and nxt.kind != "SLASH":
            return Not(self._require_boolean(self._expr(_BP_NOT)))
        if low in ("true", "false", "null") and nxt.kind not in ("SLASH", "LPAREN"):
            if low == "null":
                return Literal(None, "null")
            return Literal(low == "true", "bool")
        if nxt.kind == "LPAREN":
            return self._call(tok, low)
        return self._path(tok)

    def _call(self, tok: _Token, name: str) -> Call:
        arity = _FUNCTION_ARITY.get(name)
        if arity is None:
            raise self._error(tok, f"Unknown function '{tok.text}'")
        self._next()  # (
        args: list[FilterNode] = []
        if self._peek().kind == "RPAREN":
            self._next()
        else:
            while True:
                args.append(self._expr(0))
                nxt = self._next()
                if nxt.kind == "RPAREN":
                    break
                if nxt.kind != "COMMA":
                    raise self._error(nxt, "',' or ')' expected")
        low, high = arity
        if not low <= len(args) <= high:
            expected = f"{low}" if low == high else f"{low} to {high}"
            raise self._error(tok, f"Function '{tok.text}' expects {expected} argument(s), got {len(args)}")
        return Call(name, tuple(args))

    def _path(self, first: _Token) -> FilterNode:
        segments = [first.text]
        while self._peek().kind == "SLASH":
            self._next()
            seg = self._expect("IDENT", "Property name expected after '/'")
            low = seg.text.lower()
            if low in ("any", "all") and self._peek().kind == "LPAREN":
                return self._lambda(low, Path(tuple(segments)), seg)
            segments.append(seg.text)
        return Path(tuple(segments))

    def _lambda(self, kind: str, source: Path, kw: _Token) -> Lambda:
        self._next()  # (
        if self._peek().kind == "RPAREN":
            self._next()
            if kind == "all":
                raise self._error(kw, "'all' requires a lambda expression")
            return Lambda(kind, source, None, None)
        var = self._expect("IDENT", "Lambda variable name expected")
        self._expect("COLON", "':' expected")
        body = self._require_boolean(self._expr(0))
        self._expect("RPAREN", "')' expected")
        return Lambda(kind, source, var.text, body)


@lru_cache(maxsize=512)
def _parse_filter_cached(expr: str) -> FilterNode:
    if not expr.strip():
        raise _fail("filter", expr, 0, "Expression expected")
    try:
        return _Parser(expr, "filter").parse_filter()
    except RecursionError:
        raise ODataError("Invalid filter clause: Expression is too complex.") from None


def parse_filter(expr: str) -> FilterNode:
    """Parse a ``$filter`` expression into an immutable AST (cached by expression text)."""
    return _parse_filter_cached(expr)


@dataclass(frozen=True, slots=True)
class OrderItem:
    """One ``$orderby`` key: an expression (normally a property path) and a direction."""

    expr: FilterNode
    descending: bool = False


@lru_cache(maxsize=512)
def _parse_orderby_cached(expr: str) -> tuple[OrderItem, ...]:
    if not expr.strip():
        raise _fail("orderby", expr, 0, "Expression expected")
    try:
        return tuple(_Parser(expr, "orderby").parse_orderby())
    except RecursionError:
        raise ODataError("Invalid orderby clause: Expression is too complex.") from None


def parse_orderby(expr: str) -> list[OrderItem]:
    """Parse ``a/b desc, c`` into ordered :class:`OrderItem` keys."""
    return list(_parse_orderby_cached(expr))


# --------------------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------------------

_MISSING: Any = object()


def _get_member(mapping: Mapping[str, Any], name: str) -> Any:
    """Look up ``name`` exactly, then case-insensitively (Graph property names are case-insensitive)."""
    if name in mapping:
        return mapping[name]
    low = name.lower()
    for key in mapping:
        if isinstance(key, str) and key.lower() == low:
            return mapping[key]
    return _MISSING


def _step(current: Any, seg: str) -> Any:
    """Advance one path segment; anything unresolvable becomes null."""
    if seg == "$count":
        if current is None:
            return 0
        return len(current) if isinstance(current, (list, tuple, set, frozenset)) else None
    if isinstance(current, Mapping):
        value = _get_member(current, seg)
        if value is not _MISSING:
            return value
        if "." in seg and not seg.startswith("@"):  # type-cast segment, e.g. microsoft.graph.user/mail
            return current
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _items_of(value: Any) -> list[Any]:
    """View a lambda source as a list: null is empty, a lone value is a one-element collection."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


class _Evaluator:
    """Evaluates AST nodes against a record. ``scope`` maps lambda variables to their current item."""

    __slots__ = ("case_sensitive",)

    def __init__(self, case_sensitive: bool = False) -> None:
        self.case_sensitive = case_sensitive

    def eval(self, node: FilterNode, root: Mapping[str, Any], scope: Mapping[str, Any]) -> Any:
        match node:
            case Literal(value=value):
                return value
            case Path(segments=segments):
                return self._path(segments, root, scope)
            case Compare(op=op, left=left, right=right):
                return self._compare(op, self.eval(left, root, scope), self.eval(right, root, scope))
            case Logical(op="and", operands=operands):
                return all(self.eval(o, root, scope) is True for o in operands)
            case Logical(operands=operands):
                return any(self.eval(o, root, scope) is True for o in operands)
            case Not(operand=operand):
                return self.eval(operand, root, scope) is not True
            case InList(left=left, items=items):
                lhs = self.eval(left, root, scope)
                return any(self._compare("eq", lhs, self.eval(i, root, scope)) for i in items)
            case Call(name=name, args=args):
                return self._call(name, [self.eval(a, root, scope) for a in args])
            case Lambda(kind=kind, source=source, var=var, body=body):
                items = _items_of(self._path(source.segments, root, scope))
                if body is None or var is None:
                    return bool(items)
                results = (self.eval(body, root, {**scope, var: item}) is True for item in items)
                return any(results) if kind == "any" else all(results)
        raise TypeError(f"unsupported node {node!r}")  # pragma: no cover - parser never builds others

    def _path(self, segments: tuple[str, ...], root: Mapping[str, Any], scope: Mapping[str, Any]) -> Any:
        first = segments[0]
        if first in scope:
            current: Any = scope[first]
            rest = segments[1:]
        else:
            current = root
            rest = segments
        for seg in rest:
            current = _step(current, seg)
        return current

    # -- comparison ---------------------------------------------------------------------------
    def _comparable(self, a: Any, b: Any) -> tuple[Any, Any] | None:
        """Return two values of one comparable type, or None when the operands are incomparable."""
        if isinstance(a, bool) or isinstance(b, bool):
            return (a, b) if isinstance(a, bool) and isinstance(b, bool) else None
        if _is_number(a) and _is_number(b):
            return a, b
        if isinstance(a, (datetime, date)) or isinstance(b, (datetime, date)):
            da, db = _to_datetime(a), _to_datetime(b)
            return None if da is None or db is None else (da, db)
        if isinstance(a, str) and isinstance(b, str):
            da = _parse_iso(a)
            if da is not None:
                db = _parse_iso(b)
                if db is not None:
                    return da, db
            if isinstance(a, _Guid) or isinstance(b, _Guid) or not self.case_sensitive:
                return a.casefold(), b.casefold()
            return a, b
        return None

    def _compare(self, op: str, a: Any, b: Any) -> bool:
        if a is None or b is None:
            both_null = a is None and b is None
            return both_null if op == "eq" else (not both_null if op == "ne" else False)
        pair = self._comparable(a, b)
        if pair is None:
            return op == "ne"
        x, y = pair
        match op:
            case "eq":
                return x == y
            case "ne":
                return x != y
            case "gt":
                return x > y
            case "ge":
                return x >= y
            case "lt":
                return x < y
            case _:
                return x <= y

    # -- functions ----------------------------------------------------------------------------
    def _call(self, name: str, args: list[Any]) -> Any:
        cs = self.case_sensitive
        match name:
            case "startswith" | "endswith" | "contains":
                s, t = args
                if not isinstance(s, str) or not isinstance(t, str):
                    return False
                if not cs:
                    s, t = s.casefold(), t.casefold()
                if name == "startswith":
                    return s.startswith(t)
                return s.endswith(t) if name == "endswith" else t in s
            case "tolower":
                return args[0].lower() if isinstance(args[0], str) else None
            case "toupper":
                return args[0].upper() if isinstance(args[0], str) else None
            case "trim":
                return args[0].strip() if isinstance(args[0], str) else None
            case "length":
                return len(args[0]) if isinstance(args[0], str) else None
            case "indexof":
                s, t = args
                if not isinstance(s, str) or not isinstance(t, str):
                    return None
                return s.find(t) if cs else s.lower().find(t.lower())
            case "substring":
                s, start = args[0], args[1]
                length = args[2] if len(args) == 3 else None
                if (
                    not isinstance(s, str)
                    or not _is_int(start)
                    or (length is not None and not _is_int(length))
                ):
                    return None
                start = max(start, 0)
                return s[start:] if length is None else s[start : start + max(length, 0)]
            case "concat":
                a, b = args
                return a + b if isinstance(a, str) and isinstance(b, str) else None
            case "date":
                dt = _to_datetime(args[0])
                return None if dt is None else dt.date()
            case "now":
                return datetime.now(UTC)
            case _:  # year / month / day / hour / minute / second
                dt = _to_datetime(args[0])
                return None if dt is None else getattr(dt, name)


def compile_filter(expr: str, *, case_sensitive: bool = False) -> Callable[[Mapping[str, Any]], bool]:
    """Compile ``$filter`` text into a predicate over one record.

    Raises :class:`ODataError` immediately for malformed expressions, never lazily per record.
    """
    ast = parse_filter(expr)
    evaluator = _Evaluator(case_sensitive)

    def predicate(record: Mapping[str, Any]) -> bool:
        try:
            return evaluator.eval(ast, record, {}) is True
        except RecursionError:
            raise ODataError("Invalid filter clause: Expression is too complex.") from None

    return predicate


# --------------------------------------------------------------------------------------------------
# $orderby
# --------------------------------------------------------------------------------------------------


def _sort_key(value: Any, case_sensitive: bool) -> tuple[int, Any]:
    """Total-order key: null < bool < number < datetime < string < other, so mixed columns never raise."""
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, int(value))
    if _is_number(value):
        return (2, value)
    if isinstance(value, (datetime, date)):
        return (3, _to_datetime(value))
    if isinstance(value, str):
        dt = _parse_iso(value)
        if dt is not None:
            return (3, dt)
        return (4, value if case_sensitive else value.casefold())
    return (5, 0)


def apply_orderby(
    records: Iterable[Mapping[str, Any]],
    orderby: str | Sequence[OrderItem],
    *,
    case_sensitive: bool = False,
) -> list[Mapping[str, Any]]:
    """Return records sorted by ``$orderby`` (stable; nulls first ascending, last descending)."""
    items = parse_orderby(orderby) if isinstance(orderby, str) else list(orderby)
    result = list(records)
    evaluator = _Evaluator(case_sensitive)
    # Stable multi-key sort: apply the least significant key first so mixed asc/desc just works.
    for item in reversed(items):
        result.sort(
            key=lambda rec, node=item.expr: _sort_key(evaluator.eval(node, rec, {}), case_sensitive),
            reverse=item.descending,
        )
    return result


# --------------------------------------------------------------------------------------------------
# $select
# --------------------------------------------------------------------------------------------------


def parse_select(value: str) -> list[str] | None:
    """Split ``a, b/c`` into property paths (de-duplicated, order kept); None when blank."""
    paths = [p.strip() for p in value.split(",")]
    seen: dict[str, None] = {p: None for p in paths if p}
    return list(seen) or None


def _build_select_tree(paths: Sequence[str]) -> dict[str, dict[str, Any] | None]:
    """Nested ``{name: subtree | None}`` where None means "keep the whole value"."""
    tree: dict[str, Any] = {}
    for path in paths:
        parts = [p.lower() for p in path.split("/") if p]
        node = tree
        for idx, part in enumerate(parts):
            last = idx == len(parts) - 1
            if last:
                node[part] = None
            elif part in node and node[part] is None:
                break  # the parent is already selected whole
            else:
                node = node.setdefault(part, {})
    return tree


def _project(value: Any, tree: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in tree:
                sub = tree[key.lower()]
                out[key] = copy.deepcopy(item) if sub is None else _project(item, sub)
        return out
    if isinstance(value, list):
        return [_project(item, tree) for item in value]
    return copy.deepcopy(value)


def select_record(
    record: Mapping[str, Any],
    select: Sequence[str] | None,
    *,
    always: Sequence[str] = DEFAULT_SELECT_ALWAYS,
) -> dict[str, Any]:
    """Project one record to ``$select`` (plus ``always`` keys). Returns a deep copy; input is untouched."""
    if not select or "*" in select:
        return {k: copy.deepcopy(v) for k, v in record.items()}
    tree = _build_select_tree(select)
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key in always:
            out[key] = copy.deepcopy(value)
        elif isinstance(key, str) and key.lower() in tree:
            sub = tree[key.lower()]
            out[key] = copy.deepcopy(value) if sub is None else _project(value, sub)
    return out


def apply_select(
    records: Iterable[Mapping[str, Any]],
    select: Sequence[str] | None,
    *,
    always: Sequence[str] = DEFAULT_SELECT_ALWAYS,
) -> list[dict[str, Any]]:
    """:func:`select_record` over many records."""
    return [select_record(r, select, always=always) for r in records]


# --------------------------------------------------------------------------------------------------
# $expand
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpandItem:
    """One ``$expand`` entry: a navigation path plus its (raw, ``$``-prefixed) nested options."""

    path: str
    options: dict[str, str] = field(default_factory=dict, hash=False)

    def parse_options(self) -> QueryOptions:
        """Parse the nested options (``$select``, ``$top``, ``$filter``...) like a top-level query."""
        return parse_query_options(self.options)


def _skip_quoted(text: str, i: int, clause: str) -> int:
    """Return the index just past the quoted run starting at ``text[i]`` (single or double quotes)."""
    quote = text[i]
    j, n = i + 1, len(text)
    while j < n:
        ch = text[j]
        if quote == "'" and ch == "'":
            if j + 1 < n and text[j + 1] == "'":
                j += 2
                continue
            return j + 1
        if quote == '"':
            if ch == "\\":
                j += 2
                continue
            if ch == '"':
                return j + 1
        j += 1
    raise _fail(clause, text, i, "Unterminated string literal")


def _split_top_level(text: str, sep: str, clause: str) -> list[str]:
    """Split on ``sep`` outside quotes and parentheses; unbalanced input is an error."""
    parts: list[str] = []
    depth, start, i, n = 0, 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            i = _skip_quoted(text, i, clause)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise _fail(clause, text, i, "Unbalanced ')'")
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    if depth:
        raise _fail(clause, text, n, "')' expected")
    parts.append(text[start:])
    return parts


def _closing_paren(text: str, open_idx: int, clause: str) -> int:
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            i = _skip_quoted(text, i, clause)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise _fail(clause, text, n, "')' expected")  # pragma: no cover - callers pre-validate balance


def _parse_expand_item(chunk: str, full: str) -> ExpandItem:
    chunk = chunk.strip()
    if not chunk:
        raise _fail("expand", full, 0, "Expression expected")
    open_idx = chunk.find("(")
    if open_idx == -1:
        return ExpandItem(chunk, {})
    path = chunk[:open_idx].strip()
    if not path:
        raise _fail("expand", full, 0, "Navigation property name expected")
    if _closing_paren(chunk, open_idx, "expand") != len(chunk) - 1:
        raise _fail("expand", full, 0, "Unexpected text after ')'")
    options: dict[str, str] = {}
    inner = chunk[open_idx + 1 : -1]
    for part in _split_top_level(inner, ";", "expand"):
        if not part.strip():
            continue
        name, eq, value = part.partition("=")
        name = name.strip().lower()
        if not eq or not name:
            raise _fail("expand", full, 0, f"Expected '$option=value' but found '{part.strip()}'")
        options[name if name.startswith("$") else f"${name}"] = value.strip()
    return ExpandItem(path, options)


@lru_cache(maxsize=256)
def _parse_expand_cached(value: str) -> tuple[ExpandItem, ...]:
    return tuple(_parse_expand_item(c, value) for c in _split_top_level(value, ",", "expand"))


def parse_expand(value: str) -> list[ExpandItem]:
    """Parse ``members($select=id,displayName;$top=5),manager`` into :class:`ExpandItem` entries.

    Evaluating the expansion is the caller's job; nested options stay raw strings (use
    :meth:`ExpandItem.parse_options` to read them).
    """
    if not value.strip():
        return []
    return [ExpandItem(i.path, dict(i.options)) for i in _parse_expand_cached(value)]


# --------------------------------------------------------------------------------------------------
# $search
# --------------------------------------------------------------------------------------------------

_PROP_TERM_RE = re.compile(r"^([A-Za-z_@$][\w@$./]*):(.*)$", re.DOTALL)


@dataclass(frozen=True, slots=True)
class _SearchTerm:
    text: str
    prop: str | None


@dataclass(frozen=True, slots=True)
class _SearchNot:
    operand: _SearchNode


@dataclass(frozen=True, slots=True)
class _SearchGroup:
    op: str
    operands: tuple[_SearchNode, ...]


_SearchNode = _SearchTerm | _SearchNot | _SearchGroup


@dataclass(frozen=True, slots=True)
class _SearchToken:
    kind: str  # TERM, AND, OR, NOT, LP, RP, EOF
    pos: int
    text: str = ""
    prop: str | None = None


def _read_quoted(text: str, start: int) -> tuple[str, int]:
    """Read a double-quoted run (``\\"`` escapes a quote); returns (content, index after closing quote)."""
    out: list[str] = []
    i, n = start + 1, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise _fail("search", text, start, "Unterminated quoted term")


def _split_prop(content: str) -> tuple[str | None, str]:
    m = _PROP_TERM_RE.match(content)
    return (m.group(1), m.group(2)) if m else (None, content)


def _search_tokens(text: str) -> list[_SearchToken]:
    tokens: list[_SearchToken] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == "(":
            tokens.append(_SearchToken("LP", i))
            i += 1
        elif ch == ")":
            tokens.append(_SearchToken("RP", i))
            i += 1
        elif ch == '"':
            content, end = _read_quoted(text, i)
            prop, term = _split_prop(content)
            tokens.append(_SearchToken("TERM", i, term.strip(), prop))
            i = end
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            word = text[i:j]
            if word.endswith(":") and j < n and text[j] == '"':  # property:"quoted phrase"
                content, end = _read_quoted(text, j)
                tokens.append(_SearchToken("TERM", i, content.strip(), word[:-1]))
                i = end
                continue
            if word in ("AND", "OR", "NOT"):
                tokens.append(_SearchToken(word, i))
            else:
                prop, term = _split_prop(word)
                tokens.append(_SearchToken("TERM", i, term, prop))
            i = j
    tokens.append(_SearchToken("EOF", n))
    return tokens


class _SearchParser:
    """``or_expr := and_expr (OR and_expr)*``; adjacent terms are an implicit AND (KQL style)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _search_tokens(text)
        self.i = 0
        self.depth = 0

    def _peek(self) -> _SearchToken:
        return self.tokens[self.i]

    def _next(self) -> _SearchToken:
        tok = self.tokens[self.i]
        if tok.kind != "EOF":
            self.i += 1
        return tok

    def parse(self) -> _SearchNode:
        if self._peek().kind == "EOF":
            raise _fail("search", self.text, 0, "Expression expected")
        node = self._or()
        tok = self._peek()
        if tok.kind != "EOF":
            raise _fail("search", self.text, tok.pos, "Syntax error")
        return node

    def _or(self) -> _SearchNode:
        operands = [self._and()]
        while self._peek().kind == "OR":
            self._next()
            operands.append(self._and())
        return operands[0] if len(operands) == 1 else _SearchGroup("or", tuple(operands))

    def _and(self) -> _SearchNode:
        operands = [self._unary()]
        while True:
            kind = self._peek().kind
            if kind == "AND":
                self._next()
                operands.append(self._unary())
            elif kind in ("TERM", "LP", "NOT"):
                operands.append(self._unary())
            else:
                break
        return operands[0] if len(operands) == 1 else _SearchGroup("and", tuple(operands))

    def _unary(self) -> _SearchNode:
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise _fail("search", self.text, self._peek().pos, "Expression is nested too deeply")
        try:
            tok = self._next()
            if tok.kind == "NOT":
                return _SearchNot(self._unary())
            if tok.kind == "LP":
                inner = self._or()
                close = self._next()
                if close.kind != "RP":
                    raise _fail("search", self.text, close.pos, "')' expected")
                return inner
            if tok.kind == "TERM":
                if not tok.text:
                    raise _fail("search", self.text, tok.pos, "Empty search term")
                return _SearchTerm(tok.text, tok.prop)
            raise _fail("search", self.text, tok.pos, "Search term expected")
        finally:
            self.depth -= 1


@lru_cache(maxsize=256)
def _parse_search_cached(expr: str) -> _SearchNode:
    try:
        return _SearchParser(expr).parse()
    except RecursionError:
        raise ODataError("Invalid search clause: Expression is too complex.") from None


def _walk_strings(value: Any, segments: tuple[str, ...]) -> Iterator[str]:
    """Yield every string reachable at ``segments``, fanning out through lists and nested objects."""
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item, segments)
    elif segments:
        if isinstance(value, Mapping):
            sub = _get_member(value, segments[0])
            if sub is not _MISSING:
                yield from _walk_strings(sub, segments[1:])
    elif isinstance(value, str):
        yield value
    elif _is_number(value):
        yield str(value)
    elif isinstance(value, Mapping):
        for sub in value.values():
            yield from _walk_strings(sub, ())


def _build_search_predicate(node: _SearchNode, fields: Sequence[str]) -> Callable[[Mapping[str, Any]], bool]:
    if isinstance(node, _SearchTerm):
        needle = node.text.casefold()
        paths = [tuple(node.prop.split("/"))] if node.prop else [tuple(f.split("/")) for f in fields]

        def term(record: Mapping[str, Any]) -> bool:
            return any(needle in s.casefold() for p in paths for s in _walk_strings(record, p))

        return term
    if isinstance(node, _SearchNot):
        inner = _build_search_predicate(node.operand, fields)
        return lambda record: not inner(record)
    parts = [_build_search_predicate(o, fields) for o in node.operands]
    if node.op == "and":
        return lambda record: all(p(record) for p in parts)
    return lambda record: any(p(record) for p in parts)


def compile_search(
    expr: str, search_fields: Sequence[str] = ("displayName",)
) -> Callable[[Mapping[str, Any]], bool]:
    """Compile Graph ``$search`` text into a predicate.

    Supports ``"term"``, ``"property:term"``, ``AND``/``OR``/``NOT`` (upper case, as in KQL), parentheses
    and implicit AND between adjacent terms. Matching is a case-insensitive substring test over
    ``search_fields`` (property paths such as ``from/emailAddress/address``); ``property:term``
    restricts the test to that property. Raises :class:`ODataError` on malformed input.
    """
    return _build_search_predicate(_parse_search_cached(expr), tuple(search_fields))


# --------------------------------------------------------------------------------------------------
# High-level API
# --------------------------------------------------------------------------------------------------


@dataclass
class QueryOptions:
    """Parsed Graph query options. ``orderby``/``filter``/``search`` stay as validated source text."""

    filter: str | None = None
    orderby: str | None = None
    select: list[str] | None = None
    expand: list[ExpandItem] = field(default_factory=list)
    top: int | None = None
    skip: int = 0
    count: bool = False
    search: str | None = None
    skiptoken: str | None = None


@dataclass
class QueryResult:
    """``items`` is the projected page; ``total`` counts matches after filter/search, before paging."""

    items: list[dict[str, Any]]
    total: int


_KNOWN_OPTIONS = frozenset(
    {"filter", "orderby", "select", "expand", "top", "skip", "count", "search", "skiptoken"}
)
_NON_NEGATIVE_INT = re.compile(r"[0-9]+")


def _as_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):  # multi-dict style callers: last occurrence wins
        value = value[-1] if value else ""
    return str(value)


def _non_negative_int(option: str, raw: str) -> int:
    text = raw.strip()
    if not _NON_NEGATIVE_INT.fullmatch(text):
        raise ODataError(
            f"Invalid value '{raw}' for {option} query option. "
            f"The {option} query option requires a non-negative integer value."
        )
    return int(text)


def parse_query_options(params: Mapping[str, str]) -> QueryOptions:
    """Parse raw query parameters (``$top`` or ``top``, any case) into :class:`QueryOptions`.

    Syntax is validated eagerly so a bad ``$filter`` yields 400 even when the collection is empty.
    Unknown parameters are ignored, as Graph does for non-OData parameters.
    """
    raw: dict[str, str] = {}
    for key, value in params.items():
        name = str(key).strip().lower().removeprefix("$")
        if name in _KNOWN_OPTIONS:
            raw[name] = _as_text(value)

    opts = QueryOptions()
    if filter_text := raw.get("filter", "").strip():
        parse_filter(filter_text)
        opts.filter = filter_text
    if orderby_text := raw.get("orderby", "").strip():
        parse_orderby(orderby_text)
        opts.orderby = orderby_text
    if "select" in raw:
        opts.select = parse_select(raw["select"])
    if "expand" in raw:
        opts.expand = parse_expand(raw["expand"])
    if "top" in raw:
        opts.top = _non_negative_int("$top", raw["top"])
    if "skip" in raw:
        opts.skip = _non_negative_int("$skip", raw["skip"])
    if "count" in raw:
        flag = raw["count"].strip().lower()
        if flag not in ("true", "false"):
            raise ODataError(
                f"Invalid value '{raw['count']}' for $count query option. Expected 'true' or 'false'."
            )
        opts.count = flag == "true"
    if search_text := raw.get("search", "").strip():
        _parse_search_cached(search_text)
        opts.search = search_text
    if token := raw.get("skiptoken", "").strip():
        opts.skiptoken = token
    return opts


def apply_query(
    records: Iterable[Mapping[str, Any]],
    options: QueryOptions,
    *,
    search_fields: Sequence[str] = ("displayName",),
    select_always: Sequence[str] = ("id",),
    case_sensitive: bool = False,
) -> QueryResult:
    """Apply filter, search, orderby, then skip/top and select, in Graph's order.

    ``total`` is the number of matches before paging. Input records are never mutated and the returned
    items are deep copies, so callers may freely annotate them (``@odata.etag``, expansions...).
    ``$expand`` is not evaluated here; read ``options.expand`` and resolve it per item.
    """
    items: list[Mapping[str, Any]] = list(records)
    if options.filter:
        keep = compile_filter(options.filter, case_sensitive=case_sensitive)
        items = [r for r in items if keep(r)]
    if options.search:
        match = compile_search(options.search, search_fields)
        items = [r for r in items if match(r)]
    if options.orderby:
        items = apply_orderby(items, options.orderby, case_sensitive=case_sensitive)
    total = len(items)
    start = max(options.skip, 0)
    page = items[start:] if options.top is None else items[start : start + max(options.top, 0)]
    return QueryResult(apply_select(page, options.select, always=select_always), total)
