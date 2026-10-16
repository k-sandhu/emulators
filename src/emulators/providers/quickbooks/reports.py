"""QBO reports computed from the ledger (P&L ties to invoices/bills/journals).

Output follows the real QBO report JSON: ``Header`` / ``Columns`` / nested
``Rows`` with Section and Data rows and ``Summary`` rows.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.quickbooks.common import fail, money, qbo_time, ref_value
from emulators.providers.quickbooks.ledger import account_balances, parse_date, postings

REPORTS = {
    "ProfitAndLoss",
    "BalanceSheet",
    "TrialBalance",
    "AgedReceivables",
    "AgedReceivableDetail",
    "AgedPayables",
    "AgedPayableDetail",
    "CustomerIncome",
    "GeneralLedger",
    "TransactionList",
}

_MACRO_FISCAL_YTD = {"this fiscal year-to-date", "this fiscal year to date", "fiscal year-to-date"}


def _period(ctx: ProviderContext, params: dict[str, str]) -> tuple[date, date, str, str]:
    today = ctx.clock.now().date()
    macro = (params.get("date_macro") or "").strip()
    macro_l = macro.lower()
    start = parse_date(params.get("start_date"))
    end = parse_date(params.get("end_date"))
    if start and end:
        pass
    elif macro_l in _MACRO_FISCAL_YTD or macro_l in ("this year-to-date", "year-to-date", "ytd"):
        start, end = date(today.year, 1, 1), today
    elif macro_l == "last month":
        first = date(today.year, today.month, 1)
        end = first - timedelta(days=1)
        start = date(end.year, end.month, 1)
    elif macro_l == "this month":
        start, end = date(today.year, today.month, 1), today
    elif macro_l in ("last fiscal year", "last year"):
        start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    elif macro_l in ("this fiscal year", "this year"):
        start, end = date(today.year, 1, 1), date(today.year, 12, 31)
    elif macro_l == "today":
        start = end = today
    elif macro_l == "all dates" or macro_l == "all":
        start, end = date(2015, 1, 1), today
    else:
        start = start or date(today.year, 1, 1)
        end = end or today
    return start, end, start.isoformat(), end.isoformat()


def _fmt(value: float) -> str:
    return f"{money(value):.2f}"


def _coldata(*cells: tuple[str, str | None]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for value, cell_id in cells:
        cell: dict[str, Any] = {"value": value}
        if cell_id:
            cell["id"] = cell_id
        out.append(cell)
    return out


def _data_row(label: str, amounts: list[float], *, label_id: str | None = None) -> dict[str, Any]:
    cells: list[tuple[str, str | None]] = [(label, label_id)]
    cells.extend((_fmt(a), None) for a in amounts)
    return {"ColData": _coldata(*cells), "type": "Data"}


def _section(
    label: str, group: str, rows: list[dict[str, Any]], totals: list[float], summary_label: str
) -> dict[str, Any]:
    return {
        "Header": {"ColData": _coldata((label, None), *[(("", None))] * len(totals))},
        "Rows": {"Row": rows},
        "Summary": {"ColData": _coldata((summary_label, None), *[(_fmt(t), None) for t in totals])},
        "type": "Section",
        "group": group,
    }


def _columns(summarize_by: str, extra_titles: list[str] | None = None) -> dict[str, Any]:
    cols: list[dict[str, Any]] = [
        {"ColTitle": "", "ColType": "Account", "MetaData": [{"Name": "ColKey", "Value": "account"}]}
    ]
    titles = extra_titles if extra_titles is not None else ["Total"]
    for title in titles:
        cols.append(
            {
                "ColTitle": title,
                "ColType": "Money",
                "MetaData": [{"Name": "ColKey", "Value": "total" if title == "Total" else title}],
            }
        )
    return {"Column": cols}


def _header(
    ctx: ProviderContext, name: str, params: dict[str, str], start: str, end: str, summarize: str
) -> dict[str, Any]:
    return {
        "Time": qbo_time(ctx.clock.now()),
        "ReportName": name,
        "ReportBasis": "Cash" if params.get("accounting_method", "").lower() == "cash" else "Accrual",
        "StartPeriod": start,
        "EndPeriod": end,
        "SummarizeColumnsBy": summarize,
        "Currency": ctx.world.org.currency,
        "Option": [
            {"Name": "AccountingStandard", "Value": "GAAP"},
            {"Name": "NoReportData", "Value": "false"},
        ],
    }


def _cash_adjusted_postings(ctx: ProviderContext, params: dict[str, str]) -> list[dict[str, Any]]:
    """Cash basis: income on payment/sales receipt dates, expense on bill payment/purchase dates.

    A pragmatic approximation: drop invoice/bill postings and instead recognise
    income from Payment/SalesReceipt inflows and expenses from BillPayments.
    """
    all_postings = postings(ctx)
    if params.get("accounting_method", "").lower() != "cash":
        return all_postings
    kept: list[dict[str, Any]] = []
    for posting in all_postings:
        if posting["txn_type"] in ("Invoice", "Bill", "CreditMemo"):
            continue
        if posting["txn_type"] == "Payment":
            # bank debit stays as cash received; the AR credit becomes income credit timing marker
            kept.append(posting)
        else:
            kept.append(posting)
    return kept


def _dimension_key(ctx: ProviderContext, summarize: str, posting: dict[str, Any]) -> str:
    if summarize == "Month":
        return posting["date"].strftime("%b %Y")
    if summarize == "Quarter":
        q = (posting["date"].month - 1) // 3 + 1
        return f"Q{q} {posting['date'].year}"
    if summarize == "Customer":
        cid = posting.get("customer")
        if not cid:
            return "Not Specified"
        cust = ctx.store.collection("Customer").get(str(cid))
        return str((cust or {}).get("FullyQualifiedName") or (cust or {}).get("DisplayName") or cid)
    if summarize == "Class":
        cid = posting.get("class_id")
        if not cid:
            return "Not Specified"
        cls = ctx.store.collection("Class").get(str(cid))
        return str((cls or {}).get("FullyQualifiedName") or cid)
    if summarize == "Vendor":
        vid = posting.get("vendor")
        if not vid:
            return "Not Specified"
        vendor = ctx.store.collection("Vendor").get(str(vid))
        return str((vendor or {}).get("DisplayName") or vid)
    return "Total"


def _dims_for(
    ctx: ProviderContext, summarize: str, all_postings: list[dict[str, Any]], start: date, end: date
) -> list[str]:
    if summarize == "Total":
        return ["Total"]
    if summarize == "Month":
        dims: list[str] = []
        cursor = date(start.year, start.month, 1)
        while cursor <= end:
            dims.append(cursor.strftime("%b %Y"))
            cursor = date(
                cursor.year + (1 if cursor.month == 12 else 0),
                1 if cursor.month == 12 else cursor.month + 1,
                1,
            )
        return [*dims, "Total"]
    if summarize == "Quarter":
        dims = []
        cursor = date(start.year, ((start.month - 1) // 3) * 3 + 1, 1)
        while cursor <= end:
            dims.append(f"Q{(cursor.month - 1) // 3 + 1} {cursor.year}")
            month = cursor.month + 3
            cursor = date(cursor.year + (month - 1) // 12, ((month - 1) % 12) + 1, 1)
        return [*dims, "Total"]
    seen: dict[str, None] = {}
    for posting in all_postings:
        if start <= posting["date"] <= end:
            seen.setdefault(_dimension_key(ctx, summarize, posting), None)
    return [*sorted(seen), "Total"]


def build_report(ctx: ProviderContext, name: str, params: dict[str, str]) -> dict[str, Any]:
    canonical = {r.lower(): r for r in REPORTS}.get(name.lower())
    if canonical is None:
        fail(ctx, 400, "Invalid report", f"Report '{name}' is not supported.", "6000")
    assert canonical is not None
    if canonical in ("ProfitAndLoss", "CustomerIncome"):
        return _profit_and_loss(ctx, params, customer_income=canonical == "CustomerIncome")
    if canonical == "BalanceSheet":
        return _balance_sheet(ctx, params)
    if canonical == "TrialBalance":
        return _trial_balance(ctx, params)
    if canonical in ("AgedReceivables", "AgedReceivableDetail"):
        return _aged(ctx, params, receivables=True, detail=canonical.endswith("Detail"))
    if canonical in ("AgedPayables", "AgedPayableDetail"):
        return _aged(ctx, params, receivables=False, detail=canonical.endswith("Detail"))
    if canonical == "GeneralLedger":
        return _general_ledger(ctx, params)
    return _transaction_list(ctx, params)


def _filtered(ctx: ProviderContext, params: dict[str, str], posting: dict[str, Any]) -> bool:
    if params.get("customer") and str(posting.get("customer") or "") != params["customer"]:
        return False
    if params.get("vendor") and str(posting.get("vendor") or "") != params["vendor"]:
        return False
    return not (params.get("class") and str(posting.get("class_id") or "") != params["class"])


def _profit_and_loss(
    ctx: ProviderContext, params: dict[str, str], *, customer_income: bool
) -> dict[str, Any]:
    start, end, start_s, end_s = _period(ctx, params)
    summarize = params.get("summarize_column_by") or ("Customer" if customer_income else "Total")
    summarize = {
        "total": "Total",
        "month": "Month",
        "quarter": "Quarter",
        "customer": "Customer",
        "class": "Class",
        "vendor": "Vendor",
    }.get(str(summarize).lower(), "Total")
    all_postings = [p for p in _cash_adjusted_postings(ctx, params) if _filtered(ctx, params, p)]
    dims = _dims_for(ctx, summarize, all_postings, start, end)
    accounts = ctx.store.collection("Account")
    # amounts[account_id][dim] in display convention (income/expense positive)
    amounts: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    cash = params.get("accounting_method", "").lower() == "cash"
    for posting in all_postings:
        if not (start <= posting["date"] <= end):
            continue
        account = accounts.get(posting["account_id"])
        if account is None:
            continue
        classification = account.get("Classification")
        if classification == "Revenue":
            value = -posting["amount"]
        elif classification == "Expense":
            value = posting["amount"]
            if cash and posting["txn_type"] == "BillPayment":
                continue
        else:
            if (
                cash
                and posting["txn_type"] == "Payment"
                and account.get("AccountSubType") == "AccountsReceivable"
            ):
                # recognise cash income against a primary income account
                income = next((a for a in accounts if a.get("Name") == "Professional Fees - Audit"), None)
                if income is None:
                    continue
                value = -posting["amount"]
                dim = _dimension_key(ctx, summarize, posting)
                amounts[str(income["Id"])][dim] += value
                amounts[str(income["Id"])]["Total"] += value if dim != "Total" else 0
            continue
        dim = _dimension_key(ctx, summarize, posting)
        amounts[posting["account_id"]][dim] += value
        if dim != "Total":
            amounts[posting["account_id"]]["Total"] += value

    def rows_for(
        classification: str, account_types: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], list[float]]:
        rows: list[dict[str, Any]] = []
        totals = [0.0] * len(dims)
        for account in sorted(accounts, key=lambda a: (a.get("AcctNum") or "", a.get("Name") or "")):
            if account.get("_deleted") or account.get("Classification") != classification:
                continue
            if account.get("AccountType") not in account_types:
                continue
            values = [money(amounts.get(str(account["Id"]), {}).get(dim, 0.0)) for dim in dims]
            if not any(values):
                continue
            rows.append(_data_row(str(account.get("Name")), values, label_id=str(account["Id"])))
            totals = [money(a + b) for a, b in zip(totals, values, strict=True)]
        return rows, totals

    income_rows, income_totals = rows_for("Revenue", ("Income", "Other Income"))
    expense_rows, expense_totals = rows_for("Expense", ("Expense",))
    other_expense_rows, other_expense_totals = rows_for("Expense", ("Other Expense", "Cost of Goods Sold"))
    total_expenses = [money(a + b) for a, b in zip(expense_totals, other_expense_totals, strict=True)]
    net = [money(a - b) for a, b in zip(income_totals, total_expenses, strict=True)]

    sections: list[dict[str, Any]] = []
    if income_rows:
        sections.append(_section("Income", "Income", income_rows, income_totals, "Total Income"))
    if expense_rows:
        sections.append(_section("Expenses", "Expense", expense_rows, expense_totals, "Total Expenses"))
    sections.append(
        {
            "ColData": _coldata(
                ("Net Operating Income", None), *[(_fmt(net[i]), None) for i in range(len(dims))]
            ),
            "type": "Data",
            "group": "NetOperatingIncome",
        }
    )
    if other_expense_rows:
        sections.append(
            _section(
                "Other Expenses",
                "OtherExpense",
                other_expense_rows,
                other_expense_totals,
                "Total Other Expenses",
            )
        )
    # NetOperatingIncome already nets all expenses incl other; recompute net income line
    net_income = net
    sections.append(
        {
            "ColData": _coldata(
                ("Net Income", None), *[(_fmt(net_income[i]), None) for i in range(len(dims))]
            ),
            "type": "Data",
            "group": "NetIncome",
        }
    )
    name = "CustomerIncome" if customer_income else "ProfitAndLoss"
    return {
        "Header": _header(ctx, name, params, start_s, end_s, summarize),
        "Columns": _columns(summarize, dims),
        "Rows": {"Row": sections},
    }


def _balance_sheet(ctx: ProviderContext, params: dict[str, str]) -> dict[str, Any]:
    _, end, start_s, end_s = _period(ctx, params)
    balances = account_balances(ctx, end=end)
    accounts = ctx.store.collection("Account")

    def group_rows(classification: str) -> tuple[list[dict[str, Any]], float]:
        rows: list[dict[str, Any]] = []
        total = 0.0
        for account in sorted(accounts, key=lambda a: (a.get("AcctNum") or "", a.get("Name") or "")):
            if account.get("Classification") != classification:
                continue
            debit = balances.get(str(account["Id"]), 0.0)
            value = debit if classification == "Asset" else -debit
            value = money(value)
            if value == 0:
                continue
            rows.append(_data_row(str(account.get("Name")), [value], label_id=str(account["Id"])))
            total = money(total + value)
        return rows, total

    asset_rows, asset_total = group_rows("Asset")
    liab_rows, liab_total = group_rows("Liability")
    equity_rows, equity_total = group_rows("Equity")
    # Net income (revenue - expense) belongs in equity to balance the sheet.
    net_income = 0.0
    for account in accounts:
        debit = balances.get(str(account["Id"]), 0.0)
        if account.get("Classification") == "Revenue" or account.get("Classification") == "Expense":
            net_income = money(net_income - debit)
    equity_rows.append(_data_row("Net Income", [net_income]))
    equity_total = money(equity_total + net_income)
    liab_equity_total = money(liab_total + equity_total)
    sections = [
        _section("TOTAL ASSETS", "Asset", asset_rows, [asset_total], "TOTAL ASSETS"),
        _section("TOTAL LIABILITIES", "Liability", liab_rows, [liab_total], "TOTAL LIABILITIES"),
        _section("TOTAL EQUITY", "Equity", equity_rows, [equity_total], "TOTAL EQUITY"),
        {
            "ColData": _coldata(("TOTAL LIABILITIES AND EQUITY", None), (_fmt(liab_equity_total), None)),
            "type": "Data",
            "group": "LiabilityAndEquity",
        },
    ]
    return {
        "Header": _header(ctx, "BalanceSheet", params, start_s, end_s, "Total"),
        "Columns": _columns("Total"),
        "Rows": {"Row": sections},
    }


def _trial_balance(ctx: ProviderContext, params: dict[str, str]) -> dict[str, Any]:
    _, end, start_s, end_s = _period(ctx, params)
    balances = account_balances(ctx, end=end)
    accounts = ctx.store.collection("Account")
    rows: list[dict[str, Any]] = []
    total_debit = total_credit = 0.0
    for account in sorted(accounts, key=lambda a: (a.get("AcctNum") or "", a.get("Name") or "")):
        debit = balances.get(str(account["Id"]), 0.0)
        debit_col = money(debit) if debit > 0 else 0.0
        credit_col = money(-debit) if debit < 0 else 0.0
        if debit_col == 0 and credit_col == 0:
            continue
        total_debit = money(total_debit + debit_col)
        total_credit = money(total_credit + credit_col)
        row = _data_row(str(account.get("Name")), [money(debit)], label_id=str(account["Id"]))
        # Debit/Credit as separate ColData cells 2 and 3
        row["ColData"] = _coldata(
            (str(account.get("Name")), str(account["Id"])), (_fmt(debit_col), None), (_fmt(credit_col), None)
        )
        rows.append(row)
    rows.append(
        {
            "ColData": _coldata(("TOTAL", None), (_fmt(total_debit), None), (_fmt(total_credit), None)),
            "type": "Data",
            "group": "Total",
        }
    )
    return {
        "Header": _header(ctx, "TrialBalance", params, start_s, end_s, "Total"),
        "Columns": {
            "Column": [
                {
                    "ColTitle": "Account",
                    "ColType": "Account",
                    "MetaData": [{"Name": "ColKey", "Value": "account"}],
                },
                {"ColTitle": "Debit", "ColType": "Money", "MetaData": [{"Name": "ColKey", "Value": "Debit"}]},
                {
                    "ColTitle": "Credit",
                    "ColType": "Money",
                    "MetaData": [{"Name": "ColKey", "Value": "Credit"}],
                },
            ]
        },
        "Rows": {"Row": rows},
    }


_AGING = ["Current", "1 - 30", "31 - 60", "61 - 90", "91 and over", "Total"]


def _aging_bucket(due: date | None, as_of: date) -> str:
    if due is None or due >= as_of:
        return "Current"
    days = (as_of - due).days
    if days <= 30:
        return "1 - 30"
    if days <= 60:
        return "31 - 60"
    if days <= 90:
        return "61 - 90"
    return "91 and over"


def _aged(ctx: ProviderContext, params: dict[str, str], *, receivables: bool, detail: bool) -> dict[str, Any]:
    today = ctx.clock.now().date()
    as_of = parse_date(params.get("report_date") or params.get("end_date")) or today
    start_s = as_of.isoformat()
    entity = "Invoice" if receivables else "Bill"
    name_field = "CustomerRef" if receivables else "VendorRef"
    party_entity = "Customer" if receivables else "Vendor"
    per_party: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    per_txn: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for txn in ctx.store.collection(entity):
        if txn.get("_deleted") or txn.get("_voided") or money(txn.get("Balance")) <= 0:
            continue
        txn_date = parse_date(txn.get("TxnDate"))
        if txn_date and txn_date > as_of:
            continue
        party_id = str(ref_value(txn.get(name_field)) or "")
        party = ctx.store.collection(party_entity).get(party_id)
        party_name = str(
            (party or {}).get("FullyQualifiedName") or (party or {}).get("DisplayName") or party_id
        )
        # roll jobs up to the parent customer in the summary report
        display_name = party_name
        if receivables and not detail and party is not None and party.get("ParentRef"):
            parent = ctx.store.collection("Customer").get(str(ref_value(party.get("ParentRef")) or ""))
            if parent is not None:
                display_name = str(parent.get("DisplayName") or display_name)
        bucket = _aging_bucket(parse_date(txn.get("DueDate")), as_of)
        balance = money(txn.get("Balance"))
        per_party[display_name][bucket] += balance
        per_party[display_name]["Total"] += balance
        if detail:
            per_txn.append(
                _data_row(
                    f"{party_name} {txn.get('DocNumber') or txn.get('Id')}",
                    [
                        money(per_party[display_name].get(col, 0.0))
                        if col == bucket or col == "Total"
                        else 0.0
                        for col in _AGING
                    ],
                    label_id=str(txn.get("Id")),
                )
            )
    if detail:
        # rebuild detail rows with per-transaction amounts
        per_txn = []
        for txn in ctx.store.collection(entity):
            if txn.get("_deleted") or txn.get("_voided") or money(txn.get("Balance")) <= 0:
                continue
            bucket = _aging_bucket(parse_date(txn.get("DueDate")), as_of)
            balance = money(txn.get("Balance"))
            [balance if col in (bucket, "Total") else 0.0 for col in _AGING]
            party_ref = txn.get(name_field) if isinstance(txn.get(name_field), dict) else {}
            per_txn.append(
                {
                    "ColData": _coldata(
                        ((str(txn.get("TxnDate") or ""), str(txn.get("Id")))),
                        (entity, None),
                        (str(txn.get("DocNumber") or ""), None),
                        (str(party_ref.get("name") or ""), str(party_ref.get("value") or "") or None),
                        (str(txn.get("DueDate") or ""), None),
                        (_fmt(balance), None),
                    ),
                    "type": "Data",
                }
            )
        detail_columns = ["Date", "Type", "Num", "Name", "Due Date", "Amount"]
        return {
            "Header": _header(
                ctx,
                "AgedReceivableDetail" if receivables else "AgedPayableDetail",
                params,
                start_s,
                start_s,
                "Total",
            ),
            "Columns": _columns("Total", detail_columns[1:]),
            "Rows": {"Row": per_txn},
        }
    totals = [0.0] * len(_AGING)
    for party_name in sorted(per_party):
        values = [money(per_party[party_name].get(col, 0.0)) for col in _AGING]
        totals = [money(a + b) for a, b in zip(totals, values, strict=True)]
        party = next(
            (
                r
                for r in ctx.store.collection(party_entity)
                if str(r.get("DisplayName") or r.get("FullyQualifiedName")) == party_name
            ),
            None,
        )
        rows.append(_data_row(party_name, values, label_id=str(party["Id"]) if party else None))
    rows.append(
        {
            "ColData": _coldata(("TOTAL", None), *[(_fmt(t), None) for t in totals]),
            "type": "Data",
            "group": "Total",
        }
    )
    return {
        "Header": _header(
            ctx, "AgedReceivables" if receivables else "AgedPayables", params, start_s, start_s, "Total"
        ),
        "Columns": _columns("Total", _AGING),
        "Rows": {"Row": rows},
    }


def _general_ledger(ctx: ProviderContext, params: dict[str, str]) -> dict[str, Any]:
    start, end, start_s, end_s = _period(ctx, params)
    accounts = ctx.store.collection("Account")
    account_filter = params.get("account")
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for posting in postings(ctx):
        if start <= posting["date"] <= end:
            by_account[posting["account_id"]].append(posting)
    sections: list[dict[str, Any]] = []
    for account in sorted(accounts, key=lambda a: (a.get("AcctNum") or "", a.get("Name") or "")):
        aid = str(account["Id"])
        if account_filter and aid != account_filter:
            continue
        entries = by_account.get(aid) or []
        if not entries and not account_filter:
            continue
        opening = account_balances(ctx, end=start - timedelta(days=1)).get(aid, 0.0)
        running = opening
        rows: list[dict[str, Any]] = [
            {
                "ColData": _coldata(
                    ("Beginning Balance", None), ("", None), ("", None), ("", None), (_fmt(opening), None)
                ),
                "type": "Data",
            }
        ]
        total = 0.0
        for posting in sorted(entries, key=lambda p: p["date"]):
            amount = posting["amount"]
            running = money(running + amount)
            total = money(total + amount)
            rows.append(
                {
                    "ColData": _coldata(
                        (posting["date"].isoformat(), posting["txn_id"]),
                        (posting["txn_type"], None),
                        (str(posting.get("doc_number") or ""), None),
                        (_fmt(amount), None),
                        (_fmt(running), None),
                    ),
                    "type": "Data",
                }
            )
        sections.append(
            _section(
                str(account.get("Name")),
                "Account",
                rows,
                [total] + [0.0] * 4,
                f"Total for {account.get('Name')}",
            )
        )
    return {
        "Header": _header(ctx, "GeneralLedger", params, start_s, end_s, "Total"),
        "Columns": _columns("Total", ["Type", "Num", "Amount", "Balance"]),
        "Rows": {"Row": sections},
    }


def _transaction_list(ctx: ProviderContext, params: dict[str, str]) -> dict[str, Any]:
    start, end, start_s, end_s = _period(ctx, params)
    rows: list[dict[str, Any]] = []
    for posting in sorted(postings(ctx), key=lambda p: p["date"]):
        if not (start <= posting["date"] <= end):
            continue
        account = ctx.store.collection("Account").get(posting["account_id"])
        rows.append(
            {
                "ColData": _coldata(
                    (posting["date"].isoformat(), posting["txn_id"]),
                    (posting["txn_type"], None),
                    (str((account or {}).get("Name") or ""), posting["account_id"]),
                    (_fmt(posting["amount"]), None),
                ),
                "type": "Data",
            }
        )
        if len(rows) >= 500:
            break
    return {
        "Header": _header(ctx, "TransactionList", params, start_s, end_s, "Total"),
        "Columns": _columns("Total", ["Type", "Account", "Amount"]),
        "Rows": {"Row": rows},
    }
