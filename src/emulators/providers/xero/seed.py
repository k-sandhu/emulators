"""Project the shared organisation without generating binary content during seed."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.world.model import Address

from .common import guid, main_tenant, money, msdate, put, rows

ACCOUNT_TYPES = {
    "Bank": "BANK",
    "Accounts Receivable": "CURRENT",
    "Other Current Asset": "CURRENT",
    "Fixed Asset": "FIXED",
    "Accounts Payable": "CURRLIAB",
    "Credit Card": "BANK",
    "Other Current Liability": "CURRLIAB",
    "Long Term Liability": "TERMLIAB",
    "Equity": "EQUITY",
    "Income": "REVENUE",
    "Expense": "EXPENSE",
    "Other Expense": "OVERHEADS",
}


def address(value: Address, kind: str = "STREET") -> dict[str, Any]:
    return {
        "AddressType": kind,
        "AddressLine1": value.street,
        "City": value.city,
        "Region": value.state,
        "PostalCode": value.postal_code,
        "Country": value.country,
    }


def tax_type(ctx: ProviderContext, rate: float, *, purchase: bool = False) -> str:
    if not rate:
        return "NONE"
    rates = sorted({i.tax_rate for i in ctx.world.invoices if i.tax_rate})
    return f"TAX{rates.index(rate) * 2 + int(purchase) + 1:03d}"


def tracking(ctx: ProviderContext, person_id: str) -> list[dict[str, Any]]:
    person = ctx.world.person(person_id)
    result = []
    for name, value in [("Department", person.department_id), ("Office", person.office_id)]:
        if value:
            label = ctx.world.department(value).name if name == "Department" else ctx.world.office(value).name
            result.append(
                {
                    "TrackingCategoryID": guid("tracking", name),
                    "Name": name,
                    "TrackingOptionID": guid("trackingoption", value),
                    "Option": label,
                }
            )
    return result


def seed(ctx: ProviderContext) -> None:
    world = ctx.world
    ctx.tokens.register_client("xero-test-client", "xero-test-secret", name="Emulators")
    tenant = main_tenant(ctx)
    demo = guid("tenant", "demo")
    updated = msdate(world.anchor)
    for tid, name in [(tenant, world.org.name), (demo, "Demo Company (US)")]:
        ctx.store.collection("_tenants").put({"id": tid, "name": name})
        organisation = {
            "OrganisationID": tid,
            "Name": name,
            "LegalName": world.org.legal_name if tid == tenant else name,
            "BaseCurrency": "USD",
            "CountryCode": "US",
            "OrganisationType": "PARTNERSHIP",
            "FinancialYearEndDay": 31,
            "FinancialYearEndMonth": 12,
            "ShortCode": "!xK2Lm" if tid == tenant else "!demoUS",
            "Addresses": [address(world.org.address)],
            "Phones": [{"PhoneType": "OFFICE", "PhoneNumber": world.org.phone}],
            "Timezone": "EASTERNSTANDARDTIME",
            "OrganisationStatus": "ACTIVE",
            "IsDemoCompany": tid == demo,
            "SalesTaxBasis": "ACCRUALS",
            "SalesTaxPeriod": "MONTHLY",
            "PaysTax": True,
            "Version": "US",
            "CreatedDateUTC": msdate(world.org.founded),
        }
        put(ctx, "Organisation", tid, organisation)
        for account in world.accounts:
            kind = ACCOUNT_TYPES[account.type]
            record = {
                "AccountID": guid("account", account.id),
                "Code": account.number,
                "Name": account.name,
                "Type": kind,
                "Class": account.classification.upper(),
                "TaxType": "NONE",
                "Status": "ACTIVE",
                "Description": account.description,
                "EnablePaymentsToAccount": kind == "BANK",
                "ShowInExpenseClaims": False,
                "ReportingCode": {
                    "Asset": "ASS",
                    "Liability": "LIA",
                    "Equity": "EQU",
                    "Revenue": "REV",
                    "Expense": "EXP",
                }[account.classification],
                "UpdatedDateUTC": updated,
                "_opening": account.opening_balance if tid == tenant else 0.0,
            }
            if kind == "BANK":
                record.update(
                    BankAccountNumber="000000" + account.number, BankAccountType="BANK", CurrencyCode="USD"
                )
            if account.id in {"a1100", "a2000", "a3200"}:
                record["SystemAccount"] = {
                    "a1100": "DEBTORS",
                    "a2000": "CREDITORS",
                    "a3200": "RETAINEDEARNINGS",
                }[account.id]
            put(ctx, "Accounts", tid, record)
        # The world's opening trial balance has no retained-earnings plug; make it explicit here.
        if tid == tenant:
            opening = sum(
                a.opening_balance * (1 if a.classification in {"Asset", "Expense"} else -1)
                for a in world.accounts
            )
            next(a for a in rows(ctx, "Accounts", tid) if a["Code"] == "3200")["_opening"] += money(opening)
        for category, options in [("Department", world.departments), ("Office", world.offices)]:
            put(
                ctx,
                "TrackingCategories",
                tid,
                {
                    "TrackingCategoryID": guid("tracking", category),
                    "Name": category,
                    "Status": "ACTIVE",
                    "Options": [
                        {"TrackingOptionID": guid("trackingoption", o.id), "Name": o.name, "Status": "ACTIVE"}
                        for o in options
                    ],
                },
            )
        for rate in sorted({0.0, *(i.tax_rate for i in world.invoices)}):
            for purchase in [False, True] if rate else [False]:
                name = tax_type(ctx, rate, purchase=purchase)
                put(
                    ctx,
                    "TaxRates",
                    tid,
                    {
                        "Name": "Tax Exempt"
                        if not rate
                        else f"{'Purchases' if purchase else 'Sales'} Tax {rate * 100:g}%",
                        "TaxType": name,
                        "Status": "ACTIVE",
                        "ReportTaxType": ("INPUT" if purchase else "OUTPUT") if rate else "NONE",
                        "CanApplyToAssets": True,
                        "CanApplyToEquity": True,
                        "CanApplyToExpenses": True,
                        "CanApplyToLiabilities": True,
                        "CanApplyToRevenue": True,
                        "DisplayTaxRate": rate * 100,
                        "EffectiveRate": rate * 100,
                        "TaxComponents": [
                            {
                                "Name": "Sales Tax",
                                "Rate": rate * 100,
                                "IsCompound": False,
                                "IsNonRecoverable": False,
                            }
                        ],
                    },
                )
        exempt = next(t for t in rows(ctx, "TaxRates", tid) if t["TaxType"] == "NONE")
        for code, label in [
            ("INPUT", "Tax on Purchases"),
            ("OUTPUT", "Tax on Sales"),
            ("GSTONIMPORTS", "Sales Tax on Imports"),
        ]:
            put(ctx, "TaxRates", tid, {**exempt, "TaxType": code, "Name": label, "ReportTaxType": code})
        for code, description in [
            ("USD", "United States Dollar"),
            ("CAD", "Canadian Dollar"),
            ("GBP", "British Pound"),
        ]:
            put(ctx, "Currencies", tid, {"Code": code, "Description": description})
        put(
            ctx,
            "BrandingThemes",
            tid,
            {
                "BrandingThemeID": guid("branding", "standard"),
                "Name": "Standard",
                "SortOrder": 0,
                "CreatedDateUTC": msdate(world.org.founded),
            },
        )
        for person in world.employees:
            role = (
                "ADMIN"
                if person.is_partner or person.is_admin
                else "STANDARD"
                if person.department_id in {"finance", "exec"}
                else "READONLY"
            )
            ctx.store.collection("_users").put({"id": person.id, "role": role, "active": person.active})
            if person.active:
                put(
                    ctx,
                    "Users",
                    tid,
                    {
                        "UserID": guid("user", person.id),
                        "EmailAddress": person.email,
                        "FirstName": person.first_name,
                        "LastName": person.last_name,
                        "IsSubscriber": person.id == "p001",
                        "OrganisationRole": "FINANCIALADVISER" if role == "ADMIN" else role,
                        "UpdatedDateUTC": updated,
                    },
                )
        for item in world.items:
            put(
                ctx,
                "Items",
                tid,
                {
                    "ItemID": guid("item", item.id),
                    "Code": item.sku,
                    "Name": item.name,
                    "Description": item.description,
                    "IsSold": True,
                    "IsPurchased": False,
                    "IsTrackedAsInventory": False,
                    "SalesDetails": {
                        "UnitPrice": item.rate,
                        "AccountCode": world.account(item.income_account_id).number,
                        "TaxType": "NONE",
                    },
                    "UpdatedDateUTC": updated,
                },
            )

    for name in ["Audit Clients", "Advisory Clients"]:
        put(
            ctx,
            "ContactGroups",
            tenant,
            {"ContactGroupID": guid("contactgroup", name), "Name": name, "Status": "ACTIVE", "Contacts": []},
        )
    for client in world.clients:
        primary = world.person(client.primary_contact_id)
        engagements = [e for e in world.engagements if e.client_id == client.id]
        groups = []
        for group in rows(ctx, "ContactGroups", tenant):
            if any(("Audit" in e.service) == group["Name"].startswith("Audit") for e in engagements):
                group["Contacts"].append({"ContactID": guid("contact", client.id)})
                groups.append(
                    {"ContactGroupID": group["ContactGroupID"], "Name": group["Name"], "Status": "ACTIVE"}
                )
        put(
            ctx,
            "Contacts",
            tenant,
            {
                "ContactID": guid("contact", client.id),
                "ContactNumber": client.id,
                "Name": client.name,
                "FirstName": primary.first_name,
                "LastName": primary.last_name,
                "EmailAddress": primary.email,
                "IsCustomer": client.status != "Prospect",
                "IsSupplier": False,
                "ContactStatus": "ARCHIVED" if client.status == "Former" else "ACTIVE",
                "DefaultCurrency": "USD",
                "TaxNumber": client.tax_id,
                "Addresses": [address(client.address), address(client.address, "POBOX")],
                "Phones": [
                    {"PhoneType": "DEFAULT", "PhoneNumber": client.phone},
                    {"PhoneType": "MOBILE", "PhoneNumber": primary.phone_mobile or ""},
                ],
                "ContactPersons": [
                    {
                        "FirstName": world.person(p).first_name,
                        "LastName": world.person(p).last_name,
                        "EmailAddress": world.person(p).email,
                        "IncludeInEmails": True,
                    }
                    for p in client.contact_ids
                    if p != client.primary_contact_id
                ],
                "PaymentTerms": {"Sales": {"Day": client.payment_terms_days, "Type": "DAYSAFTERBILLDATE"}},
                "ContactGroups": groups,
                "UpdatedDateUTC": updated,
            },
        )
    for vendor in world.vendors:
        put(
            ctx,
            "Contacts",
            tenant,
            {
                "ContactID": guid("contact", vendor.id),
                "ContactNumber": vendor.id,
                "Name": vendor.name,
                "EmailAddress": vendor.email,
                "IsCustomer": False,
                "IsSupplier": True,
                "ContactStatus": "ACTIVE",
                "Addresses": [address(vendor.address)],
                "TaxNumber": vendor.tax_id,
                "Phones": [{"PhoneType": "DEFAULT", "PhoneNumber": vendor.phone}],
                "ContactPersons": [],
                "ContactGroups": [],
                "DefaultCurrency": "USD",
                "UpdatedDateUTC": updated,
                "PaymentTerms": {"Bills": {"Day": vendor.payment_terms_days, "Type": "DAYSAFTERBILLDATE"}},
            },
        )

    for invoice in world.invoices:
        contact = next(c for c in rows(ctx, "Contacts", tenant) if c["ContactNumber"] == invoice.client_id)
        lines = []
        for index, line in enumerate(invoice.lines):
            item = world.item(line.item_id)
            lines.append(
                {
                    "LineItemID": guid("line", f"{invoice.id}/{index}"),
                    "Description": line.description,
                    "Quantity": line.quantity,
                    "UnitAmount": line.rate,
                    "ItemCode": item.sku,
                    "AccountCode": world.account(item.income_account_id).number,
                    "TaxType": tax_type(ctx, invoice.tax_rate),
                    "TaxAmount": money(line.amount * invoice.tax_rate),
                    "LineAmount": line.amount,
                    "Tracking": tracking(ctx, invoice.created_by_id),
                }
            )
        if lines:
            lines[-1]["TaxAmount"] = money(
                lines[-1]["TaxAmount"] + invoice.tax - sum(line["TaxAmount"] for line in lines)
            )
        status = {"Draft": "DRAFT", "Paid": "PAID", "Void": "VOIDED"}.get(invoice.status, "AUTHORISED")
        record = {
            "InvoiceID": guid("invoice", invoice.id),
            "InvoiceNumber": invoice.number,
            "Reference": invoice.memo,
            "Type": "ACCREC",
            "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
            "Date": msdate(invoice.issue_date),
            "DateString": invoice.issue_date.isoformat() + "T00:00:00",
            "DueDate": msdate(invoice.due_date),
            "DueDateString": invoice.due_date.isoformat() + "T00:00:00",
            "Status": status,
            "LineAmountTypes": "Exclusive",
            "LineItems": lines,
            "SubTotal": invoice.subtotal,
            "TotalTax": invoice.tax,
            "Total": invoice.total,
            "AmountDue": 0 if status in {"VOIDED", "DELETED"} else invoice.balance,
            "AmountPaid": invoice.paid_amount,
            "AmountCredited": 0.0,
            "CurrencyCode": "USD",
            "CurrencyRate": 1.0,
            "Payments": [],
            "HasAttachments": False,
            "SentToContact": status != "DRAFT",
            "UpdatedDateUTC": updated,
            "BrandingThemeID": guid("branding", "standard"),
        }
        put(ctx, "Invoices", tenant, record)
    for bill in world.bills:
        contact = next(c for c in rows(ctx, "Contacts", tenant) if c["ContactNumber"] == bill.vendor_id)
        record = {
            "InvoiceID": guid("invoice", bill.id),
            "InvoiceNumber": bill.number,
            "Reference": bill.memo,
            "Type": "ACCPAY",
            "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
            "Date": msdate(bill.bill_date),
            "DateString": bill.bill_date.isoformat() + "T00:00:00",
            "DueDate": msdate(bill.due_date),
            "DueDateString": bill.due_date.isoformat() + "T00:00:00",
            "Status": "PAID" if bill.status == "Paid" else "AUTHORISED",
            "LineAmountTypes": "NoTax",
            "LineItems": [
                {
                    "LineItemID": guid("line", f"{bill.id}/{index}"),
                    "Description": line.description,
                    "Quantity": 1.0,
                    "UnitAmount": line.amount,
                    "LineAmount": line.amount,
                    "AccountCode": world.account(line.account_id).number,
                    "TaxType": "NONE",
                    "TaxAmount": 0.0,
                    "Tracking": [],
                }
                for index, line in enumerate(bill.lines)
            ],
            "SubTotal": bill.total,
            "TotalTax": 0.0,
            "Total": bill.total,
            "AmountDue": 0.0 if bill.status == "Paid" else bill.total,
            "AmountPaid": bill.total if bill.status == "Paid" else 0.0,
            "AmountCredited": 0.0,
            "CurrencyCode": "USD",
            "CurrencyRate": 1.0,
            "Payments": [],
            "HasAttachments": False,
            "SentToContact": False,
            "UpdatedDateUTC": updated,
        }
        put(ctx, "Invoices", tenant, record)
        if bill.paid_date:
            seed_payment(
                ctx,
                tenant,
                record,
                guid("payment", bill.id),
                bill.paid_date,
                bill.total,
                "a1000",
                bill.number,
            )
    for payment in world.payments:
        invoice = next(
            i for i in rows(ctx, "Invoices", tenant) if i["InvoiceID"] == guid("invoice", payment.invoice_id)
        )
        seed_payment(
            ctx,
            tenant,
            invoice,
            guid("payment", payment.id),
            payment.date,
            payment.amount,
            payment.deposit_account_id,
            payment.reference,
        )

    # Additional ledger activity uses world vendors, dates, accounts, and equipment only.
    for offset in range(12):
        first = world.anchor.date().replace(day=1)
        for _ in range(offset):
            first = (first - timedelta(days=1)).replace(day=1)
        day = min(first + timedelta(days=14), world.anchor.date())
        journal_date = date(first.year, first.month, monthrange(first.year, first.month)[1])
        if journal_date <= world.anchor.date():
            put(
                ctx,
                "ManualJournals",
                tenant,
                {
                    "ManualJournalID": guid("journal", first.isoformat()),
                    "Date": msdate(journal_date),
                    "Narration": f"Monthly equipment depreciation {first:%B %Y}",
                    "Status": "POSTED",
                    "LineAmountTypes": "NoTax",
                    "ShowOnCashBasisReports": True,
                    "JournalLines": [
                        {
                            "Description": "Computer equipment depreciation",
                            "AccountCode": "7000",
                            "LineAmount": 1500.0,
                            "TaxType": "NONE",
                            "Tracking": [],
                        },
                        {
                            "Description": "Accumulated depreciation",
                            "AccountCode": "1590",
                            "LineAmount": -1500.0,
                            "TaxType": "NONE",
                            "Tracking": [],
                        },
                    ],
                    "UpdatedDateUTC": updated,
                },
            )
        vendor = world.vendors[offset % len(world.vendors)]
        contact = next(c for c in rows(ctx, "Contacts", tenant) if c["ContactNumber"] == vendor.id)
        account = next(
            (a for a in world.accounts if a.name == vendor.expense_account), world.account("a6210")
        )
        amount = money(vendor.monthly_amount * 0.05)
        put(
            ctx,
            "BankTransactions",
            tenant,
            {
                "BankTransactionID": guid("banktransaction", first.isoformat()),
                "Type": "SPEND",
                "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
                "Date": msdate(day),
                "DateString": day.isoformat() + "T00:00:00",
                "Status": "AUTHORISED",
                "BankAccount": {
                    "AccountID": guid("account", "a2100"),
                    "Code": "2100",
                    "Name": world.account("a2100").name,
                },
                "LineAmountTypes": "NoTax",
                "LineItems": [
                    {
                        "LineItemID": guid("bankline", first.isoformat()),
                        "Description": vendor.category + " incidental expense",
                        "Quantity": 1.0,
                        "UnitAmount": amount,
                        "LineAmount": amount,
                        "TaxAmount": 0.0,
                        "TaxType": "NONE",
                        "AccountCode": account.number,
                        "Tracking": [],
                    }
                ],
                "SubTotal": amount,
                "TotalTax": 0.0,
                "Total": amount,
                "CurrencyCode": "USD",
                "CurrencyRate": 1.0,
                "Reference": f"CARD-{first:%Y%m}",
                "IsReconciled": True,
                "HasAttachments": False,
                "UpdatedDateUTC": updated,
            },
        )
    for deal in world.deals:
        if deal.is_closed:
            continue
        contact = next(c for c in rows(ctx, "Contacts", tenant) if c["ContactNumber"] == deal.client_id)
        put(
            ctx,
            "Quotes",
            tenant,
            {
                "QuoteID": guid("quote", deal.id),
                "QuoteNumber": "QU-" + deal.id,
                "Contact": {"ContactID": contact["ContactID"], "Name": contact["Name"]},
                "Title": deal.name,
                "Date": msdate(deal.created),
                "ExpiryDate": msdate(deal.close_date),
                "Status": "SENT",
                "LineAmountTypes": "Exclusive",
                "LineItems": [
                    {
                        "LineItemID": guid("quoteline", deal.id),
                        "Description": deal.name,
                        "Quantity": 1.0,
                        "UnitAmount": deal.amount,
                        "LineAmount": deal.amount,
                        "AccountCode": "4200",
                        "TaxType": "NONE",
                        "TaxAmount": 0.0,
                        "Tracking": [],
                    }
                ],
                "SubTotal": deal.amount,
                "TotalTax": 0.0,
                "Total": deal.amount,
                "CurrencyCode": "USD",
                "UpdatedDateUTC": updated,
            },
        )

    account_ids = {a["Code"]: a["AccountID"] for a in rows(ctx, "Accounts", tenant)}
    for resource in ["Invoices", "Quotes", "BankTransactions"]:
        for record in rows(ctx, resource, tenant):
            for line in record["LineItems"]:
                line["AccountID"] = account_ids[line["AccountCode"]]


def seed_payment(
    ctx: ProviderContext,
    tenant: str,
    invoice: dict[str, Any],
    identifier: str,
    day: date,
    amount: float,
    account: str,
    reference: str,
) -> None:
    payment = {
        "PaymentID": identifier,
        "Date": msdate(day),
        "Amount": amount,
        "CurrencyRate": 1.0,
        "Reference": reference,
        "Status": "AUTHORISED",
        "IsReconciled": True,
        "PaymentType": "ACCRECPAYMENT" if invoice["Type"] == "ACCREC" else "ACCPAYPAYMENT",
        "Invoice": {
            "InvoiceID": invoice["InvoiceID"],
            "InvoiceNumber": invoice["InvoiceNumber"],
            "Type": invoice["Type"],
            "Contact": invoice["Contact"],
        },
        "Account": {"AccountID": guid("account", account), "Code": ctx.world.account(account).number},
        "UpdatedDateUTC": msdate(ctx.world.anchor),
    }
    put(ctx, "Payments", tenant, payment)
    invoice["Payments"].append(
        {
            "PaymentID": identifier,
            "Date": payment["Date"],
            "Amount": amount,
            "Reference": reference,
            "CurrencyRate": 1.0,
        }
    )
    if invoice["Status"] == "PAID":
        invoice["FullyPaidOnDate"] = max(
            (p["Date"] for p in invoice["Payments"]), key=lambda x: int(x.split("(")[1].split("+")[0])
        )
