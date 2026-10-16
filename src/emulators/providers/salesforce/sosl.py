"""SOSL (``FIND {...} IN ... RETURNING ...``) and parameterized search.

Matching is token based and case-insensitive, with ``*``/``?`` wildcards, quoted phrases and
the ``AND`` / ``OR`` / ``AND NOT`` operators (juxtaposed terms are OR-ed, as in SOSL). The
RETURNING field lists, WHERE, ORDER BY and LIMIT are delegated to the SOQL engine, restricted
to the ids that matched, so sharing and field validation behave exactly like queries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from emulators.providers.salesforce.common import SfError
from emulators.providers.salesforce.org import Record
from emulators.providers.salesforce.schema import SCHEMA, SObject
from emulators.providers.salesforce.soql_exec import QueryEngine
from emulators.providers.salesforce.soql_parser import tokenize

DEFAULT_OBJECTS = [
    "Account",
    "Contact",
    "Lead",
    "Opportunity",
    "Case",
    "Campaign",
    "Product2",
    "Engagement__c",
    "User",
]
NAME_FIELDS = {"name", "firstname", "lastname", "company", "subject", "casenumber", "alias", "username"}
MAX_RESULTS = 2000


def search_error(message: str) -> SfError:
    return SfError(400, "INVALID_SEARCH", message)


@dataclass
class Returning:
    obj: str
    fields: str = "Id"
    where: str = ""
    tail: str = ""


@dataclass
class Sosl:
    term: str
    scope: str = "ALL"
    returning: list[Returning] = field(default_factory=list)
    limit: int | None = None


# -- parsing ---------------------------------------------------------------------------------


def parse_sosl(text: str) -> Sosl:
    s = text.strip()
    m = re.match(r"(?is)^FIND\s+", s)
    if not m:
        raise search_error("line 1:0 no viable alternative at input '" + s[:20] + "'")
    i = m.end()
    if i >= len(s):
        raise search_error(f"line 1:{i} mismatched input '<EOF>' expecting LCURLY")
    if s[i] == "{":
        j, buf = i + 1, []
        while j < len(s) and s[j] != "}":
            if s[j] == "\\" and j + 1 < len(s):
                buf.append(s[j + 1])
                j += 2
                continue
            buf.append(s[j])
            j += 1
        if j >= len(s):
            raise search_error(f"line 1:{j} mismatched input '<EOF>' expecting RCURLY")
        term, i = "".join(buf), j + 1
    elif s[i] == "'":
        j = s.find("'", i + 1)
        if j < 0:
            raise search_error(f"line 1:{len(s)} mismatched input '<EOF>'")
        term, i = s[i + 1 : j], j + 1
    else:
        raise search_error(f"line 1:{i} mismatched input '{s[i]}' expecting LCURLY")
    out = Sosl(term=term.strip())
    rest = s[i:].strip()
    scope = re.match(r"(?is)^IN\s+(ALL|NAME|EMAIL|PHONE|SIDEBAR)\s+FIELDS\s*", rest)
    if scope:
        out.scope = scope.group(1).upper()
        rest = rest[scope.end() :]
    if re.match(r"(?is)^RETURNING\s", rest):
        rest = rest[len("RETURNING") :].lstrip()
        items, rest = _split_returning(rest)
        out.returning = [_parse_item(x) for x in items]
    # remaining clauses: WITH ..., LIMIT n, UPDATE ...
    limit = re.search(r"(?is)\bLIMIT\s+(\d+)\s*$", rest) or re.search(r"(?is)\bLIMIT\s+(\d+)", rest)
    if limit:
        out.limit = int(limit.group(1))
    return out


def _split_returning(text: str) -> tuple[list[str], str]:
    items: list[str] = []
    depth, start, i, quote = 0, 0, 0, ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            if ch == ",":
                items.append(text[start:i].strip())
                start = i + 1
            elif re.match(r"(?is)\s(WITH|LIMIT|UPDATE|OFFSET)\b", text[i:]):
                break
        i += 1
    items.append(text[start:i].strip())
    return [x for x in items if x], text[i:]


def _parse_item(item: str) -> Returning:
    m = re.match(r"(?s)^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\((.*)\))?$", item.strip())
    if not m:
        raise search_error(f"line 1:0 no viable alternative at input '{item[:30]}'")
    ret = Returning(m.group(1))
    inner = (m.group(2) or "").strip()
    if not inner:
        return ret
    top = _top_level_keyword(inner, ("WHERE", "ORDER", "LIMIT", "OFFSET", "USING"))
    ret.fields = (inner[:top] if top is not None else inner).strip() or "Id"
    if top is not None:
        tail = inner[top:]
        if tail.upper().startswith("WHERE"):
            nxt = _top_level_keyword(tail[5:], ("ORDER", "LIMIT", "OFFSET"))
            if nxt is None:
                ret.where = tail[5:].strip()
            else:
                ret.where = tail[5 : 5 + nxt].strip()
                ret.tail = tail[5 + nxt :].strip()
        else:
            ret.tail = tail.strip()
    return ret


def _top_level_keyword(text: str, words: tuple[str, ...]) -> int | None:
    depth = 0
    try:
        tokens = tokenize(text)
    except SfError:
        return None
    for tok in tokens:
        if tok.kind == "punct" and tok.text == "(":
            depth += 1
        elif tok.kind == "punct" and tok.text == ")":
            depth -= 1
        elif depth == 0 and tok.kind == "ident" and tok.upper in words:
            return tok.pos
    return None


# -- matching --------------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9A-Za-zÀ-￿]+", text.lower()) if t]


class _Expr:
    """Boolean search expression over a record's token list."""

    def __init__(self, text: str) -> None:
        self.items = self._lex(text)
        self.pos = 0

    @staticmethod
    def _lex(text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for m in re.finditer(r'"([^"]*)"|(\()|(\))|([^\s()"]+)', text):
            if m.group(1) is not None:
                out.append(("phrase", m.group(1)))
            elif m.group(2):
                out.append(("(", "("))
            elif m.group(3):
                out.append((")", ")"))
            else:
                word = m.group(4)
                out.append(("op", word.upper()) if word.upper() in ("AND", "OR", "NOT") else ("word", word))
        return out

    def parse(self) -> Any:
        node = self._or()
        return node

    def _peek(self) -> tuple[str, str] | None:
        return self.items[self.pos] if self.pos < len(self.items) else None

    def _or(self) -> Any:
        left = self._and()
        while (p := self._peek()) is not None and (p == ("op", "OR") or p[0] in ("word", "phrase", "(")):
            if p == ("op", "OR"):
                self.pos += 1
            left = ("or", left, self._and())
        return left

    def _and(self) -> Any:
        left = self._not()
        while self._peek() == ("op", "AND"):
            self.pos += 1
            if self._peek() == ("op", "NOT"):
                self.pos += 1
                left = ("and", left, ("not", self._not()))
            else:
                left = ("and", left, self._not())
        return left

    def _not(self) -> Any:
        if self._peek() == ("op", "NOT"):
            self.pos += 1
            return ("not", self._not())
        return self._atom()

    def _atom(self) -> Any:
        p = self._peek()
        if p is None:
            return ("true",)
        self.pos += 1
        if p[0] == "(":
            node = self._or()
            if self._peek() == (")", ")"):
                self.pos += 1
            return node
        if p[0] == "phrase":
            return ("phrase", _tokens(p[1]))
        if p[0] == "word":
            return ("word", p[1])
        return ("true",)


def _word_matches(word: str, tokens: list[str], raw: str, digits: str) -> bool:
    w = word.lower()
    if "@" in w:
        return w.replace("*", "") in raw
    only_digits = re.sub(r"\D", "", w)
    if only_digits and len(only_digits) >= 3 and re.fullmatch(r"[\d\s()+\-.]+\*?", w):
        return only_digits in digits
    pattern = re.compile(
        "^" + "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in w) + "$"
    )
    if "*" not in w and "?" not in w:
        parts = _tokens(w)
        if len(parts) > 1:
            return _phrase_matches(parts, tokens)
        return w in tokens
    return any(pattern.match(t) for t in tokens)


def _phrase_matches(parts: list[str], tokens: list[str]) -> bool:
    n = len(parts)
    return n > 0 and any(tokens[i : i + n] == parts for i in range(len(tokens) - n + 1))


def _eval(node: Any, tokens: list[str], raw: str, digits: str) -> bool:
    kind = node[0]
    if kind == "true":
        return True
    if kind == "word":
        return _word_matches(node[1], tokens, raw, digits)
    if kind == "phrase":
        return _phrase_matches(node[1], tokens)
    if kind == "and":
        return _eval(node[1], tokens, raw, digits) and _eval(node[2], tokens, raw, digits)
    if kind == "or":
        return _eval(node[1], tokens, raw, digits) or _eval(node[2], tokens, raw, digits)
    if kind == "not":
        return not _eval(node[1], tokens, raw, digits)
    return False


def _search_fields(obj: SObject, scope: str) -> list[str]:
    names = []
    for f in obj.fields:
        if (
            (f.type == "email" and scope in ("ALL", "EMAIL", "SIDEBAR"))
            or (f.type == "phone" and scope in ("ALL", "PHONE", "SIDEBAR"))
            or (
                f.type in ("string", "combobox", "textarea", "url", "picklist", "multipicklist")
                and (
                    scope == "ALL"
                    or (scope in ("NAME", "SIDEBAR") and (f.name_field or f.name.lower() in NAME_FIELDS))
                )
            )
        ):
            names.append(f.name)
    return names


def matching_ids(engine: QueryEngine, obj: SObject, sosl: Sosl) -> list[str]:
    tree = _Expr(sosl.term).parse()
    fields = _search_fields(obj, sosl.scope)
    hits: list[tuple[int, str]] = []
    for rec in engine.source(obj, None, False):
        values = [str(rec[n]) for n in fields if rec.get(n) not in (None, "")]
        raw = " ".join(values).lower()
        tokens = _tokens(" ".join(values))
        digits = re.sub(r"\D", "", " ".join(str(rec[n]) for n in fields if rec.get(n) and _is_phone(obj, n)))
        if _eval(tree, tokens, raw, digits):
            name_tokens = _tokens(str(rec.get(obj.name_field) or ""))
            score = (
                2
                if any(_eval(tree, name_tokens, str(rec.get(obj.name_field) or "").lower(), "") for _ in (0,))
                else 1
            )
            hits.append((score, rec["Id"]))
    hits.sort(key=lambda h: -h[0])
    return [rid for _, rid in hits]


def _is_phone(obj: SObject, name: str) -> bool:
    f = obj.field(name)
    return f is not None and f.type == "phone"


def run_sosl(engine: QueryEngine, sosl: Sosl) -> list[dict[str, Any]]:
    """Execute the search; returns REST ``searchRecords``."""
    if not sosl.term:
        raise search_error("line 1:5 no viable alternative at input '{}'")
    targets = sosl.returning or [Returning(n) for n in DEFAULT_OBJECTS]
    results: list[dict[str, Any]] = []
    for item in targets:
        obj = SCHEMA.get(item.obj)
        if obj is None or not obj.searchable:
            raise SfError(
                400,
                "INVALID_TYPE",
                f"sObject type '{item.obj}' is not supported. If you are attempting to use a custom object, be sure "
                "to append the '__c' after the entity name. Please reference your WSDL or the describe call for "
                "the appropriate names.",
            )
        ids = matching_ids(engine, obj, sosl)
        # an empty match still runs the SOQL (against an impossible id) so bad fields are reported
        id_list = ",".join(f"'{i}'" for i in ids) or "'000000000000000'"
        where = f"WHERE Id IN ({id_list})" + (f" AND ({item.where})" if item.where else "")
        text = f"SELECT {item.fields} FROM {obj.name} {where} {item.tail}".strip()
        sub = engine.execute(text)
        # keep relevance order when the item gave no ORDER BY
        if "ORDER" not in item.tail.upper():
            rank = {rid: n for n, rid in enumerate(ids)}
            sub.records.sort(key=lambda r: rank.get(_record_id(r), 0))
        results.extend(sub.records)
    limit = sosl.limit if sosl.limit is not None else MAX_RESULTS
    return results[:limit]


def _record_id(row: dict[str, Any]) -> str:
    rid = row.get("Id")
    if rid:
        return str(rid)
    url = row.get("attributes", {}).get("url", "")
    return url.rsplit("/", 1)[-1]


def build_parameterized(
    q: str,
    *,
    scope: str = "ALL",
    sobjects: list[dict[str, Any]] | None = None,
    fields: list[str] | None = None,
    overall_limit: int | None = None,
    default_limit: int | None = None,
) -> Sosl:
    """Translate ``/parameterizedSearch`` parameters into the same plan SOSL produces."""
    sosl = Sosl(term=q.strip(), scope=scope.upper(), limit=overall_limit)
    names = sobjects or [{"name": n} for n in DEFAULT_OBJECTS]
    for entry in names:
        item_fields = entry.get("fields") or fields or ["Id"]
        tail = ""
        limit = entry.get("limit") or default_limit
        if limit:
            tail = f"LIMIT {int(limit)}"
        sosl.returning.append(
            Returning(entry["name"], ", ".join(item_fields), entry.get("where") or "", tail)
        )
    return sosl


def record_for(rec: Record) -> Record:
    return rec
