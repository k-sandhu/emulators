"""Validate Outlook complex values before writes or deliveries change mailbox state."""

from __future__ import annotations

import copy
import re
from typing import Any

from emulators.providers.microsoft import common as c


def item_body(value: Any) -> dict[str, str]:
    body = c.json_object(value)
    if not isinstance(body.get("content"), str) or not isinstance(body.get("contentType", "text"), str):
        raise c.invalid_json()
    kind = body.get("contentType", "text").lower()
    if kind not in ("html", "text"):
        raise c.graph_error(400, "BadRequest", "Invalid body contentType.")
    return {"contentType": kind, "content": body["content"]}


def recipients(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise c.invalid_json()
    for item in value:
        address = c.json_object(c.json_object(item).get("emailAddress"))
        email = address.get("address")
        if not isinstance(email, str) or not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", email):
            raise c.graph_error(400, "ErrorInvalidRecipients", "One or more recipients are invalid.")
        if "name" in address and not isinstance(address["name"], str):
            raise c.invalid_json()
    return copy.deepcopy(value)


def message(value: Any, *, sending: bool = False) -> dict[str, Any]:
    payload = copy.deepcopy(c.json_object(value))
    if "body" in payload:
        payload["body"] = item_body(payload["body"])
    for key in ("toRecipients", "ccRecipients", "bccRecipients", "replyTo"):
        if key in payload:
            payload[key] = recipients(payload[key])
    for key in ("subject", "importance"):
        if key in payload and not isinstance(payload[key], str):
            raise c.invalid_json()
    if "importance" in payload:
        if payload["importance"].lower() not in ("low", "normal", "high"):
            raise c.graph_error(400, "BadRequest", "Invalid importance value.")
        payload["importance"] = payload["importance"].lower()
    if sending and not any(payload.get(key) for key in ("toRecipients", "ccRecipients", "bccRecipients")):
        raise c.graph_error(
            400,
            "ErrorInvalidRecipients",
            "At least one recipient isn't valid. A message can't be sent because it contains no recipients.",
        )
    if "attachments" in payload and not isinstance(payload["attachments"], list):
        raise c.invalid_json()
    for key in ("isRead", "isReadReceiptRequested", "isDeliveryReceiptRequested"):
        if key in payload and not isinstance(payload[key], bool):
            raise c.invalid_json()
    if "flag" in payload:
        c.json_object(payload["flag"])
    if "categories" in payload and (
        not isinstance(payload["categories"], list)
        or not all(isinstance(v, str) for v in payload["categories"])
    ):
        raise c.invalid_json()
    return payload


def datetime_zone(value: Any) -> dict[str, str]:
    result = c.json_object(value)
    if not isinstance(result.get("dateTime"), str) or not isinstance(result.get("timeZone"), str):
        raise c.invalid_json()
    return result
