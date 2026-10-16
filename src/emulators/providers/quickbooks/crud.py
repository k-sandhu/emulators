"""Generic QBO entity CRUD: validation, totals, SyncToken, void/delete.

One engine drives every entity; per-entity differences are the required-field
table, unique-name field and the totals/linkage rules in this module.
"""

from __future__ import annotations

import copy
import re
from datetime import date, timedelta
from typing import Any

from emulators.core.clock import utc
from emulators.core.provider import ProviderContext
from emulators.providers.quickbooks.common import (
    NAME_LIST,
    REQUIRED,
    TRANSACTIONS,
    UNIQUE_NAME,
    VOIDABLE,
    fail,
    money,
    not_found,
    public,
    qbo_time,
    ref_value,
    stamp_new,
    touch,
)
from emulators.providers.quickbooks.ledger import refresh_computed
from emulators.providers.quickbooks.validation import validate_business, validate_shape

REF_TARGETS: dict[str, str] = {
    "CustomerRef": "Customer",
    "VendorRef": "Vendor",
    "EmployeeRef": "Employee",
    "ItemRef": "Item",
    "AccountRef": "Account",
    "ClassRef": "Class",
    "DepartmentRef": "Department",
    "SalesTermRef": "Term",
    "TermRef": "Term",
    "PaymentMethodRef": "PaymentMethod",
    "TaxCodeRef": "TaxCode",
    "TaxRateRef": "TaxRate",
    "AgencyRef": "TaxAgency",
    "DepositToAccountRef": "Account",
    "APAccountRef": "Account",
    "ARAccountRef": "Account",
    "IncomeAccountRef": "Account",
    "ExpenseAccountRef": "Account",
    "AssetAccountRef": "Account",
    "BankAccountRef": "Account",
    "CCAccountRef": "Account",
    "DefaultTaxCodeRef": "TaxCode",
}
SKIP_REF_VALUES = {"NON", "TAX", "USD", "CAD", ""}

Event = tuple[str, str, str]  # (entity, id, operation)


def collection(ctx: ProviderContext, entity: str):
    return ctx.store.collection(entity)


def get_record(ctx: ProviderContext, entity: str, record_id: str) -> dict[str, Any]:
    record = collection(ctx, entity).get(str(record_id))
    if record is None or record.get("_deleted"):
        not_found(ctx, entity)
    assert record is not None
    return record


def _walk_refs(value: Any) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith("Ref") and isinstance(child, dict) and "value" in child:
                found.append((key, child))
            found.extend(_walk_refs(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_refs(child))
    return found


def validate_refs(ctx: ProviderContext, entity: str, payload: dict[str, Any]) -> None:
    for field_name, ref in _walk_refs(payload):
        value = str(ref.get("value", ""))
        if field_name == "CurrencyRef" or (field_name == "TaxCodeRef" and value in {"NON", "TAX"}):
            continue
        target_entity = entity if field_name == "ParentRef" else REF_TARGETS.get(field_name)
        if target_entity is None:
            continue
        target = collection(ctx, target_entity).get(value)
        if target is None or target.get("_deleted") or target.get("Active") is False:
            not_found(ctx, target_entity)
        if not ref.get("name") and target is not None:
            if target.get("ParentRef") and target.get("FullyQualifiedName"):
                name = target["FullyQualifiedName"]
            else:
                name = target.get("DisplayName") or target.get("Name") or target.get("FullyQualifiedName")
            if name:
                ref["name"] = str(name)


def check_required(ctx: ProviderContext, entity: str, payload: dict[str, Any]) -> None:
    for field_name in REQUIRED.get(entity, []):
        value = payload.get(field_name)
        if value is None or value == "" or value == []:
            fail(
                ctx,
                400,
                f"Required param missing, need to supply the required value for the API: {field_name}",
                f"Required parameter {field_name} is missing in the request",
                "2020",
                element=field_name,
            )


def check_unique_name(
    ctx: ProviderContext, entity: str, payload: dict[str, Any], exclude_id: str | None = None
) -> None:
    field_name = UNIQUE_NAME.get(entity)
    if not field_name:
        return
    value = payload.get(field_name)
    if not value:
        return
    entities = (
        ("Customer", "Vendor", "Employee") if entity in ("Customer", "Vendor", "Employee") else (entity,)
    )
    for record in [r for name in entities for r in collection(ctx, name)]:
        if record.get("_deleted"):
            continue
        if exclude_id and record is collection(ctx, entity).get(exclude_id):
            continue
        if entity in ("Account", "Class", "Department", "Item") and ref_value(
            record.get("ParentRef")
        ) != ref_value(payload.get("ParentRef")):
            continue
        if str(record.get(field_name, "")).lower() == str(value).lower():
            fail(
                ctx,
                400,
                "Duplicate Name Exists Error",
                f"The name supplied already exists. : Another {entity.lower()} is already using this name. "
                "Please use a different name.",
                "6240",
                element=field_name,
            )


def _tax_rate_for_code(ctx: ProviderContext, code_ref: Any) -> float:
    code_id = ref_value(code_ref)
    if not code_id or code_id in SKIP_REF_VALUES:
        return 0.0
    code = collection(ctx, "TaxCode").get(code_id)
    if code is None:
        return 0.0
    return float(code.get("_rate", 0.0) or 0.0)


def compute_totals(ctx: ProviderContext, entity: str, record: dict[str, Any]) -> None:
    """Recompute line amounts, TotalAmt / Balance / tax for transaction payloads."""
    lines = record.get("Line")
    if entity in ("Invoice", "SalesReceipt", "Estimate", "CreditMemo") and isinstance(lines, list):
        subtotal = 0.0
        tax_total = 0.0
        tax_lines: list[dict[str, Any]] = []
        sales_lines: list[dict[str, Any]] = []
        for idx, line in enumerate(lines, start=1):
            if line.get("DetailType") == "SalesItemLineDetail":
                detail = line.setdefault("SalesItemLineDetail", {})
                item = collection(ctx, "Item").get(ref_value(detail.get("ItemRef")))
                detail.setdefault("Qty", 1)
                detail.setdefault("UnitPrice", (item or {}).get("UnitPrice", 0))
                qty = detail.get("Qty", 1)
                unit = detail.get("UnitPrice", 0)
                if line.get("Amount") is None:
                    line["Amount"] = money(float(qty or 0) * float(unit or 0))
                amount = money(line.get("Amount"))
                subtotal = money(subtotal + amount)
                rate = _tax_rate_for_code(ctx, detail.get("TaxCodeRef"))
                line_tax = money(amount * rate / 100.0)
                tax_total = money(tax_total + line_tax)
                line.setdefault("Id", str(idx))
                line.setdefault("LineNum", idx)
                sales_lines.append(line)
                if line_tax:
                    tax_lines.append(
                        {
                            "Amount": line_tax,
                            "DetailType": "TaxLineDetail",
                            "TaxLineDetail": {
                                "TaxRateRef": copy.deepcopy(
                                    (
                                        (
                                            collection(ctx, "TaxCode").get(
                                                ref_value(detail.get("TaxCodeRef"))
                                            )
                                            or {}
                                        ).get("SalesTaxRateList")
                                        or {}
                                    )
                                    .get("TaxRateDetail", [{}])[0]
                                    .get("TaxRateRef", {})
                                ),
                                "PercentBased": True,
                                "TaxPercent": rate,
                                "NetAmountTaxable": amount,
                            },
                        }
                    )
        # honour an explicitly supplied tax total (seeded world amounts)
        supplied = (
            (record.get("TxnTaxDetail") or {}).get("TotalTax")
            if isinstance(record.get("TxnTaxDetail"), dict)
            else None
        )
        if supplied is not None:
            tax_total = money(supplied)
            tax_lines = (record.get("TxnTaxDetail") or {}).get("TaxLine") or tax_lines
        # rebuild Line: sales lines + a SubTotal line like QBO returns
        other = [
            ln for ln in lines if ln.get("DetailType") not in ("SalesItemLineDetail", "SubTotalLineDetail")
        ]
        record["Line"] = [
            *sales_lines,
            {"Amount": subtotal, "DetailType": "SubTotalLineDetail", "SubTotalLineDetail": {}},
            *other,
        ]
        record["TxnTaxDetail"] = {"TotalTax": tax_total, "TaxLine": tax_lines}
        record["TotalAmt"] = money(subtotal + tax_total)
        if entity in ("Invoice", "CreditMemo"):
            existing_balance = record.get("Balance")
            if existing_balance is None:
                record["Balance"] = record["TotalAmt"]
    elif entity in ("Bill", "Purchase", "Deposit") and isinstance(lines, list):
        total = 0.0
        for idx, line in enumerate(lines, start=1):
            line.setdefault("Id", str(idx))
            line.setdefault("LineNum", idx)
            total = money(total + money(line.get("Amount")))
        record["TotalAmt"] = total
        if entity == "Bill" and record.get("Balance") is None:
            record["Balance"] = total
    elif entity in ("Payment", "BillPayment") and isinstance(lines, list):
        applied = money(sum(money(ln.get("Amount")) for ln in lines))
        if record.get("TotalAmt") is None:
            record["TotalAmt"] = applied
        if entity == "Payment":
            record["UnappliedAmt"] = money(max(money(record.get("TotalAmt")) - applied, 0.0))
    elif entity == "JournalEntry" and isinstance(lines, list):
        debits = credits = 0.0
        for idx, line in enumerate(lines, start=1):
            line.setdefault("Id", str(idx))
            line.setdefault("LineNum", idx)
            detail = line.get("JournalEntryLineDetail") or {}
            if detail.get("PostingType") == "Debit":
                debits = money(debits + money(line.get("Amount")))
            elif detail.get("PostingType") == "Credit":
                credits = money(credits + money(line.get("Amount")))
        if abs(debits - credits) > 0.005:
            fail(
                ctx,
                400,
                "An error occurred while processing your request",
                "Business Validation Error: The debits and credits in this journal entry do not balance.",
                "6000",
            )
        record["TotalAmt"] = debits


def _next_doc_number(ctx: ProviderContext, entity: str) -> str:
    best = 1000
    for record in collection(ctx, entity):
        doc = str(record.get("DocNumber") or "")
        match = re.search(r"(\d+)$", doc)
        if match:
            best = max(best, int(match.group(1)))
    prefix_match = None
    for record in collection(ctx, entity):
        doc = str(record.get("DocNumber") or "")
        match = re.match(r"^(.*?)(\d+)$", doc)
        if match and int(match.group(2)) == best:
            prefix_match = match.group(1)
            break
    return f"{prefix_match or ''}{best + 1}"


ACCOUNT_CLASSIFICATION = {
    "Bank": "Asset",
    "Accounts Receivable": "Asset",
    "Other Current Asset": "Asset",
    "Fixed Asset": "Asset",
    "Other Asset": "Asset",
    "Accounts Payable": "Liability",
    "Credit Card": "Liability",
    "Other Current Liability": "Liability",
    "Long Term Liability": "Liability",
    "Equity": "Equity",
    "Income": "Revenue",
    "Other Income": "Revenue",
    "Expense": "Expense",
    "Other Expense": "Expense",
    "Cost of Goods Sold": "Expense",
}


def apply_defaults(ctx: ProviderContext, entity: str, record: dict[str, Any]) -> None:
    today = utc(ctx.clock.now()).date().isoformat()
    record.setdefault("domain", "QBO")
    record.setdefault("sparse", False)
    if entity in NAME_LIST:
        record.setdefault("Active", True)
    if entity in TRANSACTIONS or entity == "Attachable":
        record.setdefault("TxnDate", today)
    if entity == "Payment":
        record.setdefault("Line", [])
        record.setdefault(
            "DepositToAccountRef", {"value": str(ctx.store.meta["system_accounts"]["undeposited"])}
        )
    if entity == "Invoice":
        record.setdefault("EmailStatus", "NotSet")
        record.setdefault("PrintStatus", "NotSet")
        record.setdefault("AllowOnlineCreditCardPayment", False)
        record.setdefault("AllowOnlineACHPayment", False)
        record.setdefault("AllowIPNPayment", False)
        record.setdefault("GlobalTaxCalculation", "TaxExcluded")
        record.setdefault("ExchangeRate", 1)
        record.setdefault("Deposit", 0.0)
        if not record.get("DueDate"):
            days = 30
            cust = collection(ctx, "Customer").get(ref_value(record.get("CustomerRef")) or "")
            term_id = ref_value(record.get("SalesTermRef")) or (
                ref_value(cust.get("SalesTermRef")) if cust else None
            )
            term = collection(ctx, "Term").get(term_id or "")
            if term is not None:
                days = int(term.get("DueDays", days))
            txn_date = date.fromisoformat(record["TxnDate"])
            try:
                record["DueDate"] = (txn_date + timedelta(days=days)).isoformat()
            except OverflowError:
                fail(ctx, 400, "Business Validation Error", "DueDate is outside the supported range.", "6000")
        cust = collection(ctx, "Customer").get(ref_value(record.get("CustomerRef")) or "")
        if cust is not None:
            email = (cust.get("PrimaryEmailAddr") or {}).get("Address")
            if email and not record.get("BillEmail"):
                record["BillEmail"] = {"Address": email}
            if not record.get("BillAddr") and cust.get("BillAddr"):
                record["BillAddr"] = copy.deepcopy(cust["BillAddr"])
    if entity == "Estimate":
        record.setdefault("TxnStatus", "Pending")
        record.setdefault("EmailStatus", "NotSet")
    if entity == "SalesReceipt":
        record.setdefault("EmailStatus", "NotSet")
        record.setdefault("PrintStatus", "NotSet")
    if entity == "Account":
        record.setdefault(
            "Classification", ACCOUNT_CLASSIFICATION.get(str(record.get("AccountType")), "Asset")
        )
        record.setdefault("AccountSubType", "")
        record.setdefault("CurrentBalance", 0.0)
        record.setdefault("CurrentBalanceWithSubAccounts", 0.0)
        record.setdefault("_opening", 0.0)
    if entity == "Customer":
        record.setdefault("Taxable", True)
        record.setdefault("Balance", 0.0)
        record.setdefault("BalanceWithJobs", 0.0)
        record.setdefault("Job", bool(record.get("ParentRef")))
        record.setdefault("BillWithParent", False)
        record.setdefault("Level", 1 if record.get("ParentRef") else 0)
        record.setdefault("PreferredDeliveryMethod", "Email")
        record.setdefault("PrintOnCheckName", record.get("DisplayName", ""))
        record.setdefault("CompanyName", record.get("DisplayName", ""))
    if entity == "Vendor":
        record.setdefault("Balance", 0.0)
        record.setdefault("Vendor1099", False)
        record.setdefault("PrintOnCheckName", record.get("DisplayName", ""))
        record.setdefault("CompanyName", record.get("DisplayName", ""))
    if entity == "Employee":
        record.setdefault(
            "DisplayName", f"{record.get('GivenName', '')} {record.get('FamilyName', '')}".strip()
        )
        record.setdefault("PrintOnCheckName", record.get("DisplayName", ""))
    if entity == "Item":
        record.setdefault("Taxable", True)
        record.setdefault("UnitPrice", 0.0)
    if entity == "Term":
        record.setdefault("Type", "STANDARD")
        match = re.search(r"Net\s+(\d+)", str(record.get("Name", "")), re.IGNORECASE)
        if match and record.get("DueDays") is None:
            record["DueDays"] = int(match.group(1))
    if entity == "PaymentMethod":
        record.setdefault("Type", "NON_CREDIT_CARD")
    if entity == "TimeActivity":
        record.setdefault("BillableStatus", "NotBillable")
        hours = int(record.get("Hours") or 0)
        minutes = int(record.get("Minutes") or 0)
        if record.get("BreakHours") is None:
            record["BreakHours"] = 0
            record["BreakMinutes"] = 0
        _ = (hours, minutes)
    # FullyQualifiedName for hierarchical name lists
    if entity in ("Account", "Customer", "Class", "Department", "Item") and not record.get(
        "FullyQualifiedName"
    ):
        name_field = "DisplayName" if entity == "Customer" else "Name"
        own = str(record.get(name_field) or record.get("Name") or "")
        parent_id = ref_value(record.get("ParentRef"))
        parent = collection(ctx, entity).get(parent_id or "")
        if parent is not None:
            parent_name = (
                parent.get("FullyQualifiedName") or parent.get(name_field) or parent.get("Name") or ""
            )
            record["FullyQualifiedName"] = f"{parent_name}:{own}" if parent_name else own
            if entity == "Account":
                record["SubAccount"] = True
        else:
            record["FullyQualifiedName"] = own
    if entity == "Account":
        record.setdefault("SubAccount", bool(record.get("ParentRef")))


def rebuild_linked(ctx: ProviderContext) -> None:
    invoices = collection(ctx, "Invoice")
    linked_by_invoice: dict[str, list[dict[str, Any]]] = {}
    for pay in collection(ctx, "Payment"):
        if pay.get("_deleted") or pay.get("_voided"):
            continue
        for line in pay.get("Line") or []:
            for linked in line.get("LinkedTxn") or []:
                if linked.get("TxnType") == "Invoice":
                    linked_by_invoice.setdefault(str(linked.get("TxnId")), []).append(
                        {"TxnId": str(pay.get("Id")), "TxnType": "Payment"}
                    )
    for inv in invoices:
        if inv.get("_deleted"):
            continue
        inv["LinkedTxn"] = linked_by_invoice.get(str(inv.get("Id")), [])
    linked_by_bill: dict[str, list[dict[str, Any]]] = {}
    for bp in collection(ctx, "BillPayment"):
        if bp.get("_deleted") or bp.get("_voided"):
            continue
        for line in bp.get("Line") or []:
            for linked in line.get("LinkedTxn") or []:
                if linked.get("TxnType") == "Bill":
                    linked_by_bill.setdefault(str(linked.get("TxnId")), []).append(
                        {"TxnId": str(bp.get("Id")), "TxnType": "BillPayment"}
                    )
    for bill in collection(ctx, "Bill"):
        if bill.get("_deleted"):
            continue
        bill["LinkedTxn"] = linked_by_bill.get(str(bill.get("Id")), [])


def refresh_hierarchy(
    ctx: ProviderContext, entity: str, record: dict[str, Any], seen: set[str]
) -> tuple[str, int]:
    own = str(record.get("DisplayName" if entity == "Customer" else "Name") or "")
    parent = collection(ctx, entity).get(ref_value(record.get("ParentRef")))
    if parent is None or str(parent["Id"]) in seen:
        name, level = own, 0
    else:
        parent_name, parent_level = refresh_hierarchy(ctx, entity, parent, seen | {str(parent["Id"])})
        name, level = f"{parent_name}:{own}", parent_level + 1
        record["ParentRef"]["name"] = parent_name
    record["FullyQualifiedName"] = name
    if entity == "Customer":
        record["Job"], record["Level"] = parent is not None, level
    return name, level


def after_change(ctx: ProviderContext) -> None:
    for entity in ("Account", "Customer", "Class", "Department", "Item"):
        for record in collection(ctx, entity):
            refresh_hierarchy(ctx, entity, record, {str(record["Id"])})
    previous = {
        (entity, r["Id"]): (r.get("Balance"), copy.deepcopy(r.get("LinkedTxn")))
        for entity in ("Invoice", "Bill")
        for r in collection(ctx, entity)
        if not r.get("_deleted")
    }
    rebuild_linked(ctx)
    refresh_computed(ctx)
    for (entity, record_id), before in previous.items():
        record = collection(ctx, entity).get(record_id)
        assert record is not None
        if before != (record.get("Balance"), record.get("LinkedTxn")):
            touch(ctx, record)
            record.pop("_pdf_blob", None)


def create_entity(
    ctx: ProviderContext, entity: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], list[Event]]:
    if not isinstance(payload, dict):
        fail(ctx, 400, "Invalid JSON", "Request body must be a JSON object.", "2010")
    record = copy.deepcopy(payload)
    validate_shape(ctx, record)
    record.pop("Id", None)
    record.pop("MetaData", None)
    check_required(ctx, entity, record)
    validate_refs(ctx, entity, record)
    check_unique_name(ctx, entity, record)
    apply_defaults(ctx, entity, record)
    validate_business(ctx, entity, record)
    if entity in ("Invoice", "Estimate", "SalesReceipt", "CreditMemo", "Bill") and not record.get(
        "DocNumber"
    ):
        record["DocNumber"] = _next_doc_number(ctx, entity)
    if entity in ("Invoice", "CreditMemo", "Bill"):
        record.pop("Balance", None)  # QBO computes Balance on create; ignore client-supplied value
    compute_totals(ctx, entity, record)
    record["Id"] = str(ctx.ids.sequence(entity))
    record["SyncToken"] = "0"
    stamp_new(ctx, record)
    ctx.store.collection(entity).put_as(record["Id"], record)
    after_change(ctx)
    return record, [(entity, record["Id"], "Create")]


def update_entity(
    ctx: ProviderContext,
    entity: str,
    payload: dict[str, Any],
    *,
    operation: str | None = None,
    include: str | None = None,
) -> tuple[dict[str, Any], list[Event]]:
    validate_shape(ctx, payload)
    if operation == "delete":
        return delete_entity(ctx, entity, payload)
    if operation == "void" or (operation == "update" and include == "void"):
        return void_entity(ctx, entity, payload)
    record_id = payload.get("Id")
    if record_id is None:
        if entity in ("CompanyInfo", "Preferences"):
            existing = next(iter(collection(ctx, entity)), None)
            if existing is None:
                not_found(ctx, entity)
            assert existing is not None
            record_id = existing.get("Id")
        else:
            return create_entity(ctx, entity, payload)
    existing = get_record(ctx, entity, str(record_id))
    supplied_token = payload.get("SyncToken")
    if supplied_token is None:
        fail(
            ctx,
            400,
            "Stale Object Error",
            "Stale Object Error : You and another user were working in the same object at the same time. "
            "SyncToken is required for update.",
            "5010",
        )
    if str(supplied_token) != str(existing.get("SyncToken", "0")):
        fail(
            ctx,
            400,
            "Stale Object Error",
            "Stale Object Error : You and another user were working in the same object at the same time. "
            f"Your SyncToken {supplied_token} does not match the current SyncToken {existing.get('SyncToken')}.",
            "5010",
        )
    payload = copy.deepcopy(payload)
    validate_refs(ctx, entity, payload)
    sparse = bool(payload.get("sparse"))
    if sparse:
        record = copy.deepcopy(existing)
        for key, value in payload.items():
            if key in ("Id", "SyncToken", "MetaData"):
                continue
            record[key] = copy.deepcopy(value)
    else:
        preserved = {k: v for k, v in existing.items() if k.startswith("_")}
        created = (existing.get("MetaData") or {}).get("CreateTime")
        record = copy.deepcopy(payload)
        record.update(preserved)
        record["Id"] = str(record_id)
        record["MetaData"] = copy.deepcopy(existing.get("MetaData", {}))
        if created:
            record["MetaData"]["CreateTime"] = created
        apply_defaults(ctx, entity, record)
    check_required(ctx, entity, record)
    check_unique_name(ctx, entity, record, exclude_id=str(record_id))
    validate_business(ctx, entity, record)
    if "Line" in payload and "TxnTaxDetail" not in payload:
        record.pop("TxnTaxDetail", None)
    if any(k in payload for k in ("Name", "DisplayName", "ParentRef")):
        record.pop("FullyQualifiedName", None)
    record.pop("_pdf_blob", None)
    compute_totals(ctx, entity, record)
    apply_defaults(ctx, entity, record)
    touch(ctx, record)
    record["sparse"] = False
    collection(ctx, entity).put_as(str(record_id), record)
    after_change(ctx)
    return record, [(entity, str(record_id), "Update")]


def delete_entity(
    ctx: ProviderContext, entity: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], list[Event]]:
    validate_shape(ctx, payload)
    if entity in NAME_LIST or entity in ("CompanyInfo", "Preferences"):
        fail(
            ctx,
            400,
            "Operation not supported",
            "Name lists must be deactivated with Active=false; singletons cannot be deleted.",
            "500",
        )
    record_id = payload.get("Id")
    if record_id is None:
        fail(ctx, 400, "Required param missing", "Id is required for delete.", "2020", element="Id")
    existing = get_record(ctx, entity, str(record_id))
    supplied = payload.get("SyncToken")
    if supplied is None or str(supplied) != str(existing.get("SyncToken", "0")):
        fail(ctx, 400, "Stale Object Error", "Stale Object Error : SyncToken does not match.", "5010")
    if entity == "Invoice":
        unlink_invoice(ctx, str(record_id))
    existing["_deleted"] = True
    existing["status"] = "Deleted"
    touch(ctx, existing)
    after_change(ctx)
    return existing, [(entity, str(record_id), "Delete")]


def void_entity(
    ctx: ProviderContext, entity: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], list[Event]]:
    validate_shape(ctx, payload)
    if entity not in VOIDABLE:
        fail(ctx, 400, "Operation not supported", f"Void is not supported for {entity}.", "6000")
    record_id = payload.get("Id")
    if record_id is None:
        fail(ctx, 400, "Required param missing", "Id is required for void.", "2020", element="Id")
    existing = get_record(ctx, entity, str(record_id))
    supplied = payload.get("SyncToken")
    if supplied is None or str(supplied) != str(existing.get("SyncToken", "0")):
        fail(ctx, 400, "Stale Object Error", "Stale Object Error : SyncToken does not match.", "5010")
    existing["_voided"] = True
    if entity == "Invoice":
        unlink_invoice(ctx, str(record_id))
    for key in ("TotalAmt", "Balance", "UnappliedAmt"):
        if key in existing:
            existing[key] = 0.0
    for line in existing.get("Line") or []:
        if "Amount" in line:
            line["Amount"] = 0.0
        detail = line.get("SalesItemLineDetail") or {}
        if "Qty" in detail:
            detail["Qty"] = 0.0
        if "UnitPrice" in detail:
            detail["UnitPrice"] = 0.0
    if isinstance(existing.get("TxnTaxDetail"), dict):
        existing["TxnTaxDetail"]["TotalTax"] = 0.0
        for line in existing["TxnTaxDetail"].get("TaxLine") or []:
            line["Amount"] = 0.0
            if "TaxLineDetail" in line:
                line["TaxLineDetail"]["NetAmountTaxable"] = 0.0
    note = str(existing.get("PrivateNote") or "")
    existing["PrivateNote"] = ("Voided " + note).strip()
    existing.pop("_pdf_blob", None)
    touch(ctx, existing)
    after_change(ctx)
    return existing, [(entity, str(record_id), "Void")]


def unlink_invoice(ctx: ProviderContext, record_id: str) -> None:
    """A removed invoice releases its allocations as unapplied customer credit."""
    for payment in collection(ctx, "Payment"):
        if payment.get("_deleted") or payment.get("_voided"):
            continue
        lines = payment.get("Line") or []
        kept = [
            line
            for line in lines
            if not any(
                link.get("TxnType") == "Invoice" and link.get("TxnId") == record_id
                for link in line.get("LinkedTxn") or []
            )
        ]
        if len(kept) != len(lines):
            payment["Line"] = kept
            compute_totals(ctx, "Payment", payment)
            touch(ctx, payment)


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    return public(record)  # type: ignore[return-value]


def deleted_stub(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "Id": record.get("Id"),
        "status": "Deleted",
        "MetaData": record.get("MetaData", {"LastUpdatedTime": qbo_time_for(record)}),
    }


def qbo_time_for(record: dict[str, Any]) -> str:
    meta = record.get("MetaData") or {}
    return str(meta.get("LastUpdatedTime") or "2026-01-01T00:00:00.000-07:00")


_ = qbo_time
