from __future__ import annotations

import io
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from pypdf import PdfReader

from .conftest import BASE, create_invoice


@pytest.mark.parametrize(
    ("resource", "key", "count"),
    [
        ("Organisation", "Organisations", 1),
        ("Accounts", "Accounts", 41),
        ("Contacts", "Contacts", 37),
        ("ContactGroups", "ContactGroups", 2),
        ("Items", "Items", 10),
        ("Invoices", "Invoices", 267),
        ("Payments", "Payments", 243),
        ("BankTransactions", "BankTransactions", 12),
        ("BankTransfers", "BankTransfers", 0),
        ("CreditNotes", "CreditNotes", 0),
        ("ManualJournals", "ManualJournals", 11),
        ("Quotes", "Quotes", 10),
        ("TrackingCategories", "TrackingCategories", 2),
        ("TaxRates", "TaxRates", 12),
        ("Currencies", "Currencies", 3),
        ("BrandingThemes", "BrandingThemes", 1),
        ("Users", "Users", 45),
    ],
)
async def test_resource_collections(client: httpx.AsyncClient, resource: str, key: str, count: int) -> None:
    response = await client.get(BASE + "/" + resource)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["Status"] == "OK"
    UUID(body["Id"])
    assert body["DateTimeUTC"].startswith("/Date(")
    assert len(body[key]) == count
    assert "_tenant" not in response.text
    assert response.headers["x-minlimit-remaining"] == "59"
    if resource == "Organisation":
        assert body[key][0]["BaseCurrency"] == "USD"
        assert body[key][0]["ShortCode"] == "!xK2Lm"
    if resource == "Users":
        assert {u["OrganisationRole"] for u in body[key]} == {"ADMIN", "STANDARD", "READONLY"}
        assert sum(u["IsSubscriber"] for u in body[key]) == 1


async def test_query_language_paging_ids_order_dates_summary_and_archived(client: httpx.AsyncClient) -> None:
    params = {
        "page": 1,
        "pageSize": 3,
        "where": 'Type=="ACCREC" AND (Status=="AUTHORISED" OR Status=="PAID")',
        "order": "Date DESC",
    }
    response = await client.get(BASE + "/Invoices", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["Invoices"]) == 3
    assert body["pagination"]["pageSize"] == 3
    assert body["pagination"]["itemCount"] > 3
    assert "LineItems" in body["Invoices"][0]
    first = body["Invoices"][0]
    second = (await client.get(BASE + "/Invoices", params={**params, "page": 2})).json()
    assert {i["InvoiceID"] for i in body["Invoices"]}.isdisjoint(i["InvoiceID"] for i in second["Invoices"])
    assert body["Invoices"] == sorted(body["Invoices"], key=lambda i: i["Date"], reverse=True)
    params = {
        "IDs": first["InvoiceID"],
        "InvoiceNumbers": first["InvoiceNumber"],
        "ContactIDs": first["Contact"]["ContactID"],
        "Statuses": first["Status"],
        "pageSize": 5000,
    }
    selected = (await client.get(BASE + "/Invoices", params=params)).json()
    assert len(selected["Invoices"]) == 1
    assert selected["pagination"]["pageSize"] == 1000
    assert (await client.get(BASE + "/Invoices/" + first["InvoiceNumber"])).json()["Invoices"][0][
        "InvoiceID"
    ] == first["InvoiceID"]
    summary = (await client.get(BASE + "/Invoices", params={"summaryOnly": True})).json()
    assert not {"LineItems", "Payments", "HasAttachments"}.intersection(summary["Invoices"][0])
    where = 'Date>=DateTime(2026, 1, 1) AND Contact.Name.Contains("Northwind") AND Type=="ACCREC"'
    matching = await client.get(BASE + "/Invoices", params={"where": where, "page": 1})
    assert matching.status_code == 200, matching.text
    assert matching.json()["Invoices"]
    assert all(
        i["Contact"]["Name"] == "Northwind Logistics" and i["DateString"] >= "2026"
        for i in matching.json()["Invoices"]
    )
    assert (
        await client.get(BASE + "/Invoices", headers={"If-Modified-Since": "2026-10-17T00:00:00"})
    ).json()["Invoices"] == []
    all_contacts = (await client.get(BASE + "/Contacts", params={"includeArchived": True})).json()["Contacts"]
    assert len(all_contacts) == 38
    assert any(c["ContactStatus"] == "ARCHIVED" for c in all_contacts)
    for invalid in ["Status==", '__import__("os")', "Date>=DateTime(2026,99,1)", 'A == "x" trailing']:
        bad = await client.get(BASE + "/Invoices", params={"where": invalid})
        assert bad.status_code == 400, bad.text
        assert bad.json()["ErrorNumber"] == 10
    assert (await client.get(BASE + "/Invoices", params={"page": 0})).status_code == 400


async def test_world_transactions_and_contact_balances(client: httpx.AsyncClient) -> None:
    invoices = (await client.get(BASE + "/Invoices", params={"page": 1, "pageSize": 1000})).json()["Invoices"]
    world = client.hub.world
    assert len(invoices) == len(world.invoices) + len(world.bills)
    assert sum(i["Type"] == "ACCREC" for i in invoices) == len(world.invoices)
    for invoice in invoices:
        assert sum(line["LineAmount"] for line in invoice["LineItems"]) == pytest.approx(invoice["SubTotal"])
        assert sum(line["TaxAmount"] for line in invoice["LineItems"]) == pytest.approx(invoice["TotalTax"])
        assert invoice["SubTotal"] + invoice["TotalTax"] == pytest.approx(invoice["Total"])
        assert sum(p["Amount"] for p in invoice["Payments"]) == pytest.approx(invoice["AmountPaid"])
        if invoice["Status"] not in {"VOIDED", "DELETED"}:
            assert invoice["AmountDue"] + invoice["AmountPaid"] == pytest.approx(invoice["Total"])
    contact = (await client.get(BASE + "/Contacts", params={"where": 'ContactNumber=="c001"'})).json()[
        "Contacts"
    ][0]
    expected = sum(
        i["AmountDue"]
        for i in invoices
        if i["Type"] == "ACCREC"
        and i["Status"] == "AUTHORISED"
        and i["Contact"]["ContactID"] == contact["ContactID"]
    )
    assert contact["Balances"]["AccountsReceivable"]["Outstanding"] == pytest.approx(expected)
    details = (await client.get(BASE + "/Contacts/" + contact["ContactID"])).json()["Contacts"][0]
    assert len(details["ContactPersons"]) == 2
    assert len(details["ContactGroups"]) >= 1


async def test_invoice_workflow_pay_reverse_delete_and_void(client: httpx.AsyncClient) -> None:
    invoice = await create_invoice(client, Status="DRAFT", InvoiceNumber="INV-XERO-WORKFLOW")
    iid = invoice["InvoiceID"]
    path = BASE + "/Invoices/" + iid
    for status in ["SUBMITTED", "AUTHORISED"]:
        response = await client.post(path, json={"Status": status})
        assert response.status_code == 200, response.text
        assert response.json()["Invoices"][0]["Status"] == status
    payment_data = {
        "Invoice": {"InvoiceID": iid},
        "Account": {"Code": "1000"},
        "Date": "2026-10-16",
        "Amount": 30,
        "Reference": "ACH-TEST",
    }
    paid = await client.put(BASE + "/Payments", json={"Payments": [payment_data]})
    assert paid.status_code == 200, paid.text
    partial_id = paid.json()["Payments"][0]["PaymentID"]
    partial = (await client.get(path)).json()["Invoices"][0]
    assert partial["AmountDue"] == 70
    assert partial["Status"] == "AUTHORISED"
    second = await client.put(BASE + "/Payments", json={**payment_data, "Amount": 70})
    assert second.status_code == 200, second.text
    second_id = second.json()["Payments"][0]["PaymentID"]
    settled = (await client.get(path)).json()["Invoices"][0]
    assert settled["Status"] == "PAID" and settled["AmountDue"] == 0
    assert settled["FullyPaidOnDate"].startswith("/Date(")
    assert (await client.post(path, json={"Status": "VOIDED"})).status_code == 400
    assert (await client.post(BASE + "/Payments/" + second_id, json={"Status": "DELETED"})).status_code == 200
    assert (
        await client.post(BASE + "/Payments/" + partial_id, json={"Status": "DELETED"})
    ).status_code == 200
    assert (await client.post(path, json={"Status": "VOIDED"})).status_code == 200
    assert (await client.get(path)).json()["Invoices"][0]["AmountDue"] == 0
    assert (await client.post(path, json={"Status": "AUTHORISED"})).status_code == 400
    draft = await create_invoice(client, Status="DRAFT")
    assert (
        await client.post(BASE + "/Invoices/" + draft["InvoiceID"], json={"Status": "DELETED"})
    ).status_code == 200


async def test_invoice_tax_tracking_items_validation_and_partial_success(client: httpx.AsyncClient) -> None:
    taxes = (await client.get(BASE + "/TaxRates")).json()["TaxRates"]
    tax = next(t for t in taxes if t["EffectiveRate"] == 13 and t["ReportTaxType"] == "OUTPUT")
    invoice = await create_invoice(
        client,
        LineAmountTypes="Inclusive",
        LineItems=[
            {
                "ItemCode": "SVC-001",
                "Quantity": 2,
                "UnitAmount": 113,
                "DiscountRate": 50,
                "TaxType": tax["TaxType"],
                "Tracking": [{"Name": "Department", "Option": "Audit & Assurance"}],
            }
        ],
    )
    assert (invoice["SubTotal"], invoice["TotalTax"], invoice["Total"]) == (100, 13, 113)
    assert invoice["LineItems"][0]["LineAmount"] == 113
    assert invoice["LineItems"][0]["Tracking"][0]["TrackingOptionID"]
    assert invoice["LineItems"][0]["AccountCode"] == "4000"
    balance = (await client.get(BASE + "/Reports/BalanceSheet", params={"date": "2026-10-16"})).json()[
        "Reports"
    ][0]
    from .test_reports import total

    assert total(balance, "Total Assets") == total(balance, "Total Liabilities") + total(
        balance, "Total Equity"
    )
    invalid = await client.put(BASE + "/Invoices", json={"Type": "ACCREC"})
    assert invalid.status_code == 400
    assert invalid.json()["Type"] == "ValidationException"
    assert invalid.json()["Elements"][0]["ValidationErrors"]
    negative = await client.put(
        BASE + "/Payments", json={"Invoice": {"InvoiceID": invoice["InvoiceID"]}, "Amount": 1000}
    )
    assert negative.status_code == 400
    count = len((await client.get(BASE + "/Contacts")).json()["Contacts"])
    batch = {"Contacts": [{"Name": "Atomic contact"}, {"Name": ""}]}
    assert (await client.put(BASE + "/Contacts", json=batch)).status_code == 400
    assert len((await client.get(BASE + "/Contacts")).json()["Contacts"]) == count
    partial = await client.put(BASE + "/Contacts", params={"summarizeErrors": False}, json=batch)
    assert partial.status_code == 200
    assert partial.json()["Contacts"][1]["HasValidationErrors"]
    assert len((await client.get(BASE + "/Contacts")).json()["Contacts"]) == count + 1


async def test_attachments_pdf_online_email_history_and_cache_invalidation(client: httpx.AsyncClient) -> None:
    invoice = await create_invoice(client)
    path = BASE + "/Invoices/" + invoice["InvoiceID"]
    pdf = await client.get(path, headers={"Accept": "application/pdf"})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert invoice["InvoiceNumber"] in PdfReader(io.BytesIO(pdf.content)).pages[0].extract_text()
    assert (await client.get(path, headers={"Accept": "application/pdf"})).content == pdf.content
    blob_count = len(client.hub.context("xero").store.blobs)
    assert (await client.post(path, json={"Reference": "Updated reference"})).status_code == 200
    assert (await client.get(path, headers={"Accept": "application/pdf"})).status_code == 200
    assert len(client.hub.context("xero").store.blobs) >= blob_count
    upload = await client.put(
        path + "/Attachments/receipt.pdf",
        content=pdf.content,
        headers={"Content-Type": "application/pdf"},
        params={"IncludeOnline": True},
    )
    assert upload.status_code == 200, upload.text
    attachment = upload.json()["Attachments"][0]
    assert attachment["ContentLength"] == len(pdf.content)
    assert attachment["IncludeOnline"] is True
    listed = (await client.get(path + "/Attachments")).json()["Attachments"]
    assert len(listed) == 1
    assert (await client.get(listed[0]["Url"])).content == pdf.content
    assert (await client.get(path + "/Attachments/receipt.pdf")).content == pdf.content
    assert (await client.post(path + "/Attachments/receipt.pdf", content=b"replacement")).status_code == 200
    assert (await client.get(path + "/Attachments/receipt.pdf")).content == b"replacement"
    assert (await client.get(path)).json()["Invoices"][0]["HasAttachments"] is True
    online = (await client.get(path + "/OnlineInvoice")).json()["OnlineInvoices"][0]["OnlineInvoiceUrl"]
    assert online.startswith("https://go.xero.com/")
    assert (await client.post(path + "/Email")).status_code == 204
    assert (await client.get(path)).json()["Invoices"][0]["SentToContact"] is True
    note = await client.put(path + "/History", json={"HistoryRecords": [{"Details": "Reviewed by Finance"}]})
    assert note.status_code == 200
    audit = (await client.get(path + "/History")).json()["HistoryRecords"]
    assert any(n["Details"] == "Reviewed by Finance" for n in audit)
    assert any(n["Changes"] == "Email Sent" for n in audit)
    assert "id" not in audit[0]
    assert (await client.get(path + "/Attachments/missing.pdf")).status_code == 404


async def test_settings_contacts_groups_items_and_deletes(client: httpx.AsyncClient) -> None:
    contact_response = await client.put(
        BASE + "/Contacts",
        json={
            "Contacts": [
                {
                    "Name": "Synthetic contact",
                    "ContactNumber": "TEST-C",
                    "EmailAddress": "contact@customer.example",
                }
            ]
        },
    )
    assert contact_response.status_code == 200
    contact = contact_response.json()["Contacts"][0]
    cid = contact["ContactID"]
    assert (
        await client.post(BASE + "/Contacts/" + cid, json={"ContactStatus": "ARCHIVED"})
    ).status_code == 200
    assert not (await client.get(BASE + "/Contacts", params={"IDs": cid})).json()["Contacts"]
    assert (await client.post(BASE + "/Contacts/" + cid, json={"ContactStatus": "ACTIVE"})).status_code == 200
    group = (await client.put(BASE + "/ContactGroups", json={"Name": "Synthetic group"})).json()[
        "ContactGroups"
    ][0]
    gid = group["ContactGroupID"]
    result = await client.put(
        BASE + "/ContactGroups/" + gid + "/Contacts", json={"Contacts": [{"ContactID": cid}]}
    )
    assert result.status_code == 200
    detail = (await client.get(BASE + "/Contacts/" + cid)).json()["Contacts"][0]
    assert any(g["ContactGroupID"] == gid for g in detail["ContactGroups"])
    assert (await client.delete(BASE + "/ContactGroups/" + gid + "/Contacts/" + cid)).status_code == 204
    assert (await client.delete(BASE + "/ContactGroups/" + gid)).status_code == 200
    accounts = (
        await client.put(
            BASE + "/Accounts", json={"Code": "6999", "Name": "Synthetic expense", "Type": "EXPENSE"}
        )
    ).json()["Accounts"]
    aid = accounts[0]["AccountID"]
    assert (await client.get(BASE + "/Accounts/" + aid)).json()["Accounts"][0]["Class"] == "EXPENSE"
    assert (
        await client.post(BASE + "/Accounts/" + aid, json={"Name": "Synthetic expense revised"})
    ).status_code == 200
    assert (await client.delete(BASE + "/Accounts/" + aid)).json()["Accounts"][0]["Status"] == "DELETED"
    assert (await client.delete(BASE + "/Accounts/1000")).status_code == 400
    item = await client.put(
        BASE + "/Items",
        json={
            "Code": "TEST-SERVICE",
            "Name": "Test service",
            "SalesDetails": {"UnitPrice": 25, "AccountCode": "4200"},
        },
    )
    assert item.status_code == 200, item.text
    iid = item.json()["Items"][0]["ItemID"]
    assert (await client.get(BASE + "/Items/" + iid)).status_code == 200
    assert (await client.post(BASE + "/Items/" + iid, json={"Name": "Revised"})).status_code == 200
    assert (await client.delete(BASE + "/Items/" + iid)).status_code == 204
    categories = (await client.get(BASE + "/TrackingCategories")).json()["TrackingCategories"]
    category_id = categories[0]["TrackingCategoryID"]
    assert (
        await client.put(
            BASE + "/TrackingCategories/" + category_id + "/Options", json={"Name": "Synthetic department"}
        )
    ).status_code == 200
    assert (
        await client.put(BASE + "/TrackingCategories", json={"Name": "Third category"})
    ).status_code == 400
    assert (
        await client.post(BASE + "/TrackingCategories/" + category_id, json={"Status": "ARCHIVED"})
    ).status_code == 200
    new_category = (
        await client.put(BASE + "/TrackingCategories", json={"Name": "Synthetic category"})
    ).json()["TrackingCategories"][0]
    assert (
        await client.delete(BASE + "/TrackingCategories/" + new_category["TrackingCategoryID"])
    ).status_code == 200


async def test_bank_journals_quotes_and_credits(client: httpx.AsyncClient) -> None:
    invoice = await create_invoice(client)
    contact = invoice["Contact"]
    spend = await client.put(
        BASE + "/BankTransactions",
        json={
            "Type": "SPEND",
            "Contact": contact,
            "BankAccount": {"Code": "1000"},
            "Date": "2026-10-16",
            "LineItems": [{"Description": "Expense", "Quantity": 1, "UnitAmount": 20, "AccountCode": "6210"}],
        },
    )
    assert spend.status_code == 200, spend.text
    bid = spend.json()["BankTransactions"][0]["BankTransactionID"]
    assert (
        await client.post(BASE + "/BankTransactions/" + bid, json={"Status": "DELETED"})
    ).status_code == 200
    transfer = await client.put(
        BASE + "/BankTransfers",
        json={
            "FromBankAccount": {"Code": "1000"},
            "ToBankAccount": {"Code": "1020"},
            "Amount": 250,
            "Date": "2026-10-16",
        },
    )
    assert transfer.status_code == 200, transfer.text
    tid = transfer.json()["BankTransfers"][0]["BankTransferID"]
    assert (await client.get(BASE + "/BankTransfers/" + tid)).status_code == 200
    journal = {
        "Narration": "Equipment adjustment",
        "Status": "POSTED",
        "Date": "2026-10-16",
        "JournalLines": [
            {"AccountCode": "7000", "LineAmount": 25},
            {"AccountCode": "1590", "LineAmount": -25},
        ],
    }
    result = await client.put(BASE + "/ManualJournals", json={"ManualJournals": [journal]})
    assert result.status_code == 200, result.text
    jid = result.json()["ManualJournals"][0]["ManualJournalID"]
    assert (await client.post(BASE + "/ManualJournals/" + jid, json={"Status": "VOIDED"})).status_code == 200
    assert (
        await client.put(
            BASE + "/ManualJournals",
            json={**journal, "JournalLines": [{"AccountCode": "7000", "LineAmount": 25}]},
        )
    ).status_code == 400
    quote = await client.put(
        BASE + "/Quotes",
        json={
            "Contact": contact,
            "Date": "2026-10-16",
            "ExpiryDate": "2026-11-16",
            "LineItems": [
                {"Description": "Proposal", "Quantity": 1, "UnitAmount": 1000, "AccountCode": "4200"}
            ],
        },
    )
    assert quote.status_code == 200, quote.text
    qid = quote.json()["Quotes"][0]["QuoteID"]
    for status in ["SENT", "ACCEPTED"]:
        assert (await client.post(BASE + "/Quotes/" + qid, json={"Status": status})).status_code == 200
    credit = await client.put(
        BASE + "/CreditNotes",
        json={
            "Type": "ACCRECCREDIT",
            "Contact": contact,
            "Date": "2026-10-16",
            "Status": "AUTHORISED",
            "LineItems": [
                {"Description": "Discount", "Quantity": 1, "UnitAmount": 100, "AccountCode": "4200"}
            ],
        },
    )
    assert credit.status_code == 200, credit.text
    cid = credit.json()["CreditNotes"][0]["CreditNoteID"]
    allocation = await client.put(
        BASE + "/CreditNotes/" + cid + "/Allocations",
        json={
            "Allocations": [
                {"Invoice": {"InvoiceID": invoice["InvoiceID"]}, "Amount": 100, "Date": "2026-10-16"}
            ]
        },
    )
    assert allocation.status_code == 200, allocation.text
    settled = (await client.get(BASE + "/Invoices/" + invoice["InvoiceID"])).json()["Invoices"][0]
    assert settled["AmountCredited"] == 100 and settled["Status"] == "PAID"
    assert settled["FullyPaidOnDate"].startswith("/Date(")
    allocation_id = allocation.json()["Allocations"][0]["AllocationID"]
    assert (
        await client.delete(BASE + "/CreditNotes/" + cid + "/Allocations/" + allocation_id)
    ).status_code == 204
    assert (await client.get(BASE + "/Invoices/" + invoice["InvoiceID"])).json()["Invoices"][0][
        "AmountDue"
    ] == 100
    assert (await client.post(BASE + "/CreditNotes/" + cid, json={"Status": "VOIDED"})).status_code == 200


async def test_rate_limit_fault_journal_reset_and_idempotency(client: httpx.AsyncClient) -> None:
    data = {"Name": "Idempotent contact"}
    first = await client.put(BASE + "/Contacts", json=data, headers={"Idempotency-Key": "test-key"})
    second = await client.put(BASE + "/Contacts", json=data, headers={"Idempotency-Key": "test-key"})
    assert first.json() == second.json()
    mismatch = await client.put(
        BASE + "/Contacts", json={"Name": "Different"}, headers={"Idempotency-Key": "test-key"}
    )
    assert mismatch.status_code == 400
    await client.post("/_emulator/faults", json={"path": "/Contacts", "status": 429, "times": 1})
    injected = await client.get(BASE + "/Contacts")
    assert injected.status_code == 429 and injected.headers["retry-after"]
    journal = (await client.get("/_emulator/requests")).json()["requests"]
    assert any(r["status"] == 429 for r in journal)
    assert journal[0]["headers"]["authorization"].startswith("Bearer …")
    for _ in range(60):
        response = await client.get(BASE + "/Accounts")
        if response.status_code == 429:
            break
    assert response.status_code == 429
    assert response.headers["x-minlimit-remaining"] == "0"
    assert response.headers["retry-after"] == "60"
    client.hub.clock.advance(timedelta(seconds=61))
    assert (await client.get(BASE + "/Accounts")).status_code == 200
    await client.post("/_emulator/reset")
    assert (await client.get(BASE + "/Accounts")).status_code == 401


async def test_missing_plaintext_and_wrong_methods(client: httpx.AsyncClient) -> None:
    for path in [BASE + "/Missing", BASE + "/Invoices/00000000-0000-0000-0000-000000000000", "/unknown"]:
        response = await client.get(path)
        assert response.status_code == 404
        assert response.text == "The resource you're looking for cannot be found"
        assert response.headers["content-type"].startswith("text/plain")
    assert (await client.put(BASE + "/Users", json={"Name": "Invalid"})).status_code == 405
    assert (await client.delete(BASE + "/Invoices/missing")).status_code == 405
