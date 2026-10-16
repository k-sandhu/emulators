"""Malformed input, validation edge cases and the consistency of seeded vs described data."""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from .conftest import ARJUN, BASE, HANNAH, LOGIN, NATALIE, Actor
from .test_bulk import ingest, parse_csv


def code_of(response: httpx.Response, status: int) -> str:
    assert response.status_code == status, response.text
    body = response.json()
    return body[0]["errorCode"] if isinstance(body, list) else body.get("error", "")


# -- malformed input never 500s --------------------------------------------------------------------


async def test_request_bodies_of_the_wrong_shape_are_rejected_not_crashed(admin: Actor) -> None:
    bad_composites: list[Any] = [
        {"compositeRequest": [1]},
        {"compositeRequest": ["x"]},
        {"compositeRequest": [{"method": 5, "url": "/x", "referenceId": "a"}]},
        {"compositeRequest": [{"method": "GET", "url": ["x"], "referenceId": "a"}]},
        {
            "compositeRequest": [
                {"method": "POST", "url": "/services/data/v62.0/sobjects/Account", "body": "x"}
            ]
        },
        {"compositeRequest": {"a": 1}},
    ]
    for body in bad_composites:
        r = await admin.post("/composite", body)
        assert r.status_code == 400 and r.json()[0]["errorCode"] in ("JSON_PARSER_ERROR", "INVALID_INPUT"), (
            body
        )
    assert code_of(await admin.post("/composite/batch", {"batchRequests": [1]}), 400) == "JSON_PARSER_ERROR"
    assert (
        code_of(await admin.post("/composite/batch", {"batchRequests": [{"method": 1, "url": "x"}]}), 400)
        == "JSON_PARSER_ERROR"
    )
    for tree in (
        {"records": [1]},
        {"records": [{"attributes": 7}]},
        {"records": [{"attributes": {"type": 5}}]},
    ):
        assert code_of(await admin.post("/composite/tree/Account", tree), 400) in (
            "JSON_PARSER_ERROR",
            "INVALID_INPUT",
        )
    assert code_of(await admin.post("/composite/sobjects", {"allOrNone": False, "records": [1]}), 400)
    assert code_of(
        await admin.post("/composite/sobjects/Account", {"recordIds": "x", "fieldNames": ["Id"]}), 400
    ) == ("JSON_PARSER_ERROR")
    assert code_of(
        await admin.post("/composite/sobjects/Account", {"recordIds": [1], "fieldNames": ["Id"]}), 400
    ) == ("JSON_PARSER_ERROR")


async def test_non_standard_and_pathological_json_is_a_parser_error(admin: Actor) -> None:
    for payload in (b"NaN", b'{"Name": NaN}', b'{"AnnualRevenue": Infinity}', b"[1, 2]", b"null", b'"str"'):
        r = await admin.post(
            "/sobjects/Account", content=payload, headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 400, payload
        assert r.json()[0]["errorCode"] == "JSON_PARSER_ERROR", payload


async def test_invalid_numeric_parameters_are_400s(admin: Actor) -> None:
    assert code_of(await admin.get("/recent", params={"limit": "lots"}), 400) == "INVALID_PARAMETER_VALUE"
    assert code_of(
        await admin.get("/parameterizedSearch", params={"q": "Northwind", "overallLimit": "x"}), 400
    ) == ("INVALID_PARAMETER_VALUE")
    assert code_of(
        await admin.get(
            "/parameterizedSearch", params={"q": "Northwind", "sobject": "Account", "Account.limit": "0"}
        ),
        400,
    ) == ("INVALID_PARAMETER_VALUE")
    assert code_of(
        await admin.post("/parameterizedSearch", {"q": "Northwind", "sobjects": "Account"}), 400
    ) == ("JSON_PARSER_ERROR")
    assert code_of(
        await admin.post("/parameterizedSearch", {"q": "Northwind", "sobjects": [{"name": 5}]}), 400
    ) == ("JSON_PARSER_ERROR")
    assert code_of(await admin.post("/parameterizedSearch", {"q": "Northwind", "in": "NOPE"}), 400) == (
        "INVALID_SEARCH"
    )
    ok = await admin.post("/parameterizedSearch", {"q": "Northwind", "sobjects": [{"name": "Account"}]})
    assert ok.status_code == 200 and ok.json()["searchRecords"]


async def test_bulk_job_parameters_are_validated(admin: Actor) -> None:
    for body in (
        {"object": ["Account"], "operation": "insert"},
        {"object": "Account", "operation": ["insert"]},
        {"object": "Account", "operation": {"x": 1}},
    ):
        assert (await admin.post("/jobs/ingest", body)).status_code == 400, body
    assert (await admin.post("/jobs/query", {"operation": "query", "query": ["x"]})).status_code == 400
    job = await admin.post("/jobs/query", {"operation": "query", "query": "SELECT Id FROM Account"})
    assert job.status_code == 200
    results = f"/jobs/query/{job.json()['id']}/results"
    assert code_of(await admin.get(results, params={"maxRecords": "0"}), 400) == "INVALID_PARAMETER_VALUE"
    assert code_of(await admin.get(results, params={"maxRecords": "many"}), 400) == "INVALID_PARAMETER_VALUE"
    assert (await admin.get(results, params={"maxRecords": "5"})).status_code == 200


async def test_bulk_ingest_job_listing_reflects_the_current_state(admin: Actor) -> None:
    job = await ingest(admin, "Account", "insert", "Name\nListed Co\n")
    listing = (await admin.get("/jobs/ingest")).json()["records"]
    assert [j["state"] for j in listing if j["id"] == job["id"]] == ["JobComplete"]
    created = (await admin.post("/jobs/ingest", {"object": "Account", "operation": "insert"})).json()
    await admin.request(
        "PUT",
        f"/jobs/ingest/{created['id']}/batches",
        content=b"Name\nPending Co\n",
        headers={"Content-Type": "text/csv"},
    )
    await admin.patch(f"/jobs/ingest/{created['id']}", {"state": "UploadComplete"})
    # the very first listing already reports the job as finished (it is synced before it is rendered)
    first = {j["id"]: j["state"] for j in (await admin.get("/jobs/ingest")).json()["records"]}
    assert first[created["id"]] == "JobComplete"
    assert parse_csv((await admin.get(f"/jobs/ingest/{job['id']}/successfulResults")).text)


# -- query cursors belong to the user who ran the query -----------------------------------------------


async def test_cursors_cannot_be_replayed_by_another_user(admin: Actor, actor: Any) -> None:
    first = (
        await admin.get(
            "/query",
            params={"q": "SELECT Id FROM Task ORDER BY Id"},
            headers={"Sforce-Query-Options": "batchSize=200"},
        )
    ).json()
    next_url = first["nextRecordsUrl"].replace("/services/data/v62.0", "")
    natalie = await actor(NATALIE)
    stolen = await natalie.get(next_url)
    assert stolen.status_code == 400 and stolen.json()[0]["errorCode"] == "INVALID_QUERY_LOCATOR"
    assert (await admin.get(next_url)).status_code == 200
    beyond = await admin.get(next_url.rsplit("-", 1)[0] + "-99999")
    assert beyond.status_code == 400 and beyond.json()[0]["errorCode"] == "INVALID_QUERY_LOCATOR"


# -- HTTP method tunnelling ------------------------------------------------------------------------------


async def test_http_method_override_lets_post_act_as_patch(admin: Actor) -> None:
    acct = await admin.create("Account", {"Name": "Tunnel Co"})
    r = await admin.post(f"/sobjects/Account/{acct}", {"Industry": "Retail"}, params={"_HttpMethod": "PATCH"})
    assert r.status_code == 204
    assert (await admin.get(f"/sobjects/Account/{acct}")).json()["Industry"] == "Retail"
    gone = await admin.post(f"/sobjects/Account/{acct}", params={"_HttpMethod": "DELETE"})
    assert gone.status_code == 204
    assert not await admin.ids(f"SELECT Id FROM Account WHERE Id = '{acct}'")
    bad = await admin.post(f"/sobjects/Account/{acct}", {"Name": "x"}, params={"_HttpMethod": "TRACE"})
    assert code_of(bad, 400) == "BAD_REQUEST"


# -- composite reference failures name the placeholder -----------------------------------------------------


async def test_unresolvable_composite_reference_names_the_placeholder(admin: Actor) -> None:
    r = await admin.post(
        "/composite",
        {
            "allOrNone": False,
            "compositeRequest": [
                {
                    "method": "POST",
                    "url": "/services/data/v62.0/sobjects/Contact",
                    "referenceId": "c",
                    "body": {"LastName": "Ref", "AccountId": "@{missing.id}"},
                },
                {
                    "method": "GET",
                    "url": "/services/data/v62.0/sobjects/Account/@{c.nope[3]}",
                    "referenceId": "g",
                },
            ],
        },
    )
    first, second = r.json()["compositeResponse"]
    assert first["httpStatusCode"] == 400 and first["body"][0]["errorCode"] == "INVALID_REFERENCE"
    assert "@{missing.id}" in first["body"][0]["message"]
    assert second["body"][0]["errorCode"] in ("PROCESSING_HALTED", "INVALID_REFERENCE")


# -- numeric ranges ---------------------------------------------------------------------------------------


async def test_probability_and_geocode_ranges(admin: Actor) -> None:
    opp = {"Name": "Range", "StageName": "Prospecting", "CloseDate": "2026-12-31"}
    for probability in (-1, 101, 250):
        r = await admin.post("/sobjects/Opportunity", {**opp, "Probability": probability})
        assert code_of(r, 400) == "NUMBER_OUTSIDE_VALID_RANGE", probability
    assert (await admin.post("/sobjects/Opportunity", {**opp, "Probability": 100})).status_code == 201
    for field, value in (("BillingLatitude", 91), ("BillingLatitude", -90.5), ("BillingLongitude", 180.1)):
        r = await admin.post("/sobjects/Account", {"Name": "Geo", field: value})
        assert code_of(r, 400) == "NUMBER_OUTSIDE_VALID_RANGE", (field, value)
    ok = await admin.post(
        "/sobjects/Account", {"Name": "Geo", "BillingLatitude": 43.64, "BillingLongitude": -79.38}
    )
    assert ok.status_code == 201


async def test_line_item_quantity_must_be_positive(admin: Actor) -> None:
    entry = (await admin.rows("SELECT Id, Pricebook2Id FROM PricebookEntry WHERE IsActive = true LIMIT 1"))[0]
    opp = await admin.create(
        "Opportunity",
        {
            "Name": "Qty",
            "StageName": "Prospecting",
            "CloseDate": "2026-12-31",
            "Pricebook2Id": entry["Pricebook2Id"],
        },
    )
    for qty in (0, -3):
        r = await admin.post(
            "/sobjects/OpportunityLineItem",
            {"OpportunityId": opp, "PricebookEntryId": entry["Id"], "Quantity": qty, "UnitPrice": 10},
        )
        assert code_of(r, 400) == "NUMBER_OUTSIDE_VALID_RANGE", qty
    good = await admin.post(
        "/sobjects/OpportunityLineItem",
        {"OpportunityId": opp, "PricebookEntryId": entry["Id"], "Quantity": 2, "UnitPrice": 10},
    )
    assert good.status_code == 201


# -- events keep start, end and duration consistent ----------------------------------------------------------


async def test_event_end_and_duration_follow_whatever_changed(admin: Actor) -> None:
    ev = await admin.create(
        "Event", {"Subject": "Sync", "StartDateTime": "2026-11-02T14:00:00.000+0000", "DurationInMinutes": 60}
    )

    async def state() -> dict[str, Any]:
        return (await admin.get(f"/sobjects/Event/{ev}")).json()

    assert (await state())["EndDateTime"] == "2026-11-02T15:00:00.000+0000"
    # moving the start keeps the length
    await admin.patch(f"/sobjects/Event/{ev}", {"StartDateTime": "2026-11-03T09:00:00.000+0000"})
    moved = await state()
    assert moved["DurationInMinutes"] == 60 and moved["EndDateTime"] == "2026-11-03T10:00:00.000+0000"
    assert moved["ActivityDate"] == "2026-11-03"
    # a new duration moves the end
    await admin.patch(f"/sobjects/Event/{ev}", {"DurationInMinutes": 90})
    assert (await state())["EndDateTime"] == "2026-11-03T10:30:00.000+0000"
    # a new end recomputes the duration, even when a stale duration is sent along
    await admin.patch(
        f"/sobjects/Event/{ev}", {"EndDateTime": "2026-11-03T11:00:00.000+0000", "DurationInMinutes": 5}
    )
    final = await state()
    assert final["DurationInMinutes"] == 120 and final["EndDateTime"] == "2026-11-03T11:00:00.000+0000"
    assert final["ShowAs"] == "Busy"  # the default for new events


# -- restricted deletes and hierarchy loops --------------------------------------------------------------------


async def test_records_in_use_cannot_be_deleted(admin: Actor) -> None:
    item = (
        await admin.rows(
            "SELECT Id, Product2Id, PricebookEntryId, OpportunityId FROM OpportunityLineItem LIMIT 1"
        )
    )[0]
    product = await admin.delete(f"/sobjects/Product2/{item['Product2Id']}")
    assert code_of(product, 400) == "DELETE_FAILED" and "opportunity products" in product.json()[0]["message"]
    entry = await admin.delete(f"/sobjects/PricebookEntry/{item['PricebookEntryId']}")
    assert code_of(entry, 400) == "DELETE_FAILED"
    opp = (await admin.rows(f"SELECT Pricebook2Id FROM Opportunity WHERE Id = '{item['OpportunityId']}'"))[0]
    book = await admin.delete(f"/sobjects/Pricebook2/{opp['Pricebook2Id']}")
    assert code_of(book, 400) == "DELETE_FAILED"
    standard = (await admin.rows("SELECT Id FROM Pricebook2 WHERE IsStandard = true"))[0]["Id"]
    assert (
        "standard price book" in (await admin.delete(f"/sobjects/Pricebook2/{standard}")).json()[0]["message"]
    )
    # nothing was half-deleted by the refused requests
    assert await admin.ids(f"SELECT Id FROM Product2 WHERE Id = '{item['Product2Id']}'")
    assert await admin.ids(f"SELECT Id FROM PricebookEntry WHERE Id = '{item['PricebookEntryId']}'")
    # an unused product goes away together with its price book entries
    spare = await admin.create("Product2", {"Name": "Spare product", "IsActive": True})
    assert (await admin.delete(f"/sobjects/Product2/{spare}")).status_code == 204


async def test_roles_in_use_cannot_be_deleted(admin: Actor) -> None:
    role = (await admin.rows("SELECT Id FROM UserRole WHERE Name = 'Managing Partner'"))[0]["Id"]
    r = await admin.delete(f"/sobjects/UserRole/{role}")
    assert code_of(r, 400) == "DELETE_FAILED"
    fresh = await admin.create("UserRole", {"Name": "Temp Role", "DeveloperName": "Temp_Role"})
    assert (await admin.delete(f"/sobjects/UserRole/{fresh}")).status_code == 204


async def test_hierarchies_may_not_loop(admin: Actor) -> None:
    a = await admin.create("Account", {"Name": "Loop A"})
    b = await admin.create("Account", {"Name": "Loop B", "ParentId": a})
    c = await admin.create("Account", {"Name": "Loop C", "ParentId": b})
    self_ref = await admin.patch(f"/sobjects/Account/{a}", {"ParentId": a})
    assert code_of(self_ref, 400) == "CIRCULAR_DEPENDENCY"
    loop = await admin.patch(f"/sobjects/Account/{a}", {"ParentId": c})
    assert code_of(loop, 400) == "CIRCULAR_DEPENDENCY" and loop.json()[0]["fields"] == ["ParentId"]
    assert (await admin.patch(f"/sobjects/Account/{c}", {"ParentId": a})).status_code == 204  # still a tree
    roles = await admin.rows("SELECT Id, ParentRoleId FROM UserRole WHERE ParentRoleId = null")
    leaf = (await admin.rows(f"SELECT Id FROM UserRole WHERE ParentRoleId = '{roles[0]['Id']}'"))[0]["Id"]
    assert code_of(
        await admin.patch(f"/sobjects/UserRole/{roles[0]['Id']}", {"ParentRoleId": leaf}), 400
    ) == ("CIRCULAR_DEPENDENCY")


# -- relationship lookups ----------------------------------------------------------------------------------------


async def test_relationship_lookups_need_an_external_id_field(admin: Actor) -> None:
    bad = await admin.post(
        "/sobjects/Contact", {"LastName": "Lookup", "Account": {"Name": "Northwind Logistics"}}
    )
    assert code_of(bad, 400) == "INVALID_FIELD" and "external ID field" in bad.json()[0]["message"]
    ok = await admin.post(
        "/sobjects/Contact", {"LastName": "Lookup", "Account": {"QuickBooks_Customer_Id__c": "2"}}
    )
    assert ok.status_code == 201
    both = await admin.post(
        "/sobjects/Contact",
        {"LastName": "Both", "AccountId": None, "Account": {"QuickBooks_Customer_Id__c": "2"}},
    )
    assert code_of(both, 400) == "INVALID_FIELD" and "both" in both.json()[0]["message"].lower()
    missing = await admin.post(
        "/sobjects/Contact", {"LastName": "Nobody", "Account": {"QuickBooks_Customer_Id__c": "no-such"}}
    )
    assert code_of(missing, 400) == "INVALID_FIELD" and "not found" in missing.json()[0]["message"]


async def test_relationship_lookups_do_not_see_records_the_caller_cannot_read(
    admin: Actor, actor: Any
) -> None:
    hannah = await actor(HANNAH)
    visible = set(await hannah.ids("SELECT Id FROM Opportunity"))
    hidden = next(o["Id"] for o in await admin.rows("SELECT Id FROM Opportunity") if o["Id"] not in visible)
    task = await hannah.post("/sobjects/Task", {"Subject": "x", "WhatId": hidden})
    assert code_of(task, 400) == "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY"


async def test_activities_are_visible_through_either_related_record(admin: Actor, actor: Any) -> None:
    hannah = await actor(HANNAH)
    visible = set(await hannah.ids("SELECT Id FROM Opportunity"))
    hidden = next(o["Id"] for o in await admin.rows("SELECT Id FROM Opportunity") if o["Id"] not in visible)
    contact = (await hannah.rows("SELECT Id FROM Contact LIMIT 1"))[0]["Id"]
    task = await admin.create("Task", {"Subject": "Two parents", "WhatId": hidden, "WhoId": contact})
    assert task in await hannah.ids("SELECT Id FROM Task")  # the contact in Who is enough
    only_what = await admin.create("Task", {"Subject": "One parent", "WhatId": hidden})
    assert only_what not in await hannah.ids("SELECT Id FROM Task")


# -- SOAP ---------------------------------------------------------------------------------------------------------


async def test_soap_login_rejects_unsupported_versions(client: httpx.AsyncClient) -> None:
    from .conftest import soap_login_xml

    body = soap_login_xml("arjun.mehta@halcyonadvisory.example.crm", "Halcyon2026!")
    for version in ("10.0", "99.0", "7.0", "abc"):
        r = await client.post(
            f"{LOGIN}/services/Soap/u/{version}",
            content=body,
            headers={"Content-Type": "text/xml", "SOAPAction": "login"},
        )
        assert r.status_code == 500 and "UNSUPPORTED_API_VERSION" in r.text, version
    ok = await client.post(
        f"{LOGIN}/services/Soap/u/62.0",
        content=body,
        headers={"Content-Type": "text/xml", "SOAPAction": "login"},
    )
    assert ok.status_code == 200 and "<sessionId>" in ok.text


# -- seeded data agrees with the describe metadata ---------------------------------------------------------------------


async def test_seeded_values_respect_their_field_definitions(admin: Actor, client: httpx.AsyncClient) -> None:
    fiscal = await admin.rows("SELECT Fiscal FROM Opportunity WHERE Fiscal != NULL")
    assert fiscal and all(len(r["Fiscal"]) <= 6 for r in fiscal)  # "2026 4"
    describe = (await admin.get("/sobjects/Opportunity/describe")).json()
    assert next(f for f in describe["fields"] if f["name"] == "Fiscal")["length"] == 6
    for sobject in ("UserRole", "Profile", "OpportunityStage", "LeadStatus", "CaseStatus"):
        fields = [f["name"] for f in (await admin.get(f"/sobjects/{sobject}/describe")).json()["fields"]]
        audit = [f for f in ("CreatedById", "LastModifiedById") if f in fields]
        assert audit, sobject
        rows = await admin.rows(f"SELECT {', '.join(audit)} FROM {sobject}")
        assert rows and all(all(r[f] for f in audit) for r in rows), sobject
    now = "2026-10-16"
    for camp in await admin.rows("SELECT Name, CreatedDate, StartDate, EndDate FROM Campaign"):
        assert camp["CreatedDate"][:10] <= now, camp["Name"]
        assert camp["StartDate"] <= camp["EndDate"]
    show_as = next(
        f for f in (await admin.get("/sobjects/Event/describe")).json()["fields"] if f["name"] == "ShowAs"
    )
    assert show_as["defaultedOnCreate"] is True and show_as["defaultValue"] == "Busy"
    states = {r["ShowAs"] for r in await admin.rows("SELECT ShowAs FROM Event")}
    assert states <= {"Busy", "Free", "OutOfOffice"}


async def test_campaign_rollups_start_at_zero_and_follow_members_and_opportunities(admin: Actor) -> None:
    camp = await admin.create("Campaign", {"Name": "Rollup test", "IsActive": True})
    row = (await admin.rows(f"SELECT FIELDS(STANDARD) FROM Campaign WHERE Id = '{camp}' LIMIT 1"))[0]
    assert row["NumberOfLeads"] == 0 and row["NumberOfContacts"] == 0 and row["AmountWonOpportunities"] == 0
    lead = await admin.create("Lead", {"LastName": "Prospect", "Company": "Rollup Inc"})
    contact = (await admin.rows("SELECT Id FROM Contact LIMIT 1"))[0]["Id"]
    await admin.create("CampaignMember", {"CampaignId": camp, "LeadId": lead, "Status": "Responded"})
    member = await admin.create("CampaignMember", {"CampaignId": camp, "ContactId": contact})
    opp = await admin.create(
        "Opportunity",
        {
            "Name": "Campaign deal",
            "StageName": "Closed Won",
            "CloseDate": "2026-10-01",
            "Amount": 5000,
            "CampaignId": camp,
        },
    )

    async def rollup() -> dict[str, Any]:
        return (
            await admin.rows(
                "SELECT NumberOfLeads, NumberOfContacts, NumberOfResponses, NumberOfOpportunities, "
                f"NumberOfWonOpportunities, AmountAllOpportunities, AmountWonOpportunities FROM Campaign WHERE Id = '{camp}'"
            )
        )[0]

    after = await rollup()
    assert (after["NumberOfLeads"], after["NumberOfContacts"], after["NumberOfResponses"]) == (1, 1, 1)
    assert after["NumberOfOpportunities"] == 1 and after["NumberOfWonOpportunities"] == 1
    assert after["AmountAllOpportunities"] == 5000 and after["AmountWonOpportunities"] == 5000
    await admin.delete(f"/sobjects/CampaignMember/{member}")
    await admin.delete(f"/sobjects/Opportunity/{opp}")
    final = await rollup()
    assert final["NumberOfContacts"] == 0 and final["NumberOfOpportunities"] == 0
    assert final["AmountAllOpportunities"] == 0 and final["NumberOfLeads"] == 1


# -- control actions validate their payloads ------------------------------------------------------------------------------


async def test_control_actions_reject_malformed_payloads(client: httpx.AsyncClient) -> None:
    def action(name: str) -> str:
        return f"{BASE}/_emulator/actions/{name}"

    for payload in (
        {"user": 5},
        {"user": "p035", "expires_in": "soon"},
        {"user": "p035", "expires_in": -5},
        {"user": "p035", "scopes": "api"},
        {"user": "p035", "scopes": [1, 2]},
        {"user": "p035", "client_id": {"a": 1}},
    ):
        r = await client.post(action("mint_token"), json=payload)
        assert r.status_code in (400, 404), payload
    for payload in ({"max": "x"}, {"used": [1]}, {"remaining": -1}, {"max": 0}):
        assert (await client.post(action("set_api_usage"), json=payload)).status_code == 400, payload
    assert (await client.post(action("expire_sessions"), json={"user": 7})).status_code == 400
    cases = (
        {"object": "Account", "url": "ftp://x.example/hook"},
        {"object": "Account", "url": "https://x.example/hook", "events": "create"},
        {"object": "Account", "url": "https://x.example/hook", "events": ["explode"]},
        {"object": "Account", "url": "https://x.example/hook", "fields": ["Nonsense"]},
        {"object": "Account", "url": "https://x.example/hook", "fields": [1]},
        {"object": ["Account"], "url": "https://x.example/hook"},
        {"object": "Account", "url": "https://x.example/hook", "name": 7},
    )
    for payload in cases:
        r = await client.post(action("configure_outbound_message"), json=payload)
        assert r.status_code == 400, payload
    ok = await client.post(
        action("configure_outbound_message"),
        json={
            "object": "Account",
            "url": "https://hooks.example/sf",
            "events": ["Create"],
            "fields": ["name", "Industry"],
        },
    )
    assert ok.status_code == 200
    assert ok.json()["events"] == ["create"] and ok.json()["fields"] == ["Name", "Industry"]


async def test_convert_lead_action_checks_its_references(client: httpx.AsyncClient, admin: Actor) -> None:
    lead = await admin.create("Lead", {"LastName": "Convertible", "Company": "Convert Co"})
    convert = f"{BASE}/_emulator/actions/convert_lead"
    for payload in (
        {"lead_id": 5},
        {"lead_id": lead, "account_id": "001000000000000AAA"},
        {"lead_id": lead, "account_id": 7},
        {"lead_id": lead, "contact_id": lead},
        {"lead_id": lead, "converted_status": "Open - Not Contacted"},
        {"lead_id": lead, "opportunity_name": 5},
    ):
        r = await client.post(convert, json=payload)
        assert r.status_code in (400, 404), payload
    assert not (await admin.rows(f"SELECT IsConverted FROM Lead WHERE Id = '{lead}'"))[0]["IsConverted"]
    account = (await admin.rows("SELECT Id FROM Account LIMIT 1"))[0]["Id"]
    ok = await client.post(
        convert, json={"lead_id": lead, "account_id": account, "do_not_create_opportunity": True}
    )
    assert ok.status_code == 200 and ok.json()["accountId"] == account


# -- OAuth ------------------------------------------------------------------------------------------------------------------


async def authorize(client: httpx.AsyncClient, **extra: str) -> httpx.Response:
    params = {
        "response_type": "code",
        "client_id": "3MVG9-web",
        "redirect_uri": "https://consumer.example/callback",
        "state": "st-1",
        "login_hint": "arjun.mehta@halcyonadvisory.example.crm",
        **extra,
    }
    return await client.get(f"{LOGIN}/services/oauth2/authorize", params=params, follow_redirects=False)


async def test_implicit_flow_only_returns_a_refresh_token_when_asked(client: httpx.AsyncClient) -> None:
    plain = await authorize(client, response_type="token")
    fragment = parse_qs(urlsplit(plain.headers["location"]).fragment)
    assert "refresh_token" not in fragment and fragment["access_token"]
    asked = await authorize(client, response_type="token", scope="api refresh_token")
    fragment = parse_qs(urlsplit(asked.headers["location"]).fragment)
    assert fragment["refresh_token"][0].startswith("5Aep861")
    code_flow = await authorize(client)  # the web-server flow keeps its default refresh token
    assert parse_qs(urlsplit(code_flow.headers["location"]).query)["code"]


async def test_pkce_challenge_method_must_be_s256(client: httpx.AsyncClient) -> None:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(b"v" * 50).digest()).rstrip(b"=").decode()
    for extra in (
        {"code_challenge": challenge, "code_challenge_method": "plain"},
        {"code_challenge_method": "S256"},
        {"code_challenge": "short", "code_challenge_method": "S256"},
    ):
        r = await authorize(client, **extra)
        assert r.status_code == 302, extra
        assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["invalid_request"], extra
    # a challenge without an explicit method is treated as S256, which is all Salesforce supports
    r = await authorize(client, code_challenge=challenge)
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    token = await client.post(
        f"{LOGIN}/services/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "3MVG9-web",
            "client_secret": "s",
            "redirect_uri": "https://consumer.example/callback",
            "code_verifier": "v" * 50,
        },
    )
    assert token.status_code == 200, token.text


# -- SOQL guard rails ----------------------------------------------------------------------------------------------------------------


async def test_soql_polymorphic_type_and_attribute_typing(admin: Actor) -> None:
    owner = (await admin.rows("SELECT Owner.Name, Owner.Type FROM Case LIMIT 1"))[0]["Owner"]
    assert owner["Type"] == "User" and owner["attributes"]["type"] == "Name"
    plain = (await admin.rows("SELECT Owner.Name FROM Account LIMIT 1"))[0]["Owner"]
    assert plain["attributes"]["type"] == "User"  # a single-target lookup keeps its concrete type
    assert ARJUN  # silence the unused-import check for the shared constant
