"""Check wire types before mutations can poison shared accounting state."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .common import validation

STRINGS = set(
    [
        "Name",
        "Type",
        "Status",
        "ContactStatus",
        "Code",
        "ItemCode",
        "AccountCode",
        "TaxType",
        "LineAmountTypes",
        "CurrencyCode",
        "DefaultCurrency",
        "Description",
        "Narration",
        "Reference",
        "InvoiceNumber",
        "CreditNoteNumber",
        "QuoteNumber",
        "ContactNumber",
        "AccountNumber",
        "FirstName",
        "LastName",
        "EmailAddress",
        "BankAccountDetails",
        "TaxNumber",
        "BankAccountNumber",
        "BankAccountType",
        "Title",
        "Summary",
        "Terms",
        "Url",
        "Website",
        "Details",
        "AddressType",
        "AddressLine1",
        "AddressLine2",
        "AddressLine3",
        "AddressLine4",
        "City",
        "Region",
        "PostalCode",
        "Country",
        "AttentionTo",
        "PhoneType",
        "PhoneNumber",
        "PhoneAreaCode",
        "PhoneCountryCode",
        "Option",
    ]
)
OBJECTS = set(
    [
        "Contact",
        "Invoice",
        "Account",
        "BankAccount",
        "FromBankAccount",
        "ToBankAccount",
        "SalesDetails",
        "PurchaseDetails",
        "PaymentTerms",
        "Sales",
        "Bills",
    ]
)
ARRAYS = set(
    [
        "LineItems",
        "JournalLines",
        "Tracking",
        "Addresses",
        "Phones",
        "ContactPersons",
        "ContactGroups",
        "Contacts",
        "Options",
        "Allocations",
        "HistoryRecords",
    ]
)
BOOLEANS = set(
    [
        "IsCustomer",
        "IsSupplier",
        "IsSold",
        "IsPurchased",
        "IsTrackedAsInventory",
        "IsReconciled",
        "SentToContact",
        "EnablePaymentsToAccount",
        "ShowInExpenseClaims",
        "ShowOnCashBasisReports",
        "IncludeInEmails",
        "FromIsReconciled",
        "ToIsReconciled",
    ]
)
NUMBERS = set(
    [
        "Amount",
        "Quantity",
        "UnitAmount",
        "UnitPrice",
        "LineAmount",
        "TaxAmount",
        "DiscountRate",
        "DiscountAmount",
        "Day",
        "CurrencyRate",
    ]
)


def fields(value: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Validate nested input too, since a saved bad reference breaks subsequent reads."""
    if depth > 20:
        validation("The request body is too deeply nested.")
    result = {}
    for key, item in value.items():
        text(key)
        if key.startswith("_"):
            continue
        if key in STRINGS and not isinstance(item, str):
            validation(f"{key} must be a string.")
        if key in STRINGS and len(item) > (4000 if key in {"Description", "Details", "Narration"} else 255):
            validation(f"{key} is too long.")
        if key.endswith("ID") and key not in {"SalesTaxCodeID"}:
            try:
                if not isinstance(item, str):
                    raise ValueError
                item = str(UUID(item))
            except ValueError:
                validation(f"{key} must be a GUID.")
        if key in OBJECTS and not isinstance(item, dict):
            validation(f"{key} must be an object.")
        if key in ARRAYS and (
            not isinstance(item, list) or any(not isinstance(element, dict) for element in item)
        ):
            validation(f"{key} must be an array of objects.")
        if key in BOOLEANS and not isinstance(item, bool):
            validation(f"{key} must be a boolean.")
        if key in NUMBERS:
            from .transactions import number

            number(item, key)
        if key in {
            "Date",
            "DueDate",
            "ExpiryDate",
            "ExpectedPaymentDate",
            "PlannedPaymentDate",
        } and not isinstance(item, str):
            validation(f"{key} must be a date string.")
        result[key] = nested(item, depth + 1)
    return result


def nested(value: Any, depth: int) -> Any:
    if depth > 20:
        validation("The request body is too deeply nested.")
    if isinstance(value, dict):
        return fields(value, depth)
    if isinstance(value, list):
        return [nested(item, depth + 1) for item in value]
    if isinstance(value, str):
        text(value)
    return value


def text(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        validation("Strings must contain valid Unicode characters.")
