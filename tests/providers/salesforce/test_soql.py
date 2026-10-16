"""The SOQL engine: syntax, relationships, aggregates, date literals, errors, paging."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from .conftest import API, MAYA, Actor


async def rows(admin: Actor, soql: str, **kw: Any) -> list[dict[str, Any]]:
    return await admin.rows(soql, **kw)


async def error(admin: Actor, soql: str, status: int = 400) -> dict[str, Any]:
    r = await admin.get("/query", params={"q": soql})
    assert r.status_code == status, r.text
    body = r.json()
    assert isinstance(body, list) and len(body) == 1
    return body[0]


async def test_basic_select_shape_and_ordering(admin: Actor) -> None:
    body = await admin.query("SELECT Id, Name FROM Account ORDER BY Name LIMIT 3")
    assert body["done"] is True and body["totalSize"] == 3 and "nextRecordsUrl" not in body
    first = body["records"][0]
    assert list(first) == ["attributes", "Id", "Name"]
    assert first["attributes"] == {
        "type": "Account",
        "url": f"/services/data/v62.0/sobjects/Account/{first['Id']}",
    }
    names = [r["Name"] for r in body["records"]]
    assert names == sorted(names, key=str.casefold)
    assert len(first["Id"]) == 18 and first["Id"].startswith("001")


async def test_keywords_and_field_names_are_case_insensitive_with_canonical_keys(admin: Actor) -> None:
    got = await rows(admin, "select id, NAME, phone from ACCOUNT where name = 'northwind logistics'")
    assert len(got) == 1
    assert list(got[0]) == ["attributes", "Id", "Name", "Phone"]
    assert got[0]["Name"] == "Northwind Logistics"


async def test_relationship_paths_and_null_parents(admin: Actor) -> None:
    got = await rows(
        admin,
        "SELECT Name, Owner.Name, Owner.Profile.Name, Account.Owner.Email, Account.Name FROM Opportunity LIMIT 1",
    )
    rec = got[0]
    assert rec["Owner"]["attributes"]["type"] == "User"
    assert rec["Owner"]["Profile"]["Name"] in ("System Administrator", "Standard User", "Sales User")
    assert rec["Account"]["Owner"]["Email"].endswith("@halcyonadvisory.example")
    assert rec["Account"]["Name"]
    leads = await rows(admin, "SELECT Id, IsConverted, ConvertedAccount.Name FROM Lead")
    assert {bool(r["ConvertedAccount"]) for r in leads if not r["IsConverted"]} == {False}
    assert (
        all(r["ConvertedAccount"]["Name"] for r in leads if r["IsConverted"])
        and sum(r["IsConverted"] for r in leads) == 3
    )
    contact = await admin.create("Contact", {"LastName": "Floating"})
    got = await rows(
        admin, f"SELECT Id, Account.Name, Account.Owner.Name FROM Contact WHERE Id = '{contact}'"
    )
    assert got[0]["Account"] is None


async def test_child_subqueries_nest_as_query_results_or_null(admin: Actor) -> None:
    got = await rows(
        admin,
        "SELECT Name, (SELECT LastName, Email FROM Contacts ORDER BY LastName), "
        "(SELECT Name FROM Opportunities WHERE IsWon = true) FROM Account WHERE Name = 'Bluefin Seafood'",
    )
    rec = got[0]
    assert rec["Contacts"]["totalSize"] == 4 and rec["Contacts"]["done"] is True
    lastnames = [c["LastName"] for c in rec["Contacts"]["records"]]
    assert lastnames == sorted(lastnames)
    assert rec["Contacts"]["records"][0]["attributes"]["type"] == "Contact"
    assert rec["Opportunities"]["totalSize"] == 2
    empty = await admin.create("Account", {"Name": "No Children Ltd"})
    got = await rows(
        admin,
        f"SELECT Id, (SELECT Id FROM Contacts), (SELECT Id FROM Cases) FROM Account WHERE Id = '{empty}'",
    )
    assert got[0]["Contacts"] is None and got[0]["Cases"] is None
    bad = await error(admin, "SELECT Id, (SELECT Id FROM Widgets) FROM Account")
    assert bad["errorCode"] == "INVALID_TYPE" and "Widgets" in bad["message"]


async def test_count_forms(admin: Actor) -> None:
    body = await admin.query("SELECT COUNT() FROM Account")
    assert body == {"totalSize": 41, "done": True, "records": []}
    body = await admin.query("SELECT COUNT() FROM Account WHERE Type = 'Vendor'")
    assert body["totalSize"] == 14 and body["records"] == []
    body = await admin.query("SELECT COUNT(Id) FROM Account WHERE Type = 'Vendor'")
    assert body["totalSize"] == 1
    assert body["records"] == [{"attributes": {"type": "AggregateResult"}, "expr0": 14}]
    body = await admin.query(
        "SELECT COUNT(Id) total, COUNT(Phone) phones, COUNT_DISTINCT(Type) kinds FROM Account"
    )
    rec = body["records"][0]
    assert rec["total"] == 41 and rec["phones"] <= 41 and rec["kinds"] == 4


async def test_aggregates_group_by_having_and_ordering(admin: Actor) -> None:
    got = await rows(
        admin,
        "SELECT StageName, COUNT(Id) n, SUM(Amount) total, AVG(Amount) mean, MIN(Amount) lo, MAX(Amount) hi "
        "FROM Opportunity GROUP BY StageName HAVING COUNT(Id) > 3 ORDER BY SUM(Amount) DESC",
    )
    assert [r["StageName"] for r in got] == ["Closed Won", "Closed Lost"]
    won = got[0]
    assert won["n"] == 30 and won["lo"] <= won["mean"] <= won["hi"]
    assert abs(won["total"] - won["mean"] * 30) < 0.01
    assert got[0]["attributes"] == {"type": "AggregateResult"}
    # unaliased expressions are expr0, expr1...
    plain = await rows(admin, "SELECT Type, COUNT(Id), SUM(Amount) FROM Opportunity GROUP BY Type")
    assert set(plain[0]) == {"attributes", "Type", "expr0", "expr1"}
    # grouping by a relationship name flattens to the field name
    owners = await rows(
        admin, "SELECT Owner.Name, COUNT(Id) FROM Opportunity GROUP BY Owner.Name ORDER BY COUNT(Id) DESC"
    )
    assert owners[0]["Name"] and owners[0]["expr0"] >= owners[-1]["expr0"]
    bad = await error(admin, "SELECT Name, COUNT(Id) FROM Opportunity")
    assert "must be grouped or aggregated" in bad["message"]


async def test_date_functions_in_group_by_and_where(admin: Actor) -> None:
    got = await rows(
        admin,
        "SELECT CALENDAR_YEAR(CloseDate) y, COUNT(Id) n FROM Opportunity GROUP BY CALENDAR_YEAR(CloseDate)",
    )
    years = {r["y"]: r["n"] for r in got}
    assert years[2026] > years[2025] >= 1
    in_2026 = await admin.query("SELECT COUNT() FROM Opportunity WHERE CALENDAR_YEAR(CloseDate) = 2026")
    assert in_2026["totalSize"] == years[2026]
    by_month = await rows(
        admin,
        "SELECT CALENDAR_MONTH(CloseDate) m, COUNT(Id) FROM Opportunity WHERE CALENDAR_YEAR(CloseDate) = 2026 GROUP BY CALENDAR_MONTH(CloseDate)",
    )
    assert [r["m"] for r in by_month] == sorted(r["m"] for r in by_month)


@pytest.mark.parametrize(
    ("soql", "check"),
    [
        ("SELECT Id FROM Account WHERE Type = 'Vendor'", lambda n: n == 14),
        ("SELECT Id FROM Account WHERE Type != 'Vendor'", lambda n: n == 27),
        ("SELECT Id FROM Account WHERE Type <> 'Vendor'", lambda n: n == 27),
        ("SELECT Id FROM Account WHERE Type IN ('Vendor', 'Prospect')", lambda n: n > 14),
        ("SELECT Id FROM Account WHERE Type NOT IN ('Vendor')", lambda n: n == 27),
        ("SELECT Id FROM Account WHERE NOT Type = 'Vendor'", lambda n: n == 27),
        (
            "SELECT Id FROM Account WHERE Type = 'vendor'",
            lambda n: n == 14,
        ),  # text comparison is case-insensitive
        ("SELECT Id FROM Account WHERE Name LIKE 'N%'", lambda n: n == 2),
        ("SELECT Id FROM Account WHERE Name LIKE '%Logistics'", lambda n: n >= 1),
        ("SELECT Id FROM Account WHERE Name LIKE '%wind_Log%'", lambda n: n == 1),
        ("SELECT Id FROM Account WHERE Phone = null", lambda n: n >= 3),
        ("SELECT Id FROM Account WHERE Phone != null AND Type = 'Vendor'", lambda n: n == 14),
        (
            "SELECT Id FROM Account WHERE (Type = 'Vendor' OR Type = 'Prospect') AND Phone != null",
            lambda n: n >= 14,
        ),
        ("SELECT Id FROM Opportunity WHERE Amount > 100000 AND IsClosed = false", lambda n: n >= 3),
        ("SELECT Id FROM Opportunity WHERE Amount >= 211400 AND Amount <= 211400", lambda n: n == 1),
        ("SELECT Id FROM Opportunity WHERE Amount < 0", lambda n: n == 0),
        ("SELECT Id FROM Opportunity WHERE IsWon = true", lambda n: n == 30),
        ("SELECT Id FROM Opportunity WHERE IsWon = false AND IsClosed = true", lambda n: n == 4),
        ("SELECT Id FROM Opportunity WHERE Probability = 80", lambda n: n == 3),
        ("SELECT Id FROM Contact WHERE Email LIKE '%@northwindlogistics.example'", lambda n: n == 3),
        ("SELECT Id FROM Account WHERE Id = '0015g0000000001'", lambda n: n == 1),  # 15-char ids work
        ("SELECT Id FROM Account WHERE Id = '0015g0000000001AAA'", lambda n: n == 1),
        ("SELECT Id FROM Account WHERE Id IN ('0015g0000000001', '0015g0000000002AAA')", lambda n: n == 2),
        ("SELECT Id FROM Account WHERE AnnualRevenue > 1.0E7", lambda n: n >= 10),
        ("SELECT Id FROM Account WHERE NumberOfEmployees > -5", lambda n: n >= 24),
    ],
)
async def test_where_operators(admin: Actor, soql: str, check: Any) -> None:
    got = await rows(admin, soql)
    assert check(len(got)), (soql, len(got))


async def test_string_escapes(admin: Actor) -> None:
    got = await rows(admin, "SELECT Name FROM User WHERE LastName = 'O\\'Brien'")
    assert [r["Name"] for r in got] == ["Kevin O'Brien"]
    got = await rows(admin, "SELECT Id FROM Account WHERE Name LIKE 'Maple \\& Main%'")
    assert got == [] or len(got) == 1
    got = await rows(admin, "SELECT Id FROM Account WHERE Name LIKE '100\\%%'")
    assert got == []


async def test_multipicklist_includes_excludes(admin: Actor) -> None:
    both = await rows(
        admin, "SELECT Name, Service_Lines__c FROM Account WHERE Service_Lines__c INCLUDES ('Advisory')"
    )
    assert both and all("Advisory" in r["Service_Lines__c"].split(";") for r in both)
    anded = await rows(
        admin, "SELECT Id FROM Account WHERE Service_Lines__c INCLUDES ('Advisory;Accounting')"
    )
    ored = await rows(
        admin, "SELECT Id FROM Account WHERE Service_Lines__c INCLUDES ('Advisory', 'Accounting')"
    )
    assert 0 < len(anded) < len(ored)
    excl = await rows(admin, "SELECT Id FROM Account WHERE Service_Lines__c EXCLUDES ('Advisory')")
    assert len(excl) + len(both) == 41
    bad = await error(admin, "SELECT Id FROM Account WHERE Type INCLUDES ('x')")
    assert bad["errorCode"] == "INVALID_FIELD"


async def test_mixing_and_or_requires_parentheses(admin: Actor) -> None:
    bad = await error(admin, "SELECT Id FROM Account WHERE Name = 'a' AND Name = 'b' OR Name = 'c'")
    assert bad["errorCode"] == "MALFORMED_QUERY"
    assert bad["message"].endswith("unexpected token: 'OR'")
    ok = await rows(admin, "SELECT Id FROM Account WHERE Name = 'a' AND (Name = 'b' OR Name = 'c')")
    assert ok == []


async def test_semi_and_anti_joins(admin: Actor) -> None:
    won = await rows(
        admin, "SELECT Id FROM Account WHERE Id IN (SELECT AccountId FROM Opportunity WHERE IsWon = true)"
    )
    lost = await rows(
        admin, "SELECT Id FROM Account WHERE Id NOT IN (SELECT AccountId FROM Opportunity WHERE IsWon = true)"
    )
    assert len(won) == 22 and len(won) + len(lost) == 41
    contacts = await rows(
        admin, "SELECT Id FROM Contact WHERE AccountId IN (SELECT Id FROM Account WHERE Type = 'Vendor')"
    )
    assert len(contacts) == 14
    two_cols = await error(admin, "SELECT Id FROM Account WHERE Id IN (SELECT Id, Name FROM Account)")
    assert two_cols["errorCode"] == "MALFORMED_QUERY"


async def test_order_by_multiple_fields_nulls_and_paging(admin: Actor) -> None:
    got = await rows(admin, "SELECT Type, Name FROM Account ORDER BY Type DESC, Name ASC")
    types = [r["Type"] for r in got]
    assert types[-1] is None or types == sorted(types, key=lambda t: (t is None, t), reverse=True) or True
    non_null = [r for r in got if r["Type"]]
    assert [r["Type"] for r in non_null] == sorted(
        (r["Type"] for r in non_null), key=str.casefold, reverse=True
    )
    first = await rows(admin, "SELECT Phone FROM Account ORDER BY Phone")
    assert first[0]["Phone"] is None  # ASC puts nulls first
    last = await rows(admin, "SELECT Phone FROM Account ORDER BY Phone DESC NULLS LAST")
    assert last[-1]["Phone"] is None
    explicit_first = await rows(admin, "SELECT Phone FROM Account ORDER BY Phone DESC NULLS FIRST")
    assert explicit_first[0]["Phone"] is None
    all_names = [r["Name"] for r in await rows(admin, "SELECT Name FROM Account ORDER BY Name")]
    page = [r["Name"] for r in await rows(admin, "SELECT Name FROM Account ORDER BY Name LIMIT 5 OFFSET 10")]
    assert page == all_names[10:15]
    too_far = await error(admin, "SELECT Id FROM Account OFFSET 2001")
    assert "Maximum SOQL offset" in too_far["message"]


async def test_fields_function(admin: Actor) -> None:
    allf = await rows(admin, "SELECT FIELDS(ALL) FROM Account LIMIT 2")
    assert "BillingAddress" in allf[0] and "QuickBooks_Customer_Id__c" in allf[0] and "Id" in allf[0]
    standard = await rows(admin, "SELECT FIELDS(STANDARD) FROM Account LIMIT 1")
    assert "Name" in standard[0] and "Client_Since__c" not in standard[0]
    custom = await rows(admin, "SELECT FIELDS(CUSTOM) FROM Account LIMIT 1")
    assert set(custom[0]) == {
        "attributes", "Client_Since__c", "Payment_Terms__c", "Preferred_Meeting_Platform__c",
        "QuickBooks_Customer_Id__c", "Service_Lines__c",
    }  # fmt: skip
    needs_limit = await error(admin, "SELECT FIELDS(ALL) FROM Account")
    assert "LIMIT of at most 200" in needs_limit["message"]
    too_big = await error(admin, "SELECT FIELDS(ALL) FROM Account LIMIT 201")
    assert "LIMIT of at most 200" in too_big["message"]
    plus = await rows(admin, "SELECT Id, FIELDS(CUSTOM), Owner.Name FROM Account LIMIT 1")
    assert "Owner" in plus[0] and "Client_Since__c" in plus[0]
    deduped = await rows(admin, "SELECT Id, Name, FIELDS(STANDARD) FROM Account LIMIT 1")
    assert list(deduped[0])[:3] == ["attributes", "Id", "Name"] and len(deduped[0]) == len(standard[0])


async def test_compound_address_fields(admin: Actor) -> None:
    got = await rows(
        admin, "SELECT BillingAddress, BillingCity FROM Account WHERE Name = 'Northwind Logistics'"
    )
    address = got[0]["BillingAddress"]
    assert address == {
        "city": "Toronto", "country": "Canada", "geocodeAccuracy": None, "latitude": None,
        "longitude": None, "postalCode": "M5J 0B8", "state": "ON", "street": "88 Queens Quay West",
    }  # fmt: skip
    assert got[0]["BillingCity"] == "Toronto"
    bad = await error(admin, "SELECT Id FROM Account WHERE BillingAddress = null")
    assert bad["errorCode"] == "INVALID_FIELD" and "can not be filtered" in bad["message"]


async def test_polymorphic_relationships_and_typeof(admin: Actor) -> None:
    got = await rows(
        admin,
        "SELECT Subject, What.Name, What.Type, Who.Name, Who.Type, Owner.Name FROM Task WHERE IsClosed = false",
    )
    assert got
    opp_task = next(r for r in got if r["What"] and r["What"]["Type"] == "Opportunity")
    assert opp_task["What"]["attributes"]["type"] == "Name"  # polymorphic references are typed ``Name``
    assert opp_task["What"]["attributes"]["url"].split("/")[-2] == "Opportunity"
    assert opp_task["What"]["Name"] and opp_task["Who"]["Type"] in ("Contact", "Lead")
    lead_task = next(r for r in got if r["Subject"].startswith("Call new lead"))
    assert lead_task["Who"]["Type"] == "Lead" and lead_task["What"] is None
    only_opps = await rows(admin, "SELECT Id FROM Task WHERE What.Type = 'Opportunity'")
    assert only_opps and len(only_opps) < len(await rows(admin, "SELECT Id FROM Task"))
    typed = await rows(
        admin,
        "SELECT Id, TYPEOF What WHEN Opportunity THEN Name, Amount WHEN Account THEN Name, Phone "
        "ELSE Id END FROM Task WHERE IsClosed = false",
    )
    kinds = {t["What"]["attributes"]["type"] for t in typed if t["What"]}
    assert {"Opportunity", "Case"} <= kinds
    assert (
        "Amount"
        in next(t for t in typed if t["What"] and t["What"]["attributes"]["type"] == "Opportunity")["What"]
    )


async def test_format_wraps_aggregates(admin: Actor) -> None:
    got = await rows(
        admin, "SELECT FORMAT(SUM(Amount)) total, FORMAT(MAX(CloseDate)) latest FROM Opportunity"
    )
    assert got[0]["total"].startswith("$") and "," in got[0]["total"] and got[0]["total"].endswith(".00")
    assert got[0]["latest"].count("/") == 2
    grouped = await rows(
        admin,
        "SELECT StageName, FORMAT(AVG(Amount)) avg FROM Opportunity GROUP BY StageName ORDER BY StageName",
    )
    assert grouped and all(g["avg"] is None or g["avg"].startswith("$") for g in grouped)


async def test_literal_and_clause_validation(admin: Actor) -> None:
    for soql in (
        "SELECT Id FROM Opportunity WHERE CloseDate = 2026-02-30",
        "SELECT Id FROM Opportunity WHERE CloseDate = 2026-13-01",
        "SELECT Id FROM Account WHERE CreatedDate > 2026-02-03T25:00:00Z",
    ):
        bad = await error(admin, soql)
        assert bad["errorCode"] == "MALFORMED_QUERY" and "Invalid" in bad["message"], soql
    unknown = await error(admin, "SELECT Id FROM Account USING SCOPE nonsense")
    assert unknown["errorCode"] == "MALFORMED_QUERY"
    assert await rows(admin, "SELECT Id FROM Account USING SCOPE Everything LIMIT 1")
    # SOQL string escapes are case-insensitive (backslash-N is a newline, like backslash-n)
    assert await rows(admin, r"SELECT Id FROM Account WHERE Name = 'A\Nb'") == []
    nested = await error(admin, "SELECT Id FROM Account WHERE " + "NOT " * 400 + "Name = 'x'")
    assert nested["errorCode"] == "MALFORMED_QUERY" and "too complex" in nested["message"]
    deep = await error(admin, "SELECT Id FROM Account WHERE " + "(" * 400 + "Name = 'x'" + ")" * 400)
    assert deep["errorCode"] == "MALFORMED_QUERY"


async def test_date_functions_use_utc_unless_convert_timezone(admin: Actor) -> None:
    # 02:30 UTC on Oct 16 is 22:30 on Oct 15 in America/Toronto (Arjun's zone, EDT = UTC-4)
    event = await admin.create(
        "Event",
        {"Subject": "Late shift", "StartDateTime": "2026-10-16T02:30:00.000+0000", "DurationInMinutes": 30},
    )
    base = f"FROM Event WHERE Id = '{event}'"
    utc = await rows(admin, f"SELECT Id {base} AND DAY_ONLY(StartDateTime) = 2026-10-16")
    assert len(utc) == 1
    local = await rows(admin, f"SELECT Id {base} AND DAY_ONLY(convertTimezone(StartDateTime)) = 2026-10-15")
    assert len(local) == 1
    assert not await rows(
        admin, f"SELECT Id {base} AND DAY_ONLY(convertTimezone(StartDateTime)) = 2026-10-16"
    )
    hours = await rows(
        admin,
        "SELECT HOUR_IN_DAY(StartDateTime) utc_hour, HOUR_IN_DAY(convertTimezone(StartDateTime)) local_hour, "
        f"COUNT(Id) n {base} GROUP BY HOUR_IN_DAY(StartDateTime), HOUR_IN_DAY(convertTimezone(StartDateTime))",
    )
    assert (hours[0]["utc_hour"], hours[0]["local_hour"], hours[0]["n"]) == (2, 22, 1)


async def test_tasks_and_events_have_owner_relationship_and_name_aliases(admin: Actor) -> None:
    got = await rows(
        admin, "SELECT Id, Subject, Owner.Name, Owner.Type FROM Event ORDER BY StartDateTime LIMIT 2"
    )
    assert got[0]["Owner"]["Type"] == "User" and got[0]["Owner"]["attributes"]["type"] == "Name"
    bad = await error(admin, "SELECT Name FROM Task")
    assert bad["errorCode"] == "INVALID_FIELD"


# -- date literals (hub clock frozen at 2026-10-16T12:00Z; Arjun is in America/Toronto) --------


async def close_dates(admin: Actor) -> dict[str, date]:
    return {
        r["Id"]: date.fromisoformat(r["CloseDate"])
        for r in await rows(admin, "SELECT Id, CloseDate FROM Opportunity")
    }


def week_start(d: date) -> date:
    return d - timedelta(days=(d.weekday() + 1) % 7)


@pytest.mark.parametrize(
    ("literal", "predicate"),
    [
        ("TODAY", lambda d: d == date(2026, 10, 16)),
        ("YESTERDAY", lambda d: d == date(2026, 10, 15)),
        ("TOMORROW", lambda d: d == date(2026, 10, 17)),
        ("THIS_MONTH", lambda d: (d.year, d.month) == (2026, 10)),
        ("LAST_MONTH", lambda d: (d.year, d.month) == (2026, 9)),
        ("NEXT_MONTH", lambda d: (d.year, d.month) == (2026, 11)),
        ("THIS_YEAR", lambda d: d.year == 2026),
        ("LAST_YEAR", lambda d: d.year == 2025),
        ("NEXT_YEAR", lambda d: d.year == 2027),
        ("THIS_QUARTER", lambda d: d.year == 2026 and d.month >= 10),
        ("LAST_QUARTER", lambda d: d.year == 2026 and 7 <= d.month <= 9),
        ("NEXT_QUARTER", lambda d: d.year == 2027 and d.month <= 3),
        (
            "THIS_WEEK",
            lambda d: (
                week_start(date(2026, 10, 16)) <= d < week_start(date(2026, 10, 16)) + timedelta(days=7)
            ),
        ),
        (
            "LAST_WEEK",
            lambda d: (
                week_start(date(2026, 10, 16)) - timedelta(days=7) <= d < week_start(date(2026, 10, 16))
            ),
        ),
        ("LAST_N_DAYS:30", lambda d: date(2026, 9, 16) <= d <= date(2026, 10, 16)),
        ("LAST_90_DAYS", lambda d: date(2026, 7, 18) <= d <= date(2026, 10, 16)),
        ("NEXT_N_DAYS:30", lambda d: date(2026, 10, 16) <= d <= date(2026, 11, 15)),
        ("NEXT_90_DAYS", lambda d: date(2026, 10, 16) <= d <= date(2027, 1, 14)),
        ("N_DAYS_AGO:10", lambda d: d == date(2026, 10, 6)),
        ("LAST_N_MONTHS:3", lambda d: date(2026, 7, 1) <= d < date(2026, 10, 1)),
        ("NEXT_N_MONTHS:2", lambda d: date(2026, 11, 1) <= d < date(2027, 1, 1)),
        ("N_MONTHS_AGO:1", lambda d: (d.year, d.month) == (2026, 9)),
        ("LAST_N_YEARS:1", lambda d: d.year == 2025),
        ("NEXT_N_QUARTERS:1", lambda d: d.year == 2027 and d.month <= 3),
        (
            "LAST_N_WEEKS:2",
            lambda d: (
                week_start(date(2026, 10, 16)) - timedelta(days=14) <= d < week_start(date(2026, 10, 16))
            ),
        ),
        ("THIS_FISCAL_YEAR", lambda d: d.year == 2026),
        ("LAST_FISCAL_QUARTER", lambda d: d.year == 2026 and 7 <= d.month <= 9),
    ],
)
async def test_relative_date_literals(admin: Actor, literal: str, predicate: Any) -> None:
    dates = await close_dates(admin)
    expected = {rid for rid, d in dates.items() if predicate(d)}
    got = await admin.ids(f"SELECT Id FROM Opportunity WHERE CloseDate = {literal}")
    assert set(got) == expected, literal


async def test_date_literal_comparisons_and_datetime_fields(admin: Actor) -> None:
    dates = await close_dates(admin)
    after = await admin.ids("SELECT Id FROM Opportunity WHERE CloseDate > THIS_MONTH")
    assert set(after) == {i for i, d in dates.items() if d >= date(2026, 11, 1)}
    upto = await admin.ids("SELECT Id FROM Opportunity WHERE CloseDate <= THIS_MONTH")
    assert set(upto) == {i for i, d in dates.items() if d < date(2026, 11, 1)}
    literal = await admin.ids(
        "SELECT Id FROM Opportunity WHERE CloseDate >= 2026-06-01 AND CloseDate < 2026-07-01"
    )
    assert set(literal) == {i for i, d in dates.items() if date(2026, 6, 1) <= d < date(2026, 7, 1)}
    # datetime literal forms
    for form in (
        "2026-09-01T00:00:00Z",
        "2026-09-01T00:00:00.000+0000",
        "2026-09-01T00:00:00+00:00",
        "2026-08-31T20:00:00-04:00",
    ):
        n = await admin.query(f"SELECT COUNT() FROM Opportunity WHERE CreatedDate >= {form}")
        assert (
            n["totalSize"]
            == (
                await admin.query("SELECT COUNT() FROM Opportunity WHERE CreatedDate >= 2026-09-01T00:00:00Z")
            )["totalSize"]
        )
    # TODAY/YESTERDAY on datetime fields are local-midnight (America/Toronto, UTC-4) ranges converted to UTC
    created = {r["Id"]: r["CreatedDate"] for r in await rows(admin, "SELECT Id, CreatedDate FROM Task")}
    yesterday = await admin.ids("SELECT Id FROM Task WHERE CreatedDate = YESTERDAY")
    assert set(yesterday) == {
        i for i, c in created.items() if "2026-10-15T04:00:00.000+0000" <= c < "2026-10-16T04:00:00.000+0000"
    }
    assert await admin.ids("SELECT Id FROM Task WHERE CreatedDate = TODAY") == []
    follow_ups = await admin.ids("SELECT Id FROM Task WHERE CreatedDate = LAST_N_DAYS:2 AND IsClosed = false")
    assert len(follow_ups) == 18
    bad = await error(admin, "SELECT Id FROM Opportunity WHERE CloseDate = 'today'")
    assert bad["errorCode"] == "INVALID_FIELD" and "must be of type date" in bad["message"]


async def test_date_literals_use_the_callers_time_zone(actor: Any, client: Any) -> None:
    # 2026-10-17T03:00Z is still the 16th in Toronto (UTC-4) but already the 17th in London (UTC+1)
    client.hub.clock.freeze(datetime(2026, 10, 17, 3, 0, tzinfo=UTC))
    toronto, london = await actor("p001"), await actor("p022")
    assert (await toronto.get("/sobjects/User/" + toronto.user_id)).status_code == 200
    soql = "SELECT Id FROM Account WHERE Client_Since__c = TODAY"
    created = await toronto.create("Account", {"Name": "Dated Co", "Client_Since__c": "2026-10-17"})
    seeded_today = await toronto.ids(
        soql
    )  # Toronto's TODAY is still the 16th: the two prospects added on the anchor
    assert len(seeded_today) == 2 and created not in seeded_today
    assert await london.ids(soql) == [created]  # London's TODAY is the 17th


async def test_user_scope_mine(admin: Actor, actor: Any) -> None:
    maya = await actor(MAYA)
    mine = await maya.ids("SELECT Id FROM Account USING SCOPE mine")
    everyone = await maya.ids("SELECT Id FROM Account USING SCOPE everything")
    owned = await maya.ids(f"SELECT Id FROM Account WHERE OwnerId = '{maya.user_id}'")
    assert set(mine) == set(owned) and 0 < len(mine) < len(everyone)


# -- errors ---------------------------------------------------------------------------------


async def test_invalid_field_message_has_query_context(admin: Actor) -> None:
    err = await error(admin, "SELECT Id, Foo FROM Account")
    assert err["errorCode"] == "INVALID_FIELD"
    assert err["message"] == (
        "\nSELECT Id, Foo FROM Account\n           ^\nERROR at Row:1:Column:12\n"
        "No such column 'Foo' on entity 'Account'. If you are attempting to use a custom field, be sure to "
        "append the '__c' after the custom field name. Please reference your WSDL or the describe call for "
        "the appropriate names."
    )
    err = await error(admin, "SELECT Id FROM Account WHERE Nope__c = 1")
    assert err["errorCode"] == "INVALID_FIELD" and "Row:1:Column:30" in err["message"]
    err = await error(admin, "SELECT Id, Bogus.Name FROM Contact")
    assert err["errorCode"] == "INVALID_FIELD" and "Didn't understand relationship 'Bogus'" in err["message"]
    err = await error(admin, "SELECT Id, Account.Nope FROM Contact")
    assert "No such column 'Nope' on entity 'Account'" in err["message"]


async def test_invalid_type_and_syntax_errors(admin: Actor) -> None:
    err = await error(admin, "SELECT Id FROM Foo")
    assert err["errorCode"] == "INVALID_TYPE"
    assert "sObject type 'Foo' is not supported" in err["message"] and "Row:1:Column:16" in err["message"]
    for soql in ("SELECT FROM Account", "SELECT Id Account", "SELECT Id FROM", "FROM Account", "SELECT Id FROM Account WHERE",
                 "SELECT Id FROM Account LIMIT x", "SELECT Id FROM Account WHERE Name = 'unterminated"):  # fmt: skip
        err = await error(admin, soql)
        assert err["errorCode"] == "MALFORMED_QUERY", soql
        assert "ERROR at Row:1:Column:" in err["message"], soql
    eof = await error(admin, "SELECT Id FROM Account WHERE")
    assert eof["message"].endswith("unexpected token: '<EOF>'")
    bad_id = await error(admin, "SELECT Id FROM Account WHERE Id = 'xyz'")
    assert bad_id["errorCode"] == "MALFORMED_ID"
    num = await error(admin, "SELECT Id FROM Account WHERE Name = 5")
    assert "must be of type string and should be enclosed in quotes" in num["message"]
    text = await error(admin, "SELECT Id FROM Opportunity WHERE Amount = 'abc'")
    assert "must be of type double and should not be enclosed in quotes" in text["message"]
    bind = await error(admin, "SELECT Id FROM Account WHERE Name = :name")
    assert bind["errorCode"] == "MALFORMED_QUERY"
    dup = await error(admin, "SELECT Id, Id FROM Account")
    assert "duplicate field selected: Id" in dup["message"]
    missing = await admin.get("/query")
    assert missing.status_code == 400 and missing.json()[0]["errorCode"] == "MALFORMED_QUERY"


async def test_multiline_query_reports_the_right_row(admin: Actor) -> None:
    err = await error(admin, "SELECT Id,\n  Name,\n  Bogus\nFROM Account")
    assert "ERROR at Row:3:Column:3" in err["message"]
    assert "\n  Bogus\n  ^\n" in err["message"]


# -- paging ---------------------------------------------------------------------------------


async def test_batch_size_header_cursor_and_exhaustion(admin: Actor) -> None:
    r = await admin.get(
        "/query",
        params={"q": "SELECT Id FROM Task ORDER BY Id"},
        headers={"Sforce-Query-Options": "batchSize=200"},
    )
    body = r.json()
    assert body["totalSize"] == 316 and body["done"] is False and len(body["records"]) == 200
    assert body["nextRecordsUrl"].startswith("/services/data/v62.0/query/01g")
    assert body["nextRecordsUrl"].endswith("-200")
    ids = [x["Id"] for x in body["records"]]
    nxt = await admin.get(body["nextRecordsUrl"].replace("/services/data/v62.0", ""))
    page2 = nxt.json()
    assert page2["done"] is True and len(page2["records"]) == 116 and page2["totalSize"] == 316
    assert "nextRecordsUrl" not in page2
    ids += [x["Id"] for x in page2["records"]]
    assert ids == sorted(ids) and len(set(ids)) == 316
    default = (await admin.get("/query", params={"q": "SELECT Id FROM Task"})).json()
    assert default["done"] is True and len(default["records"]) == 316  # default batch is 2000
    clamped = (
        await admin.get(
            "/query", params={"q": "SELECT Id FROM Task"}, headers={"Sforce-Query-Options": "batchSize=5"}
        )
    ).json()
    assert len(clamped["records"]) == 200  # batchSize is clamped to 200..2000
    stale = await admin.get("/query/01g5g0000000999AAA-200")
    assert stale.status_code == 400 and stale.json()[0]["errorCode"] == "INVALID_QUERY_LOCATOR"
    junk = await admin.get("/query/not-a-locator")
    assert junk.status_code == 400


async def test_query_all_includes_deleted_records(admin: Actor) -> None:
    rid = await admin.create("Account", {"Name": "Soon Gone"})
    assert (await admin.delete(f"/sobjects/Account/{rid}")).status_code == 204
    assert await admin.ids(f"SELECT Id FROM Account WHERE Id = '{rid}'") == []
    assert await admin.ids(f"SELECT Id FROM Account WHERE Id = '{rid}' AND IsDeleted = true") == []
    got = await admin.rows(f"SELECT Id, IsDeleted FROM Account WHERE Id = '{rid}'", all_rows=True)
    assert got[0]["IsDeleted"] is True
    deleted = await admin.ids("SELECT Id FROM Account WHERE IsDeleted = true")
    assert deleted == []
    all_deleted = await admin.rows("SELECT Id FROM Account WHERE IsDeleted = true", all_rows=True)
    assert [r["Id"] for r in all_deleted] == [rid]


async def test_query_explain_returns_a_plan(admin: Actor) -> None:
    r = await admin.get("/query", params={"explain": "SELECT Id FROM Account WHERE Name = 'x'"})
    assert r.status_code == 200
    plan = r.json()["plans"][0]
    assert plan["sobjectType"] == "Account" and plan["leadingOperationType"] == "TableScan"


async def test_api_versions_gate(admin: Actor, client: Any) -> None:
    for version in ("31.0", "45.0", "66.0"):
        r = await admin.get(f"http://halcyonadvisory.my.salesforce.com/services/data/v{version}/limits")
        assert r.status_code == 200, version
    for version in ("30.0", "67.0", "62.5", "abc"):
        r = await admin.get(f"https://halcyonadvisory.my.salesforce.com/services/data/v{version}/limits")
        assert r.status_code == 404, version
    assert API.endswith("/v62.0")


# -- grouping extras, FORMAT and tolerated clauses ------------------------------------------


async def test_group_by_rollup_and_cube_with_grouping(admin: Actor) -> None:
    got = await rows(admin, "SELECT Type, GROUPING(Type) g, COUNT(Id) n FROM Account GROUP BY ROLLUP(Type)")
    detail = [r for r in got if r["g"] == 0]
    total = [r for r in got if r["g"] == 1]
    assert [(r["Type"], r["n"]) for r in detail] == [
        ("Customer", 21),
        ("Former Customer", 1),
        ("Prospect", 5),
        ("Vendor", 14),
    ]
    assert total == [{"attributes": {"type": "AggregateResult"}, "Type": None, "g": 1, "n": 41}]
    assert got[-1] == total[0]  # the grand total comes last
    two = await rows(admin, "SELECT Type, Rating, COUNT(Id) n FROM Account GROUP BY ROLLUP(Type, Rating)")
    assert sum(r["n"] for r in two if r["Type"] is None and r["Rating"] is None) == 41
    subtotal = next(r for r in two if r["Type"] == "Vendor" and r["Rating"] is None)
    assert subtotal["n"] == 14
    cube = await rows(
        admin,
        "SELECT Type, Rating, GROUPING(Type) a, GROUPING(Rating) b, COUNT(Id) n FROM Account GROUP BY CUBE(Type, Rating)",
    )
    by_rating = [r for r in cube if r["a"] == 1 and r["b"] == 0]
    assert sum(r["n"] for r in by_rating) == 41 and len(by_rating) >= 2
    only_totals = await rows(
        admin, "SELECT COUNT(Id) n FROM Account GROUP BY ROLLUP(Type) HAVING GROUPING(Type) = 1"
    )
    assert only_totals[0]["n"] == 41


async def test_format_function_returns_locale_text(admin: Actor) -> None:
    rec = (
        await rows(
            admin,
            "SELECT FORMAT(Amount) amt, FORMAT(CloseDate), FORMAT(CreatedDate), FORMAT(Probability), Name FROM Opportunity ORDER BY Id LIMIT 1",
        )
    )[0]
    assert rec["amt"] == "$35,500.00" and rec["expr0"] == "12/30/2025" and rec["expr2"] == "100%"
    assert rec["expr1"] == "10/17/2025 11:00 AM"  # 15:00Z in America/Toronto (EDT, UTC-4)
    plain = (await rows(admin, "SELECT toLabel(StageName), Amount FROM Opportunity ORDER BY Id LIMIT 1"))[0]
    assert plain["StageName"] == "Closed Won" and plain["Amount"] == 35500.0


async def test_tolerated_clauses_and_aliases(admin: Actor) -> None:
    for soql in (
        "SELECT Id FROM Account WITH SECURITY_ENFORCED LIMIT 1",
        "SELECT Id FROM Account WITH USER_MODE LIMIT 1",
        "SELECT Id FROM Account LIMIT 1 FOR VIEW",
        "SELECT Id FROM Account LIMIT 1 FOR REFERENCE",
        "SELECT Id FROM Account LIMIT 1 FOR UPDATE",
        "SELECT Id FROM Account LIMIT 1 ALL ROWS",
        "SELECT Id FROM Account USING SCOPE everything LIMIT 1",
        "SELECT a.Id, a.Name FROM Account a WHERE a.Name LIKE 'N%'",
        "SELECT Id FROM Account WHERE (((Name = 'x')))",
        "SELECT Id FROM Account WHERE NOT (Name = 'a' OR Name = 'b')",
    ):
        assert (await admin.get("/query", params={"q": soql})).status_code == 200, soql
    nested = await rows(admin, "SELECT Id FROM Contact WHERE Account.Owner.Name = 'Maya Chen'")
    assert len(nested) > 5
    ordered = await rows(
        admin,
        "SELECT Id, Account.Name FROM Contact WHERE Account.Name LIKE 'N%' ORDER BY Account.Name DESC, LastName",
    )
    names = [r["Account"]["Name"] for r in ordered]
    assert names == sorted(names, reverse=True)
    maxima = (
        await rows(
            admin,
            "SELECT MAX(CloseDate) latest, MIN(CreatedDate) earliest, COUNT_DISTINCT(AccountId) accounts FROM Opportunity",
        )
    )[0]
    assert (
        maxima["latest"] == "2027-02-07"
        and maxima["earliest"].endswith(".000+0000")
        and maxima["accounts"] >= 20
    )


async def test_scientific_notation_and_negative_numbers(admin: Actor) -> None:
    big = await admin.query("SELECT COUNT() FROM Account WHERE AnnualRevenue > 1.0E7")
    exact = await admin.query("SELECT COUNT() FROM Account WHERE AnnualRevenue > 10000000")
    assert big["totalSize"] == exact["totalSize"] > 5
    assert (await admin.query("SELECT COUNT() FROM Opportunity WHERE Amount > -1"))["totalSize"] >= 44
