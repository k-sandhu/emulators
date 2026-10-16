"""The seeded org: the Halcyon world projected into Salesforce objects."""

from __future__ import annotations

import time
from collections import Counter

import httpx

from emulators.core.clock import Clock
from emulators.core.hub import EmulatorHub
from emulators.providers.salesforce.common import norm_id

from .conftest import NOW, Actor

PREFIXES = {
    "Account": "001", "Contact": "003", "Opportunity": "006", "Lead": "00Q", "Case": "500", "User": "005",
    "UserRole": "00E", "Profile": "00e", "Task": "00T", "Event": "00U", "Campaign": "701",
    "CampaignMember": "00v", "OpportunityContactRole": "00K", "OpportunityLineItem": "00k",
    "Pricebook2": "01s", "Product2": "01t", "PricebookEntry": "01u", "Engagement__c": "a0X",
}  # fmt: skip


def test_seeding_is_fast_and_deterministic() -> None:
    first = EmulatorHub(providers=["salesforce"], anchor="2026-10-16", clock=Clock(NOW), webhook_mode="sync")
    started = time.perf_counter()
    first._seed("salesforce")  # clear + seed + snapshot, excluding the shared world build
    assert time.perf_counter() - started < 2.0
    second = EmulatorHub(providers=["salesforce"], anchor="2026-10-16", clock=Clock(NOW), webhook_mode="sync")
    for name in ("Account", "Contact", "Opportunity", "User", "Task", "ContentDocument"):
        a = [
            (r["Id"], r.get("Name") or r.get("Subject") or r.get("Title"))
            for r in first.contexts["salesforce"].store.collection(name)
        ]
        b = [
            (r["Id"], r.get("Name") or r.get("Subject") or r.get("Title"))
            for r in second.contexts["salesforce"].store.collection(name)
        ]
        assert a == b and a, name


async def test_ids_are_18_char_with_the_right_key_prefixes(admin: Actor) -> None:
    for sobject, prefix in PREFIXES.items():
        for rec in await admin.rows(f"SELECT Id FROM {sobject}"):
            assert len(rec["Id"]) == 18 and rec["Id"].startswith(prefix), (sobject, rec["Id"])
            assert norm_id(rec["Id"][:15]) == rec["Id"]  # the 15-char form round-trips to the same record


async def test_object_counts_match_the_world(admin: Actor, client: httpx.AsyncClient) -> None:
    world = client.hub.world  # type: ignore[attr-defined]
    expect = {
        "Account": len(world.clients) + len(world.vendors) + 3,
        "Contact": len(world.external_people) + 3,
        "Opportunity": len(world.deals) + 3,
        "Lead": len(world.leads),
        "Case": len(world.cases),
        "User": len(world.employees) + 1,
        "Engagement__c": len(world.engagements),
        "Product2": len(world.items),
        "PricebookEntry": len(world.items),
        "Campaign": 3,
    }
    for sobject, count in expect.items():
        assert (await admin.query(f"SELECT COUNT() FROM {sobject}"))["totalSize"] == count, sobject
    assert (await admin.query("SELECT COUNT() FROM Event"))["totalSize"] == sum(
        1 for m in world.meetings if m.client_id and not m.cancelled
    )


async def test_accounts_mirror_clients_and_vendors(admin: Actor, client: httpx.AsyncClient) -> None:
    world = client.hub.world  # type: ignore[attr-defined]
    accounts = {a["Name"]: a for a in await admin.rows("SELECT FIELDS(ALL) FROM Account LIMIT 200")}
    types = Counter(a["Type"] for a in accounts.values())
    assert types == {"Customer": 21, "Prospect": 5, "Former Customer": 1, "Vendor": 14}
    for c in world.clients:
        a = accounts[c.name]
        assert (
            a["Industry"] == c.industry
            and a["AnnualRevenue"] == c.annual_revenue
            and a["NumberOfEmployees"] == c.employees
        )
        assert a["Phone"] == c.phone and a["Website"] == c.website and a["Rating"] == c.rating
        assert a["BillingCity"] == c.address.city and a["BillingCountry"] == c.address.country
        assert (
            a["Client_Since__c"] == c.client_since.isoformat()
            and a["Payment_Terms__c"] == f"Net {c.payment_terms_days}"
        )
        assert a["Preferred_Meeting_Platform__c"] in ("Microsoft Teams", "Zoom", "Google Meet")
        assert a["QuickBooks_Customer_Id__c"] == str(int(c.id[1:]))
        owner = world.person(c.owner_id)
        owner_row = (await admin.rows(f"SELECT Email FROM User WHERE Id = '{a['OwnerId']}'"))[0]
        assert owner_row["Email"] == owner.email
    for v in world.vendors:
        a = accounts[v.name]
        assert a["Type"] == "Vendor" and a["Phone"] == v.phone and a["BillingCity"] == v.address.city
    assert accounts["Northwind Logistics"]["AccountSource"] == "Partner"
    assert accounts["Northwind Logistics"]["LastActivityDate"] is not None


async def test_contacts_report_to_the_primary_contact(admin: Actor, client: httpx.AsyncClient) -> None:
    world = client.hub.world  # type: ignore[attr-defined]
    contacts = {
        c["Email"]: c
        for c in await admin.rows(
            "SELECT Id, Email, ReportsToId, Account.Name, Title, OwnerId, MailingCity FROM Contact"
        )
    }
    for c in world.clients:
        primary = contacts[world.person(c.primary_contact_id).email]
        assert primary["ReportsToId"] is None and primary["Account"]["Name"] == c.name
        for pid in c.contact_ids:
            if pid != c.primary_contact_id:
                assert contacts[world.person(pid).email]["ReportsToId"] == primary["Id"]
    vendor_contact = contacts[world.person("v001-1").email]
    assert vendor_contact["Account"]["Name"] == "Brightline Office Supply"


async def test_opportunities_contact_roles_and_line_items(admin: Actor, client: httpx.AsyncClient) -> None:
    world = client.hub.world  # type: ignore[attr-defined]
    rows = await admin.rows("SELECT FIELDS(ALL) FROM Opportunity ORDER BY Id LIMIT 200")
    for o, d in zip(rows, world.deals, strict=False):  # seeded in world order; deal names are not unique
        assert o["Name"] == d.name
        assert (o["StageName"], o["Amount"], o["Probability"], o["IsWon"], o["IsClosed"]) == (
            d.stage,
            d.amount,
            float(d.probability),
            d.is_won,
            d.is_closed,
        )
        assert (
            o["CloseDate"] == d.close_date.isoformat()
            and o["Type"] == d.type
            and o["LeadSource"] == d.lead_source
        )
        assert o["NextStep"] == d.next_step and o["CreatedDate"].startswith(
            d.created.strftime("%Y-%m-%dT%H:%M")
        )
    categories = Counter(o["ForecastCategoryName"] for o in rows)
    assert categories["Closed"] == 30 and categories["Omitted"] == 4 and categories["Commit"] == 4
    assert (
        rows[0]["Fiscal"] == "2025 4"
        and rows[0]["Engagement__c"]
        and rows[0]["HasOpportunityLineItem"] is True
    )
    roles = await admin.rows(
        "SELECT Opportunity.Name, Contact.Email, Role, IsPrimary FROM OpportunityContactRole WHERE IsPrimary = true"
    )
    assert len(roles) == 47 and {r["Role"] for r in roles} == {"Decision Maker"}
    influencers = await admin.rows(
        "SELECT Opportunity.Amount, Role FROM OpportunityContactRole WHERE IsPrimary = false"
    )
    assert influencers and all(
        i["Role"] == "Influencer" and i["Opportunity"]["Amount"] >= 100000 for i in influencers
    )
    items = await admin.rows(
        "SELECT Name, Quantity, UnitPrice, TotalPrice, ListPrice, Opportunity.Name, Opportunity.IsWon, Opportunity.Amount FROM OpportunityLineItem"
    )
    assert len(items) == 30 and all(
        i["Opportunity"]["IsWon"] and i["TotalPrice"] == i["Opportunity"]["Amount"] for i in items
    )
    webinar = await admin.rows("SELECT Name, Campaign.Name FROM Opportunity WHERE CampaignId != NULL")
    assert len(webinar) == 3 and {w["Campaign"]["Name"] for w in webinar} == {
        "Year-End Tax Planning Webinar 2026"
    }
    book = (await admin.rows("SELECT Name, IsStandard, IsActive FROM Pricebook2"))[0]
    assert book == {
        "attributes": book["attributes"],
        "Name": "Standard Price Book",
        "IsStandard": True,
        "IsActive": True,
    }


async def test_leads_cases_engagements_and_campaigns(admin: Actor, client: httpx.AsyncClient) -> None:
    world = client.hub.world  # type: ignore[attr-defined]
    leads = {x["Email"]: x for x in await admin.rows("SELECT FIELDS(ALL) FROM Lead LIMIT 200")}
    for lead in world.leads:
        got = leads[lead.email]
        assert (got["Status"], got["Company"], got["Rating"], got["LeadSource"]) == (
            lead.status,
            lead.company,
            lead.rating,
            lead.source,
        )
        assert got["IsConverted"] == (lead.status == "Closed - Converted")
        if got["IsConverted"]:
            assert (
                got["ConvertedAccountId"].startswith("001")
                and got["ConvertedContactId"].startswith("003")
                and got["ConvertedOpportunityId"].startswith("006")
            )
            assert got["ConvertedDate"]
    cases = {c["CaseNumber"]: c for c in await admin.rows("SELECT FIELDS(ALL) FROM Case LIMIT 200")}
    for case in world.cases:
        got = cases[case.number]
        assert (got["Subject"], got["Status"], got["Priority"], got["Origin"]) == (
            case.subject,
            case.status,
            case.priority,
            case.origin,
        )
        assert got["IsClosed"] == (case.status == "Closed")
    engagements = await admin.rows(
        "SELECT Name, Account__r.Name, Lead__r.Email, Service__c, Status__c, Billing_Type__c, Budget_Hours__c, Hourly_Rate__c, Fixed_Fee__c, Engagement_Code__c FROM Engagement__c ORDER BY Engagement_Code__c"
    )
    assert len(engagements) == 30
    e = world.engagements[0]
    assert (
        engagements[0]["Account__r"]["Name"] == world.client(e.client_id).name
        and engagements[0]["Name"] == e.name
    )
    assert (
        engagements[0]["Lead__r"]["Email"] == world.person(e.lead_id).email
        and engagements[0]["Fixed_Fee__c"] == e.fixed_fee
    )
    camp = (
        await admin.rows(
            "SELECT Name, Type, Status, NumberOfLeads, NumberOfContacts, NumberOfResponses, NumberOfOpportunities, BudgetedCost FROM Campaign WHERE Name = 'Year-End Tax Planning Webinar 2026'"
        )
    )[0]
    members = await admin.rows(
        "SELECT Id, LeadId, ContactId, HasResponded FROM CampaignMember WHERE Campaign.Name = 'Year-End Tax Planning Webinar 2026'"
    )
    assert camp["Type"] == "Webinar" and camp["NumberOfLeads"] == sum(1 for m in members if m["LeadId"])
    assert camp["NumberOfContacts"] == sum(1 for m in members if m["ContactId"]) and camp[
        "NumberOfResponses"
    ] == sum(m["HasResponded"] for m in members)
    assert camp["NumberOfOpportunities"] == 3 and camp["BudgetedCost"] == 4500.0


async def test_activities(admin: Actor) -> None:
    emails = await admin.rows(
        "SELECT Subject, TaskSubtype, Status, IsClosed, ActivityDate, Who.Email, What.Name, Owner.Email FROM Task WHERE TaskSubtype = 'Email'"
    )
    assert len(emails) == 298 and all(
        t["Subject"].startswith("Email: ") and t["Status"] == "Completed" for t in emails
    )
    assert all(t["Owner"]["Email"].endswith("@halcyonadvisory.example") for t in emails)
    events = await admin.rows(
        "SELECT Subject, Location, IsAllDayEvent, StartDateTime, EndDateTime, DurationInMinutes, Who.Name, What.Name, Owner.Name FROM Event ORDER BY StartDateTime"
    )
    assert len(events) == 77 and all(e["Who"] and e["What"] and e["DurationInMinutes"] > 0 for e in events)
    assert events[0]["StartDateTime"].endswith(".000+0000") and events[0]["Location"] in (
        "Microsoft Teams",
        "Zoom",
        "Google Meet",
    )
    open_tasks = await admin.rows(
        "SELECT Subject, Status, Priority, ActivityDate FROM Task WHERE IsClosed = false"
    )
    assert len(open_tasks) == 18 and {t["Status"] for t in open_tasks} <= {"Not Started", "In Progress"}
    assert any(t["ActivityDate"] < "2026-10-16" for t in open_tasks)  # something is overdue


async def test_users_roles_and_integration_user(admin: Actor, client: httpx.AsyncClient) -> None:
    world = client.hub.world  # type: ignore[attr-defined]
    users = {
        u["EmployeeNumber"]: u
        for u in await admin.rows("SELECT FIELDS(ALL) FROM User LIMIT 200")
        if u["EmployeeNumber"]
    }
    for p in world.employees:
        u = users[p.employee_id]
        assert u["Username"] == f"{p.username}@halcyonadvisory.example.crm" and u["Email"] == p.email
        assert (u["FirstName"], u["LastName"], u["Name"], u["Title"], u["IsActive"]) == (
            p.first_name,
            p.last_name,
            p.display_name,
            p.job_title,
            p.active,
        )
        assert u["TimeZoneSidKey"] == p.timezone and u["LocaleSidKey"] == p.locale.replace("-", "_")
        assert (
            u["UserType"] == "Standard"
            and u["EmailEncodingKey"] == "UTF-8"
            and u["LanguageLocaleKey"] == "en_US"
        )
        if p.manager_id:
            assert u["ManagerId"] == users[world.person(p.manager_id).employee_id]["Id"]
    bot = (
        await admin.rows(
            "SELECT Username, Name, Profile.Name, IsActive FROM User WHERE Username LIKE 'integration@%'"
        )
    )[0]
    assert (
        bot["Name"] == "Integration User"
        and bot["Profile"]["Name"] == "Salesforce API Only System Integrations"
    )
    profiles = {p["Name"] for p in await admin.rows("SELECT Name FROM Profile")}
    assert profiles == {
        "System Administrator",
        "Standard User",
        "Sales User",
        "Read Only",
        "Salesforce API Only System Integrations",
    }
    organization = (await admin.rows("SELECT Name, Country, Phone, FiscalYearStartMonth FROM Organization"))[
        0
    ]
    assert organization["Name"] == world.org.name and organization["Country"] == "Canada"


async def test_stage_and_status_setup_objects(admin: Actor) -> None:
    stages = await admin.rows(
        "SELECT MasterLabel, DefaultProbability, IsClosed, IsWon, ForecastCategoryName, SortOrder FROM OpportunityStage ORDER BY SortOrder"
    )
    assert [s["MasterLabel"] for s in stages][:3] == ["Prospecting", "Qualification", "Needs Analysis"]
    assert stages[-2] == {
        "attributes": stages[-2]["attributes"],
        "MasterLabel": "Closed Won",
        "DefaultProbability": 100.0,
        "IsClosed": True,
        "IsWon": True,
        "ForecastCategoryName": "Closed",
        "SortOrder": 9,
    }
    assert [s["DefaultProbability"] for s in stages if not s["IsClosed"]] == sorted(
        s["DefaultProbability"] for s in stages if not s["IsClosed"]
    )
    leads = await admin.rows("SELECT MasterLabel, IsDefault, IsConverted FROM LeadStatus ORDER BY SortOrder")
    assert [(r["MasterLabel"], r["IsDefault"], r["IsConverted"]) for r in leads] == [
        ("Open - Not Contacted", True, False),
        ("Working - Contacted", False, False),
        ("Closed - Converted", False, True),
        ("Closed - Not Converted", False, False),
    ]
    cases = await admin.rows("SELECT MasterLabel, IsClosed FROM CaseStatus ORDER BY SortOrder")
    assert [(r["MasterLabel"], r["IsClosed"]) for r in cases] == [
        ("New", False),
        ("Working", False),
        ("Escalated", False),
        ("Closed", True),
    ]
    r = await admin.post("/sobjects/OpportunityStage", {"MasterLabel": "Mystery"})
    assert r.status_code == 403
    # the live picklist and the setup object agree about every stage's default probability
    probabilities = {s["MasterLabel"]: s["DefaultProbability"] for s in stages}
    opp = await admin.create(
        "Opportunity", {"Name": "Stage check", "StageName": "Perception Analysis", "CloseDate": "2027-03-01"}
    )
    assert (await admin.get(f"/sobjects/Opportunity/{opp}")).json()["Probability"] == probabilities[
        "Perception Analysis"
    ]
