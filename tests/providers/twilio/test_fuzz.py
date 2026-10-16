"""Wrong types, empty and hostile input must never produce a 500 on any Twilio route."""

from __future__ import annotations

import re
from typing import Any

import httpx

from tests.providers.twilio.conftest import CONTACT, FROM_NUMBER

SKIP_PREFIXES = ("/openapi", "/docs", "/_emulator/")
ACTIONS = ("inbound_sms", "advance_message", "inbound_call", "advance_call")

BODIES: list[dict[str, Any]] = [
    {"content": b""},
    {"data": {"To": "", "From": "", "Body": "", "Status": "", "FriendlyName": "", "Code": "", "Channel": ""}},
    {"json": [1, 2, 3]},
    {"json": "text"},
    {"content": b"{{{{", "headers": {"content-type": "application/json"}},
    {"content": b"--x\r\ngarbage", "headers": {"content-type": "multipart/form-data; boundary=x"}},
    {
        "content": b"To=%ff%fe&Body=%c3%28&From=%",
        "headers": {"content-type": "application/x-www-form-urlencoded"},
    },
    {
        "json": {
            "To": 5,
            "From": ["a"],
            "Body": {"x": 1},
            "MediaUrl": [1, None, {"a": 1}],
            "FriendlyName": 12,
            "from": 5,
            "to": ["x"],
            "body": {"a": 1},
            "sid": ["x"],
            "media_urls": 5,
            "status": 7,
            "Code": 1234,
            "Channel": 5,
            "SendAt": 5,
            "Attributes": 5,
            "ContentSid": 5,
            "StatusCallbackEvent": [5],
            "Url": 5,
        }
    },
    {
        "data": {
            "To": "+1‮4165550141",
            "From": FROM_NUMBER,
            "Body": "\U0001f600" * 20 + "\x00\x01",
            "FriendlyName": "\u0000퟿",
            "Status": "é",
            "Url": "http://x/é",
            "Code": "٣٤",
            "Attributes": "é",
        }
    },
    {
        "data": {
            "To": "+" + "1" * 5000,
            "From": "x" * 5000,
            "Body": "y" * 200_000,
            "FriendlyName": "z" * 100_000,
            "Url": "http://" + "a" * 5000,
        }
    },
    {"data": {"PageSize": "-5", "Page": "-9", "ValidityPeriod": "-1", "CodeLength": "-1", "Timeout": "x"}},
    {
        "data": {
            "To": CONTACT,
            "From": FROM_NUMBER,
            "Body": "x",
            "MessagingServiceSid": "",
            "SendAt": "9999-99-99",
            "ScheduleType": "x",
        }
    },
]
QUERIES = [
    {
        "PageSize": "abc",
        "Page": "-1",
        "To": "é",
        "DateSent": "garbage",
        "DateSent<": "2026-13-45",
        "StartTime": "zz",
        "PageToken": "!!!",
        "Fields": "é,,",
        "AreaCode": "x",
        "Contains": "*",
        "Beta": "q",
        "CountryCode": "zz",
        "StartDate": "x",
        "EndDate": "9999-99-99",
        "Order": "zz",
        "State": "zz",
    },
    {"PageSize": "1", "Page": "999999999999999999999"},
]


def _fill(client: httpx.AsyncClient, path: str) -> str:
    ctx = client.hub.context("twilio")  # type: ignore[attr-defined]
    store = ctx.store

    def first(collection: str) -> str:
        records = store.collection(collection).values()
        return records[0]["sid"] if records else "XX"

    def pick(name: str) -> str:
        table = {
            "account_sid": client.account_sid,  # type: ignore[attr-defined]
            "media_sid": first("media"),
            "pn_sid": first("numbers"),
            "verification_sid": first("verifications"),
            "participant_sid": first("conv_participants"),
            "message_sid": first("conv_messages"),
            "rec_sid": first("recordings"),
            "country": "US",
            "period": "Daily",
            "phone_number": FROM_NUMBER,
        }
        if name in table:
            return table[name]
        if "/Conversations" in path:
            return first("conversations")
        if "/Messages" in path:
            return first("messages")
        for marker, collection in (
            ("/Calls", "calls"),
            ("/Recordings", "recordings"),
            ("/IncomingPhoneNumbers", "numbers"),
            ("/Keys", "api_keys"),
            ("/v1/Services", "messaging_services"),
            ("/v2/Services", "verify_services"),
        ):
            if marker in path:
                return first(collection)
        return client.account_sid  # type: ignore[attr-defined]

    return re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: pick(m.group(1)), path)


def _host(path: str) -> str:
    if path.startswith("/v1/Services"):
        return "messaging.twilio.com"
    if path.startswith("/v2/Services"):
        return "verify.twilio.com"
    if "/PhoneNumbers" in path and path.startswith("/v"):
        return "lookups.twilio.com"
    if path.startswith("/v1/Conversations"):
        return "conversations.twilio.com"
    return "api.twilio.com"


async def test_no_route_returns_a_500_for_hostile_input(twilio: httpx.AsyncClient) -> None:
    hub = twilio.hub  # type: ignore[attr-defined]
    routes: list[tuple[str, str]] = []
    for route in hub.apps["twilio"].routes:
        path = getattr(route, "path", "")
        if path.startswith(SKIP_PREFIXES):
            continue
        routes += [
            (m, path) for m in sorted(getattr(route, "methods", None) or []) if m not in ("HEAD", "OPTIONS")
        ]
    routes += [("POST", f"/_emulator/actions/{name}") for name in ACTIONS]
    assert len(routes) > 80

    failures: list[str] = []
    for method, path in routes:
        hub.reset("twilio")
        url = f"https://{_host(path)}{_fill(twilio, path)}"
        attempts = [{"params": q} for q in QUERIES] if method in ("GET", "DELETE") else BODIES
        for kwargs in attempts:
            try:
                response = await twilio.request(method, url, **kwargs)
            except Exception as exc:  # an unhandled server error surfaces as an exception in-process
                failures.append(f"{method} {path}: {exc!r}"[:200])
                continue
            if response.status_code >= 500:
                failures.append(f"{method} {path}: {response.status_code} {response.text[:120]}")
    assert not failures, "\n".join(failures)
