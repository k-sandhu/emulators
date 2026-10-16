"""Xero has no installed SDK here; preserve its documented HTTP call sequence."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytestmark = pytest.mark.sdk


def test_xero_documented_oauth_discovery_invoices_payment_and_reports(emulator_intercept: Any) -> None:
    with httpx.Client() as client:
        authorize = client.get(
            "https://login.xero.com/identity/connect/authorize",
            params={
                "response_type": "code",
                "client_id": "http-contract",
                "redirect_uri": "https://consumer.example/callback",
                "scope": "openid profile email offline_access accounting.transactions accounting.contacts accounting.settings accounting.reports.read",
                "state": "sdk-state",
                "login_hint": "eleanor.whitfield@halcyonadvisory.example",
            },
        )
        assert authorize.status_code == 302
        callback = parse_qs(urlparse(authorize.headers["location"]).query)
        assert callback["state"] == ["sdk-state"]
        tokens = client.post(
            "https://identity.xero.com/connect/token",
            auth=("http-contract", "test-secret"),
            data={
                "grant_type": "authorization_code",
                "code": callback["code"][0],
                "redirect_uri": "https://consumer.example/callback",
            },
        )
        tokens.raise_for_status()
        client.headers.update(
            {"Authorization": "Bearer " + tokens.json()["access_token"], "Accept": "application/json"}
        )
        connections = client.get("https://api.xero.com/connections").json()
        assert len(connections) == 2
        main = next(c for c in connections if c["tenantName"] == "Halcyon Advisory Group")
        client.headers["xero-tenant-id"] = main["tenantId"]
        base = "https://api.xero.com/api.xro/2.0"
        contacts = client.get(base + "/Contacts", params={"where": 'Name=="Northwind Logistics"'}).json()[
            "Contacts"
        ]
        invoice = client.put(
            base + "/Invoices",
            json={
                "Invoices": [
                    {
                        "Type": "ACCREC",
                        "Contact": {"ContactID": contacts[0]["ContactID"]},
                        "Date": "2026-10-16",
                        "DueDate": "2026-11-16",
                        "Status": "AUTHORISED",
                        "LineAmountTypes": "Exclusive",
                        "LineItems": [
                            {
                                "Description": "SDK contract advisory",
                                "Quantity": 1,
                                "UnitAmount": 125,
                                "AccountCode": "4200",
                                "TaxType": "NONE",
                            }
                        ],
                    }
                ]
            },
        )
        invoice.raise_for_status()
        iid = invoice.json()["Invoices"][0]["InvoiceID"]
        listed = client.get(base + "/Invoices", params={"IDs": iid, "page": 1, "pageSize": 100})
        assert listed.json()["Invoices"][0]["Total"] == 125
        payment = client.put(
            base + "/Payments",
            json={
                "Payments": [
                    {
                        "Invoice": {"InvoiceID": iid},
                        "Account": {"Code": "1000"},
                        "Date": "2026-10-16",
                        "Amount": 125,
                        "Reference": "http-contract",
                    }
                ]
            },
        )
        payment.raise_for_status()
        assert client.get(base + "/Invoices/" + iid).json()["Invoices"][0]["Status"] == "PAID"
        assert client.get(
            base + "/Invoices/" + iid, headers={"Accept": "application/pdf"}
        ).content.startswith(b"%PDF-")
        report = client.get(base + "/Reports/BalanceSheet", params={"date": "2026-10-16"})
        report.raise_for_status()
        assert report.json()["Reports"][0]["ReportType"] == "BalanceSheet"
        demo = next(c for c in connections if c["tenantName"] == "Demo Company (US)")
        assert (
            client.get(base + "/Invoices", headers={"xero-tenant-id": demo["tenantId"]}).json()["Invoices"]
            == []
        )
