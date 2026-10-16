"""Two small query languages: Box search (``AND``/``OR``/``NOT``, quoted phrases) and the
SQL-ish metadata query (``clientName = :name AND fiscalYear >= :year``)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from emulators.providers.box import errors

Predicate = Callable[[dict[str, Any]], bool]

# -- search -------------------------------------------------------------------------------
_SEARCH_TOKEN = re.compile(r'"([^"]*)"|(\S+)')


def parse_search(query: str) -> Callable[[Callable[[str, bool], bool]], bool]:
    """Parse a Box search ``query`` into a function of a ``matches(term, is_phrase)`` oracle."""
    tokens: list[tuple[str, str]] = []
    for match in _SEARCH_TOKEN.finditer(query):
        if match.group(1) is not None:
            tokens.append(("phrase", match.group(1).lower()))
        else:
            word = match.group(2)
            if word in ("AND", "OR", "NOT"):
                tokens.append(("op", word))
            else:
                tokens.append(("term", word.lower().strip("*")))
    pos = 0

    def peek() -> tuple[str, str] | None:
        return tokens[pos] if pos < len(tokens) else None

    def parse_or() -> Callable[[Callable[[str, bool], bool]], bool]:
        # Box reads multi-word queries as OR unless an explicit AND joins the terms.
        nonlocal pos
        parts = [parse_and()]
        while peek() is not None:
            if peek() == ("op", "OR"):
                pos += 1
            parts.append(parse_and())
        return lambda m: any(part(m) for part in parts)

    def parse_and() -> Callable[[Callable[[str, bool], bool]], bool]:
        nonlocal pos
        parts = [parse_not()]
        while peek() in (("op", "AND"), ("op", "NOT")):
            if peek() == ("op", "AND"):
                pos += 1
            parts.append(parse_not())  # ``a NOT b`` reads as ``a AND NOT b``
        return lambda m: all(part(m) for part in parts)

    def parse_not() -> Callable[[Callable[[str, bool], bool]], bool]:
        nonlocal pos
        token = peek()
        if token == ("op", "NOT"):
            pos += 1
            inner = parse_not()
            return lambda m: not inner(m)
        if token is None:
            return lambda m: True
        pos += 1
        kind, text = token
        if kind == "op":  # stray AND/OR: treat as a literal word
            return lambda m: m(text.lower(), False)
        return lambda m: m(text, kind == "phrase")

    return parse_or()


def text_matcher(text: str) -> Callable[[str, bool], bool]:
    lowered = text.lower()

    def match(term: str, is_phrase: bool) -> bool:
        if not term:
            return True
        if is_phrase:
            return term in lowered
        return re.search(r"(?<![a-z0-9])" + re.escape(term), lowered) is not None

    return match


# -- metadata queries ----------------------------------------------------------------------
_MD_TOKEN = re.compile(
    r"\s*(?:(?P<param>:[A-Za-z_]\w*)|(?P<str>'(?:[^'\\]|\\.)*')|(?P<num>-?\d+(?:\.\d+)?)"
    r"|(?P<op><=|>=|<>|!=|=|<|>)|(?P<paren>[()])|(?P<word>[A-Za-z_]\w*))"
)


def tokenize_md(query: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(query):
        match = _MD_TOKEN.match(query, pos)
        if not match or match.end() == pos:
            if query[pos:].strip() == "":
                break
            raise errors.bad_request(
                f"Invalid query near '{query[pos : pos + 12].strip()}'",
                reason="invalid_parameter",
                name="query",
            )
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
        pos = match.end()
    return tokens


def like_match(text: str, pattern: str) -> bool:
    """SQL ``LIKE`` (``%`` any run, ``_`` one character, case-insensitive) in O(len(text) * len(pattern)).

    A regular expression would backtrack catastrophically on patterns such as ``%%%%%%%%x``.
    """
    text, pattern = text.lower(), pattern.lower()
    t = p = 0
    star = -1
    mark = 0
    while t < len(text):
        if p < len(pattern) and pattern[p] in ("_", text[t]) and pattern[p] != "%":
            t += 1
            p += 1
        elif p < len(pattern) and pattern[p] == "%":
            star, mark = p, t
            p += 1
        elif star != -1:
            p = star + 1
            mark += 1
            t = mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "%":
        p += 1
    return p == len(pattern)


def parse_metadata_query(query: str, params: dict[str, Any]) -> Predicate:
    """Compile ``query`` into a predicate over the instance's field values."""
    tokens = tokenize_md(query)
    pos = 0

    def peek() -> tuple[str, str] | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> tuple[str, str]:
        nonlocal pos
        token = peek()
        if token is None:
            raise errors.bad_request("Unexpected end of query", reason="invalid_parameter", name="query")
        pos += 1
        return token

    def keyword(token: tuple[str, str] | None, word: str) -> bool:
        return token is not None and token[0] == "word" and token[1].upper() == word

    def parse_or() -> Predicate:
        parts = [parse_and()]
        while keyword(peek(), "OR"):
            take()
            parts.append(parse_and())
        return lambda v: any(part(v) for part in parts)

    def parse_and() -> Predicate:
        parts = [parse_atom()]
        while keyword(peek(), "AND"):
            take()
            parts.append(parse_atom())
        return lambda v: all(part(v) for part in parts)

    def operand() -> Callable[[dict[str, Any]], Any]:
        kind, text = take()
        if kind == "param":
            name = text[1:]
            if name not in params:
                raise errors.bad_request(
                    f"Missing query parameter '{name}'", reason="missing_parameter", name="query_params"
                )
            value = params[name]
            return lambda v: value
        if kind == "str":
            literal = text[1:-1].replace("\\'", "'")
            return lambda v: literal
        if kind == "num":
            number = float(text) if "." in text else int(text)
            return lambda v: number
        if kind == "word":
            return lambda v: v.get(text)
        raise errors.bad_request("Invalid operand in query", reason="invalid_parameter", name="query")

    def parse_atom() -> Predicate:
        token = peek()
        if token == ("paren", "("):
            take()
            inner = parse_or()
            if take() != ("paren", ")"):
                raise errors.bad_request(
                    "Unbalanced parentheses in query", reason="invalid_parameter", name="query"
                )
            return inner
        if keyword(token, "NOT"):
            take()
            inner = parse_atom()
            return lambda v: not inner(v)
        left = operand()
        op_token = take()
        if op_token[0] == "word" and op_token[1].upper() == "LIKE":
            right = operand()

            def like(values: dict[str, Any]) -> bool:
                return left(values) is not None and like_match(str(left(values)), str(right(values)))

            return like
        if op_token[0] != "op":
            raise errors.bad_request(
                f"Unexpected '{op_token[1]}' in query", reason="invalid_parameter", name="query"
            )
        right = operand()
        op = op_token[1]

        def compare(values: dict[str, Any]) -> bool:
            a, b = left(values), right(values)
            if a is None:
                return False
            try:
                return {
                    "=": a == b, "!=": a != b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b,
                }[op]  # fmt: skip
            except TypeError:
                return False

        return compare

    predicate = parse_or()
    if peek() is not None:
        raise errors.bad_request(
            f"Unexpected '{peek()[1]}' in query", reason="invalid_parameter", name="query"
        )  # type: ignore[index]
    return predicate
