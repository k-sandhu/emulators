from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from emulators.core.clock import parse_instant
from emulators.testing.helpers import mint

API = "https://api.xero.com"
BASE = "/api.xro/2.0"


@pytest.fixture
async def client(make_client: Callable[..., Any]) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client("xero", base_url=API) as instance:
        instance.hub.clock.freeze(parse_instant("2026-10-16T12:00:00Z"))
        token = await mint(instance, API, user="p001")
        instance.headers.update({"Authorization": "Bearer " + token, "Accept": "application/json"})
        tenants = (await instance.get("/connections")).json()
        instance.headers["xero-tenant-id"] = tenants[0]["tenantId"]
        yield instance


async def create_invoice(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    contacts = (await client.get(BASE + "/Contacts")).json()["Contacts"]
    contact = next(c for c in contacts if c.get("ContactNumber") == "c001")
    value = {
        "Type": "ACCREC",
        "Contact": {"ContactID": contact["ContactID"]},
        "Date": "2026-10-16",
        "DueDate": "2026-11-15",
        "Status": "AUTHORISED",
        "LineItems": [
            {
                "Description": "Advisory service",
                "Quantity": 2,
                "UnitAmount": 50,
                "AccountCode": "4200",
                "TaxType": "NONE",
            }
        ],
        **overrides,
    }
    response = await client.put(BASE + "/Invoices", json={"Invoices": [value]})
    assert response.status_code == 200, response.text
    return response.json()["Invoices"][0]
