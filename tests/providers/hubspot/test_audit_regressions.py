"""Regression tests from the adversarial fidelity audit."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx


async def test_mint_token_rejects_malformed_control_payloads(client: httpx.AsyncClient) -> None:
    for payload in (
        {"expires_in": "abc"},
        {"expires_in": True},
        {"expires_in": 10**20},
        {"expires_in": 1.5},
        {"scopes": "crm.objects.contacts.read"},
        {"scopes": [123]},
        {"app_only": "false", "user": "p018"},
        {"user": 123},
    ):
        result = await client.post("/_emulator/tokens", json=payload)
        assert result.status_code == 400, (payload, result.text)


async def test_pipeline_defaults_and_company_name_or_domain(client: httpx.AsyncClient) -> None:
    deal = await client.post(
        "/crm/v3/objects/deals",
        json={"properties": {"dealname": "No pipeline", "dealstage": "appointmentscheduled"}},
    )
    assert deal.status_code == 201, deal.text
    assert deal.json()["properties"]["pipeline"] == "default"
    assert deal.json()["properties"]["hs_deal_stage_probability"] == "0.2"
    assert deal.json()["properties"]["hs_is_closed"] == "false"
    ticket = await client.post(
        "/crm/v3/objects/tickets",
        json={"properties": {"subject": "No pipeline", "hs_pipeline_stage": "1"}},
    )
    assert ticket.status_code == 201, ticket.text
    assert ticket.json()["properties"]["hs_pipeline"] == "0"
    company = await client.post(
        "/crm/v3/objects/companies", json={"properties": {"domain": "domain-only.example"}}
    )
    assert company.status_code == 201, company.text
    neither = await client.post("/crm/v3/objects/companies", json={"properties": {"city": "Nowhere"}})
    assert neither.status_code == 400


async def test_deal_calculated_properties_follow_stage_changes(client: httpx.AsyncClient) -> None:
    stages = (await client.get("/crm/v3/pipelines/deals/default/stages")).json()["results"]
    expected = {s["id"]: s["metadata"]["probability"] for s in stages}
    deals = (
        await client.get(
            "/crm/v3/objects/deals",
            params={"limit": 100, "properties": "dealstage,hs_deal_stage_probability,hs_is_closed_won"},
        )
    ).json()["results"]
    assert deals
    for deal in deals:
        props = deal["properties"]
        assert float(props["hs_deal_stage_probability"]) == float(expected[props["dealstage"]])
        assert props["hs_is_closed_won"] == str(props["dealstage"] == "closedwon").lower()
    created = (
        await client.post(
            "/crm/v3/objects/deals",
            json={
                "properties": {
                    "dealname": "Stage change",
                    "pipeline": "default",
                    "dealstage": "appointmentscheduled",
                }
            },
        )
    ).json()
    updated = await client.patch(
        "/crm/v3/objects/deals/" + created["id"], json={"properties": {"dealstage": "closedwon"}}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["properties"]["hs_deal_stage_probability"] == "1.0"
    assert updated.json()["properties"]["hs_is_closed"] == "true"
    assert updated.json()["properties"]["hs_is_closed_won"] == "true"
    rejected = await client.patch(
        "/crm/v3/objects/deals/" + created["id"],
        json={"properties": {"hs_deal_stage_probability": "0.5"}},
    )
    assert rejected.status_code == 400


async def test_no_seeded_record_is_created_in_the_future(client: httpx.AsyncClient) -> None:
    meetings = await client.post(
        "/crm/v3/objects/meetings/search",
        json={
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "hs_createdate",
                            "operator": "GT",
                            "value": "2026-10-16T00:00:00.000Z",
                        }
                    ]
                }
            ]
        },
    )
    assert meetings.status_code == 200, meetings.text
    assert meetings.json()["total"] == 0


async def test_id_property_lookup_after_archive_and_recreate(client: httpx.AsyncClient) -> None:
    base = "/crm/v3/objects/contacts"
    first = (await client.post(base, json={"properties": {"email": "reuse@consumer.example"}})).json()
    await client.delete(base + "/" + first["id"])
    second = (
        await client.post(
            base, json={"properties": {"email": "reuse@consumer.example", "firstname": "Second"}}
        )
    ).json()
    found = await client.get(base + "/reuse@consumer.example", params={"idProperty": "email"})
    assert found.status_code == 200, found.text
    assert found.json()["id"] == second["id"]
    archived = await client.get(
        base + "/reuse@consumer.example", params={"idProperty": "email", "archived": "true"}
    )
    assert archived.status_code == 200 and archived.json()["id"] == first["id"]


async def test_patch_with_unchanged_values_is_a_no_op(client: httpx.AsyncClient) -> None:
    created = (
        await client.post(
            "/crm/v3/objects/contacts",
            json={"properties": {"email": "noop@consumer.example", "firstname": "Same"}},
        )
    ).json()
    before = (
        await client.get(
            "/crm/v3/objects/contacts/" + created["id"],
            params={"propertiesWithHistory": "firstname"},
        )
    ).json()
    patched = (
        await client.patch(
            "/crm/v3/objects/contacts/" + created["id"], json={"properties": {"firstname": "Same"}}
        )
    ).json()
    assert patched["updatedAt"] == before["updatedAt"]
    after = (
        await client.get(
            "/crm/v3/objects/contacts/" + created["id"],
            params={"propertiesWithHistory": "firstname"},
        )
    ).json()
    assert after["propertiesWithHistory"]["firstname"] == before["propertiesWithHistory"]["firstname"]


async def test_upsert_by_record_id_and_batch_update_id_property(client: httpx.AsyncClient) -> None:
    base = "/crm/v3/objects/contacts"
    created = (await client.post(base, json={"properties": {"email": "byid@consumer.example"}})).json()
    for payload in (
        {"inputs": [{"id": created["id"], "properties": {"firstname": "Defaulted"}}]},
        {
            "inputs": [
                {"id": created["id"], "idProperty": "hs_object_id", "properties": {"firstname": "ById"}}
            ]
        },
    ):
        result = await client.post(base + "/batch/upsert", json=payload)
        assert result.status_code == 200, result.text
        assert result.json()["results"][0]["new"] is False
        assert result.json()["results"][0]["id"] == created["id"]
    missing = await client.post(
        base + "/batch/upsert",
        json={"inputs": [{"id": "424242", "idProperty": "hs_object_id", "properties": {"firstname": "X"}}]},
    )
    assert missing.status_code == 207 and missing.json()["numErrors"] == 1
    updated = await client.post(
        base + "/batch/update",
        json={
            "inputs": [
                {"id": "byid@consumer.example", "idProperty": "email", "properties": {"lastname": "Batch"}}
            ]
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["results"][0]["properties"]["lastname"] == "Batch"


async def test_batch_create_is_atomic_unless_multi_status_opted_in(client: httpx.AsyncClient) -> None:
    base = "/crm/v3/objects/contacts"
    before = (await client.post(base + "/search", json={})).json()["total"]
    inputs = [
        {"properties": {"email": "atomic@consumer.example"}},
        {"properties": {"email": "atomic@consumer.example"}},
    ]
    failed = await client.post(base + "/batch/create", json={"inputs": inputs})
    assert failed.status_code == 409, failed.text
    assert (await client.post(base + "/search", json={})).json()["total"] == before
    traced = [{**item, "objectWriteTraceId": f"trace-{i}"} for i, item in enumerate(inputs)]
    partial = await client.post(base + "/batch/create", json={"inputs": traced})
    assert partial.status_code == 207, partial.text
    assert len(partial.json()["results"]) == 1 and partial.json()["numErrors"] == 1
    assert (await client.post(base + "/search", json={})).json()["total"] == before + 1


async def test_search_filter_value_types(client: httpx.AsyncClient) -> None:
    for value in (["x"], {"x": 1}, None, True):
        result = await client.post(
            "/crm/v3/objects/contacts/search",
            json={
                "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": value}]}]
            },
        )
        assert result.status_code == 400, (value, result.text)
    numeric_in = await client.post(
        "/crm/v3/objects/deals/search",
        json={
            "filterGroups": [{"filters": [{"propertyName": "amount", "operator": "GT", "value": 0}]}],
            "limit": 1,
        },
    )
    assert numeric_in.status_code == 200 and numeric_in.json()["total"] > 0


async def test_v4_association_ids_are_integers_and_v3_types_deduped(client: httpx.AsyncClient) -> None:
    contact = (await client.get("/crm/v3/objects/contacts", params={"limit": 1})).json()["results"][0]
    links = (await client.get(f"/crm/v4/objects/contacts/{contact['id']}/associations/companies")).json()[
        "results"
    ]
    assert links and isinstance(links[0]["toObjectId"], int)
    expanded = (
        await client.get(f"/crm/v3/objects/contacts/{contact['id']}", params={"associations": "companies"})
    ).json()["associations"]["companies"]["results"]
    assert expanded == [{"id": str(links[0]["toObjectId"]), "type": "contact_to_company"}]


async def test_v4_and_v3_batch_association_writes(client: httpx.AsyncClient) -> None:
    contact = (
        await client.post("/crm/v3/objects/contacts", json={"properties": {"firstname": "BatchLink"}})
    ).json()
    companies = (await client.get("/crm/v3/objects/companies", params={"limit": 2})).json()["results"]
    first, second = companies
    created = await client.post(
        "/crm/v4/associations/contacts/companies/batch/create",
        json={
            "inputs": [
                {
                    "from": {"id": contact["id"]},
                    "to": {"id": first["id"]},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}],
                }
            ]
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["results"][0]["labels"] == ["Primary"]
    assert created.json()["results"][0]["toObjectId"] == int(first["id"])
    defaulted = await client.post(
        "/crm/v4/associations/contacts/companies/batch/associate/default",
        json={"inputs": [{"from": {"id": contact["id"]}, "to": {"id": second["id"]}}]},
    )
    assert defaulted.status_code == 200, defaulted.text
    assert defaulted.json()["results"][0]["associationSpec"]["associationTypeId"] == 279
    links = (await client.get(f"/crm/v4/objects/contacts/{contact['id']}/associations/companies")).json()[
        "results"
    ]
    assert {row["toObjectId"] for row in links} == {int(first["id"]), int(second["id"])}
    labels_removed = await client.post(
        "/crm/v4/associations/contacts/companies/batch/labels/archive",
        json={
            "inputs": [
                {
                    "from": {"id": contact["id"]},
                    "to": {"id": first["id"]},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}],
                }
            ]
        },
    )
    assert labels_removed.status_code == 204, labels_removed.text
    links = (await client.get(f"/crm/v4/objects/contacts/{contact['id']}/associations/companies")).json()[
        "results"
    ]
    first_links = next(row for row in links if row["toObjectId"] == int(first["id"]))
    assert {t["typeId"] for t in first_links["associationTypes"]} == {279}
    v3_read = await client.post(
        "/crm/v3/associations/contacts/companies/batch/read",
        json={"inputs": [{"id": contact["id"]}]},
    )
    assert v3_read.status_code == 200, v3_read.text
    assert {t["type"] for t in v3_read.json()["results"][0]["to"]} == {"contact_to_company"}
    archived = await client.post(
        "/crm/v4/associations/contacts/companies/batch/archive",
        json={"inputs": [{"from": {"id": contact["id"]}, "to": [{"id": first["id"]}, {"id": second["id"]}]}]},
    )
    assert archived.status_code == 204, archived.text
    assert (await client.get(f"/crm/v4/objects/contacts/{contact['id']}/associations/companies")).json()[
        "results"
    ] == []
    v3_created = await client.post(
        "/crm/v3/associations/contacts/companies/batch/create",
        json={
            "inputs": [
                {"from": {"id": contact["id"]}, "to": {"id": first["id"]}, "type": "contact_to_company"}
            ]
        },
    )
    assert v3_created.status_code == 201, v3_created.text
    v3_archived = await client.post(
        "/crm/v3/associations/contacts/companies/batch/archive",
        json={
            "inputs": [
                {"from": {"id": contact["id"]}, "to": {"id": first["id"]}, "type": "contact_to_company"}
            ]
        },
    )
    assert v3_archived.status_code == 204, v3_archived.text


async def test_batch_association_malformed_inputs_do_not_crash(client: httpx.AsyncClient) -> None:
    for path in (
        "/crm/v4/associations/contacts/companies/batch/create",
        "/crm/v4/associations/contacts/companies/batch/associate/default",
        "/crm/v3/associations/contacts/companies/batch/create",
    ):
        result = await client.post(path, json={"inputs": [{"from": "not-an-object", "to": 42}]})
        assert result.status_code == 207, (path, result.text)
        assert result.json()["numErrors"] == 1
    archived = await client.post(
        "/crm/v4/associations/contacts/companies/batch/archive",
        json={"inputs": [{"from": {"id": "1"}, "to": "not-a-list"}]},
    )
    assert archived.status_code == 400, archived.text


async def test_property_patch_group_patch_delete_and_duplicate_options(
    client: httpx.AsyncClient,
) -> None:
    base = "/crm/v3/properties/contacts"
    duplicate = await client.post(
        base,
        json={
            "name": "dup_option_prop",
            "label": "Dup",
            "type": "enumeration",
            "fieldType": "select",
            "groupName": "contactinformation",
            "options": [{"label": "A", "value": "a"}, {"label": "Again", "value": "a"}],
        },
    )
    assert duplicate.status_code == 400, duplicate.text
    made = await client.post(
        base,
        json={
            "name": "patchable_prop",
            "label": "Before",
            "type": "string",
            "fieldType": "text",
            "groupName": "contactinformation",
        },
    )
    assert made.status_code == 201 and made.json()["createdAt"]
    patched = await client.patch(base + "/patchable_prop", json={"label": "After"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["label"] == "After"
    rejected = await client.patch(base + "/patchable_prop", json={"type": "number"})
    assert rejected.status_code == 400
    group = await client.post(base + "/groups", json={"name": "patch_group", "label": "Before"})
    assert group.status_code == 201
    renamed = await client.patch(base + "/groups/patch_group", json={"label": "After"})
    assert renamed.status_code == 200 and renamed.json()["label"] == "After"
    assert (await client.delete(base + "/groups/patch_group")).status_code == 204
    labels = await client.post(
        "/crm/v4/associations/contacts/companies/labels",
        json={"name": "audit_label", "label": "Audit label"},
    )
    assert labels.status_code == 200, labels.text


async def test_correlation_and_rate_limit_headers(client: httpx.AsyncClient) -> None:
    ok = await client.get("/crm/v3/objects/contacts", params={"limit": 1})
    assert ok.headers["x-hubspot-correlation-id"]
    assert ok.headers["x-hubspot-ratelimit-daily"] == "1000000"
    assert int(ok.headers["x-hubspot-ratelimit-daily-remaining"]) < 1000000
    assert ok.headers["x-hubspot-ratelimit-interval-milliseconds"] == "10000"
    missing = await client.get("/crm/v3/objects/contacts/424242")
    assert missing.status_code == 404
    assert missing.headers["x-hubspot-correlation-id"] == missing.json()["correlationId"]


async def test_unsupported_optional_scope_is_dropped(client: httpx.AsyncClient) -> None:
    redirect = "https://consumer.example/callback"
    authorized = await client.get(
        "https://app.hubspot.com/oauth/authorize",
        params={
            "client_id": "optional-scope-client",
            "redirect_uri": redirect,
            "scope": "oauth crm.objects.contacts.read",
            "optional_scope": "crm.objects.quotes.read",
            "login_hint": "p018",
        },
    )
    assert authorized.status_code == 302, authorized.text
    code = parse_qs(urlparse(authorized.headers["location"]).query)["code"][0]
    tokens = await client.post(
        "/oauth/v1/token",
        data={
            "grant_type": "authorization_code",
            "client_id": "optional-scope-client",
            "client_secret": "secret",
            "redirect_uri": redirect,
            "code": code,
        },
    )
    assert tokens.status_code == 200, tokens.text
    info = (await client.get("/oauth/v1/access-tokens/" + tokens.json()["access_token"])).json()
    assert info["scopes"] == ["oauth", "crm.objects.contacts.read"]


async def test_activity_timestamp_default_lead_status_and_trailing_slash(
    client: httpx.AsyncClient,
) -> None:
    note = await client.post(
        "/crm/v3/objects/notes", json={"properties": {"hs_note_body": "No timestamp supplied"}}
    )
    assert note.status_code == 201, note.text
    assert note.json()["properties"]["hs_timestamp"] == note.json()["createdAt"]
    definitions = (await client.get("/crm/v3/properties/contacts")).json()["results"]
    lead_status = next((p for p in definitions if p["name"] == "hs_lead_status"), None)
    assert lead_status is not None and lead_status["type"] == "enumeration"
    contact = await client.post(
        "/crm/v3/objects/contacts",
        json={"properties": {"email": "leadstatus@consumer.example", "hs_lead_status": "OPEN"}},
    )
    assert contact.status_code == 201, contact.text
    slash = await client.get("/crm/v3/objects/contacts/", params={"limit": 1})
    assert slash.status_code == 200, slash.text
    owners = (await client.get("/crm/v3/owners", params={"limit": 1})).json()["results"]
    filtered = await client.get("/crm/v3/owners", params={"userId": owners[0]["userId"]})
    assert [o["id"] for o in filtered.json()["results"]] == [owners[0]["id"]]
