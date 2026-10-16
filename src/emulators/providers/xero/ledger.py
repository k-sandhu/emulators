"""Derive a balanced ledger from current transaction state rather than cached totals."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from emulators.core.provider import ProviderContext

from .common import instant, money, rows


@dataclass
class Posting:
    day: date
    code: str
    amount: Decimal  # Debits positive, credits negative.
    tracking: list[dict[str, Any]]


def postings(ctx: ProviderContext, tenant: str) -> list[Posting]:
    result: list[Posting] = []

    def add(
        day: date, code: str, amount: float | Decimal, tracking: list[dict[str, Any]] | None = None
    ) -> None:
        result.append(Posting(day, code, Decimal(str(amount)), tracking or []))

    opening_date = ctx.world.anchor.date().replace(year=ctx.world.anchor.year - 1, day=1)
    for account in rows(ctx, "Accounts", tenant):
        sign = 1 if account["Class"] in {"ASSET", "EXPENSE"} else -1
        add(opening_date, account["Code"], Decimal(str(account.get("_opening", 0))) * sign)
    for resource in ["Invoices", "CreditNotes", "BankTransactions"]:
        for record in rows(ctx, resource, tenant):
            if record["Status"] not in {"AUTHORISED", "PAID"}:
                continue
            day = instant(record["Date"]).date()
            kind = record["Type"]
            if kind in {"SPEND-TRANSFER", "RECEIVE-TRANSFER"}:
                continue  # The paired entries are posted once through BankTransfers below.
            credit = resource == "CreditNotes"
            purchase = kind in {"ACCPAY", "ACCPAYCREDIT", "SPEND"}
            sign = Decimal(1 if purchase != credit else -1)
            for line in record["LineItems"]:
                if "AccountCode" not in line:
                    continue
                track = line.get("Tracking", [])
                net = Decimal(str(line["LineAmount"]))
                if record["LineAmountTypes"] == "Inclusive":
                    net -= Decimal(str(line["TaxAmount"]))
                add(day, line["AccountCode"], net * sign, track)
                add(day, "2300", Decimal(str(line["TaxAmount"])) * sign, track)
                control = (
                    record["BankAccount"]["Code"]
                    if resource == "BankTransactions"
                    else "2000"
                    if purchase
                    else "1100"
                )
                add(
                    day,
                    control,
                    -(net + Decimal(str(line["TaxAmount"]))) * sign,
                    track,
                )
    for payment in rows(ctx, "Payments", tenant):
        if payment["Status"] != "AUTHORISED":
            continue
        day = instant(payment["Date"]).date()
        invoice = next(
            (i for i in rows(ctx, "Invoices", tenant) if i["InvoiceID"] == payment["Invoice"]["InvoiceID"]),
            None,
        )
        if not invoice or invoice["Status"] not in {"AUTHORISED", "PAID"}:
            continue
        sign = Decimal(1 if invoice["Type"] == "ACCREC" else -1)
        add(day, payment["Account"]["Code"], Decimal(str(payment["Amount"])) * sign)
        add(day, "1100" if invoice["Type"] == "ACCREC" else "2000", -Decimal(str(payment["Amount"])) * sign)
    for transfer in rows(ctx, "BankTransfers", tenant):
        if transfer.get("Status") == "DELETED":
            continue
        day = instant(transfer["Date"]).date()
        add(day, transfer["FromBankAccount"]["Code"], -Decimal(str(transfer["Amount"])))
        add(day, transfer["ToBankAccount"]["Code"], transfer["Amount"])
    for journal in rows(ctx, "ManualJournals", tenant):
        if journal["Status"] == "POSTED":
            for line in journal["JournalLines"]:
                add(
                    instant(journal["Date"]).date(),
                    line["AccountCode"],
                    line["LineAmount"],
                    line.get("Tracking", []),
                )
    return result


def balances(
    entries: list[Posting],
    end: date,
    start: date | None = None,
    tracking: list[tuple[str, str | None]] | None = None,
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = defaultdict(Decimal)
    for entry in entries:
        if entry.day > end or (start and entry.day < start):
            continue
        if tracking and not all(
            any(
                t.get("TrackingCategoryID") == category
                and (option is None or t.get("TrackingOptionID") == option)
                for t in entry.tracking
            )
            for category, option in tracking
        ):
            continue
        result[entry.code] += entry.amount
    return result


def invoice_due(
    ctx: ProviderContext, tenant: str, invoice: dict[str, Any], day: date
) -> tuple[float, float, float]:
    if invoice["Status"] not in {"AUTHORISED", "PAID"} or instant(invoice["Date"]).date() > day:
        return 0.0, 0.0, 0.0
    paid = money(
        sum(
            p["Amount"]
            for p in rows(ctx, "Payments", tenant)
            if p["Status"] == "AUTHORISED"
            and p.get("Invoice", {}).get("InvoiceID") == invoice["InvoiceID"]
            and instant(p["Date"]).date() <= day
        )
    )
    credited = money(
        sum(
            a["Amount"]
            for c in rows(ctx, "CreditNotes", tenant)
            if c["Status"] in {"AUTHORISED", "PAID"}
            for a in c.get("Allocations", [])
            if a["Invoice"]["InvoiceID"] == invoice["InvoiceID"] and instant(a["Date"]).date() <= day
        )
    )
    return money(invoice["Total"] - paid - credited), paid, credited


def contact_balances(ctx: ProviderContext, tenant: str, contact: dict[str, Any]) -> dict[str, Any]:
    result = {}
    today = ctx.clock.now().date()
    for kind, label in [("ACCREC", "AccountsReceivable"), ("ACCPAY", "AccountsPayable")]:
        outstanding = overdue = Decimal(0)
        for invoice in rows(ctx, "Invoices", tenant):
            if invoice["Contact"]["ContactID"] == contact["ContactID"] and invoice["Type"] == kind:
                due, _, _ = invoice_due(ctx, tenant, invoice, today)
                outstanding += Decimal(str(due))
                if instant(invoice["DueDate"]).date() < today:
                    overdue += Decimal(str(due))
        for credit in rows(ctx, "CreditNotes", tenant):
            if (
                credit["Contact"]["ContactID"] != contact["ContactID"]
                or credit["Type"] != kind + "CREDIT"
                or credit["Status"] not in {"AUTHORISED", "PAID"}
                or instant(credit["Date"]).date() > today
            ):
                continue
            allocated = sum(
                Decimal(str(a["Amount"]))
                for a in credit.get("Allocations", [])
                if instant(a["Date"]).date() <= today
            )
            remaining = Decimal(str(credit["Total"])) - allocated
            outstanding -= remaining
            if instant(credit.get("DueDate", credit["Date"])).date() < today:
                overdue -= remaining
        result[label] = {"Outstanding": money(outstanding), "Overdue": money(overdue)}
    return result
