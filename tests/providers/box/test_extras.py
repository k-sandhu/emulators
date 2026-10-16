"""Content transfer, watermarks, folder locks, representations and classification."""

from __future__ import annotations

import io

DELIVERABLES = "Client Engagements/Northwind Logistics/FY2025 Review/Deliverables"


async def test_transfer_a_departed_users_content_to_a_manager(box):
    owner = (await box.get("/users/me", "p018")).json()
    notes = await box.find("Notes", "p018")
    await box.upload(notes, "handover.txt", b"state of play", "p018")
    manager = (await box.get("/users/me", "p019")).json()
    assert (
        await box.put(f"/users/{owner['id']}/folders/0", "p019", json={"owned_by": {"id": manager["id"]}})
    ).status_code == 403
    assert (
        await box.put(f"/users/{owner['id']}/folders/0", json={"owned_by": {"id": owner["id"]}})
    ).status_code == 400
    r = await box.put(f"/users/{owner['id']}/folders/0", json={"owned_by": {"id": manager["id"]}})
    assert r.status_code == 200, r.text
    folder = r.json()
    assert folder["name"] == "Maya Chen's Files and Folders" and folder["owned_by"]["id"] == manager["id"]
    moved = {e["name"] for e in await box.items(folder["id"], "p019")}
    assert "Notes" in moved
    assert await box.items("0", "p018") == [
        e for e in await box.items("0", "p018") if e["id"] != notes
    ]  # nothing left
    notes_items = [e["name"] for e in await box.items(notes, "p019")]
    assert "handover.txt" in notes_items
    info = (
        await box.get(
            f"/files/{(await box.items(notes, 'p019'))[0]['id']}", "p019", params={"fields": "owned_by"}
        )
    ).json()
    assert info["owned_by"]["id"] == manager["id"]


async def test_watermarks_on_files_and_folders(box):
    folder = await box.find("Technology")
    child = await box.mkdir(folder, "Marked")
    fid = (await box.upload(child, "w.txt", b"x")).json()["entries"][0]["id"]
    assert (await box.get(f"/files/{fid}/watermark")).status_code == 404
    assert (await box.get(f"/files/{fid}", params={"fields": "watermark_info"})).json()["watermark_info"][
        "is_watermarked"
    ] is False
    assert (
        await box.put(f"/folders/{child}/watermark", json={"watermark": {"imprint": "other"}})
    ).status_code == 400
    applied = await box.put(f"/folders/{child}/watermark", json={"watermark": {"imprint": "default"}})
    assert applied.status_code == 200 and set(applied.json()["watermark"]) == {"created_at", "modified_at"}
    assert (await box.get(f"/folders/{child}/watermark")).status_code == 200
    inherited = (await box.get(f"/files/{fid}", params={"fields": "watermark_info"})).json()["watermark_info"]
    assert inherited == {"is_watermarked": True, "is_watermark_inherited": True}
    assert (
        await box.put(f"/files/{fid}/watermark", "p004", json={"watermark": {"imprint": "default"}})
    ).status_code == 403
    assert (await box.delete(f"/folders/{child}/watermark")).status_code == 204
    assert (await box.delete(f"/folders/{child}/watermark")).status_code == 404
    assert (await box.get(f"/files/{fid}", params={"fields": "watermark_info"})).json()["watermark_info"][
        "is_watermarked"
    ] is False


async def test_folder_locks_freeze_move_and_delete(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Frozen")
    target = await box.mkdir(parent, "Elsewhere")
    created = await box.post(
        "/folder_locks",
        json={
            "folder": {"id": folder, "type": "folder"},
            "locked_operations": {"move": True, "delete": True},
        },
    )
    assert created.status_code == 201, created.text
    lock = created.json()
    assert lock["type"] == "folder_lock" and lock["folder"]["id"] == folder and lock["lock_type"] == "freeze"
    assert lock["locked_operations"] == {"move": True, "delete": True}
    assert (
        await box.post("/folder_locks", json={"folder": {"id": folder, "type": "folder"}})
    ).status_code == 409
    listed = (await box.get("/folder_locks", params={"folder_id": folder})).json()
    assert [e["id"] for e in listed["entries"]] == [lock["id"]]
    moved = await box.put(f"/folders/{folder}", json={"parent": {"id": target}})
    assert moved.status_code == 403 and moved.json()["code"] == "access_denied_item_locked"
    assert (await box.delete(f"/folders/{folder}")).status_code == 403
    assert (await box.put(f"/folders/{folder}", json={"name": "Still renamable"})).status_code == 200
    assert (await box.delete(f"/folder_locks/{lock['id']}", "p004")).status_code == 403
    assert (await box.delete(f"/folder_locks/{lock['id']}")).status_code == 204
    assert (await box.delete(f"/folders/{folder}")).status_code == 204
    assert (
        await box.post("/folder_locks", json={"folder": {"id": "0", "type": "folder"}})
    ).status_code == 403
    assert (await box.get("/folder_locks")).status_code == 400


async def test_representations_via_rep_hints(box):
    folder = await box.find(DELIVERABLES)
    entry = next(e for e in await box.items(folder) if e["name"] == "Management Letter.docx")
    plain = (await box.get(f"/files/{entry['id']}", params={"fields": "representations"})).json()[
        "representations"
    ]
    assert plain["entries"] == [] and plain["info"]["url"].startswith(
        "https://api.box.com/2.0/internal_files/"
    )
    hinted = await box.get(
        f"/files/{entry['id']}",
        params={"fields": "representations"},
        headers={"X-Rep-Hints": "[pdf][extracted_text][png?dimensions=256x256]"},
    )
    reps = {r["representation"]: r for r in hinted.json()["representations"]["entries"]}
    assert set(reps) == {"pdf", "extracted_text", "png"}
    assert reps["png"]["properties"]["dimensions"] == "256x256"
    assert reps["pdf"]["status"] == {"state": "success"}
    info = await box.client.get(reps["pdf"]["info"]["url"], headers=box.auth())
    assert info.status_code == 200 and info.json()["representation"] == "pdf"
    template = reps["pdf"]["content"]["url_template"]
    assert template.startswith("https://public.boxcloud.com/api/2.0/internal_files/")
    pdf = await box.client.get(template.replace("{+asset_path}", "1.pdf"), headers=box.auth())
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    import pypdf

    assert len(pypdf.PdfReader(io.BytesIO(pdf.content)).pages) >= 1
    text = await box.client.get(
        reps["extracted_text"]["content"]["url_template"].replace("{+asset_path}", "text"), headers=box.auth()
    )
    assert text.status_code == 200 and "Management Letter" in text.text
    image = await box.client.get(
        reps["png"]["content"]["url_template"].replace("{+asset_path}", "1.png"), headers=box.auth()
    )
    assert image.content.startswith(b"\x89PNG")
    assert (
        await box.client.get(
            reps["pdf"]["content"]["url_template"].replace("{+asset_path}", "1.pdf"), headers=box.auth("p044")
        )
    ).status_code == 404
    assert (
        await box.client.get(reps["pdf"]["info"]["url"].replace("/pdf", "/hologram"), headers=box.auth())
    ).status_code == 404


async def test_security_classification(box):
    template = (
        await box.get("/metadata_templates/enterprise/securityClassification-6VMVochwUWo/schema")
    ).json()
    field = template["fields"][0]
    assert field["key"] == "Box__Security__Classification__Key"
    assert [o["key"] for o in field["options"]] == ["Public", "Internal", "Confidential"]
    assert field["options"][2]["staticConfig"]["classification"]["colorID"] == 5
    restricted = (
        await box.get("/search", "p031", params={"query": "salary bands", "fields": "classification"})
    ).json()["entries"][0]
    assert restricted["classification"]["name"] == "Confidential"
    assert (
        restricted["classification"]["color"].startswith("#") and restricted["classification"]["definition"]
    )
    plain = await box.upload(await box.find("Technology"), "unclassified.txt", b"x")
    assert (
        await box.get(f"/files/{plain.json()['entries'][0]['id']}", params={"fields": "classification"})
    ).json()["classification"] is None
    fid = plain.json()["entries"][0]["id"]
    set_label = await box.post(
        f"/files/{fid}/metadata/enterprise/securityClassification-6VMVochwUWo",
        json={"Box__Security__Classification__Key": "Internal"},
    )
    assert set_label.status_code == 201
    assert (await box.get(f"/files/{fid}", params={"fields": "classification"})).json()["classification"][
        "name"
    ] == "Internal"
    copy = (
        await box.post(
            f"/files/{fid}/copy",
            json={"parent": {"id": await box.find("Technology")}, "name": "classified copy.txt"},
        )
    ).json()
    assert (await box.get(f"/files/{copy['id']}", params={"fields": "classification"})).json()[
        "classification"
    ]["name"] == "Internal"
