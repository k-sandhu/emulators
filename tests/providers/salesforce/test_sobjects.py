"""sObject CRUD, validation errors, derived fields, upsert and relationship traversal."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import Actor


def err(r: Any, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    body = r.json()
    assert isinstance(body, list) and body[0]["errorCode"] == code, body
    return body[0]


async def test_create_get_update_delete_roundtrip(admin: Actor) -> None:
    r = await admin.post(
        "/sobjects/Account", {"Name": "Roundtrip Ltd", "Industry": "Technology", "AnnualRevenue": 1250000}
    )
    assert r.status_code == 201
    body = r.json()
    assert body == {"id": body["id"], "success": True, "errors": []}
    assert r.headers["location"] == f"/services/data/v62.0/sobjects/Account/{body['id']}"
    rid = body["id"]
    got = (await admin.get(f"/sobjects/Account/{rid}")).json()
    describe = (await admin.get("/sobjects/Account/describe")).json()
    assert set(got) == {"attributes"} | {f["name"] for f in describe["fields"]}
    assert list(got)[:3] == ["attributes", "Id", "IsDeleted"]
    assert got["Name"] == "Roundtrip Ltd" and got["AnnualRevenue"] == 1250000.0
    assert got["OwnerId"] == admin.user_id and got["CreatedById"] == admin.user_id
    assert got["CreatedDate"] == "2026-10-16T12:00:00.000+0000"
    admin.client.hub.clock.advance(__import__("datetime").timedelta(minutes=5))  # type: ignore[attr-defined]
    r = await admin.patch(f"/sobjects/Account/{rid}", {"Rating": "Hot", "Phone": "+14165550199"})
    assert r.status_code == 204 and r.content == b""
    again = (await admin.get(f"/sobjects/Account/{rid}")).json()
    assert again["Rating"] == "Hot" and again["Name"] == "Roundtrip Ltd"
    assert again["CreatedDate"] == "2026-10-16T12:00:00.000+0000"
    assert again["LastModifiedDate"] == again["SystemModstamp"] == "2026-10-16T12:05:00.000+0000"
    assert (await admin.delete(f"/sobjects/Account/{rid}")).status_code == 204
    err(await admin.get(f"/sobjects/Account/{rid}"), 404, "NOT_FOUND")
    err(await admin.delete(f"/sobjects/Account/{rid}"), 404, "ENTITY_IS_DELETED")
    err(await admin.patch(f"/sobjects/Account/{rid}", {"Rating": "Cold"}), 404, "ENTITY_IS_DELETED")


async def test_get_with_fields_and_fifteen_char_ids(admin: Actor) -> None:
    rid = (await admin.ids("SELECT Id FROM Account WHERE Name = 'Northwind Logistics'"))[0]
    got = (
        await admin.get(
            f"/sobjects/Account/{rid}", params={"fields": "Name,Phone,Owner"} | {"fields": "Name,Phone"}
        )
    ).json()
    assert list(got) == ["attributes", "Name", "Phone"]
    short = (await admin.get(f"/sobjects/Account/{rid[:15]}", params={"fields": "Id"})).json()
    assert short["Id"] == rid
    bad = await admin.get(f"/sobjects/Account/{rid}", params={"fields": "Name,Nope"})
    assert err(bad, 400, "INVALID_FIELD")["message"] == "No such column 'Nope' on sobject of type Account"


async def test_unknown_objects_ids_and_methods(admin: Actor) -> None:
    for method, path in (
        ("GET", "/sobjects/Widget"),
        ("POST", "/sobjects/Widget"),
        ("GET", "/sobjects/Widget/describe"),
    ):
        r = await admin.request(method, path, json={} if method == "POST" else None)
        err(r, 404, "NOT_FOUND")
    contact = (await admin.ids("SELECT Id FROM Contact LIMIT 1"))[0]
    err(await admin.get(f"/sobjects/Account/{contact}"), 404, "NOT_FOUND")  # wrong key prefix
    err(await admin.get("/sobjects/Account/001000000000000AAA"), 404, "NOT_FOUND")
    err(await admin.get("/sobjects/Account/garbage"), 404, "NOT_FOUND")
    r = await admin.request("PUT", f"/sobjects/Account/{contact}", json={})
    e = err(r, 405, "METHOD_NOT_ALLOWED")
    assert "not allowed" in e["message"] and "GET" in r.headers["allow"]
    err(await admin.get("/nothing/here"), 404, "NOT_FOUND")
    bad_json = await admin.post(
        "/sobjects/Account", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    err(bad_json, 400, "JSON_PARSER_ERROR")
    err(
        await admin.post("/sobjects/Account", content=b"[]", headers={"Content-Type": "application/json"}),
        400,
        "JSON_PARSER_ERROR",
    )


async def test_required_fields(admin: Actor) -> None:
    e = err(await admin.post("/sobjects/Account", {}), 400, "REQUIRED_FIELD_MISSING")
    assert e == {
        "message": "Required fields are missing: [Name]",
        "errorCode": "REQUIRED_FIELD_MISSING",
        "fields": ["Name"],
    }
    e = err(await admin.post("/sobjects/Lead", {"FirstName": "x"}), 400, "REQUIRED_FIELD_MISSING")
    assert e["fields"] == ["LastName", "Company"]
    e = err(await admin.post("/sobjects/Opportunity", {"Name": "x"}), 400, "REQUIRED_FIELD_MISSING")
    assert e["fields"] == ["StageName", "CloseDate"]
    e = err(await admin.post("/sobjects/Contact", {"FirstName": "x"}), 400, "REQUIRED_FIELD_MISSING")
    assert e["fields"] == ["LastName"]
    acct = await admin.create("Account", {"Name": "Keep Me"})
    e = err(await admin.patch(f"/sobjects/Account/{acct}", {"Name": None}), 400, "REQUIRED_FIELD_MISSING")
    assert e["fields"] == ["Name"]


async def test_unknown_and_read_only_fields(admin: Actor) -> None:
    e = err(await admin.post("/sobjects/Account", {"Name": "x", "Foo__c": 1}), 400, "INVALID_FIELD")
    assert e["message"] == "No such column 'Foo__c' on sobject of type Account"
    e = err(
        await admin.post(
            "/sobjects/Account", {"Name": "x", "CreatedDate": "2020-01-01T00:00:00Z", "IsDeleted": True}
        ),
        400,
        "INVALID_FIELD_FOR_INSERT_UPDATE",
    )
    assert e["fields"] == ["IsDeleted", "CreatedDate"] or set(e["fields"]) == {"IsDeleted", "CreatedDate"}
    assert e["message"].startswith("Unable to create/update fields:")
    acct = await admin.create("Account", {"Name": "x"})
    e = err(
        await admin.patch(f"/sobjects/Account/{acct}", {"Id": acct}), 400, "INVALID_FIELD_FOR_INSERT_UPDATE"
    )
    assert e["fields"] == ["Id"]
    e = err(
        await admin.patch(f"/sobjects/Account/{acct}", {"BillingAddress": {"city": "x"}}),
        400,
        "INVALID_FIELD_FOR_INSERT_UPDATE",
    )
    e = err(
        await admin.post("/sobjects/Contact", {"LastName": "x", "Name": "forced"}),
        400,
        "INVALID_FIELD_FOR_INSERT_UPDATE",
    )
    # the attributes block is ignored
    ok = await admin.post("/sobjects/Account", {"attributes": {"type": "Account"}, "Name": "With attrs"})
    assert ok.status_code == 201


@pytest.mark.parametrize(
    ("sobject", "body", "code"),
    [
        (
            "Opportunity",
            {"Name": "x", "StageName": "Prospecting", "CloseDate": "2026-12-01", "Amount": "abc"},
            "JSON_PARSER_ERROR",
        ),
        (
            "Opportunity",
            {"Name": "x", "StageName": "Prospecting", "CloseDate": "12/01/2026"},
            "JSON_PARSER_ERROR",
        ),
        (
            "Opportunity",
            {"Name": "x", "StageName": "Prospecting", "CloseDate": "2026-12-01", "IsPrivate": "maybe"},
            "JSON_PARSER_ERROR",
        ),
        ("Account", {"Name": "x", "NumberOfEmployees": "many"}, "JSON_PARSER_ERROR"),
        ("Account", {"Name": "x", "NumberOfEmployees": 12345678901}, "NUMBER_OUTSIDE_VALID_RANGE"),
        (
            "Opportunity",
            {"Name": "x", "StageName": "Prospecting", "CloseDate": "2026-12-01", "Amount": 1e18},
            "NUMBER_OUTSIDE_VALID_RANGE",
        ),
        (
            "Opportunity",
            {"Name": "x", "StageName": "Teleporting", "CloseDate": "2026-12-01"},
            "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST",
        ),
        ("Account", {"Name": "x" * 256}, "STRING_TOO_LONG"),
        ("Contact", {"LastName": "x", "Email": "not-an-email"}, "INVALID_EMAIL_ADDRESS"),
        ("Account", {"Name": "x", "QuickBooks_Customer_Id__c": "1"}, "DUPLICATE_VALUE"),
        (
            "Account",
            {"Name": "x", "Service_Lines__c": "Audit & Assurance;Time Travel"},
            "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST",
        ),
        ("Contact", {"LastName": "x", "AccountId": "001000000000000AAA"}, "INVALID_CROSS_REFERENCE_KEY"),
        ("Contact", {"LastName": "x", "AccountId": "003000000000000AAA"}, "FIELD_INTEGRITY_EXCEPTION"),
        ("Contact", {"LastName": "x", "AccountId": "not-an-id"}, "FIELD_INTEGRITY_EXCEPTION"),
        (
            "Opportunity",
            {"Name": "x", "StageName": "Prospecting", "CloseDate": "2026-12-01", "Amount": -5},
            "FIELD_CUSTOM_VALIDATION_EXCEPTION",
        ),
        (
            "Engagement__c",
            {"Name": "x", "Start_Date__c": "2026-06-01", "End_Date__c": "2026-05-01"},
            "FIELD_CUSTOM_VALIDATION_EXCEPTION",
        ),
        (
            "Engagement__c",
            {"Name": "x", "Service__c": "Astrology"},
            "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST",
        ),
        ("Account", {"Name": "x", "OwnerId": "005000000000000AAA"}, "INVALID_CROSS_REFERENCE_KEY"),
        ("Event", {"Subject": "no start"}, "REQUIRED_FIELD_MISSING"),
        ("CampaignMember", {"Status": "Sent"}, "REQUIRED_FIELD_MISSING"),
    ],
)
async def test_validation_errors(admin: Actor, sobject: str, body: dict[str, Any], code: str) -> None:
    if sobject == "CampaignMember":
        body = {**body, "CampaignId": (await admin.ids("SELECT Id FROM Campaign LIMIT 1"))[0]}
    r = await admin.post(f"/sobjects/{sobject}", body)
    e = err(r, 400, code)
    assert e["message"]


async def test_error_messages_and_field_lists(admin: Actor) -> None:
    e = err(
        await admin.post(
            "/sobjects/Opportunity", {"Name": "x", "StageName": "Nope", "CloseDate": "2026-12-01"}
        ),
        400,
        "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST",
    )
    assert e == {
        "message": "Stage: bad value for restricted picklist field: Nope",
        "errorCode": "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST",
        "fields": ["StageName"],
    }
    e = err(
        await admin.post("/sobjects/Contact", {"LastName": "x", "Email": "oops"}),
        400,
        "INVALID_EMAIL_ADDRESS",
    )
    assert e["message"] == "Email: invalid email address: oops" and e["fields"] == ["Email"]
    e = err(
        await admin.post("/sobjects/Account", {"Name": "x", "QuickBooks_Customer_Id__c": "1"}),
        400,
        "DUPLICATE_VALUE",
    )
    assert e["message"].startswith(
        "duplicate value found: QuickBooks_Customer_Id__c duplicates value on record with id: 001"
    )
    e = err(
        await admin.post("/sobjects/Contact", {"LastName": "x", "AccountId": "001000000000000AAA"}),
        400,
        "INVALID_CROSS_REFERENCE_KEY",
    )
    assert e == {
        "message": "invalid cross reference id",
        "errorCode": "INVALID_CROSS_REFERENCE_KEY",
        "fields": ["AccountId"],
    }
    e = err(await admin.post("/sobjects/Account", {"Name": "x" * 300}), 400, "STRING_TOO_LONG")
    assert e["message"].startswith("Account Name: data value too large: ") and e["message"].endswith(
        "(max length=255)"
    )
    e = err(
        await admin.post(
            "/sobjects/Opportunity",
            {"Name": "x", "StageName": "Prospecting", "CloseDate": "2026-12-01", "Amount": -5},
        ),
        400,
        "FIELD_CUSTOM_VALIDATION_EXCEPTION",
    )
    assert e["fields"] == ["Amount"]


async def test_value_coercion(admin: Actor) -> None:
    rid = await admin.create(
        "Opportunity",
        {
            "Name": "Coerce", "StageName": "prospecting", "CloseDate": "2026-12-01T10:00:00Z",
            "Amount": "1234.567", "IsPrivate": "true", "NextStep": 42,
        },
    )  # fmt: skip
    got = (await admin.get(f"/sobjects/Opportunity/{rid}")).json()
    assert got["StageName"] == "Prospecting"  # picklist case is normalised
    assert got["CloseDate"] == "2026-12-01"  # datetime string truncated to a date
    assert got["Amount"] == 1234.57  # currency rounds to its scale
    assert got["IsPrivate"] is True and got["NextStep"] == "42"
    evt = await admin.create(
        "Event", {"Subject": "tz", "StartDateTime": "2026-12-01T10:00:00-05:00", "DurationInMinutes": 30}
    )
    got = (await admin.get(f"/sobjects/Event/{evt}")).json()
    assert got["StartDateTime"] == "2026-12-01T15:00:00.000+0000"
    assert got["EndDateTime"] == "2026-12-01T15:30:00.000+0000" and got["ActivityDate"] == "2026-12-01"


async def test_opportunity_stage_drives_derived_fields(admin: Actor) -> None:
    rid = await admin.create(
        "Opportunity",
        {"Name": "Stages", "StageName": "Qualification", "CloseDate": "2027-01-15", "Amount": 10000},
    )
    got = (await admin.get(f"/sobjects/Opportunity/{rid}")).json()
    assert (got["Probability"], got["IsClosed"], got["IsWon"], got["ForecastCategoryName"]) == (
        20.0,
        False,
        False,
        "Pipeline",
    )
    assert got["ExpectedRevenue"] == 2000.0 and (got["FiscalYear"], got["FiscalQuarter"], got["Fiscal"]) == (
        2027,
        1,
        "2027 1",
    )
    assert got["LastStageChangeDate"] == "2026-10-16T12:00:00.000+0000"
    await admin.patch(f"/sobjects/Opportunity/{rid}", {"StageName": "Negotiation/Review"})
    got = (await admin.get(f"/sobjects/Opportunity/{rid}")).json()
    assert (got["Probability"], got["ForecastCategory"], got["ForecastCategoryName"]) == (
        80.0,
        "Commit",
        "Commit",
    )
    assert got["ExpectedRevenue"] == 8000.0
    await admin.patch(f"/sobjects/Opportunity/{rid}", {"StageName": "Closed Won"})
    got = (await admin.get(f"/sobjects/Opportunity/{rid}")).json()
    assert (got["Probability"], got["IsClosed"], got["IsWon"], got["ForecastCategoryName"]) == (
        100.0,
        True,
        True,
        "Closed",
    )
    await admin.patch(f"/sobjects/Opportunity/{rid}", {"StageName": "Closed Lost", "Probability": 5})
    got = (await admin.get(f"/sobjects/Opportunity/{rid}")).json()
    assert (got["Probability"], got["IsClosed"], got["IsWon"], got["ForecastCategory"]) == (
        5.0,
        True,
        False,
        "Omitted",
    )
    await admin.patch(f"/sobjects/Opportunity/{rid}", {"CloseDate": "2026-04-01"})
    got = (await admin.get(f"/sobjects/Opportunity/{rid}")).json()
    assert (got["FiscalYear"], got["FiscalQuarter"]) == (2026, 2)


async def test_contact_lead_and_user_names_are_computed(admin: Actor) -> None:
    c = await admin.create("Contact", {"FirstName": "Ada", "LastName": "Lovelace", "Salutation": "Dr."})
    assert (await admin.get(f"/sobjects/Contact/{c}")).json()["Name"] == "Ada Lovelace"
    await admin.patch(f"/sobjects/Contact/{c}", {"FirstName": "Augusta"})
    assert (await admin.get(f"/sobjects/Contact/{c}")).json()["Name"] == "Augusta Lovelace"
    lead = await admin.create("Lead", {"LastName": "Solo", "Company": "Solo Inc"})
    got = (await admin.get(f"/sobjects/Lead/{lead}")).json()
    assert (
        got["Name"] == "Solo" and got["Status"] == "Open - Not Contacted" and got["IsUnreadByOwner"] is True
    )
    assert got["IsConverted"] is False


async def test_case_numbering_and_closing(admin: Actor) -> None:
    contact = (
        await admin.ids(
            "SELECT Id FROM Contact WHERE Email = 'christopher.ingram@northwindlogistics.example'"
        )
    )[0]
    first = await admin.create("Case", {"Subject": "Printer", "ContactId": contact})
    second = await admin.create("Case", {"Subject": "Scanner"})
    a = (await admin.get(f"/sobjects/Case/{first}")).json()
    b = (await admin.get(f"/sobjects/Case/{second}")).json()
    assert (a["CaseNumber"], b["CaseNumber"]) == ("00001010", "00001011")
    assert a["AccountId"] and a["ContactEmail"] == "christopher.ingram@northwindlogistics.example"
    assert (a["Status"], a["Priority"], a["IsClosed"], a["ClosedDate"]) == ("New", "Medium", False, None)
    await admin.patch(f"/sobjects/Case/{first}", {"Status": "Closed"})
    closed = (await admin.get(f"/sobjects/Case/{first}")).json()
    assert closed["IsClosed"] is True and closed["ClosedDate"] == "2026-10-16T12:00:00.000+0000"
    await admin.patch(f"/sobjects/Case/{first}", {"Status": "Working"})
    assert (await admin.get(f"/sobjects/Case/{first}")).json()["ClosedDate"] is None
    err(
        await admin.patch(f"/sobjects/Case/{first}", {"CaseNumber": "9"}),
        400,
        "INVALID_FIELD_FOR_INSERT_UPDATE",
    )


async def test_task_and_event_derivations(admin: Actor) -> None:
    acct = (await admin.ids("SELECT Id FROM Account WHERE Name = 'Bluefin Seafood'"))[0]
    opp = (await admin.ids(f"SELECT Id FROM Opportunity WHERE AccountId = '{acct}' LIMIT 1"))[0]
    t = await admin.create("Task", {"Subject": "Call", "WhatId": opp, "Priority": "High"})
    got = (await admin.get(f"/sobjects/Task/{t}")).json()
    assert got["AccountId"] == acct and got["Status"] == "Not Started" and got["IsHighPriority"] is True
    assert got["IsClosed"] is False and got["TaskSubtype"] == "Task"
    await admin.patch(f"/sobjects/Task/{t}", {"Status": "Completed"})
    done = (await admin.get(f"/sobjects/Task/{t}")).json()
    assert done["IsClosed"] is True and done["CompletedDateTime"] == "2026-10-16T12:00:00.000+0000"
    e = await admin.create(
        "Event",
        {
            "Subject": "Offsite",
            "WhatId": acct,
            "StartDateTime": "2026-11-02T09:00:00Z",
            "EndDateTime": "2026-11-02T11:30:00Z",
        },
    )
    got = (await admin.get(f"/sobjects/Event/{e}")).json()
    assert (
        got["DurationInMinutes"] == 150 and got["ActivityDate"] == "2026-11-02" and got["AccountId"] == acct
    )
    allday = await admin.create(
        "Event", {"Subject": "Holiday", "IsAllDayEvent": True, "ActivityDate": "2026-12-25"}
    )
    got = (await admin.get(f"/sobjects/Event/{allday}")).json()
    assert got["DurationInMinutes"] == 1440 and got["StartDateTime"] == "2026-12-25T00:00:00.000+0000"
    bad = await admin.post(
        "/sobjects/Event",
        {"Subject": "x", "StartDateTime": "2026-11-02T09:00:00Z", "EndDateTime": "2026-11-02T08:00:00Z"},
    )
    err(bad, 400, "FIELD_INTEGRITY_EXCEPTION")


async def test_line_items_roll_up_into_the_opportunity(admin: Actor) -> None:
    opp = await admin.create(
        "Opportunity", {"Name": "Lines", "StageName": "Prospecting", "CloseDate": "2027-02-01"}
    )
    entries = await admin.rows(
        "SELECT Id, UnitPrice, Product2.Name, Pricebook2Id FROM PricebookEntry ORDER BY UnitPrice"
    )
    cheap, dear = entries[0], entries[-1]
    err(
        await admin.post("/sobjects/OpportunityLineItem", {"OpportunityId": opp, "Quantity": 2}),
        400,
        "REQUIRED_FIELD_MISSING",
    )
    one = await admin.create(
        "OpportunityLineItem",
        {
            "OpportunityId": opp,
            "PricebookEntryId": cheap["Id"],
            "Quantity": 4,
            "UnitPrice": cheap["UnitPrice"],
        },
    )
    got = (await admin.get(f"/sobjects/OpportunityLineItem/{one}")).json()
    assert got["UnitPrice"] == cheap["UnitPrice"] == got["ListPrice"] and got["TotalPrice"] == round(
        4 * cheap["UnitPrice"], 2
    )
    assert got["Name"] == cheap["Product2"]["Name"] and got["Product2Id"].startswith("01t")
    two = await admin.create(
        "OpportunityLineItem",
        {"OpportunityId": opp, "PricebookEntryId": dear["Id"], "Quantity": 1, "UnitPrice": 999},
    )
    parent = (await admin.get(f"/sobjects/Opportunity/{opp}")).json()
    assert parent["HasOpportunityLineItem"] is True and parent["Pricebook2Id"] == cheap["Pricebook2Id"]
    assert parent["Amount"] == round(4 * cheap["UnitPrice"] + 999, 2)
    assert (await admin.delete(f"/sobjects/OpportunityLineItem/{two}")).status_code == 204
    parent = (await admin.get(f"/sobjects/Opportunity/{opp}")).json()
    assert parent["Amount"] == round(4 * cheap["UnitPrice"], 2)
    await admin.delete(f"/sobjects/OpportunityLineItem/{one}")
    assert (await admin.get(f"/sobjects/Opportunity/{opp}")).json()["HasOpportunityLineItem"] is False


async def test_primary_contact_role_is_exclusive_and_members_are_unique(admin: Actor) -> None:
    opp = (await admin.ids("SELECT Id FROM Opportunity WHERE IsClosed = false LIMIT 1"))[0]
    contact = (await admin.ids("SELECT Id FROM Contact LIMIT 2"))[1]
    roles = await admin.rows(
        f"SELECT Id, IsPrimary FROM OpportunityContactRole WHERE OpportunityId = '{opp}'"
    )
    assert [r["IsPrimary"] for r in roles] == [True]
    await admin.create(
        "OpportunityContactRole",
        {"OpportunityId": opp, "ContactId": contact, "Role": "Influencer", "IsPrimary": True},
    )
    after = await admin.rows(f"SELECT IsPrimary FROM OpportunityContactRole WHERE OpportunityId = '{opp}'")
    assert sorted(r["IsPrimary"] for r in after) == [False, True]
    campaign = (await admin.ids("SELECT Id FROM Campaign WHERE Name LIKE 'SR&ED%'"))[0]
    lead = (await admin.ids("SELECT Id FROM Lead WHERE Status = 'Open - Not Contacted' LIMIT 1"))[0]
    member = await admin.create(
        "CampaignMember", {"CampaignId": campaign, "LeadId": lead, "Status": "Responded"}
    )
    got = (await admin.get(f"/sobjects/CampaignMember/{member}")).json()
    assert (
        got["HasResponded"] is True and got["Type"] == "Lead" and got["LastName"] and got["CompanyOrAccount"]
    )
    err(
        await admin.post("/sobjects/CampaignMember", {"CampaignId": campaign, "LeadId": lead}),
        400,
        "DUPLICATE_VALUE",
    )
    err(
        await admin.post(
            "/sobjects/CampaignMember", {"CampaignId": campaign, "LeadId": lead, "ContactId": contact}
        ),
        400,
        "REQUIRED_FIELD_MISSING",
    )


async def test_cascade_delete_and_clear_references(admin: Actor) -> None:
    acct = await admin.create("Account", {"Name": "Parent Co"})
    child_acct = await admin.create("Account", {"Name": "Child Co", "ParentId": acct})
    contact = await admin.create("Contact", {"LastName": "Kid", "AccountId": acct})
    opp = await admin.create(
        "Opportunity",
        {"Name": "Deal", "StageName": "Prospecting", "CloseDate": "2027-03-01", "AccountId": acct},
    )
    case = await admin.create("Case", {"Subject": "Help", "AccountId": acct, "ContactId": contact})
    task = await admin.create("Task", {"Subject": "On contact", "WhoId": contact})
    event = await admin.create(
        "Event",
        {
            "Subject": "On acct",
            "WhatId": acct,
            "StartDateTime": "2026-11-02T09:00:00Z",
            "DurationInMinutes": 15,
        },
    )
    assert (await admin.delete(f"/sobjects/Account/{acct}")).status_code == 204
    for sobject, rid in (
        ("Contact", contact),
        ("Opportunity", opp),
        ("Case", case),
        ("Task", task),
        ("Event", event),
    ):
        err(await admin.get(f"/sobjects/{sobject}/{rid}"), 404, "NOT_FOUND")
    survivor = (await admin.get(f"/sobjects/Account/{child_acct}")).json()
    assert survivor["ParentId"] is None  # lookups are cleared, not cascaded
    engagement = (await admin.ids("SELECT Id FROM Engagement__c LIMIT 1"))[0]
    acct2 = (await admin.get(f"/sobjects/Engagement__c/{engagement}")).json()["Account__c"]
    await admin.delete(f"/sobjects/Account/{acct2}")
    assert (await admin.get(f"/sobjects/Engagement__c/{engagement}")).json()["Account__c"] is None


async def test_upsert_update_only_does_not_create_missing_record(admin: Actor) -> None:
    path = "/sobjects/Account/QuickBooks_Customer_Id__c/UPDATE-ONLY-MISSING"
    before = await admin.query("SELECT COUNT() FROM Account")
    response = await admin.patch(path, {"Name": "Must not create"}, params={"updateOnly": "true"})
    err(response, 404, "NOT_FOUND")
    assert await admin.query("SELECT COUNT() FROM Account") == before
    created = await admin.patch(path, {"Name": "May create"}, params={"updateOnly": "false"})
    assert created.status_code == 201
    updated = await admin.patch(path, {"Name": "Existing only"}, params={"updateOnly": "true"})
    assert updated.status_code == 200 and updated.json()["created"] is False
    assert (await admin.get(path)).json()["Name"] == "Existing only"


@pytest.mark.parametrize("version", ["45.0", "46.0"])
async def test_upsert_response_changes_at_api_46(admin: Actor, version: str) -> None:
    path = (
        f"https://halcyonadvisory.my.salesforce.com/services/data/v{version}"
        "/sobjects/Account/QuickBooks_Customer_Id__c/VERSIONED-UPSERT"
    )
    created = await admin.patch(path, {"Name": "Versioned"})
    assert created.status_code == 201
    expected = {"id": created.json()["id"], "success": True, "errors": []}
    assert created.json() == (expected if version == "45.0" else {**expected, "created": True})
    updated = await admin.patch(path, {"Name": "Versioned update"})
    if version == "45.0":
        assert updated.status_code == 204 and updated.content == b""
    else:
        assert updated.status_code == 200
        assert updated.json() == {**expected, "created": False}


async def test_upsert_by_external_id(admin: Actor) -> None:
    created = await admin.patch("/sobjects/Account/QuickBooks_Customer_Id__c/QB-900", {"Name": "Upserted Co"})
    assert created.status_code == 201
    rid = created.json()["id"]
    assert created.json() == {"id": rid, "success": True, "errors": [], "created": True}
    got = (await admin.get("/sobjects/Account/QuickBooks_Customer_Id__c/QB-900")).json()
    assert got["Id"] == rid and got["QuickBooks_Customer_Id__c"] == "QB-900" and got["Name"] == "Upserted Co"
    updated = await admin.patch(
        "/sobjects/Account/QuickBooks_Customer_Id__c/QB-900", {"Name": "Renamed Co", "Rating": "Hot"}
    )
    assert updated.status_code == 200
    assert updated.json() == {"id": rid, "success": True, "errors": [], "created": False}
    again = (
        await admin.get(
            f"/sobjects/Account/{rid}", params={"fields": "Name,Rating,QuickBooks_Customer_Id__c"}
        )
    ).json()
    assert (again["Name"], again["Rating"]) == ("Renamed Co", "Hot")
    # lookup by a case-insensitive external id value, with url-encoding
    assert (await admin.get("/sobjects/Account/QuickBooks_Customer_Id__c/qb-900")).status_code == 200
    err(await admin.get("/sobjects/Account/QuickBooks_Customer_Id__c/QB-NOPE"), 404, "NOT_FOUND")
    err(await admin.get("/sobjects/Account/Industry/Retail"), 404, "NOT_FOUND")  # not an external id field
    assert (await admin.delete("/sobjects/Account/QuickBooks_Customer_Id__c/QB-900")).status_code == 204
    err(await admin.get("/sobjects/Account/QuickBooks_Customer_Id__c/QB-900"), 404, "NOT_FOUND")
    code = await admin.patch("/sobjects/Engagement__c/Engagement_Code__c/e001", {"Status__c": "On Hold"})
    assert code.status_code == 200
    assert (await admin.rows("SELECT Status__c FROM Engagement__c WHERE Engagement_Code__c = 'e001'"))[0][
        "Status__c"
    ] == "On Hold"
    by_id = await admin.patch(f"/sobjects/Account/Id/{rid}", {"Rating": "Cold"})
    err(by_id, 404, "NOT_FOUND")


async def test_relationship_traversal_resources(admin: Actor) -> None:
    acct = (await admin.ids("SELECT Id FROM Account WHERE Name = 'Northwind Logistics'"))[0]
    contacts = (await admin.get(f"/sobjects/Account/{acct}/Contacts")).json()
    assert contacts["totalSize"] == 3 and contacts["done"] is True
    assert all(c["AccountId"] == acct and "Email" in c for c in contacts["records"])
    narrowed = (
        await admin.get(f"/sobjects/Account/{acct}/Contacts", params={"fields": "Id,LastName"})
    ).json()
    assert list(narrowed["records"][0]) == ["attributes", "Id", "LastName"]
    parent = (await admin.get(f"/sobjects/Contact/{contacts['records'][0]['Id']}/Account")).json()
    assert parent["Id"] == acct
    owner = (await admin.get(f"/sobjects/Account/{acct}/Owner")).json()
    assert owner["attributes"]["type"] == "User" and owner["Id"].startswith("005")
    err(await admin.get(f"/sobjects/Account/{acct}/Nothings"), 404, "NOT_FOUND")


async def test_updated_and_deleted_windows(admin: Actor) -> None:
    clock = admin.client.hub.clock  # type: ignore[attr-defined]
    keep = await admin.create("Account", {"Name": "Touched"})
    gone = await admin.create("Account", {"Name": "Doomed"})
    clock.advance(__import__("datetime").timedelta(minutes=10))
    await admin.delete(f"/sobjects/Account/{gone}")
    window = {"start": "2026-10-16T11:00:00Z", "end": "2026-10-16T12:10:00Z"}
    updated = (await admin.get("/sobjects/Account/updated", params=window)).json()
    assert keep in updated["ids"] and gone not in updated["ids"]
    assert updated["latestDateCovered"] == "2026-10-16T12:10:00.000+0000"
    deleted = (await admin.get("/sobjects/Account/deleted", params=window)).json()
    assert deleted["deletedRecords"] == [{"deletedDate": "2026-10-16T12:10:00.000+0000", "id": gone}]
    assert deleted["earliestDateAvailable"] == "2026-09-16T12:10:00.000+0000"
    err(
        await admin.get(
            "/sobjects/Account/updated",
            params={"start": "2026-10-16T13:00:00Z", "end": "2026-10-16T11:00:00Z"},
        ),
        400,
        "INVALID_REPLICATION_DATE",
    )
    err(
        await admin.get(
            "/sobjects/Account/updated",
            params={"start": "2020-01-01T00:00:00Z", "end": "2026-10-16T11:00:00Z"},
        ),
        400,
        "INVALID_REPLICATION_DATE",
    )
    err(await admin.get("/sobjects/Account/updated"), 400, "INVALID_FIELD")
    # a '+' offset arriving as a space (unencoded query strings) still parses
    r = await admin.get(
        "/sobjects/Account/updated?start=2026-10-16T11:00:00+00:00&end=2026-10-16T12:10:00+00:00"
    )
    assert r.status_code == 200
    err(
        await admin.get(
            "/sobjects/Account/updated",
            params={"start": "2026-10-16T11:00:00Z", "end": "2026-10-16T13:00:00Z"},
        ),
        400,
        "INVALID_REPLICATION_DATE",
    )


async def test_recent_items_and_object_info(admin: Actor) -> None:
    rid = (await admin.ids("SELECT Id FROM Account WHERE Name = 'Bluefin Seafood'"))[0]
    assert (await admin.get("/recent")).json() == []
    await admin.get(f"/sobjects/Account/{rid}")
    contact = await admin.create("Contact", {"LastName": "Recent"})
    recent = (await admin.get("/recent")).json()
    assert [r["Id"] for r in recent] == [contact, rid]
    assert recent[0]["attributes"]["type"] == "Contact" and recent[0]["Name"] == "Recent"
    info = (await admin.get("/sobjects/Account")).json()
    assert info["objectDescribe"]["name"] == "Account" and info["objectDescribe"]["keyPrefix"] == "001"
    assert [r["Id"] for r in info["recentItems"]] == [rid]
    assert (await admin.get("/recent", params={"limit": 1})).json()[0]["Id"] == contact
    viewed = await admin.rows(f"SELECT Id, LastViewedDate FROM Account WHERE Id = '{rid}'")
    assert viewed[0]["LastViewedDate"] == "2026-10-16T12:00:00.000+0000"


async def test_if_modified_since_returns_304(admin: Actor) -> None:
    rid = (await admin.ids("SELECT Id FROM Account WHERE Name = 'Bluefin Seafood'"))[0]
    first = await admin.get(f"/sobjects/Account/{rid}")
    stamp = first.headers["last-modified"]
    assert (
        await admin.get(f"/sobjects/Account/{rid}", headers={"If-Modified-Since": stamp})
    ).status_code == 304
    await admin.patch(f"/sobjects/Account/{rid}", {"Rating": "Hot"})
    admin.client.hub.clock.advance(__import__("datetime").timedelta(minutes=5))  # type: ignore[attr-defined]
    await admin.patch(f"/sobjects/Account/{rid}", {"Rating": "Cold"})
    assert (
        await admin.get(f"/sobjects/Account/{rid}", headers={"If-Modified-Since": stamp})
    ).status_code == 200
    describe = await admin.get("/sobjects/Account/describe")
    assert (
        await admin.get(
            "/sobjects/Account/describe", headers={"If-Modified-Since": describe.headers["last-modified"]}
        )
    ).status_code == 304


async def test_list_views(admin: Actor) -> None:
    views = (await admin.get("/sobjects/Account/listviews")).json()
    assert views["sobjectType"] == "Account" and views["done"] is True and views["size"] == 2
    assert [v["label"] for v in views["listviews"]] == ["All Accounts", "My Accounts"]
    all_view = views["listviews"][0]
    assert all_view["id"].startswith("00B") and all_view["resultsUrl"].endswith("/results")
    results = (
        await admin.get(all_view["resultsUrl"].replace("/services/data/v62.0", ""), params={"pageSize": 5})
    ).json()
    assert results["size"] == 41 and len(results["records"]) == 5
    assert results["records"][0]["columns"][0]["fieldNameOrPath"] == "Name"
    describe = (await admin.get(all_view["describeUrl"].replace("/services/data/v62.0", ""))).json()
    assert describe["sobjectType"] == "Account" and describe["columns"]
    err(await admin.get("/sobjects/Account/listviews/00B000000000000AAA/describe"), 404, "NOT_FOUND")


async def test_user_password_resource(admin: Actor, actor: Any) -> None:
    hannah = await actor("p011")
    target = hannah.user_id
    err(
        await hannah.post(f"/sobjects/User/{admin.user_id}/password", {"NewPassword": "Abcdefg1!"}),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    assert (await hannah.get(f"/sobjects/User/{target}/password")).json() == {"IsExpired": False}
    err(
        await admin.post(f"/sobjects/User/{target}/password", {"NewPassword": "short"}),
        400,
        "INVALID_NEW_PASSWORD",
    )
    assert (
        await admin.post(f"/sobjects/User/{target}/password", {"NewPassword": "Brand-New-1"})
    ).status_code == 204
    r = await admin.client.post(
        "https://login.salesforce.com/services/oauth2/token",
        data={
            "grant_type": "password",
            "client_id": "x",
            "username": "hannah.schmidt@halcyonadvisory.example.crm",
            "password": "Brand-New-1",
        },
    )
    assert r.status_code == 200
    old = await admin.client.post(
        "https://login.salesforce.com/services/oauth2/token",
        data={
            "grant_type": "password",
            "client_id": "x",
            "username": "hannah.schmidt@halcyonadvisory.example.crm",
            "password": "Halcyon2026!",
        },
    )
    assert old.status_code == 400
    reset = await admin.delete(f"/sobjects/User/{target}/password")
    assert reset.status_code == 200 and reset.json()["NewPassword"]


async def test_creating_users_through_the_api(admin: Actor) -> None:
    profile = (await admin.ids("SELECT Id FROM Profile WHERE Name = 'Standard User'"))[0]
    body = {
        "Username": "new.hire@halcyonadvisory.example.crm", "LastName": "Hire", "FirstName": "New", "Email": "new.hire@halcyonadvisory.example",
        "Alias": "nhire", "TimeZoneSidKey": "America/Toronto", "LocaleSidKey": "en_CA", "EmailEncodingKey": "UTF-8",
        "LanguageLocaleKey": "en_US", "ProfileId": profile,
    }  # fmt: skip
    uid = await admin.create("User", body)
    got = (await admin.get(f"/sobjects/User/{uid}")).json()
    assert got["Name"] == "New Hire" and got["IsActive"] is True and got["UserType"] == "Standard"
    err(await admin.post("/sobjects/User", {**body, "Alias": "dup"}), 400, "DUPLICATE_USERNAME")
    err(await admin.delete(f"/sobjects/User/{uid}"), 403, "INSUFFICIENT_ACCESS_OR_READONLY")
    r = await admin.client.post(
        "https://login.salesforce.com/services/oauth2/token",
        data={
            "grant_type": "password",
            "client_id": "x",
            "username": body["Username"],
            "password": "Halcyon2026!",
        },
    )
    assert r.status_code == 200  # new users get the default password
    await admin.patch(f"/sobjects/User/{uid}", {"IsActive": False})
    r = await admin.client.post(
        "https://login.salesforce.com/services/oauth2/token",
        data={
            "grant_type": "password",
            "client_id": "x",
            "username": body["Username"],
            "password": "Halcyon2026!",
        },
    )
    assert r.status_code == 400
    err(
        await admin.post("/sobjects/Account", {"Name": "Inactive owner", "OwnerId": uid}),
        400,
        "INACTIVE_OWNER_OR_USER",
    )


async def test_organization_is_read_only(admin: Actor) -> None:
    org = (
        await admin.rows(
            "SELECT Id, Name, IsSandbox, OrganizationType, InstanceName, DefaultAccountAccess, DefaultOpportunityAccess FROM Organization"
        )
    )[0]
    assert org["Name"] == "Halcyon Advisory Group" and org["Id"] == "00D5g0000049673EAA"
    assert (org["IsSandbox"], org["OrganizationType"], org["InstanceName"]) == (
        False,
        "Enterprise Edition",
        "NA224",
    )
    assert (org["DefaultAccountAccess"], org["DefaultOpportunityAccess"]) == ("Read", "None")
    err(await admin.post("/sobjects/Organization", {"Name": "x"}), 403, "INSUFFICIENT_ACCESS_OR_READONLY")


async def test_lookups_by_external_id_in_request_bodies(admin: Actor) -> None:
    contact = await admin.post(
        "/sobjects/Contact", {"LastName": "Lookup", "Account": {"QuickBooks_Customer_Id__c": "2"}}
    )
    assert contact.status_code == 201, contact.text
    got = (await admin.get(f"/sobjects/Contact/{contact.json()['id']}")).json()
    bluefin = (await admin.ids("SELECT Id FROM Account WHERE Name = 'Bluefin Seafood'"))[0]
    assert got["AccountId"] == bluefin
    nested = await admin.post(
        "/sobjects/Engagement__c",
        {
            "Name": "By code",
            "Account__r": {"attributes": {"type": "Account"}, "QuickBooks_Customer_Id__c": "1"},
        },
    )
    assert nested.status_code == 201
    moved = await admin.patch(
        f"/sobjects/Contact/{contact.json()['id']}", {"Account": {"QuickBooks_Customer_Id__c": "1"}}
    )
    assert moved.status_code == 204
    assert (await admin.get(f"/sobjects/Contact/{contact.json()['id']}")).json()["AccountId"] != bluefin
    missing = await admin.post(
        "/sobjects/Contact", {"LastName": "x", "Account": {"QuickBooks_Customer_Id__c": "NOPE"}}
    )
    e = err(missing, 400, "INVALID_FIELD")
    assert (
        e["message"]
        == "Foreign key external ID: NOPE not found for field QuickBooks_Customer_Id__c in entity Account"
    )
    err(
        await admin.post("/sobjects/Contact", {"LastName": "x", "Nope": {"Ext__c": "1"}}),
        400,
        "INVALID_FIELD",
    )
    upserted = await admin.patch("/sobjects/Contact/Id/" + contact.json()["id"], {"Title": "x"})
    assert upserted.status_code == 200


async def test_chatter_me_and_apex_rest_not_found(admin: Actor) -> None:
    me = (await admin.get("/chatter/users/me")).json()
    assert (
        me["id"] == admin.user_id
        and me["name"] == "Arjun Mehta"
        and me["email"] == "arjun.mehta@halcyonadvisory.example"
    )
    assert me["userType"] == "Internal" and me["url"].endswith(f"/chatter/users/{admin.user_id}")
    other = (await admin.get(f"/chatter/users/{admin.user_id}")).json()
    assert other["id"] == admin.user_id
    r = await admin.get("https://halcyonadvisory.my.salesforce.com/services/apexrest/Custom/Endpoint")
    assert r.status_code == 404
    assert r.json() == [
        {"errorCode": "NOT_FOUND", "message": "Could not find a match for URL /Custom/Endpoint"}
    ]


async def test_a_failed_save_leaves_no_partial_changes(admin: Actor) -> None:
    opp = (await admin.ids("SELECT Id FROM Opportunity WHERE IsClosed = false LIMIT 1"))[0]
    contact = (await admin.ids("SELECT Id FROM Contact LIMIT 2"))[1]
    before = await admin.rows(
        f"SELECT Id, IsPrimary FROM OpportunityContactRole WHERE OpportunityId = '{opp}'"
    )
    assert [r["IsPrimary"] for r in before] == [True]
    # the new role would take over as primary, then the invalid role value is rejected: nothing may change
    bad = await admin.post(
        "/sobjects/OpportunityContactRole",
        {"OpportunityId": opp, "ContactId": contact, "IsPrimary": True, "Role": "Astronaut"},
    )
    assert bad.status_code in (201, 400)
    if bad.status_code == 400:
        after = await admin.rows(
            f"SELECT Id, IsPrimary FROM OpportunityContactRole WHERE OpportunityId = '{opp}'"
        )
        assert after == before
    # a failing line item must not touch the parent opportunity
    entry = (await admin.ids("SELECT Id FROM PricebookEntry LIMIT 1"))[0]
    parent = (await admin.get(f"/sobjects/Opportunity/{opp}")).json()
    r = await admin.post(
        "/sobjects/OpportunityLineItem", {"OpportunityId": opp, "PricebookEntryId": entry, "Quantity": "lots"}
    )
    assert r.status_code == 400
    assert (await admin.get(f"/sobjects/Opportunity/{opp}")).json() == parent


async def test_empty_strings_clear_text_fields(admin: Actor) -> None:
    rid = await admin.create("Account", {"Name": "Blanks", "Phone": "+14165550100", "Industry": "Retail"})
    assert (await admin.patch(f"/sobjects/Account/{rid}", {"Phone": "", "Industry": ""})).status_code == 204
    got = (await admin.get(f"/sobjects/Account/{rid}")).json()
    assert got["Phone"] is None and got["Industry"] is None
    e = err(await admin.post("/sobjects/Account", {"Name": ""}), 400, "REQUIRED_FIELD_MISSING")
    assert e["fields"] == ["Name"]
    err(await admin.patch(f"/sobjects/Account/{rid}", {"Client_Since__c": ""}), 400, "JSON_PARSER_ERROR")


async def test_if_unmodified_since_guards_updates(admin: Actor) -> None:
    rid = await admin.create("Account", {"Name": "Guarded"})
    stamp = (await admin.get(f"/sobjects/Account/{rid}")).headers["last-modified"]
    admin.client.hub.clock.advance(__import__("datetime").timedelta(minutes=10))  # type: ignore[attr-defined]
    assert (
        await admin.patch(
            f"/sobjects/Account/{rid}", {"Rating": "Hot"}, headers={"If-Unmodified-Since": stamp}
        )
    ).status_code == 204
    e = err(
        await admin.patch(
            f"/sobjects/Account/{rid}", {"Rating": "Cold"}, headers={"If-Unmodified-Since": stamp}
        ),
        412,
        "PRECONDITION_FAILED",
    )
    assert "modified" in e["message"]
    assert (await admin.get(f"/sobjects/Account/{rid}")).json()["Rating"] == "Hot"
    head = await admin.request("HEAD", f"/sobjects/Account/{rid}")
    assert head.status_code == 200 and head.content == b""
