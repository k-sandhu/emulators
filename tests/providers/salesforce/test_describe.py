"""Metadata: describeGlobal, describe, versions, resource map, limits."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .conftest import BASE, Actor


async def describe(admin: Actor, sobject: str) -> dict[str, Any]:
    r = await admin.get(f"/sobjects/{sobject}/describe")
    assert r.status_code == 200, r.text
    return r.json()


def field(d: dict[str, Any], name: str) -> dict[str, Any]:
    return next(f for f in d["fields"] if f["name"] == name)


async def test_versions_list_is_public_and_complete(client: httpx.AsyncClient) -> None:
    r = await client.get(f"{BASE}/services/data/")
    assert r.status_code == 200
    versions = r.json()
    assert [v["version"] for v in versions] == [f"{n}.0" for n in range(31, 67)]
    by_version = {v["version"]: v for v in versions}
    assert by_version["31.0"] == {"label": "Summer '14", "url": "/services/data/v31.0", "version": "31.0"}
    assert by_version["62.0"]["label"] == "Winter '25" and by_version["66.0"]["label"] == "Spring '26"
    assert (await client.get(f"{BASE}/services/data")).json() == versions


async def test_resource_map_and_limits(admin: Actor) -> None:
    body = (await admin.get("/")).json()
    for key in (
        "sobjects",
        "query",
        "queryAll",
        "search",
        "parameterizedSearch",
        "composite",
        "limits",
        "recent",
        "identity",
    ):
        assert key in body
    assert body["sobjects"] == "/services/data/v62.0/sobjects"
    assert body["identity"] == f"https://login.salesforce.com/id/00D5g0000049673EAA/{admin.user_id}"
    first = await admin.get("/limits")
    used = int(first.headers["sforce-limit-info"].split("=")[1].split("/")[0])
    second = await admin.get("/limits")
    assert int(second.headers["sforce-limit-info"].split("=")[1].split("/")[0]) == used + 1
    assert second.headers["sforce-limit-info"].endswith("/15000")
    daily = second.json()["DailyApiRequests"]
    assert daily["Max"] == 15000 and daily["Remaining"] == 15000 - (used + 1)
    assert {"DailyBulkApiBatches", "DataStorageMB", "SingleEmail", "MassEmail"} <= set(second.json())
    counts = (await admin.get("/limits/recordCount", params={"sObjects": "Account,Contact"})).json()
    assert counts == {"sObjects": [{"count": 41, "name": "Account"}, {"count": 89, "name": "Contact"}]}


async def test_request_limit_exceeded(admin: Actor, client: httpx.AsyncClient) -> None:
    r = await client.post(f"{BASE}/_emulator/actions/set_api_usage", json={"remaining": 2, "max": 15000})
    assert r.json() == {"used": 14998, "max": 15000, "remaining": 2}
    assert (await admin.get("/limits")).status_code == 200
    assert (await admin.get("/limits")).status_code == 200
    blocked = await admin.get("/limits")
    assert blocked.status_code == 403
    assert blocked.json() == [
        {"message": "TotalRequests Limit exceeded.", "errorCode": "REQUEST_LIMIT_EXCEEDED"}
    ]
    await client.post(f"{BASE}/_emulator/actions/set_api_usage", json={"used": 0})
    assert (await admin.get("/limits")).status_code == 200
    await client.post(f"{BASE}/_emulator/reset")
    token = (await client.post(f"{BASE}/_emulator/actions/mint_token", json={"user": "p035"})).json()[
        "access_token"
    ]
    assert (
        await client.get(f"{BASE}/services/data/v62.0/limits", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 200


async def test_describe_global(admin: Actor) -> None:
    body = (await admin.get("/sobjects")).json()
    assert body["encoding"] == "UTF-8" and body["maxBatchSize"] == 200
    objs = {o["name"]: o for o in body["sobjects"]}
    assert {"Account", "Contact", "Opportunity", "Lead", "Case", "Task", "Event", "Campaign", "CampaignMember", "User",
            "UserRole", "Profile", "Product2", "Pricebook2", "PricebookEntry", "OpportunityLineItem",
            "OpportunityContactRole", "Organization", "Engagement__c"} <= set(objs)  # fmt: skip
    account = objs["Account"]
    assert (
        account["keyPrefix"] == "001"
        and account["label"] == "Account"
        and account["labelPlural"] == "Accounts"
    )
    assert (account["queryable"], account["createable"], account["updateable"], account["deletable"], account["custom"]) == (
        True, True, True, True, False,
    )  # fmt: skip
    assert account["urls"] == {
        "sobject": "/services/data/v62.0/sobjects/Account",
        "describe": "/services/data/v62.0/sobjects/Account/describe",
        "rowTemplate": "/services/data/v62.0/sobjects/Account/{ID}",
    }
    engagement = objs["Engagement__c"]
    assert (
        engagement["custom"] is True
        and engagement["keyPrefix"] == "a0X"
        and engagement["label"] == "Engagement"
    )
    assert objs["Organization"]["createable"] is False
    assert set(account) >= {"activateable", "customSetting", "deprecatedAndHidden", "feedEnabled", "layoutable", "mergeable",
                            "mruEnabled", "replicateable", "retrieveable", "searchable", "triggerable", "undeletable"}  # fmt: skip


async def test_describe_account_fields(admin: Actor) -> None:
    d = await describe(admin, "Account")
    assert d["name"] == "Account" and d["keyPrefix"] == "001" and d["custom"] is False
    keys = {
        "fields",
        "childRelationships",
        "recordTypeInfos",
        "urls",
        "supportedScopes",
        "actionOverrides",
        "label",
        "labelPlural",
    }
    assert keys <= set(d)
    ident = field(d, "Id")
    assert (ident["type"], ident["length"], ident["nillable"], ident["createable"], ident["updateable"]) == (
        "id",
        18,
        False,
        False,
        False,
    )
    assert ident["defaultedOnCreate"] is True and ident["idLookup"] is True and ident["soapType"] == "tns:ID"
    name = field(d, "Name")
    assert (name["type"], name["length"], name["nameField"], name["nillable"], name["createable"]) == (
        "string",
        255,
        True,
        False,
        True,
    )
    assert name["label"] == "Account Name" and name["byteLength"] == 765 and name["soapType"] == "xsd:string"
    owner = field(d, "OwnerId")
    assert (
        owner["type"] == "reference"
        and owner["referenceTo"] == ["User"]
        and owner["relationshipName"] == "Owner"
    )
    assert (
        owner["nillable"] is False
        and owner["defaultedOnCreate"] is True
        and owner["polymorphicForeignKey"] is False
    )
    typ = field(d, "Type")
    assert typ["type"] == "picklist" and typ["restrictedPicklist"] is False
    assert {
        "value": "Customer",
        "label": "Customer",
        "active": True,
        "defaultValue": False,
        "validFor": None,
    } in typ["picklistValues"]
    revenue = field(d, "AnnualRevenue")
    assert (revenue["type"], revenue["precision"], revenue["scale"], revenue["soapType"]) == (
        "currency",
        18,
        0,
        "xsd:double",
    )
    employees = field(d, "NumberOfEmployees")
    assert (employees["type"], employees["digits"], employees["soapType"]) == ("int", 9, "xsd:int")
    assert (
        field(d, "BillingAddress")["type"] == "address" and field(d, "BillingAddress")["filterable"] is False
    )
    assert field(d, "BillingCity")["compoundFieldName"] == "BillingAddress"
    assert field(d, "Description")["type"] == "textarea" and field(d, "Description")["filterable"] is False
    assert field(d, "Website")["type"] == "url" and field(d, "Phone")["type"] == "phone"
    assert field(d, "CreatedDate")["type"] == "datetime" and field(d, "LastActivityDate")["type"] == "date"
    assert field(d, "IsDeleted")["type"] == "boolean" and field(d, "IsDeleted")["defaultedOnCreate"] is True
    qb = field(d, "QuickBooks_Customer_Id__c")
    assert (
        qb["custom"] is True and qb["externalId"] is True and qb["unique"] is True and qb["idLookup"] is True
    )
    lines = field(d, "Service_Lines__c")
    assert lines["type"] == "multipicklist" and lines["restrictedPicklist"] is True
    assert field(d, "Client_Since__c")["type"] == "date"
    contacts = next(c for c in d["childRelationships"] if c["relationshipName"] == "Contacts")
    assert (contacts["childSObject"], contacts["field"], contacts["cascadeDelete"]) == (
        "Contact",
        "AccountId",
        True,
    )
    names = {c["relationshipName"] for c in d["childRelationships"]}
    assert {
        "Contacts",
        "Opportunities",
        "Cases",
        "Tasks",
        "Events",
        "Engagements__r",
        "ChildAccounts",
    } <= names
    master = d["recordTypeInfos"][0]
    assert (
        master["master"] is True
        and master["recordTypeId"] == "012000000000000AAA"
        and master["developerName"] == "Master"
    )
    assert {"label": "My accounts", "name": "mine"} in d["supportedScopes"]
    assert d["urls"]["describe"] == "/services/data/v62.0/sobjects/Account/describe"
    every = set(field(d, "Name"))
    assert {"aggregatable", "calculated", "caseSensitive", "defaultValue", "digits", "externalId", "filterable", "groupable",
            "idLookup", "inlineHelpText", "label", "length", "nameField", "namePointing", "nillable", "permissionable",
            "picklistValues", "precision", "referenceTo", "relationshipName", "restrictedPicklist", "scale", "soapType",
            "sortable", "type", "unique", "updateable", "createable", "custom", "defaultedOnCreate"} <= every  # fmt: skip


async def test_describe_custom_object_engagement(admin: Actor) -> None:
    d = await describe(admin, "Engagement__c")
    assert (d["custom"], d["keyPrefix"], d["label"], d["labelPlural"], d["name"]) == (
        True,
        "a0X",
        "Engagement",
        "Engagements",
        "Engagement__c",
    )
    account = field(d, "Account__c")
    assert (account["type"], account["referenceTo"], account["relationshipName"], account["custom"]) == (
        "reference",
        ["Account"],
        "Account__r",
        True,
    )
    lead = field(d, "Lead__c")
    assert lead["referenceTo"] == ["User"] and lead["relationshipName"] == "Lead__r"
    service = field(d, "Service__c")
    assert service["type"] == "picklist" and service["restrictedPicklist"] is True
    assert "Audit" in [v["value"] for v in service["picklistValues"]]
    for name, kind in (("Start_Date__c", "date"), ("End_Date__c", "date"), ("Budget_Hours__c", "double"), ("Hourly_Rate__c", "currency"),
                       ("Fixed_Fee__c", "currency"), ("Billing_Type__c", "picklist"), ("Status__c", "picklist")):  # fmt: skip
        assert field(d, name)["type"] == kind and field(d, name)["custom"] is True, name
    assert all(f["custom"] for f in d["fields"] if f["name"].endswith("__c"))
    assert field(d, "Name")["nameField"] is True and field(d, "OwnerId")["referenceTo"] == ["User"]
    assert field(d, "Engagement_Code__c")["externalId"] is True


async def test_polymorphic_and_special_fields(admin: Actor) -> None:
    task = await describe(admin, "Task")
    who, what = field(task, "WhoId"), field(task, "WhatId")
    assert (
        who["referenceTo"] == ["Contact", "Lead"]
        and who["namePointing"] is True
        and who["relationshipName"] == "Who"
    )
    assert "Opportunity" in what["referenceTo"] and what["relationshipName"] == "What"
    assert field(task, "Subject")["type"] == "combobox" and field(task, "Subject")["nameField"] is True
    lead = await describe(admin, "Lead")
    assert (
        field(lead, "OwnerId")["referenceTo"] == ["Group", "User"]
        and field(lead, "Status")["restrictedPicklist"] is True
    )
    assert field(lead, "Name")["createable"] is False and field(lead, "Name")["calculated"] is True
    opp = await describe(admin, "Opportunity")
    stage = field(opp, "StageName")
    assert stage["restrictedPicklist"] is True and stage["nillable"] is False
    assert [v["value"] for v in stage["picklistValues"]][-2:] == ["Closed Won", "Closed Lost"]
    assert field(opp, "Engagement__c")["relationshipName"] == "Engagement__r"
    case = await describe(admin, "Case")
    assert field(case, "CaseNumber")["autoNumber"] is True and field(case, "CaseNumber")["nameField"] is True
    user = await describe(admin, "User")
    assert (
        field(user, "Username")["unique"] is True
        and field(user, "ManagerId")["relationshipName"] == "Manager"
    )


async def test_field_type_vocabulary_is_covered(admin: Actor) -> None:
    types: set[str] = set()
    for name in (
        "Account",
        "Contact",
        "Opportunity",
        "Lead",
        "Case",
        "Task",
        "Event",
        "Campaign",
        "User",
        "Organization",
        "Engagement__c",
    ):
        types |= {f["type"] for f in (await describe(admin, name))["fields"]}
    assert {"id", "string", "textarea", "email", "phone", "url", "picklist", "multipicklist", "reference", "boolean",
            "currency", "double", "percent", "int", "date", "datetime", "address", "combobox"} <= types  # fmt: skip


@pytest.mark.parametrize("kind", ["layouts", "compactLayouts", "approvalLayouts"])
async def test_layout_describes(admin: Actor, kind: str) -> None:
    r = await admin.get(f"/sobjects/Account/describe/{kind}")
    assert r.status_code == 200
    body = r.json()
    if kind == "layouts":
        assert body["layouts"][0]["detailLayoutSections"] and body["recordTypeSelectorRequired"] == [False]
        single = await admin.get("/sobjects/Account/describe/layouts/012000000000000AAA")
        assert single.status_code == 200 and single.json()["editLayoutSections"]
        assert (await admin.get("/sobjects/Account/describe/layouts/012xx0000000000AAA")).status_code == 404
    elif kind == "compactLayouts":
        assert body["compactLayouts"][0]["objectType"] == "Account"
    else:
        assert body == {"approvalLayouts": []}


async def test_unknown_resources_are_404_in_the_vendor_envelope(admin: Actor) -> None:
    for path in ("/tooling/describe", "/connect/communities", "/chatter/feeds"):
        r = await admin.get(path)
        assert r.status_code == 404 and r.json()[0]["errorCode"] == "NOT_FOUND", path
