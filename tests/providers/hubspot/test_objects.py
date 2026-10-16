from __future__ import annotations

import httpx
import pytest

CREATES = {
    "contacts": {"firstname": "New", "email": "new.contact@consumer.example", "lastname": "Contact"},
    "companies": {"name": "New company", "domain": "consumer.example"},
    "deals": {"dealname": "New deal", "amount": "100.50", "pipeline": "default", "dealstage": "contractsent"},
    "tickets": {
        "subject": "New ticket",
        "hs_pipeline": "0",
        "hs_pipeline_stage": "1",
        "hs_ticket_priority": "LOW",
    },
    "products": {"name": "Service", "price": "10.00", "description": "New service"},
    "line_items": {"name": "Service line", "price": "10.00", "quantity": "2"},
    "meetings": {
        "hs_meeting_title": "Client check-in",
        "hs_timestamp": "2026-10-16T12:00:00Z",
        "hs_meeting_start_time": "1792152000000",
        "hs_meeting_end_time": "2026-10-16T12:30:00Z",
    },
    "emails": {
        "hs_email_subject": "Client update",
        "hs_email_text": "Update",
        "hs_email_direction": "EMAIL",
        "hs_timestamp": "2026-10-16T12:00:00Z",
    },
    "notes": {"hs_note_body": "Client note", "hs_timestamp": "2026-10-16T12:00:00Z"},
    "tasks": {
        "hs_task_subject": "Follow up",
        "hs_task_status": "NOT_STARTED",
        "hs_timestamp": "2026-10-16T12:00:00Z",
    },
    "calls": {
        "hs_call_title": "Client call",
        "hs_call_status": "COMPLETED",
        "hs_timestamp": "2026-10-16T12:00:00Z",
    },
}


@pytest.mark.parametrize("kind", CREATES)
async def test_seed_list_crud_history_and_archive(client: httpx.AsyncClient, kind: str) -> None:
    base = "/crm/v3/objects/" + kind
    seeded = await client.get(base, params={"limit": 2})
    assert seeded.status_code == 200 and len(seeded.json()["results"]) == 2
    first_ids = [r["id"] for r in seeded.json()["results"]]
    second = await client.get(seeded.json()["paging"]["next"]["link"])
    assert not set(first_ids) & {r["id"] for r in second.json()["results"]}
    created = await client.post(base, json={"properties": CREATES[kind]})
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["id"].isdigit() and record["properties"]["hs_object_id"] == record["id"]
    prop = next(iter(CREATES[kind]))
    assert record["properties"][prop] == CREATES[kind][prop]
    assert record["archived"] is False
    assert record["createdAt"].endswith("Z")
    updated = await client.patch(base + "/" + record["id"], json={"properties": {prop: "Updated value"}})
    assert updated.status_code == 200, updated.text
    read = (
        await client.get(
            base + "/" + record["id"], params={"properties": prop, "propertiesWithHistory": prop}
        )
    ).json()
    assert read["properties"][prop] == "Updated value"
    assert [h["value"] for h in read["propertiesWithHistory"][prop]] == [CREATES[kind][prop], "Updated value"]
    assert (await client.delete(base + "/" + record["id"])).status_code == 204
    assert (await client.get(base + "/" + record["id"])).status_code == 404
    archived = await client.get(base + "/" + record["id"], params={"archived": "true"})
    assert archived.status_code == 200 and archived.json()["archived"] is True
    assert "archivedAt" in archived.json()


async def test_id_property_repeated_properties_unknown_property_and_null(client: httpx.AsyncClient) -> None:
    base = "/crm/v3/objects/contacts"
    item = (await client.post(base, json={"properties": CREATES["contacts"]})).json()
    email = CREATES["contacts"]["email"]
    result = await client.get(
        base + "/" + email,
        params=[
            ("idProperty", "email"),
            ("properties", "email"),
            ("properties", "firstname,phone,does_not_exist"),
        ],
    )
    assert result.status_code == 200
    props = result.json()["properties"]
    assert props["email"] == email and props["firstname"] == "New"
    assert props["phone"] is None and "does_not_exist" not in props
    update = await client.patch(
        base + "/" + email, params={"idProperty": "email"}, json={"properties": {"phone": None}}
    )
    assert update.status_code == 200 and update.json()["properties"]["phone"] == ""
    duplicate = await client.post(base, json={"properties": {"email": email.upper()}})
    assert duplicate.status_code == 409
    assert (await client.get(base + "/New", params={"idProperty": "firstname"})).status_code == 400
    assert (await client.get(base + "/" + item["id"])).json()["id"] == item["id"]


@pytest.mark.parametrize("kind", CREATES)
async def test_batches_read_create_update_upsert_archive(client: httpx.AsyncClient, kind: str) -> None:
    base = "/crm/v3/objects/" + kind + "/batch/"
    created = await client.post(
        base + "create", json={"inputs": [{"properties": CREATES[kind], "objectWriteTraceId": "trace-1"}]}
    )
    assert created.status_code == 201, created.text
    result = created.json()
    assert result["status"] == "COMPLETE" and result["startedAt"] <= result["completedAt"]
    item = result["results"][0]
    assert item["objectWriteTraceId"] == "trace-1"
    prop = next(iter(CREATES[kind]))
    read = await client.post(base + "read", json={"inputs": [{"id": item["id"]}], "properties": [prop]})
    assert read.status_code == 200 and read.json()["results"][0]["properties"][prop] == CREATES[kind][prop]
    updated = await client.post(
        base + "update", json={"inputs": [{"id": item["id"], "properties": {prop: "Batch changed"}}]}
    )
    assert updated.status_code == 200 and updated.json()["results"][0]["properties"][prop] == "Batch changed"
    archived = await client.post(base + "archive", json={"inputs": [{"id": item["id"]}]})
    assert archived.status_code == 204 and archived.content == b""
    gone = await client.post(base + "read", json={"inputs": [{"id": item["id"]}]})
    assert gone.status_code == 207 and gone.json()["numErrors"] == 1


async def test_upsert_by_email_and_custom_unique_property(client: httpx.AsyncClient) -> None:
    base = "/crm/v3/objects/contacts"
    inputs = [{"id": "unique@consumer.example", "idProperty": "email", "properties": {"firstname": "Before"}}]
    first = await client.post(base + "/batch/upsert", json={"inputs": inputs})
    assert first.status_code == 200 and first.json()["results"][0]["new"] is True
    inputs[0]["properties"]["firstname"] = "After"
    second = await client.post(base + "/batch/upsert", json={"inputs": inputs})
    assert second.status_code == 200 and second.json()["results"][0]["new"] is False
    assert second.json()["results"][0]["id"] == first.json()["results"][0]["id"]
    read = await client.post(
        base + "/batch/read", json={"idProperty": "email", "inputs": [{"id": inputs[0]["id"]}]}
    )
    assert read.status_code == 200 and read.json()["results"][0]["properties"]["firstname"] == "After"
    custom = await client.post(
        "/crm/v3/properties/companies",
        json={
            "name": "external_key",
            "label": "External key",
            "type": "string",
            "fieldType": "text",
            "groupName": "companyinformation",
            "hasUniqueValue": True,
        },
    )
    assert custom.status_code == 201
    payload = {
        "inputs": [
            {"idProperty": "external_key", "id": "client-123", "properties": {"name": "Upserted company"}}
        ]
    }
    company = await client.post("/crm/v3/objects/companies/batch/upsert", json=payload)
    assert company.status_code == 200 and company.json()["results"][0]["new"] is True
    reread = await client.get(
        "/crm/v3/objects/companies/client-123",
        params={"idProperty": "external_key", "properties": "name,external_key"},
    )
    assert reread.status_code == 200 and reread.json()["properties"]["external_key"] == "client-123"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"properties": []},
        {"properties": {"unknown": "x"}},
        {"properties": {"createdate": "2026-10-16"}},
        {"properties": {"email": "bad"}},
        {"properties": {"email": {"x": 1}}},
        {"properties": {"lifecyclestage": "not_a_stage"}},
    ],
)
async def test_invalid_object_bodies(client: httpx.AsyncClient, payload: object) -> None:
    result = await client.post(
        "/crm/v3/objects/contacts",
        content="null" if payload is None else __import__("json").dumps(payload),
        headers={"content-type": "application/json"},
    )
    assert result.status_code == 400, result.text
    assert result.json()["category"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 101},
        {"limit": 0},
        {"limit": "x"},
        {"after": -1},
        {"after": "not-a-cursor"},
        {"archived": "yes"},
    ],
)
async def test_list_parameter_validation(client: httpx.AsyncClient, params: dict) -> None:
    assert (await client.get("/crm/v3/objects/contacts", params=params)).status_code == 400


async def test_batch_limit_partial_errors_and_failed_create_is_atomic(client: httpx.AsyncClient) -> None:
    base = "/crm/v3/objects/contacts"
    assert (await client.post(base + "/batch/read", json={"inputs": [{"id": "1"}] * 101})).status_code == 400
    assert (await client.post(base + "/batch/read", json={"inputs": []})).status_code == 400
    before = (await client.post(base + "/search", json={})).json()["total"]
    bad_association = await client.post(
        base,
        json={
            "properties": CREATES["contacts"],
            "associations": [
                {
                    "to": {"id": "0"},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}],
                }
            ],
        },
    )
    assert bad_association.status_code == 404
    assert (await client.post(base + "/search", json={})).json()["total"] == before
    first = (await client.get(base)).json()["results"][0]
    partial = await client.post(
        base + "/batch/read", json={"inputs": [{"id": first["id"]}, {"id": "missing"}]}
    )
    assert partial.status_code == 207 and len(partial.json()["results"]) == 1
    assert partial.json()["numErrors"] == 1
