"""HTTP contracts for relational records, auth, validation, cursors and attachments."""

from __future__ import annotations

import base64
import copy
import json
import re
import time
from datetime import timedelta
from typing import Any

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.providers.airtable.common import OWNER_TOKEN, READER_TOKEN

from .conftest import CONTENT, base_id, table_url


async def mint(client: httpx.AsyncClient, **options: Any) -> str:
    response = await client.post("/_emulator/tokens", json=options)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def test_seed_is_deterministic_fast_and_lazy() -> None:
    one = EmulatorHub(providers=["airtable"], anchor="2026-10-16")
    start = time.perf_counter()
    two = EmulatorHub(providers=["airtable"], anchor="2026-10-16")
    assert time.perf_counter() - start < 2
    a, b = one.context("airtable"), two.context("airtable")
    assert a.store.dump() == b.store.dump()
    assert not len(a.store.blobs)
    assert a.provider.default_port == 44119
    for kind in ("bases", "tables", "records", "users"):
        assert all(
            re.fullmatch(r"(?:app|tbl|rec|usr)[a-zA-Z0-9]{14}", r["id"]) for r in a.store.collection(kind)
        )
    assert len(a.store.collection("records")) > 500


async def test_schema_world_relationships_and_computed_fields(client: httpx.AsyncClient) -> None:
    bid = await base_id(client)
    response = await client.get(f"/v0/meta/bases/{bid}/tables", params={"include[]": "visibleFieldIds"})
    tables = {t["name"]: t for t in response.json()["tables"]}
    assert set(tables) == {"Employees", "Clients", "Engagements", "Documents"}
    for table in tables.values():
        assert table["primaryFieldId"] in [f["id"] for f in table["fields"]]
        assert all(re.fullmatch(r"fld[a-zA-Z0-9]{14}", f["id"]) for f in table["fields"])
        assert all(view["visibleFieldIds"] for view in table["views"])
    employees = (await client.get(f"/v0/{bid}/Employees")).json()["records"]
    assert sum(r["fields"].get("Active", False) for r in employees) == 45
    clients = (await client.get(f"/v0/{bid}/Clients")).json()["records"]
    assert len(clients) == 24
    employee_ids = {r["id"] for r in employees}
    assert all(r["fields"]["Owner"][0] in employee_ids for r in clients)
    finance = await base_id(client, finance=True)
    invoices = (await client.get(f"/v0/{finance}/Invoices", params={"pageSize": 100})).json()["records"]
    assert all(
        r["fields"].get("Balance", 0)
        == pytest.approx(r["fields"].get("Total", 0) - r["fields"].get("Paid", 0))
        for r in invoices
    )


@pytest.mark.parametrize("authorization", [None, "Bearer unknown", "Basic abc", "Bearer "])
async def test_authentication_required(client: httpx.AsyncClient, authorization: str | None) -> None:
    client.headers.pop("Authorization")
    response = await client.get(
        "/v0/meta/bases", headers={"Authorization": authorization} if authorization else {}
    )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "AUTHENTICATION_REQUIRED"


async def test_base_roles_scopes_expiry_revoke_and_inactive_users(client: httpx.AsyncClient) -> None:
    bid, finance = await base_id(client), await base_id(client, finance=True)
    headers = {"Authorization": "Bearer " + READER_TOKEN}
    assert len((await client.get("/v0/meta/bases", headers=headers)).json()["bases"]) == 1
    assert (await client.get(f"/v0/{finance}/Invoices", headers=headers)).status_code == 403
    assert (await client.get(f"/v0/{bid}/Clients", headers=headers)).status_code == 200
    assert (
        await client.post(f"/v0/{bid}/Clients", headers=headers, json={"fields": {"Name": "Denied"}})
    ).status_code == 403
    for extra in ({"scopes": []}, {"bases": []}, {"expires_in": -1}):
        token = await mint(client, user="p001", **extra)
        denied = await client.get(f"/v0/{bid}/Clients", headers={"Authorization": "Bearer " + token})
        assert denied.status_code in (401, 403)
    token = await mint(client, user="p001")
    assert (await client.post("/_emulator/actions/revoke_token", json={"token": token})).status_code == 200
    assert (
        await client.get("/v0/meta/bases", headers={"Authorization": "Bearer " + token})
    ).status_code == 401
    ctx = client.hub.context("airtable")
    owner = ctx.tokens.get(OWNER_TOKEN)
    ctx.store.collection("users").get(owner.user_id)["_active"] = False
    assert (await client.get("/v0/meta/bases")).status_code == 401


async def test_create_patch_put_omission_and_field_ids(client: httpx.AsyncClient) -> None:
    url = await table_url(client)
    created = await client.post(
        url, json={"fields": {"Name": "Synthetic client", "Notes": "Memo", "Status": "Prospect"}}
    )
    assert created.status_code == 200, created.text
    record = created.json()
    assert set(record) == {"id", "createdTime", "fields"}
    path = url + "/" + record["id"]
    patched = (await client.patch(path, json={"fields": {"Name": "Renamed", "Notes": None}})).json()
    assert patched["fields"] == {"Name": "Renamed", "Status": "Prospect"}
    replaced = (await client.put(path, json={"fields": {"Name": "Replacement"}})).json()
    assert replaced["fields"] == {"Name": "Replacement"}
    by_id = (await client.get(path, params={"returnFieldsByFieldId": "true"})).json()
    field_id = next(iter(by_id["fields"]))
    assert field_id.startswith("fld")
    changed = await client.patch(
        path, json={"fields": {field_id: "ID update"}, "returnFieldsByFieldId": True}
    )
    assert changed.json()["fields"] == {field_id: "ID update"}
    assert (await client.delete(path)).json() == {"id": record["id"], "deleted": True}
    assert (await client.get(path)).status_code == 404


async def test_batch_upsert_and_atomic_errors(client: httpx.AsyncClient) -> None:
    url = await table_url(client)
    created = (
        await client.post(url, json={"records": [{"fields": {"Name": "One"}}, {"fields": {"Name": "Two"}}]})
    ).json()["records"]
    before = copy.deepcopy(client.hub.context("airtable").store.dump())
    bad = await client.patch(
        url,
        json={
            "records": [
                {"id": created[0]["id"], "fields": {"Name": "Corrupt"}},
                {"id": created[1]["id"], "fields": {"Status": "Invalid"}},
            ]
        },
    )
    assert bad.status_code == 422
    assert client.hub.context("airtable").store.dump() == before
    upsert = await client.patch(
        url,
        json={
            "performUpsert": {"fieldsToMergeOn": ["Name"]},
            "records": [{"fields": {"Name": "One", "Notes": "Updated"}}, {"fields": {"Name": "Three"}}],
        },
    )
    assert upsert.status_code == 200, upsert.text
    assert upsert.json()["updatedRecords"] == [created[0]["id"]]
    assert len(upsert.json()["createdRecords"]) == 1
    ids = [r["id"] for r in created] + upsert.json()["createdRecords"]
    assert (await client.delete(url, params=[("records[]", rid) for rid in ids])).json()["records"] == [
        {"id": rid, "deleted": True} for rid in ids
    ]


async def test_relational_inverse_links_and_delete_cleanup(client: httpx.AsyncClient) -> None:
    bid = await base_id(client)
    clients = (await client.get(f"/v0/{bid}/Clients")).json()["records"]
    c1, c2 = clients[:2]
    await client.patch(f"/v0/{bid}/Clients/{c1['id']}", json={"fields": {"Engagements": None}})
    engagement = await client.post(
        f"/v0/{bid}/Engagements", json={"fields": {"Name": "Linked engagement", "Client": [c1["id"]]}}
    )
    rid = engagement.json()["id"]
    assert rid in (await client.get(f"/v0/{bid}/Clients/{c1['id']}")).json()["fields"]["Engagements"]
    await client.patch(f"/v0/{bid}/Engagements/{rid}", json={"fields": {"Client": [c2["id"]]}})
    assert rid not in (await client.get(f"/v0/{bid}/Clients/{c1['id']}")).json()["fields"].get(
        "Engagements", []
    )
    await client.delete(f"/v0/{bid}/Clients/{c2['id']}")
    assert "Client" not in (await client.get(f"/v0/{bid}/Engagements/{rid}")).json()["fields"]


async def test_cursor_pagination_binding_and_max_records(client: httpx.AsyncClient) -> None:
    url = await table_url(client)
    params = {"pageSize": 2, "maxRecords": 5, "sort[0][field]": "Name"}
    page = await client.get(url, params=params)
    seen = page.json()["records"]
    cursor = page.json()["offset"]
    repeated = await client.get(url, params={**params, "offset": cursor})
    assert (
        repeated.json()["records"]
        == (await client.get(url, params={**params, "offset": cursor})).json()["records"]
    )
    while "offset" in page.json():
        page = await client.get(url, params={**params, "offset": page.json()["offset"]})
        seen.extend(page.json()["records"])
    assert len(seen) == len({r["id"] for r in seen}) == 5
    for request_params, headers in [
        ({"pageSize": 3, "offset": cursor}, {}),
        ({**params, "offset": cursor}, {"Authorization": "Bearer " + READER_TOKEN}),
    ]:
        bad = await client.get(url, params=request_params, headers=headers)
        assert bad.status_code == 422 and bad.json()["error"]["type"] == "LIST_RECORDS_ITERATOR_NOT_AVAILABLE"
    client.hub.clock.advance(timedelta(seconds=3601))
    assert (await client.get(url, params={**params, "offset": cursor})).status_code == 422


@pytest.mark.parametrize(
    "formula,count",
    [
        ("{Status}='Active'", None),
        ("AND({Name}='Northwind Logistics',FIND('northwind',LOWER({Domain})))", 1),
        ("OR(FALSE(),RECORD_ID()='missing')", 0),
        ("IF({Name}='Northwind Logistics',TRUE(),FALSE())", 1),
        ("LEN({Name})>0", 24),
    ],
)
async def test_formula_subset_and_post_list(
    client: httpx.AsyncClient, formula: str, count: int | None
) -> None:
    url = await table_url(client)
    data = {"filterByFormula": formula, "fields": ["Name"], "sort": [{"field": "Name", "direction": "desc"}]}
    response = await client.post(url + "/listRecords", json=data)
    assert response.status_code == 200, response.text
    rows = response.json()["records"]
    if count is not None:
        assert len(rows) == count
    else:
        view = (await client.get(url, params={"view": "Active clients"})).json()["records"]
        assert {r["id"] for r in rows} == {r["id"] for r in view}
    assert all(set(r["fields"]) == {"Name"} for r in rows)


@pytest.mark.parametrize(
    "formula",
    [
        "__import__('os')",
        "AND(",
        "{Missing}=1",
        "(" * 50 + "1" + ")" * 50,
        "1e999",
        "{Name}.constructor",
        "SUM({Name})",
    ],
)
async def test_invalid_formulas_fail_without_execution(client: httpx.AsyncClient, formula: str) -> None:
    response = await client.get(await table_url(client), params={"filterByFormula": formula})
    assert response.status_code == 422 and response.json()["error"]["type"] == "INVALID_FILTER_BY_FORMULA"


@pytest.mark.parametrize(
    "options",
    [
        {"pageSize": 0},
        {"pageSize": 101},
        {"pageSize": True},
        {"maxRecords": -1},
        {"fields": "Name"},
        {"fields": ["Missing"]},
        {"sort": [{}]},
        {"sort": [{"field": "Name", "direction": "down"}]},
        {"sort": [{"field": "Name", "direction": []}]},
        {"sort": [{"field": "Name", "direction": {}}]},
        {"cellFormat": []},
        {"cellFormat": "string"},
        {"timeZone": "Bad/Zone"},
        {"userLocale": "invalid!"},
        {"recordMetadata": ["unknown"]},
        {"view": "Missing"},
        {"returnFieldsByFieldId": 1},
        {"unknown": True},
    ],
)
async def test_invalid_list_options(client: httpx.AsyncClient, options: dict[str, Any]) -> None:
    response = await client.post((await table_url(client)) + "/listRecords", json=options)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"fields": {"Unknown": "x"}},
        {"fields": {"Name": False}},
        {"fields": {"Status": []}},
        {"fields": {"Owner": ["recMissing00000000"]}},
        {"fields": [], "typecast": True},
        {"fields": {"Name": "x"}, "typecast": "true"},
        {"records": []},
        {"records": [{"fields": {}}] * 11},
        {"fields": {"Name": "\ud800"}},
    ],
)
async def test_invalid_record_payloads_are_atomic(client: httpx.AsyncClient, payload: dict[str, Any]) -> None:
    url = await table_url(client)
    before = copy.deepcopy(client.hub.context("airtable").store.dump())
    response = await client.post(
        url, content=json.dumps(payload), headers={"Content-Type": "application/json"}
    )
    assert response.status_code in (400, 404, 422), response.text
    assert client.hub.context("airtable").store.dump() == before


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"null",
        b"true",
        b"not json",
        b"\xff",
        b'{"fields":{"Name":NaN}}',
        b'{"fields":{"Name":Infinity}}',
        b'{"fields":{"Name":"\\ud800"}}',
    ],
)
async def test_invalid_json(client: httpx.AsyncClient, raw: bytes) -> None:
    response = await client.post(
        await table_url(client), content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400, response.text


async def test_numeric_bool_validation_typecast_and_computed_guard(client: httpx.AsyncClient) -> None:
    url = await table_url(client, "Engagements")
    for bad in (True, "NaN", float("inf"), 10**100):
        response = await client.post(
            url,
            content=json.dumps({"fields": {"Budget hours": bad}}),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code in (400, 422), response.text
    cast = await client.post(url, json={"fields": {"Name": "Typed", "Budget hours": "12"}, "typecast": True})
    assert cast.json()["fields"]["Budget hours"] == 12
    invoice_url = await table_url(client, "Invoices", finance=True)
    assert (await client.post(invoice_url, json={"fields": {"Balance": 1}})).status_code == 422


async def test_attachments_upload_download_expiry_and_host_modes(client: httpx.AsyncClient) -> None:
    bid = await base_id(client)
    url = await table_url(client, "Documents")
    created = (await client.post(url, json={"fields": {"Name": "Upload test", "Files": None}})).json()
    schema = (await client.get(f"/v0/meta/bases/{bid}/tables")).json()["tables"]
    field = next(f for t in schema if t["name"] == "Documents" for f in t["fields"] if f["name"] == "Files")
    upload_url = f"{CONTENT}/v0/{bid}/{created['id']}/{field['id']}/uploadAttachment"
    response = await client.post(
        upload_url,
        json={
            "file": base64.b64encode(b"Synthetic content").decode(),
            "filename": "sample.txt",
            "contentType": "text/plain",
        },
    )
    assert response.status_code == 200, response.text
    attachment = response.json()["fields"][field["id"]][0]
    assert attachment["url"].startswith("https://v5.airtableusercontent.com/")
    download = await client.get(attachment["url"], headers={"Authorization": ""})
    assert download.content == b"Synthetic content" and int(download.headers["Content-Length"]) == 17
    assert (await client.head(attachment["url"])).content == b""
    client.hub.clock.advance(timedelta(hours=2))
    assert (await client.get(attachment["url"])).status_code == 403
    client.headers["Authorization"] = "Bearer " + OWNER_TOKEN
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.hub.gateway),
        headers={"Authorization": "Bearer " + OWNER_TOKEN},
    ) as gateway:
        prefix = await gateway.get(f"http://localhost/airtable/v0/{bid}/Documents/{created['id']}")
        assert prefix.status_code == 200, prefix.text
        assert prefix.json()["fields"]["Files"][0]["url"].startswith("http://localhost/airtable/attachments/")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.hub.apps["airtable"]),
        headers={"Authorization": "Bearer " + OWNER_TOKEN},
    ) as dedicated:
        per_port = await dedicated.get(f"http://localhost:44119/v0/{bid}/Documents/{created['id']}")
        assert per_port.status_code == 200, per_port.text
        assert per_port.json()["fields"]["Files"][0]["url"].startswith("http://localhost:44119/attachments/")
    external = await client.post(
        url,
        json={"fields": {"Name": "Synthetic PDF", "Files": [{"url": "https://files.example/report.pdf"}]}},
    )
    pdf = external.json()["fields"]["Files"][0]
    assert pdf["type"] == "application/pdf" and (await client.get(pdf["url"])).content.startswith(b"%PDF-")


async def test_reset_restores_data_and_invalidates_cursors(client: httpx.AsyncClient) -> None:
    url = await table_url(client)
    cursor = (await client.get(url, params={"pageSize": 1})).json()["offset"]
    created = (await client.post(url, json={"fields": {"Name": "Reset"}})).json()["id"]
    await client.post("/_emulator/reset")
    assert (await client.get(url + "/" + created)).status_code == 404
    assert (await client.get(url, params={"pageSize": 1, "offset": cursor})).status_code == 422


async def test_field_id_formulas_string_cells_and_comment_metadata(client: httpx.AsyncClient) -> None:
    bid = await base_id(client)
    schema = (await client.get(f"/v0/meta/bases/{bid}/tables")).json()["tables"]
    engagement = next(t for t in schema if t["name"] == "Engagements")
    budget = next(f["id"] for f in engagement["fields"] if f["name"] == "Budget hours")
    url = f"/v0/{bid}/{engagement['id']}"
    response = await client.get(
        url,
        params=[
            ("filterByFormula", "{" + budget + "}>0"),
            ("fields[]", "Name"),
            ("fields[]", "Budget hours"),
            ("cellFormat", "string"),
            ("timeZone", "America/Toronto"),
            ("userLocale", "en-US"),
            ("recordMetadata[]", "commentCount"),
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json()["records"]
    assert all(
        r["commentCount"] == 0 and all(isinstance(v, str) for v in r["fields"].values())
        for r in response.json()["records"]
    )


async def test_ambiguous_upsert_and_missing_delete_do_not_mutate(client: httpx.AsyncClient) -> None:
    url = await table_url(client)
    created = (
        await client.post(
            url, json={"records": [{"fields": {"Name": "Duplicate"}}, {"fields": {"Name": "Duplicate"}}]}
        )
    ).json()["records"]
    before = copy.deepcopy(client.hub.context("airtable").store.dump())
    response = await client.patch(
        url,
        json={
            "performUpsert": {"fieldsToMergeOn": ["Name"]},
            "records": [{"fields": {"Name": "Duplicate", "Notes": "Rejected"}}],
        },
    )
    assert response.status_code == 422 and response.json()["error"]["type"] == "INVALID_UPSERT_RECORDS"
    assert client.hub.context("airtable").store.dump() == before
    failed = await client.delete(
        url, params=[("records[]", created[0]["id"]), ("records[]", "recMissing00000000")]
    )
    assert failed.status_code == 404 and client.hub.context("airtable").store.dump() == before


@pytest.mark.parametrize(
    "params",
    [
        [("pageSize", "1"), ("pageSize", "2")],
        [("sort[wrong]", "x")],
        [("sort[1][field]", "Name")],
        [("returnFieldsByFieldId", "yes")],
    ],
)
async def test_malformed_query_parameters(client: httpx.AsyncClient, params: list[tuple[str, str]]) -> None:
    response = await client.get(await table_url(client), params=params)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "options",
    [
        {"user": "unknown"},
        {"app_only": True},
        {"scopes": "data.records:read"},
        {"scopes": ["invalid"]},
        {"bases": ["appMissing00000000"]},
        {"expires_in": True},
        {"expires_in": 10**100},
    ],
)
async def test_invalid_control_tokens(client: httpx.AsyncClient, options: dict[str, Any]) -> None:
    assert (await client.post("/_emulator/tokens", json=options)).status_code == 422
