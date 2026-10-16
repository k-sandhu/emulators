"""Gmail query grammar: implicit AND, OR, negation and nested groups."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from emulators.providers.google.common import google_error

TOKEN = re.compile(r'-|[(){}]|(?:[^\s(){}"]|"[^"]*")+')


def compile_query(query: str, now: datetime, labels: dict[str, str]) -> Callable[[dict[str, Any]], bool]:
    tokens = TOKEN.findall(query)
    index = 0

    def match_term(term: str) -> Callable[[dict[str, Any]], bool]:
        operator, sep, value = term.partition(":")
        operator = operator.lower()
        value = value.strip('"').casefold()
        if not sep:
            value = term.strip('"').casefold()
            return lambda r: (
                value
                in (r.get("_subject", "") + " " + r.get("_body", "") + " " + r.get("_from", "")).casefold()
            )
        if operator in ("from", "to", "cc", "bcc", "subject"):
            return lambda r: value in r.get("_" + operator, "").casefold()
        if operator in ("label", "in"):
            mapped = {
                "inbox": "INBOX",
                "sent": "SENT",
                "trash": "TRASH",
                "spam": "SPAM",
                "draft": "DRAFT",
                "drafts": "DRAFT",
            }
            if value == "anywhere":
                return lambda _: True
            label = mapped.get(value, labels.get(value, value.upper()))
            return lambda r: label in r["labelIds"]
        if operator == "is":
            if value == "read":
                return lambda r: "UNREAD" not in r["labelIds"]
            return lambda r: (
                {"unread": "UNREAD", "starred": "STARRED", "important": "IMPORTANT"}.get(value, "")
                in r["labelIds"]
            )
        if operator == "has" and value == "attachment":
            return lambda r: bool(r.get("_attachments"))
        if operator == "filename":
            return lambda r: any(value in a["filename"].casefold() for a in r.get("_attachments", []))
        if operator in ("after", "before", "newer", "older"):
            try:
                instant = (
                    datetime.fromtimestamp(int(value), UTC)
                    if value.isdigit()
                    else datetime.strptime(value.replace("-", "/"), "%Y/%m/%d").replace(
                        tzinfo=ZoneInfo("America/Los_Angeles")
                    )
                )
            except (ValueError, OverflowError, OSError) as exc:
                raise google_error(400, "Invalid search date", reason="invalidArgument") from exc
            threshold = instant.timestamp() * 1000
            return lambda r: (
                int(r["internalDate"]) > threshold
                if operator in ("after", "newer")
                else int(r["internalDate"]) < threshold
            )
        if operator in ("newer_than", "older_than"):
            match = re.fullmatch(r"(\d+)([dmy])", value)
            if not match:
                raise google_error(400, "Invalid search duration", reason="invalidArgument")
            threshold = (
                now - timedelta(days=int(match[1]) * {"d": 1, "m": 30, "y": 365}[match[2]])
            ).timestamp() * 1000
            return lambda r: (
                int(r["internalDate"]) > threshold
                if operator == "newer_than"
                else int(r["internalDate"]) < threshold
            )
        if operator in ("larger", "smaller", "size"):
            match = re.fullmatch(r"(\d+)([km]?)", value)
            if not match:
                raise google_error(400, "Invalid search size", reason="invalidArgument")
            size = int(match[1]) * {"": 1, "k": 1000, "m": 1000000}[match[2]]
            return lambda r: r["sizeEstimate"] < size if operator == "smaller" else r["sizeEstimate"] > size
        if operator == "rfc822msgid":
            return lambda r: value.strip("<>") == r.get("_rfcId", "").strip("<>").casefold()
        return lambda _: False

    def atom() -> Callable[[dict[str, Any]], bool]:
        nonlocal index
        if index >= len(tokens):
            raise ValueError("Incomplete query")
        token = tokens[index]
        index += 1
        if token == "-":
            operand = atom()
            return lambda r: not operand(r)
        if token in ("(", "{"):
            closing = ")" if token == "(" else "}"
            result = expression(closing, token == "{")
            if index >= len(tokens) or tokens[index] != closing:
                raise ValueError("Unbalanced query")
            index += 1
            return result
        if token in (")", "}", "OR"):
            raise ValueError("Unexpected token")
        if token.endswith(":") and index < len(tokens) and tokens[index] in ("(", "{"):
            prefix = token
            start, depth = index + 1, 1
            index += 1
            while index < len(tokens) and depth:
                if tokens[index] in ("(", "{"):
                    depth += 1
                elif tokens[index] in (")", "}"):
                    depth -= 1
                if depth and tokens[index] not in ("OR", "-", "(", ")", "{", "}"):
                    tokens[index] = prefix + tokens[index]
                index += 1
            if depth:
                raise ValueError("Unbalanced query")
            index = start - 1
            return atom()
        return match_term(token)

    def expression(closing: str = "", braces: bool = False) -> Callable[[dict[str, Any]], bool]:
        nonlocal index
        groups: list[list[Callable[[dict[str, Any]], bool]]] = [[]]
        while index < len(tokens) and tokens[index] != closing:
            if tokens[index] == "OR":
                if not groups[-1]:
                    raise ValueError("Missing OR operand")
                groups.append([])
                index += 1
            else:
                if braces and groups[-1]:
                    groups.append([])
                groups[-1].append(atom())
        if len(groups) > 1 and not groups[-1]:
            raise ValueError("Missing OR operand")
        return lambda r: any(all(part(r) for part in group) for group in groups)

    try:
        result = expression()
        if index != len(tokens):
            raise ValueError("Unexpected token")
        return result
    except (ValueError, RecursionError) as exc:
        raise google_error(400, f"Invalid query: {exc}", reason="invalidArgument") from exc
