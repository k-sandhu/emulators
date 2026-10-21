"""Adversarial regressions for real-client failures found during the fidelity audit."""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from emulators.testing.helpers import mint

from .conftest import API, BASE, create_invoice
from .test_identity import IDENTITY, authorize, exchange
from .test_reports import fetch, total

WRITABLE = [
    ("Contacts", "ContactID"),
    ("ContactGroups", "ContactGroupID"),
    ("Accounts", "AccountID"),
    ("Items", "ItemID"),
    ("Invoices", "InvoiceID"),
    ("CreditNotes", "CreditNoteID"),
    ("Quotes", "QuoteID"),
    ("Payments", "PaymentID"),
    ("ManualJournals", "ManualJournalID"),
    ("BankTransactions", "BankTransactionID"),
    ("BankTransfers", "BankTransferID"),
    ("TrackingCategories", "TrackingCategoryID"),
]


@pytest.mark.parametrize("resource", ["Invoices", "CreditNotes"])
async def test_settled_transactions_cannot_change_line_amount_types(
    client: httpx.AsyncClient, resource: str
) -> None:
    taxes = (await client.get(BASE + "/TaxRates")).json()["TaxRates"]
    tax = next(t for t in taxes if t["EffectiveRate"] == 13 and t["ReportTaxType"] == "OUTPUT")
    lines = [{"Quantity": 1, "UnitAmount": 100, "AccountCode": "4200", "TaxType": tax["TaxType"]}]
    invoice = await create_invoice(client, LineItems=lines)
    if resource == "Invoices":
        path = BASE + "/Invoices/" + invoice["InvoiceID"]
        paid = await client.put(
            BASE + "/Payments",
            json={
                "Invoice": {"InvoiceID": invoice["InvoiceID"]},
                "Account": {"Code": "1000"},
                "Date": "2026-10-16",
                "Amount": invoice["Total"],
            },
        )
        assert paid.status_code == 200, paid.text
    else:
        credit = await client.put(
            BASE + "/CreditNotes",
            json={
                "Type": "ACCRECCREDIT",
                "Contact": invoice["Contact"],
                "Date": "2026-10-16",
                "Status": "AUTHORISED",
                "LineItems": lines,
            },
        )
        assert credit.status_code == 200, credit.text
        path = BASE + "/CreditNotes/" + credit.json()["CreditNotes"][0]["CreditNoteID"]
        allocated = await client.put(
            path + "/Allocations",
            json={"Allocations": [{"Invoice": {"InvoiceID": invoice["InvoiceID"]}, "Amount": 100}]},
        )
        assert allocated.status_code == 200, allocated.text
    before = (await client.get(path)).json()[resource]
    rejected = await client.post(path, json={"LineAmountTypes": "Inclusive"})
    assert rejected.status_code == 400, rejected.text
    assert (await client.get(path)).json()[resource] == before


async def test_line_amount_type_change_recomputes_generated_tax(client: httpx.AsyncClient) -> None:
    taxes = (await client.get(BASE + "/TaxRates")).json()["TaxRates"]
    tax = next(t for t in taxes if t["EffectiveRate"] == 13 and t["ReportTaxType"] == "OUTPUT")
    invoice = await create_invoice(
        client,
        LineItems=[{"Quantity": 1, "UnitAmount": 100, "AccountCode": "4200", "TaxType": tax["TaxType"]}],
    )
    path = BASE + "/Invoices/" + invoice["InvoiceID"]
    for mode, total_amount, tax_amount in [("Inclusive", 100, 11.5), ("Exclusive", 113, 13)]:
        response = await client.post(path, json={"LineAmountTypes": mode})
        assert response.status_code == 200, response.text
        changed = response.json()["Invoices"][0]
        assert changed["Total"] == total_amount and changed["TotalTax"] == tax_amount
        assert changed["AmountDue"] == total_amount


@pytest.mark.parametrize(("resource", "field"), WRITABLE)
async def test_wrong_id_types_are_validation_errors(
    client: httpx.AsyncClient, resource: str, field: str
) -> None:
    for value in [True, 123, 1e308, {}, [], None]:
        method = "PUT" if resource in {"Accounts", "ContactGroups", "TrackingCategories"} else "POST"
        response = await client.request(method, BASE + "/" + resource, json={field: value})
        assert response.status_code == 400, response.text
        assert response.json()["ErrorNumber"] == 10


@pytest.mark.parametrize(("resource", "field"), WRITABLE)
async def test_every_resource_rejects_malformed_bodies(
    client: httpx.AsyncClient, resource: str, field: str
) -> None:
    for body in [
        b"",
        b"not-json",
        b"\xff",
        b"[]",
        b"null",
        b"123",
        b"{}",
        b'{"Name":NaN}',
        b'{"Name":1e309}',
    ]:
        response = await client.put(
            BASE + "/" + resource, content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400, response.text
        assert response.json()["Type"] == "ValidationException"


@pytest.mark.parametrize(
    ("resource", "body"),
    [
        ("Invoices", {"Type": []}),
        ("Invoices", {"Status": {}}),
        ("Invoices", {"LineAmountTypes": []}),
        ("Contacts", {"Name": "Bad", "ContactStatus": []}),
        ("Accounts", {"Name": "Bad", "Code": "9999", "Type": {}}),
        ("Items", {"Code": "BAD", "SalesDetails": None}),
        ("Items", {"Code": "BAD", "PurchaseDetails": []}),
        ("Items", {"Code": "BAD", "SalesDetails": {"UnitPrice": 1e308}}),
        ("Contacts", {"Name": "Bad", "Phones": [123]}),
        ("Contacts", {"Name": "Bad", "Addresses": {}}),
        ("Contacts", {"Name": "Bad", "PaymentTerms": []}),
        ("CreditNotes", {"Type": {}}),
        ("BankTransactions", {"Type": []}),
        ("ManualJournals", {"Status": []}),
        ("Invoices", {"Contact": {"ContactID": 123}}),
        ("Invoices", {"LineItems": [{"ItemCode": []}]}),
        ("Invoices", {"LineItems": [{"Quantity": True}]}),
        ("Invoices", {"LineItems": [{"Description": 123}]}),
        ("Invoices", {"LineItems": [{"UnitAmount": 1e308}]}),
    ],
)
async def test_nested_shapes_and_enums_do_not_crash_or_mutate(
    client: httpx.AsyncClient, resource: str, body: dict[str, Any]
) -> None:
    store = client.hub.context("xero").store
    before = store.collection(resource).values()
    method = "PUT" if resource in {"Accounts", "ContactGroups", "TrackingCategories"} else "POST"
    response = await client.request(method, BASE + "/" + resource, json=body)
    assert response.status_code == 400, response.text
    assert store.collection(resource).values() == before


async def test_secondary_writes_validate_before_mutating(client: httpx.AsyncClient) -> None:
    invoice = await create_invoice(client)
    group = (await client.get(BASE + "/ContactGroups")).json()["ContactGroups"][0]
    category = (await client.get(BASE + "/TrackingCategories")).json()["TrackingCategories"][0]
    cases = [
        (f"/Invoices/{invoice['InvoiceID']}/History", {"HistoryRecords": [{"Details": []}]}),
        (f"/ContactGroups/{group['ContactGroupID']}/Contacts", {"Contacts": [None]}),
        (f"/ContactGroups/{group['ContactGroupID']}/Contacts", {"Contacts": [{"ContactID": True}]}),
        (f"/TrackingCategories/{category['TrackingCategoryID']}/Options", {"Name": {}}),
    ]
    for path, body in cases:
        response = await client.put(BASE + path, json=body)
        assert response.status_code == 400, response.text
    for path, _ in cases:
        response = await client.put(BASE + path, content=b"broken-json")
        assert response.status_code == 400


async def test_calculated_invoice_values_cannot_desynchronise_the_ledger(client: httpx.AsyncClient) -> None:
    invoice = await create_invoice(client)
    path = BASE + "/Invoices/" + invoice["InvoiceID"]
    before = await fetch(client, "BalanceSheet", date="2026-10-16")
    response = await client.post(
        path,
        json={
            "Total": 999999,
            "SubTotal": -500,
            "TotalTax": 10000,
            "AmountPaid": 999999,
            "AmountCredited": 999999,
            "AmountDue": 0,
            "Payments": [{"Amount": 999999}],
            "HasAttachments": True,
        },
    )
    assert response.status_code == 200
    result = response.json()["Invoices"][0]
    assert (result["Total"], result["SubTotal"], result["TotalTax"]) == (100, 100, 0)
    assert (result["AmountDue"], result["AmountPaid"], result["AmountCredited"]) == (100, 0, 0)
    assert result["Payments"] == [] and result["HasAttachments"] is False
    after = await fetch(client, "BalanceSheet", date="2026-10-16")
    assert total(before, "Total Assets") == total(after, "Total Assets")


async def test_guid_casing_updates_existing_instead_of_creating_duplicate(client: httpx.AsyncClient) -> None:
    invoice = await create_invoice(client)
    response = await client.post(
        BASE + "/Invoices",
        json={
            "InvoiceID": invoice["InvoiceID"].upper(),
            "Reference": "Updated through uppercase GUID",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["Invoices"][0]["InvoiceID"] == invoice["InvoiceID"]
    listed = (await client.get(BASE + "/Invoices", params={"IDs": invoice["InvoiceID"], "page": 1})).json()
    assert len(listed["Invoices"]) == 1
    assert listed["Invoices"][0]["Reference"] == "Updated through uppercase GUID"
    assert (
        await client.put(
            BASE + "/Invoices",
            json={"InvoiceID": invoice["InvoiceID"].upper(), "Reference": "PUT must not update"},
        )
    ).status_code == 400


async def test_line_amount_inference_description_lines_account_id_and_date_strings(
    client: httpx.AsyncClient,
) -> None:
    account = (await client.get(BASE + "/Accounts/4200")).json()["Accounts"][0]
    invoice = await create_invoice(
        client,
        LineItems=[
            {"Description": "Scope of work — café 東京"},
            {
                "Description": "Derived unit price",
                "Quantity": 4,
                "LineAmount": 100,
                "AccountID": account["AccountID"],
            },
        ],
    )
    assert invoice["Total"] == 100
    assert invoice["LineItems"][1]["UnitAmount"] == 25
    assert invoice["LineItems"][1]["AccountCode"] == "4200"
    response = await client.post(
        BASE + "/Invoices/" + invoice["InvoiceID"],
        json={
            "DateString": "2026-10-15T00:00:00",
            "DueDateString": "2026-11-20T00:00:00",
        },
    )
    assert response.status_code == 200
    assert response.json()["Invoices"][0]["DateString"] == "2026-10-15T00:00:00"
    assert response.json()["Invoices"][0]["DueDateString"] == "2026-11-20T00:00:00"
    pdf = await client.get(BASE + "/Invoices/" + invoice["InvoiceID"], headers={"Accept": "application/pdf"})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    trial = await fetch(client, "TrialBalance", date="2026-10-16")
    assert total(trial, "Total", 3) == total(trial, "Total", 4)


async def test_unitdp_controls_rounding_before_multiplication(client: httpx.AsyncClient) -> None:
    invoice = await create_invoice(client, Status="DRAFT")
    body = {
        "Contact": invoice["Contact"],
        "Type": "ACCREC",
        "LineItems": [
            {"Quantity": 100, "UnitAmount": "0.015", "AccountCode": "4200"},
        ],
    }
    for unitdp, expected in [(2, 2), (4, 1.5)]:
        response = await client.post(BASE + "/Invoices", params={"unitdp": unitdp}, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["Invoices"][0]["Total"] == expected
        if unitdp == 4:
            path = BASE + "/Invoices/" + response.json()["Invoices"][0]["InvoiceID"]
            assert (await client.get(path)).json()["Invoices"][0]["LineItems"][0]["UnitAmount"] == 0.02
            exact = await client.get(path, params={"unitdp": 4})
            assert exact.json()["Invoices"][0]["LineItems"][0]["UnitAmount"] == 0.015
            assert exact.json()["Invoices"][0]["Total"] == 1.5
    assert (await client.post(BASE + "/Invoices", params={"unitdp": 9}, json=body)).status_code == 400


async def test_payments_accept_invoice_number_and_bank_without_payment_flag(
    client: httpx.AsyncClient,
) -> None:
    invoice = await create_invoice(client)
    bank = (await client.get(BASE + "/Accounts/1000")).json()["Accounts"][0]
    assert (
        await client.post(
            BASE + "/Accounts/" + bank["AccountID"],
            json={
                "EnablePaymentsToAccount": False,
            },
        )
    ).status_code == 200
    payment = await client.put(
        BASE + "/Payments",
        json={
            "Invoice": {"InvoiceNumber": invoice["InvoiceNumber"]},
            "Account": {"Code": "1000"},
            "Amount": 100,
            "Date": "2026-10-16",
        },
    )
    assert payment.status_code == 200, payment.text
    result = (await client.get(BASE + "/Invoices/" + invoice["InvoiceID"])).json()["Invoices"][0]
    assert result["Status"] == "PAID" and result["AmountDue"] == 0


async def test_batch_error_shapes_and_parameter_casing(client: httpx.AsyncClient) -> None:
    invoice = await create_invoice(client)
    response = await client.post(
        BASE + "/Invoices",
        params={"SummarizeErrors": "false"},
        json={
            "Invoices": [
                {
                    "Type": "ACCREC",
                    "Contact": invoice["Contact"],
                    "LineItems": [
                        {"Quantity": 1, "UnitAmount": 20, "AccountCode": "4200"},
                    ],
                },
                {"Type": []},
            ],
        },
    )
    assert response.status_code == 200, response.text
    good, bad = response.json()["Invoices"]
    assert good["StatusAttributeString"] == "OK" and good["HasErrors"] is False
    assert bad["StatusAttributeString"] == "ERROR" and bad["HasErrors"] is True
    assert bad["ValidationErrors"]


async def test_numeric_date_and_secondary_ordering_and_bad_queries(client: httpx.AsyncClient) -> None:
    listed = (
        await client.get(BASE + "/Invoices", params={"page": 1, "pageSize": 1000, "order": "AmountDue"})
    ).json()
    amounts = [r["AmountDue"] for r in listed["Invoices"]]
    assert amounts == sorted(amounts)
    for amount in set(amounts):
        identifiers = [r["InvoiceID"] for r in listed["Invoices"] if r["AmountDue"] == amount]
        assert identifiers == sorted(identifiers)
    for expression in ["Contact", "LineItems", "Date,,", ""]:
        response = await client.get(BASE + "/Invoices", params={"order": expression})
        assert response.status_code == 400, response.text
    chain = " OR ".join(["true"] * 999)
    response = await client.get(BASE + "/Invoices", params={"where": chain})
    assert response.status_code == 400
    invoice = await create_invoice(client, Date="1999-01-01", DueDate="1999-01-31")
    other = await create_invoice(client, Date="2005-01-01", DueDate="2005-01-31")
    response = await client.get(
        BASE + "/Invoices",
        params={
            "IDs": invoice["InvoiceID"] + "," + other["InvoiceID"],
            "page": 1,
            "order": "Date ASC",
        },
    )
    assert [r["InvoiceID"] for r in response.json()["Invoices"]] == [invoice["InvoiceID"], other["InvoiceID"]]
    overflow = await client.post(
        BASE + "/Invoices/" + invoice["InvoiceID"], json={"Date": "/Date(99999999999999999999)/"}
    )
    assert overflow.status_code == 400


async def test_contact_search_and_quote_filters_match_vendor_parameters(client: httpx.AsyncClient) -> None:
    response = await client.get(BASE + "/Contacts", params={"searchTerm": "christopher.ingram@northwind"})
    assert [c["ContactNumber"] for c in response.json()["Contacts"]] == ["c001"]
    quotes = (await client.get(BASE + "/Quotes")).json()["Quotes"]
    target = quotes[0]
    response = await client.get(
        BASE + "/Quotes",
        params={
            "ContactID": target["Contact"]["ContactID"],
            "Status": target["Status"],
            "QuoteNumber": target["QuoteNumber"],
            "DateFrom": "1900-01-01",
            "DateTo": "2100-01-01",
        },
    )
    assert [q["QuoteID"] for q in response.json()["Quotes"]] == [target["QuoteID"]]
    assert (await client.get(BASE + "/Quotes", params={"DateFrom": "2100-01-01"})).json()["Quotes"] == []


async def test_granular_scope_resource_groups_and_tenant_optional_custom_connection(
    client: httpx.AsyncClient,
) -> None:
    token = await mint(
        client, API, app_only=True, scopes=["accounting.invoices.read"], client_id="granular-audit"
    )
    auth = {"Authorization": "Bearer " + token, "xero-tenant-id": ""}
    for resource in ["Invoices", "CreditNotes", "Quotes", "Items"]:
        response = await client.get(BASE + "/" + resource, headers=auth)
        assert response.status_code == 200, response.text
        assert "x-minlimit-remaining" in response.headers
        UUID(response.headers["xero-correlation-id"])
    assert (await client.get(BASE + "/Payments", headers=auth)).status_code == 401
    assert (await client.post(BASE + "/Items", headers=auth, json={"Code": "DENIED"})).status_code == 401
    bank = await mint(
        client, API, app_only=True, scopes=["accounting.banktransactions.read"], client_id="bank-audit"
    )
    assert (
        await client.get(BASE + "/BankTransfers", headers={"Authorization": "Bearer " + bank})
    ).status_code == 200
    aged = await mint(
        client, API, app_only=True, scopes=["accounting.reports.aged.read"], client_id="aged-audit"
    )
    invoice = await create_invoice(client)
    for name in ["AgedReceivablesByContact", "AgedPayablesByContact"]:
        response = await client.get(
            BASE + "/Reports/" + name,
            params={"contactId": invoice["Contact"]["ContactID"]},
            headers={"Authorization": "Bearer " + aged},
        )
        assert response.status_code == 200, response.text


async def test_refresh_revoke_removes_connections_and_honours_grace_boundary(
    client: httpx.AsyncClient,
) -> None:
    tokens = (await exchange(client, await authorize(client))).json()
    data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
    rotated = await client.post(
        IDENTITY + "/connect/token", auth=("xero-test-client", "xero-test-secret"), data=data
    )
    assert rotated.status_code == 200
    client.hub.clock.advance(timedelta(seconds=1800))
    expired = await client.post(
        IDENTITY + "/connect/token", auth=("xero-test-client", "xero-test-secret"), data=data
    )
    assert expired.status_code == 400
    other = await mint(client, API, user="p001", client_id="xero-test-client")
    revoked = await client.post(
        IDENTITY + "/connect/revocation",
        auth=("xero-test-client", "xero-test-secret"),
        data={"token": rotated.json()["refresh_token"]},
    )
    assert revoked.status_code == 200
    # Another grant family cannot retain access through stale connections.
    assert (await client.get("/connections", headers={"Authorization": "Bearer " + other})).json() == []
    assert (
        await client.get(BASE + "/Invoices", headers={"Authorization": "Bearer " + other})
    ).status_code == 403


async def test_pkce_public_client_can_refresh_without_secret_in_strict_mode(
    client: httpx.AsyncClient,
) -> None:
    ctx = client.hub.context("xero")
    ctx.tokens.lenient_clients = False
    try:
        verifier = "p" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        code = await authorize(client, code_challenge=challenge, code_challenge_method="S256")
        token = await client.post(
            IDENTITY + "/connect/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "xero-test-client",
                "code": code,
                "redirect_uri": "https://consumer.example/callback",
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        response = await client.post(
            IDENTITY + "/connect/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "xero-test-client",
                "refresh_token": token.json()["refresh_token"],
            },
        )
        assert response.status_code == 200, response.text
    finally:
        ctx.tokens.lenient_clients = True


async def test_contact_rename_refreshes_transaction_references_and_cached_pdf(
    client: httpx.AsyncClient,
) -> None:
    invoice = await create_invoice(client)
    path = BASE + "/Invoices/" + invoice["InvoiceID"]
    before = (await client.get(path, headers={"Accept": "application/pdf"})).content
    response = await client.post(
        BASE + "/Contacts/" + invoice["Contact"]["ContactID"], json={"Name": "Northwind Logistics Updated"}
    )
    assert response.status_code == 200
    assert (await client.get(path)).json()["Invoices"][0]["Contact"]["Name"] == "Northwind Logistics Updated"
    after = (await client.get(path, headers={"Accept": "application/pdf"})).content
    assert before != after and b"Northwind Logistics Updated" in after
    demo = next(
        t["tenantId"]
        for t in (await client.get("/connections")).json()
        if t["tenantName"] == "Demo Company (US)"
    )
    headers = {"xero-tenant-id": demo}
    contact = (
        await client.put(BASE + "/Contacts", headers=headers, json={"Name": "Demo PDF customer"})
    ).json()["Contacts"][0]
    draft = (
        await client.post(
            BASE + "/Invoices",
            headers=headers,
            json={"Type": "ACCREC", "Contact": {"ContactID": contact["ContactID"]}},
        )
    ).json()["Invoices"][0]
    pdf = await client.get(
        BASE + "/Invoices/" + draft["InvoiceID"], headers={**headers, "Accept": "application/pdf"}
    )
    assert pdf.status_code == 200 and b"Demo Company" in pdf.content


async def test_bank_transfer_exposes_both_sides_without_double_posting(client: httpx.AsyncClient) -> None:
    accounts = (
        await client.get(BASE + "/Accounts", params={"where": 'Type=="BANK" AND Class=="ASSET"'})
    ).json()["Accounts"]
    before = await fetch(client, "BalanceSheet", date="2026-10-16")
    cash_before = await fetch(client, "BankSummary", fromDate="2026-10-16", toDate="2026-10-16")
    response = await client.put(
        BASE + "/BankTransfers",
        json={
            "FromBankAccount": {"AccountID": accounts[0]["AccountID"]},
            "ToBankAccount": {"AccountID": accounts[1]["AccountID"]},
            "Amount": 50,
            "Date": "2026-10-16",
            "Reference": "AUDIT-TRANSFER",
        },
    )
    assert response.status_code == 200, response.text
    transfer = response.json()["BankTransfers"][0]
    for side, kind in [("From", "SPEND-TRANSFER"), ("To", "RECEIVE-TRANSFER")]:
        transaction = (
            await client.get(BASE + "/BankTransactions/" + transfer[side + "BankTransactionID"])
        ).json()["BankTransactions"][0]
        assert transaction["Type"] == kind and transaction["Total"] == 50
    after = await fetch(client, "BalanceSheet", date="2026-10-16")
    assert total(before, "Total Assets") == total(after, "Total Assets")
    cash_after = await fetch(client, "BankSummary", fromDate="2026-10-16", toDate="2026-10-16")
    assert total(cash_after, accounts[0]["Name"], 4) == total(cash_before, accounts[0]["Name"], 4) - 50
    assert total(cash_after, accounts[1]["Name"], 4) == total(cash_before, accounts[1]["Name"], 4) + 50
    deleted = await client.post(
        BASE + "/BankTransfers/" + transfer["BankTransferID"], json={"Status": "DELETED"}
    )
    assert deleted.status_code == 200, deleted.text
    for side in ["From", "To"]:
        transaction = (
            await client.get(BASE + "/BankTransactions/" + transfer[side + "BankTransactionID"])
        ).json()["BankTransactions"][0]
        assert transaction["Status"] == "DELETED"
    reversed_cash = await fetch(client, "BankSummary", fromDate="2026-10-16", toDate="2026-10-16")
    assert total(reversed_cash, accounts[0]["Name"], 4) == total(cash_before, accounts[0]["Name"], 4)
    assert (
        await client.post(
            BASE + "/BankTransfers",
            json={
                "FromBankAccount": {"Code": accounts[0]["Code"]},
                "ToBankAccount": {"Code": accounts[1]["Code"]},
                "Amount": 50,
            },
        )
    ).status_code == 400


async def test_voided_journals_stay_voided_and_tracking_reactivation_obeys_limit(
    client: httpx.AsyncClient,
) -> None:
    journal = (await client.get(BASE + "/ManualJournals")).json()["ManualJournals"][0]
    path = BASE + "/ManualJournals/" + journal["ManualJournalID"]
    assert (await client.post(path, json={"Status": "VOIDED"})).status_code == 200
    assert (await client.post(path, json={"Status": "POSTED"})).status_code == 400
    categories = (await client.get(BASE + "/TrackingCategories")).json()["TrackingCategories"]
    path = BASE + "/TrackingCategories/" + categories[0]["TrackingCategoryID"]
    assert (await client.post(path, json={"Status": "ARCHIVED"})).status_code == 200
    created = await client.put(
        BASE + "/TrackingCategories", json={"Name": "Audit category", "Options": [{"Name": "Audit option"}]}
    )
    assert created.status_code == 200, created.text
    UUID(created.json()["TrackingCategories"][0]["Options"][0]["TrackingOptionID"])
    assert (await client.post(path, json={"Status": "ACTIVE"})).status_code == 400
    assert (await client.post(path, json={"Options": [{}]})).status_code == 400


async def test_bill_numbers_can_repeat_between_suppliers(client: httpx.AsyncClient) -> None:
    contacts = (await client.get(BASE + "/Contacts")).json()["Contacts"]
    suppliers = [c for c in contacts if c["IsSupplier"]][:2]
    for supplier in suppliers:
        await create_invoice(
            client, Type="ACCPAY", InvoiceNumber="SUPPLIER-001", Contact={"ContactID": supplier["ContactID"]}
        )
    response = await client.post(
        BASE + "/Invoices",
        json={
            "Type": "ACCPAY",
            "InvoiceNumber": "SUPPLIER-001",
            "Contact": {"ContactID": suppliers[0]["ContactID"]},
            "LineItems": [{"Quantity": 1, "UnitAmount": 10, "AccountCode": "6200"}],
        },
    )
    assert response.status_code == 400


async def test_fault_headers_and_idempotency_query_changes(client: httpx.AsyncClient) -> None:
    await client.post("/_emulator/faults", json={"path": BASE + "/Invoices", "status": 429, "times": 1})
    fault = await client.get(BASE + "/Invoices")
    assert fault.status_code == 429 and fault.headers["retry-after"] == "60"
    UUID(fault.headers["xero-correlation-id"])
    invoice = await create_invoice(client)
    data = {
        "Type": "ACCREC",
        "Contact": invoice["Contact"],
        "LineItems": [{"Quantity": 100, "UnitAmount": 0.015, "AccountCode": "4200"}],
    }
    headers = {"Idempotency-Key": "audit-rounding"}
    first = await client.post(BASE + "/Invoices", params={"unitdp": 2}, headers=headers, json=data)
    assert first.status_code == 200
    assert (
        await client.post(BASE + "/Invoices", params={"unitdp": 2}, headers=headers, json=data)
    ).json() == first.json()
    assert (
        await client.post(BASE + "/Invoices", params={"unitdp": 4}, headers=headers, json=data)
    ).status_code == 400
    other = await mint(client, API, user="p001", client_id="other-idempotency-app")
    repeated = await client.post(
        BASE + "/Invoices",
        params={"unitdp": 2},
        json=data,
        headers={**headers, "Authorization": "Bearer " + other},
    )
    assert repeated.status_code == 200
    assert repeated.json()["Invoices"][0]["InvoiceID"] != first.json()["Invoices"][0]["InvoiceID"]


@pytest.mark.parametrize(
    "name", ["ProfitAndLoss", "BalanceSheet", "TrialBalance", "BankSummary", "ExecutiveSummary"]
)
async def test_report_date_underflow_returns_validation(client: httpx.AsyncClient, name: str) -> None:
    response = await client.get(BASE + "/Reports/" + name, params={"date": "0001-01-01", "periods": 12})
    assert response.status_code == 400
    assert response.json()["ErrorNumber"] == 10


async def test_huge_datetime_filter_and_oauth_body_types_are_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get(
        BASE + "/Invoices", params={"where": "Date>=DateTime(999999999999999999999,1,1)"}
    )
    assert response.status_code == 400
    deeply_nested: Any = {}
    for _ in range(50):
        deeply_nested = [deeply_nested]
    assert (
        await client.post(BASE + "/Contacts", json={"Name": "Nested", "Unknown": deeply_nested})
    ).status_code == 400
    for content in [b'{"Name":"\\ud800"}', b'{"Name":"Valid name","\\ud800":true}']:
        invalid_unicode = await client.post(
            BASE + "/Contacts", content=content, headers={"Content-Type": "application/json"}
        )
        assert invalid_unicode.status_code == 400
    partial = await client.post(
        BASE + "/Contacts",
        params={"summarizeErrors": "false"},
        content=b'{"Contacts":[{"Name":"\\ud800"}]}',
        headers={"Content-Type": "application/json"},
    )
    assert partial.status_code == 200 and partial.json()["Contacts"][0]["HasValidationErrors"] is True
    very_deep = b'{"Contacts":[{"Name":"Too deep","Extra":' + b"[" * 600 + b"0" + b"]" * 600 + b"}]}"
    partial = await client.post(
        BASE + "/Contacts",
        params={"summarizeErrors": "false"},
        content=very_deep,
        headers={"Content-Type": "application/json"},
    )
    assert partial.status_code in {200, 400}
    for path in ["/connect/token", "/connect/revocation"]:
        for body in [{"grant_type": "client_credentials"}, [1], None]:
            response = await client.post(IDENTITY + path, json=body)
            assert response.status_code == 400
            assert response.json()["error"] == "invalid_request"
        response = await client.post(IDENTITY + path, files={"client_id": ("audit.txt", b"invalid client")})
        assert response.status_code == 400
    # A rejected upload must not save an uncopyable file object in the OAuth registry.
    assert (
        await client.post(BASE + "/Contacts", json={"Name": "Contact after malformed OAuth"})
    ).status_code == 200


async def test_files_scope_cannot_access_accounting_attachments_or_use_wrong_auth_scheme(
    client: httpx.AsyncClient,
) -> None:
    invoice = await create_invoice(client)
    token = await mint(client, API, user="p001", scopes=["files"])
    path = BASE + "/Invoices/" + invoice["InvoiceID"] + "/Attachments"
    assert (await client.get(path, headers={"Authorization": "Bearer " + token})).status_code == 401
    bearer = client.headers["Authorization"].split(" ", 1)[1]
    assert (
        await client.get(BASE + "/Invoices", headers={"Authorization": "OAuth " + bearer})
    ).status_code == 401


@pytest.mark.parametrize(("resource", "kind"), [("Invoices", "ACCREC"), ("CreditNotes", "ACCRECCREDIT")])
async def test_minimum_draft_documents_allow_no_lines_but_require_type(
    client: httpx.AsyncClient, resource: str, kind: str
) -> None:
    contact = (await client.get(BASE + "/Contacts", params={"where": 'ContactNumber=="c001"'})).json()[
        "Contacts"
    ][0]
    body = {"Type": kind, "Contact": {"ContactID": contact["ContactID"]}}
    draft = await client.post(BASE + "/" + resource, json=body)
    assert draft.status_code == 200, draft.text
    record = draft.json()[resource][0]
    assert record["Status"] == "DRAFT" and record["LineItems"] == [] and record["Total"] == 0
    field = "InvoiceID" if resource == "Invoices" else "CreditNoteID"
    assert (
        await client.post(BASE + "/" + resource + "/" + record[field], json={"Status": "AUTHORISED"})
    ).status_code == 400
    body.pop("Type")
    assert (await client.post(BASE + "/" + resource, json=body)).status_code == 400


async def test_resource_methods_and_contact_group_memberships(client: httpx.AsyncClient) -> None:
    for resource in ["Accounts", "ContactGroups", "TrackingCategories"]:
        assert (await client.post(BASE + "/" + resource, json={})).status_code == 405
    invoice = await create_invoice(client)
    created = await client.put(BASE + "/ContactGroups", json={"Name": "Audit membership"})
    group = created.json()["ContactGroups"][0]
    path = BASE + "/ContactGroups/" + group["ContactGroupID"]
    assert (await client.delete(path)).status_code == 405
    assert (await client.put(path + "/Contacts", json={"Contacts": [invoice["Contact"]]})).status_code == 200
    assert (await client.post(path, json={"Name": "Renamed audit membership"})).status_code == 200
    contact_path = BASE + "/Contacts/" + invoice["Contact"]["ContactID"]
    member = next(
        g
        for g in (await client.get(contact_path)).json()["Contacts"][0]["ContactGroups"]
        if g["ContactGroupID"] == group["ContactGroupID"]
    )
    assert member["Name"] == "Renamed audit membership"
    assert (await client.post(path, json={"Status": "DELETED"})).status_code == 200
    assert all(
        g["ContactGroupID"] != group["ContactGroupID"]
        for g in (await client.get(contact_path)).json()["Contacts"][0]["ContactGroups"]
    )


async def test_control_actions_reject_types_that_would_poison_grants(client: httpx.AsyncClient) -> None:
    for data in [
        {"user": {}},
        {"scopes": [[]]},
        {"scopes": "accounting.invoices"},
        {"client_id": []},
        {"expires_in": 1e308},
        {"expires_in": None},
        {"app_only": "false"},
    ]:
        response = await client.post("/_emulator/tokens", json=data)
        assert response.status_code == 400, response.text
    response = await client.post(
        "/_emulator/actions/configure_webhook",
        content=b'{"url":"https://consumer.example/hook","signing_key":"\\ud800"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    token = await mint(client, API, user="p001")
    assert (
        await client.get(BASE + "/Invoices", headers={"Authorization": "Bearer " + token})
    ).status_code == 200
