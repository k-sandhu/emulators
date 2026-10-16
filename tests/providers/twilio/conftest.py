"""Fixtures for Twilio provider tests: a reset hub client with Basic auth preset."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from emulators.core.hub import EmulatorHub

API = "https://api.twilio.com"
MESSAGING = "https://messaging.twilio.com"
VERIFY = "https://verify.twilio.com"
LOOKUPS = "https://lookups.twilio.com"
CONVERSATIONS = "https://conversations.twilio.com"

FROM_NUMBER = "+14165550188"  # Halcyon Client Line (Toronto)
NY_NUMBER = "+12125550188"
WHATSAPP_SENDER = "whatsapp:+14165550177"
CONTACT = "+14165550141"  # Christopher Ingram, Northwind Logistics (c001-1)
CONTACT_NAME = "Christopher Ingram"


@pytest.fixture
async def twilio(make_client: Callable[..., Any]) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client("twilio") as client:
        hub: EmulatorHub = client.hub  # type: ignore[attr-defined]
        creds = hub.context("twilio").store.meta["credentials"]
        client.auth = httpx.BasicAuth(creds["account_sid"], creds["auth_token"])
        client.creds = creds  # type: ignore[attr-defined]
        client.account_sid = creds["account_sid"]  # type: ignore[attr-defined]
        yield client


def account_base(client: httpx.AsyncClient) -> str:
    return f"{API}/2010-04-01/Accounts/{client.account_sid}"  # type: ignore[attr-defined]


def messages_url(client: httpx.AsyncClient) -> str:
    return f"{account_base(client)}/Messages.json"


async def seeded_messaging_service(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.get(f"{MESSAGING}/v1/Services")
    assert r.status_code == 200, r.text
    return next(s for s in r.json()["services"] if s["friendly_name"] == "Halcyon Client Notifications")


async def seeded_verify_service(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.get(f"{VERIFY}/v2/Services")
    assert r.status_code == 200, r.text
    return next(s for s in r.json()["services"] if s["friendly_name"] == "Halcyon Client Portal")
