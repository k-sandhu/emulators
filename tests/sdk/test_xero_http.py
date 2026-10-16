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


def test_xero_custom_connection_granular_scopes_and_invoice_number_payment(emulator_intercept: Any) -> None:
    with httpx.Client() as client:
        api = "https://api.xero.com"
        base = api + "/api.xro/2.0"
        credentials = client.get(api + "/_emulator/info").json()["credentials"]
        tokens = client.post(
            "https://identity.xero.com/connect/token",
            auth=(credentials["client_id"], credentials["client_secret"]),
            data={
                "grant_type": "client_credentials",
                "scope": "accounting.invoices accounting.payments accounting.contacts.read accounting.settings.read",
            },
        )
        tokens.raise_for_status()
        client.headers.update(
            {"Authorization": "Bearer " + tokens.json()["access_token"], "Accept": "application/json"}
        )
        # A Custom Connection carries the tenant in its token; there is no tenant header.
        contacts = client.get(base + "/Contacts", params={"searchTerm": "christopher.ingram@northwind"})
        contacts.raise_for_status()
        contact = contacts.json()["Contacts"][0]
        invoice_response = client.post(
            base + "/Invoices",
            params={"unitdp": 4},
            json={
                "Invoices": [
                    {
                        "Type": "ACCREC",
                        "Contact": {"ContactID": contact["ContactID"]},
                        "Date": "2026-10-16",
                        "DueDate": "2026-11-15",
                        "Status": "AUTHORISED",
                        "LineItems": [
                            {
                                "Description": "Precision contract",
                                "Quantity": 100,
                                "UnitAmount": 0.015,
                                "AccountCode": "4200",
                            }
                        ],
                    }
                ],
            },
        )
        invoice_response.raise_for_status()
        invoice = invoice_response.json()["Invoices"][0]
        assert invoice["Total"] == 1.5
        payment = client.put(
            base + "/Payments",
            json={
                "Invoice": {"InvoiceNumber": invoice["InvoiceNumber"]},
                "Account": {"Code": "1000"},
                "Date": "2026-10-16",
                "Amount": 1.5,
            },
        )
        payment.raise_for_status()
        settled = client.get(base + "/Invoices/" + invoice["InvoiceID"], params={"unitdp": 4})
        settled.raise_for_status()
        assert settled.json()["Invoices"][0]["Status"] == "PAID"
        assert settled.json()["Invoices"][0]["LineItems"][0]["UnitAmount"] == 0.015
        assert client.get(base + "/CreditNotes").status_code == 200
        assert client.get(base + "/BankTransactions").status_code == 401
