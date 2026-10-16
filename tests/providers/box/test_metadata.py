"""Metadata templates and instances on files and folders."""

from __future__ import annotations

import re

SCOPE = "enterprise_336533359"
DELIVERABLES = "Client Engagements/Northwind Logistics/FY2025 Review/Deliverables"


async def first_file(box, folder_path=DELIVERABLES, who="p035"):
    folder = await box.find(folder_path, who)
    return next(e for e in await box.items(folder, who) if e["type"] == "file")


async def test_the_seeded_client_info_template(box):
    listing = (await box.get("/metadata_templates/enterprise")).json()
    assert listing["limit"] == 100 and listing["next_marker"] is None
    template = next(t for t in listing["entries"] if t["templateKey"] == "clientInfo")
    assert template["type"] == "metadata_template" and template["scope"] == SCOPE
    assert template["displayName"] == "Client Info" and template["hidden"] is False
    by_key = {f["key"]: f for f in template["fields"]}
    assert set(by_key) == {"clientName", "engagementType", "fiscalYear"}
    assert by_key["clientName"]["type"] == "string"
    assert by_key["fiscalYear"]["type"] == "float"
    assert by_key["engagementType"]["type"] == "enum"
    options = {o["key"] for o in by_key["engagementType"]["options"]}
    assert {"Audit", "Tax Compliance", "Review Engagement", "Other"} <= options
    assert re.fullmatch(r"[0-9a-f-]{36}", template["id"])
    same = (await box.get("/metadata_templates/enterprise/clientInfo/schema")).json()
    assert same == template
    assert (await box.get(f"/metadata_templates/{SCOPE}/clientInfo/schema")).json() == template
    assert (await box.get(f"/metadata_templates/{template['id']}")).json() == template
    assert (await box.get("/metadata_templates/enterprise/nope/schema")).status_code == 404
    global_templates = (await box.get("/metadata_templates/global")).json()
    assert any(t["templateKey"] == "properties" for t in global_templates["entries"])


async def test_client_files_carry_client_info_instances(box):
    file = await first_file(box)
    instance = (await box.get(f"/files/{file['id']}/metadata/enterprise/clientInfo")).json()
    assert instance["$parent"] == f"file_{file['id']}"
    assert instance["$template"] == "clientInfo" and instance["$scope"] == SCOPE
    assert instance["$version"] == 0 and instance["$canEdit"] is True
    assert instance["$type"].startswith("clientInfo-") and "$id" in instance
    assert instance["clientName"] == "Northwind Logistics"
    assert instance["engagementType"] == "Review Engagement"
    assert instance["fiscalYear"] == 2025
    listing = (await box.get(f"/files/{file['id']}/metadata")).json()
    assert [e["$template"] for e in listing["entries"]] == ["clientInfo"] and listing["limit"] == 100
    assert (await box.get(f"/files/{file['id']}/metadata/{SCOPE}/clientInfo")).json() == instance
    internal = await first_file(box, "Finance & Operations/Budgets")
    assert (await box.get(f"/files/{internal['id']}/metadata/enterprise/clientInfo")).status_code == 404
    missing = await box.get(f"/files/{internal['id']}/metadata/enterprise/clientInfo")
    assert missing.json()["code"] == "instance_not_found"
    # Metadata follows file permissions.
    assert (await box.get(f"/files/{file['id']}/metadata/enterprise/clientInfo", "p044")).status_code == 404


async def test_create_update_and_delete_instances(box):
    parent = await box.find("Technology")
    fid = (await box.upload(parent, "tracked.txt", b"x")).json()["entries"][0]["id"]
    base = f"/files/{fid}/metadata/enterprise/clientInfo"
    created = await box.post(base, json={"clientName": "Acme", "engagementType": "Audit", "fiscalYear": 2026})
    assert created.status_code == 201, created.text
    assert created.json()["clientName"] == "Acme" and created.json()["$version"] == 0
    assert (await box.post(base, json={"clientName": "Again"})).status_code == 409
    patched = await box.put(
        base,
        headers={"Content-Type": "application/json-patch+json"},
        content='[{"op": "replace", "path": "/clientName", "value": "Acme Corp"},'
        ' {"op": "add", "path": "/fiscalYear", "value": 2027}, {"op": "remove", "path": "/engagementType"}]',
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["clientName"] == "Acme Corp" and body["fiscalYear"] == 2027 and "engagementType" not in body
    assert body["$version"] == 1
    tested = await box.put(
        base,
        headers={"Content-Type": "application/json-patch+json"},
        content='[{"op": "test", "path": "/clientName", "value": "wrong"}]',
    )
    assert tested.status_code == 400
    for bad_patch in (
        '[{"op": "replace", "path": "/engagementType", "value": "Audit"}]',  # field is not set
        '[{"op": "add", "path": "/fiscalYear", "value": "soon"}]',  # wrong type
        '[{"op": "add", "path": "/engagementType", "value": "Not an option"}]',
        '[{"op": "add", "path": "/unknown", "value": 1}]',
        '[{"op": "wiggle", "path": "/clientName"}]',
        "[]",
    ):
        r = await box.put(base, headers={"Content-Type": "application/json-patch+json"}, content=bad_patch)
        assert r.status_code == 400, bad_patch
    assert (await box.delete(base)).status_code == 204
    assert (await box.get(base)).status_code == 404
    assert (await box.delete(base)).status_code == 404
    assert (await box.post(base, json={"clientName": 5})).status_code == 400
    assert (await box.post(base, json={"mystery": "x"})).status_code == 400
    assert (await box.post(f"/files/{fid}/metadata/enterprise/nope", json={})).status_code == 404


async def test_global_properties_accept_free_form_keys(box):
    parent = await box.find("Technology")
    fid = (await box.upload(parent, "props.txt", b"x")).json()["entries"][0]["id"]
    created = await box.post(
        f"/files/{fid}/metadata/global/properties", json={"owner": "ops", "ticket": 4821}
    )
    assert created.status_code == 201
    assert created.json()["owner"] == "ops" and created.json()["ticket"] == "4821"
    assert created.json()["$scope"] == "global" and created.json()["$template"] == "properties"
    got = (await box.get(f"/files/{fid}/metadata/global/properties")).json()
    assert got["owner"] == "ops"


async def test_folder_metadata_and_viewers_cannot_edit(box):
    folder = await box.find("Technology")
    created = await box.post(
        f"/folders/{folder}/metadata/enterprise/clientInfo", json={"clientName": "Internal"}
    )
    assert created.status_code == 201 and created.json()["$parent"] == f"folder_{folder}"
    assert (await box.get(f"/folders/{folder}/metadata")).json()["entries"][0]["clientName"] == "Internal"
    as_viewer = await box.post(f"/folders/{folder}/metadata/global/properties", "p004", json={"a": "b"})
    assert as_viewer.status_code == 403  # leadership may read IT, not write
    assert (await box.get(f"/folders/{folder}/metadata/enterprise/clientInfo", "p004")).status_code == 200
    assert (await box.delete(f"/folders/{folder}/metadata/enterprise/clientInfo")).status_code == 204
    fields = await box.get(f"/folders/{folder}", params={"fields": "metadata.enterprise.clientInfo"})
    assert fields.json()["metadata"] == {}


async def test_template_administration(box):
    created = await box.post(
        "/metadata_templates/schema",
        json={
            "scope": "enterprise",
            "displayName": "Matter Details",
            "copyInstanceOnItemCopy": True,
            "fields": [
                {"type": "string", "key": "matterId", "displayName": "Matter Id"},
                {
                    "type": "multiSelect",
                    "key": "regions",
                    "displayName": "Regions",
                    "options": [{"key": "EMEA"}, {"key": "APAC"}],
                },
                {"type": "date", "key": "opened", "displayName": "Opened"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    template = created.json()
    assert template["templateKey"] == "matterDetails" and template["scope"] == SCOPE
    assert template["copyInstanceOnItemCopy"] is True
    assert (
        await box.post("/metadata_templates/schema", json={"displayName": "Matter Details"})
    ).status_code == 409
    assert (
        await box.post("/metadata_templates/schema", "p018", json={"displayName": "Nope"})
    ).status_code == 403
    assert (
        await box.post(
            "/metadata_templates/schema",
            json={"displayName": "Bad", "fields": [{"type": "money", "key": "x"}]},
        )
    ).status_code == 400
    parent = await box.find("Technology")
    fid = (await box.upload(parent, "m.txt", b"x")).json()["entries"][0]["id"]
    base = f"/files/{fid}/metadata/enterprise/matterDetails"
    ok = await box.post(
        base, json={"matterId": "M-1", "regions": ["EMEA"], "opened": "2026-01-01T00:00:00.000Z"}
    )
    assert ok.status_code == 201
    assert (
        await box.post(f"/files/{fid}/metadata/enterprise/matterDetails", json={"regions": ["MARS"]})
    ).status_code == 400  # not an option of the multiSelect field
    # Copies keep the instance because the template says so.
    copy = (
        await box.post(f"/files/{fid}/copy", json={"parent": {"id": parent}, "name": "m copy.txt"})
    ).json()
    assert (await box.get(f"/files/{copy['id']}/metadata/enterprise/matterDetails")).status_code == 200
    edit = await box.put(
        "/metadata_templates/enterprise/matterDetails/schema",
        headers={"Content-Type": "application/json-patch+json"},
        content='[{"op": "addField", "data": {"type": "string", "key": "note", "displayName": "Note"}},'
        ' {"op": "editField", "fieldKey": "matterId", "data": {"displayName": "Matter number"}},'
        ' {"op": "addEnumOption", "fieldKey": "regions", "data": {"key": "AMER"}},'
        ' {"op": "editTemplate", "data": {"displayName": "Matter details"}}]',
    )
    assert edit.status_code == 200, edit.text
    updated = edit.json()
    assert updated["displayName"] == "Matter details"
    assert {f["key"] for f in updated["fields"]} == {"matterId", "regions", "opened", "note"}
    assert next(f for f in updated["fields"] if f["key"] == "matterId")["displayName"] == "Matter number"
    removal = await box.put(
        "/metadata_templates/enterprise/matterDetails/schema",
        headers={"Content-Type": "application/json-patch+json"},
        content='[{"op": "removeField", "fieldKey": "opened"}]',
    )
    assert "opened" not in {f["key"] for f in removal.json()["fields"]}
    assert "opened" not in (await box.get(base)).json()
    assert (
        await box.delete("/metadata_templates/enterprise/matterDetails/schema", "p018")
    ).status_code == 403
    assert (await box.delete("/metadata_templates/enterprise/matterDetails/schema")).status_code == 204
    assert (await box.get(base)).status_code == 404
    assert (await box.get("/metadata_templates/enterprise/matterDetails/schema")).status_code == 404


async def test_metadata_template_lookup_by_instance(box):
    file = await first_file(box)
    instance = (await box.get(f"/files/{file['id']}/metadata/enterprise/clientInfo")).json()
    found = (await box.get("/metadata_templates", params={"metadata_instance_id": instance["$id"]})).json()
    assert [t["templateKey"] for t in found["entries"]] == ["clientInfo"]
    assert (await box.get("/metadata_templates")).status_code == 400
