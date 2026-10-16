"""Project the Halcyon world into the QBO data model (deterministic, <2s)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.quickbooks.common import date_time, money
from emulators.providers.quickbooks.ledger import refresh_computed
from emulators.world.model import Address, World

USD = {"value": "USD", "name": "United States Dollar"}


def _meta(d: date) -> dict[str, Any]:
    return {"CreateTime": date_time(d), "LastUpdatedTime": date_time(d, 16)}


def _addr(address: Address, addr_id: str = "1") -> dict[str, Any]:
    return {
        "Id": addr_id,
        "Line1": address.street,
        "City": address.city,
        "CountrySubDivisionCode": address.state,
        "PostalCode": address.postal_code,
        "Country": address.country,
    }


def _ref(record_id: str, name: str) -> dict[str, Any]:
    return {"value": str(record_id), "name": name}


def _put(ctx: ProviderContext, entity: str, record: dict[str, Any]) -> dict[str, Any]:
    record.setdefault("domain", "QBO")
    record.setdefault("sparse", False)
    ctx.store.collection(entity).put_as(str(record["Id"]), record)
    return record


def _seq(ctx: ProviderContext, entity: str) -> str:
    return str(ctx.ids.sequence(entity))


def _bill_rate(ctx: ProviderContext, person: Any) -> float:
    if person.is_partner:
        return 350.0
    title = person.job_title.lower()
    if "director" in title or "principal" in title:
        return 295.0
    if "manager" in title or "senior" in title:
        return 225.0
    if "associate" in title or "analyst" in title:
        return 145.0
    return 175.0


# Engagement service -> (class top name, sub name or None)
SERVICE_CLASS: dict[str, tuple[str, str | None]] = {
    "Audit": ("Audit & Assurance", None),
    "Review Engagement": ("Audit & Assurance", None),
    "Tax Compliance": ("Tax", "Tax Compliance"),
    "Tax Advisory": ("Tax", "Tax Advisory"),
    "SR&ED Claim": ("Tax", "Tax Advisory"),
    "Transaction Advisory": ("Advisory", "Transaction Advisory"),
    "Systems Advisory": ("Advisory", "Systems Advisory"),
    "Fractional CFO": ("Advisory", "Fractional CFO"),
    "Bookkeeping": ("Operations", None),
}


def seed(ctx: ProviderContext) -> None:
    world: World = ctx.world
    org = world.org
    anchor = world.anchor.date()
    meta: dict[str, Any] = {"maps": {}}
    ctx.store.meta.update(meta)
    maps: dict[str, dict[str, str]] = ctx.store.meta["maps"]

    # -- users behind delegated tokens / userinfo ------------------------------
    for person in world.employees:
        full = person.is_partner or person.department_id in ("finance", "exec") or person.is_admin
        ctx.store.collection("_users").put(
            {
                "id": person.id,
                "email": person.email,
                "display_name": person.display_name,
                "first_name": person.first_name,
                "last_name": person.last_name,
                "phone": person.phone_business or person.phone_mobile or "",
                "active": person.active,
                "role": "full" if full else "readonly",
                "office_id": person.office_id,
                "department_id": person.department_id,
                "sub": stable_hash("quickbooks-sub", person.id, length=21),
            }
        )

    # -- company ----------------------------------------------------------------
    company_addr = _addr(org.address)
    _put(
        ctx,
        "CompanyInfo",
        {
            "Id": org.quickbooks_realm_id,
            "SyncToken": "0",
            "CompanyName": org.name,
            "LegalName": org.legal_name,
            "CompanyAddr": company_addr,
            "CustomerCommunicationAddr": _addr(org.address, "2"),
            "LegalAddr": _addr(org.address, "3"),
            "PrimaryPhone": {"FreeFormNumber": org.phone},
            "CompanyStartDate": org.founded.isoformat(),
            "FiscalYearStartMonth": "January",
            "Country": org.address.country,
            "Email": {"Address": f"billing@{org.domain}"},
            "WebAddr": {"URI": org.website},
            "SupportedLanguages": "en",
            "NameValue": [
                {"Name": "NeoEnabled", "Value": "true"},
                {"Name": "IndustryType", "Value": org.industry},
                {"Name": "CompanyType", "Value": "LLP"},
                {"Name": "SubscriptionStatus", "Value": "SUBSCRIBED"},
                {"Name": "OfferingSku", "Value": "QuickBooks Online Advanced"},
                {"Name": "IsQbdtMigrated", "Value": "false"},
            ],
            "MetaData": _meta(org.founded),
        },
    )
    _put(
        ctx,
        "Preferences",
        {
            "Id": "1",
            "SyncToken": "0",
            "AccountingInfoPrefs": {
                "FirstMonthOfFiscalYear": "January",
                "BookCloseDate": "2025-12-31",
                "TaxForm": "Other",
                "AccountNumbersEnabled": True,
                "ClassTrackingPerTxnLine": True,
                "ClassTrackingPerTxn": True,
                "TrackDepartments": True,
                "DepartmentTerminology": "Location",
                "CustomerTerminology": {"Customers": "Customers", "Jobs": "Jobs"},
            },
            "ProductAndServicesPrefs": {
                "TrackQuantitiesOnHand": False,
                "ForSales": True,
                "ForPurchase": False,
            },
            "SalesFormsPrefs": {
                "CustomTxnNumbers": True,
                "AllowDeposit": True,
                "AllowDiscount": True,
                "DefaultTerms": {"value": "2", "name": "Net 30"},
                "AllowEstimates": True,
            },
            "EmailMessagesPrefs": {},
            "VendorAndPurchasesPrefs": {"TrackingByAccount": True, "BillableExpenseTracking": True},
            "TimeTrackingPrefs": {"UseServicesLocation": False, "BillCustomers": True},
            "TaxPrefs": {"UseSalesTax": True},
            "CurrencyPrefs": {"MultiCurrencyEnabled": False, "HomeCurrency": dict(USD)},
            "OtherPrefs": {"NameValue": [{"Name": "SalesFormsPrefs.DefaultItemAccount", "Value": ""}]},
            "MetaData": _meta(org.founded),
        },
    )

    # -- terms / payment methods / departments / classes -------------------------
    term_by_days: dict[int, dict[str, Any]] = {}
    due = _put(
        ctx,
        "Term",
        {
            "Id": _seq(ctx, "Term"),
            "SyncToken": "0",
            "Name": "Due on receipt",
            "Active": True,
            "Type": "STANDARD",
            "DueDays": 0,
            "MetaData": _meta(org.founded),
        },
    )
    term_by_days[0] = due
    days_seen = sorted(
        {c.payment_terms_days for c in world.clients} | {v.payment_terms_days for v in world.vendors}
    )
    for days in days_seen:
        term_by_days[days] = _put(
            ctx,
            "Term",
            {
                "Id": _seq(ctx, "Term"),
                "SyncToken": "0",
                "Name": f"Net {days}",
                "Active": True,
                "Type": "STANDARD",
                "DueDays": days,
                "MetaData": _meta(org.founded),
            },
        )
    maps["term_by_days"] = {str(k): v["Id"] for k, v in term_by_days.items()}

    pm_types = {
        "Cash": "CASH",
        "Check": "CHECK",
        "Credit Card": "CREDIT_CARD",
        "ACH": "NON_CREDIT_CARD",
        "Wire": "NON_CREDIT_CARD",
    }
    payment_methods: dict[str, dict[str, Any]] = {}
    for name, pm_type in pm_types.items():
        payment_methods[name] = _put(
            ctx,
            "PaymentMethod",
            {
                "Id": _seq(ctx, "PaymentMethod"),
                "SyncToken": "0",
                "Name": name,
                "Active": True,
                "Type": pm_type,
                "MetaData": _meta(org.founded),
            },
        )

    dept_by_office: dict[str, dict[str, Any]] = {}
    for office in world.offices:
        city = office.address.city
        dept_by_office[office.id] = _put(
            ctx,
            "Department",
            {
                "Id": _seq(ctx, "Department"),
                "SyncToken": "0",
                "Name": city,
                "FullyQualifiedName": city,
                "Active": True,
                "SubDepartment": False,
                "MetaData": _meta(org.founded),
            },
        )

    class_by_fqn: dict[str, dict[str, Any]] = {}

    def make_class(name: str, parent: dict[str, Any] | None = None) -> dict[str, Any]:
        fqn = f"{parent['FullyQualifiedName']}:{name}" if parent else name
        if fqn in class_by_fqn:
            return class_by_fqn[fqn]
        record: dict[str, Any] = {
            "Id": _seq(ctx, "Class"),
            "SyncToken": "0",
            "Name": name,
            "FullyQualifiedName": fqn,
            "Active": True,
            "SubClass": bool(parent),
            "MetaData": _meta(org.founded),
        }
        if parent:
            record["ParentRef"] = _ref(parent["Id"], parent["FullyQualifiedName"])
        class_by_fqn[fqn] = _put(ctx, "Class", record)
        return class_by_fqn[fqn]

    for top in ("Audit & Assurance", "Tax", "Advisory", "Operations"):
        make_class(top)
    for top, sub in (
        ("Tax", "Tax Compliance"),
        ("Tax", "Tax Advisory"),
        ("Advisory", "Transaction Advisory"),
        ("Advisory", "Systems Advisory"),
        ("Advisory", "Fractional CFO"),
    ):
        make_class(sub, class_by_fqn[top])

    def class_for_service(service: str) -> dict[str, Any]:
        top, sub = SERVICE_CLASS.get(service, ("Operations", None))
        return class_by_fqn[f"{top}:{sub}"] if sub else class_by_fqn[top]

    # -- tax ---------------------------------------------------------------------
    agencies: dict[str, dict[str, Any]] = {}
    for name in ("Canada Revenue Agency", "Revenu Québec"):
        agencies[name] = _put(
            ctx,
            "TaxAgency",
            {
                "Id": _seq(ctx, "TaxAgency"),
                "SyncToken": "0",
                "DisplayName": name,
                "Active": True,
                "TaxTrackedOnPurchases": True,
                "TaxTrackedOnSales": True,
                "MetaData": _meta(org.founded),
            },
        )

    def make_tax(name: str, rate_pct: float, agency: str, taxable: bool = True) -> dict[str, Any]:
        rate = _put(
            ctx,
            "TaxRate",
            {
                "Id": _seq(ctx, "TaxRate"),
                "SyncToken": "0",
                "Name": name,
                "Description": f"{name} at {rate_pct}%",
                "RateValue": rate_pct,
                "AgencyRef": _ref(agencies[agency]["Id"], agency),
                "SpecialTaxType": "NONE",
                "Active": True,
                "EffectiveTaxRate": [{"RateValue": rate_pct, "EffectiveDate": "2015-01-01"}],
                "MetaData": _meta(org.founded),
            },
        )
        detail = {
            "TaxRateRef": _ref(rate["Id"], name),
            "TaxTypeApplicable": "TaxOnAmount",
            "TaxOrder": 0,
            "TaxOnTaxOrder": 0,
        }
        return _put(
            ctx,
            "TaxCode",
            {
                "Id": _seq(ctx, "TaxCode"),
                "SyncToken": "0",
                "Name": name,
                "Description": f"{name} {rate_pct}%" if taxable else f"{name} (0% / exempt)",
                "Active": True,
                "Taxable": taxable,
                "TaxGroup": False,
                "SalesTaxRateList": {"TaxRateDetail": [dict(detail)]},
                "PurchaseTaxRateList": {"TaxRateDetail": [dict(detail)]},
                "MetaData": _meta(org.founded),
                "_rate": rate_pct,
            },
        )

    taxcode_by_rate: dict[float, dict[str, Any]] = {
        0.13: make_tax("HST ON", 13.0, "Canada Revenue Agency"),
        0.15: make_tax("HST ATL", 15.0, "Canada Revenue Agency"),
        0.14975: make_tax("GST/QST", 14.975, "Revenu Québec"),
        0.05: make_tax("GST", 5.0, "Canada Revenue Agency"),
        0.0: make_tax("Zero-rated", 0.0, "Canada Revenue Agency", taxable=False),
    }
    maps["taxcode_by_rate"] = {str(k): v["Id"] for k, v in taxcode_by_rate.items()}

    # -- chart of accounts ---------------------------------------------------------
    account_by_world: dict[str, dict[str, Any]] = {}
    retained: dict[str, Any] | None = None
    opening_total = 0.0
    for account in world.accounts:
        debit_opening = account.opening_balance
        if account.classification in ("Liability", "Equity", "Revenue"):
            debit_opening = -account.opening_balance
        record = _put(
            ctx,
            "Account",
            {
                "Id": _seq(ctx, "Account"),
                "SyncToken": "0",
                "Name": account.name,
                "FullyQualifiedName": account.name,
                "AcctNum": account.number,
                "AccountType": account.type,
                "AccountSubType": account.subtype,
                "Classification": account.classification,
                "Description": account.description,
                "Active": True,
                "SubAccount": False,
                "CurrencyRef": dict(USD),
                "CurrentBalance": 0.0,
                "CurrentBalanceWithSubAccounts": 0.0,
                "MetaData": _meta(date(2025, 1, 1)),
                "_opening": money(debit_opening),
                "_world_id": account.id,
            },
        )
        account_by_world[account.id] = record
        if account.subtype == "RetainedEarnings":
            retained = record
        else:
            opening_total = money(opening_total + debit_opening)
    undeposited = _put(
        ctx,
        "Account",
        {
            "Id": _seq(ctx, "Account"),
            "SyncToken": "0",
            "Name": "Undeposited Funds",
            "FullyQualifiedName": "Undeposited Funds",
            "AcctNum": "1150",
            "AccountType": "Other Current Asset",
            "AccountSubType": "UndepositedFunds",
            "Classification": "Asset",
            "Description": "Payments received but not yet deposited (QBO system account).",
            "Active": True,
            "SubAccount": False,
            "CurrencyRef": dict(USD),
            "CurrentBalance": 0.0,
            "CurrentBalanceWithSubAccounts": 0.0,
            "MetaData": _meta(date(2025, 1, 1)),
            "_opening": 0.0,
            "_world_id": "",
        },
    )
    assert retained is not None
    retained["_opening"] = money(-opening_total)  # plug so opening trial balance balances
    maps["account_by_world"] = {k: v["Id"] for k, v in account_by_world.items()}

    def account_ref(world_id: str) -> dict[str, Any]:
        rec = account_by_world[world_id]
        return _ref(rec["Id"], rec["Name"])

    by_subtype = {r.get("AccountSubType"): r for r in account_by_world.values()}
    by_type = {r.get("AccountType"): r for r in account_by_world.values()}
    ctx.store.meta["system_accounts"] = {
        "ar": by_subtype["AccountsReceivable"]["Id"],
        "ap": by_subtype["AccountsPayable"]["Id"],
        "tax": by_subtype["SalesTaxPayable"]["Id"],
        "undeposited": undeposited["Id"],
        "bank": account_by_world["a1000"]["Id"],
        "credit_card": by_type["Credit Card"]["Id"],
    }

    # -- items / employees / vendors ------------------------------------------------
    item_by_world: dict[str, dict[str, Any]] = {}
    item_by_name: dict[str, dict[str, Any]] = {}
    for item in world.items:
        record = _put(
            ctx,
            "Item",
            {
                "Id": _seq(ctx, "Item"),
                "SyncToken": "0",
                "Name": item.name,
                "FullyQualifiedName": item.name,
                "Sku": item.sku,
                "Description": item.description,
                "Type": item.type,
                "UnitPrice": item.rate,
                "IncomeAccountRef": account_ref(item.income_account_id),
                "PurchaseCost": 0.0,
                "Taxable": item.type == "Service",
                "Active": True,
                "MetaData": _meta(date(2025, 1, 1)),
                "_world_id": item.id,
            },
        )
        item_by_world[item.id] = record
        item_by_name[item.name] = record
    maps["item_by_world"] = {k: v["Id"] for k, v in item_by_world.items()}

    employee_by_world: dict[str, dict[str, Any]] = {}
    for person in world.employees:
        office = world.office(person.office_id) if person.office_id else None
        record = _put(
            ctx,
            "Employee",
            {
                "Id": _seq(ctx, "Employee"),
                "SyncToken": "0",
                "GivenName": person.first_name,
                "FamilyName": person.last_name,
                "DisplayName": person.display_name,
                "PrintOnCheckName": person.display_name,
                "PrimaryEmailAddr": {"Address": person.email},
                "PrimaryPhone": {"FreeFormNumber": person.phone_business or ""},
                "Mobile": {"FreeFormNumber": person.phone_mobile or ""},
                "EmployeeNumber": person.employee_id or "",
                "SSN": "XXX-XX-" + stable_hash("ssn", person.id, length=4),
                "HiredDate": person.hire_date.isoformat() if person.hire_date else None,
                "BillableTime": person.department_id in ("audit", "tax", "advisory", "exec"),
                "BillRate": _bill_rate(ctx, person),
                "CostRate": money(_bill_rate(ctx, person) * 0.4),
                "Organization": False,
                "Active": person.active,
                **({"PrimaryAddr": _addr(office.address)} if office else {}),
                "MetaData": _meta(person.hire_date or date(2020, 1, 1)),
                "_world_id": person.id,
            },
        )
        employee_by_world[person.id] = record
    maps["employee_by_world"] = {k: v["Id"] for k, v in employee_by_world.items()}

    vendor_by_world: dict[str, dict[str, Any]] = {}
    for vendor in world.vendors:
        digits = "".join(ch for ch in vendor.tax_id if ch.isdigit())
        term = term_by_days.get(vendor.payment_terms_days, term_by_days[30])
        record = _put(
            ctx,
            "Vendor",
            {
                "Id": _seq(ctx, "Vendor"),
                "SyncToken": "0",
                "DisplayName": vendor.name,
                "CompanyName": vendor.name,
                "PrintOnCheckName": vendor.name,
                "PrimaryEmailAddr": {"Address": vendor.email},
                "PrimaryPhone": {"FreeFormNumber": vendor.phone},
                "BillAddr": _addr(vendor.address),
                "TaxIdentifier": ("*****" + digits[-4:]) if digits else "",
                "Vendor1099": vendor.category == "Contract Labor",
                "Balance": 0.0,
                "TermRef": _ref(term["Id"], term["Name"]),
                "AcctNum": vendor.id.upper(),
                "CurrencyRef": dict(USD),
                "Active": True,
                "MetaData": _meta(date(2024, 1, 1)),
                "_world_id": vendor.id,
            },
        )
        vendor_by_world[vendor.id] = record
    maps["vendor_by_world"] = {k: v["Id"] for k, v in vendor_by_world.items()}

    # -- customers and jobs -----------------------------------------------------------
    customer_by_client: dict[str, dict[str, Any]] = {}
    for client in world.clients:
        contact = world.person(client.primary_contact_id)
        term = term_by_days.get(client.payment_terms_days, term_by_days[30])
        record = _put(
            ctx,
            "Customer",
            {
                "Id": _seq(ctx, "Customer"),
                "SyncToken": "0",
                "DisplayName": client.name,
                "CompanyName": client.name,
                "FullyQualifiedName": client.name,
                "PrintOnCheckName": client.name,
                "GivenName": contact.first_name,
                "FamilyName": contact.last_name,
                "PrimaryEmailAddr": {"Address": contact.email},
                "PrimaryPhone": {"FreeFormNumber": client.phone},
                "Mobile": {"FreeFormNumber": contact.phone_mobile or ""},
                "BillAddr": _addr(client.address),
                "ShipAddr": _addr(client.address, "2"),
                "WebAddr": {"URI": client.website},
                "Balance": 0.0,
                "BalanceWithJobs": 0.0,
                "Taxable": client.address.country_code == "CA",
                "SalesTermRef": _ref(term["Id"], term["Name"]),
                "PreferredDeliveryMethod": "Email",
                "Notes": client.description,
                "Active": client.status != "Former",
                "Job": False,
                "BillWithParent": False,
                "Level": 0,
                "CurrencyRef": dict(USD),
                "ResaleNum": "",
                "PrimaryTaxIdentifier": client.tax_id,
                "MetaData": _meta(client.client_since),
                "_world_id": client.id,
            },
        )
        customer_by_client[client.id] = record
    maps["customer_by_client"] = {k: v["Id"] for k, v in customer_by_client.items()}

    job_by_engagement: dict[str, dict[str, Any]] = {}
    used_display: set[str] = {r["DisplayName"].lower() for r in customer_by_client.values()}
    for engagement in world.engagements:
        client = world.client(engagement.client_id)
        parent = customer_by_client[client.id]
        segment = engagement.name
        for sep in (" — ", " – ", " - "):
            if sep in segment:
                segment = segment.split(sep, 1)[1]
                break
        if segment.lower().startswith(client.name.lower()):
            segment = segment[len(client.name) :].lstrip(" —–-:")
        display = segment or engagement.name
        if display.lower() in used_display:
            display = f"{display} ({engagement.id.upper()})"
        used_display.add(display.lower())
        term = term_by_days.get(client.payment_terms_days, term_by_days[30])
        record = _put(
            ctx,
            "Customer",
            {
                "Id": _seq(ctx, "Customer"),
                "SyncToken": "0",
                "DisplayName": display,
                "CompanyName": client.name,
                "FullyQualifiedName": f"{client.name}:{display}",
                "PrintOnCheckName": client.name,
                "PrimaryEmailAddr": dict(parent["PrimaryEmailAddr"]),
                "PrimaryPhone": dict(parent["PrimaryPhone"]),
                "BillAddr": dict(parent["BillAddr"]),
                "ShipAddr": dict(parent["ShipAddr"]),
                "Balance": 0.0,
                "BalanceWithJobs": 0.0,
                "Taxable": parent["Taxable"],
                "SalesTermRef": _ref(term["Id"], term["Name"]),
                "PreferredDeliveryMethod": "Email",
                "Notes": f"{engagement.service} engagement ({engagement.status}) led by "
                f"{world.person(engagement.lead_id).display_name}.",
                "Job": True,
                "JobInfo": {
                    "Job": True,
                    "JobDesc": engagement.name,
                    "JobStatus": "Closed" if engagement.status == "Completed" else "InProgress",
                    "StartDate": engagement.start.isoformat(),
                    "ProjectedEndDate": engagement.end.isoformat(),
                    "EndDate": engagement.end.isoformat() if engagement.status == "Completed" else None,
                },
                "BillWithParent": False,
                "ParentRef": _ref(parent["Id"], client.name),
                "Level": 1,
                "CurrencyRef": dict(USD),
                "Active": client.status != "Former",
                "MetaData": _meta(engagement.start),
                "_world_id": engagement.id,
            },
        )
        job_by_engagement[engagement.id] = record
    maps["job_by_engagement"] = {k: v["Id"] for k, v in job_by_engagement.items()}

    _seed_transactions(ctx, locals())
    refresh_computed(ctx)


def _seed_transactions(ctx: ProviderContext, env: dict[str, Any]) -> None:
    world: World = env["world"]
    anchor: date = env["anchor"]
    account_by_world = env["account_by_world"]
    item_by_world = env["item_by_world"]
    item_by_name = env["item_by_name"]
    vendor_by_world = env["vendor_by_world"]
    employee_by_world = env["employee_by_world"]
    customer_by_client = env["customer_by_client"]
    job_by_engagement = env["job_by_engagement"]
    taxcode_by_rate = env["taxcode_by_rate"]
    term_by_days = env["term_by_days"]
    payment_methods = env["payment_methods"]
    dept_by_office = env["dept_by_office"]
    class_for_service = env["class_for_service"]
    maps = env["maps"]
    system = ctx.store.meta["system_accounts"]
    operations_class = next(
        r for r in ctx.store.collection("Class") if r["FullyQualifiedName"] == "Operations"
    )

    def cust_ref_for(invoice_like: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if getattr(invoice_like, "engagement_id", None) and invoice_like.engagement_id in job_by_engagement:
            rec = job_by_engagement[invoice_like.engagement_id]
        else:
            rec = customer_by_client[invoice_like.client_id]
        return rec, _ref(rec["Id"], rec["FullyQualifiedName"])

    # invoices
    invoice_by_world: dict[str, dict[str, Any]] = {}
    for invoice in world.invoices:
        client = world.client(invoice.client_id)
        engagement = world.engagement(invoice.engagement_id) if invoice.engagement_id else None
        cust_rec, cust_ref = cust_ref_for(invoice)
        taxcode = taxcode_by_rate.get(invoice.tax_rate, taxcode_by_rate[0.0])
        voided = invoice.status == "Void"
        lines: list[dict[str, Any]] = []
        for i, line in enumerate(invoice.lines, start=1):
            item = item_by_world[line.item_id]
            detail: dict[str, Any] = {
                "ItemRef": _ref(item["Id"], item["Name"]),
                "Qty": line.quantity,
                "UnitPrice": line.rate,
                "TaxCodeRef": _ref(taxcode["Id"], taxcode["Name"]),
                "ServiceDate": invoice.issue_date.isoformat(),
            }
            if engagement is not None:
                cls = class_for_service(engagement.service)
                detail["ClassRef"] = _ref(cls["Id"], cls["FullyQualifiedName"])
            lines.append(
                {
                    "Id": str(i),
                    "LineNum": i,
                    "Description": line.description,
                    "Amount": 0.0 if voided else line.amount,
                    "DetailType": "SalesItemLineDetail",
                    "SalesItemLineDetail": detail,
                }
            )
        lines.append(
            {
                "Amount": 0.0 if voided else invoice.subtotal,
                "DetailType": "SubTotalLineDetail",
                "SubTotalLineDetail": {},
            }
        )
        tax_lines: list[dict[str, Any]] = []
        if invoice.tax and not voided:
            rate_rec = (taxcode["SalesTaxRateList"]["TaxRateDetail"][0])["TaxRateRef"]
            tax_lines.append(
                {
                    "Amount": invoice.tax,
                    "DetailType": "TaxLineDetail",
                    "TaxLineDetail": {
                        "TaxRateRef": dict(rate_rec),
                        "PercentBased": True,
                        "TaxPercent": taxcode["_rate"],
                        "NetAmountTaxable": invoice.subtotal,
                    },
                }
            )
        term = term_by_days.get(client.payment_terms_days, term_by_days[30])
        record: dict[str, Any] = {
            "Id": _seq(ctx, "Invoice"),
            "SyncToken": "0",
            "DocNumber": invoice.number,
            "TxnDate": invoice.issue_date.isoformat(),
            "DueDate": invoice.due_date.isoformat(),
            "CustomerRef": cust_ref,
            "Line": lines,
            "TxnTaxDetail": {"TotalTax": 0.0 if voided else invoice.tax, "TaxLine": tax_lines},
            "TotalAmt": 0.0 if voided else invoice.total,
            "Balance": 0.0 if voided else invoice.balance,
            "Deposit": 0.0,
            "EmailStatus": "NotSet" if invoice.status == "Draft" or voided else "EmailSent",
            "PrintStatus": "NotSet",
            "BillEmail": dict(cust_rec["PrimaryEmailAddr"]),
            "BillAddr": dict(cust_rec["BillAddr"]),
            "SalesTermRef": _ref(term["Id"], term["Name"]),
            "CustomerMemo": {"value": invoice.memo},
            "AllowOnlineCreditCardPayment": client.pays_by_card,
            "AllowOnlineACHPayment": True,
            "AllowIPNPayment": False,
            "GlobalTaxCalculation": "TaxExcluded",
            "ExchangeRate": 1,
            "CurrencyRef": dict(USD),
            "LinkedTxn": [],
            "MetaData": _meta(invoice.issue_date),
            "_world_id": invoice.id,
            "_world_status": invoice.status,
        }
        if engagement is not None:
            lead = world.person(engagement.lead_id)
            if lead.office_id and lead.office_id in dept_by_office:
                dept = dept_by_office[lead.office_id]
                record["DepartmentRef"] = _ref(dept["Id"], dept["Name"])
        if voided:
            record["_voided"] = True
            record["PrivateNote"] = "Voided"
            record["status"] = "Voided"
        invoice_by_world[invoice.id] = _put(ctx, "Invoice", record)
    maps["invoice_by_world"] = {k: v["Id"] for k, v in invoice_by_world.items()}

    # payments
    for payment in world.payments:
        invoice_rec = invoice_by_world[payment.invoice_id]
        world_invoice = next(i for i in world.invoices if i.id == payment.invoice_id)
        _, cust_ref = cust_ref_for(world_invoice)
        method = payment_methods[payment.method]
        deposit = account_by_world[payment.deposit_account_id]
        record = _put(
            ctx,
            "Payment",
            {
                "Id": _seq(ctx, "Payment"),
                "SyncToken": "0",
                "CustomerRef": cust_ref,
                "TotalAmt": payment.amount,
                "UnappliedAmt": 0.0,
                "TxnDate": payment.date.isoformat(),
                "PaymentMethodRef": _ref(method["Id"], method["Name"]),
                "PaymentRefNum": payment.reference,
                "DepositToAccountRef": _ref(deposit["Id"], deposit["Name"]),
                "CurrencyRef": dict(USD),
                "ExchangeRate": 1,
                "Line": [
                    {
                        "Amount": payment.amount,
                        "LinkedTxn": [{"TxnId": invoice_rec["Id"], "TxnType": "Invoice"}],
                        "LineEx": [],
                    }
                ],
                "MetaData": _meta(payment.date),
                "_world_id": payment.id,
            },
        )
        invoice_rec["LinkedTxn"].append({"TxnId": record["Id"], "TxnType": "Payment"})

    # bills + bill payments
    bill_by_world: dict[str, dict[str, Any]] = {}
    for bill in world.bills:
        vendor = world.vendor(bill.vendor_id)
        vendor_rec = vendor_by_world[vendor.id]
        term = term_by_days.get(vendor.payment_terms_days, term_by_days[30])
        lines = []
        for i, line in enumerate(bill.lines, start=1):
            acct = account_by_world[line.account_id]
            lines.append(
                {
                    "Id": str(i),
                    "LineNum": i,
                    "Description": line.description,
                    "Amount": line.amount,
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": _ref(acct["Id"], acct["Name"]),
                        "BillableStatus": "NotBillable",
                        "TaxCodeRef": _ref(taxcode_by_rate[0.0]["Id"], taxcode_by_rate[0.0]["Name"]),
                        "ClassRef": _ref(operations_class["Id"], operations_class["FullyQualifiedName"]),
                    },
                }
            )
        ap_rec = ctx.store.collection("Account").get(system["ap"])
        record = _put(
            ctx,
            "Bill",
            {
                "Id": _seq(ctx, "Bill"),
                "SyncToken": "0",
                "DocNumber": bill.number,
                "VendorRef": _ref(vendor_rec["Id"], vendor_rec["DisplayName"]),
                "TxnDate": bill.bill_date.isoformat(),
                "DueDate": bill.due_date.isoformat(),
                "TotalAmt": bill.total,
                "Balance": 0.0 if bill.status == "Paid" else bill.total,
                "Line": lines,
                "APAccountRef": _ref(ap_rec["Id"], ap_rec["Name"]) if ap_rec else {"value": system["ap"]},
                "SalesTermRef": _ref(term["Id"], term["Name"]),
                "PrivateNote": bill.memo,
                "CurrencyRef": dict(USD),
                "LinkedTxn": [],
                "MetaData": _meta(bill.bill_date),
                "_world_id": bill.id,
                "_world_status": bill.status,
            },
        )
        bill_by_world[bill.id] = record
    check_no = 1000
    bank = account_by_world["a1000"]
    for bill in world.bills:
        if bill.status != "Paid" or bill.paid_date is None:
            continue
        check_no += 1
        bill_rec = bill_by_world[bill.id]
        vendor_rec = vendor_by_world[bill.vendor_id]
        record = _put(
            ctx,
            "BillPayment",
            {
                "Id": _seq(ctx, "BillPayment"),
                "SyncToken": "0",
                "DocNumber": str(check_no),
                "VendorRef": _ref(vendor_rec["Id"], vendor_rec["DisplayName"]),
                "PayType": "Check",
                "CheckPayment": {
                    "BankAccountRef": _ref(bank["Id"], bank["Name"]),
                    "PrintStatus": "PrintComplete",
                },
                "TotalAmt": bill.total,
                "TxnDate": bill.paid_date.isoformat(),
                "CurrencyRef": dict(USD),
                "Line": [
                    {
                        "Amount": bill.total,
                        "LinkedTxn": [{"TxnId": bill_rec["Id"], "TxnType": "Bill"}],
                    }
                ],
                "MetaData": _meta(bill.paid_date),
                "_world_id": bill.id,
            },
        )
        bill_rec["LinkedTxn"].append({"TxnId": record["Id"], "TxnType": "BillPayment"})

    # estimates from CRM deals
    est_no = 1000
    for deal in world.deals:
        if deal.stage == "Closed Won":
            status = "Accepted"
            if deal.id not in ("o001", "o002", "o003"):
                continue
        elif deal.stage == "Closed Lost":
            status = "Rejected"
        else:
            status = "Pending"
        est_no += 1
        cust_rec = customer_by_client[deal.client_id]
        item = item_by_name.get("Financial Due Diligence") or next(iter(item_by_name.values()))
        _put(
            ctx,
            "Estimate",
            {
                "Id": _seq(ctx, "Estimate"),
                "SyncToken": "0",
                "DocNumber": f"EST-{est_no}",
                "TxnDate": deal.created.date().isoformat(),
                "ExpirationDate": deal.close_date.isoformat(),
                "DueDate": deal.close_date.isoformat(),
                "CustomerRef": _ref(cust_rec["Id"], cust_rec["FullyQualifiedName"]),
                "TxnStatus": status,
                "EmailStatus": "EmailSent" if status != "Pending" else "NotSet",
                "BillEmail": dict(cust_rec["PrimaryEmailAddr"]),
                "BillAddr": dict(cust_rec["BillAddr"]),
                "Line": [
                    {
                        "Id": "1",
                        "LineNum": 1,
                        "Description": deal.name,
                        "Amount": deal.amount,
                        "DetailType": "SalesItemLineDetail",
                        "SalesItemLineDetail": {
                            "ItemRef": _ref(item["Id"], item["Name"]),
                            "Qty": 1,
                            "UnitPrice": deal.amount,
                            "TaxCodeRef": _ref(taxcode_by_rate[0.0]["Id"], taxcode_by_rate[0.0]["Name"]),
                        },
                    },
                    {"Amount": deal.amount, "DetailType": "SubTotalLineDetail", "SubTotalLineDetail": {}},
                ],
                "TxnTaxDetail": {"TotalTax": 0.0, "TaxLine": []},
                "TotalAmt": deal.amount,
                "CustomerMemo": {"value": f"Proposal: {deal.name}"},
                "PrivateNote": f"CRM deal {deal.id} stage {deal.stage}",
                "CurrencyRef": dict(USD),
                "MetaData": _meta(deal.created.date()),
                "_world_id": deal.id,
            },
        )

    # credit memos (goodwill adjustments on two long-standing clients)
    cm_no = 100
    for client_id, hours in (("c001", 2.0), ("c003", 1.5)):
        client = world.client(client_id)
        cust_rec = customer_by_client[client_id]
        item = item_by_name["Audit & Assurance Services"]
        amount = money(hours * item["UnitPrice"])
        cm_no += 1
        when = anchor - timedelta(days=20 + cm_no)
        _put(
            ctx,
            "CreditMemo",
            {
                "Id": _seq(ctx, "CreditMemo"),
                "SyncToken": "0",
                "DocNumber": f"CM-{cm_no}",
                "TxnDate": when.isoformat(),
                "CustomerRef": _ref(cust_rec["Id"], cust_rec["FullyQualifiedName"]),
                "Line": [
                    {
                        "Id": "1",
                        "LineNum": 1,
                        "Description": f"Goodwill credit for {client.name}",
                        "Amount": amount,
                        "DetailType": "SalesItemLineDetail",
                        "SalesItemLineDetail": {
                            "ItemRef": _ref(item["Id"], item["Name"]),
                            "Qty": hours,
                            "UnitPrice": item["UnitPrice"],
                            "TaxCodeRef": _ref(taxcode_by_rate[0.0]["Id"], taxcode_by_rate[0.0]["Name"]),
                        },
                    },
                    {"Amount": amount, "DetailType": "SubTotalLineDetail", "SubTotalLineDetail": {}},
                ],
                "TxnTaxDetail": {"TotalTax": 0.0, "TaxLine": []},
                "TotalAmt": amount,
                "Balance": amount,
                "RemainingCredit": amount,
                "BillEmail": dict(cust_rec["PrimaryEmailAddr"]),
                "CurrencyRef": dict(USD),
                "MetaData": _meta(when),
                "_world_id": client_id,
            },
        )

    # journals: depreciation + payroll accrual/payment, 12 months to Sep 2026
    accounts_by_name = {r["Name"]: r for r in account_by_world.values()}
    total_revenue = money(sum(i.subtotal for i in world.invoices))
    monthly_payroll = money(total_revenue * 0.55 / 12)
    monthly_depr = money((86000 + 140000 + 310000) / 120)
    monthly_interest = money(400000 * 0.05 / 12)
    je_no = 0
    months: list[date] = []
    cursor = date(2025, 10, 31)
    for _ in range(12):
        months.append(cursor)
        year = cursor.year + (1 if cursor.month == 12 else 0)
        month = 1 if cursor.month == 12 else cursor.month + 1
        following = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
        cursor = following - timedelta(days=1)

    def je_line(idx: int, acct_name: str, posting: str, amount: float, desc: str) -> dict[str, Any]:
        acct = accounts_by_name[acct_name]
        return {
            "Id": str(idx),
            "LineNum": idx,
            "Description": desc,
            "Amount": amount,
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {
                "PostingType": posting,
                "AccountRef": _ref(acct["Id"], acct["Name"]),
                "ClassRef": _ref(operations_class["Id"], operations_class["FullyQualifiedName"]),
            },
        }

    for month_end in months:
        je_no += 1
        _put(
            ctx,
            "JournalEntry",
            {
                "Id": _seq(ctx, "JournalEntry"),
                "SyncToken": "0",
                "DocNumber": f"JE-{je_no}",
                "TxnDate": month_end.isoformat(),
                "Adjustment": True,
                "PrivateNote": f"Month-end accruals {month_end:%Y-%m}",
                "TotalAmt": money(monthly_payroll + monthly_depr + monthly_interest),
                "Line": [
                    je_line(1, "Salaries & Wages", "Debit", monthly_payroll, "Payroll accrual"),
                    je_line(2, "Depreciation Expense", "Debit", monthly_depr, "Monthly depreciation"),
                    je_line(3, "Interest Expense", "Debit", monthly_interest, "Term loan interest"),
                    je_line(4, "Accrued Liabilities", "Credit", monthly_payroll, "Payroll accrual"),
                    je_line(5, "Accumulated Depreciation", "Credit", monthly_depr, "Monthly depreciation"),
                    je_line(
                        6,
                        "Operating Account - Northbridge Bank",
                        "Credit",
                        monthly_interest,
                        "Term loan interest paid",
                    ),
                ],
                "CurrencyRef": dict(USD),
                "MetaData": _meta(month_end),
            },
        )
        je_no += 1
        pay_day = month_end + timedelta(days=1)
        if pay_day > anchor:
            pay_day = month_end
        _put(
            ctx,
            "JournalEntry",
            {
                "Id": _seq(ctx, "JournalEntry"),
                "SyncToken": "0",
                "DocNumber": f"JE-{je_no}",
                "TxnDate": pay_day.isoformat(),
                "Adjustment": False,
                "PrivateNote": f"Payroll payment {month_end:%Y-%m}",
                "TotalAmt": monthly_payroll,
                "Line": [
                    je_line(1, "Accrued Liabilities", "Debit", monthly_payroll, "Payroll payment"),
                    je_line(
                        2,
                        "Operating Account - Northbridge Bank",
                        "Credit",
                        monthly_payroll,
                        "Payroll payment",
                    ),
                ],
                "CurrencyRef": dict(USD),
                "MetaData": _meta(pay_day),
            },
        )

    # corporate-card purchases by partners (last 6 months)
    card = accounts_by_name["Corporate Card - Northbridge Visa"]
    meals = accounts_by_name["Meals & Entertainment"]
    travel = accounts_by_name["Travel"]
    partners = [p for p in world.active_employees if p.is_partner]
    for month_offset in range(6):
        year = anchor.year
        month = anchor.month - month_offset
        while month < 1:
            month += 12
            year -= 1
        when = date(year, month, 15)
        if when > anchor:
            when = anchor - timedelta(days=month_offset * 7 + 1)
        for partner in partners:
            digest = stable_hash("purchase", partner.id, when.isoformat(), length=4)
            amount = money(60 + (int(digest, 16) % 440) + (int(digest, 16) % 77) / 100)
            expense = meals if int(digest, 16) % 2 == 0 else travel
            emp_rec = employee_by_world[partner.id]
            _put(
                ctx,
                "Purchase",
                {
                    "Id": _seq(ctx, "Purchase"),
                    "SyncToken": "0",
                    "PaymentType": "CreditCard",
                    "Credit": False,
                    "AccountRef": _ref(card["Id"], card["Name"]),
                    "EntityRef": {"value": emp_rec["Id"], "name": partner.display_name, "type": "Employee"},
                    "TxnDate": when.isoformat(),
                    "TotalAmt": amount,
                    "PrintStatus": "NotSet",
                    "Line": [
                        {
                            "Id": "1",
                            "LineNum": 1,
                            "Description": f"{expense['Name']} - {partner.display_name} {when:%Y-%m}",
                            "Amount": amount,
                            "DetailType": "AccountBasedExpenseLineDetail",
                            "AccountBasedExpenseLineDetail": {
                                "AccountRef": _ref(expense["Id"], expense["Name"]),
                                "BillableStatus": "NotBillable",
                                "ClassRef": _ref(
                                    operations_class["Id"], operations_class["FullyQualifiedName"]
                                ),
                            },
                        }
                    ],
                    "CurrencyRef": dict(USD),
                    "MetaData": _meta(when),
                    "_world_id": partner.id,
                },
            )

    # time activities: weekly for in-progress engagement teams, last 12 weeks
    monday = anchor - timedelta(days=anchor.weekday())
    weeks = [monday - timedelta(weeks=i) for i in range(11, -1, -1)]
    for engagement in world.engagements:
        if engagement.status != "In Progress":
            continue
        job = job_by_engagement[engagement.id]
        item = item_by_name.get(engagement.service_item) or next(iter(item_by_name.values()))
        cls = env["class_for_service"](engagement.service)
        lead = world.person(engagement.lead_id)
        dept = dept_by_office.get(lead.office_id or "")
        members = (engagement.lead_id, *engagement.team_ids)
        for week in weeks:
            if week < engagement.start or week > engagement.end:
                continue
            for member_id in members:
                person = world.person(member_id)
                if not person.active:
                    continue
                digest = stable_hash("time", engagement.id, member_id, week.isoformat(), length=6)
                hours = 3 + (int(digest, 16) % 10)
                minutes = (int(digest, 16) % 4) * 15
                age_days = (anchor - week).days
                emp_rec = employee_by_world[member_id]
                _put(
                    ctx,
                    "TimeActivity",
                    {
                        "Id": _seq(ctx, "TimeActivity"),
                        "SyncToken": "0",
                        "NameOf": "Employee",
                        "EmployeeRef": _ref(emp_rec["Id"], emp_rec["DisplayName"]),
                        "CustomerRef": _ref(job["Id"], job["FullyQualifiedName"]),
                        "ItemRef": _ref(item["Id"], item["Name"]),
                        "ClassRef": _ref(cls["Id"], cls["FullyQualifiedName"]),
                        **({"DepartmentRef": _ref(dept["Id"], dept["Name"])} if dept else {}),
                        "BillableStatus": "HasBeenBilled" if age_days > 28 else "Billable",
                        "Taxable": False,
                        "HourlyRate": engagement.hourly_rate,
                        "CostRate": emp_rec["CostRate"],
                        "Hours": hours,
                        "Minutes": minutes,
                        "BreakHours": 0,
                        "BreakMinutes": 0,
                        "TxnDate": week.isoformat(),
                        "Description": f"{engagement.service} work for {job['FullyQualifiedName']} - week of {week.isoformat()}",
                        "MetaData": _meta(week),
                        "_world_id": engagement.id,
                    },
                )
