"""Double-entry ledger projected from the stored QBO transactions.

Balances are never cached as truth: account/customer/vendor balances and every
report recompute from the transaction records, so CRUD, voids and deletes move
the books exactly like QBO.  Internal sign convention is debit-positive.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.quickbooks.common import money, ref_value

ACCOUNT_COLLECTIONS_TXN = (
    "Invoice",
    "CreditMemo",
    "Payment",
    "Bill",
    "BillPayment",
    "Purchase",
    "JournalEntry",
    "SalesReceipt",
    "Deposit",
)


def parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _records(ctx: ProviderContext, entity: str) -> list[dict[str, Any]]:
    return [r for r in ctx.store.collection(entity) if not r.get("_deleted")]


def account_by_ref(ctx: ProviderContext, value: Any) -> dict[str, Any] | None:
    rid = ref_value(value)
    return ctx.store.collection("Account").get(rid) if rid else None


def system_account_id(ctx: ProviderContext, kind: str) -> str | None:
    """kind: ar | ap | tax | undeposited | bank"""
    meta = ctx.store.meta.get("system_accounts", {})
    return meta.get(kind)


def item_income_account(ctx: ProviderContext, line: dict[str, Any]) -> str | None:
    detail = line.get("SalesItemLineDetail") or {}
    rid = ref_value(detail.get("ItemRef"))
    if not rid:
        return None
    item = ctx.store.collection("Item").get(rid)
    if item is None:
        return None
    return ref_value(item.get("IncomeAccountRef"))


def postings(ctx: ProviderContext) -> list[dict[str, Any]]:
    """All ledger postings: {account_id, amount(debit+), date, txn_type, txn_id, customer, vendor, class, memo}."""
    out: list[dict[str, Any]] = []
    ar = system_account_id(ctx, "ar")
    ap = system_account_id(ctx, "ap")
    tax_payable = system_account_id(ctx, "tax")
    undeposited = system_account_id(ctx, "undeposited")

    def add(
        account_id: Any, amount: float, when: Any, txn: dict[str, Any], txn_type: str, **dims: Any
    ) -> None:
        if not account_id or not amount:
            return
        out.append(
            {
                "account_id": str(account_id),
                "amount": money(amount),
                "date": parse_date(when) or date(1970, 1, 1),
                "txn_type": txn_type,
                "txn_id": str(txn.get("Id", "")),
                "doc_number": txn.get("DocNumber", ""),
                "memo": txn.get("PrivateNote") or "",
                "department": ref_value(txn.get("DepartmentRef")),
                **dims,
            }
        )

    for inv in _records(ctx, "Invoice"):
        if inv.get("_voided"):
            continue
        cust = ref_value(inv.get("CustomerRef"))
        add(ar, money(inv.get("TotalAmt")), inv.get("TxnDate"), inv, "Invoice", customer=cust)
        deposit = money(inv.get("Deposit"))
        deposit_to = ref_value(inv.get("DepositToAccountRef")) or undeposited
        add(deposit_to, deposit, inv.get("TxnDate"), inv, "Invoice", customer=cust)
        add(ar, -deposit, inv.get("TxnDate"), inv, "Invoice", customer=cust)
        tax = money((inv.get("TxnTaxDetail") or {}).get("TotalTax"))
        add(tax_payable, -tax, inv.get("TxnDate"), inv, "Invoice", customer=cust)
        for line in inv.get("Line") or []:
            if line.get("DetailType") == "SalesItemLineDetail":
                detail = line.get("SalesItemLineDetail") or {}
                add(
                    item_income_account(ctx, line),
                    -money(line.get("Amount")),
                    inv.get("TxnDate"),
                    inv,
                    "Invoice",
                    customer=cust,
                    class_id=ref_value(detail.get("ClassRef")),
                )
    for cm in _records(ctx, "CreditMemo"):
        if cm.get("_voided"):
            continue
        cust = ref_value(cm.get("CustomerRef"))
        add(ar, -money(cm.get("TotalAmt")), cm.get("TxnDate"), cm, "CreditMemo", customer=cust)
        tax = money((cm.get("TxnTaxDetail") or {}).get("TotalTax"))
        add(tax_payable, tax, cm.get("TxnDate"), cm, "CreditMemo", customer=cust)
        for line in cm.get("Line") or []:
            if line.get("DetailType") == "SalesItemLineDetail":
                detail = line.get("SalesItemLineDetail") or {}
                add(
                    item_income_account(ctx, line),
                    money(line.get("Amount")),
                    cm.get("TxnDate"),
                    cm,
                    "CreditMemo",
                    customer=cust,
                    class_id=ref_value(detail.get("ClassRef")),
                )
    for pay in _records(ctx, "Payment"):
        if pay.get("_voided"):
            continue
        cust = ref_value(pay.get("CustomerRef"))
        deposit_to = ref_value(pay.get("DepositToAccountRef")) or undeposited
        add(deposit_to, money(pay.get("TotalAmt")), pay.get("TxnDate"), pay, "Payment", customer=cust)
        add(ar, -money(pay.get("TotalAmt")), pay.get("TxnDate"), pay, "Payment", customer=cust)
    for sr in _records(ctx, "SalesReceipt"):
        if sr.get("_voided"):
            continue
        cust = ref_value(sr.get("CustomerRef"))
        deposit_to = ref_value(sr.get("DepositToAccountRef")) or undeposited
        add(deposit_to, money(sr.get("TotalAmt")), sr.get("TxnDate"), sr, "SalesReceipt", customer=cust)
        tax = money((sr.get("TxnTaxDetail") or {}).get("TotalTax"))
        add(tax_payable, -tax, sr.get("TxnDate"), sr, "SalesReceipt", customer=cust)
        for line in sr.get("Line") or []:
            if line.get("DetailType") == "SalesItemLineDetail":
                detail = line.get("SalesItemLineDetail") or {}
                add(
                    item_income_account(ctx, line),
                    -money(line.get("Amount")),
                    sr.get("TxnDate"),
                    sr,
                    "SalesReceipt",
                    customer=cust,
                    class_id=ref_value(detail.get("ClassRef")),
                )
    for bill in _records(ctx, "Bill"):
        if bill.get("_voided"):
            continue
        vendor = ref_value(bill.get("VendorRef"))
        ap_acct = ref_value(bill.get("APAccountRef")) or ap
        add(ap_acct, -money(bill.get("TotalAmt")), bill.get("TxnDate"), bill, "Bill", vendor=vendor)
        for line in bill.get("Line") or []:
            detail = line.get("AccountBasedExpenseLineDetail") or {}
            add(
                ref_value(detail.get("AccountRef")),
                money(line.get("Amount")),
                bill.get("TxnDate"),
                bill,
                "Bill",
                vendor=vendor,
                class_id=ref_value(detail.get("ClassRef")),
                customer=ref_value(detail.get("CustomerRef")),
            )
    for bp in _records(ctx, "BillPayment"):
        if bp.get("_voided"):
            continue
        vendor = ref_value(bp.get("VendorRef"))
        add(ap, money(bp.get("TotalAmt")), bp.get("TxnDate"), bp, "BillPayment", vendor=vendor)
        credit_acct = None
        if bp.get("CheckPayment"):
            credit_acct = ref_value((bp.get("CheckPayment") or {}).get("BankAccountRef"))
        if not credit_acct and bp.get("CreditCardPayment"):
            credit_acct = ref_value((bp.get("CreditCardPayment") or {}).get("CCAccountRef"))
        add(credit_acct, -money(bp.get("TotalAmt")), bp.get("TxnDate"), bp, "BillPayment", vendor=vendor)
    for purchase in _records(ctx, "Purchase"):
        if purchase.get("_voided"):
            continue
        add(
            ref_value(purchase.get("AccountRef")),
            -money(purchase.get("TotalAmt")),
            purchase.get("TxnDate"),
            purchase,
            "Purchase",
            vendor=ref_value(purchase.get("EntityRef")),
        )
        for line in purchase.get("Line") or []:
            detail = line.get("AccountBasedExpenseLineDetail") or {}
            add(
                ref_value(detail.get("AccountRef")),
                money(line.get("Amount")),
                purchase.get("TxnDate"),
                purchase,
                "Purchase",
                class_id=ref_value(detail.get("ClassRef")),
                customer=ref_value(detail.get("CustomerRef")),
            )
    for je in _records(ctx, "JournalEntry"):
        if je.get("_voided"):
            continue
        for line in je.get("Line") or []:
            detail = line.get("JournalEntryLineDetail") or {}
            amount = money(line.get("Amount"))
            if detail.get("PostingType") == "Credit":
                amount = -amount
            add(
                ref_value(detail.get("AccountRef")),
                amount,
                je.get("TxnDate"),
                je,
                "JournalEntry",
                class_id=ref_value(detail.get("ClassRef")),
                customer=ref_value((detail.get("Entity") or {}).get("EntityRef"))
                if isinstance(detail.get("Entity"), dict)
                else None,
            )
    for dep in _records(ctx, "Deposit"):
        if dep.get("_voided"):
            continue
        add(
            ref_value(dep.get("DepositToAccountRef")),
            money(dep.get("TotalAmt")),
            dep.get("TxnDate"),
            dep,
            "Deposit",
        )
        for line in dep.get("Line") or []:
            detail = line.get("DepositLineDetail") or {}
            add(
                ref_value(detail.get("AccountRef")),
                -money(line.get("Amount")),
                dep.get("TxnDate"),
                dep,
                "Deposit",
            )
    return out


def account_balances(
    ctx: ProviderContext, *, start: date | None = None, end: date | None = None, include_opening: bool = True
) -> dict[str, float]:
    """Debit-positive balances per account id."""
    balances: dict[str, float] = {}
    if include_opening:
        for account in ctx.store.collection("Account"):
            balances[str(account["Id"])] = money(account.get("_opening", 0.0))
    for posting in postings(ctx):
        if start and posting["date"] < start:
            continue
        if end and posting["date"] > end:
            continue
        balances[posting["account_id"]] = money(balances.get(posting["account_id"], 0.0) + posting["amount"])
    return balances


def display_balance(account: dict[str, Any], debit_balance: float) -> float:
    if account.get("Classification") in ("Asset", "Expense"):
        return money(debit_balance)
    return money(-debit_balance)


def refresh_computed(ctx: ProviderContext) -> None:
    """Refresh Account/Customer/Vendor/Invoice/Bill computed balance fields in place."""
    balances = account_balances(ctx)
    accounts = ctx.store.collection("Account")
    for account in accounts:
        debit = balances.get(str(account["Id"]), money(account.get("_opening", 0.0)))
        account["CurrentBalance"] = display_balance(account, debit)
        account["CurrentBalanceWithSubAccounts"] = account["CurrentBalance"]
    # sub-account rollup
    for account in accounts:
        parent_id = ref_value(account.get("ParentRef"))
        seen = {str(account["Id"])}
        while parent_id and parent_id not in seen and accounts.get(parent_id):
            seen.add(parent_id)
            parent = accounts.get(parent_id)
            assert parent is not None
            parent["CurrentBalanceWithSubAccounts"] = money(
                parent.get("CurrentBalanceWithSubAccounts", 0.0) + account.get("CurrentBalance", 0.0)
            )
            parent_id = ref_value(parent.get("ParentRef"))

    # Invoice balances from linked payments / credit memos
    paid_by_invoice: dict[str, float] = {}
    for pay in _records(ctx, "Payment"):
        if pay.get("_voided"):
            continue
        for line in pay.get("Line") or []:
            for linked in line.get("LinkedTxn") or []:
                if linked.get("TxnType") == "Invoice":
                    tid = str(linked.get("TxnId"))
                    paid_by_invoice[tid] = money(paid_by_invoice.get(tid, 0.0) + money(line.get("Amount")))
        if not pay.get("Line") and pay.get("TotalAmt"):
            pass
    invoices = ctx.store.collection("Invoice")
    for inv in invoices:
        if inv.get("_deleted"):
            continue
        if inv.get("_voided"):
            inv["Balance"] = 0.0
            continue
        paid = paid_by_invoice.get(str(inv["Id"]), 0.0)
        inv["Balance"] = money(max(money(inv.get("TotalAmt")) - paid - money(inv.get("Deposit")), 0.0))

    paid_by_bill: dict[str, float] = {}
    for bp in _records(ctx, "BillPayment"):
        if bp.get("_voided"):
            continue
        for line in bp.get("Line") or []:
            for linked in line.get("LinkedTxn") or []:
                if linked.get("TxnType") == "Bill":
                    tid = str(linked.get("TxnId"))
                    paid_by_bill[tid] = money(paid_by_bill.get(tid, 0.0) + money(line.get("Amount")))
    for bill in ctx.store.collection("Bill"):
        if bill.get("_deleted") or bill.get("_voided"):
            if bill.get("_voided"):
                bill["Balance"] = 0.0
            continue
        paid = paid_by_bill.get(str(bill["Id"]), 0.0)
        bill["Balance"] = money(max(money(bill.get("TotalAmt")) - paid, 0.0))

    customers = ctx.store.collection("Customer")
    own: dict[str, float] = {}
    for inv in invoices:
        if inv.get("_deleted") or inv.get("_voided"):
            continue
        cust = ref_value(inv.get("CustomerRef"))
        if cust:
            own[cust] = money(own.get(cust, 0.0) + money(inv.get("Balance")))
    # unapplied credits and payments reduce the customer balance in QBO
    for cm in _records(ctx, "CreditMemo"):
        if cm.get("_voided"):
            continue
        cust = ref_value(cm.get("CustomerRef"))
        if cust:
            own[cust] = money(own.get(cust, 0.0) - money(cm.get("Balance", cm.get("TotalAmt"))))
    for pay in _records(ctx, "Payment"):
        if pay.get("_voided"):
            continue
        cust = ref_value(pay.get("CustomerRef"))
        if cust and money(pay.get("UnappliedAmt")):
            own[cust] = money(own.get(cust, 0.0) - money(pay.get("UnappliedAmt")))
    for cust in customers:
        cust["Balance"] = money(own.get(str(cust["Id"]), 0.0))
        cust["BalanceWithJobs"] = cust["Balance"]
    for cust in customers:
        parent_id = ref_value(cust.get("ParentRef"))
        seen = {str(cust["Id"])}
        while parent_id and parent_id not in seen and customers.get(parent_id):
            seen.add(parent_id)
            parent = customers.get(parent_id)
            assert parent is not None
            parent["BalanceWithJobs"] = money(parent.get("BalanceWithJobs", 0.0) + cust.get("Balance", 0.0))
            parent_id = ref_value(parent.get("ParentRef"))

    vendor_open: dict[str, float] = {}
    for bill in ctx.store.collection("Bill"):
        if bill.get("_deleted") or bill.get("_voided"):
            continue
        vendor = ref_value(bill.get("VendorRef"))
        if vendor:
            vendor_open[vendor] = money(vendor_open.get(vendor, 0.0) + money(bill.get("Balance")))
    for vendor in ctx.store.collection("Vendor"):
        vendor["Balance"] = money(vendor_open.get(str(vendor["Id"]), 0.0))
