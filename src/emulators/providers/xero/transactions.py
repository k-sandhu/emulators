"""Validated resource mutations and invoice settlement invariants."""

from __future__ import annotations

import copy
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext

from .common import ID_FIELDS, find, history, instant, money, msdate, put, rows, stamp, validation


def number(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (ValueError, TypeError, InvalidOperation):
        validation(f"{field} must be a valid number.")


def account_ref(ctx: ProviderContext, tenant: str, value: Any, *, payment: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or not (value.get("AccountID") or value.get("Code")):
        validation("An AccountID or Code is required.")
    account = find(ctx, "Accounts", tenant, str(value.get("AccountID") or value["Code"]))
    if account["Status"] != "ACTIVE" or (payment and not account.get("EnablePaymentsToAccount")):
        validation("The account is not valid for payments.")
    return {k: account[k] for k in ["AccountID", "Code", "Name"]}


def contact_ref(ctx: ProviderContext, tenant: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        validation("A contact is required.")
    identifier = value.get("ContactID")
    if identifier:
        try:
            contact = find(ctx, "Contacts", tenant, str(identifier))
        except EmulatorError:
            validation("The contact is not valid.")
    else:
        name = str(value.get("Name", "")).strip()
        if not name:
            validation("A ContactID or contact Name is required.")
        contact = next((c for c in rows(ctx, "Contacts", tenant) if c["Name"].casefold() == name.casefold()), None)
        if contact is None:
            contact = save_simple(ctx, tenant, "Contacts", value, None)
    if contact.get("ContactStatus") != "ACTIVE":
        validation("The contact has been archived.")
    return {k: contact[k] for k in ["ContactID", "Name"]}


def dates(ctx: ProviderContext, record: dict[str, Any], fields: list[str]) -> None:
    for field in fields:
        if field not in record:
            if field == "Date":
                record[field] = msdate(ctx.clock.now().date())
            else:
                continue
        try:
            value = instant(record[field])
        except (ValueError, TypeError, OverflowError):
            validation(f"{field} is invalid.")
        record[field] = msdate(value.date())
        record[field + "String"] = value.date().isoformat() + "T00:00:00"


def line_items(ctx: ProviderContext, tenant: str, record: dict[str, Any], *, purchase: bool = False) -> None:
    lines = record.get("LineItems", [])
    if not isinstance(lines, list) or not lines:
        validation("At least one line item must be specified.", record)
    mode = record.setdefault("LineAmountTypes", "Exclusive")
    if mode not in {"Exclusive", "Inclusive", "NoTax"}:
        validation("LineAmountTypes must be Exclusive, Inclusive or NoTax.")
    result = []
    for value in lines:
        if not isinstance(value, dict):
            validation("Invalid LineItems.")
        line = copy.deepcopy(value)
        item = None
        if line.get("ItemCode"):
            try:
                item = find(ctx, "Items", tenant, line["ItemCode"])
            except EmulatorError:
                validation("The ItemCode is not valid.")
        defaults = (item or {}).get("PurchaseDetails" if purchase else "SalesDetails", {})
        line.setdefault("Description", (item or {}).get("Description", ""))
        line.setdefault("Quantity", 1)
        line.setdefault("UnitAmount", defaults.get("UnitPrice", 0))
        quantity, unit = number(line["Quantity"], "Quantity"), number(line["UnitAmount"], "UnitAmount")
        line["Quantity"], line["UnitAmount"] = float(quantity), float(unit)
        discount = number(line.get("DiscountRate", 0), "DiscountRate")
        if not 0 <= discount <= 100:
            validation("DiscountRate must be between 0 and 100.")
        gross = money(quantity * unit * (1 - discount / 100))
        line.setdefault("AccountCode", defaults.get("AccountCode", ""))
        try:
            account = find(ctx, "Accounts", tenant, str(line["AccountCode"]))
        except EmulatorError:
            validation("The AccountCode is not valid.")
        if account["Status"] != "ACTIVE" or account.get("SystemAccount"):
            validation("The account cannot be used on a line item.")
        line["AccountCode"] = account["Code"]
        line.setdefault("TaxType", defaults.get("TaxType", account.get("TaxType", "NONE")))
        taxes = [t for t in rows(ctx, "TaxRates", tenant) if t["TaxType"] == line["TaxType"] and t["Status"] == "ACTIVE"]
        if not taxes:
            validation("The TaxType is not valid.")
        rate = Decimal(str(taxes[0]["EffectiveRate"])) / 100 if mode != "NoTax" else Decimal(0)
        tax = money(Decimal(str(gross)) * rate / (1 + rate)) if mode == "Inclusive" else money(Decimal(str(gross)) * rate)
        if "TaxAmount" in line and mode != "NoTax":
            tax = money(line["TaxAmount"])
        if mode == "NoTax":
            tax = 0.0
        line["TaxAmount"] = tax
        line["LineAmount"] = money(gross - tax) if mode == "Inclusive" else gross
        line.setdefault("LineItemID", ctx.ids.guid())
        tracking = line.setdefault("Tracking", [])
        if not isinstance(tracking, list) or len(tracking) > 2:
            validation("A line may have at most two tracking categories.")
        categories = set()
        for track in tracking:
            category = next((c for c in rows(ctx, "TrackingCategories", tenant)
                             if track.get("TrackingCategoryID") == c["TrackingCategoryID"] or track.get("Name") == c["Name"]), None)
            option = next((o for o in (category or {}).get("Options", [])
                           if track.get("TrackingOptionID") == o["TrackingOptionID"] or track.get("Option") == o["Name"]), None)
            if not category or not option or option["Status"] != "ACTIVE" or category["Status"] != "ACTIVE":
                validation("The tracking category or option is not valid.")
            if category["TrackingCategoryID"] in categories:
                validation("A tracking category cannot be repeated on a line.")
            categories.add(category["TrackingCategoryID"])
            track.update(TrackingCategoryID=category["TrackingCategoryID"], Name=category["Name"],
                         TrackingOptionID=option["TrackingOptionID"], Option=option["Name"])
        result.append(line)
    record["LineItems"] = result
    record["SubTotal"] = money(sum(Decimal(str(l["LineAmount"])) for l in result))
    record["TotalTax"] = money(sum(Decimal(str(l["TaxAmount"])) for l in result))
    record["Total"] = money(Decimal(str(record["SubTotal"])) + Decimal(str(record["TotalTax"])))


def settle(ctx: ProviderContext, tenant: str, invoice: dict[str, Any]) -> None:
    payments = [p for p in rows(ctx, "Payments", tenant)
                if p.get("Invoice", {}).get("InvoiceID") == invoice["InvoiceID"] and p["Status"] == "AUTHORISED"]
    invoice["Payments"] = [{k: p[k] for k in ["PaymentID", "Date", "Amount", "Reference", "CurrencyRate"]}
                           for p in payments]
    invoice["AmountPaid"] = money(sum(p["Amount"] for p in payments))
    credited = sum(a["AppliedAmount"] for c in rows(ctx, "CreditNotes", tenant) if c["Status"] not in {"VOIDED", "DELETED"}
                   for a in c.get("Allocations", []) if a["Invoice"]["InvoiceID"] == invoice["InvoiceID"])
    invoice["AmountCredited"] = money(credited)
    invoice["AmountDue"] = 0.0 if invoice["Status"] in {"VOIDED", "DELETED"} else money(invoice["Total"] - invoice["AmountPaid"] - credited)
    if invoice["Status"] in {"AUTHORISED", "PAID"}:
        invoice["Status"] = "PAID" if invoice["AmountDue"] == 0 else "AUTHORISED"
    if invoice["Status"] == "PAID" and payments:
        invoice["FullyPaidOnDate"] = max(payments, key=lambda p: instant(p["Date"]))["Date"]
    else:
        invoice.pop("FullyPaidOnDate", None)
    stamp(ctx, invoice)


def save_invoice(ctx: ProviderContext, tenant: str, value: dict[str, Any], old: dict[str, Any] | None,
                 resource: str = "Invoices") -> dict[str, Any]:
    record = {**copy.deepcopy(old or {}), **copy.deepcopy(value)}
    field = ID_FIELDS[resource]
    record.setdefault(field, ctx.ids.guid())
    default_type = "ACCRECCREDIT" if resource == "CreditNotes" else "ACCREC"
    record.setdefault("Type", default_type)
    types = {"ACCRECCREDIT", "ACCPAYCREDIT"} if resource == "CreditNotes" else {"ACCREC", "ACCPAY"}
    if resource != "Quotes" and record["Type"] not in types:
        validation("The Type is not valid.", record)
    if resource == "Quotes":
        record.pop("Type", None)
    record.setdefault("Status", "DRAFT")
    transitions = {"DRAFT": {"DRAFT", "SUBMITTED", "AUTHORISED", "DELETED"},
        "SUBMITTED": {"SUBMITTED", "DRAFT", "AUTHORISED", "DELETED"},
        "AUTHORISED": {"AUTHORISED", "VOIDED"}, "PAID": {"PAID"}, "VOIDED": {"VOIDED"}, "DELETED": {"DELETED"}}
    if resource == "Quotes":
        transitions = {"DRAFT": {"DRAFT", "SENT", "DELETED"}, "SENT": {"SENT", "ACCEPTED", "DECLINED", "DELETED"},
                       "ACCEPTED": {"ACCEPTED", "INVOICED"}, "DECLINED": {"DECLINED"}, "INVOICED": {"INVOICED"}, "DELETED": {"DELETED"}}
    before = (old or {}).get("Status", "DRAFT")
    if record["Status"] not in transitions.get(before, set()):
        validation("The status transition is not valid.", record)
    if old and (old.get("AmountPaid", 0) or old.get("AmountCredited", 0)) and any(k in value for k in ["LineItems", "Contact", "Type", "Date", "CurrencyCode"]):
        validation("Paid or credited invoices cannot have their financial details changed.", record)
    if record["Status"] == "VOIDED" and (record.get("AmountPaid", 0) or record.get("AmountCredited", 0)):
        validation("Remove payments and credit allocations before voiding.", record)
    record["Contact"] = contact_ref(ctx, tenant, record.get("Contact"))
    record.setdefault("CurrencyCode", "USD")
    if record["CurrencyCode"] != "USD":
        validation("This emulator supports transaction posting in USD only.")
    record["CurrencyRate"] = 1.0
    dates(ctx, record, ["Date", "ExpiryDate"] if resource == "Quotes" else ["Date", "DueDate"])
    if "DueDate" not in record and resource == "Invoices":
        record["DueDate"] = msdate(instant(record["Date"]) + timedelta(days=30))
        record["DueDateString"] = instant(record["DueDate"]).strftime("%Y-%m-%dT00:00:00")
    if not old or "LineItems" in value or "LineAmountTypes" in value:
        line_items(ctx, tenant, record, purchase=record.get("Type", "").startswith("ACCPAY"))
    if record["Total"] < 0:
        validation("The total cannot be negative.", record)
    number_field = {"Invoices": "InvoiceNumber", "CreditNotes": "CreditNoteNumber", "Quotes": "QuoteNumber"}[resource]
    record.setdefault(number_field, {"Invoices": "INV-", "CreditNotes": "CN-", "Quotes": "QU-"}[resource] + str(ctx.ids.sequence(resource, 2000)))
    if any(r[number_field] == record[number_field] and r[field] != record[field] and r.get("Type") == record.get("Type") for r in rows(ctx, resource, tenant)):
        validation(f"{number_field} must be unique.", record)
    record.setdefault("Reference", "")
    record.setdefault("HasAttachments", False)
    if resource == "Invoices":
        record.setdefault("SentToContact", False)
        record.setdefault("AmountCredited", 0.0)
        settle(ctx, tenant, record)
    elif resource == "CreditNotes":
        record.setdefault("Allocations", [])
        record["RemainingCredit"] = money(record["Total"] - sum(a["AppliedAmount"] for a in record["Allocations"]))
    stamp(ctx, record)
    put(ctx, resource, tenant, record)
    if resource == "Invoices":
        contact = find(ctx, "Contacts", tenant, record["Contact"]["ContactID"])
        contact["IsCustomer" if record["Type"] == "ACCREC" else "IsSupplier"] = True
    return record


def save_payment(ctx: ProviderContext, tenant: str, value: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    if old:
        if value.get("Status") != "DELETED":
            validation("Payments can only be updated to DELETED.")
        record = copy.deepcopy(old)
        record["Status"] = "DELETED"
        stamp(ctx, record)
        put(ctx, "Payments", tenant, record)
        settle(ctx, tenant, find(ctx, "Invoices", tenant, record["Invoice"]["InvoiceID"]))
        return record
    if not isinstance(value.get("Invoice"), dict) or not value["Invoice"].get("InvoiceID"):
        validation("An InvoiceID is required.")
    invoice = find(ctx, "Invoices", tenant, value["Invoice"]["InvoiceID"])
    if invoice["Status"] != "AUTHORISED":
        validation("Payments can only be applied to authorised invoices.", value)
    amount = money(value.get("Amount", 0))
    if amount <= 0 or amount > invoice["AmountDue"]:
        validation("The payment amount must be positive and cannot exceed the amount outstanding.", value)
    account = account_ref(ctx, tenant, value.get("Account"), payment=True)
    if value.get("Status", "AUTHORISED") != "AUTHORISED":
        validation("The payment Status is not valid.")
    record = {"PaymentID": value.get("PaymentID", ctx.ids.guid()), "Amount": amount,
              "Account": account, "Invoice": {k: invoice[k] for k in ["InvoiceID", "InvoiceNumber", "Type", "Contact"]},
              "Reference": value.get("Reference", ""), "CurrencyRate": 1.0, "IsReconciled": bool(value.get("IsReconciled", False)),
              "Status": "AUTHORISED", "PaymentType": "ACCRECPAYMENT" if invoice["Type"] == "ACCREC" else "ACCPAYPAYMENT"}
    if "Date" not in value:
        validation("A Date is required.", value)
    record["Date"] = value["Date"]
    dates(ctx, record, ["Date"])
    if instant(record["Date"]) < instant(invoice["Date"]):
        validation("Payment date cannot precede invoice date.")
    stamp(ctx, record)
    put(ctx, "Payments", tenant, record)
    settle(ctx, tenant, invoice)
    return record


def save_simple(ctx: ProviderContext, tenant: str, resource: str, value: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    record = {**copy.deepcopy(old or {}), **copy.deepcopy(value)}
    field = ID_FIELDS[resource]
    record.setdefault(field, ctx.ids.guid())
    if resource in {"Contacts", "ContactGroups", "Accounts", "TrackingCategories"}:
        name = record.get("Name")
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            validation("Name is required and must be less than 256 characters.", record)
        if name != name.strip() or "  " in name or "<" in name or ">" in name:
            validation("Name contains invalid whitespace or characters.", record)
        if any(r["Name"].casefold() == name.casefold() and r[field] != record[field] for r in rows(ctx, resource, tenant)):
            validation("Name must be unique.", record)
    if resource == "Contacts":
        record.setdefault("ContactStatus", "ACTIVE")
        if record["ContactStatus"] not in {"ACTIVE", "ARCHIVED"}:
            validation("ContactStatus is invalid.")
        if old is None:
            record["IsCustomer"] = record["IsSupplier"] = False
        elif "IsCustomer" in value or "IsSupplier" in value:
            record["IsCustomer"], record["IsSupplier"] = old["IsCustomer"], old["IsSupplier"]
        for key in ["Addresses", "Phones", "ContactPersons", "ContactGroups"]:
            record.setdefault(key, [])
        record.setdefault("DefaultCurrency", "USD")
        record.pop("Balances", None)
    elif resource == "Accounts":
        kinds = {"BANK", "CURRENT", "CURRLIAB", "DEPRECIATN", "DIRECTCOSTS", "EQUITY", "EXPENSE", "FIXED", "INVENTORY", "LIABILITY", "NONCURRENT", "OTHERINCOME", "OVERHEADS", "PREPAYMENT", "REVENUE", "SALES", "TERMLIAB"}
        if not record.get("Code") or record.get("Type") not in kinds:
            validation("A Code and valid Type are required.", record)
        if any(r["Code"] == record["Code"] and r[field] != record[field] for r in rows(ctx, resource, tenant)):
            validation("Account Code must be unique.")
        record["Class"] = {"BANK": "ASSET", "CURRENT": "ASSET", "FIXED": "ASSET", "INVENTORY": "ASSET", "NONCURRENT": "ASSET", "PREPAYMENT": "ASSET", "CURRLIAB": "LIABILITY", "TERMLIAB": "LIABILITY", "LIABILITY": "LIABILITY", "EQUITY": "EQUITY", "REVENUE": "REVENUE", "SALES": "REVENUE", "OTHERINCOME": "REVENUE"}.get(record["Type"], "EXPENSE")
        record.setdefault("TaxType", "NONE")
        record.setdefault("EnablePaymentsToAccount", record["Type"] == "BANK")
        record.setdefault("Status", "ACTIVE")
        if record["Status"] not in {"ACTIVE", "ARCHIVED"}:
            validation("Invalid account Status.")
        if old and old.get("SystemAccount") and any(k in value for k in ["Status", "Code", "Type"]):
            validation("System accounts cannot be changed.")
    elif resource == "Items":
        if not record.get("Code"):
            validation("Code is required.")
        if record.get("IsTrackedAsInventory"):
            validation("Tracked inventory is not supported by this emulator.")
        if any(r["Code"] == record["Code"] and r[field] != record[field] for r in rows(ctx, resource, tenant)):
            validation("Item Code must be unique.")
        record.setdefault("IsSold", True)
        record.setdefault("IsPurchased", False)
        record.setdefault("IsTrackedAsInventory", False)
        for detail in ["SalesDetails", "PurchaseDetails"]:
            if detail in record and record[detail].get("AccountCode"):
                account_ref(ctx, tenant, {"Code": record[detail]["AccountCode"]})
    elif resource in {"ContactGroups", "TrackingCategories"}:
        record.setdefault("Status", "ACTIVE")
        if record["Status"] not in {"ACTIVE", "ARCHIVED", "DELETED"}:
            validation("Invalid Status.")
        if resource == "TrackingCategories":
            if not old and len([r for r in rows(ctx, resource, tenant) if r["Status"] == "ACTIVE"]) >= 2:
                validation("Only two active tracking categories are allowed.")
            record.setdefault("Options", [])
        else:
            record.setdefault("Contacts", [])
    stamp(ctx, record)
    return put(ctx, resource, tenant, record)


def save_bank(ctx: ProviderContext, tenant: str, resource: str, value: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    record = {**copy.deepcopy(old or {}), **copy.deepcopy(value)}
    record.setdefault(ID_FIELDS[resource], ctx.ids.guid())
    if resource == "BankTransfers":
        if old:
            validation("Bank transfers cannot be updated.")
        for name in ["FromBankAccount", "ToBankAccount"]:
            record[name] = account_ref(ctx, tenant, record.get(name), payment=True)
            if find(ctx, "Accounts", tenant, record[name]["AccountID"])["Type"] != "BANK":
                validation("Bank transfers require bank accounts.")
        if record["FromBankAccount"]["AccountID"] == record["ToBankAccount"]["AccountID"]:
            validation("Bank transfer accounts must differ.")
        record["Amount"] = money(record.get("Amount", 0))
        if record["Amount"] <= 0:
            validation("Amount must be greater than zero.")
        record.setdefault("CurrencyRate", 1.0)
    elif resource == "ManualJournals":
        record.setdefault("Status", "DRAFT")
        if record["Status"] not in {"DRAFT", "POSTED", "VOIDED", "DELETED"}:
            validation("Invalid manual journal Status.")
        if old and old["Status"] == "POSTED" and record["Status"] in {"DRAFT", "DELETED"}:
            validation("Posted journals must be voided.")
        if not record.get("Narration") or not record.get("JournalLines"):
            validation("Narration and JournalLines are required.")
        if record.get("LineAmountTypes", "NoTax") != "NoTax":
            validation("This emulator supports NoTax manual journals only.")
        record["LineAmountTypes"] = "NoTax"
        for line in record["JournalLines"]:
            account = account_ref(ctx, tenant, {"Code": line.get("AccountCode")})
            account_record = find(ctx, "Accounts", tenant, account["AccountID"])
            if account_record["Type"] == "BANK" or account_record.get("SystemAccount"):
                validation("Bank and system accounts cannot be used in manual journals.")
            line["LineAmount"] = money(line.get("LineAmount", 0))
            line.setdefault("TaxType", "NONE")
        if money(sum(l["LineAmount"] for l in record["JournalLines"])) != 0:
            validation("Debits and credits must balance.")
        record.setdefault("ShowOnCashBasisReports", True)
    else:
        record.setdefault("Type", "SPEND")
        if record["Type"] not in {"SPEND", "RECEIVE"}:
            validation("Type must be SPEND or RECEIVE.")
        record.setdefault("Status", "AUTHORISED")
        if record["Status"] not in {"AUTHORISED", "DELETED"}:
            validation("Invalid bank transaction Status.")
        record["Contact"] = contact_ref(ctx, tenant, record.get("Contact"))
        record["BankAccount"] = account_ref(ctx, tenant, record.get("BankAccount"), payment=True)
        line_items(ctx, tenant, record, purchase=record["Type"] == "SPEND")
        record.setdefault("CurrencyCode", "USD")
        if record["CurrencyCode"] != "USD":
            validation("This emulator supports transaction posting in USD only.")
        record.setdefault("IsReconciled", False)
        record.setdefault("HasAttachments", False)
    dates(ctx, record, ["Date"])
    stamp(ctx, record)
    return put(ctx, resource, tenant, record)


def mutate(ctx: ProviderContext, tenant: str, resource: str, value: dict[str, Any], identifier: str | None = None) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, dict):
        validation("Each element must be an object.")
    value = {k: v for k, v in value.items() if not k.startswith("_")}
    field = ID_FIELDS[resource]
    if identifier:
        target = find(ctx, resource, tenant, identifier)
        if field in value and value[field] != target[field]:
            validation("The resource ID does not match the URL.")
        value[field] = target[field]
    old = next((r for r in rows(ctx, resource, tenant) if r[field] == value.get(field)), None)
    if not old and value.get(field):
        # Client-provided GUIDs are permitted, but can never select a different tenant's record.
        from uuid import UUID

        try:
            UUID(value[field])
        except (ValueError, TypeError):
            validation(f"{field} must be a GUID.")
    if resource in {"Invoices", "CreditNotes", "Quotes"}:
        record = save_invoice(ctx, tenant, value, old, resource)
    elif resource == "Payments":
        record = save_payment(ctx, tenant, value, old)
    elif resource in {"BankTransactions", "BankTransfers", "ManualJournals"}:
        record = save_bank(ctx, tenant, resource, value, old)
    else:
        record = save_simple(ctx, tenant, resource, value, old)
    history(ctx, tenant, resource, record[field], "Resource updated" if old else "Resource created", changes="Updated" if old else "Created")
    return record, old is None
