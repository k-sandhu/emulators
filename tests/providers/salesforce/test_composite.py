"""Composite requests, batch, tree inserts and sObject collections."""

from __future__ import annotations

from typing import Any

from .conftest import Actor

HALTED = {
    "errorCode": "PROCESSING_HALTED",
    "message": "The transaction was rolled back since another operation in the same transaction failed.",
}


def sub(method: str, url: str, ref: str, body: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"method": method, "url": f"/services/data/v62.0{url}", "referenceId": ref}
    if body is not None:
        out["body"] = body
    return out


async def test_composite_chains_references_across_subrequests(admin: Actor) -> None:
    body = {
        "allOrNone": True,
        "compositeRequest": [
            sub("POST", "/sobjects/Account", "acct", {"Name": "Composite Co", "Industry": "Retail"}),
            sub("POST", "/sobjects/Contact", "con", {"LastName": "Smith", "AccountId": "@{acct.id}"}),
            sub("GET", "/query?q=SELECT+Id,Name+FROM+Account+WHERE+Id='@{acct.id}'", "q"),
            sub("GET", "/sobjects/Contact/@{con.id}?fields=Id,AccountId", "getContact"),
            sub(
                "POST",
                "/sobjects/Opportunity",
                "opp",
                {
                    "Name": "@{q.records[0].Name} deal",
                    "StageName": "Prospecting",
                    "CloseDate": "2027-01-01",
                    "AccountId": "@{q.records[0].Id}",
                },
            ),
            sub("PATCH", "/sobjects/Account/@{acct.id}", "patch", {"Rating": "Hot"}),
            sub("DELETE", "/sobjects/Contact/@{con.id}", "del"),
        ],
    }
    r = await admin.post("/composite", body)
    assert r.status_code == 200, r.text
    out = r.json()["compositeResponse"]
    assert [o["referenceId"] for o in out] == ["acct", "con", "q", "getContact", "opp", "patch", "del"]
    assert [o["httpStatusCode"] for o in out] == [201, 201, 200, 200, 201, 204, 204]
    acct = out[0]["body"]["id"]
    assert out[0]["httpHeaders"] == {"Location": f"/services/data/v62.0/sobjects/Account/{acct}"}
    assert out[1]["body"] == {"id": out[1]["body"]["id"], "success": True, "errors": []}
    assert out[2]["body"]["records"][0]["Name"] == "Composite Co"
    assert out[3]["body"]["AccountId"] == acct
    assert out[5]["body"] is None and out[6]["body"] is None
    opp = (await admin.get(f"/sobjects/Opportunity/{out[4]['body']['id']}")).json()
    assert opp["Name"] == "Composite Co deal" and opp["AccountId"] == acct
    assert (await admin.get(f"/sobjects/Account/{acct}")).json()["Rating"] == "Hot"
    assert (await admin.get(f"/sobjects/Contact/{out[1]['body']['id']}")).status_code == 404


async def test_all_or_none_rolls_everything_back(admin: Actor) -> None:
    body = {
        "allOrNone": True,
        "compositeRequest": [
            sub("POST", "/sobjects/Account", "acct", {"Name": "Doomed Composite"}),
            sub("POST", "/sobjects/Contact", "bad", {"FirstName": "no last name", "AccountId": "@{acct.id}"}),
            sub("POST", "/sobjects/Account", "never", {"Name": "Never Reached"}),
        ],
    }
    out = (await admin.post("/composite", body)).json()["compositeResponse"]
    assert out[0]["httpStatusCode"] == 400 and out[0]["body"] == [HALTED]
    assert out[1]["httpStatusCode"] == 400
    assert out[1]["body"][0]["errorCode"] == "REQUIRED_FIELD_MISSING" and out[1]["body"][0]["fields"] == [
        "LastName"
    ]
    assert out[2]["body"] == [HALTED]
    assert (
        await admin.query("SELECT COUNT() FROM Account WHERE Name IN ('Doomed Composite', 'Never Reached')")
    )["totalSize"] == 0


async def test_without_all_or_none_each_subrequest_stands_alone(admin: Actor) -> None:
    body = {
        "allOrNone": False,
        "compositeRequest": [
            sub("POST", "/sobjects/Account", "a", {"Name": "Survivor"}),
            sub("POST", "/sobjects/Contact", "bad", {"FirstName": "x"}),
            sub("POST", "/sobjects/Contact", "dependent", {"LastName": "Orphan", "AccountId": "@{bad.id}"}),
            sub("POST", "/sobjects/Account", "b", {"Name": "Also Survivor"}),
        ],
    }
    out = (await admin.post("/composite", body)).json()["compositeResponse"]
    assert [o["httpStatusCode"] for o in out] == [201, 400, 400, 201]
    assert out[2]["body"][0]["errorCode"] == "INVALID_REFERENCE"
    assert (await admin.query("SELECT COUNT() FROM Account WHERE Name IN ('Survivor', 'Also Survivor')"))[
        "totalSize"
    ] == 2


async def test_composite_validation(admin: Actor) -> None:
    too_many = {"compositeRequest": [sub("GET", "/sobjects/Account/describe", f"r{i}") for i in range(26)]}
    r = await admin.post("/composite", too_many)
    assert r.status_code == 400 and r.json()[0]["errorCode"] == "INVALID_INPUT"
    duplicate = {"compositeRequest": [sub("GET", "/limits", "same"), sub("GET", "/limits", "same")]}
    assert (await admin.post("/composite", duplicate)).status_code == 400
    assert (await admin.post("/composite", {"compositeRequest": []})).status_code == 400
    ok25 = {"compositeRequest": [sub("GET", "/sobjects/Account/describe", f"r{i}") for i in range(25)]}
    assert len((await admin.post("/composite", ok25)).json()["compositeResponse"]) == 25
    unknown = {"compositeRequest": [sub("GET", "/sobjects/Nope/001000000000000AAA", "x")]}
    got = (await admin.post("/composite", unknown)).json()["compositeResponse"][0]
    assert got["httpStatusCode"] == 404 and got["body"][0]["errorCode"] == "NOT_FOUND"
    mixed_version = {
        "compositeRequest": [
            {"method": "GET", "url": "/services/data/v50.0/sobjects/Account/describe", "referenceId": "v"}
        ]
    }
    assert (await admin.post("/composite", mixed_version)).json()["compositeResponse"][0][
        "httpStatusCode"
    ] == 200


async def test_composite_batch(admin: Actor) -> None:
    acct = (await admin.ids("SELECT Id FROM Account LIMIT 1"))[0]
    body = {
        "haltOnError": False,
        "batchRequests": [
            {"method": "GET", "url": f"v62.0/sobjects/Account/{acct}?fields=Name"},
            {"method": "POST", "url": "v62.0/sobjects/Account", "richInput": {"Name": "Batch Co"}},
            {"method": "GET", "url": "v62.0/query?q=SELECT+COUNT()+FROM+Account"},
            {"method": "DELETE", "url": "v62.0/sobjects/Account/001000000000000AAA"},
            {"method": "PATCH", "url": f"v62.0/sobjects/Account/{acct}", "richInput": {"Rating": "Cold"}},
        ],
    }
    out = (await admin.post("/composite/batch", body)).json()
    assert out["hasErrors"] is True
    statuses = [r["statusCode"] for r in out["results"]]
    assert statuses == [200, 201, 200, 404, 204]
    assert out["results"][0]["result"]["Name"] and out["results"][1]["result"]["success"] is True
    assert out["results"][2]["result"]["totalSize"] == 42
    assert out["results"][3]["result"][0]["errorCode"] == "NOT_FOUND" and out["results"][4]["result"] is None
    halted = {**body, "haltOnError": True}
    halted["batchRequests"] = [body["batchRequests"][3], body["batchRequests"][0]]
    out = (await admin.post("/composite/batch", halted)).json()
    assert [r["statusCode"] for r in out["results"]] == [404, 412]
    assert out["results"][1]["result"][0]["errorCode"] == "BATCH_PROCESSING_HALTED"
    clean = (
        await admin.post(
            "/composite/batch", {"batchRequests": [{"method": "GET", "url": "/services/data/v62.0/limits"}]}
        )
    ).json()
    assert clean["hasErrors"] is False
    too_many = {"batchRequests": [{"method": "GET", "url": "v62.0/limits"}] * 26}
    assert (await admin.post("/composite/batch", too_many)).status_code == 400


async def test_composite_tree_creates_nested_records(admin: Actor) -> None:
    body = {
        "records": [
            {
                "attributes": {"type": "Account", "referenceId": "acct1"},
                "Name": "Tree Co",
                "Contacts": {
                    "records": [
                        {"attributes": {"type": "Contact", "referenceId": "c1"}, "LastName": "Leaf"},
                        {
                            "attributes": {"type": "Contact", "referenceId": "c2"},
                            "LastName": "Leaf2",
                            "Email": "leaf2@tree.example",
                        },
                    ]
                },
                "Opportunities": {
                    "records": [
                        {
                            "attributes": {"type": "Opportunity", "referenceId": "o1"},
                            "Name": "Tree deal",
                            "StageName": "Prospecting",
                            "CloseDate": "2027-05-01",
                        }
                    ]
                },
            },
            {"attributes": {"type": "Account", "referenceId": "acct2"}, "Name": "Second Tree Co"},
        ]
    }
    r = await admin.post("/composite/tree/Account", body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["hasErrors"] is False
    assert [x["referenceId"] for x in out["results"]] == ["acct1", "c1", "c2", "o1", "acct2"]
    ids = {x["referenceId"]: x["id"] for x in out["results"]}
    contacts = await admin.rows(
        f"SELECT LastName FROM Contact WHERE AccountId = '{ids['acct1']}' ORDER BY LastName"
    )
    assert [c["LastName"] for c in contacts] == ["Leaf", "Leaf2"]
    assert (await admin.get(f"/sobjects/Opportunity/{ids['o1']}")).json()["AccountId"] == ids["acct1"]


async def test_composite_tree_is_all_or_nothing(admin: Actor) -> None:
    body = {
        "records": [
            {
                "attributes": {"type": "Account", "referenceId": "acct1"},
                "Name": "Rolled Back Tree",
                "Contacts": {
                    "records": [
                        {"attributes": {"type": "Contact", "referenceId": "c1"}, "FirstName": "no last"}
                    ]
                },
            }
        ]
    }
    r = await admin.post("/composite/tree/Account", body)
    assert r.status_code == 400
    out = r.json()
    assert out["hasErrors"] is True
    by_ref = {x["referenceId"]: x["errors"][0] for x in out["results"]}
    assert by_ref["c1"]["statusCode"] == "REQUIRED_FIELD_MISSING" and by_ref["c1"]["fields"] == ["LastName"]
    assert by_ref["acct1"]["statusCode"] == "PROCESSING_HALTED"
    assert (await admin.query("SELECT COUNT() FROM Account WHERE Name = 'Rolled Back Tree'"))[
        "totalSize"
    ] == 0
    no_ref = await admin.post(
        "/composite/tree/Account", {"records": [{"attributes": {"type": "Account"}, "Name": "x"}]}
    )
    assert no_ref.status_code == 400
    assert (await admin.post("/composite/tree/Nothing", body)).status_code == 400


async def test_collections_create_update_delete_retrieve(admin: Actor) -> None:
    created = (
        await admin.post(
            "/composite/sobjects",
            {
                "allOrNone": False,
                "records": [
                    {"attributes": {"type": "Account"}, "Name": "Bulk One"},
                    {"attributes": {"type": "Account"}},
                    {"attributes": {"type": "Contact"}, "LastName": "Bulk Contact"},
                ],
            },
        )
    ).json()
    assert [c["success"] for c in created] == [True, False, True]
    assert created[0]["errors"] == [] and created[0]["id"].startswith("001")
    assert created[1]["errors"] == [
        {
            "statusCode": "REQUIRED_FIELD_MISSING",
            "message": "Required fields are missing: [Name]",
            "fields": ["Name"],
        }
    ]
    ids = [created[0]["id"], created[2]["id"]]
    updated = (
        await admin.request(
            "PATCH",
            "/composite/sobjects",
            json={
                "allOrNone": True,
                "records": [
                    {"attributes": {"type": "Account"}, "id": ids[0], "Rating": "Hot"},
                    {"attributes": {"type": "Contact"}, "id": ids[1], "Title": "Boss"},
                ],
            },
        )
    ).json()
    assert [u["success"] for u in updated] == [True, True] and updated[0]["id"] == ids[0]
    fetched = (
        await admin.get(
            "/composite/sobjects/Account",
            params={"ids": f"{ids[0]},001000000000000AAA", "fields": "Id,Name,Rating"},
        )
    ).json()
    assert fetched[0] == {
        "attributes": {"type": "Account", "url": f"/services/data/v62.0/sobjects/Account/{ids[0]}"},
        "Id": ids[0],
        "Name": "Bulk One",
        "Rating": "Hot",
    }
    assert fetched[1] is None
    posted = (
        await admin.post("/composite/sobjects/Account", {"recordIds": [ids[0]], "fieldNames": ["Name"]})
    ).json()
    assert posted[0]["Name"] == "Bulk One"
    removed = (
        await admin.request(
            "DELETE", "/composite/sobjects", params={"ids": f"{ids[0]},{ids[1]},001000000000000AAA"}
        )
    ).json()
    assert [d["success"] for d in removed] == [True, True, False]
    assert removed[2]["errors"][0]["statusCode"] == "ENTITY_IS_DELETED"
    assert (await admin.get(f"/sobjects/Account/{ids[0]}")).status_code == 404


async def test_collections_all_or_none_semantics(admin: Actor) -> None:
    out = (
        await admin.post(
            "/composite/sobjects",
            {
                "allOrNone": True,
                "records": [
                    {"attributes": {"type": "Account"}, "Name": "Rolled A"},
                    {"attributes": {"type": "Account"}},
                    {"attributes": {"type": "Account"}, "Name": "Rolled B"},
                ],
            },
        )
    ).json()
    assert [o["success"] for o in out] == [False, False, False]
    assert out[0]["errors"][0]["statusCode"] == "ALL_OR_NONE_OPERATION_ROLLED_BACK"
    assert out[1]["errors"][0]["statusCode"] == "REQUIRED_FIELD_MISSING"
    assert (await admin.query("SELECT COUNT() FROM Account WHERE Name LIKE 'Rolled %'"))["totalSize"] == 0
    survivors = await admin.ids("SELECT Id FROM Account ORDER BY Name LIMIT 2")
    out = (
        await admin.request(
            "DELETE",
            "/composite/sobjects",
            params={"ids": f"{survivors[0]},001000000000000AAA", "allOrNone": "true"},
        )
    ).json()
    assert [o["success"] for o in out] == [False, False]
    assert (await admin.get(f"/sobjects/Account/{survivors[0]}")).status_code == 200


async def test_collection_limits_and_errors(admin: Actor) -> None:
    many = {"records": [{"attributes": {"type": "Account"}, "Name": f"A{i}"} for i in range(201)]}
    r = await admin.post("/composite/sobjects", many)
    assert r.status_code == 400 and r.json()[0]["errorCode"] == "EXCEEDED_ID_LIMIT"
    exactly = {"records": [{"attributes": {"type": "Account"}, "Name": f"Limit {i}"} for i in range(200)]}
    assert all(x["success"] for x in (await admin.post("/composite/sobjects", exactly)).json())
    assert (await admin.post("/composite/sobjects", {"records": []})).status_code == 400
    no_type = await admin.post("/composite/sobjects", {"records": [{"Name": "typeless"}]})
    assert no_type.status_code == 400
    bad_type = await admin.post(
        "/composite/sobjects", {"records": [{"attributes": {"type": "Zebra"}, "Name": "x"}]}
    )
    assert bad_type.status_code == 400 and bad_type.json()[0]["errorCode"] == "INVALID_TYPE"
    missing_id = (
        await admin.request(
            "PATCH",
            "/composite/sobjects",
            json={"records": [{"attributes": {"type": "Account"}, "Rating": "Hot"}]},
        )
    ).json()
    assert missing_id[0]["errors"][0]["statusCode"] == "MISSING_ARGUMENT"


async def test_collection_upsert_by_external_id(admin: Actor) -> None:
    out = (
        await admin.request(
            "PATCH",
            "/composite/sobjects/Account/QuickBooks_Customer_Id__c",
            json={
                "allOrNone": False,
                "records": [
                    {
                        "attributes": {"type": "Account"},
                        "QuickBooks_Customer_Id__c": "1",
                        "Industry": "Retail",
                    },
                    {
                        "attributes": {"type": "Account"},
                        "QuickBooks_Customer_Id__c": "NEW-1",
                        "Name": "Brand New",
                    },
                    {"attributes": {"type": "Account"}, "QuickBooks_Customer_Id__c": "NEW-2"},
                ],
            },
        )
    ).json()
    assert [o["success"] for o in out] == [True, True, False]
    assert out[0]["created"] is False and out[1]["created"] is True
    assert out[2]["errors"][0]["statusCode"] == "REQUIRED_FIELD_MISSING"
    first = (await admin.rows("SELECT Industry, Name FROM Account WHERE QuickBooks_Customer_Id__c = '1'"))[0]
    assert first["Industry"] == "Retail" and first["Name"] == "Northwind Logistics"


async def test_composite_requests_cannot_be_nested(admin: Actor) -> None:
    inner = {"compositeRequest": [sub("GET", "/limits", "x")]}
    body = {
        "compositeRequest": [
            {"method": "POST", "url": "/services/data/v62.0/composite", "referenceId": "n", "body": inner}
        ]
    }
    got = (await admin.post("/composite", body)).json()["compositeResponse"][0]
    assert got["httpStatusCode"] == 400 and got["body"][0]["errorCode"] == "INVALID_INPUT"
    batch = (
        await admin.post(
            "/composite/batch",
            {"batchRequests": [{"method": "POST", "url": "v62.0/composite", "richInput": inner}]},
        )
    ).json()
    assert batch["hasErrors"] is True and batch["results"][0]["statusCode"] == 400
