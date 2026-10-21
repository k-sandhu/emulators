"""Validated resource mutations and invoice settlement invariants."""

from __future__ import annotations

import copy
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext

from .common import ID_FIELDS, find, history, instant, money, msdate, put, rows, stamp, validation


def number(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
        if isinstance(value, bool) or not result.is_finite() or abs(result) > Decimal("999999999999.99"):
            raise ValueError
        return result
    except (ValueError, TypeError, InvalidOperation):
        validation(f"{field} must be a valid number.")


def account_ref(ctx: ProviderContext, tenant: str, value: Any, *, payment: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or not (value.get("AccountID") or value.get("Code")):
        validation("An AccountID or Code is required.")
    try:
        account = find(ctx, "Accounts", tenant, str(value.get("AccountID") or value["Code"]))
    except EmulatorError:
        validation("The account is not valid.")
    if account["Status"] != "ACTIVE" or (
        payment and account["Type"] != "BANK" and not account.get("EnablePaymentsToAccount")
    ):
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
        contact = next(
            (c for c in rows(ctx, "Contacts", tenant) if c["Name"].casefold() == name.casefold()), None
        )
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


def line_items(
    ctx: ProviderContext, tenant: str, record: dict[str, Any], *, purchase: bool = False, unitdp: int = 2
) -> None:
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
        if set(line).issubset({"Description", "LineItemID", "Tracking"}) and line.get("Description"):
            line.setdefault("LineItemID", ctx.ids.guid())
            result.append(line)
            continue
        item = None
        if line.get("ItemCode"):
            try:
                item = find(ctx, "Items", tenant, line["ItemCode"])
            except EmulatorError:
                validation("The ItemCode is not valid.")
        defaults = (item or {}).get("PurchaseDetails" if purchase else "SalesDetails", {})
        line.setdefault("Description", (item or {}).get("Description", ""))
        if "LineAmount" in line and ("Quantity" not in line or "UnitAmount" not in line):
            amount = number(line["LineAmount"], "LineAmount")
            if "UnitAmount" not in line:
                quantity = number(line.get("Quantity", 1), "Quantity")
                if not quantity:
                    validation("Quantity must not be zero when deriving UnitAmount.")
                line["UnitAmount"] = str(amount / quantity)
            else:
                unit = number(line["UnitAmount"], "UnitAmount")
                if not unit:
                    validation("UnitAmount must not be zero when deriving Quantity.")
                line["Quantity"] = str(amount / unit)
        line.setdefault("Quantity", 1)
        line.setdefault("UnitAmount", defaults.get("UnitPrice", 0))
        quantity, unit = number(line["Quantity"], "Quantity"), number(line["UnitAmount"], "UnitAmount")
        unit = unit.quantize(Decimal(1).scaleb(-unitdp), rounding=ROUND_HALF_UP)
        line["Quantity"], line["UnitAmount"] = float(quantity), float(unit)
        discount = number(line.get("DiscountRate", 0), "DiscountRate")
        if not 0 <= discount <= 100:
            validation("DiscountRate must be between 0 and 100.")
        if (purchase or record.get("Type", "").endswith("CREDIT")) and any(
            k in line for k in ["DiscountRate", "DiscountAmount"]
        ):
            validation("Discounts are supported only on sales invoices and quotes.")
        if "DiscountAmount" in line:
            discount_amount = number(line["DiscountAmount"], "DiscountAmount")
            if discount_amount < 0 or discount_amount > quantity * unit:
                validation("DiscountAmount must not exceed the line value.")
            gross = money(quantity * unit - discount_amount)
        else:
            gross = money(quantity * unit * (1 - discount / 100))
        if abs(gross) > 9999999999.99:
            validation("LineAmount cannot exceed 9,999,999,999.99.")
        line.setdefault("AccountCode", defaults.get("AccountCode", ""))
        try:
            account = find(ctx, "Accounts", tenant, line.get("AccountID") or str(line["AccountCode"]))
        except EmulatorError:
            validation("The AccountCode is not valid.")
        if account["Status"] != "ACTIVE" or account.get("SystemAccount") or account["Type"] == "BANK":
            validation("The account cannot be used on a line item.")
        line["AccountCode"] = account["Code"]
        line["AccountID"] = account["AccountID"]
        line.setdefault("TaxType", defaults.get("TaxType", account.get("TaxType", "NONE")))
        taxes = [
            t
            for t in rows(ctx, "TaxRates", tenant)
            if t["TaxType"] == line["TaxType"] and t["Status"] == "ACTIVE"
        ]
        if not taxes:
            validation("The TaxType is not valid.")
        rate = Decimal(str(taxes[0]["EffectiveRate"])) / 100 if mode != "NoTax" else Decimal(0)
        tax = (
            money(Decimal(str(gross)) * rate / (1 + rate))
            if mode == "Inclusive"
            else money(Decimal(str(gross)) * rate)
        )
        if "TaxAmount" in line and mode != "NoTax":
            tax = money(line["TaxAmount"])
        if mode == "NoTax":
            tax = 0.0
        line["TaxAmount"] = tax
        line["LineAmount"] = gross
        line.setdefault("LineItemID", ctx.ids.guid())
        tracking = line.setdefault("Tracking", [])
        if not isinstance(tracking, list) or len(tracking) > 2:
            validation("A line may have at most two tracking categories.")
        categories = set()
        for track in tracking:
            if not isinstance(track, dict):
                validation("Tracking must contain category and option objects.")
            category = next(
                (
                    c
                    for c in rows(ctx, "TrackingCategories", tenant)
                    if track.get("TrackingCategoryID") == c["TrackingCategoryID"]
                    or track.get("Name") == c["Name"]
                ),
                None,
            )
            option = next(
                (
                    o
                    for o in (category or {}).get("Options", [])
                    if track.get("TrackingOptionID") == o["TrackingOptionID"]
                    or track.get("Option") == o["Name"]
                ),
                None,
            )
            if not category or not option or option["Status"] != "ACTIVE" or category["Status"] != "ACTIVE":
                validation("The tracking category or option is not valid.")
            if category["TrackingCategoryID"] in categories:
                validation("A tracking category cannot be repeated on a line.")
            categories.add(category["TrackingCategoryID"])
            track.update(
                TrackingCategoryID=category["TrackingCategoryID"],
                Name=category["Name"],
                TrackingOptionID=option["TrackingOptionID"],
                Option=option["Name"],
            )
        result.append(line)
    record["LineItems"] = result
    record["SubTotal"] = money(
        sum(
            Decimal(str(line.get("LineAmount", 0)))
            - (Decimal(str(line.get("TaxAmount", 0))) if mode == "Inclusive" else Decimal(0))
            for line in result
        )
    )
    record["TotalTax"] = money(sum(Decimal(str(line.get("TaxAmount", 0))) for line in result))
    record["Total"] = money(Decimal(str(record["SubTotal"])) + Decimal(str(record["TotalTax"])))


def settle(ctx: ProviderContext, tenant: str, invoice: dict[str, Any]) -> None:
    invoice.pop("_pdf", None)
    payments = [
        p
        for p in rows(ctx, "Payments", tenant)
        if p.get("Invoice", {}).get("InvoiceID") == invoice["InvoiceID"] and p["Status"] == "AUTHORISED"
    ]
    invoice["Payments"] = [
        {k: p[k] for k in ["PaymentID", "Date", "Amount", "Reference", "CurrencyRate"]} for p in payments
    ]
    invoice["AmountPaid"] = money(sum(p["Amount"] for p in payments))
    credited = sum(
        a["Amount"]
        for c in rows(ctx, "CreditNotes", tenant)
        if c["Status"] not in {"VOIDED", "DELETED"}
        for a in c.get("Allocations", [])
        if a["Invoice"]["InvoiceID"] == invoice["InvoiceID"]
    )
    invoice["AmountCredited"] = money(credited)
    invoice["AmountDue"] = (
        0.0
        if invoice["Status"] in {"VOIDED", "DELETED"}
        else money(invoice["Total"] - invoice["AmountPaid"] - credited)
    )
    if invoice["Status"] in {"AUTHORISED", "PAID"}:
        invoice["Status"] = "PAID" if invoice["AmountDue"] == 0 else "AUTHORISED"
    if invoice["Status"] == "PAID":
        settled_dates = [p["Date"] for p in payments]
        settled_dates.extend(
            a["Date"]
            for credit in rows(ctx, "CreditNotes", tenant)
            if credit["Status"] in {"AUTHORISED", "PAID"}
            for a in credit.get("Allocations", [])
            if a["Invoice"]["InvoiceID"] == invoice["InvoiceID"]
        )
        invoice["FullyPaidOnDate"] = max(settled_dates or [invoice["Date"]], key=instant)
    else:
        invoice.pop("FullyPaidOnDate", None)
    stamp(ctx, invoice)


def save_invoice(
    ctx: ProviderContext,
    tenant: str,
    value: dict[str, Any],
    old: dict[str, Any] | None,
    resource: str = "Invoices",
    *,
    unitdp: int = 2,
) -> dict[str, Any]:
    record = {**copy.deepcopy(old or {}), **copy.deepcopy(value)}
    field = ID_FIELDS[resource]
    record.setdefault(field, ctx.ids.guid())
    if not old and resource != "Quotes" and not record.get("Type"):
        validation("Type is required.")
    default_type = "ACCRECCREDIT" if resource == "CreditNotes" else "ACCREC"
    record.setdefault("Type", default_type)
    types = {"ACCRECCREDIT", "ACCPAYCREDIT"} if resource == "CreditNotes" else {"ACCREC", "ACCPAY"}
    if resource != "Quotes" and record["Type"] not in types:
        validation("The Type is not valid.", record)
    if resource == "Quotes":
        record.pop("Type", None)
    record.setdefault("Status", "DRAFT")
    transitions = {
        "DRAFT": {"DRAFT", "SUBMITTED", "AUTHORISED", "DELETED"},
        "SUBMITTED": {"SUBMITTED", "DRAFT", "AUTHORISED", "DELETED"},
        "AUTHORISED": {"AUTHORISED", "VOIDED"},
        "PAID": {"PAID"},
        "VOIDED": {"VOIDED"},
        "DELETED": {"DELETED"},
    }
    if resource == "Quotes":
        transitions = {
            "DRAFT": {"DRAFT", "SENT", "DELETED"},
            "SENT": {"SENT", "ACCEPTED", "DECLINED", "DELETED"},
            "ACCEPTED": {"ACCEPTED", "INVOICED"},
            "DECLINED": {"DECLINED"},
            "INVOICED": {"INVOICED"},
            "DELETED": {"DELETED"},
        }
    before = (old or {}).get("Status", "DRAFT")
    if record["Status"] not in transitions.get(before, set()):
        validation("The status transition is not valid.", record)
    if old and resource != "Quotes" and record["Type"] != old["Type"]:
        validation("The Type cannot be changed.", record)
    if old and old["Status"] in {"DELETED", "VOIDED"} and any(k not in {field, "Status"} for k in value):
        validation("Deleted or voided transactions cannot be edited.", record)
    if resource == "CreditNotes":
        if "Allocations" in value:
            validation("Use the Allocations endpoint to allocate a credit note.")
        if (
            old
            and old.get("Allocations")
            and (
                record["Status"] == "VOIDED"
                or any(
                    k in value for k in ["LineItems", "LineAmountTypes", "Contact", "Date", "CurrencyCode"]
                )
            )
        ):
            validation("Remove credit allocations before changing financial details or voiding.")
    if (
        old
        and (old.get("AmountPaid", 0) or old.get("AmountCredited", 0))
        and any(
            k in value for k in ["LineItems", "LineAmountTypes", "Contact", "Type", "Date", "CurrencyCode"]
        )
    ):
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
        if old and old.get("LineAmountTypes") != record.get("LineAmountTypes") and "LineItems" not in value:
            # Prior response tax amounts belong to the previous tax mode, not this request.
            for line in record.get("LineItems", []):
                line.pop("TaxAmount", None)
        if (
            resource in {"Invoices", "CreditNotes"}
            and record["Status"] == "DRAFT"
            and not record.get("LineItems")
        ):
            record.update(LineItems=[], SubTotal=0.0, TotalTax=0.0, Total=0.0)
        else:
            line_items(
                ctx, tenant, record, purchase=record.get("Type", "").startswith("ACCPAY"), unitdp=unitdp
            )
    if record["Status"] in {"SUBMITTED", "AUTHORISED"} and not record.get("LineItems"):
        validation("At least one line item must be specified before submitting or approving.")
    if record["Total"] < 0:
        validation("The total cannot be negative.", record)
    number_field = {"Invoices": "InvoiceNumber", "CreditNotes": "CreditNoteNumber", "Quotes": "QuoteNumber"}[
        resource
    ]
    record.setdefault(
        number_field,
        {"Invoices": "INV-", "CreditNotes": "CN-", "Quotes": "QU-"}[resource]
        + str(ctx.ids.sequence(resource, 2000)),
    )
    if any(
        r[number_field] == record[number_field]
        and r[field] != record[field]
        and r.get("Type") == record.get("Type")
        and (record.get("Type") != "ACCPAY" or r["Contact"]["ContactID"] == record["Contact"]["ContactID"])
        for r in rows(ctx, resource, tenant)
    ):
        validation(f"{number_field} must be unique.", record)
    record.setdefault("Reference", "")
    record.setdefault("HasAttachments", False)
    if resource == "Invoices":
        record.setdefault("SentToContact", False)
        record.setdefault("AmountCredited", 0.0)
        settle(ctx, tenant, record)
    elif resource == "CreditNotes":
        record.setdefault("Allocations", [])
        record["RemainingCredit"] = money(record["Total"] - sum(a["Amount"] for a in record["Allocations"]))
        if record["Status"] in {"VOIDED", "DELETED"}:
            record["RemainingCredit"] = 0.0
    stamp(ctx, record)
    put(ctx, resource, tenant, record)
    if resource == "Invoices":
        contact = find(ctx, "Contacts", tenant, record["Contact"]["ContactID"])
        contact["IsCustomer" if record["Type"] == "ACCREC" else "IsSupplier"] = True
    return record


def save_payment(
    ctx: ProviderContext, tenant: str, value: dict[str, Any], old: dict[str, Any] | None
) -> dict[str, Any]:
    if old:
        if value.get("Status") != "DELETED":
            validation("Payments can only be updated to DELETED.")
        record = copy.deepcopy(old)
        record["Status"] = "DELETED"
        stamp(ctx, record)
        put(ctx, "Payments", tenant, record)
        settle(ctx, tenant, find(ctx, "Invoices", tenant, record["Invoice"]["InvoiceID"]))
        return record
    reference = value.get("Invoice", {})
    identifier = reference.get("InvoiceID") or reference.get("InvoiceNumber") or value.get("InvoiceNumber")
    if not identifier:
        validation("An InvoiceID or InvoiceNumber is required.")
    try:
        invoice = find(ctx, "Invoices", tenant, identifier)
    except EmulatorError:
        validation("The InvoiceID is not valid.")
    if invoice["Status"] != "AUTHORISED":
        validation("Payments can only be applied to authorised invoices.", value)
    amount = money(value.get("Amount", 0))
    if amount <= 0 or amount > invoice["AmountDue"]:
        validation("The payment amount must be positive and cannot exceed the amount outstanding.", value)
    account = account_ref(ctx, tenant, value.get("Account"), payment=True)
    if value.get("Status", "AUTHORISED") != "AUTHORISED":
        validation("The payment Status is not valid.")
    record = {
        "PaymentID": value.get("PaymentID", ctx.ids.guid()),
        "Amount": amount,
        "Account": account,
        "Invoice": {k: invoice[k] for k in ["InvoiceID", "InvoiceNumber", "Type", "Contact"]},
        "Reference": value.get("Reference", ""),
        "CurrencyRate": 1.0,
        "IsReconciled": bool(value.get("IsReconciled", False)),
        "Status": "AUTHORISED",
        "PaymentType": "ACCRECPAYMENT" if invoice["Type"] == "ACCREC" else "ACCPAYPAYMENT",
    }
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


def save_simple(
    ctx: ProviderContext,
    tenant: str,
    resource: str,
    value: dict[str, Any],
    old: dict[str, Any] | None,
    *,
    unitdp: int = 2,
) -> dict[str, Any]:
    record = {**copy.deepcopy(old or {}), **copy.deepcopy(value)}
    field = ID_FIELDS[resource]
    record.setdefault(field, ctx.ids.guid())
    if resource in {"Contacts", "ContactGroups", "Accounts", "TrackingCategories"}:
        name = record.get("Name")
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            validation("Name is required and must be less than 256 characters.", record)
        if name != name.strip() or "  " in name or "<" in name or ">" in name:
            validation("Name contains invalid whitespace or characters.", record)
        if any(
            r["Name"].casefold() == name.casefold() and r[field] != record[field]
            for r in rows(ctx, resource, tenant)
        ):
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
        record["ContactGroups"] = copy.deepcopy((old or {}).get("ContactGroups", []))
        record.setdefault("DefaultCurrency", "USD")
        record.pop("Balances", None)
        if record.get("ContactNumber") and any(
            c.get("ContactNumber") == record["ContactNumber"] and c["ContactID"] != record["ContactID"]
            for c in rows(ctx, resource, tenant)
        ):
            validation("ContactNumber must be unique.")
        if old and record["Name"] != old["Name"]:
            for related in ["Invoices", "CreditNotes", "Quotes", "BankTransactions", "Payments"]:
                for transaction in rows(ctx, related, tenant):
                    contact = transaction.get("Invoice", transaction).get("Contact", {})
                    if contact.get("ContactID") == record["ContactID"]:
                        contact["Name"] = record["Name"]
                        transaction.pop("_pdf", None)
    elif resource == "Accounts":
        kinds = {
            "BANK",
            "CURRENT",
            "CURRLIAB",
            "DEPRECIATN",
            "DIRECTCOSTS",
            "EQUITY",
            "EXPENSE",
            "FIXED",
            "INVENTORY",
            "LIABILITY",
            "NONCURRENT",
            "OTHERINCOME",
            "OVERHEADS",
            "PREPAYMENT",
            "REVENUE",
            "SALES",
            "TERMLIAB",
        }
        if not record.get("Code") or record.get("Type") not in kinds:
            validation("A Code and valid Type are required.", record)
        if any(
            r["Code"] == record["Code"] and r[field] != record[field] for r in rows(ctx, resource, tenant)
        ):
            validation("Account Code must be unique.")
        record["Class"] = {
            "BANK": "ASSET",
            "CURRENT": "ASSET",
            "FIXED": "ASSET",
            "INVENTORY": "ASSET",
            "NONCURRENT": "ASSET",
            "PREPAYMENT": "ASSET",
            "CURRLIAB": "LIABILITY",
            "TERMLIAB": "LIABILITY",
            "LIABILITY": "LIABILITY",
            "EQUITY": "EQUITY",
            "REVENUE": "REVENUE",
            "SALES": "REVENUE",
            "OTHERINCOME": "REVENUE",
        }.get(record["Type"], "EXPENSE")
        record.setdefault("TaxType", "NONE")
        record.setdefault("EnablePaymentsToAccount", record["Type"] == "BANK")
        record.setdefault("Status", "ACTIVE")
        if record["Status"] not in {"ACTIVE", "ARCHIVED"}:
            validation("Invalid account Status.")
        if old and old.get("SystemAccount") and any(k in value for k in ["Status", "Code", "Type"]):
            validation("System accounts cannot be changed.")
        if old and any(record[k] != old[k] for k in ["Code", "Type"]):
            from .ledger import postings

            if any(p.code == old["Code"] and p.amount for p in postings(ctx, tenant)):
                validation(
                    "This emulator requires account codes and types with transactions to remain unchanged."
                )
    elif resource == "Items":
        if not record.get("Code"):
            validation("Code is required.")
        if record.get("IsTrackedAsInventory"):
            validation("Tracked inventory is not supported by this emulator.")
        if any(
            r["Code"] == record["Code"] and r[field] != record[field] for r in rows(ctx, resource, tenant)
        ):
            validation("Item Code must be unique.")
        record.setdefault("IsSold", True)
        record.setdefault("IsPurchased", False)
        record.setdefault("IsTrackedAsInventory", False)
        for detail in ["SalesDetails", "PurchaseDetails"]:
            if detail in record and "UnitPrice" in record[detail]:
                record[detail]["UnitPrice"] = float(
                    number(record[detail]["UnitPrice"], "UnitPrice").quantize(
                        Decimal(1).scaleb(-unitdp), rounding=ROUND_HALF_UP
                    )
                )
            if detail in record and record[detail].get("AccountCode"):
                account_ref(ctx, tenant, {"Code": record[detail]["AccountCode"]})
    elif resource in {"ContactGroups", "TrackingCategories"}:
        record.setdefault("Status", "ACTIVE")
        if record["Status"] not in {"ACTIVE", "ARCHIVED", "DELETED"}:
            validation("Invalid Status.")
        if resource == "TrackingCategories":
            if (
                record["Status"] == "ACTIVE"
                and len(
                    [
                        r
                        for r in rows(ctx, resource, tenant)
                        if r["Status"] == "ACTIVE" and r[field] != record[field]
                    ]
                )
                >= 2
            ):
                validation("Only two active tracking categories are allowed.")
            record.setdefault("Options", [])
            names = set()
            for option in record["Options"]:
                name = option.get("Name", "")
                if not name.strip() or len(name) > 100 or name.casefold() in names:
                    validation("Tracking option names must be present, unique, and at most 100 characters.")
                names.add(name.casefold())
                option.setdefault("TrackingOptionID", ctx.ids.guid())
                option.setdefault("Status", "ACTIVE")
                if option["Status"] not in {"ACTIVE", "ARCHIVED", "DELETED"}:
                    validation("Invalid tracking option Status.")
            if len(record["Name"]) > 100:
                validation("Tracking category Name must not exceed 100 characters.")
        else:
            record["Contacts"] = copy.deepcopy((old or {}).get("Contacts", []))
            for contact in rows(ctx, "Contacts", tenant):
                memberships = contact.get("ContactGroups", [])
                for membership in memberships:
                    if membership["ContactGroupID"] == record[field]:
                        membership.update(Name=record["Name"], Status=record["Status"])
                if record["Status"] == "DELETED":
                    contact["ContactGroups"] = [
                        g for g in memberships if g["ContactGroupID"] != record[field]
                    ]
            if record["Status"] == "DELETED":
                record["Contacts"] = []
    stamp(ctx, record)
    return put(ctx, resource, tenant, record)


def save_bank(
    ctx: ProviderContext,
    tenant: str,
    resource: str,
    value: dict[str, Any],
    old: dict[str, Any] | None,
    *,
    unitdp: int = 2,
) -> dict[str, Any]:
    record = {**copy.deepcopy(old or {}), **copy.deepcopy(value)}
    record.setdefault(ID_FIELDS[resource], ctx.ids.guid())
    if resource == "BankTransfers":
        if old:
            if value.get("Status") != "DELETED" or any(k not in {"BankTransferID", "Status"} for k in value):
                validation("Bank transfers can only be updated to DELETED.")
            record = copy.deepcopy(old)
            record["Status"] = "DELETED"
            stamp(ctx, record)
            for side in ["From", "To"]:
                transaction = find(ctx, "BankTransactions", tenant, record[side + "BankTransactionID"])
                transaction["Status"] = "DELETED"
                stamp(ctx, transaction)
            return put(ctx, resource, tenant, record)
        for name in ["FromBankAccount", "ToBankAccount"]:
            record[name] = account_ref(ctx, tenant, record.get(name), payment=True)
            bank = find(ctx, "Accounts", tenant, record[name]["AccountID"])
            if bank["Type"] != "BANK":
                validation("Bank transfers require bank accounts.")
            if bank.get("CurrencyCode", "USD") != "USD":
                validation("This emulator supports bank transfers in USD only.")
        if record["FromBankAccount"]["AccountID"] == record["ToBankAccount"]["AccountID"]:
            validation("Bank transfer accounts must differ.")
        record["Amount"] = money(record.get("Amount", 0))
        if record["Amount"] <= 0:
            validation("Amount must be greater than zero.")
        record.setdefault("CurrencyRate", 1.0)
        record["Status"] = "AUTHORISED"
        record.setdefault("FromIsReconciled", False)
        record.setdefault("ToIsReconciled", False)
        record.setdefault("HasAttachments", False)
        record["CreatedDateUTC"] = msdate(ctx.clock.now())
    elif resource == "ManualJournals":
        record.setdefault("Status", "DRAFT")
        transitions = {
            "DRAFT": {"DRAFT", "POSTED", "DELETED"},
            "POSTED": {"POSTED", "VOIDED"},
            "VOIDED": {"VOIDED"},
            "DELETED": {"DELETED"},
        }
        if record["Status"] not in transitions[(old or {}).get("Status", "DRAFT")]:
            validation("Invalid manual journal status transition.")
        if (
            old
            and old["Status"] in {"VOIDED", "DELETED"}
            and any(k not in {"ManualJournalID", "Status"} for k in value)
        ):
            validation("Deleted or voided journals cannot be edited.")
        if not record.get("Narration") or not record.get("JournalLines"):
            validation("Narration and JournalLines are required.")
        if not isinstance(record["JournalLines"], list) or any(
            not isinstance(line, dict) for line in record["JournalLines"]
        ):
            validation("JournalLines must be an array of objects.")
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
        if money(sum(line["LineAmount"] for line in record["JournalLines"])) != 0:
            validation("Debits and credits must balance.")
        record.setdefault("ShowOnCashBasisReports", True)
    else:
        record.setdefault("Type", "SPEND")
        if record["Type"] not in {"SPEND", "RECEIVE"}:
            validation("Type must be SPEND or RECEIVE.")
        record.setdefault("Status", "AUTHORISED")
        if record["Status"] not in {"AUTHORISED", "DELETED"}:
            validation("Invalid bank transaction Status.")
        if old and old["Status"] == "DELETED" and record["Status"] != "DELETED":
            validation("Deleted bank transactions cannot be restored.")
        record["Contact"] = contact_ref(ctx, tenant, record.get("Contact"))
        record["BankAccount"] = account_ref(ctx, tenant, record.get("BankAccount"), payment=True)
        if find(ctx, "Accounts", tenant, record["BankAccount"]["AccountID"])["Type"] != "BANK":
            validation("BankAccount must reference a bank account.")
        line_items(ctx, tenant, record, purchase=record["Type"] == "SPEND", unitdp=unitdp)
        record.setdefault("CurrencyCode", "USD")
        if record["CurrencyCode"] != "USD":
            validation("This emulator supports transaction posting in USD only.")
        record.setdefault("IsReconciled", False)
        record.setdefault("HasAttachments", False)
    dates(ctx, record, ["Date"])
    stamp(ctx, record)
    if resource == "BankTransfers":
        for side, kind in [("From", "SPEND-TRANSFER"), ("To", "RECEIVE-TRANSFER")]:
            transaction_id = ctx.ids.guid()
            record[side + "BankTransactionID"] = transaction_id
            put(
                ctx,
                "BankTransactions",
                tenant,
                {
                    "BankTransactionID": transaction_id,
                    "Type": kind,
                    "BankAccount": copy.deepcopy(record[side + "BankAccount"]),
                    "Date": record["Date"],
                    "DateString": record["DateString"],
                    "Status": "AUTHORISED",
                    "CurrencyCode": "USD",
                    "CurrencyRate": 1.0,
                    "Reference": record.get("Reference", ""),
                    "IsReconciled": record.get(side + "IsReconciled", False),
                    "HasAttachments": False,
                    "LineItems": [],
                    "LineAmountTypes": "NoTax",
                    "SubTotal": record["Amount"],
                    "Total": record["Amount"],
                    "TotalTax": 0.0,
                    "UpdatedDateUTC": record["UpdatedDateUTC"],
                },
            )
    return put(ctx, resource, tenant, record)


def mutate(
    ctx: ProviderContext,
    tenant: str,
    resource: str,
    value: dict[str, Any],
    identifier: str | None = None,
    *,
    unitdp: int = 2,
) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, dict):
        validation("Each element must be an object.")
    from .validation import fields

    value = fields(value)
    for key in ["Date", "DueDate", "ExpiryDate"]:
        if key not in value and key + "String" in value:
            value[key] = value[key + "String"]
    # These values are calculated from line items, settlements, or file metadata.
    if resource in {"Invoices", "CreditNotes", "Quotes", "BankTransactions"}:
        for key in [
            "SubTotal",
            "TotalTax",
            "Total",
            "AmountDue",
            "AmountPaid",
            "AmountCredited",
            "RemainingCredit",
            "FullyPaidOnDate",
            "Payments",
            "HasAttachments",
            "UpdatedDateUTC",
        ]:
            value.pop(key, None)
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
        except (ValueError, TypeError, AttributeError):
            validation(f"{field} must be a GUID.")
    if resource in {"Invoices", "CreditNotes", "Quotes"}:
        record = save_invoice(ctx, tenant, value, old, resource, unitdp=unitdp)
    elif resource == "Payments":
        record = save_payment(ctx, tenant, value, old)
    elif resource in {"BankTransactions", "BankTransfers", "ManualJournals"}:
        record = save_bank(ctx, tenant, resource, value, old, unitdp=unitdp)
    else:
        record = save_simple(ctx, tenant, resource, value, old, unitdp=unitdp)
    history(
        ctx,
        tenant,
        resource,
        record[field],
        "Resource updated" if old else "Resource created",
        changes="Updated" if old else "Created",
    )
    return record, old is None
