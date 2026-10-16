"""Xero row/cell reports computed from the same postings as transaction balances."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import Request

from emulators.core.provider import ProviderContext

from .common import envelope, find, instant, missing, money, msdate, rows, validation
from .ledger import Posting, balances, invoice_due, postings

NAMES = {
    "ProfitAndLoss": "Profit and Loss",
    "BalanceSheet": "Balance Sheet",
    "TrialBalance": "Trial Balance",
    "AgedReceivablesByContact": "Aged Receivables By Contact",
    "AgedPayablesByContact": "Aged Payables By Contact",
    "BankSummary": "Bank Summary",
    "ExecutiveSummary": "Executive Summary",
}


def cell(value: Any, identifier: str | None = None, attr: str = "account") -> dict[str, Any]:
    result = {"Value": f"{money(value):.2f}" if isinstance(value, (int, float, Decimal)) else str(value)}
    if identifier:
        result["Attributes"] = [{"Value": identifier, "Id": attr}]
    return result


def row(
    label: str,
    values: list[Any],
    *,
    summary: bool = False,
    identifier: str | None = None,
    attr: str = "account",
) -> dict[str, Any]:
    return {
        "RowType": "SummaryRow" if summary else "Row",
        "Cells": [cell(label, identifier, attr), *(cell(v, identifier, attr) for v in values)],
    }


def section(title: str, contents: list[dict[str, Any]]) -> dict[str, Any]:
    return {"RowType": "Section", "Title": title, "Rows": contents}


def header(labels: list[str]) -> dict[str, Any]:
    return {"RowType": "Header", "Cells": [cell(v) for v in labels]}


def shift(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    last = monthrange(year, month + 1)[1]
    return date(
        year, month + 1, last if day.day == monthrange(day.year, day.month)[1] else min(day.day, last)
    )


def fiscal_start(ctx: ProviderContext, end: date) -> date:
    month = ctx.world.org.fiscal_year_start_month
    return date(end.year - int(end.month < month), month, 1)


def profit(accounts: list[dict[str, Any]], values: dict[str, Decimal]) -> Decimal:
    return -sum(
        (values.get(a["Code"], Decimal(0)) for a in accounts if a["Class"] in {"REVENUE", "EXPENSE"}),
        Decimal(0),
    )


def pnl(
    accounts: list[dict[str, Any]], columns: list[dict[str, Decimal]], labels: list[str]
) -> list[dict[str, Any]]:
    result = [header(["", *labels])]
    for kind, name in [("REVENUE", "Income"), ("EXPENSE", "Operating Expenses")]:
        members = [a for a in accounts if a["Class"] == kind]
        sign = -1 if kind == "REVENUE" else 1
        detail = [
            row(a["Name"], [v.get(a["Code"], Decimal(0)) * sign for v in columns], identifier=a["AccountID"])
            for a in members
            if any(v.get(a["Code"], 0) for v in columns)
        ]
        totals = [sum((v.get(a["Code"], Decimal(0)) * sign for a in members), Decimal(0)) for v in columns]
        detail.append(row("Total " + name, totals, summary=True))
        result.append(section(name, detail))
    result.append(section("", [row("Net Profit", [profit(accounts, v) for v in columns], summary=True)]))
    return result


def balance_sheet(
    ctx: ProviderContext, accounts: list[dict[str, Any]], entries: list[Posting], ends: list[date]
) -> list[dict[str, Any]]:
    columns = [balances(entries, day) for day in ends]
    # Close preceding financial years into retained earnings, preserving the complete ledger total.
    current_profit = []
    for day, values in zip(ends, columns, strict=True):
        year_values = balances(entries, day, fiscal_start(ctx, day))
        current = profit(accounts, year_values)
        prior = profit(accounts, values) - current
        values["3200"] = values.get("3200", Decimal(0)) - prior
        current_profit.append(current)
    result = [header(["", *(d.strftime("%d %b %Y") for d in ends)])]
    totals: dict[str, list[Decimal]] = {}
    for kind, title in [("ASSET", "Assets"), ("LIABILITY", "Liabilities"), ("EQUITY", "Equity")]:
        sign = 1 if kind == "ASSET" else -1
        members = [a for a in accounts if a["Class"] == kind]
        detail = [
            row(a["Name"], [v.get(a["Code"], Decimal(0)) * sign for v in columns], identifier=a["AccountID"])
            for a in members
            if any(v.get(a["Code"], 0) for v in columns)
        ]
        totals[kind] = [
            sum((v.get(a["Code"], Decimal(0)) * sign for a in members), Decimal(0)) for v in columns
        ]
        if kind == "EQUITY":
            detail.append(row("Current Year Earnings", current_profit))
            totals[kind] = [a + b for a, b in zip(totals[kind], current_profit, strict=True)]
        detail.append(row("Total " + title, totals[kind], summary=True))
        result.append(section(title, detail))
    result.append(
        section(
            "",
            [
                row(
                    "Net Assets",
                    [a - b for a, b in zip(totals["ASSET"], totals["LIABILITY"], strict=True)],
                    summary=True,
                )
            ],
        )
    )
    return result


def trial_balance(
    ctx: ProviderContext, accounts: list[dict[str, Any]], entries: list[Posting], end: date
) -> list[dict[str, Any]]:
    month_values = balances(entries, end, end.replace(day=1))
    ytd_values = balances(entries, end, fiscal_start(ctx, end))
    full_values = balances(entries, end)
    prior_profit = profit(accounts, full_values) - profit(accounts, ytd_values)
    full_values["3200"] = full_values.get("3200", Decimal(0)) - prior_profit
    result = [header(["Account", "Debit", "Credit", "YTD Debit", "YTD Credit"])]
    totals = [Decimal(0)] * 4
    for kind, title in [
        ("REVENUE", "Revenue"),
        ("EXPENSE", "Expenses"),
        ("ASSET", "Assets"),
        ("LIABILITY", "Liabilities"),
        ("EQUITY", "Equity"),
    ]:
        detail = []
        for account in accounts:
            if account["Class"] != kind:
                continue
            code = account["Code"]
            current = (
                month_values.get(code, Decimal(0))
                if kind in {"REVENUE", "EXPENSE"}
                else full_values.get(code, Decimal(0))
            )
            ytd = (
                ytd_values.get(code, Decimal(0))
                if kind in {"REVENUE", "EXPENSE"}
                else full_values.get(code, Decimal(0))
            )
            if not current and not ytd:
                continue
            amounts = [
                max(current, Decimal(0)),
                max(-current, Decimal(0)),
                max(ytd, Decimal(0)),
                max(-ytd, Decimal(0)),
            ]
            totals = [a + b for a, b in zip(totals, amounts, strict=True)]
            detail.append(
                row(
                    f"{account['Name']} ({code})",
                    [v if v else "" for v in amounts],
                    identifier=account["AccountID"],
                )
            )
        result.append(section(title, detail))
    # The monthly columns need the previous months' earnings carried into equity too.
    monthly_carry = profit(accounts, ytd_values) - profit(accounts, month_values)
    if monthly_carry:
        result[-1]["Rows"].append(
            row(
                "Current Year Earnings",
                [max(-monthly_carry, Decimal(0)) or "", max(monthly_carry, Decimal(0)) or "", "", ""],
            )
        )
        totals[0] += max(-monthly_carry, Decimal(0))
        totals[1] += max(monthly_carry, Decimal(0))
    result.append(section("", [row("Total", totals, summary=True)]))
    return result


def bank_summary(
    accounts: list[dict[str, Any]], entries: list[Posting], start: date, end: date
) -> list[dict[str, Any]]:
    opening = balances(entries, start - timedelta(days=1))
    detail = []
    totals = [Decimal(0)] * 4
    for account in accounts:
        if account["Type"] != "BANK":
            continue
        relevant = [p.amount for p in entries if p.code == account["Code"] and start <= p.day <= end]
        received = sum((p for p in relevant if p > 0), Decimal(0))
        spent = -sum((p for p in relevant if p < 0), Decimal(0))
        beginning = opening.get(account["Code"], Decimal(0))
        values = [beginning, received, spent, beginning + received - spent]
        totals = [a + b for a, b in zip(totals, values, strict=True)]
        detail.append(row(account["Name"], values, identifier=account["AccountID"]))
    detail.append(row("Total", totals, summary=True))
    return [
        header(["Bank Accounts", "Opening Balance", "Cash Received", "Cash Spent", "Closing Balance"]),
        section("", detail),
    ]


def aged(
    ctx: ProviderContext, tenant: str, name: str, query: dict[str, str], start: date, end: date, cutoff: date
) -> tuple[list[dict[str, Any]], str]:
    identifier = query.get("contactid")
    if not identifier:
        validation("contactId is required.")
    contact = find(ctx, "Contacts", tenant, identifier)
    identifier = contact["ContactID"]
    kind = "ACCREC" if name == "AgedReceivablesByContact" else "ACCPAY"
    detail = []
    totals = [Decimal(0)] * 4
    opening = Decimal(0)
    for invoice in rows(ctx, "Invoices", tenant):
        issue = instant(invoice["Date"]).date()
        if invoice["Contact"]["ContactID"] != identifier or invoice["Type"] != kind or issue > end:
            continue
        due, paid, credited = invoice_due(ctx, tenant, invoice, cutoff)
        if not due:
            continue
        if issue < start:
            opening += Decimal(str(due))
            continue
        values = [invoice["Total"], paid, credited, due]
        totals = [a + Decimal(str(b)) for a, b in zip(totals, values, strict=True)]
        overdue = (cutoff - instant(invoice["DueDate"]).date()).days
        record = row(
            invoice["DateString"],
            [
                invoice["InvoiceNumber"],
                invoice["DueDateString"],
                f"{overdue} days overdue" if overdue > 0 else "",
                *values,
            ],
            identifier=invoice["InvoiceID"],
            attr="invoiceID",
        )
        detail.append(record)
    # Unallocated credits are ledger receivables/payables too, represented as negative rows.
    for credit in rows(ctx, "CreditNotes", tenant):
        if (
            credit["Status"] not in {"AUTHORISED", "PAID"}
            or credit["Contact"]["ContactID"] != identifier
            or credit["Type"] != kind + "CREDIT"
            or instant(credit["Date"]).date() > min(cutoff, end)
        ):
            continue
        remaining = money(
            credit["Total"]
            - sum(a["Amount"] for a in credit.get("Allocations", []) if instant(a["Date"]).date() <= cutoff)
        )
        if not remaining:
            continue
        if instant(credit["Date"]).date() < start:
            opening -= Decimal(str(remaining))
        else:
            detail.append(
                row(
                    credit["DateString"],
                    [credit["CreditNoteNumber"], "", "", -remaining, 0.0, 0.0, -remaining],
                    identifier=credit["CreditNoteID"],
                    attr="creditNoteID",
                )
            )
            totals[0] -= Decimal(str(remaining))
            totals[3] -= Decimal(str(remaining))
    detail.append(row("Total", ["", "", "", *totals], summary=True))
    closing = ["", "", "", totals[0], totals[1], totals[2], totals[3] + opening]
    return [
        header(["Date", "Number", "Due Date", "", "Total", "Paid", "Credited", "Due"]),
        section("", [row(start.isoformat() + "T00:00:00", ["Opening Balance", "", "", "", "", "", opening])]),
        section("", detail),
        section("", [row("Closing Balance", closing, summary=True)]),
    ], contact["Name"]


def executive_summary(
    ctx: ProviderContext,
    tenant: str,
    accounts: list[dict[str, Any]],
    entries: list[Posting],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    ranges = [(start, end), (shift(start, 1), shift(end, 1))]
    metrics = []
    for first, last in ranges:
        current = balances(entries, last, first)
        all_values = balances(entries, last)
        bank = bank_summary(accounts, entries, first, last)[1]["Rows"][-1]["Cells"]
        revenue = -sum(
            (current.get(a["Code"], Decimal(0)) for a in accounts if a["Class"] == "REVENUE"), Decimal(0)
        )
        expense = sum(
            (current.get(a["Code"], Decimal(0)) for a in accounts if a["Class"] == "EXPENSE"), Decimal(0)
        )
        issued = [
            i
            for i in rows(ctx, "Invoices", tenant)
            if i["Type"] == "ACCREC"
            and i["Status"] in {"AUTHORISED", "PAID"}
            and first <= instant(i["Date"]).date() <= last
        ]
        metrics.append(
            {
                "Cash received": Decimal(bank[2]["Value"]),
                "Cash spent": Decimal(bank[3]["Value"]),
                "Cash surplus (deficit)": Decimal(bank[2]["Value"]) - Decimal(bank[3]["Value"]),
                "Closing bank balance": Decimal(bank[4]["Value"]),
                "Income": revenue,
                "Expenses": expense,
                "Profit (loss)": revenue - expense,
                "Debtors": all_values.get("1100", Decimal(0)),
                "Creditors": -all_values.get("2000", Decimal(0)),
                "Net assets": sum(
                    (
                        all_values.get(a["Code"], Decimal(0))
                        for a in accounts
                        if a["Class"] in {"ASSET", "LIABILITY"}
                    ),
                    Decimal(0),
                ),
                "Number of invoices issued": Decimal(len(issued)),
                "Average value of invoices": sum((Decimal(str(i["SubTotal"])) for i in issued), Decimal(0))
                / len(issued)
                if issued
                else Decimal(0),
            }
        )
    result = [header(["", end.strftime("%b %Y"), ranges[1][1].strftime("%b %Y"), "Variance"])]
    for title, labels in [
        ("Cash", ["Cash received", "Cash spent", "Cash surplus (deficit)", "Closing bank balance"]),
        ("Profitability", ["Income", "Expenses", "Profit (loss)"]),
        ("Balance Sheet", ["Debtors", "Creditors", "Net assets"]),
        ("Income", ["Number of invoices issued", "Average value of invoices"]),
    ]:
        details = []
        for label in labels:
            current, previous = metrics[0][label], metrics[1][label]
            variance = (current - previous) / abs(previous) * 100 if previous else Decimal(0)
            details.append(row(label, [current, previous, f"{variance:.1f}%"]))
        result.append(section(title, details))
    return result


def report(ctx: ProviderContext, request: Request, tenant: str, name: str) -> dict[str, Any]:
    if name not in NAMES:
        missing()
    q = {k.lower(): v for k, v in request.query_params.items()}
    if q.get("paymentsonly", "false").lower() == "true":
        validation("This emulator implements accrual reports only.")
    try:
        end = instant(q.get("todate", q.get("date", ctx.clock.now().date().isoformat()))).date()
        cutoff = instant(q.get("date", end.isoformat())).date()
        default_start = date(1970, 1, 1) if name.startswith("Aged") else end.replace(day=1)
        start = instant(q.get("fromdate", default_start.isoformat())).date()
        periods = int(q.get("periods", "0"))
        if not 0 <= periods <= 12:
            raise ValueError
    except (ValueError, TypeError):
        validation("Invalid report date or periods.")
    if start > end:
        validation("fromDate must not be after toDate.")
    timeframe = q.get("timeframe", "MONTH").upper()
    if timeframe not in {"MONTH", "QUARTER", "YEAR"}:
        validation("timeframe must be MONTH, QUARTER or YEAR.")
    months = {"MONTH": 1, "QUARTER": 3, "YEAR": 12}[timeframe]
    ends = [end, *(shift(end, months * i) for i in range(1, periods + 1))]
    entries = postings(ctx, tenant)
    accounts = sorted(rows(ctx, "Accounts", tenant), key=lambda a: a["Code"])
    title_org = ctx.store.collection("_tenants").get(tenant)["name"]
    title = f"As at {end:%d %B %Y}"
    if name == "ProfitAndLoss":
        filters = []
        for suffix in ["", "2"]:
            category_id, option_id = q.get("trackingcategoryid" + suffix), q.get("trackingoptionid" + suffix)
            if category_id:
                category = find(ctx, "TrackingCategories", tenant, category_id)
                if option_id and not any(o["TrackingOptionID"] == option_id for o in category["Options"]):
                    validation("Invalid trackingOptionID.")
                filters.append((category_id, option_id))
        columns = [balances(entries, to, shift(start, months * i), filters) for i, to in enumerate(ends)]
        contents = pnl(accounts, columns, [d.strftime("%d %b %Y") for d in ends])
        title = f"For the period {start:%d %B %Y} to {end:%d %B %Y}"
    elif name == "BalanceSheet":
        if any(k.startswith("tracking") for k in q):
            validation("Tracking-filtered balance sheets are not implemented.")
        contents = balance_sheet(ctx, accounts, entries, ends)
    elif name == "TrialBalance":
        contents = trial_balance(ctx, accounts, entries, end)
    elif name.startswith("Aged"):
        contents, title_org = aged(ctx, tenant, name, q, start, end, cutoff)
    elif name == "BankSummary":
        contents = bank_summary(accounts, entries, start, end)
    else:
        contents = executive_summary(ctx, tenant, accounts, entries, start, end)
    result = {
        "ReportID": name,
        "ReportName": NAMES[name],
        "ReportType": name,
        "ReportTitles": [NAMES[name], title_org, title],
        "ReportDate": ctx.clock.now().strftime("%d %B %Y"),
        "UpdatedDateUTC": msdate(ctx.clock.now(), offset=False),
        "Fields": [],
        "Rows": contents,
    }
    return envelope(ctx, "Reports", [result])
