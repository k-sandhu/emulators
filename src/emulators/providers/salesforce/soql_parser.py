"""A real SOQL tokenizer and recursive-descent parser.

Produces the AST consumed by :mod:`soql_exec`. Syntax errors raise :class:`SfError` with the
exact message layout Salesforce uses (query line, caret under the offending token,
``ERROR at Row:R:Column:C``) and ``MALFORMED_QUERY`` as the error code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from emulators.providers.salesforce.common import SfError

KEYWORDS = {
    "SELECT",
    "FROM",
    "WHERE",
    "GROUP",
    "ORDER",
    "BY",
    "HAVING",
    "LIMIT",
    "OFFSET",
    "USING",
    "SCOPE",
    "WITH",
    "FOR",
    "ALL",
    "ROWS",
    "AND",
    "OR",
    "NOT",
    "IN",
    "LIKE",
    "INCLUDES",
    "EXCLUDES",
    "ASC",
    "DESC",
    "NULLS",
    "FIRST",
    "LAST",
    "TYPEOF",
    "WHEN",
    "THEN",
    "ELSE",
    "END",
    "UPDATE",
    "VIEW",
    "REFERENCE",
}
AGGREGATES = {"COUNT", "COUNT_DISTINCT", "SUM", "AVG", "MIN", "MAX"}
DATE_FUNCTIONS = {
    "CALENDAR_MONTH",
    "CALENDAR_QUARTER",
    "CALENDAR_YEAR",
    "DAY_IN_MONTH",
    "DAY_IN_WEEK",
    "DAY_IN_YEAR",
    "DAY_ONLY",
    "FISCAL_MONTH",
    "FISCAL_QUARTER",
    "FISCAL_YEAR",
    "HOUR_IN_DAY",
    "WEEK_IN_MONTH",
    "WEEK_IN_YEAR",
}
OTHER_FUNCTIONS = {"TOLABEL", "FORMAT", "CONVERTCURRENCY", "CONVERTTIMEZONE", "FIELDS"}
DATE_LITERALS = {
    "YESTERDAY",
    "TODAY",
    "TOMORROW",
    "LAST_WEEK",
    "THIS_WEEK",
    "NEXT_WEEK",
    "LAST_MONTH",
    "THIS_MONTH",
    "NEXT_MONTH",
    "LAST_90_DAYS",
    "NEXT_90_DAYS",
    "THIS_QUARTER",
    "LAST_QUARTER",
    "NEXT_QUARTER",
    "THIS_YEAR",
    "LAST_YEAR",
    "NEXT_YEAR",
    "THIS_FISCAL_QUARTER",
    "LAST_FISCAL_QUARTER",
    "NEXT_FISCAL_QUARTER",
    "THIS_FISCAL_YEAR",
    "LAST_FISCAL_YEAR",
    "NEXT_FISCAL_YEAR",
}
DATE_LITERALS_N = {
    "LAST_N_DAYS",
    "NEXT_N_DAYS",
    "N_DAYS_AGO",
    "LAST_N_WEEKS",
    "NEXT_N_WEEKS",
    "N_WEEKS_AGO",
    "LAST_N_MONTHS",
    "NEXT_N_MONTHS",
    "N_MONTHS_AGO",
    "LAST_N_QUARTERS",
    "NEXT_N_QUARTERS",
    "N_QUARTERS_AGO",
    "LAST_N_YEARS",
    "NEXT_N_YEARS",
    "N_YEARS_AGO",
    "LAST_N_FISCAL_QUARTERS",
    "NEXT_N_FISCAL_QUARTERS",
    "N_FISCAL_QUARTERS_AGO",
    "LAST_N_FISCAL_YEARS",
    "NEXT_N_FISCAL_YEARS",
    "N_FISCAL_YEARS_AGO",
}
INVALID_FIELD_HINT = (
    "If you are attempting to use a custom field, be sure to append the '__c' after the custom field name. "
    "Please reference your WSDL or the describe call for the appropriate names."
)
INVALID_TYPE_HINT = (
    "If you are attempting to use a custom object, be sure to append the '__c' after the entity name. "
    "Please reference your WSDL or the describe call for the appropriate names."
)


@dataclass
class Token:
    kind: str  # ident number string date datetime op punct eof
    value: Any
    text: str
    pos: int
    row: int
    col: int

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind == "ident" else ""


def query_error(query: str, tok: Token | None, message: str, code: str = "MALFORMED_QUERY") -> SfError:
    """``[{"message": "\\nSELECT ...\\n   ^\\nERROR at Row:1:Column:N\\n<message>", "errorCode": code}]``."""
    if tok is None:
        return SfError(400, code, message)
    lines = query.split("\n")
    line = lines[tok.row - 1] if 0 < tok.row <= len(lines) else query
    caret = " " * (tok.col - 1) + "^"
    return SfError(400, code, f"\n{line}\n{caret}\nERROR at Row:{tok.row}:Column:{tok.col}\n{message}")


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<datetime>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)
  | (?P<date>\d{4}-\d{2}-\d{2})(?![\d])
  | (?P<number>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|!=|<>|=|<|>)
  | (?P<punct>[(),.:+\-*])
    """,
    re.VERBOSE,
)
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", '"': '"', "'": "'", "\\": "\\"}


def _valid_date_literal(text: str) -> bool:
    try:
        date.fromisoformat(text[:10])
    except ValueError:
        return False
    if len(text) == 10:
        return True
    hour, minute, second = int(text[11:13]), int(text[14:16]), int(text[17:19])
    return hour < 24 and minute < 60 and second < 60


def tokenize(query: str) -> list[Token]:
    tokens: list[Token] = []
    pos, row, line_start = 0, 1, 0
    n = len(query)
    while pos < n:
        ch = query[pos]
        col = pos - line_start + 1
        if ch == "'":
            end, buf = pos + 1, []
            while end < n and query[end] != "'":
                if query[end] == "\\" and end + 1 < n:
                    nxt = query[end + 1]
                    # \% and \_ stay as escapes so LIKE can treat them literally
                    key = nxt.lower() if nxt in "NRTBF" else nxt
                    buf.append(_ESCAPES[key] if key in _ESCAPES else "\\" + nxt)
                    end += 2
                    continue
                buf.append(query[end])
                end += 1
            if end >= n:
                raise query_error(
                    query, Token("string", None, query[pos:], pos, row, col), "unexpected token: '<EOF>'"
                )
            tokens.append(Token("string", "".join(buf), query[pos : end + 1], pos, row, col))
            pos = end + 1
            continue
        match = _TOKEN_RE.match(query, pos)
        if not match:
            raise query_error(query, Token("punct", ch, ch, pos, row, col), f"unexpected token: '{ch}'")
        kind = match.lastgroup or "ws"
        text = match.group()
        if kind == "ws":
            newlines = text.count("\n")
            if newlines:
                row += newlines
                line_start = pos + text.rfind("\n") + 1
        else:
            value: Any = text
            if kind in ("date", "datetime") and not _valid_date_literal(text):
                raise query_error(
                    query, Token(kind, text, text, pos, row, col), f"Invalid {kind} literal: '{text}'"
                )
            if kind == "number":
                value = float(text) if any(c in text for c in ".eE") else int(text)
            tokens.append(Token(kind, value, text, pos, row, col))
        pos = match.end()
    tokens.append(Token("eof", None, "<EOF>", n, row, n - line_start + 1))
    return tokens


# -- AST ---------------------------------------------------------------------------------


@dataclass
class Ref:
    parts: list[str]
    tok: Token

    @property
    def dotted(self) -> str:
        return ".".join(self.parts)


@dataclass
class Lit:
    kind: str  # string number bool null date datetime daterel
    value: Any
    tok: Token
    n: int | None = None


@dataclass
class Func:
    name: str  # upper-cased
    args: list[Any]
    tok: Token
    alias: str | None = None
    star: bool = False  # COUNT()


@dataclass
class TypeOf:
    ref: Ref
    whens: list[tuple[str, list[Ref]]]
    else_fields: list[Ref] | None
    tok: Token


@dataclass
class SubQuery:
    query: Query
    tok: Token
    alias: str | None = None


@dataclass
class Cmp:
    left: Any
    op: str
    right: Any  # Lit | list[Lit] | SubQuery
    tok: Token


@dataclass
class Logic:
    op: str
    items: list[Any]


@dataclass
class Not:
    item: Any


@dataclass
class OrderItem:
    expr: Any
    desc: bool = False
    nulls_first: bool | None = None


@dataclass
class Query:
    text: str
    select: list[Any] = field(default_factory=list)
    from_name: str = ""
    from_tok: Token | None = None
    alias: str | None = None
    scope: str | None = None
    where: Any = None
    security: str | None = None
    group_by: list[Any] = field(default_factory=list)
    group_modifier: str | None = None
    having: Any = None
    order_by: list[OrderItem] = field(default_factory=list)
    limit: int | None = None
    offset: int | None = None
    for_clauses: list[str] = field(default_factory=list)
    all_rows: bool = False


# -- parser ------------------------------------------------------------------------------

MAX_NESTING = 100
SCOPES = {
    "everything",
    "mine",
    "team",
    "mineandmygroups",
    "delegated",
    "my_territory",
    "my_team_territory",
    "scopingrule",
}


class Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.i = 0
        self.depth = 0

    # -- primitives
    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def peek(self, offset: int = 1) -> Token:
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        tok = self.tokens[self.i]
        if tok.kind != "eof":
            self.i += 1
        return tok

    def error(self, tok: Token | None = None, message: str | None = None) -> SfError:
        tok = tok or self.tok
        return query_error(self.text, tok, message or f"unexpected token: '{tok.text}'")

    def is_kw(self, *words: str) -> bool:
        return self.tok.kind == "ident" and self.tok.upper in words

    def accept_kw(self, *words: str) -> Token | None:
        if self.is_kw(*words):
            return self.advance()
        return None

    def expect_kw(self, word: str) -> Token:
        if not self.is_kw(word):
            raise self.error()
        return self.advance()

    def is_punct(self, ch: str) -> bool:
        return self.tok.kind == "punct" and self.tok.text == ch

    def accept_punct(self, ch: str) -> Token | None:
        return self.advance() if self.is_punct(ch) else None

    def expect_punct(self, ch: str) -> Token:
        if not self.is_punct(ch):
            raise self.error()
        return self.advance()

    # -- top level
    def parse(self) -> Query:
        query = self.parse_query()
        if self.tok.kind != "eof":
            raise self.error()
        return query

    def parse_query(self) -> Query:
        self.depth += 1
        try:
            if self.depth > MAX_NESTING:
                raise self.error(message="Query is too complex: subqueries are nested too deeply")
            return self._parse_query()
        finally:
            self.depth -= 1

    def _parse_query(self) -> Query:
        q = Query(text=self.text)
        self.expect_kw("SELECT")
        q.select = self.parse_select_list()
        self.expect_kw("FROM")
        if self.tok.kind != "ident":
            raise self.error()
        q.from_tok = self.advance()
        q.from_name = q.from_tok.text
        # a relationship path (FROM Account.Contacts) is not valid SOQL here but keep the dotted form
        while self.is_punct("."):
            self.advance()
            q.from_name += "." + self.advance().text
        if self.tok.kind == "ident" and self.tok.upper not in KEYWORDS:
            alias_tok = self.advance()
            q.alias = alias_tok.text
        if self.is_kw("USING"):
            self.advance()
            self.expect_kw("SCOPE")
            scope_tok = self.tok
            if scope_tok.kind != "ident" or scope_tok.text.lower() not in SCOPES:
                raise self.error(scope_tok)
            self.advance()
            q.scope = scope_tok.text.lower()
        if self.accept_kw("WHERE"):
            q.where = self.parse_condition()
        if self.is_kw("WITH"):
            self.advance()
            word = self.advance()
            if word.upper in ("SECURITY_ENFORCED", "USER_MODE", "SYSTEM_MODE"):
                q.security = word.upper
            else:
                raise self.error(word, f"WITH {word.text} is not supported by this endpoint")
        if self.is_kw("GROUP"):
            self.advance()
            self.expect_kw("BY")
            if self.is_kw("ROLLUP", "CUBE") and self.peek().text == "(":
                q.group_modifier = self.advance().upper
                self.expect_punct("(")
                q.group_by = self.parse_expr_list()
                self.expect_punct(")")
            else:
                q.group_by = self.parse_expr_list()
            if self.accept_kw("HAVING"):
                q.having = self.parse_condition()
        if self.is_kw("ORDER"):
            self.advance()
            self.expect_kw("BY")
            q.order_by = self.parse_order_list()
        if self.accept_kw("LIMIT"):
            q.limit = self.parse_int()
        if self.accept_kw("OFFSET"):
            q.offset = self.parse_int()
        while self.is_kw("FOR", "UPDATE", "ALL"):
            if self.accept_kw("ALL"):
                self.expect_kw("ROWS")
                q.all_rows = True
            elif self.accept_kw("FOR"):
                q.for_clauses.append(self.advance().upper)
            else:
                self.advance()
                q.for_clauses.append(self.advance().upper)
        return q

    def parse_int(self) -> int:
        tok = self.tok
        if tok.kind != "number" or not isinstance(tok.value, int):
            raise self.error()
        self.advance()
        return int(tok.value)

    # -- SELECT list
    def parse_select_list(self) -> list[Any]:
        items = [self.parse_select_item()]
        while self.accept_punct(","):
            items.append(self.parse_select_item())
        return items

    def parse_select_item(self) -> Any:
        tok = self.tok
        if self.is_punct("("):
            self.advance()
            sub_tok = self.tok
            if not self.is_kw("SELECT"):
                raise self.error()
            sub = self.parse_query()
            self.expect_punct(")")
            alias = None
            if self.tok.kind == "ident" and self.tok.upper not in KEYWORDS:
                alias = self.advance().text
            return SubQuery(sub, sub_tok, alias)
        if tok.kind != "ident":
            raise self.error()
        if tok.upper == "TYPEOF":
            return self.parse_typeof()
        expr = self.parse_expr()
        if isinstance(expr, Func) and self.tok.kind == "ident" and self.tok.upper not in KEYWORDS:
            expr.alias = self.advance().text
        return expr

    def parse_typeof(self) -> TypeOf:
        tok = self.advance()
        ref = self.parse_ref()
        whens: list[tuple[str, list[Ref]]] = []
        else_fields: list[Ref] | None = None
        while self.accept_kw("WHEN"):
            obj = self.advance().text
            self.expect_kw("THEN")
            fields = [self.parse_ref()]
            while self.accept_punct(","):
                fields.append(self.parse_ref())
            whens.append((obj, fields))
        if self.accept_kw("ELSE"):
            else_fields = [self.parse_ref()]
            while self.accept_punct(","):
                else_fields.append(self.parse_ref())
        self.expect_kw("END")
        return TypeOf(ref, whens, else_fields, tok)

    def parse_ref(self) -> Ref:
        tok = self.tok
        if tok.kind != "ident":
            raise self.error()
        parts = [self.advance().text]
        while self.is_punct(".") and self.peek().kind == "ident":
            self.advance()
            parts.append(self.advance().text)
        return Ref(parts, tok)

    def parse_expr(self) -> Any:
        """A field path or a function call (aggregate, date function, toLabel...)."""
        tok = self.tok
        if tok.kind != "ident":
            raise self.error()
        if self.peek().kind == "punct" and self.peek().text == "(" and tok.upper not in KEYWORDS:
            return self.parse_func()
        return self.parse_ref()

    def parse_func(self) -> Func:
        name_tok = self.advance()
        name = name_tok.upper
        self.expect_punct("(")
        func = Func(name, [], name_tok)
        if self.is_punct(")"):
            func.star = True
        else:
            if self.is_punct("*"):
                self.advance()
                func.star = True
            else:
                func.args.append(self.parse_func_arg())
                while self.accept_punct(","):
                    func.args.append(self.parse_func_arg())
        self.expect_punct(")")
        return func

    def parse_func_arg(self) -> Any:
        tok = self.tok
        if tok.kind == "ident":
            return self.parse_expr()
        return self.parse_literal()

    def parse_expr_list(self) -> list[Any]:
        items = [self.parse_expr()]
        while self.accept_punct(","):
            items.append(self.parse_expr())
        return items

    def parse_order_list(self) -> list[OrderItem]:
        items: list[OrderItem] = []
        while True:
            item = OrderItem(self.parse_expr())
            if self.accept_kw("ASC"):
                item.desc = False
            elif self.accept_kw("DESC"):
                item.desc = True
            if self.accept_kw("NULLS"):
                word = self.advance()
                if word.upper not in ("FIRST", "LAST"):
                    raise self.error(word)
                item.nulls_first = word.upper == "FIRST"
            items.append(item)
            if not self.accept_punct(","):
                return items

    # -- conditions
    def parse_condition(self) -> Any:
        """``a AND b AND c`` or ``a OR b`` (mixing without parentheses is a syntax error, as in SOQL)."""
        first = self.parse_term()
        op: str | None = None
        items = [first]
        while self.is_kw("AND", "OR"):
            tok = self.tok
            word = tok.upper
            if op is not None and word != op:
                raise self.error(tok)
            op = word
            self.advance()
            items.append(self.parse_term())
        return items[0] if op is None else Logic(op, items)

    def parse_term(self) -> Any:
        self.depth += 1
        try:
            if self.depth > MAX_NESTING:
                raise self.error(message="Query is too complex: conditions are nested too deeply")
            if self.accept_kw("NOT"):
                return Not(self.parse_term())
            if self.is_punct("("):
                self.advance()
                inner = self.parse_condition()
                self.expect_punct(")")
                return inner
            return self.parse_comparison()
        finally:
            self.depth -= 1

    def parse_comparison(self) -> Any:
        left = self.parse_expr()
        tok = self.tok
        if tok.kind == "op":
            self.advance()
            return Cmp(left, "<>" if tok.text == "!=" else tok.text, self.parse_value(), tok)
        if tok.kind == "ident":
            word = tok.upper
            if word == "LIKE":
                self.advance()
                return Cmp(left, "LIKE", self.parse_value(), tok)
            if word == "NOT" and self.peek().kind == "ident" and self.peek().upper == "IN":
                self.advance()
                self.advance()
                return Cmp(left, "NOT IN", self.parse_in_list(), tok)
            if word == "NOT" and self.peek().kind == "ident" and self.peek().upper == "LIKE":
                raise self.error(self.peek())
            if word == "IN":
                self.advance()
                return Cmp(left, "IN", self.parse_in_list(), tok)
            if word in ("INCLUDES", "EXCLUDES"):
                self.advance()
                return Cmp(left, word, self.parse_in_list(), tok)
        raise self.error()

    def parse_in_list(self) -> Any:
        self.expect_punct("(")
        if self.is_kw("SELECT"):
            tok = self.tok
            sub = self.parse_query()
            self.expect_punct(")")
            return SubQuery(sub, tok)
        values = [self.parse_literal()]
        while self.accept_punct(","):
            values.append(self.parse_literal())
        self.expect_punct(")")
        return values

    def parse_value(self) -> Any:
        return self.parse_literal()

    def parse_literal(self) -> Lit:
        tok = self.tok
        sign = 1
        if tok.kind == "punct" and tok.text in "+-" and self.peek().kind == "number":
            sign = -1 if tok.text == "-" else 1
            self.advance()
            num = self.advance()
            return Lit("number", sign * num.value, num)
        if tok.kind == "number":
            self.advance()
            return Lit("number", tok.value, tok)
        if tok.kind == "string":
            self.advance()
            return Lit("string", tok.value, tok)
        if tok.kind in ("date", "datetime"):
            self.advance()
            return Lit(tok.kind, tok.text, tok)
        if tok.kind == "ident":
            word = tok.upper
            if word in ("TRUE", "FALSE"):
                self.advance()
                return Lit("bool", word == "TRUE", tok)
            if word == "NULL":
                self.advance()
                return Lit("null", None, tok)
            if word in DATE_LITERALS:
                self.advance()
                return Lit("daterel", word, tok)
            if word in DATE_LITERALS_N:
                self.advance()
                self.expect_punct(":")
                num = self.tok
                if num.kind != "number" or not isinstance(num.value, int):
                    raise self.error()
                self.advance()
                return Lit("daterel", word, tok, n=int(num.value))
        if tok.kind == "punct" and tok.text == ":":
            raise self.error(tok, "Bind variables only allowed in Apex code")
        raise self.error()


def parse(text: str) -> Query:
    return Parser(text).parse()
