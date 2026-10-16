"""Validate wire types before mutations so rejected requests leave the books intact."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.quickbooks.common import fail, money, not_found, ref_value

OBJECTS = {
    "MetaData",
    "TxnTaxDetail",
    "BillEmail",
    "PrimaryEmailAddr",
    "Email",
    "PrimaryPhone",
    "Mobile",
    "AlternatePhone",
    "Fax",
    "BillAddr",
    "ShipAddr",
    "CompanyAddr",
    "LegalAddr",
    "WebAddr",
    "CustomerMemo",
    "CheckPayment",
    "CreditCardPayment",
    "DeliveryInfo",
    "Entity",
    "MarkupInfo",
    "AccountingInfoPrefs",
    "ProductAndServicesPrefs",
    "SalesFormsPrefs",
    "EmailMessagesPrefs",
    "VendorAndPurchasesPrefs",
    "TimeTrackingPrefs",
    "TaxPrefs",
    "CurrencyPrefs",
    "OtherPrefs",
    "SalesTaxRateList",
    "PurchaseTaxRateList",
}
ARRAYS = {"Line", "LinkedTxn", "TaxLine", "NameValue", "AttachableRef", "TaxRateDetail", "CustomField"}
BOOLEANS = {
    "sparse",
    "Active",
    "Taxable",
    "Job",
    "BillWithParent",
    "SubAccount",
    "SubClass",
    "SubDepartment",
    "SubItem",
    "Vendor1099",
    "BillableTime",
    "PercentBased",
    "AllowOnlineCreditCardPayment",
    "AllowOnlineACHPayment",
    "AllowIPNPayment",
    "TrackDepartments",
    "AccountNumbersEnabled",
    "ClassTrackingPerTxnLine",
    "ClassTrackingPerTxn",
    "MultiCurrencyEnabled",
    "TrackQtyOnHand",
}
NUMBERS = {
    "Amount",
    "TotalAmt",
    "Balance",
    "BalanceWithJobs",
    "CurrentBalance",
    "CurrentBalanceWithSubAccounts",
    "UnappliedAmt",
    "TotalTax",
    "UnitPrice",
    "Qty",
    "HourlyRate",
    "BillRate",
    "RateValue",
    "TaxPercent",
    "NetAmountTaxable",
    "ExchangeRate",
    "Deposit",
    "DiscountAmt",
    "DiscountPercent",
    "QtyOnHand",
}
INTEGERS = {"Hours", "Minutes", "BreakHours", "BreakMinutes", "DueDays", "LineNum", "Level", "Size"}
TEXT = {
    "Id",
    "SyncToken",
    "Name",
    "DisplayName",
    "CompanyName",
    "GivenName",
    "FamilyName",
    "MiddleName",
    "Title",
    "Suffix",
    "PrintOnCheckName",
    "Notes",
    "Description",
    "PrivateNote",
    "DocNumber",
    "Sku",
    "Type",
    "DetailType",
    "PostingType",
    "PaymentType",
    "PayType",
    "NameOf",
    "BillableStatus",
    "TxnStatus",
    "EmailStatus",
    "PrintStatus",
    "PreferredDeliveryMethod",
    "TaxIdentifier",
    "AcctNum",
    "AccountType",
    "AccountSubType",
    "Classification",
    "FullyQualifiedName",
    "EmployeeNumber",
    "PaymentRefNum",
    "Address",
    "FreeFormNumber",
    "URI",
    "Line1",
    "Line2",
    "Line3",
    "Line4",
    "Line5",
    "City",
    "Country",
    "CountrySubDivisionCode",
    "PostalCode",
    "TxnId",
    "TxnType",
    "TxnLineId",
    "FileName",
    "ContentType",
    "domain",
    "status",
    "GlobalTaxCalculation",
    "CreateTime",
    "LastUpdatedTime",
    "Value",
}
ENUMS = {
    "PostingType": {"Debit", "Credit"},
    "PaymentType": {"Cash", "Check", "CreditCard"},
    "PayType": {"Check", "CreditCard"},
    "NameOf": {"Employee", "Vendor"},
    "BillableStatus": {"Billable", "NotBillable", "HasBeenBilled"},
    "TxnStatus": {"Pending", "Accepted", "Closed", "Rejected", "Converted"},
}
OBJECT_PROPERTIES = {
    "MetaData": {"CreateTime", "LastUpdatedTime", "LastModifiedByRef"},
    "BillEmail": {"Address"},
    "TxnTaxDetail": {"TotalTax", "TaxLine", "TxnTaxCodeRef"},
}


def invalid(ctx: ProviderContext, field: str, detail: str) -> Any:
    fail(ctx, 400, "Request has invalid or unsupported property", detail, "2010", element=field)


def validate_shape(ctx: ProviderContext, payload: Any, path: str = "", depth: int = 0) -> None:
    if depth > 30 or not isinstance(payload, dict):
        invalid(ctx, path, "Request must contain JSON objects with bounded nesting.")
    for key, value in list(payload.items()):
        field = f"{path}.{key}" if path else key
        if isinstance(value, str) and any(
            0xD800 <= ord(c) <= 0xDFFF or (ord(c) < 32 and c not in "\t\r\n") for c in value
        ):
            invalid(ctx, field, "Invalid Unicode or control character.")
        if value == "" and (key.endswith("Date") or key in NUMBERS):
            payload.pop(key)
            continue
        if key in ("SyncToken", "TxnLineId") and isinstance(value, int) and not isinstance(value, bool):
            payload[key] = str(value)
            continue
        if key.startswith("_"):
            invalid(ctx, field, "Internal properties cannot be supplied through the Accounting API.")
        if value is None:
            # Optional properties can be cleared, but required containers cannot become null.
            if key in ARRAYS or key == "MetaData" or key.endswith("LineDetail") or key in BOOLEANS:
                invalid(ctx, field, f"Invalid null value for {key}.")
            if key == "TxnDate":
                invalid(ctx, field, "TxnDate cannot be null.")
            continue
        if key not in ARRAYS and (
            key in OBJECTS or key.endswith("Prefs") or key.endswith("LineDetail") or key.endswith("Ref")
        ):
            if not isinstance(value, dict):
                invalid(ctx, field, f"{key} must be an object.")
            if key in OBJECT_PROPERTIES and set(value) - OBJECT_PROPERTIES[key]:
                invalid(ctx, field, f"Unsupported property inside {key}.")
            if key.endswith("Ref"):
                if not isinstance(value.get("value"), str) or not value["value"]:
                    invalid(ctx, field, "Reference value must be a nonempty string.")
                if "name" in value and not isinstance(value["name"], str):
                    invalid(ctx, field, "Reference name must be a string.")
        elif key in ARRAYS:
            if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
                invalid(ctx, field, f"{key} must be an array of objects.")
        elif key in BOOLEANS:
            if not isinstance(value, bool):
                invalid(ctx, field, f"{key} must be a boolean.")
        elif key in NUMBERS:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                invalid(ctx, field, f"{key} must be a decimal number.")
            try:
                number = float(value)
                if not math.isfinite(number) or abs(number) > 1e15:
                    raise ValueError
            except (ValueError, OverflowError):
                invalid(ctx, field, f"{key} must be a finite decimal in the supported range.")
            payload[key] = number
        elif key in INTEGERS:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**31 - 1:
                invalid(ctx, field, f"{key} must be a nonnegative integer.")
            if key in ("Minutes", "BreakMinutes") and value > 59:
                invalid(ctx, field, f"{key} must be between 0 and 59.")
        elif key.endswith("Date"):
            try:
                if not isinstance(value, str) or len(value) != 10:
                    raise ValueError
                date.fromisoformat(value)
            except ValueError:
                invalid(ctx, field, f"{key} must be a YYYY-MM-DD date.")
        elif key in TEXT:
            if not isinstance(value, str):
                invalid(ctx, field, f"{key} must be a string.")
            limit = (
                500
                if key == "DisplayName"
                else 100
                if key == "Name"
                else 4000
                if key == "Description"
                else 10000
            )
            if len(value) > limit:
                invalid(ctx, field, f"{key} exceeds its supported length.")
        if key in ENUMS and value not in ENUMS[key]:
            invalid(ctx, field, f"Invalid {key} value.")
        if isinstance(value, dict):
            validate_shape(ctx, value, field, depth + 1)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, dict):
                    validate_shape(ctx, child, field, depth + 1)
        if isinstance(value, float) and not math.isfinite(value):
            invalid(ctx, field, "Non-finite JSON numbers are not supported.")


def validate_business(ctx: ProviderContext, entity: str, record: dict[str, Any]) -> None:
    """Validate hierarchy and linked transactions before changing any collection."""
    parent_id = ref_value(record.get("ParentRef"))
    seen = {str(record.get("Id", ""))}
    while parent_id:
        if parent_id in seen:
            fail(
                ctx, 400, "Business Validation Error", "A parent relationship cannot contain a cycle.", "6000"
            )
        seen.add(parent_id)
        parent = ctx.store.collection(entity).get(parent_id)
        parent_id = ref_value((parent or {}).get("ParentRef"))
    line_types = {
        "Invoice": {"SalesItemLineDetail", "SubTotalLineDetail", "DescriptionOnly"},
        "Estimate": {"SalesItemLineDetail", "SubTotalLineDetail", "DescriptionOnly"},
        "SalesReceipt": {"SalesItemLineDetail", "SubTotalLineDetail", "DescriptionOnly"},
        "CreditMemo": {"SalesItemLineDetail", "SubTotalLineDetail", "DescriptionOnly"},
        "Bill": {"AccountBasedExpenseLineDetail"},
        "Purchase": {"AccountBasedExpenseLineDetail"},
        "JournalEntry": {"JournalEntryLineDetail"},
        "Deposit": {"DepositLineDetail"},
    }
    for line in record.get("Line") or []:
        if entity in line_types:
            detail_type = line.get("DetailType")
            if detail_type not in line_types[entity]:
                invalid(ctx, "Line.DetailType", "Unsupported line detail type.")
            if detail_type in (
                "AccountBasedExpenseLineDetail",
                "JournalEntryLineDetail",
                "DepositLineDetail",
            ) and not ref_value((line.get(detail_type) or {}).get("AccountRef")):
                fail(ctx, 400, "Required param missing", "Line AccountRef is required.", "2020")
            if detail_type == "JournalEntryLineDetail" and not (line.get(detail_type) or {}).get(
                "PostingType"
            ):
                fail(ctx, 400, "Required param missing", "PostingType is required.", "2020")
    if entity == "Account" and record.get("AccountType") not in {
        "Bank",
        "Accounts Receivable",
        "Other Current Asset",
        "Fixed Asset",
        "Other Asset",
        "Accounts Payable",
        "Credit Card",
        "Other Current Liability",
        "Long Term Liability",
        "Equity",
        "Income",
        "Other Income",
        "Expense",
        "Other Expense",
        "Cost of Goods Sold",
    }:
        invalid(ctx, "AccountType", "Invalid account type.")
    if entity == "TimeActivity":
        field = "EmployeeRef" if record.get("NameOf") == "Employee" else "VendorRef"
        if not ref_value(record.get(field)):
            fail(ctx, 400, "Required param missing", f"{field} is required.", "2020", element=field)
    if entity not in ("Payment", "BillPayment"):
        return
    if entity == "BillPayment":
        field = "CheckPayment" if record.get("PayType") == "Check" else "CreditCardPayment"
        ref_field = "BankAccountRef" if field == "CheckPayment" else "CCAccountRef"
        if not ref_value((record.get(field) or {}).get(ref_field)):
            fail(ctx, 400, "Required param missing", f"{field}.{ref_field} is required.", "2020")
    total = money(record.get("TotalAmt"))
    lines = record.get("Line") or []
    if total < 0 or sum(money(line.get("Amount")) for line in lines) > total + 0.005:
        fail(
            ctx, 400, "Business Validation Error", "Applied amounts cannot exceed the payment total.", "6000"
        )
    target_entity, party = ("Invoice", "CustomerRef") if entity == "Payment" else ("Bill", "VendorRef")
    for line in lines:
        if money(line.get("Amount")) < 0:
            fail(ctx, 400, "Business Validation Error", "Payment line amounts cannot be negative.", "6000")
        for linked in line.get("LinkedTxn") or []:
            if linked.get("TxnType") != target_entity:
                fail(ctx, 400, "Business Validation Error", "Unsupported linked transaction type.", "6000")
            target = ctx.store.collection(target_entity).get(linked.get("TxnId"))
            if not target or target.get("_deleted") or target.get("_voided"):
                not_found(ctx, target_entity)
            if ref_value(target.get(party)) != ref_value(record.get(party)):
                fail(
                    ctx,
                    400,
                    "Business Validation Error",
                    "Linked transaction belongs to another party.",
                    "6000",
                )
