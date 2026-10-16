"""Stripe's form encoding and query semantics, independent of resource handlers."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import parse_qsl

from emulators.core.errors import EmulatorError

VERSION = "2025-09-30.clover"
SECRET_KEY = "sk_test_51HalcyonAdvisoryEmulator000000000000"
PUBLIC_KEY = "pk_test_51HalcyonAdvisoryEmulator000000000000"
Record = dict[str, Any]


def fail(
    message: str,
    param: str | None = None,
    *,
    code: str | None = None,
    status: int = 400,
    error_type: str = "invalid_request_error",
    **extra: Any,
) -> None:
    error: Record = {"type": error_type, "message": message, **extra}
    if param is not None:
        error["param"] = param
    if code:
        error.update(code=code, doc_url="https://stripe.com/docs/error-codes/" + code.replace("_", "-"))
    raise EmulatorError(status, {"error": error})


def integer(value: Any, param: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"-?\d+", str(value)):
        fail(f"Invalid integer: {value}", param)
    number = int(value)
    if minimum is not None and number < minimum:
        fail(f"This value must be greater than or equal to {minimum}.", param)
    if maximum is not None and number > maximum:
        fail(f"This value must be less than or equal to {maximum}.", param)
    return number


def boolean(value: Any) -> bool:
    if value in (True, "true", "True", "1", 1):
        return True
    if value in (False, "false", "False", "0", 0):
        return False
    fail(f"Invalid boolean: {value}")
    return False


def decode(data: str | bytes | Iterable[tuple[str, str]]) -> Record:
    """Decode nested Stripe forms without conflating indexed arrays with dictionaries."""
    pairs = (
        parse_qsl(data.decode() if isinstance(data, bytes) else data, keep_blank_values=True)
        if isinstance(data, (str, bytes))
        else data
    )
    root: Record = {}
    for key, value in pairs:
        if not re.fullmatch(r"[^\[\]]+(?:\[[^\[\]]*\])*", key):
            fail(f"Invalid parameter: {key}", key)
        parts = re.findall(r"([^\[\]]+)|\[([^\[\]]*)\]", key)
        path = [a or b for a, b in parts]
        if len(path) > 16:
            fail("Parameter nesting exceeds the maximum depth.", key)
        node: Any = root
        for index, part in enumerate(path):
            last = index == len(path) - 1
            next_list = (
                not last and part != "metadata" and (path[index + 1] == "" or path[index + 1].isdigit())
            )
            if isinstance(node, list):
                if part and not part.isdigit():
                    fail(f"Invalid array parameter: {key}", key)
                slot = integer(part, key, minimum=0, maximum=9999) if part else len(node)
                if (
                    not part
                    and not last
                    and node
                    and isinstance(node[-1], dict)
                    and path[index + 1] not in node[-1]
                ):
                    slot -= 1
                while len(node) <= slot:
                    node.append(None)
                if last:
                    node[slot] = value
                else:
                    if node[slot] is None:
                        node[slot] = [] if next_list else {}
                    node = node[slot]
            elif isinstance(node, dict):
                if last:
                    node[part] = value
                else:
                    if part not in node:
                        node[part] = [] if next_list else {}
                    node = node[part]
            else:
                fail(f"Conflicting parameters for {key}.", key)
    return root


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return copy.deepcopy(value)


def metadata(record: Record, params: Record) -> None:
    if "metadata" in params:
        if params["metadata"] == "":
            record["metadata"] = {}
        elif isinstance(params["metadata"], dict):
            target = record.setdefault("metadata", {})
            for key, value in params["metadata"].items():
                if value == "":
                    target.pop(key, None)
                else:
                    target[key] = str(value)
        else:
            fail("Invalid hash", "metadata")


def list_object(records: list[Record], params: Record, url: str) -> Record:
    limit = integer(params.get("limit", 10), "limit", minimum=1, maximum=100)
    if params.get("starting_after") and params.get("ending_before"):
        fail("You may only specify one of these parameters: starting_after, ending_before.")
    rows = sorted(records, key=lambda r: (r.get("created", 0), r.get("_order", 0)), reverse=True)
    for field, value in params.items():
        if field in {
            "customer",
            "payment_intent",
            "charge",
            "currency",
            "type",
            "email",
            "subscription",
            "product",
            "invoice",
            "test_clock",
            "status",
        }:
            if field == "status" and value == "all":
                continue
            rows = [
                r
                for r in rows
                if r.get(field, r.get("_subscription") if field == "subscription" else None) == value
            ]
        elif field == "types":
            rows = [r for r in rows if r.get("type") in value]
        elif field in {"active", "paid"}:
            rows = [r for r in rows if r.get(field) is boolean(value)]
        elif field == "lookup_keys":
            rows = [r for r in rows if r.get("lookup_key") in value]
        elif field == "created":
            if isinstance(value, dict):
                for op, bound in value.items():
                    number = integer(bound, "created")
                    if op not in {"gt", "gte", "lt", "lte"}:
                        fail("Invalid created filter", "created")
                    rows = [
                        r
                        for r in rows
                        if {
                            "gt": r["created"] > number,
                            "gte": r["created"] >= number,
                            "lt": r["created"] < number,
                            "lte": r["created"] <= number,
                        }[op]
                    ]
            else:
                rows = [r for r in rows if r.get("created") == integer(value, "created")]
    before = params.get("ending_before")
    cursor = before or params.get("starting_after")
    if cursor:
        ids = [r["id"] for r in rows]
        if cursor not in ids:
            fail(
                f"No such cursor: '{cursor}'",
                "ending_before" if before else "starting_after",
                code="resource_missing",
            )
        pos = ids.index(cursor)
        rows = rows[:pos] if before else rows[pos + 1 :]
    page = rows[-limit:] if before else rows[:limit]
    return {"object": "list", "data": page, "has_more": len(rows) > limit, "url": url}


_CLAUSE = re.compile(
    r"""\s*(-?)(metadata\[['"](?:\\.|[^'"\\])+['"]\]|[a-z_]+)\s*(:|~|>=|<=|>|<)\s*('(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?)"""
)


def search_predicate(query: str) -> Callable[[Record], bool]:
    """Stripe search disallows mixed AND/OR; reject invalid syntax instead of guessing."""
    clauses: list[tuple[bool, str, str, Any]] = []
    joins: list[str] = []
    offset = 0
    while offset < len(query):
        match = _CLAUSE.match(query, offset)
        if not match:
            fail("Invalid search query.", "query")
        negated, field, op, raw = match.groups()
        value: Any = re.sub(r"\\(.)", r"\1", raw[1:-1]) if raw.startswith(("'", '"')) else float(raw)
        clauses.append((bool(negated), field, op, value))
        offset = match.end()
        if offset == len(query) or not query[offset:].strip():
            break
        join = re.match(r"\s+(AND|OR)\s+", query[offset:])
        if not join:
            fail("Invalid search query: use AND or OR between clauses.", "query")
        joins.append(join.group(1))
        offset += join.end()
        if offset == len(query):
            fail("Invalid search query.", "query")
    if not clauses or len(clauses) > 10 or len(set(joins)) > 1:
        fail("Search supports up to ten clauses with AND or OR, without mixing operators.", "query")

    def predicate(record: Record) -> bool:
        matches = []
        for negate, field, op, expected in clauses:
            key = re.match(r"""metadata\[['"](.*?)['"]\]""", field)
            actual = record.get("metadata", {}).get(key.group(1)) if key else record.get(field)
            hit = False
            if actual is not None:
                if op == ":":
                    hit = (
                        str(actual).casefold() == str(expected).casefold()
                        if isinstance(expected, str)
                        else actual == expected
                    )
                elif op == "~":
                    hit = str(expected).casefold() in str(actual).casefold()
                else:
                    try:
                        a, b = float(actual), float(expected)
                        hit = {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]
                    except (ValueError, TypeError):
                        pass
            matches.append(not hit if negate else hit)
        return any(matches) if joins and joins[0] == "OR" else all(matches)

    return predicate
