"""Bulk API 2.0: ingest and query jobs."""

from __future__ import annotations

import csv
import io
from collections.abc import Awaitable, Callable
from typing import Any

from .conftest import RAJESH, Actor

Make = Callable[..., Awaitable[Actor]]


def parse_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


async def ingest(
    actor: Actor,
    sobject: str,
    operation: str,
    data: str,
    *,
    external_id: str | None = None,
    delimiter: str = "COMMA",
    line_ending: str = "LF",
    final: str = "JobComplete",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "object": sobject, "operation": operation, "contentType": "CSV", "lineEnding": line_ending,
        "columnDelimiter": delimiter,
    }  # fmt: skip
    if external_id:
        body["externalIdFieldName"] = external_id
    created = await actor.post("/jobs/ingest", body)
    assert created.status_code == 200, created.text
    job = created.json()
    uploaded = await actor.request(
        "PUT",
        f"/jobs/ingest/{job['id']}/batches",
        content=data.encode(),
        headers={"Content-Type": "text/csv"},
    )
    assert uploaded.status_code == 201, uploaded.text
    closed = await actor.patch(f"/jobs/ingest/{job['id']}", {"state": "UploadComplete"})
    assert closed.status_code == 200, closed.text
    assert closed.json()["state"] == "UploadComplete"
    done = (await actor.get(f"/jobs/ingest/{job['id']}")).json()
    assert done["state"] == final, done
    return done


async def results(actor: Actor, job: dict[str, Any], kind: str) -> list[dict[str, str]]:
    r = await actor.get(f"/jobs/ingest/{job['id']}/{kind}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv"), r.text
    return parse_csv(r.text)


async def test_job_creation_shape(admin: Actor) -> None:
    r = await admin.post(
        "/jobs/ingest", {"object": "Account", "operation": "insert", "contentType": "CSV", "lineEnding": "LF"}
    )
    assert r.status_code == 200
    job = r.json()
    assert job["id"].startswith("750") and len(job["id"]) == 18
    assert job["state"] == "Open" and job["operation"] == "insert" and job["object"] == "Account"
    assert job["contentType"] == "CSV" and job["apiVersion"] == 62.0 and job["jobType"] == "V2Ingest"
    assert (
        job["lineEnding"] == "LF"
        and job["columnDelimiter"] == "COMMA"
        and job["concurrencyMode"] == "Parallel"
    )
    assert job["contentUrl"] == f"services/data/v62.0/jobs/ingest/{job['id']}/batches"
    assert job["createdById"] == admin.user_id and job["numberRecordsProcessed"] == 0
    assert job["createdDate"] == "2026-10-16T12:00:00.000+0000"
    same = (await admin.get(f"/jobs/ingest/{job['id']}")).json()
    assert same == job
    listing = (await admin.get("/jobs/ingest")).json()
    assert listing["done"] is True and [j["id"] for j in listing["records"]] == [job["id"]]


async def test_insert_with_partial_failures(admin: Actor) -> None:
    csv_data = "Name,Industry,AnnualRevenue,Rating\nBulk Alpha,Retail,125000,Hot\n,Retail,1,Hot\nBulk Gamma,Retail,notanumber,Hot\nBulk Delta,Healthcare,99,Warm\n"
    job = await ingest(admin, "Account", "insert", csv_data)
    assert job["numberRecordsProcessed"] == 4 and job["numberRecordsFailed"] == 2
    assert "contentUrl" not in job
    ok = await results(admin, job, "successfulResults")
    assert list(ok[0]) == ["sf__Id", "sf__Created", "Name", "Industry", "AnnualRevenue", "Rating"]
    assert [r["Name"] for r in ok] == ["Bulk Alpha", "Bulk Delta"]
    assert all(r["sf__Created"] == "true" and r["sf__Id"].startswith("001") for r in ok)
    failed = await results(admin, job, "failedResults")
    assert list(failed[0])[:2] == ["sf__Id", "sf__Error"]
    assert (
        failed[0]["sf__Id"] == ""
        and failed[0]["sf__Error"] == "REQUIRED_FIELD_MISSING:Required fields are missing: [Name]:Name --"
    )
    assert failed[1]["sf__Error"].startswith("JSON_PARSER_ERROR:") and failed[1]["Name"] == "Bulk Gamma"
    assert await results(admin, job, "unprocessedrecords") == []
    raw = (await admin.get(f"/jobs/ingest/{job['id']}/successfulResults")).text
    assert raw.startswith('"sf__Id","sf__Created","Name"')  # every cell is quoted, like Salesforce
    stored = await admin.rows(
        "SELECT Name, AnnualRevenue FROM Account WHERE Name LIKE 'Bulk %' ORDER BY Name"
    )
    assert [(r["Name"], r["AnnualRevenue"]) for r in stored] == [
        ("Bulk Alpha", 125000.0),
        ("Bulk Delta", 99.0),
    ]


async def test_update_delete_and_blank_cell_semantics(admin: Actor) -> None:
    ids = await admin.rows(
        "SELECT Id, Phone, Rating FROM Account WHERE Name IN ('Northwind Logistics', 'Bluefin Seafood') ORDER BY Name"
    )
    blue, north = ids
    data = f"Id,Phone,Rating\n{blue['Id']},,Hot\n{north['Id']},#N/A,Warm\n001000000000000AAA,1,Cold\n"
    job = await ingest(admin, "Account", "update", data)
    assert (job["numberRecordsProcessed"], job["numberRecordsFailed"]) == (3, 1)
    after = {
        r["Id"]: r
        for r in await admin.rows(
            f"SELECT Id, Phone, Rating FROM Account WHERE Id IN ('{blue['Id']}', '{north['Id']}')"
        )
    }
    assert (
        after[blue["Id"]]["Phone"] == blue["Phone"] and after[blue["Id"]]["Rating"] == "Hot"
    )  # a blank cell leaves the field alone
    assert after[north["Id"]]["Phone"] is None and after[north["Id"]]["Rating"] == "Warm"  # #N/A clears it
    failed = await results(admin, job, "failedResults")
    assert failed[0]["sf__Error"].startswith("NOT_FOUND:")
    job = await ingest(admin, "Account", "delete", f"Id\n{blue['Id']}\n{north['Id']}\n")
    ok = await results(admin, job, "successfulResults")
    assert [r["sf__Id"] for r in ok] == [blue["Id"], north["Id"]] and {r["sf__Created"] for r in ok} == {
        "false"
    }
    assert await admin.ids(f"SELECT Id FROM Account WHERE Id IN ('{blue['Id']}', '{north['Id']}')") == []
    missing_id = await ingest(admin, "Account", "update", "Rating\nHot\n", final="Failed")
    assert missing_id["errorMessage"] == "InvalidBatch : Id field is required for this operation"
    assert missing_id["numberRecordsProcessed"] == 0


async def test_bad_csv_headers_fail_the_job(admin: Actor) -> None:
    unknown = await ingest(admin, "Account", "insert", "Name,Nonsense__c\nA,1\n", final="Failed")
    assert unknown["errorMessage"] == "InvalidBatch : Field name not found : Nonsense__c"
    duplicate = await ingest(admin, "Account", "insert", "Name,name\nA,B\n", final="Failed")
    assert "Duplicate field specified" in duplicate["errorMessage"]
    bad_relationship = await ingest(admin, "Contact", "insert", "LastName,Nope.Name\nA,B\n", final="Failed")
    assert "Field name not found : Nope.Name" in bad_relationship["errorMessage"]
    assert await admin.ids("SELECT Id FROM Account WHERE Name = 'A'") == []


async def test_upsert_and_relationship_columns(admin: Actor) -> None:
    data = "QuickBooks_Customer_Id__c,Name,Industry\n2,Ignored Rename,Technology\nBULK-77,Bulk Seventy-Seven,Retail\n"
    job = await ingest(admin, "Account", "upsert", data, external_id="QuickBooks_Customer_Id__c")
    ok = await results(admin, job, "successfulResults")
    assert [r["sf__Created"] for r in ok] == ["false", "true"]
    updated = (await admin.rows("SELECT Name, Industry FROM Account WHERE QuickBooks_Customer_Id__c = '2'"))[
        0
    ]
    assert updated["Industry"] == "Technology"
    contacts = (
        "LastName,FirstName,Account.QuickBooks_Customer_Id__c\nBulkson,Ann,BULK-77\nOrphanson,Bob,NO-SUCH\n"
    )
    job = await ingest(admin, "Contact", "insert", contacts)
    assert (job["numberRecordsProcessed"], job["numberRecordsFailed"]) == (2, 1)
    failed = await results(admin, job, "failedResults")
    assert "Foreign key external ID: NO-SUCH not found" in failed[0]["sf__Error"]
    linked = await admin.rows("SELECT LastName, Account.Name FROM Contact WHERE LastName = 'Bulkson'")
    assert linked[0]["Account"]["Name"] == "Bulk Seventy-Seven"
    bad = await admin.post("/jobs/ingest", {"object": "Account", "operation": "upsert"})
    assert bad.status_code == 400 and bad.json()[0]["errorCode"] == "InvalidJob"


async def test_delimiters_and_line_endings(admin: Actor) -> None:
    job = await ingest(
        admin,
        "Account",
        "insert",
        "Name|Industry\r\nPipe Co|Retail\r\n",
        delimiter="PIPE",
        line_ending="CRLF",
    )
    assert job["columnDelimiter"] == "PIPE" and job["lineEnding"] == "CRLF"
    raw = (await admin.get(f"/jobs/ingest/{job['id']}/successfulResults")).text
    assert "|" in raw.splitlines()[0] and "\r\n" in raw
    quoted = 'Name,Description\n"Comma, Co","Line one\nline two ""quoted"""\n'
    job = await ingest(admin, "Account", "insert", quoted)
    got = (await admin.rows("SELECT Name, Description FROM Account WHERE Name = 'Comma, Co'"))[0]
    assert got["Description"] == 'Line one\nline two "quoted"' and job["numberRecordsFailed"] == 0


async def test_job_state_machine_and_errors(admin: Actor) -> None:
    created = (await admin.post("/jobs/ingest", {"object": "Account", "operation": "insert"})).json()
    jid = created["id"]
    premature = await admin.patch(f"/jobs/ingest/{jid}", {"state": "UploadComplete"})
    assert premature.status_code == 400 and premature.json()[0]["errorCode"] == "InvalidBatch"
    early = await admin.get(f"/jobs/ingest/{jid}/successfulResults")
    assert early.status_code == 400
    assert (
        await admin.request("PUT", f"/jobs/ingest/{jid}/batches", content=b"Name\nOnce\n")
    ).status_code == 201
    twice = await admin.request("PUT", f"/jobs/ingest/{jid}/batches", content=b"Name\nTwice\n")
    assert twice.status_code == 400
    assert (await admin.patch(f"/jobs/ingest/{jid}", {"state": "Nonsense"})).status_code == 400
    aborted = (await admin.patch(f"/jobs/ingest/{jid}", {"state": "Aborted"})).json()
    assert aborted["state"] == "Aborted"
    assert (
        await admin.request("PUT", f"/jobs/ingest/{jid}/batches", content=b"Name\nLate\n")
    ).status_code == 400
    unprocessed = parse_csv((await admin.get(f"/jobs/ingest/{jid}/unprocessedrecords")).text)
    assert unprocessed == [{"Name": "Once"}]
    assert (await admin.query("SELECT COUNT() FROM Account WHERE Name = 'Once'"))["totalSize"] == 0
    assert (await admin.delete(f"/jobs/ingest/{jid}")).status_code == 204
    assert (await admin.get(f"/jobs/ingest/{jid}")).status_code == 404
    for body in ({"object": "Account", "operation": "merge"}, {"object": "Nothing__c", "operation": "insert"},
                 {"object": "Account", "operation": "insert", "contentType": "JSON"},
                 {"object": "Account", "operation": "insert", "columnDelimiter": "WAVY"}):  # fmt: skip
        assert (await admin.post("/jobs/ingest", body)).status_code == 400, body
    open_job = (await admin.post("/jobs/ingest", {"object": "Account", "operation": "insert"})).json()
    assert (
        await admin.delete(f"/jobs/ingest/{open_job['id']}")
    ).status_code == 400  # cannot delete an Open job
    empty = await admin.patch(f"/jobs/ingest/{open_job['id']}", {"state": "UploadComplete"})
    assert empty.status_code == 400


async def test_jobs_are_private_to_their_creator(admin: Actor, actor: Make) -> None:
    rajesh = await actor(RAJESH)
    job = (await rajesh.post("/jobs/ingest", {"object": "Account", "operation": "insert"})).json()
    other = await actor("p011")
    assert (await other.get(f"/jobs/ingest/{job['id']}")).status_code == 404
    assert (await other.get("/jobs/ingest")).json()["records"] == []
    assert (await admin.get(f"/jobs/ingest/{job['id']}")).status_code == 200  # admins see all jobs


async def test_bulk_dml_respects_sharing_and_permissions(admin: Actor, actor: Make) -> None:
    rajesh, natalie_opp = (
        await actor(RAJESH),
        (await admin.ids("SELECT Id FROM Opportunity WHERE Owner.Name = 'Natalie Cho' LIMIT 1"))[0],
    )
    job = await ingest(rajesh, "Opportunity", "update", f"Id,NextStep\n{natalie_opp},hacked\n")
    assert job["numberRecordsFailed"] == 1
    assert (await results(rajesh, job, "failedResults"))[0]["sf__Error"].startswith("NOT_FOUND")
    jasmine = await actor("p029")
    job = await ingest(jasmine, "Account", "insert", "Name\nRead Only Bulk\n")
    assert job["numberRecordsFailed"] == 1
    assert "INSUFFICIENT_ACCESS_OR_READONLY" in (await results(jasmine, job, "failedResults"))[0]["sf__Error"]


async def test_hard_delete_requires_admin_and_purges(admin: Actor, actor: Make) -> None:
    rid = await admin.create("Account", {"Name": "Purge Me"})
    rajesh = await actor(RAJESH)
    assert (
        await rajesh.post("/jobs/ingest", {"object": "Account", "operation": "hardDelete"})
    ).status_code == 400
    job = await ingest(admin, "Account", "hardDelete", f"Id\n{rid}\n")
    assert job["numberRecordsFailed"] == 0
    assert await admin.rows(f"SELECT Id FROM Account WHERE Id = '{rid}'", all_rows=True) == []
    soft = await admin.create("Account", {"Name": "Soft Delete"})
    await ingest(admin, "Account", "delete", f"Id\n{soft}\n")
    assert len(await admin.rows(f"SELECT Id FROM Account WHERE Id = '{soft}'", all_rows=True)) == 1


async def test_inprogress_polls_setting(admin: Actor) -> None:
    ctx = admin.client.hub.contexts["salesforce"]  # type: ignore[attr-defined]
    ctx.settings["BULK_INPROGRESS_POLLS"] = "2"
    try:
        job = (await admin.post("/jobs/ingest", {"object": "Account", "operation": "insert"})).json()
        await admin.request("PUT", f"/jobs/ingest/{job['id']}/batches", content=b"Name\nSlow Job\n")
        await admin.patch(f"/jobs/ingest/{job['id']}", {"state": "UploadComplete"})
        states = [(await admin.get(f"/jobs/ingest/{job['id']}")).json()["state"] for _ in range(4)]
        assert states == ["InProgress", "InProgress", "JobComplete", "JobComplete"]
    finally:
        ctx.settings.pop("BULK_INPROGRESS_POLLS")


# -- query jobs ------------------------------------------------------------------------------


async def query_job(actor: Actor, soql: str, operation: str = "query") -> dict[str, Any]:
    r = await actor.post("/jobs/query", {"operation": operation, "query": soql})
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["state"] == "UploadComplete"
    done = (await actor.get(f"/jobs/query/{created['id']}")).json()
    return done


async def test_query_job_flow_and_csv_shape(admin: Actor) -> None:
    created = (
        await admin.post(
            "/jobs/query",
            {"operation": "query", "query": "SELECT Id, Name, Owner.Name FROM Account ORDER BY Name"},
        )
    ).json()
    assert set(created) >= {"id", "operation", "object", "createdById", "createdDate", "systemModstamp", "state",
                            "concurrencyMode", "contentType", "apiVersion", "lineEnding", "columnDelimiter"}  # fmt: skip
    assert (
        created["operation"] == "query" and created["object"] == "Account" and created["jobType"] == "V2Query"
    )
    job = (await admin.get(f"/jobs/query/{created['id']}")).json()
    assert job["state"] == "JobComplete" and job["numberRecordsProcessed"] == 41 and job["retries"] == 0
    r = await admin.get(f"/jobs/query/{job['id']}/results")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["sforce-numberofrecords"] == "41" and r.headers["sforce-locator"] == "null"
    lines = r.text.splitlines()
    assert lines[0] == '"Id","Name","Owner.Name"'
    rows = parse_csv(r.text)
    assert len(rows) == 41 and rows[0]["Id"].startswith("001") and rows[0]["Owner.Name"]
    names = [x["Name"] for x in rows]
    assert names == sorted(names, key=str.casefold)
    listing = (await admin.get("/jobs/query")).json()
    assert [j["id"] for j in listing["records"]] == [job["id"]]


async def test_query_results_paging_with_locators(admin: Actor) -> None:
    job = await query_job(admin, "SELECT Id FROM Task ORDER BY Id")
    seen: list[str] = []
    locator = None
    calls = 0
    while True:
        params: dict[str, Any] = {"maxRecords": 100}
        if locator:
            params["locator"] = locator
        r = await admin.get(f"/jobs/query/{job['id']}/results", params=params)
        assert r.status_code == 200
        calls += 1
        seen += [x["Id"] for x in parse_csv(r.text)]
        assert int(r.headers["sforce-numberofrecords"]) == len(parse_csv(r.text))
        locator = r.headers["sforce-locator"]
        if locator == "null":
            break
    assert calls == 4 and len(seen) == 316 and seen == sorted(seen)
    assert (
        await admin.get(f"/jobs/query/{job['id']}/results", params={"locator": "garbage!!"})
    ).status_code == 400


async def test_query_all_jobs_include_deleted_and_values_are_formatted(admin: Actor) -> None:
    rid = await admin.create("Account", {"Name": "Bulk Gone", "AnnualRevenue": 5, "Industry": None})
    await admin.delete(f"/sobjects/Account/{rid}")
    live = await query_job(admin, "SELECT Id FROM Account WHERE IsDeleted = true")
    assert live["numberRecordsProcessed"] == 0
    everything = await query_job(
        admin,
        "SELECT Id, IsDeleted, AnnualRevenue, Industry, CreatedDate FROM Account WHERE IsDeleted = true",
        "queryAll",
    )
    row = parse_csv((await admin.get(f"/jobs/query/{everything['id']}/results")).text)[0]
    assert row == {
        "Id": rid,
        "IsDeleted": "true",
        "AnnualRevenue": "5.0",
        "Industry": "",
        "CreatedDate": "2026-10-16T12:00:00.000+0000",
    }
    standard = await query_job(admin, "SELECT Id FROM Account WHERE Id = '" + rid + "'")
    assert standard["numberRecordsProcessed"] == 0


async def test_unsupported_and_failing_query_jobs(admin: Actor) -> None:
    sub = await query_job(admin, "SELECT Id, (SELECT Id FROM Contacts) FROM Account")
    assert sub["state"] == "Failed" and "subqueries" in sub["errorMessage"].lower()
    agg = await query_job(admin, "SELECT COUNT(Id) FROM Account")
    assert agg["state"] == "Failed"
    bad = await query_job(admin, "SELECT Nope FROM Account")
    assert bad["state"] == "Failed" and "No such column 'Nope'" in bad["errorMessage"]
    results = await admin.get(f"/jobs/query/{bad['id']}/results")
    assert results.status_code == 400
    assert (await admin.post("/jobs/query", {"operation": "query"})).status_code == 400
    assert (await admin.post("/jobs/query", {"operation": "query", "query": "SELEKT"})).status_code == 400
    assert (
        await admin.post("/jobs/query", {"operation": "delete", "query": "SELECT Id FROM Account"})
    ).status_code == 400


async def test_query_job_lifecycle_and_sharing(admin: Actor, actor: Make) -> None:
    rajesh = await actor(RAJESH)
    mine = await query_job(rajesh, "SELECT Id FROM Opportunity")
    everyone = await query_job(admin, "SELECT Id FROM Opportunity")
    assert mine["numberRecordsProcessed"] == 5 and everyone["numberRecordsProcessed"] == 47
    assert (await rajesh.get(f"/jobs/query/{everyone['id']}")).status_code == 404
    aborted = await admin.patch(f"/jobs/query/{everyone['id']}", {"state": "Aborted"})
    assert aborted.status_code == 400  # already complete
    assert (await admin.delete(f"/jobs/query/{everyone['id']}")).status_code == 204
    assert (await admin.get(f"/jobs/query/{everyone['id']}/results")).status_code == 404
    assert (await admin.get("/jobs/ingest/750000000000000AAA")).status_code == 404
