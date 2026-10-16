"""QBO query language, batch and change-data-capture."""

from __future__ import annotations

from tests.providers.quickbooks.conftest import API


async def q(client, h, text, expect=200):
    r = await client.get(f"{API}/query", params={"query": text}, headers=h)
    assert r.status_code == expect, r.text
    return r


async def test_query_operators_and_pagination(qb) -> None:
    client, h = qb
    r = await q(client, h, "select count(*) from Customer")
    active_count = r.json()["QueryResponse"]["totalCount"]
    assert active_count > 24  # clients + jobs, active only

    r = await q(client, h, "SELECT * FROM Customer WHERE Active IN (true, false) MAXRESULTS 1000")
    assert len(r.json()["QueryResponse"]["Customer"]) > active_count

    r = await q(client, h, "SELECT * FROM Item WHERE Name LIKE '%Audit%'")
    names = [i["Name"] for i in r.json()["QueryResponse"]["Item"]]
    assert "Audit & Assurance Services" in names

    r = await q(client, h, "SELECT * FROM Invoice WHERE Balance > 0 AND TotalAmt >= 1000 MAXRESULTS 3")
    assert all(i["Balance"] > 0 and i["TotalAmt"] >= 1000 for i in r.json()["QueryResponse"]["Invoice"])

    page1 = (await q(client, h, "SELECT * FROM Customer STARTPOSITION 1 MAXRESULTS 2")).json()[
        "QueryResponse"
    ]
    page2 = (await q(client, h, "SELECT * FROM Customer STARTPOSITION 3 MAXRESULTS 2")).json()[
        "QueryResponse"
    ]
    assert page1["startPosition"] == 1 and page1["maxResults"] == 2
    assert {c["Id"] for c in page1["Customer"]}.isdisjoint({c["Id"] for c in page2["Customer"]})

    r = await q(client, h, "SELECT DisplayName, Balance FROM Customer MAXRESULTS 2")
    row = r.json()["QueryResponse"]["Customer"][0]
    assert set(row) <= {"Id", "SyncToken", "DisplayName", "Balance"}

    r = await q(client, h, "SELECT * FROM Customer WHERE DisplayName = 'No Such Company'")
    assert r.json()["QueryResponse"] == {}

    r = await q(
        client,
        h,
        "SELECT * FROM Invoice WHERE MetaData.LastUpdatedTime > '2026-01-01T00:00:00-07:00' MAXRESULTS 5",
    )
    assert r.json()["QueryResponse"]["Invoice"]


async def test_query_orderby_and_post(qb) -> None:
    client, h = qb
    r = await q(client, h, "SELECT * FROM Invoice ORDERBY TxnDate DESC MAXRESULTS 5")
    dates = [i["TxnDate"] for i in r.json()["QueryResponse"]["Invoice"]]
    assert dates == sorted(dates, reverse=True)
    # POST with text body, as python-quickbooks does
    r = await client.post(
        f"{API}/query",
        headers={**h, "Content-Type": "application/text"},
        content="SELECT * FROM Term",
    )
    assert r.status_code == 200, r.text
    assert any(t["Name"] == "Net 30" for t in r.json()["QueryResponse"]["Term"])


async def test_query_errors(qb) -> None:
    client, h = qb
    r = await q(client, h, "SELECT * FROM Customer WHERE Active = true OR Active = false", expect=400)
    err = r.json()["Fault"]["Error"][0]
    assert err["code"] == "4000" and err["Message"] == "Error parsing query"
    r = await q(client, h, "SELECT FROM WHERE", expect=400)
    assert r.json()["Fault"]["Error"][0]["code"] == "4000"
    r = await q(client, h, "SELECT * FROM NotARealEntity", expect=400)
    assert r.json()["Fault"]["Error"][0]["code"] == "4000"


async def test_batch_mixed(qb) -> None:
    client, h = qb
    r = await client.post(
        f"{API}/batch",
        headers=h,
        json={
            "BatchItemRequest": [
                {"bId": "cust1", "operation": "create", "Customer": {"DisplayName": "Batch Customer"}},
                {"bId": "bad", "operation": "create", "Customer": {"CompanyName": "Missing name"}},
                {"bId": "count", "Query": "SELECT COUNT(*) FROM Invoice"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    items = {i["bId"]: i for i in r.json()["BatchItemResponse"]}
    assert items["cust1"]["Customer"]["DisplayName"] == "Batch Customer"
    assert items["bad"]["Fault"]["Error"][0]["code"] == "2020"
    assert items["count"]["QueryResponse"]["totalCount"] == 115

    cust = items["cust1"]["Customer"]
    r = await client.post(
        f"{API}/batch",
        headers=h,
        json={
            "BatchItemRequest": [
                {
                    "bId": "upd",
                    "operation": "update",
                    "Customer": {
                        "Id": cust["Id"],
                        "SyncToken": cust["SyncToken"],
                        "sparse": True,
                        "Notes": "via batch",
                    },
                }
            ]
        },
    )
    assert r.json()["BatchItemResponse"][0]["Customer"]["Notes"] == "via batch"

    r = await client.post(
        f"{API}/batch",
        headers=h,
        json={"BatchItemRequest": [{"bId": str(i), "Query": "SELECT * FROM Term"} for i in range(31)]},
    )
    assert r.status_code == 400


async def test_cdc(qb) -> None:
    client, h = qb
    created = (await client.post(f"{API}/customer", headers=h, json={"DisplayName": "CDC Customer"})).json()[
        "Customer"
    ]
    # delete an invoice so CDC shows a Deleted stub
    items = (
        await client.get(f"{API}/query", params={"query": "SELECT * FROM Item MAXRESULTS 1"}, headers=h)
    ).json()["QueryResponse"]["Item"]
    invoice = (
        await client.post(
            f"{API}/invoice",
            headers=h,
            json={
                "CustomerRef": {"value": created["Id"]},
                "Line": [
                    {
                        "DetailType": "SalesItemLineDetail",
                        "Amount": 10,
                        "SalesItemLineDetail": {
                            "ItemRef": {"value": items[0]["Id"]},
                            "Qty": 1,
                            "UnitPrice": 10,
                        },
                    }
                ],
            },
        )
    ).json()["Invoice"]
    await client.post(
        f"{API}/invoice",
        params={"operation": "delete"},
        headers=h,
        json={"Id": invoice["Id"], "SyncToken": invoice["SyncToken"]},
    )

    r = await client.get(
        f"{API}/cdc",
        params={"entities": "Customer,Invoice", "changedSince": "2026-10-01T00:00:00-07:00"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    responses = r.json()["CDCResponse"][0]["QueryResponse"]
    by_entity = {
        next(k for k in resp if k not in ("startPosition", "maxResults", "totalCount")): resp
        for resp in responses
    }
    assert any(c["Id"] == created["Id"] for c in by_entity["Customer"]["Customer"])
    assert by_entity["Customer"]["totalCount"] >= 1
    deleted = [i for i in by_entity["Invoice"]["Invoice"] if i.get("status") == "Deleted"]
    assert any(d["Id"] == invoice["Id"] for d in deleted)

    r = await client.get(
        f"{API}/cdc",
        params={"entities": "Customer", "changedSince": "2025-01-01T00:00:00-07:00"},
        headers=h,
    )
    assert r.status_code == 400  # > 30 days back
