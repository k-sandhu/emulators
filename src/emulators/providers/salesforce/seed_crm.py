"""Seed the sales/service objects from the world: accounts, contacts, pricebooks, engagements,
campaigns, opportunities (+ contact roles and line items), leads and cases."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from emulators.providers.salesforce.common import fmt_dt
from emulators.providers.salesforce.hooks import recompute_campaign
from emulators.providers.salesforce.opp_logic import forecast_for, stage_info
from emulators.providers.salesforce.org import Record
from emulators.providers.salesforce.seedlib import (
    SeedState,
    at,
    date_str,
    hnum,
    modified_after,
    stamp,
)
from emulators.world.model import Deal, Lead, Person, World

ACCOUNT_TYPE = {"Active": "Customer", "Prospect": "Prospect", "Former": "Former Customer"}
SIC = {
    "Aerospace Manufacturing": ("3720", "Aircraft & Parts"),
    "Agriculture": ("0100", "Agricultural Production - Crops"),
    "Construction": ("1500", "General Building Contractors"),
    "Craft Beverage": ("2082", "Malt Beverages"),
    "Fashion & Apparel": ("2300", "Apparel & Other Finished Products"),
    "Financial Services": ("6199", "Finance Services"),
    "Food Distribution": ("5140", "Groceries & Related Products"),
    "Freight Brokerage": ("4731", "Freight Transportation Arrangement"),
    "Healthcare": ("8000", "Health Services"),
    "Hospitality": ("7011", "Hotels & Motels"),
    "Industrial Automation": ("3823", "Industrial Instruments"),
    "Interior Design": ("7389", "Business Services"),
    "Legal Services": ("8111", "Legal Services"),
    "Manufacturing": ("3990", "Miscellaneous Manufacturing"),
    "Media & Entertainment": ("7812", "Motion Picture Production"),
    "Outdoor Retail": ("5940", "Sporting Goods Stores"),
    "Real Estate": ("6512", "Operators of Nonresidential Buildings"),
    "Renewable Energy": ("4911", "Electric Services"),
    "Retail Food & Beverage": ("5812", "Eating Places"),
    "Senior Care": ("8051", "Skilled Nursing Care Facilities"),
    "Software / SaaS": ("7372", "Prepackaged Software"),
    "Transportation & Logistics": ("4731", "Freight Transportation Arrangement"),
    "Veterinary Healthcare": ("0742", "Veterinary Services"),
}
PLATFORM = {"teams": "Microsoft Teams", "zoom": "Zoom", "meet": "Google Meet"}
SERVICE_LINE = {
    "Audit": "Audit & Assurance", "Review Engagement": "Audit & Assurance", "Tax Compliance": "Tax",
    "Tax Advisory": "Tax", "SR&ED Claim": "Tax", "Transaction Advisory": "Advisory",
    "Systems Advisory": "Advisory", "Fractional CFO": "Advisory", "Bookkeeping": "Accounting",
}  # fmt: skip
VENDOR_INDUSTRY = {
    "Software": "Technology",
    "IT Services": "Technology",
    "Telecom": "Telecommunications",
    "Insurance": "Insurance",
    "Travel": "Hospitality",
    "Meals": "Food & Beverage",
    "Rent": "Other",
    "Payroll": "Consulting",
    "Training": "Education",
    "Research": "Education",
    "Shipping": "Shipping",
}
FAMILY_BY_ACCOUNT = {"a4000": "Audit & Assurance", "a4100": "Tax", "a4200": "Advisory", "a4900": "Expenses"}
OPEN_STATUSES = {"Open - Not Contacted", "Working - Contacted"}


def seed(state: SeedState, world: World) -> None:
    seed_accounts_and_contacts(state, world)
    seed_pricebook(state, world)
    seed_campaigns(state, world)
    seed_engagements(state, world)
    seed_opportunities(state, world)
    seed_leads(state, world)
    seed_cases(state, world)
    seed_campaign_members(state, world)


def _owner(state: SeedState, person_id: str | None) -> str:
    return state.user_by_person.get(person_id or "", state.admin_id)


def _department_for(title: str) -> str | None:
    t = title.lower()
    if any(k in t for k in ("controller", "financial", "finance", "fp&a", "accounting", "receivable")):
        return "Finance"
    if "counsel" in t:
        return "Legal"
    if "operations" in t or "office manager" in t:
        return "Operations"
    if any(k in t for k in ("chief executive", "owner")):
        return "Executive"
    return None


# -- accounts & contacts -------------------------------------------------------------------


def _address(prefix: str, address: Any) -> dict[str, Any]:
    return {
        f"{prefix}Street": address.street,
        f"{prefix}City": address.city,
        f"{prefix}State": address.state,
        f"{prefix}PostalCode": address.postal_code,
        f"{prefix}Country": address.country,
    }


def _service_lines(world: World, client_id: str) -> str | None:
    lines = sorted(
        {SERVICE_LINE.get(e.service, "Advisory") for e in world.engagements if e.client_id == client_id}
    )
    return ";".join(lines) or None


def _first_deal_source(world: World, client_id: str) -> str | None:
    deals = sorted((d for d in world.deals if d.client_id == client_id), key=lambda d: d.created)
    return deals[0].lead_source if deals else None


def seed_accounts_and_contacts(state: SeedState, world: World) -> None:
    anchor = world.anchor
    for n, c in enumerate(world.clients, start=1):
        owner = _owner(state, c.owner_id)
        created = min(at(c.client_since, 9), anchor)
        sic, sic_desc = SIC.get(c.industry, (None, None))
        rec = state.new(
            "Account",
            Name=c.name,
            Type=ACCOUNT_TYPE.get(c.status, "Other"),
            Phone=c.phone,
            AccountNumber=f"AC-{1000 + n}",
            Website=c.website,
            Sic=sic,
            SicDesc=sic_desc,
            Industry=c.industry,
            AnnualRevenue=float(c.annual_revenue),
            NumberOfEmployees=c.employees,
            Ownership="Private",
            Description=c.description,
            Rating=c.rating,
            OwnerId=owner,
            CleanStatus="Pending",
            AccountSource=_first_deal_source(world, c.id),
            Client_Since__c=date_str(c.client_since),
            Payment_Terms__c=f"Net {c.payment_terms_days}",
            Preferred_Meeting_Platform__c=PLATFORM.get(c.preferred_platform),
            QuickBooks_Customer_Id__c=str(int(c.id[1:])) if c.id[1:].isdigit() else None,
            Service_Lines__c=_service_lines(world, c.id),
            **_address("Billing", c.address),
            **_address("Shipping", c.address),
        )
        rec["PhotoUrl"] = f"/services/images/photo/{rec['Id']}"
        stamp(rec, created, modified=modified_after(anchor, created, "acct", c.id), created_by=owner)
        state.save("Account", rec)
        state.account_by_client[c.id] = rec["Id"]
    for v in world.vendors:
        owner = _owner(state, "p028")
        created = anchor - timedelta(days=540 - hnum("vendor", v.id, mod=200))
        rec = state.new(
            "Account",
            Name=v.name,
            Type="Vendor",
            Phone=v.phone,
            Industry=VENDOR_INDUSTRY.get(v.category, "Other"),
            Description=f"Vendor: {v.category}. Expense account: {v.expense_account}.",
            OwnerId=owner,
            CleanStatus="Pending",
            Payment_Terms__c=f"Net {v.payment_terms_days}",
            **_address("Billing", v.address),
            **_address("Shipping", v.address),
        )
        rec["AccountNumber"] = f"VN-{rec['Id'][-6:-3]}"
        rec["PhotoUrl"] = f"/services/images/photo/{rec['Id']}"
        stamp(rec, created, modified=modified_after(anchor, created, "acct", v.id), created_by=owner)
        state.save("Account", rec)
        state.account_by_vendor[v.id] = rec["Id"]
    for person in world.external_people:
        _contact_for(state, world, person)


def _contact_for(state: SeedState, world: World, person: Person) -> None:
    anchor = world.anchor
    client = world.client(person.company_id) if person.company_id in {c.id for c in world.clients} else None
    vendor = None
    if client is None and person.company_id:
        vendor = next((v for v in world.vendors if v.id == person.company_id), None)
    if client is not None:
        account_id = state.account_by_client[client.id]
        owner = _owner(state, client.owner_id)
        address = client.address
        created = min(at(client.client_since, 10), anchor) + timedelta(days=hnum("c", person.id, mod=30))
        primary = client.primary_contact_id
        reports_to = state.contact_by_person.get(primary) if person.id != primary else None
        source = _first_deal_source(world, client.id) if person.id == primary else None
    elif vendor is not None:
        account_id = state.account_by_vendor[vendor.id]
        owner = _owner(state, "p028")
        address = vendor.address
        created = anchor - timedelta(days=500 - hnum("vc", person.id, mod=100))
        reports_to, source = None, None
    else:
        return
    created = min(created, anchor)
    rec = state.new(
        "Contact",
        AccountId=account_id,
        LastName=person.last_name,
        FirstName=person.first_name,
        Name=f"{person.first_name} {person.last_name}".strip(),
        Phone=person.phone_business,
        MobilePhone=person.phone_mobile,
        ReportsToId=reports_to,
        Email=person.email,
        Title=person.job_title,
        Department=_department_for(person.job_title),
        LeadSource=source,
        OwnerId=owner,
        CleanStatus="Pending",
        **_address("Mailing", address),
    )
    rec["PhotoUrl"] = f"/services/images/photo/{rec['Id']}"
    stamp(rec, created, modified=modified_after(anchor, created, "contact", person.id), created_by=owner)
    state.save("Contact", rec)
    state.contact_by_person[person.id] = rec["Id"]


# -- products ------------------------------------------------------------------------------


def seed_pricebook(state: SeedState, world: World) -> None:
    anchor = world.anchor
    created = at(world.org.founded.replace(year=world.org.founded.year + 8), 9)
    book = state.new(
        "Pricebook2",
        Name="Standard Price Book",
        IsActive=True,
        IsStandard=True,
        Description="Standard price book",
    )
    stamp(book, created, created_by=state.admin_id)
    state.save("Pricebook2", book)
    state.pricebook_id = book["Id"]
    for item in world.items:
        family = FAMILY_BY_ACCOUNT.get(item.income_account_id, "Advisory")
        if item.sku == "SVC-009":
            family = "Accounting"
        product = state.new(
            "Product2",
            Name=item.name,
            ProductCode=item.sku,
            Description=item.description,
            IsActive=True,
            Family=family,
            StockKeepingUnit=item.sku,
            QuantityUnitOfMeasure="Hour" if item.type == "Service" else None,
        )
        stamp(
            product,
            created,
            modified=modified_after(anchor, created, "prod", item.id),
            created_by=state.admin_id,
        )
        state.save("Product2", product)
        state.product_by_item[item.id] = product["Id"]
        entry = state.new(
            "PricebookEntry",
            Name=item.name,
            Pricebook2Id=book["Id"],
            Product2Id=product["Id"],
            UnitPrice=float(item.rate),
            IsActive=True,
            UseStandardPrice=False,
            ProductCode=item.sku,
        )
        stamp(entry, created, created_by=state.admin_id)
        state.save("PricebookEntry", entry)
        state.pbe_by_item[item.id] = entry["Id"]


# -- campaigns -----------------------------------------------------------------------------


def seed_campaigns(state: SeedState, world: World) -> None:
    anchor = world.anchor.date()
    owner = _owner(state, "p041") if "p041" in state.user_by_person else state.admin_id
    owner2 = _owner(state, "p044") if "p044" in state.user_by_person else owner
    specs: list[dict[str, Any]] = [
        dict(
            key="year_end",
            Name="Year-End Tax Planning Webinar 2026",
            Type="Webinar",
            Status="In Progress",
            StartDate=anchor - timedelta(days=15),
            EndDate=anchor + timedelta(days=60),
            ExpectedRevenue=150000.0,
            BudgetedCost=4500.0,
            ActualCost=1800.0,
            ExpectedResponse=5.0,
            NumberSent=1200.0,
            OwnerId=owner,
            Description="Webinar series on year-end tax planning for owner-managed businesses.",
        ),
        dict(
            key="audit_roundtable",
            Name="Fall Audit Readiness Roundtable 2026",
            Type="Conference",
            Status="Completed",
            StartDate=anchor - timedelta(days=31),
            EndDate=anchor - timedelta(days=31),
            ExpectedRevenue=90000.0,
            BudgetedCost=7500.0,
            ActualCost=7120.0,
            ExpectedResponse=8.0,
            NumberSent=400.0,
            OwnerId=owner,
            Description="Invitation-only roundtable for finance leaders preparing for year-end audits.",
        ),
        dict(
            key="sred",
            Name="SR&ED Credit Workshop 2026",
            Type="Webinar",
            Status="Planned",
            StartDate=anchor + timedelta(days=33),
            EndDate=anchor + timedelta(days=33),
            ExpectedRevenue=60000.0,
            BudgetedCost=3000.0,
            ActualCost=0.0,
            ExpectedResponse=4.0,
            NumberSent=0.0,
            OwnerId=owner2,
            Description="Workshop on maximising SR&ED credits for technology companies.",
        ),
    ]
    for spec in specs:
        key = spec.pop("key")
        created = at(min(spec["StartDate"] - timedelta(days=30), anchor - timedelta(days=7)), 9)
        rec = state.new(
            "Campaign",
            IsActive=True,
            **{k: (d.isoformat() if isinstance(d, date) else d) for k, d in spec.items()},
        )
        stamp(
            rec,
            created,
            modified=modified_after(world.anchor, created, "camp", key),
            created_by=spec["OwnerId"],
        )
        state.save("Campaign", rec)
        state.campaign_ids[key] = rec["Id"]


# -- engagements -----------------------------------------------------------------------------


def seed_engagements(state: SeedState, world: World) -> None:
    anchor = world.anchor
    for e in world.engagements:
        owner = _owner(state, e.lead_id)
        created = min(at(e.start, 9) - timedelta(days=21), anchor)
        rec = state.new(
            "Engagement__c",
            Name=e.name,
            OwnerId=owner,
            Account__c=state.account_by_client.get(e.client_id),
            Service__c=e.service,
            Status__c=e.status,
            Start_Date__c=date_str(e.start),
            End_Date__c=date_str(e.end),
            Budget_Hours__c=float(e.budget_hours),
            Hourly_Rate__c=float(e.hourly_rate),
            Fixed_Fee__c=float(e.fixed_fee) if e.fixed_fee is not None else None,
            Billing_Type__c=e.billing,
            Lead__c=owner,
            Engagement_Code__c=e.id,
        )
        stamp(rec, created, modified=modified_after(anchor, created, "eng", e.id), created_by=owner)
        state.save("Engagement__c", rec)
        state.engagement_by_world[e.id] = rec["Id"]


# -- opportunities ---------------------------------------------------------------------------


def _fiscal(close: date, start_month: int) -> tuple[int, int]:
    shifted = (close.month - start_month) % 12
    quarter = shifted // 3 + 1
    year = close.year if start_month == 1 or close.month < start_month else close.year + 1
    return year, quarter


def seed_opportunities(state: SeedState, world: World) -> None:
    anchor = world.anchor
    items_by_name = {i.name: i for i in world.items}
    engagements = {e.id: e for e in world.engagements}
    webinar_campaign = state.campaign_ids.get("year_end")
    for d in world.deals:
        owner = _owner(state, d.owner_id)
        info = stage_info(d.stage)
        created = min(d.created, anchor)
        fy, fq = _fiscal(d.close_date, world.org.fiscal_year_start_month)
        engagement = engagements.get(d.engagement_id or "")
        item = items_by_name.get(engagement.service_item) if engagement else None
        has_line = bool(d.is_won and item is not None)
        category, category_name = forecast_for(d.stage)
        rec = state.new(
            "Opportunity",
            AccountId=state.account_by_client.get(d.client_id),
            Name=d.name,
            StageName=d.stage,
            Amount=float(d.amount),
            Probability=float(d.probability),
            ExpectedRevenue=round(d.amount * d.probability / 100, 2),
            CloseDate=d.close_date.isoformat(),
            Type=d.type,
            NextStep=d.next_step,
            LeadSource=d.lead_source,
            IsClosed=bool(info["closed"]),
            IsWon=bool(info["won"]),
            ForecastCategory=category,
            ForecastCategoryName=category_name,
            CampaignId=webinar_campaign if d.lead_source == "Webinar" else None,
            HasOpportunityLineItem=has_line,
            Pricebook2Id=state.pricebook_id if has_line else None,
            OwnerId=owner,
            FiscalQuarter=fq,
            FiscalYear=fy,
            Fiscal=f"{fy} {fq}",
            Engagement__c=state.engagement_by_world.get(d.engagement_id or ""),
        )
        modified = (
            at(d.close_date, 17)
            if d.is_closed and at(d.close_date, 17) <= anchor
            else modified_after(anchor, created, "opp", d.id)
        )
        stamp(rec, created, modified=min(modified, anchor), created_by=owner)
        rec["LastStageChangeDate"] = fmt_dt(min(modified, anchor))
        state.save("Opportunity", rec)
        state.opp_by_deal[d.id] = rec["Id"]
        _contact_roles(state, world, d, rec, created)
        if has_line and item is not None:
            _line_item(state, d, rec, item, created)


def _contact_roles(state: SeedState, world: World, d: Deal, opp: Record, created: datetime) -> None:
    client = world.client(d.client_id)
    chosen = [(client.primary_contact_id, "Decision Maker", True)]
    others = [c for c in client.contact_ids if c != client.primary_contact_id]
    if others and d.amount >= 100000:
        chosen.append((others[0], "Influencer", False))
    for person_id, role, primary in chosen:
        contact_id = state.contact_by_person.get(person_id)
        if not contact_id:
            continue
        rec = state.new(
            "OpportunityContactRole",
            OpportunityId=opp["Id"],
            ContactId=contact_id,
            Role=role,
            IsPrimary=primary,
        )
        stamp(rec, created, created_by=opp["OwnerId"])
        state.save("OpportunityContactRole", rec)


def _line_item(state: SeedState, d: Deal, opp: Record, item: Any, created: datetime) -> None:
    rec = state.new(
        "OpportunityLineItem",
        OpportunityId=opp["Id"],
        SortOrder=0,
        PricebookEntryId=state.pbe_by_item[item.id],
        Product2Id=state.product_by_item[item.id],
        ProductCode=item.sku,
        Name=item.name,
        Quantity=1.0,
        TotalPrice=float(d.amount),
        UnitPrice=float(d.amount),
        ListPrice=float(item.rate),
        Description=item.description,
    )
    stamp(rec, created, created_by=opp["OwnerId"])
    state.save("OpportunityLineItem", rec)


# -- leads -----------------------------------------------------------------------------------


def seed_leads(state: SeedState, world: World) -> None:
    anchor = world.anchor
    for lead in world.leads:
        owner = _owner(state, lead.owner_id)
        created = min(lead.created, anchor)
        domain = lead.email.split("@")[-1]
        converted = lead.status == "Closed - Converted"
        rec = state.new(
            "Lead",
            LastName=lead.last_name,
            FirstName=lead.first_name,
            Name=f"{lead.first_name} {lead.last_name}",
            Title=lead.title,
            Company=lead.company,
            City=lead.city,
            State=lead.state,
            Country=lead.country,
            Phone=lead.phone,
            Email=lead.email,
            Website=f"https://www.{domain}",
            Description=lead.description,
            LeadSource=lead.source,
            Status=lead.status,
            Industry=lead.industry,
            Rating=lead.rating,
            AnnualRevenue=float(lead.annual_revenue),
            NumberOfEmployees=lead.employees,
            OwnerId=owner,
            IsConverted=converted,
            IsUnreadByOwner=lead.status == "Open - Not Contacted",
            CleanStatus="Pending",
        )
        modified = modified_after(anchor, created, "lead", lead.id)
        if converted:
            conv_date = min(created + timedelta(days=7), anchor)
            modified = max(modified, conv_date)
            _convert(state, world, lead, rec, conv_date, owner)
        stamp(rec, created, modified=modified, created_by=owner)
        rec["PhotoUrl"] = f"/services/images/photo/{rec['Id']}"
        state.save("Lead", rec)
        state.lead_by_world[lead.id] = rec["Id"]


def _convert(state: SeedState, world: World, lead: Lead, rec: Record, when: datetime, owner: str) -> None:
    """Create the Account/Contact/Opportunity a converted lead turned into."""
    account = state.new(
        "Account",
        Name=lead.company,
        Type="Prospect",
        Industry=lead.industry,
        Rating=lead.rating,
        AnnualRevenue=float(lead.annual_revenue),
        NumberOfEmployees=lead.employees,
        OwnerId=owner,
        BillingCity=lead.city,
        BillingState=lead.state,
        BillingCountry=lead.country,
        AccountSource=lead.source,
        CleanStatus="Pending",
    )
    stamp(account, when, created_by=owner)
    state.save("Account", account)
    contact = state.new(
        "Contact",
        AccountId=account["Id"],
        LastName=lead.last_name,
        FirstName=lead.first_name,
        Name=f"{lead.first_name} {lead.last_name}",
        Email=lead.email,
        Phone=lead.phone,
        Title=lead.title,
        LeadSource=lead.source,
        OwnerId=owner,
        MailingCity=lead.city,
        MailingState=lead.state,
        MailingCountry=lead.country,
        CleanStatus="Pending",
    )
    stamp(contact, when, created_by=owner)
    state.save("Contact", contact)
    close = (when + timedelta(days=60)).date()
    fy, fq = _fiscal(close, world.org.fiscal_year_start_month)
    info = stage_info("Prospecting")
    category, category_name = forecast_for("Prospecting")
    opp = state.new(
        "Opportunity",
        AccountId=account["Id"],
        Name=f"{lead.company} - New Business",
        StageName="Prospecting",
        Probability=float(info["probability"]),
        CloseDate=close.isoformat(),
        Type="New Business",
        LeadSource=lead.source,
        ForecastCategory=category,
        ForecastCategoryName=category_name,
        OwnerId=owner,
        FiscalQuarter=fq,
        FiscalYear=fy,
        Fiscal=f"{fy} {fq}",
    )
    stamp(opp, when, created_by=owner)
    opp["LastStageChangeDate"] = fmt_dt(when)
    state.save("Opportunity", opp)
    role = state.new(
        "OpportunityContactRole",
        OpportunityId=opp["Id"],
        ContactId=contact["Id"],
        Role="Decision Maker",
        IsPrimary=True,
    )
    stamp(role, when, created_by=owner)
    state.save("OpportunityContactRole", role)
    rec.update(
        ConvertedDate=when.date().isoformat(),
        ConvertedAccountId=account["Id"],
        ConvertedContactId=contact["Id"],
        ConvertedOpportunityId=opp["Id"],
    )
    state.converted[lead.id] = {"account": account["Id"], "contact": contact["Id"], "opportunity": opp["Id"]}


# -- cases -----------------------------------------------------------------------------------


def seed_cases(state: SeedState, world: World) -> None:
    anchor = world.anchor
    highest = 1000
    for case in world.cases:
        owner = _owner(state, case.owner_id)
        contact = state.contact_by_person.get(case.contact_id)
        contact_rec = state.org.get("Contact", contact) if contact else None
        created = min(case.created, anchor)
        closed = case.status == "Closed"
        closed_at = min(case.closed, anchor) if case.closed else None
        number = case.number
        highest = max(highest, int(number)) if number.isdigit() else highest
        rec = state.new(
            "Case",
            CaseNumber=number,
            ContactId=contact,
            AccountId=state.account_by_client.get(case.client_id),
            SuppliedName=contact_rec["Name"] if contact_rec and case.origin == "Web" else None,
            SuppliedEmail=contact_rec["Email"] if contact_rec and case.origin == "Web" else None,
            Type=("Question", "Problem", "Feature Request")[hnum("case-type", case.id, mod=3)],
            Status=case.status,
            Origin=case.origin,
            Subject=case.subject,
            Priority=case.priority,
            Description=case.description,
            IsClosed=closed,
            ClosedDate=fmt_dt(closed_at or anchor) if closed else None,
            IsEscalated=case.status == "Escalated",
            OwnerId=owner,
            ContactPhone=contact_rec["Phone"] if contact_rec else None,
            ContactMobile=contact_rec["MobilePhone"] if contact_rec else None,
            ContactEmail=contact_rec["Email"] if contact_rec else None,
        )
        modified = closed_at if closed and closed_at else modified_after(anchor, created, "case", case.id)
        stamp(rec, created, modified=min(modified, anchor), created_by=owner)
        state.save("Case", rec)
    state.org.meta["case_number"] = max(highest, state.org.meta["case_number"])


# -- campaign members & rollups --------------------------------------------------------------


def _member(
    state: SeedState,
    campaign: str,
    *,
    lead_id: str | None = None,
    contact_id: str | None = None,
    responded: bool,
    when: datetime,
) -> None:
    source = state.org.get("Lead", lead_id) if lead_id else state.org.get("Contact", contact_id)
    assert source is not None
    account = state.org.get("Account", source.get("AccountId")) if contact_id else None
    rec = state.new(
        "CampaignMember",
        CampaignId=state.campaign_ids[campaign],
        LeadId=lead_id,
        ContactId=contact_id,
        Status="Responded" if responded else "Sent",
        HasResponded=responded,
        FirstRespondedDate=(when + timedelta(days=1)).date().isoformat() if responded else None,
        FirstName=source.get("FirstName"),
        LastName=source.get("LastName"),
        Name=source.get("Name"),
        Title=source.get("Title"),
        Email=source.get("Email"),
        Phone=source.get("Phone"),
        MobilePhone=source.get("MobilePhone"),
        CompanyOrAccount=source.get("Company") if lead_id else (account or {}).get("Name"),
        LeadOrContactId=lead_id or contact_id,
        LeadOrContactOwnerId=source.get("OwnerId"),
        Type="Lead" if lead_id else "Contact",
    )
    stamp(rec, when, created_by=state.admin_id)
    state.save("CampaignMember", rec)


def seed_campaign_members(state: SeedState, world: World) -> None:
    anchor = world.anchor
    year_end = world.anchor - timedelta(days=14)
    roundtable = world.anchor - timedelta(days=45)
    for lead in world.leads:
        lid = state.lead_by_world[lead.id]
        responded = lead.status != "Open - Not Contacted"
        if lead.source == "Webinar":
            _member(state, "year_end", lead_id=lid, responded=responded, when=year_end)
        elif lead.source == "Trade Show":
            _member(state, "audit_roundtable", lead_id=lid, responded=True, when=roundtable)
        elif lead.source == "LinkedIn":
            _member(state, "sred", lead_id=lid, responded=False, when=anchor - timedelta(days=3))
    active = [c for c in world.clients if c.status == "Active"]
    for i, client in enumerate(active[:8]):
        cid = state.contact_by_person.get(client.primary_contact_id)
        if not cid:
            continue
        _member(state, "audit_roundtable", contact_id=cid, responded=True, when=roundtable)
        if i < 6:
            _member(
                state,
                "year_end",
                contact_id=cid,
                responded=hnum("resp", client.id, mod=3) == 0,
                when=year_end,
            )
    _campaign_rollups(state)


def _campaign_rollups(state: SeedState) -> None:
    for camp in state.org.coll("Campaign"):
        recompute_campaign(state.org, camp)
