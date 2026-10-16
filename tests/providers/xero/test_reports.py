from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from emulators.providers.xero.common import instant

from .conftest import BASE, create_invoice


def flatten(contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for entry in contents:
        if entry["RowType"] == "Section":
            result.extend(flatten(entry["Rows"]))
        elif entry["RowType"] != "Header":
            result.append(entry)
    return result


def total(report: dict[str, Any], label: str, column: int = 1) -> Decimal:
    entry = next(r for r in flatten(report["Rows"]) if r["Cells"][0]["Value"] == label)
    return Decimal(entry["Cells"][column]["Value"] or "0")


async def fetch(client: httpx.AsyncClient, name: str, **params: Any) -> dict[str, Any]:
    response = await client.get(BASE + "/Reports/" + name, params=params)
    assert response.status_code == 200, response.text
    report = response.json()["Reports"][0]
    assert report["ReportID"] == name
    assert report["ReportName"]
    assert report["ReportTitles"]
    assert report["UpdatedDateUTC"].startswith("/Date(")
    assert report["Rows"][0]["RowType"] == "Header"
    return report


async def test_profit_and_loss_ties_to_world_and_other_posted_transactions(client: httpx.AsyncClient) -> None:
    start, end = date(2025, 10, 1), date(2026, 10, 16)
    world = client.hub.world
    expected_income = sum(
        Decimal(str(i.subtotal))
        for i in world.invoices
        if i.status not in {"Draft", "Void"} and start <= i.issue_date <= end
    )
    expected_expenses = sum(Decimal(str(b.total)) for b in world.bills if start <= b.bill_date <= end)
    banks = (await client.get(BASE + "/BankTransactions", params={"page": 1, "pageSize": 1000})).json()[
        "BankTransactions"
    ]
    journals = (await client.get(BASE + "/ManualJournals")).json()["ManualJournals"]
    expected_expenses += sum(
        Decimal(str(b["SubTotal"])) for b in banks if start <= instant(b["Date"]).date() <= end
    )
    expected_expenses += sum(
        Decimal(str(line["LineAmount"]))
        for j in journals
        for line in j["JournalLines"]
        if line["AccountCode"] == "7000" and start <= instant(j["Date"]).date() <= end
    )
    report = await fetch(client, "ProfitAndLoss", fromDate=start.isoformat(), toDate=end.isoformat())
    assert total(report, "Total Income") == expected_income
    assert total(report, "Total Operating Expenses") == expected_expenses
    assert total(report, "Net Profit") == expected_income - expected_expenses
    account_rows = [r for r in flatten(report["Rows"]) if "Attributes" in r["Cells"][0]]
    assert account_rows[0]["Cells"][0]["Attributes"][0]["Id"] == "account"


@pytest.mark.parametrize("day", ["2025-10-01", "2025-12-31", "2026-03-31", "2026-10-16"])
async def test_balance_sheet_and_both_trial_balance_columns_balance(
    client: httpx.AsyncClient, day: str
) -> None:
    balance = await fetch(client, "BalanceSheet", date=day)
    assert total(balance, "Total Assets") == total(balance, "Total Liabilities") + total(
        balance, "Total Equity"
    )
    assert total(balance, "Net Assets") == total(balance, "Total Equity")
    trial = await fetch(client, "TrialBalance", date=day)
    assert total(trial, "Total", 1) == total(trial, "Total", 2)
    assert total(trial, "Total", 3) == total(trial, "Total", 4)


async def test_reports_respond_to_posting_payment_credit_and_reversal(client: httpx.AsyncClient) -> None:
    before = await fetch(client, "BalanceSheet", date="2026-10-16")
    invoice = await create_invoice(client)
    after = await fetch(client, "BalanceSheet", date="2026-10-16")
    assert total(after, "Total Assets") == total(before, "Total Assets") + 100
    assert total(after, "Total Equity") == total(before, "Total Equity") + 100
    before_pnl = await fetch(client, "ProfitAndLoss", fromDate="2026-10-01", toDate="2026-10-16")
    payment = await client.put(
        BASE + "/Payments",
        json={
            "Invoice": {"InvoiceID": invoice["InvoiceID"]},
            "Account": {"Code": "1000"},
            "Date": "2026-10-16",
            "Amount": 100,
        },
    )
    assert payment.status_code == 200
    after_pnl = await fetch(client, "ProfitAndLoss", fromDate="2026-10-01", toDate="2026-10-16")
    assert total(before_pnl, "Net Profit") == total(after_pnl, "Net Profit")
    assert (
        await client.post(
            BASE + "/Payments/" + payment.json()["Payments"][0]["PaymentID"], json={"Status": "DELETED"}
        )
    ).status_code == 200
    assert (
        await client.post(BASE + "/Invoices/" + invoice["InvoiceID"], json={"Status": "VOIDED"})
    ).status_code == 200
    reversed_balance = await fetch(client, "BalanceSheet", date="2026-10-16")
    assert total(reversed_balance, "Total Assets") == total(before, "Total Assets")
    assert total(reversed_balance, "Total Equity") == total(before, "Total Equity")


async def test_aged_reports_sum_to_control_accounts_at_historical_cutoff(client: httpx.AsyncClient) -> None:
    contacts = (await client.get(BASE + "/Contacts", params={"includeArchived": True})).json()["Contacts"]
    receivables = payables = Decimal(0)
    for contact in contacts:
        name = "AgedPayablesByContact" if contact["IsSupplier"] else "AgedReceivablesByContact"
        report = await fetch(client, name, contactId=contact["ContactID"], date="2026-03-31")
        due = total(report, "Closing Balance", 7)
        if contact["IsSupplier"]:
            payables += due
        else:
            receivables += due
    balance = await fetch(client, "BalanceSheet", date="2026-03-31")
    assert total(balance, "Accounts Receivable (A/R)") == receivables
    assert total(balance, "Accounts Payable (A/P)") == payables
    assert (await client.get(BASE + "/Reports/AgedReceivablesByContact")).status_code == 400


async def test_comparison_periods_tracking_bank_summary_and_executive(client: httpx.AsyncClient) -> None:
    result = await fetch(
        client, "ProfitAndLoss", fromDate="2026-09-01", toDate="2026-09-30", periods=2, timeframe="MONTH"
    )
    assert len(result["Rows"][0]["Cells"]) == 4
    for i, (start, end) in enumerate(
        [("2026-09-01", "2026-09-30"), ("2026-08-01", "2026-08-31"), ("2026-07-01", "2026-07-31")], 1
    ):
        separate = await fetch(client, "ProfitAndLoss", fromDate=start, toDate=end)
        assert total(result, "Net Profit", i) == total(separate, "Net Profit")
    categories = (await client.get(BASE + "/TrackingCategories")).json()["TrackingCategories"]
    department = next(c for c in categories if c["Name"] == "Department")
    filtered = await fetch(
        client,
        "ProfitAndLoss",
        fromDate="2025-10-01",
        toDate="2026-10-16",
        trackingCategoryID=department["TrackingCategoryID"],
        trackingOptionID=department["Options"][0]["TrackingOptionID"],
    )
    assert filtered["Rows"]
    balance = await fetch(client, "BalanceSheet", date="2026-10-16", periods=2, timeframe="QUARTER")
    for column in [1, 2, 3]:
        assert total(balance, "Total Assets", column) == total(balance, "Total Liabilities", column) + total(
            balance, "Total Equity", column
        )
    bank = await fetch(client, "BankSummary", fromDate="2026-09-01", toDate="2026-09-30")
    assert total(bank, "Total", 1) + total(bank, "Total", 2) - total(bank, "Total", 3) == total(
        bank, "Total", 4
    )
    executive = await fetch(client, "ExecutiveSummary", fromDate="2026-09-01", toDate="2026-09-30")
    pnl = await fetch(client, "ProfitAndLoss", fromDate="2026-09-01", toDate="2026-09-30")
    assert total(executive, "Profit (loss)") == total(pnl, "Net Profit")
    for params in [
        {"date": "bad"},
        {"periods": 99},
        {"timeframe": "INVALID"},
        {"fromDate": "2027-01-01", "toDate": "2026-01-01"},
        {"paymentsOnly": True},
    ]:
        assert (await client.get(BASE + "/Reports/ProfitAndLoss", params=params)).status_code == 400


async def test_credit_allocation_aged_total_matches_ledger(client: httpx.AsyncClient) -> None:
    invoice = await create_invoice(client)
    credit = await client.put(
        BASE + "/CreditNotes",
        json={
            "Contact": invoice["Contact"],
            "Type": "ACCRECCREDIT",
            "Date": "2026-10-16",
            "Status": "AUTHORISED",
            "LineItems": [
                {"Description": "Service credit", "Quantity": 1, "UnitAmount": 40, "AccountCode": "4200"}
            ],
        },
    )
    assert credit.status_code == 200
    cid = credit.json()["CreditNotes"][0]["CreditNoteID"]
    before = await fetch(
        client, "AgedReceivablesByContact", contactId=invoice["Contact"]["ContactID"], date="2026-10-16"
    )
    contact = (await client.get(BASE + "/Contacts/" + invoice["Contact"]["ContactID"])).json()["Contacts"][0]
    assert Decimal(str(contact["Balances"]["AccountsReceivable"]["Outstanding"])) == total(
        before, "Closing Balance", 7
    )
    allocation = await client.put(
        BASE + "/CreditNotes/" + cid + "/Allocations",
        json={
            "Allocations": [
                {"Invoice": {"InvoiceID": invoice["InvoiceID"]}, "Amount": 40, "Date": "2026-10-16"}
            ]
        },
    )
    assert allocation.status_code == 200
    after = await fetch(
        client, "AgedReceivablesByContact", contactId=invoice["Contact"]["ContactID"], date="2026-10-16"
    )
    assert total(before, "Closing Balance", 7) == total(after, "Closing Balance", 7)
