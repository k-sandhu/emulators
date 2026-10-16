from __future__ import annotations

import httpx
import pytest

from emulators.testing.helpers import bearer, mint


async def test_properties_groups_custom_types_and_archive(client: httpx.AsyncClient) -> None:
    base = "/crm/v3/properties/contacts"
    definitions = (await client.get(base)).json()["results"]
    email = next(p for p in definitions if p["name"] == "email")
    assert email["type"] == "string" and email["hasUniqueValue"] is True
    assert email["hubspotDefined"] is True
    assert (await client.get("/crm/v3/properties/0-1/email")).json() == email
    groups = (await client.get(base + "/groups")).json()["results"]
    assert groups[0]["name"] == "contactinformation"
    group = await client.post(
        base + "/groups", json={"name": "integration", "label": "Integration", "displayOrder": 1}
    )
    assert group.status_code == 201
    assert (await client.get(base + "/groups/integration")).json()["label"] == "Integration"
    prop = {
        "name": "meeting_preference",
        "label": "Meeting preference",
        "type": "enumeration",
        "fieldType": "select",
        "groupName": "integration",
        "options": [{"label": "Zoom", "value": "zoom"}, {"label": "Teams", "value": "teams"}],
    }
    result = await client.post(base, json=prop)
    assert result.status_code == 201, result.text
    assert result.json()["hubspotDefined"] is False
    assert result.json()["options"][0]["displayOrder"] == 0
    assert (await client.post(base, json=prop)).status_code == 409
    contact = (
        await client.post(
            "/crm/v3/objects/contacts",
            json={
                "properties": {
                    "firstname": "Custom",
                    "meeting_preference": "zoom",
                    "client_since": "2026-01-01",
                }
            },
        )
    ).json()
    assert contact["properties"]["meeting_preference"] == "zoom"
    assert (
        await client.patch(
            "/crm/v3/objects/contacts/" + contact["id"],
            json={"properties": {"meeting_preference": "invalid"}},
        )
    ).status_code == 400
    assert (await client.delete(base + "/email")).status_code == 400
    assert (await client.delete(base + "/meeting_preference")).status_code == 204
    assert (await client.get(base + "/meeting_preference")).status_code == 404
    assert (await client.get(base + "/meeting_preference", params={"archived": "true"})).json()[
        "archived"
    ] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "unsupported"},
        {"fieldType": "textarea"},
        {"groupName": "missing"},
        {"name": "hs_reserved"},
        {"name": "Bad Name"},
        {"options": []},
        {"hasUniqueValue": "true"},
    ],
)
async def test_property_validation(client: httpx.AsyncClient, changes: dict) -> None:
    prop = {
        "name": "custom_choice",
        "label": "Choice",
        "type": "enumeration",
        "fieldType": "select",
        "groupName": "contactinformation",
        "options": [{"label": "Yes", "value": "yes"}],
        **changes,
    }
    result = await client.post("/crm/v3/properties/contacts", json=prop)
    assert result.status_code == 400


async def test_pipeline_owners_and_account_shapes(client: httpx.AsyncClient) -> None:
    pipelines = await client.get("/crm/v3/pipelines/deals")
    assert pipelines.status_code == 200
    assert [p["label"] for p in pipelines.json()["results"]] == ["Sales Pipeline", "Advisory Engagements"]
    pipeline = (await client.get("/crm/v3/pipelines/deals/default")).json()
    stages = (await client.get("/crm/v3/pipelines/deals/default/stages")).json()["results"]
    assert pipeline["stages"] == stages and len(stages) == 7
    won = (await client.get("/crm/v3/pipelines/deals/default/stages/closedwon")).json()
    assert won["metadata"] == {"probability": "1.0", "isClosed": "true"}
    assert (await client.get("/crm/v3/pipelines/tickets/0/stages/4")).json()["metadata"] == {
        "ticketState": "CLOSED"
    }
    assert (await client.get("/crm/v3/pipelines/deals/missing")).status_code == 404
    assert (await client.get("/crm/v3/pipelines/deals/default/stages/missing")).status_code == 404
    first = (await client.get("/crm/v3/owners", params={"limit": 2})).json()
    assert len(first["results"]) == 2 and "paging" in first
    second = (await client.get(first["paging"]["next"]["link"])).json()
    assert not {o["id"] for o in first["results"]} & {o["id"] for o in second["results"]}
    owner = (
        await client.get("/crm/v3/owners/", params={"email": "maya.chen@halcyonadvisory.example"})
    ).json()["results"][0]
    assert owner["teams"][0]["name"] == "Advisory"
    assert not any(k.startswith("_") for k in owner)
    assert (await client.get("/crm/v3/owners/" + owner["id"])).json() == owner
    assert (
        await client.get("/crm/v3/owners/" + str(owner["userId"]), params={"idProperty": "userId"})
    ).json() == owner
    account = (await client.get("/account-info/v3/details")).json()
    assert account["portalId"] == 24681357 and account["companyCurrency"] == "USD"
    assert account["utcOffset"] in ("-04:00", "-05:00") and account["accountType"] == "STANDARD"
    usage = (await client.get("/account-info/v3/api-usage/daily/private-apps")).json()["results"][0]
    assert usage["currentUsage"] > 0 and usage["usageLimit"] == 1000000 and usage["fetchStatus"] == "SUCCESS"


async def test_custom_pipeline_stages_are_distinct_and_validated(client: httpx.AsyncClient) -> None:
    pipelines = (await client.get("/crm/v3/pipelines/deals")).json()["results"]
    default, custom = pipelines
    assert custom["id"].isdigit()
    assert not {s["id"] for s in default["stages"]} & {s["id"] for s in custom["stages"]}
    good = await client.post(
        "/crm/v3/objects/deals",
        json={
            "properties": {
                "dealname": "Advisory engagement",
                "pipeline": custom["id"],
                "dealstage": custom["stages"][0]["id"],
            }
        },
    )
    assert good.status_code == 201
    bad = await client.post(
        "/crm/v3/objects/deals",
        json={
            "properties": {
                "dealname": "Incorrect stage",
                "pipeline": custom["id"],
                "dealstage": default["stages"][0]["id"],
            }
        },
    )
    assert bad.status_code == 400


async def test_default_labeled_inline_reverse_batch_and_remove_associations(
    client: httpx.AsyncClient,
) -> None:
    contact = (
        await client.post("/crm/v3/objects/contacts", json={"properties": {"firstname": "Association"}})
    ).json()
    company = (await client.get("/crm/v3/objects/companies")).json()["results"][0]
    sid, did = contact["id"], company["id"]
    base = f"/crm/v4/objects/contacts/{sid}/associations"
    default = await client.put(base + "/default/companies/" + did)
    assert default.status_code == 200, default.text
    assert default.json()["status"] == "COMPLETE"
    spec = default.json()["results"][0]
    assert spec == {
        "from": {"id": sid},
        "to": {"id": did},
        "associationSpec": {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 279},
    }
    labeled = await client.put(
        base + "/companies/" + did, json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}]
    )
    assert labeled.status_code == 201 and labeled.json()["labels"] == ["Primary"]
    links = (await client.get(base + "/companies")).json()["results"]
    assert links[0]["toObjectId"] == did
    assert {t["typeId"] for t in links[0]["associationTypes"]} == {1, 279}
    inverse = (await client.get(f"/crm/v4/objects/companies/{did}/associations/contacts")).json()["results"]
    match = next(r for r in inverse if r["toObjectId"] == sid)
    assert {t["typeId"] for t in match["associationTypes"]} == {2, 280}
    expanded = (
        await client.get(f"/crm/v3/objects/companies/{did}", params={"associations": "contacts"})
    ).json()["associations"]["contacts"]["results"]
    assert {r["type"] for r in expanded if r["id"] == sid} == {
        "company_to_contact",
        "company_to_contact_unlabeled",
    }
    batch = await client.post(
        "/crm/v4/associations/contacts/companies/batch/read", json={"inputs": [{"id": sid}]}
    )
    assert batch.status_code == 200 and batch.json()["results"][0]["to"] == links
    partial = await client.post(
        "/crm/v4/associations/contacts/companies/batch/read", json={"inputs": [{"id": sid}, {"id": "bad"}]}
    )
    assert partial.status_code == 207 and partial.json()["numErrors"] == 1
    inline = await client.post(
        "/crm/v3/objects/contacts",
        json={
            "properties": {"firstname": "Inline"},
            "associations": [
                {
                    "to": {"id": did},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}],
                }
            ],
        },
    )
    assert inline.status_code == 201
    inline_links = (
        await client.get(f"/crm/v4/objects/contacts/{inline.json()['id']}/associations/companies")
    ).json()["results"]
    assert inline_links[0]["toObjectId"] == did
    assert (await client.delete(base + "/companies/" + did)).status_code == 204
    assert (await client.get(base + "/companies")).json()["results"] == []
    bad_direction = await client.put(
        base + "/companies/" + did, json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 2}]
    )
    assert bad_direction.status_code == 400


async def test_custom_association_labels_and_pagination(client: httpx.AsyncClient) -> None:
    labels = await client.post(
        "/crm/v4/associations/contacts/companies/labels",
        json={"name": "decision_maker", "label": "Decision maker", "inverseLabel": "Decision maker at"},
    )
    assert labels.status_code == 201
    forward, reverse = labels.json()["results"]
    contact = (await client.get("/crm/v3/objects/contacts")).json()["results"][0]
    companies = (await client.get("/crm/v3/objects/companies")).json()["results"][:2]
    for company in companies:
        linked = await client.put(
            f"/crm/v4/objects/contacts/{contact['id']}/associations/companies/{company['id']}",
            json=[{"associationCategory": "USER_DEFINED", "associationTypeId": forward["typeId"]}],
        )
        assert linked.status_code == 201 and linked.json()["labels"] == ["Decision maker"]
    found = (await client.get("/crm/v4/associations/companies/contacts/labels")).json()["results"]
    assert reverse in found
    first = (
        await client.get(
            f"/crm/v4/objects/contacts/{contact['id']}/associations/companies", params={"limit": 1}
        )
    ).json()
    next_page = (await client.get(first["paging"]["next"]["link"])).json()
    assert first["results"][0]["toObjectId"] != next_page["results"][0]["toObjectId"]


async def test_associations_respect_user_visibility(client: httpx.AsyncClient) -> None:
    rep = (
        await client.get("/crm/v3/owners", params={"email": "zoe.anderson@halcyonadvisory.example"})
    ).json()["results"][0]
    contact = (
        await client.post(
            "/crm/v3/objects/contacts",
            json={"properties": {"firstname": "Rep client", "hubspot_owner_id": rep["id"]}},
        )
    ).json()
    hidden = (await client.get("/crm/v3/objects/deals")).json()["results"][0]
    await client.put(f"/crm/v4/objects/contacts/{contact['id']}/associations/default/deals/{hidden['id']}")
    token = await mint(client, "https://api.hubapi.com", user="p042")
    auth = bearer(token)
    assert (
        await client.get(f"/crm/v4/objects/contacts/{contact['id']}/associations/deals", headers=auth)
    ).json()["results"] == []
    expanded = (
        await client.get(
            f"/crm/v3/objects/contacts/{contact['id']}", params={"associations": "deals"}, headers=auth
        )
    ).json()
    assert "deals" not in expanded.get("associations", {})
    denied = await client.put(
        f"/crm/v4/objects/contacts/{contact['id']}/associations/default/deals/{hidden['id']}", headers=auth
    )
    assert denied.status_code == 404


async def test_primary_company_replacement_preserves_unlabeled_relationship(
    client: httpx.AsyncClient,
) -> None:
    contact = (
        await client.post("/crm/v3/objects/contacts", json={"properties": {"firstname": "Primary"}})
    ).json()
    companies = (await client.get("/crm/v3/objects/companies")).json()["results"][:2]
    for company in companies:
        response = await client.put(
            f"/crm/v4/objects/contacts/{contact['id']}/associations/companies/{company['id']}",
            json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}],
        )
        assert response.status_code == 201
    links = (await client.get(f"/crm/v4/objects/contacts/{contact['id']}/associations/companies")).json()[
        "results"
    ]
    assert len(links) == 2
    primary = [r["toObjectId"] for r in links if any(t["typeId"] == 1 for t in r["associationTypes"])]
    assert primary == [companies[-1]["id"]]
    assert all(any(t["typeId"] == 279 for t in r["associationTypes"]) for r in links)
