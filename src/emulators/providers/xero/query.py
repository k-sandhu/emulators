"""A small parsed Xero where language; never execute client-supplied expressions."""

from __future__ import annotations

import json
import math
import operator
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from fastapi import Request

from .common import ID_FIELDS, instant, public, validation

TOKEN = re.compile(r'\s*("(?:[^"\\]|\\.)*"|==|!=|>=|<=|&&|\|\||[><(),!]|-?\d+(?:\.\d+)?|[A-Za-z_][\w.]*)')


def path(record: dict[str, Any], name: str) -> Any:
    value: Any = record
    for part in name.split("."):
        if not isinstance(value, dict):
            return None
        value = next((v for k, v in value.items() if k.lower() == part.lower()), None)
    return value


class Parser:
    def __init__(self, text: str) -> None:
        self.tokens: list[str] = []
        self.index = 0
        position = 0
        if len(text) > 8000:
            validation("The where expression is too long.")
        while position < len(text.rstrip()):
            match = TOKEN.match(text, position)
            if not match:
                validation("The where expression is invalid.")
            self.tokens.append(match[1])
            position = match.end()

    def take(self) -> str:
        if self.index >= len(self.tokens):
            validation("Incomplete where expression.")
        value = self.tokens[self.index]
        self.index += 1
        return value

    def peek(self, *values: str) -> bool:
        return self.index < len(self.tokens) and self.tokens[self.index].lower() in values

    def expect(self, value: str) -> None:
        if self.take() != value:
            validation("Invalid where expression.")

    def logical(self, level: int = 0, depth: int = 0) -> Any:
        if depth > 40:
            validation("The where expression is too complex.")
        if level < 2:
            left = self.logical(level + 1, depth + 1)
            operators = ("or", "||") if level == 0 else ("and", "&&")
            while self.peek(*operators):
                self.take()
                left = (operators[0], left, self.logical(level + 1, depth + 1))
            return left
        if self.peek("not", "!"):
            self.take()
            return ("not", self.logical(2, depth + 1))
        if self.peek("("):
            self.take()
            node = self.logical(depth=depth + 1)
            self.expect(")")
            return node
        left = self.value()
        if self.peek("==", "!=", ">=", "<=", ">", "<"):
            return (self.take(), left, self.value())
        return left

    def value(self) -> Any:
        token = self.take()
        if token.startswith('"'):
            try:
                return ("literal", json.loads(token))
            except ValueError:
                validation("Invalid quoted string in where expression.")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", token):
            return ("literal", float(token))
        if token.lower() in {"true", "false", "null"}:
            return ("literal", {"true": True, "false": False, "null": None}[token.lower()])
        if self.peek("("):
            self.take()
            if token.lower() == "datetime":
                parts = [int(self.take())]
                while self.peek(","):
                    self.take()
                    parts.append(int(self.take()))
                self.expect(")")
                try:
                    return ("literal", datetime(*parts, tzinfo=UTC))
                except (TypeError, ValueError):
                    validation("Invalid DateTime in where expression.")
            field, _, method = token.rpartition(".")
            if method.lower() not in {"contains", "startswith", "endswith", "equals"} or not field:
                validation("Unsupported where function.")
            argument = self.value()
            self.expect(")")
            return (method.lower(), ("path", field), argument)
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.\w+)*", token):
            validation("Invalid property in where expression.")
        return ("path", token)

    def parse(self) -> Any:
        try:
            node = self.logical()
        except (ValueError, RecursionError):
            validation("The where expression is invalid.")
        if self.index != len(self.tokens):
            validation("Unexpected token in where expression.")
        return node


def evaluate(node: Any, record: dict[str, Any]) -> Any:
    op = node[0]
    if op == "literal":
        return node[1]
    if op == "path":
        return path(record, node[1])
    if op == "not":
        return not evaluate(node[1], record)
    left, right = evaluate(node[1], record), evaluate(node[2], record)
    if op == "and":
        return bool(left and right)
    if op == "or":
        return bool(left or right)
    if op in {"contains", "startswith", "endswith", "equals"}:
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        left, right = left.casefold(), right.casefold()
        return {
            "contains": lambda: right in left,
            "startswith": lambda: left.startswith(right),
            "endswith": lambda: left.endswith(right),
            "equals": lambda: left == right,
        }[op]()
    if isinstance(left, datetime) or isinstance(right, datetime):
        try:
            left, right = instant(left), instant(right)
        except (ValueError, TypeError):
            return False
    if isinstance(left, str) and isinstance(right, str):
        left, right = left.casefold(), right.casefold()
    try:
        return {
            "==": operator.eq,
            "!=": operator.ne,
            ">": operator.gt,
            "<": operator.lt,
            ">=": operator.ge,
            "<=": operator.le,
        }[op](left, right)
    except TypeError:
        return False


def select(
    request: Request, resource: str, records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = {k.lower(): v for k, v in request.query_params.items()}
    if resource == "Contacts" and query.get("includearchived", "false").lower() != "true":
        records = [r for r in records if r.get("ContactStatus") != "ARCHIVED"]
    for parameter, field in {
        "ids": ID_FIELDS[resource],
        "invoicenumbers": "InvoiceNumber",
        "contactids": "Contact.ContactID" if resource != "Contacts" else "ContactID",
        "statuses": "Status",
    }.items():
        if parameter in query:
            wanted = {v.strip().lower() for v in query[parameter].split(",")}
            records = [r for r in records if str(path(r, field)).lower() in wanted]
    if query.get("where"):
        node = Parser(query["where"]).parse()
        records = [r for r in records if evaluate(node, r)]
    if value := request.headers.get("if-modified-since"):
        try:
            since = parsedate_to_datetime(value) if "," in value else instant(value)
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
        except ValueError:
            validation("If-Modified-Since must be a valid UTC date.")
        records = [r for r in records if r.get("UpdatedDateUTC") and instant(r["UpdatedDateUTC"]) > since]
    if term := query.get("searchterm"):
        records = [
            r
            for r in records
            if term.casefold().strip('"')
            in str(
                r.get("InvoiceNumber", "") + " " + r.get("Reference", "") + " " + r.get("Name", "")
            ).casefold()
        ]
    order = query.get("order", "UpdatedDateUTC ASC," + ID_FIELDS[resource] + " ASC")
    for clause in reversed(order.split(",")):
        match = re.fullmatch(r"\s*([\w.]+)(?:\s+(ASC|DESC))?\s*", clause, re.I)
        if not match:
            validation("Invalid order expression.")
        records = sorted(
            records,
            key=lambda r: (path(r, match[1]) is None, path(r, match[1]) or ""),
            reverse=(match[2] or "").upper() == "DESC",
        )
    paged = (
        "page" in query
        or "pagesize" in query
        or "statuses" in query
        or query.get("summaryonly", "").lower() == "true"
    )
    extra: dict[str, Any] = {}
    if paged:
        try:
            page, size = int(query.get("page", "1")), max(1, min(1000, int(query.get("pagesize", "100"))))
        except ValueError:
            validation("Page and pageSize must be integers.")
        if page < 1:
            validation("Page must be greater than zero.")
        count = len(records)
        records = records[(page - 1) * size : page * size]
        extra["pagination"] = {
            "page": page,
            "pageSize": size,
            "pageCount": math.ceil(count / size),
            "itemCount": count,
        }
    result = public(records)
    if resource == "Invoices" and not paged:
        result = [{k: v for k, v in r.items() if k != "LineItems"} for r in result]
    if query.get("summaryonly", "").lower() == "true":
        excluded = (
            {"LineItems", "Payments", "HasAttachments", "CISDeduction"}
            if resource == "Invoices"
            else {"Addresses", "Balances", "ContactGroups", "ContactPersons", "IsCustomer", "IsSupplier"}
        )
        result = [{k: v for k, v in r.items() if k not in excluded} for r in result]
    return result, extra
