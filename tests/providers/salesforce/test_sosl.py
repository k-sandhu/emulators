"""SOSL (/search) and /parameterizedSearch."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import Actor


async def find(admin: Actor, sosl: str) -> list[dict[str, Any]]:
    r = await admin.get("/search", params={"q": sosl})
    assert r.status_code == 200, r.text
    body = r.json()
    assert list(body) == ["searchRecords"]
    return body["searchRecords"]


def types(records: list[dict[str, Any]]) -> list[str]:
    return [r["attributes"]["type"] for r in records]


async def test_find_all_fields_with_returning_lists(admin: Actor) -> None:
    got = await find(
        admin, "FIND {Northwind} IN ALL FIELDS RETURNING Account(Id, Name), Contact(Id, Name, Email)"
    )
    assert got[0]["attributes"] == {
        "type": "Account",
        "url": f"/services/data/v62.0/sobjects/Account/{got[0]['Id']}",
    }
    assert list(got[0]) == ["attributes", "Id", "Name"]
    assert [r["Name"] for r in got if r["attributes"]["type"] == "Account"] == ["Northwind Logistics"]
    contacts = await find(admin, "FIND {northwind*} IN EMAIL FIELDS RETURNING Contact(Id, Name, Email)")
    assert len(contacts) == 3 and all("northwindlogistics" in c["Email"] for c in contacts)
    assert list(contacts[0]) == ["attributes", "Id", "Name", "Email"]


async def test_default_returning_covers_searchable_objects_with_ids(admin: Actor) -> None:
    got = await find(admin, "FIND {Foxglove}")
    assert {"Account", "Opportunity"} <= set(types(got))
    assert all(list(r) == ["attributes", "Id"] for r in got)


@pytest.mark.parametrize(
    ("sosl", "expected"),
    [
        ("FIND {Northwind} IN NAME FIELDS RETURNING Account(Name)", {"Northwind Logistics"}),
        ('FIND {"Northwind Logistics"} RETURNING Account(Name)', {"Northwind Logistics"}),
        ('FIND {"Logistics Northwind"} RETURNING Account(Name)', set()),
        ("FIND {Northwind AND Logistics} RETURNING Account(Name)", {"Northwind Logistics"}),
        ("FIND {Northwind AND Bluefin} RETURNING Account(Name)", set()),
        ("FIND {Northwind OR Bluefin} RETURNING Account(Name)", {"Northwind Logistics", "Bluefin Seafood"}),
        ("FIND {Northwind Bluefin} RETURNING Account(Name)", {"Northwind Logistics", "Bluefin Seafood"}),
        ("FIND {(Northwind OR Logistics) AND NOT Logistics} IN NAME FIELDS RETURNING Account(Name)", set()),
        (
            "FIND {North*} IN NAME FIELDS RETURNING Account(Name)",
            {"Northwind Logistics", "Northstar IT Services"},
        ),
        ("FIND {Nor?hwind} RETURNING Account(Name)", {"Northwind Logistics"}),
        ("FIND {(Northwind OR Bluefin) AND Seafood} RETURNING Account(Name)", {"Bluefin Seafood"}),
        ("FIND {+14165550111} IN PHONE FIELDS RETURNING Account(Name)", {"Northwind Logistics"}),
        ("FIND {416-555-0111} IN PHONE FIELDS RETURNING Account(Name)", {"Northwind Logistics"}),
        ("FIND {zzzzqqq} RETURNING Account(Name)", set()),
    ],
)
async def test_search_expressions(admin: Actor, sosl: str, expected: set[str]) -> None:
    got = {r["Name"] for r in await find(admin, sosl)}
    assert got == expected


async def test_juxtaposed_terms_are_or_ed_and_name_matches_rank_first(admin: Actor) -> None:
    got = [r["Name"] for r in await find(admin, "FIND {northwind logistics} RETURNING Account(Name)")]
    assert (
        got[0] == "Northwind Logistics" and len(got) >= 2
    )  # other accounts mention "logistics" in their text


async def test_returning_where_order_and_limit(admin: Actor) -> None:
    got = await find(
        admin,
        "FIND {Healthcare} IN ALL FIELDS RETURNING Account(Name, Industry WHERE Industry = 'Healthcare' ORDER BY Name DESC LIMIT 1)",
    )
    assert len(got) == 1 and got[0]["Industry"] == "Healthcare"
    allh = await find(
        admin, "FIND {Healthcare} RETURNING Account(Name WHERE Industry = 'Healthcare' ORDER BY Name)"
    )
    assert len(allh) >= 1 and [r["Name"] for r in allh] == sorted(r["Name"] for r in allh)
    limited = await find(admin, "FIND {Portal} RETURNING Case(Subject), Task(Subject) LIMIT 1")
    assert len(limited) == 1
    both = await find(admin, "FIND {Portal} RETURNING Case(CaseNumber, Subject), Task(Subject)")
    assert {"Case", "Task"} <= set(types(both))
    rel = await find(admin, "FIND {Northwind} RETURNING Contact(Name, Account.Name)")
    assert rel == [] or rel[0]["Account"]["Name"] == "Northwind Logistics"


async def test_search_respects_relevance_order_and_objects_order(admin: Actor) -> None:
    got = await find(admin, "FIND {Pinnacle} RETURNING Opportunity(Name), Account(Name)")
    assert types(got)[0] == "Opportunity"  # RETURNING order is preserved
    assert types(got)[-1] == "Account"


async def test_search_errors(admin: Actor) -> None:
    for sosl, code in (
        ("FIND {x} RETURNING Widget(Id)", "INVALID_TYPE"),
        ("FIND Northwind", "INVALID_SEARCH"),
        ("SELECT Id FROM Account", "INVALID_SEARCH"),
        ("FIND {unterminated", "INVALID_SEARCH"),
        ("FIND {x} RETURNING Account(Nope)", "INVALID_FIELD"),
    ):
        r = await admin.get("/search", params={"q": sosl})
        assert r.status_code == 400, sosl
        assert r.json()[0]["errorCode"] == code, (sosl, r.json())
    assert (await admin.get("/search")).status_code == 400


async def test_parameterized_search_get_and_post(admin: Actor) -> None:
    r = await admin.get(
        "/parameterizedSearch", params={"q": "Northwind", "sobject": "Account", "Account.fields": "Id,Name"}
    )
    assert r.status_code == 200
    got = r.json()["searchRecords"]
    assert [x["Name"] for x in got] == ["Northwind Logistics"] and list(got[0]) == [
        "attributes",
        "Id",
        "Name",
    ]
    multi = await admin.get(
        "/parameterizedSearch",
        params=[
            ("q", "Foxglove"),
            ("sobject", "Account"),
            ("sobject", "Opportunity"),
            ("fields", "Id"),
            ("overallLimit", "1"),
        ],
    )
    assert len(multi.json()["searchRecords"]) == 1
    body = {
        "q": "Foxglove",
        "fields": ["Id", "Name"],
        "sobjects": [
            {"name": "Account", "fields": ["Name", "Industry"]},
            {"name": "Opportunity", "limit": 1},
        ],
        "in": "ALL",
        "overallLimit": 10,
    }
    posted = (await admin.post("/parameterizedSearch", body)).json()["searchRecords"]
    assert types(posted) == ["Account", "Opportunity"]
    assert set(posted[0]) == {"attributes", "Name", "Industry"} and set(posted[1]) == {
        "attributes",
        "Id",
        "Name",
    }
    scoped = (
        await admin.post(
            "/parameterizedSearch", {"q": "northwind*", "in": "EMAIL", "sobjects": [{"name": "Contact"}]}
        )
    ).json()["searchRecords"]
    assert len(scoped) == 3
    assert (await admin.post("/parameterizedSearch", {"sobjects": [{"name": "Account"}]})).status_code == 400
    bad = await admin.post("/parameterizedSearch", {"q": "x", "sobjects": [{"name": "Widget"}]})
    assert bad.status_code == 400 and bad.json()[0]["errorCode"] == "INVALID_TYPE"
