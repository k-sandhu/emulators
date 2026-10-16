"""Fixtures for the Salesforce provider tests: a frozen-clock hub and per-user API actors."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from emulators.core.clock import Clock
from emulators.core.hub import EmulatorHub

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
BASE = "https://halcyonadvisory.my.salesforce.com"
LOGIN = "https://login.salesforce.com"
VERSION = "62.0"
API = f"{BASE}/services/data/v{VERSION}"
PASSWORD = "Halcyon2026!"
TOKEN = "EMULATORTOKEN"
# world person ids used throughout
ARJUN, SARAH = "p035", "p036"  # system administrators
ELEANOR = "p001"  # managing partner (top of the role hierarchy)
RAJESH = "p012"  # tax partner
MAYA = "p018"  # advisory partner
NATALIE = "p040"  # director of business development (Sales User)
JASMINE = "p029"  # office manager (Read Only profile)
HANNAH = "p011"  # audit staff


def username(first_last: str) -> str:
    return f"{first_last}@halcyonadvisory.example.crm"


class Actor:
    """An authenticated caller: ``await actor.get('/sobjects/Account/001...')``."""

    def __init__(self, client: httpx.AsyncClient, token: str, user_id: str, who: str) -> None:
        self.client = client
        self.token = token
        self.user_id = user_id
        self.who = who

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.headers, **kwargs.pop("headers", {})}
        url = path if path.startswith("http") else API + path
        return await self.client.request(method, url, headers=headers, **kwargs)

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, body: Any = None, **kw: Any) -> httpx.Response:
        if body is not None and "content" not in kw and "data" not in kw:
            kw["json"] = body
        return await self.request("POST", path, **kw)

    async def patch(self, path: str, body: Any = None, **kw: Any) -> httpx.Response:
        if body is not None and "content" not in kw:
            kw["json"] = body
        return await self.request("PATCH", path, **kw)

    async def delete(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("DELETE", path, **kw)

    async def query(self, soql: str, *, all_rows: bool = False, **kw: Any) -> dict[str, Any]:
        r = await self.get("/queryAll" if all_rows else "/query", params={"q": soql}, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    async def rows(self, soql: str, **kw: Any) -> list[dict[str, Any]]:
        return (await self.query(soql, **kw))["records"]

    async def ids(self, soql: str) -> list[str]:
        return [r["Id"] for r in await self.rows(soql)]

    async def create(self, sobject: str, body: dict[str, Any]) -> str:
        r = await self.post(f"/sobjects/{sobject}", body)
        assert r.status_code == 201, r.text
        return r.json()["id"]


@pytest.fixture(scope="module")
def _salesforce_hub() -> EmulatorHub:
    return EmulatorHub(providers=["salesforce"], anchor="2026-10-16", clock=Clock(NOW), webhook_mode="sync")


@pytest.fixture
def hub(_salesforce_hub: EmulatorHub) -> EmulatorHub:
    _salesforce_hub.reset()
    _salesforce_hub.clock.freeze(NOW)
    _salesforce_hub.webhook_transport = None
    return _salesforce_hub


@pytest.fixture
async def client(hub: EmulatorHub) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=hub.async_transport(), timeout=30) as c:
        c.hub = hub  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def actor(client: httpx.AsyncClient) -> Callable[..., Awaitable[Actor]]:
    """``await actor('p035')`` mints a session token for a world person (or ``integration``)."""

    async def make(person: str = ARJUN, **payload: Any) -> Actor:
        r = await client.post(f"{BASE}/_emulator/actions/mint_token", json={"user": person, **payload})
        assert r.status_code == 200, r.text
        body = r.json()
        return Actor(client, body["access_token"], body["user_id"], person)

    return make


@pytest.fixture
async def admin(actor: Callable[..., Awaitable[Actor]]) -> Actor:
    return await actor(ARJUN)


def soap_login_xml(user: str, password: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<env:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:env="http://schemas.xmlsoap.org/soap/envelope/" xmlns:urn="urn:partner.soap.sforce.com">'
        '<env:Body><n1:login xmlns:n1="urn:partner.soap.sforce.com">'
        f"<n1:username>{user}</n1:username><n1:password>{password}</n1:password>"
        "</n1:login></env:Body></env:Envelope>"
    )


def pretty(data: Any) -> str:
    return json.dumps(data, indent=1)[:2000]
