"""Reports tie to the ledger; webhooks are Intuit-signed."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
from fastapi import FastAPI, Request

from tests.providers.quickbooks.conftest import API, HOST


def section(report: dict, group: str) -> dict:
    return next(row for row in report["Rows"]["Row"] if row.get("group") == group)


def summary_value(row: dict, index: int = 1) -> float:
    return float(row["Summary"]["ColData"][index]["value"])


async def query(client, headers, text):
    r = await client.get(f"{API}/query", params={"query": text}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["QueryResponse"]


async def test_profit_and_loss_ties_to_invoices_and_bills(qb) -> None:
    client, h = qb
    invoices = (await query(client, h, "SELECT * FROM Invoice MAXRESULTS 1000"))["Invoice"]
    credit_memos = (await query(client, h, "SELECT * FROM CreditMemo MAXRESULTS 100"))["CreditMemo"]
    expected_income = round(
        sum(i["TotalAmt"] - i["TxnTaxDetail"]["TotalTax"] for i in invoices)
        - sum(c["TotalAmt"] - c["TxnTaxDetail"]["TotalTax"] for c in credit_memos),
        2,
    )
    r = await client.get(f"{API}/reports/ProfitAndLoss", params={"date_macro": "All dates"}, headers=h)
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["Header"]["ReportName"] == "ProfitAndLoss"
    assert report["Header"]["ReportBasis"] == "Accrual"
    assert report["Header"]["Option"][0] == {"Name": "AccountingStandard", "Value": "GAAP"}
    income = section(report, "Income")
    total_income = summary_value(income)
    assert total_income == expected_income
    # Net Income = Total Income - all expenses
    net = next(
        row for row in report["Rows"]["Row"] if row.get("ColData", [{}])[0].get("value") == "Net Income"
    )
    expenses = summary_value(section(report, "Expense"))
    other = summary_value(section(report, "OtherExpense"))
    assert float(net["ColData"][1]["value"]) == round(total_income - expenses - other, 2)


async def test_profit_and_loss_summarize_and_cash(qb) -> None:
    client, h = qb
    r = await client.get(
        f"{API}/reports/ProfitAndLoss",
        params={"start_date": "2026-01-01", "end_date": "2026-03-31", "summarize_column_by": "Month"},
        headers=h,
    )
    titles = [c["ColTitle"] for c in r.json()["Columns"]["Column"]]
    assert titles[1:] == ["Jan 2026", "Feb 2026", "Mar 2026", "Total"]
    income = section(r.json(), "Income")
    months_total = round(sum(float(c["value"]) for c in income["Summary"]["ColData"][1:4]), 2)
    assert months_total == float(income["Summary"]["ColData"][4]["value"])

    r = await client.get(
        f"{API}/reports/ProfitAndLoss",
        params={"date_macro": "This Fiscal Year-to-date", "accounting_method": "Cash"},
        headers=h,
    )
    assert r.json()["Header"]["ReportBasis"] == "Cash"
    assert r.json()["Header"]["StartPeriod"] == "2026-01-01"

    r = await client.get(
        f"{API}/reports/CustomerIncome",
        params={"date_macro": "All dates"},
        headers=h,
    )
    titles = [c["ColTitle"] for c in r.json()["Columns"]["Column"]]
    assert any(":" in t or "Logistics" in t for t in titles)  # customer columns


async def test_balance_sheet_balances_and_trial_balance(qb) -> None:
    client, h = qb
    r = await client.get(f"{API}/reports/BalanceSheet", headers=h)
    report = r.json()
    assets = summary_value(section(report, "Asset"))
    total_le = next(
        row
        for row in report["Rows"]["Row"]
        if row.get("ColData", [{}])[0].get("value") == "TOTAL LIABILITIES AND EQUITY"
    )
    assert assets == float(total_le["ColData"][1]["value"])

    r = await client.get(f"{API}/reports/TrialBalance", headers=h)
    rows = r.json()["Rows"]["Row"]
    total_row = rows[-1]
    assert float(total_row["ColData"][1]["value"]) == float(total_row["ColData"][2]["value"])

    # AR in the ledger equals the sum of open invoice balances less unapplied credit memos
    invoices = (await query(client, h, "SELECT * FROM Invoice MAXRESULTS 1000"))["Invoice"]
    credit_memos = (await query(client, h, "SELECT * FROM CreditMemo MAXRESULTS 100"))["CreditMemo"]
    accounts = await query(client, h, "SELECT * FROM Account WHERE AccountSubType = 'AccountsReceivable'")
    ar = accounts["Account"][0]
    expected_ar = round(sum(i["Balance"] for i in invoices) - sum(c["Balance"] for c in credit_memos), 2)
    assert ar["CurrentBalance"] == expected_ar


async def test_aged_receivables_buckets(qb) -> None:
    client, h = qb
    r = await client.get(f"{API}/reports/AgedReceivables", headers=h)
    report = r.json()
    titles = [c["ColTitle"] for c in report["Columns"]["Column"]]
    assert titles[1:] == ["Current", "1 - 30", "31 - 60", "61 - 90", "91 and over", "Total"]
    invoices = (await query(client, h, "SELECT * FROM Invoice WHERE Balance > 0 MAXRESULTS 1000"))["Invoice"]
    total_row = report["Rows"]["Row"][-1]
    assert float(total_row["ColData"][-1]["value"]) == round(sum(i["Balance"] for i in invoices), 2)
    # overdue seeded invoices land in the >90 bucket
    assert float(total_row["ColData"][-2]["value"]) > 0

    r = await client.get(f"{API}/reports/AgedReceivableDetail", headers=h)
    detail_rows = r.json()["Rows"]["Row"]
    assert detail_rows
    r = await client.get(f"{API}/reports/AgedPayables", headers=h)
    assert r.json()["Rows"]["Row"]


async def test_general_ledger_and_unknown_report(qb) -> None:
    client, h = qb
    accounts = await query(client, h, "SELECT * FROM Account WHERE AccountType = 'Bank' MAXRESULTS 1")
    r = await client.get(
        f"{API}/reports/GeneralLedger",
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "account": accounts["Account"][0]["Id"],
        },
        headers=h,
    )
    rows = r.json()["Rows"]["Row"]
    assert len(rows) == 1
    assert rows[0]["Rows"]["Row"]
    r = await client.get(f"{API}/reports/NotAReport", headers=h)
    assert r.status_code == 400


async def test_webhook_delivery_is_signed(make_client) -> None:
    received: list[tuple[bytes, str | None]] = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> dict:
        received.append((await request.body(), request.headers.get("intuit-signature")))
        return {}

    async with make_client("quickbooks") as client:
        client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
        r = await client.post(f"{HOST}/_emulator/tokens", json={"app_only": True})
        headers = {"Authorization": f"Bearer {r.json()['access_token']}", "Accept": "application/json"}
        r = await client.post(
            f"{HOST}/_emulator/actions/configure_webhook",
            json={"url": "http://consumer.test/hook", "verifier_token": "test-verifier"},
        )
        assert r.status_code == 200, r.text
        created = (
            await client.post(f"{API}/customer", headers=headers, json={"DisplayName": "Hook Co"})
        ).json()["Customer"]
        assert received, "webhook should be delivered synchronously in sync mode"
        body, signature = received[-1]
        expected = base64.b64encode(hmac.new(b"test-verifier", body, hashlib.sha256).digest()).decode()
        assert signature == expected
        payload = json.loads(body)
        entities = payload["eventNotifications"][0]["dataChangeEvent"]["entities"]
        assert entities[0]["name"] == "Customer"
        assert entities[0]["id"] == created["Id"]
        assert entities[0]["operation"] == "Create"
        assert payload["eventNotifications"][0]["realmId"] == "9341569385067351"
        journal = (await client.get(f"{HOST}/_emulator/webhooks", headers=headers)).json()
        assert journal["deliveries"][-1]["response_status"] == 200
