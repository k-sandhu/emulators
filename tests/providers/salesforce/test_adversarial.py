"""Regressions found by probing vendor wire contracts and hostile HTTP inputs."""

from __future__ import annotations

import base64
import csv
import io
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from emulators.core.hub import EmulatorHub

from .conftest import BASE, HANNAH, LOGIN, PASSWORD, Actor, username
from .test_composite import sub


def error(response: httpx.Response, status: int = 400) -> str:
    assert response.status_code == status, response.text
    return response.json()[0]["errorCode"]


@pytest.mark.parametrize("value", [True, False, 0, 7, [], ["Account"], {}, {"name": "Account"}])
@pytest.mark.parametrize("field", ["type", "referenceId"])
async def test_tree_attributes_reject_wrong_types(admin: Actor, field: str, value: Any) -> None:
    attrs = {"type": "Account", "referenceId": "root", field: value}
    assert (
        error(await admin.post("/composite/tree/Account", {"records": [{"attributes": attrs, "Name": "x"}]}))
        == "JSON_PARSER_ERROR"
    )


async def test_tree_types_must_match_root_and_relationship(admin: Actor) -> None:
    mismatch = {"records": [{"attributes": {"type": "Contact", "referenceId": "root"}, "LastName": "x"}]}
    assert error(await admin.post("/composite/tree/Account", mismatch)) == "INVALID_INPUT"
    mismatch["records"][0] = {
        "attributes": {"type": "Account", "referenceId": "root"},
        "Name": "Tree mismatch",
        "Contacts": {
            "records": [
                {"attributes": {"type": "Lead", "referenceId": "child"}, "LastName": "x", "Company": "x"}
            ]
        },
    }
    assert error(await admin.post("/composite/tree/Account", mismatch)) == "INVALID_INPUT"
    assert (await admin.query("SELECT COUNT() FROM Account WHERE Name = 'Tree mismatch'"))["totalSize"] == 0


@pytest.mark.parametrize("flag", ["allOrNone", "collateSubrequests"])
@pytest.mark.parametrize("value", ["false", [], {}, 1, None])
async def test_composite_flags_are_booleans(admin: Actor, flag: str, value: Any) -> None:
    body = {flag: value, "compositeRequest": [sub("POST", "/sobjects/Account", "a", {"Name": "Bad flags"})]}
    assert error(await admin.post("/composite", body)) == "JSON_PARSER_ERROR"
    assert (await admin.query("SELECT COUNT() FROM Account WHERE Name = 'Bad flags'"))["totalSize"] == 0


@pytest.mark.parametrize("headers", [[], "bad", {"Sforce-Query-Options": []}, {"If-Match": 42}])
async def test_composite_headers_are_a_string_map(admin: Actor, headers: Any) -> None:
    item = {**sub("GET", "/sobjects/Account/describe", "a"), "httpHeaders": headers}
    assert error(await admin.post("/composite", {"compositeRequest": [item]})) == "JSON_PARSER_ERROR"


async def test_composite_query_limit_and_reference_id_syntax(admin: Actor) -> None:
    items = [sub("GET", "/query?q=SELECT+Id+FROM+Account", f"q{i}") for i in range(6)]
    assert error(await admin.post("/composite", {"compositeRequest": items})) == "INVALID_INPUT"
    for reference in ("bad.ref", "_bad", "bad-ref"):
        assert (
            error(
                await admin.post(
                    "/composite", {"compositeRequest": [sub("GET", "/sobjects/Account/describe", reference)]}
                )
            )
            == "INVALID_INPUT"
        )
    assert (
        error(
            await admin.post("/composite", {"compositeRequest": [sub("PUT", "/sobjects/Account/x", "a", {})]})
        )
        == "INVALID_INPUT"
    )


@pytest.mark.parametrize("outer", [False, True])
@pytest.mark.parametrize("inner", [False, True])
async def test_collection_subrequests_honor_both_transaction_flags(
    admin: Actor, outer: bool, inner: bool
) -> None:
    body = {
        "allOrNone": outer,
        "compositeRequest": [
            sub("POST", "/sobjects/Account", "before", {"Name": "Audit before"}),
            sub(
                "POST",
                "/composite/sobjects",
                "many",
                {
                    "allOrNone": inner,
                    "records": [
                        {"attributes": {"type": "Account"}, "Name": "Audit inside"},
                        {"attributes": {"type": "Account"}},
                    ],
                },
            ),
            sub("POST", "/sobjects/Account", "after", {"Name": "Audit after"}),
        ],
    }
    response = await admin.post("/composite", body)
    assert response.status_code == 200, response.text
    results = response.json()["compositeResponse"]
    assert results[1]["httpStatusCode"] == 200
    assert results[1]["body"][1]["errors"][0]["statusCode"] == "REQUIRED_FIELD_MISSING"
    names = {row["Name"] for row in await admin.rows("SELECT Name FROM Account WHERE Name LIKE 'Audit %'")}
    expected = set() if outer else {"Audit before", "Audit after"} | (set() if inner else {"Audit inside"})
    assert names == expected
    if outer:
        assert results[0]["httpStatusCode"] == results[2]["httpStatusCode"] == 400


@pytest.mark.parametrize(
    "item",
    [
        {"method": "GET"},
        {"url": "v62.0/limits"},
        {"method": "GET", "url": "v99.0/limits"},
        {"method": "GET", "url": "https://other.example/"},
    ],
)
async def test_batch_requires_method_and_a_supported_versioned_url(
    admin: Actor, item: dict[str, Any]
) -> None:
    assert error(await admin.post("/composite/batch", {"batchRequests": [item]})) == "INVALID_INPUT"


async def test_batch_subrequests_each_count_toward_api_limits(admin: Actor) -> None:
    response = await admin.post(
        "/composite/batch", {"batchRequests": [{"method": "GET", "url": "v62.0/limits"}] * 3}
    )
    assert response.status_code == 200 and response.headers["Sforce-Limit-Info"] == "api-usage=3/15000"
    assert [row["result"]["DailyApiRequests"]["Remaining"] for row in response.json()["results"]] == [
        14999,
        14998,
        14997,
    ]
    response = await admin.post(
        "/composite",
        {"compositeRequest": [sub("GET", "/sobjects/Account/describe", f"r{n}") for n in range(3)]},
    )
    assert response.headers["Sforce-Limit-Info"] == "api-usage=4/15000"


async def test_batch_quota_failure_preserves_previously_committed_writes(admin: Actor) -> None:
    await admin.client.post(f"{BASE}/_emulator/actions/set_api_usage", json={"max": 1, "used": 0})
    response = await admin.post(
        "/composite/batch",
        {
            "batchRequests": [
                {"method": "POST", "url": "v62.0/sobjects/Account", "richInput": {"Name": "Quota committed"}},
                {"method": "POST", "url": "v62.0/sobjects/Account", "richInput": {"Name": "Quota denied"}},
            ]
        },
    )
    assert response.status_code == 200 and response.json()["hasErrors"] is True
    assert [row["statusCode"] for row in response.json()["results"]] == [201, 403]
    assert response.json()["results"][1]["result"][0]["errorCode"] == "REQUEST_LIMIT_EXCEEDED"
    await admin.client.post(f"{BASE}/_emulator/actions/set_api_usage", json={"max": 15000, "used": 0})
    assert [
        row["Name"] for row in await admin.rows("SELECT Name FROM Account WHERE Name LIKE 'Quota %'")
    ] == ["Quota committed"]


async def test_composite_can_reference_array_collection_results(admin: Actor) -> None:
    response = await admin.post(
        "/composite",
        {
            "allOrNone": True,
            "compositeRequest": [
                sub(
                    "POST",
                    "/composite/sobjects",
                    "accounts",
                    {"records": [{"attributes": {"type": "Account"}, "Name": "Array reference"}]},
                ),
                sub(
                    "POST",
                    "/sobjects/Contact",
                    "contact",
                    {"LastName": "Array", "AccountId": "@{accounts[0].id}"},
                ),
            ],
        },
    )
    assert response.status_code == 200, response.text
    results = response.json()["compositeResponse"]
    assert [r["httpStatusCode"] for r in results] == [200, 201]
    saved = await admin.get(f"/sobjects/Contact/{results[1]['body']['id']}")
    assert saved.json()["AccountId"] == results[0]["body"][0]["id"]


async def test_collection_retrieve_post_contract_and_2000_record_limit(admin: Actor) -> None:
    rid = (await admin.ids("SELECT Id FROM Account LIMIT 1"))[0]
    response = await admin.post(
        "/composite/sobjects/Account", {"recordIds": [rid] * 2000, "fieldNames": ["Id", "Name"]}
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == 2000 and all(row["Id"] == rid for row in response.json())
    response = await admin.post(
        "/composite/sobjects/Account", {"recordIds": [rid] * 2001, "fieldNames": ["Id"]}
    )
    assert error(response) == "EXCEEDED_ID_LIMIT"
    assert (
        await admin.post("/composite/sobjects/Account", {"ids": [rid], "fields": ["Id"]})
    ).status_code == 400
    assert (await admin.get("/composite/sobjects/Account", params={"ids": rid})).status_code == 400
    assert (
        await admin.post("/composite/sobjects/Account", {"recordIds": [], "fieldNames": ["Id"]})
    ).status_code == 400


async def test_collections_do_not_ignore_insert_ids_or_upsert_types(admin: Actor) -> None:
    rid = (await admin.ids("SELECT Id FROM Account LIMIT 1"))[0]
    created = await admin.post(
        "/composite/sobjects",
        {"records": [{"attributes": {"type": "Account"}, "Id": rid, "Name": "Ignored ID"}]},
    )
    assert created.status_code == 200 and created.json()[0]["success"] is False
    assert created.json()[0]["errors"][0]["statusCode"] == "INVALID_FIELD_FOR_INSERT_UPDATE"
    for record in (
        {"Name": "Missing type", "QuickBooks_Customer_Id__c": "audit"},
        {"attributes": {"type": "Contact"}, "LastName": "Wrong type"},
    ):
        assert (
            await admin.patch("/composite/sobjects/Account/QuickBooks_Customer_Id__c", {"records": [record]})
        ).status_code == 400
    assert (
        error(
            await admin.patch(
                "/composite/sobjects/Account/Id",
                {"records": [{"attributes": {"type": "Account"}, "Id": rid}]},
            )
        )
        == "INVALID_FIELD"
    )


async def test_external_id_alias_respects_private_record_visibility(
    admin: Actor,
    actor: Callable[..., Awaitable[Actor]],
) -> None:
    reader = await actor(HANNAH)
    hidden = await admin.create(
        "Opportunity", {"Name": "Hidden alias", "StageName": "Prospecting", "CloseDate": "2026-11-01"}
    )
    for rid in (hidden, hidden[:15], hidden.lower()):
        assert error(await reader.get(f"/sobjects/Opportunity/Id/{rid}"), 404) == "NOT_FOUND"
        assert (await admin.get(f"/sobjects/Opportunity/Id/{rid}")).json()["Id"] == hidden
    assert not await reader.rows(f"SELECT Id FROM Opportunity WHERE Id = '{hidden}'")
    own = await reader.create(
        "Opportunity", {"Name": "Visible alias", "StageName": "Prospecting", "CloseDate": "2026-11-01"}
    )
    assert (await reader.get(f"/sobjects/Opportunity/Id/{own}")).json()["Id"] == own


async def test_18_character_ids_ignore_case_but_15_character_ids_preserve_it(admin: Actor) -> None:
    rid = await admin.create("Lead", {"Company": "Case safe", "LastName": "IDs"})
    for spelling in (rid.upper(), rid.lower(), rid[:15]):
        assert (await admin.get(f"/sobjects/Lead/{spelling}")).json()["Id"] == rid
        assert (await admin.rows(f"SELECT Id FROM Lead WHERE Id = '{spelling}'"))[0]["Id"] == rid
    assert (await admin.get(f"/sobjects/Lead/{rid[:15].lower()}")).status_code == 404


@pytest.mark.parametrize(
    "field,value", [("AnnualRevenue", "1e99999999999"), ("NumberOfEmployees", "9" * 5000)]
)
async def test_extreme_numbers_return_dml_errors(admin: Actor, field: str, value: str) -> None:
    assert (
        error(await admin.post("/sobjects/Account", {"Name": "Overflow", field: value}))
        == "NUMBER_OUTSIDE_VALID_RANGE"
    )


@pytest.mark.parametrize(
    "stamp", ["0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59", "2026-10-16T12:00:00+99:99"]
)
async def test_datetime_overflow_and_invalid_offsets_are_rejected(admin: Actor, stamp: str) -> None:
    assert (
        error(
            await admin.post(
                "/sobjects/Event",
                {"Subject": "Overflow", "StartDateTime": stamp, "EndDateTime": "2026-10-17T12:00:00Z"},
            )
        )
        == "JSON_PARSER_ERROR"
    )


@pytest.mark.parametrize(
    "literal",
    [
        "LAST_N_DAYS:9999999999999999",
        "NEXT_N_MONTHS:9999999999999999",
        "LAST_N_YEARS:9999999999999999",
        "1e999999",
    ],
)
async def test_query_literal_overflows_are_400s(admin: Actor, literal: str) -> None:
    field = "AnnualRevenue" if literal == "1e999999" else "CreatedDate"
    assert (
        error(await admin.get("/query", params={"q": f"SELECT Id FROM Account WHERE {field} = {literal}"}))
        == "MALFORMED_QUERY"
    )


@pytest.mark.parametrize("literal", ["NEXT_90_DAYS", "NEXT_N_DAYS:90"])
async def test_future_date_literals_exclude_today(admin: Actor, literal: str) -> None:
    for day in ("2026-10-16", "2026-10-17"):
        await admin.create(
            "Opportunity", {"Name": "Audit future dates", "StageName": "Prospecting", "CloseDate": day}
        )
    rows = await admin.rows(
        f"SELECT CloseDate FROM Opportunity WHERE Name = 'Audit future dates' AND CloseDate = {literal}"
    )
    assert [row["CloseDate"] for row in rows] == ["2026-10-17"]


async def test_large_integer_query_literals_do_not_overflow_float_comparisons(admin: Actor) -> None:
    query = "SELECT Id FROM Account WHERE AnnualRevenue = " + "9" * 400
    assert error(await admin.get("/query", params={"q": query})) == "MALFORMED_QUERY"


async def test_invalid_listview_page_size_and_conditional_dates(admin: Actor) -> None:
    view = (await admin.get("/sobjects/Account/listviews")).json()["listviews"][0]["id"]
    assert (
        error(await admin.get(f"/sobjects/Account/listviews/{view}/results", params={"pageSize": "many"}))
        == "INVALID_PARAMETER_VALUE"
    )
    rid = (await admin.ids("SELECT Id FROM Account LIMIT 1"))[0]
    for value in ("not a date", "Fri, 16 Oct 2026 12:00:00"):
        assert (
            await admin.get(f"/sobjects/Account/{rid}", headers={"If-Modified-Since": value})
        ).status_code == 200


async def test_oversized_query_batch_size_cannot_overflow_integer_parsing(admin: Actor) -> None:
    response = await admin.get(
        "/query",
        params={"q": "SELECT Id FROM Task"},
        headers={"Sforce-Query-Options": "batchSize=" + "9" * 5000},
    )
    assert response.status_code == 200 and len(response.json()["records"]) == 316


async def test_query_cursors_expire_after_two_days(
    admin: Actor,
    actor: Callable[..., Awaitable[Actor]],
    hub: EmulatorHub,
) -> None:
    first = await admin.query("SELECT Id FROM Task", headers={"Sforce-Query-Options": "batchSize=200"})
    next_url = BASE + first["nextRecordsUrl"]
    assert (await admin.get(next_url)).status_code == 200
    hub.clock.advance(timedelta(days=2))
    fresh = await actor()
    assert error(await fresh.get(next_url)) == "INVALID_QUERY_LOCATOR"


@pytest.mark.parametrize("value", [True, 5, ["Id"], {"field": "Id"}])
async def test_bulk_external_id_field_rejects_wrong_types(admin: Actor, value: Any) -> None:
    assert (
        error(
            await admin.post(
                "/jobs/ingest", {"object": "Account", "operation": "upsert", "externalIdFieldName": value}
            )
        )
        == "InvalidJob"
    )


@pytest.mark.parametrize(
    "text",
    ['Name\n"unterminated\n', 'Name\n"x"extra\n', "Name\nshort,extra\n", 'Name\n"' + "a" * 140000 + '"\n'],
    ids=["unterminated", "trailing-text", "wrong-columns", "large-field"],
)
async def test_malformed_bulk_csv_fails_job_without_writes(admin: Actor, text: str) -> None:
    before = (await admin.query("SELECT COUNT() FROM Account"))["totalSize"]
    job = (await admin.post("/jobs/ingest", {"object": "Account", "operation": "insert"})).json()["id"]
    assert (await admin.request("PUT", f"/jobs/ingest/{job}/batches", content=text)).status_code == 201
    assert (await admin.patch(f"/jobs/ingest/{job}", {"state": "UploadComplete"})).status_code == 200
    state = (await admin.get(f"/jobs/ingest/{job}")).json()
    assert state["state"] == "Failed" and state["numberRecordsProcessed"] == 0
    assert (await admin.query("SELECT COUNT() FROM Account"))["totalSize"] == before


async def test_bulk_locators_belong_to_their_job_and_replay_the_same_page(admin: Actor) -> None:
    body = {"operation": "query", "query": "SELECT Id, Name FROM Account ORDER BY Id"}
    job = (await admin.post("/jobs/query", body)).json()["id"]
    other = (await admin.post("/jobs/query", body)).json()["id"]
    assert (await admin.get(f"/jobs/query/{job[:15]}")).json()["id"] == job
    first = await admin.get(f"/jobs/query/{job}/results", params={"maxRecords": 20})
    locator = first.headers["Sforce-Locator"]
    second = await admin.get(f"/jobs/query/{job}/results", params={"maxRecords": 30, "locator": locator})
    replay = await admin.get(f"/jobs/query/{job}/results", params={"maxRecords": 30, "locator": locator})
    assert replay.text == second.text and second.headers["Sforce-Locator"] == "null"
    assert (
        len(list(csv.DictReader(io.StringIO(first.text))))
        + len(list(csv.DictReader(io.StringIO(second.text))))
        == 41
    )
    assert error(await admin.get(f"/jobs/query/{other}/results", params={"locator": locator})) == "InvalidJob"
    for bad in (
        "☃",
        "%%%",
        base64.urlsafe_b64encode(f"{job}:-1".encode()).decode(),
        base64.urlsafe_b64encode(f"{job}:99999".encode()).decode(),
    ):
        assert error(await admin.get(f"/jobs/query/{job}/results", params={"locator": bad})) == "InvalidJob"


@pytest.mark.parametrize("value", [["long-enough-password"], {"password": "long-enough-password"}])
async def test_password_changes_require_a_string(admin: Actor, value: Any) -> None:
    assert (
        error(await admin.post(f"/sobjects/User/{admin.user_id}/password", {"NewPassword": value}))
        == "JSON_PARSER_ERROR"
    )


async def test_reset_password_returns_the_new_working_password(admin: Actor) -> None:
    rid = (await admin.rows("SELECT Id FROM User WHERE Username = '" + username("hannah.schmidt") + "'"))[0][
        "Id"
    ]
    response = await admin.delete(f"/sobjects/User/{rid}/password")
    assert response.status_code == 200
    form = {
        "grant_type": "password",
        "client_id": "audit",
        "username": username("hannah.schmidt"),
        "password": response.json()["NewPassword"],
    }
    assert (await admin.client.post(f"{LOGIN}/services/oauth2/token", data=form)).status_code == 200
    form["password"] = PASSWORD
    assert (await admin.client.post(f"{LOGIN}/services/oauth2/token", data=form)).status_code == 400


async def test_unicode_blob_filename_download_and_invalid_unicode(admin: Actor) -> None:
    name = 'résumé-文档"\r\n.txt'
    rid = await admin.create(
        "ContentVersion",
        {
            "Title": "Unicode file",
            "PathOnClient": name,
            "VersionData": base64.b64encode(b"file bytes").decode(),
        },
    )
    response = await admin.get(f"/sobjects/ContentVersion/{rid}/VersionData")
    assert response.status_code == 200 and response.content == b"file bytes"
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "filename*=UTF-8''" + quote(name, safe="") in disposition
    assert error(await admin.post("/sobjects/Account", content=b'{"Name":"\\ud800"}')) == "JSON_PARSER_ERROR"


async def test_invalid_content_type_cannot_break_blob_download_headers(admin: Actor) -> None:
    account = (await admin.ids("SELECT Id FROM Account LIMIT 1"))[0]
    for content_type in ("☃", "text/plain\r\nInjected: yes"):
        rid = await admin.create(
            "Attachment",
            {
                "ParentId": account,
                "Name": "audit.txt",
                "ContentType": content_type,
                "Body": base64.b64encode(b"safe bytes").decode(),
            },
        )
        response = await admin.get(f"/sobjects/Attachment/{rid}/Body")
        assert response.status_code == 200 and response.content == b"safe bytes"
        assert response.headers["content-type"] == "application/octet-stream"


async def test_clearing_line_item_total_is_a_dml_error_and_preserves_rollups(admin: Actor) -> None:
    item = (await admin.rows("SELECT Id, TotalPrice, OpportunityId FROM OpportunityLineItem LIMIT 1"))[0]
    before = (await admin.rows(f"SELECT Amount FROM Opportunity WHERE Id = '{item['OpportunityId']}'"))[0]
    assert (
        error(await admin.patch(f"/sobjects/OpportunityLineItem/{item['Id']}", {"TotalPrice": None}))
        == "REQUIRED_FIELD_MISSING"
    )
    saved = (await admin.get(f"/sobjects/OpportunityLineItem/{item['Id']}")).json()
    assert saved["TotalPrice"] == item["TotalPrice"]
    assert (await admin.rows(f"SELECT Amount FROM Opportunity WHERE Id = '{item['OpportunityId']}'"))[
        0
    ] == before


async def test_line_item_quantity_only_update_preserves_total_price(admin: Actor) -> None:
    item = (await admin.rows("SELECT Id, Quantity, TotalPrice, UnitPrice FROM OpportunityLineItem LIMIT 1"))[
        0
    ]
    quantity = item["Quantity"] * 2
    assert (
        await admin.patch(f"/sobjects/OpportunityLineItem/{item['Id']}", {"Quantity": quantity})
    ).status_code == 204
    saved = (await admin.get(f"/sobjects/OpportunityLineItem/{item['Id']}")).json()
    assert saved["TotalPrice"] == item["TotalPrice"] and saved["UnitPrice"] == round(item["UnitPrice"] / 2, 2)
    assert (
        error(
            await admin.patch(
                f"/sobjects/OpportunityLineItem/{item['Id']}", {"UnitPrice": 100, "TotalPrice": 200}
            )
        )
        == "FIELD_INTEGRITY_EXCEPTION"
    )
    assert (
        error(await admin.patch(f"/sobjects/OpportunityLineItem/{item['Id']}", {"UnitPrice": None}))
        == "REQUIRED_FIELD_MISSING"
    )


async def test_new_line_items_require_an_explicit_unit_or_total_price(admin: Actor) -> None:
    item = (await admin.rows("SELECT OpportunityId, PricebookEntryId FROM OpportunityLineItem LIMIT 1"))[0]
    response = await admin.post(
        "/sobjects/OpportunityLineItem",
        {"OpportunityId": item["OpportunityId"], "PricebookEntryId": item["PricebookEntryId"], "Quantity": 2},
    )
    assert error(response) == "REQUIRED_FIELD_MISSING" and response.json()[0]["fields"] == ["UnitPrice"]


async def test_event_start_overflow_preserves_record(admin: Actor) -> None:
    event = (await admin.rows("SELECT Id, StartDateTime, EndDateTime FROM Event LIMIT 1"))[0]
    assert (
        error(await admin.patch(f"/sobjects/Event/{event['Id']}", {"StartDateTime": "9999-12-31T23:59:59Z"}))
        == "FIELD_INTEGRITY_EXCEPTION"
    )
    saved = (await admin.get(f"/sobjects/Event/{event['Id']}")).json()
    assert saved["StartDateTime"] == event["StartDateTime"] and saved["EndDateTime"] == event["EndDateTime"]


async def test_deeply_nested_soql_functions_are_a_query_error(admin: Actor) -> None:
    text = "SELECT " + "FORMAT(" * 500 + "Name" + ")" * 500 + " FROM Account"
    assert error(await admin.get("/query", params={"q": text})) == "MALFORMED_QUERY"


@pytest.mark.parametrize("metadata", [b'{"Name":NaN}', b'{"Name":Infinity}', b"[1]", b"null", b'"text"'])
async def test_multipart_metadata_uses_the_same_json_validation(admin: Actor, metadata: bytes) -> None:
    raw = (
        b'--audit-boundary\r\nContent-Disposition: form-data; name="entity_content"\r\n'
        b"Content-Type: application/json\r\n\r\n" + metadata + b"\r\n--audit-boundary--\r\n"
    )
    response = await admin.post(
        "/sobjects/Account",
        content=raw,
        headers={"Content-Type": "multipart/form-data; boundary=audit-boundary"},
    )
    assert error(response) == "JSON_PARSER_ERROR"
