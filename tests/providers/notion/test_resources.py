from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from emulators.providers.notion.common import NEW, person_id
from emulators.providers.notion.seed import sid

from .conftest import delegated
from .test_workspace import CLIENTS, KNOWLEDGE, PEOPLE, create_page, text


@pytest.mark.parametrize(
    "kind,data",
    [
        ("paragraph", {"rich_text": [text("Paragraph")]}),
        ("heading_1", {"rich_text": [text("Heading")]}),
        ("heading_2", {"rich_text": [text("Heading")], "is_toggleable": True}),
        ("heading_3", {"rich_text": [text("Heading")]}),
        ("bulleted_list_item", {"rich_text": [text("Bullet")]}),
        ("numbered_list_item", {"rich_text": [text("Numbered")]}),
        ("to_do", {"rich_text": [text("Task")], "checked": True}),
        (
            "toggle",
            {"rich_text": [text("Toggle")], "children": [{"paragraph": {"rich_text": [text("Nested")]}}]},
        ),
        ("quote", {"rich_text": [text("Quote")]}),
        ("callout", {"rich_text": [text("Callout")], "icon": {"type": "emoji", "emoji": "💡"}}),
        ("code", {"rich_text": [text("x = 1")], "language": "python"}),
        ("divider", {}),
        (
            "table",
            {
                "table_width": 2,
                "has_column_header": True,
                "has_row_header": False,
                "children": [{"table_row": {"cells": [[text("A")], [text("B")]]}}],
            },
        ),
        (
            "image",
            {"type": "external", "external": {"url": "https://assets.halcyonadvisory.example/image.png"}},
        ),
        (
            "file",
            {
                "type": "external",
                "external": {"url": "https://assets.halcyonadvisory.example/file.txt"},
                "name": "file.txt",
            },
        ),
        ("pdf", {"type": "external", "external": {"url": "https://assets.halcyonadvisory.example/file.pdf"}}),
        ("bookmark", {"url": "https://halcyonadvisory.example"}),
        ("synced_block", {"synced_from": None, "children": [{"paragraph": {"rich_text": [text("Shared")]}}]}),
        (
            "column_list",
            {
                "children": [
                    {"column": {"children": [{"paragraph": {"rich_text": [text("One")]}}]}},
                    {"column": {"children": [{"paragraph": {"rich_text": [text("Two")]}}]}},
                ]
            },
        ),
        ("link_to_page", {"type": "page_id", "page_id": KNOWLEDGE}),
        ("equation", {"expression": "x^2"}),
        ("table_of_contents", {}),
    ],
)
async def test_block_types_crud(client: httpx.AsyncClient, kind: str, data: dict[str, Any]) -> None:
    page = await create_page(client)
    route = "/v1/blocks/" + page["id"] + "/children"
    response = await client.patch(route, json={"children": [{"object": "block", "type": kind, kind: data}]})
    assert response.status_code == 200, response.text
    block = response.json()["results"][0]
    assert block["type"] == kind and block["object"] == "block"
    fetched = (await client.get("/v1/blocks/" + block["id"])).json()
    assert fetched == block
    assert block["parent"] == {"type": "page_id", "page_id": page["id"]}
    if data.get("children"):
        assert block["has_children"] and "children" not in block[kind]
        nested = (await client.get(f"/v1/blocks/{block['id']}/children")).json()
        assert len(nested["results"]) == len(data["children"])
        assert nested["results"][0]["parent"] == {"type": "block_id", "block_id": block["id"]}
    updated = await client.patch(
        "/v1/blocks/" + block["id"], json={kind: {k: v for k, v in data.items() if k != "children"}}
    )
    assert updated.status_code == 200, updated.text
    deleted = (await client.delete("/v1/blocks/" + block["id"])).json()
    assert deleted["archived"] and deleted["in_trash"]
    assert not (await client.get(route)).json()["results"]
    restored = await client.patch("/v1/blocks/" + block["id"], json={"in_trash": False})
    assert restored.status_code == 200 and not restored.json()["archived"]


async def test_block_append_after_pagination_and_atomic_validation(client: httpx.AsyncClient) -> None:
    page = await create_page(
        client, children=[{"paragraph": {"rich_text": [text(str(i))]}} for i in range(3)]
    )
    route = "/v1/blocks/" + page["id"] + "/children"
    first = (await client.get(route, params={"page_size": 2})).json()
    last = (await client.get(route, params={"start_cursor": first["next_cursor"]})).json()
    assert len(first["results"]) == 2 and len(last["results"]) == 1
    response = await client.patch(
        route,
        json={
            "children": [{"paragraph": {"rich_text": [text("inserted")]}}],
            "after": first["results"][0]["id"],
        },
    )
    assert response.status_code == 200
    rows = (await client.get(route)).json()["results"]
    assert [r["paragraph"]["rich_text"][0]["plain_text"] for r in rows] == ["0", "inserted", "1", "2"]
    bad = await client.patch(route, json={"children": [{"divider": {}}, {"unknown": {}}]})
    assert bad.status_code == 400
    assert len((await client.get(route)).json()["results"]) == 4
    assert (
        await client.patch(route, json={"children": [{"divider": {}}], "after": KNOWLEDGE})
    ).status_code == 400
    assert (
        await client.patch(
            route,
            json={
                "children": [
                    {"table": {"table_width": 2, "children": [{"table_row": {"cells": [[text("one")]]}}]}}
                ]
            },
        )
    ).status_code == 400


async def test_synced_blocks_and_rich_mentions(client: httpx.AsyncClient) -> None:
    page = await create_page(client)
    route = f"/v1/blocks/{page['id']}/children"
    original = (
        await client.patch(
            route,
            json={
                "children": [
                    {
                        "synced_block": {
                            "synced_from": None,
                            "children": [{"paragraph": {"rich_text": [text("source")]}}],
                        }
                    }
                ]
            },
        )
    ).json()["results"][0]
    duplicate = (
        await client.patch(
            route, json={"children": [{"synced_block": {"synced_from": {"block_id": original["id"]}}}]}
        )
    ).json()["results"][0]
    assert duplicate["has_children"]
    mirrored = await client.get(f"/v1/blocks/{duplicate['id']}/children")
    assert mirrored.json()["results"][0]["paragraph"]["rich_text"][0]["plain_text"] == "source"
    mentions = [
        {"type": "mention", "mention": {"type": "user", "user": {"id": person_id("p018")}}},
        {"type": "mention", "mention": {"type": "page", "page": {"id": KNOWLEDGE}}},
        {"type": "mention", "mention": {"type": "date", "date": {"start": "2026-10-16"}}},
    ]
    response = await client.patch(route, json={"children": [{"paragraph": {"rich_text": mentions}}]})
    assert response.status_code == 200, response.text
    rich = response.json()["results"][0]["paragraph"]["rich_text"]
    assert rich[0]["plain_text"] == "Maya Chen" and rich[0]["mention"]["user"]["object"] == "user"
    assert rich[1]["plain_text"] == "Knowledge Base" and rich[2]["plain_text"] == "2026-10-16"


async def test_child_page_and_database_blocks_and_visibility(client: httpx.AsyncClient) -> None:
    child = await create_page(client)
    blocks = (await client.get(f"/v1/blocks/{KNOWLEDGE}/children")).json()["results"]
    assert any(b["id"] == child["id"] and b["type"] == "child_page" for b in blocks)
    databases = (await client.get(f"/v1/blocks/{sid('space', 'clients')}/children")).json()["results"]
    assert any(b["type"] == "child_database" and b["child_database"]["title"] == "Clients" for b in databases)
    assert (await client.get(f"/v1/blocks/{PEOPLE}/children")).status_code == 404
    reader = await delegated(client, "p008")
    assert (
        await client.patch(
            f"/v1/blocks/{KNOWLEDGE}/children", headers=reader, json={"children": [{"divider": {}}]}
        )
    ).status_code == 403


async def test_database_schema_create_update_and_page_properties(client: httpx.AsyncClient) -> None:
    payload = {
        "parent": {"page_id": KNOWLEDGE},
        "title": [text("Tasks")],
        "properties": {
            "Name": {"title": {}},
            "Count": {"number": {}},
            "Tags": {"multi_select": {}},
            "Done": {"checkbox": {}},
            "Summary": {"rich_text": {}},
            "People": {"people": {}},
            "Due": {"date": {}},
            "State": {"select": {}},
        },
    }
    result = await client.post("/v1/databases", json=payload)
    assert result.status_code == 200, result.text
    db = result.json()
    response = await client.post(
        "/v1/pages",
        json={
            "parent": {"database_id": db["id"]},
            "properties": {
                "Name": {"title": [text("Task A")]},
                "Count": {"number": 12},
                "Tags": {"multi_select": [{"name": "QA"}]},
                "Done": {"checkbox": True},
                "Summary": {"rich_text": [text("details")]},
                "People": {"people": [{"id": person_id("p018")}]},
                "Due": {"date": {"start": "2026-10-16"}},
                "State": {"select": {"name": "Ready"}},
            },
        },
    )
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["properties"]["Tags"]["multi_select"][0]["color"] == "default"
    count_id = page["properties"]["Count"]["id"]
    changed = await client.patch("/v1/pages/" + page["id"], json={"properties": {count_id: {"number": 15}}})
    assert changed.json()["properties"]["Count"]["number"] == 15
    schema_response = await client.patch(
        "/v1/databases/" + db["id"],
        json={
            "title": [text("Renamed")],
            "properties": {count_id: {"name": "Total"}, "Summary": None, "Extra": {"url": {}}},
        },
    )
    assert schema_response.status_code == 200, schema_response.text
    fetched = (await client.get("/v1/pages/" + page["id"])).json()["properties"]
    assert "Count" not in fetched and "Summary" not in fetched
    assert fetched["Total"]["number"] == 15 and fetched["Extra"]["url"] is None
    invalid = await client.patch("/v1/databases/" + db["id"], json={"properties": {"Name": None}})
    assert invalid.status_code == 400
    assert "Name" in (await client.get("/v1/databases/" + db["id"])).json()["properties"]


async def test_new_database_data_source_create_query_update(client: httpx.AsyncClient) -> None:
    headers = {"Notion-Version": NEW}
    response = await client.post(
        "/v1/databases",
        headers=headers,
        json={
            "parent": {"page_id": KNOWLEDGE},
            "title": [text("New Tasks")],
            "initial_data_source": {"properties": {"Name": {"title": {}}, "Priority": {"number": {}}}},
        },
    )
    assert response.status_code == 200, response.text
    db = response.json()
    source_id = db["data_sources"][0]["id"]
    page = await client.post(
        "/v1/pages",
        headers=headers,
        json={
            "parent": {"type": "data_source_id", "data_source_id": source_id},
            "properties": {"Name": {"title": [text("New row")]}, "Priority": {"number": 1}},
        },
    )
    assert page.status_code == 200, page.text
    query = await client.post(
        f"/v1/data_sources/{source_id}/query",
        headers=headers,
        json={"filter": {"property": "Priority", "number": {"equals": 1}}},
    )
    assert len(query.json()["results"]) == 1
    update = await client.patch(
        "/v1/data_sources/" + source_id,
        headers=headers,
        json={
            "title": [text("Updated Source")],
            "description": [text("Test")],
            "properties": {"Flag": {"checkbox": {}}},
        },
    )
    assert update.status_code == 200 and "Flag" in update.json()["properties"]
    second = await client.post(
        "/v1/data_sources",
        headers=headers,
        json={"parent": {"database_id": db["id"]}, "title": [text("Second")]},
    )
    assert second.status_code == 200, second.text
    assert len((await client.get("/v1/databases/" + db["id"], headers=headers)).json()["data_sources"]) == 2
    assert (await client.get("/v1/databases/" + db["id"])).status_code == 400
    assert (
        await client.patch(
            "/v1/databases/" + db["id"], headers=headers, json={"properties": {"No": {"number": {}}}}
        )
    ).status_code == 400
    assert (await client.get("/v1/data_sources/" + source_id)).status_code == 400


@pytest.mark.parametrize(
    "property_name,condition",
    [
        ("Name", {"title": {"contains": "Northwind"}}),
        ("Name", {"title": {"equals": "Northwind Logistics"}}),
        ("Name", {"title": {"starts_with": "Northwind"}}),
        ("Annual Revenue", {"number": {"greater_than": 0}}),
        ("Annual Revenue", {"number": {"less_than": 10**12}}),
        ("Status", {"status": {"equals": "Active"}}),
        ("Industry", {"select": {"equals": "Logistics & Transportation"}}),
        ("Client Since", {"date": {"before": "2027-01-01"}}),
        ("Client Since", {"date": {"after": "2000-01-01"}}),
        ("Client Since", {"date": {"on_or_after": "2000-01-01"}}),
        ("Owner", {"people": {"contains": person_id("p004")}}),
        ("Engagements", {"relation": {"contains": sid("engagement", "e001")}}),
    ],
)
async def test_database_filters(
    client: httpx.AsyncClient, property_name: str, condition: dict[str, Any]
) -> None:
    # Use the world's real industry rather than introducing an unrelated company.
    if property_name == "Industry":
        condition["select"]["equals"] = client.hub.world.clients[0].industry
    response = await client.post(
        f"/v1/databases/{CLIENTS}/query", json={"filter": {"property": property_name, **condition}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"]


async def test_query_compounds_sorts_properties_and_pagination(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"/v1/databases/{CLIENTS}/query",
        json={
            "filter": {
                "and": [
                    {"property": "Annual Revenue", "number": {"greater_than": 0}},
                    {
                        "or": [
                            {"property": "Status", "status": {"equals": "Active"}},
                            {"property": "Status", "status": {"equals": "Prospect"}},
                        ]
                    },
                ]
            },
            "sorts": [{"property": "Annual Revenue", "direction": "descending"}],
            "page_size": 3,
        },
    )
    assert response.status_code == 200, response.text
    first = response.json()
    revenues = [r["properties"]["Annual Revenue"]["number"] for r in first["results"]]
    assert revenues == sorted(revenues, reverse=True) and first["has_more"]
    all_rows = (await client.post(f"/v1/databases/{CLIENTS}/query", json={})).json()
    assert len(all_rows["results"]) == len(client.hub.world.clients) == 24
    filtered = (
        await client.post(
            f"/v1/databases/{CLIENTS}/query", params=[("filter_properties", "title")], json={"page_size": 1}
        )
    ).json()
    assert list(filtered["results"][0]["properties"]) == ["Name"]
    timestamp = await client.post(
        f"/v1/databases/{CLIENTS}/query",
        json={
            "filter": {"timestamp": "created_time", "created_time": {"on_or_after": "2026-10-16"}},
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
        },
    )
    assert timestamp.status_code == 200 and len(timestamp.json()["results"]) == 24
    bad_filters = [
        {"property": "missing", "number": {"equals": 2}},
        {"property": "Name", "title": {"nonsense": "x"}},
        {"and": [{"or": [{"and": [{"property": "Name", "title": {"contains": "x"}}]}]}]},
    ]
    for bad in bad_filters:
        assert (await client.post(f"/v1/databases/{CLIENTS}/query", json={"filter": bad})).status_code == 400


async def test_formula_checkbox_multiselect_and_relative_dates(client: httpx.AsyncClient) -> None:
    db = (
        await client.post(
            "/v1/databases",
            json={
                "parent": {"page_id": KNOWLEDGE},
                "properties": {
                    "Name": {"title": {}},
                    "Done": {"checkbox": {}},
                    "Tags": {"multi_select": {}},
                    "Date": {"date": {}},
                    "Count": {"number": {}},
                    "Double": {"formula": {"expression": 'prop("Count") * 2'}},
                },
            },
        )
    ).json()
    ctx = client.hub.context("notion")
    ctx.clock.freeze(client.hub.world.anchor)
    try:
        page = await client.post(
            "/v1/pages",
            json={
                "parent": {"database_id": db["id"]},
                "properties": {
                    "Name": {"title": [text("QA")]},
                    "Done": {"checkbox": True},
                    "Tags": {"multi_select": [{"name": "Test"}]},
                    "Date": {"date": {"start": "2026-10-15"}},
                    "Count": {"number": 4},
                },
            },
        )
        assert page.status_code == 200, page.text
        assert page.json()["properties"]["Double"]["formula"]["number"] == 8
        for condition in [
            {"property": "Done", "checkbox": {"equals": True}},
            {"property": "Tags", "multi_select": {"contains": "Test"}},
            {"property": "Double", "formula": {"number": {"greater_than": 7}}},
            {"property": "Date", "date": {"past_week": {}}},
            {"property": "Date", "date": {"this_week": {}}},
        ]:
            query = await client.post(f"/v1/databases/{db['id']}/query", json={"filter": condition})
            assert query.status_code == 200 and len(query.json()["results"]) == 1, query.text
        await client.patch(
            "/v1/pages/" + page.json()["id"], json={"properties": {"Date": {"date": {"start": "2026-11-01"}}}}
        )
        assert (
            await client.post(
                f"/v1/databases/{db['id']}/query",
                json={"filter": {"property": "Date", "date": {"next_month": {}}}},
            )
        ).json()["results"]
    finally:
        ctx.clock.unfreeze()


async def test_comments_threads_and_permissions(client: httpx.AsyncClient) -> None:
    page = await create_page(client)
    comment = await client.post(
        "/v1/comments", json={"parent": {"page_id": page["id"]}, "rich_text": [text("Review this")]}
    )
    assert comment.status_code == 200, comment.text
    first = comment.json()
    assert first["object"] == "comment" and first["parent"]["page_id"] == page["id"]
    reply = await client.post(
        "/v1/comments", json={"discussion_id": first["discussion_id"], "rich_text": [text("Reviewed")]}
    )
    assert reply.status_code == 200 and reply.json()["discussion_id"] == first["discussion_id"]
    results = (await client.get("/v1/comments", params={"block_id": page["id"], "page_size": 1})).json()
    assert results["type"] == "comment" and results["has_more"]
    more = (
        await client.get(
            "/v1/comments", params={"block_id": page["id"], "start_cursor": results["next_cursor"]}
        )
    ).json()
    assert more["results"][0]["rich_text"][0]["plain_text"] == "Reviewed"
    assert (await client.get("/v1/comments", params={"block_id": PEOPLE})).status_code == 404
    reader = await delegated(client, "p008")
    assert (
        await client.post(
            "/v1/comments",
            headers=reader,
            json={"parent": {"page_id": KNOWLEDGE}, "rich_text": [text("Denied")]},
        )
    ).status_code == 403


async def test_upload_single_part_attach_download_and_expiry(client: httpx.AsyncClient) -> None:
    headers = {"Notion-Version": NEW}
    upload = (
        await client.post(
            "/v1/file_uploads",
            headers=headers,
            json={"filename": "report.pdf", "content_type": "application/pdf"},
        )
    ).json()
    assert upload["status"] == "pending" and "upload_url" in upload
    sent = await client.post(
        upload["upload_url"],
        headers=headers,
        files={"file": ("report.pdf", b"%PDF-1.4\nsynthetic", "application/pdf")},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "uploaded" and "upload_url" not in sent.json()
    page = await create_page(client)
    appended = await client.patch(
        f"/v1/blocks/{page['id']}/children",
        headers=headers,
        json={"children": [{"pdf": {"type": "file_upload", "file_upload": {"id": upload["id"]}}}]},
    )
    assert appended.status_code == 200, appended.text
    file = appended.json()["results"][0]["pdf"]
    assert file["type"] == "file" and "file.notion.so" in file["file"]["url"]
    downloaded = await client.get(file["file"]["url"])
    assert downloaded.content == b"%PDF-1.4\nsynthetic"
    assert downloaded.headers["content-type"] == "application/pdf"
    stored = (await client.get("/v1/file_uploads/" + upload["id"], headers=headers)).json()
    assert stored["expiry_time"] is None
    listed = await client.get("/v1/file_uploads", headers=headers, params={"status": "uploaded"})
    assert listed.status_code == 200 and listed.json()["results"][0]["id"] == upload["id"]
    assert (await client.get(file["file"]["url"] + "tampered")).status_code == 403
    ctx = client.hub.context("notion")
    ctx.clock.advance(timedelta(hours=2))
    try:
        assert (await client.get(file["file"]["url"])).status_code == 403
        refreshed = (await client.get("/v1/blocks/" + appended.json()["results"][0]["id"])).json()
        assert (await client.get(refreshed["pdf"]["file"]["url"])).content.startswith(b"%PDF")
    finally:
        ctx.clock.advance(timedelta(hours=-2))


async def test_upload_multipart_complete_isolation_and_failures(client: httpx.AsyncClient) -> None:
    headers = {"Notion-Version": NEW}
    response = await client.post(
        "/v1/file_uploads",
        headers=headers,
        json={"mode": "multi_part", "number_of_parts": 2, "filename": "combined.txt"},
    )
    upload = response.json()
    assert response.status_code == 200 and "complete_url" in upload
    assert (await client.post(upload["complete_url"], headers=headers)).status_code == 400
    assert (await client.post(upload["upload_url"], headers=headers, content=b"raw")).status_code == 400
    for number, content in [(2, b"world"), (1, b"hello ")]:
        sent = await client.post(
            upload["upload_url"],
            headers=headers,
            data={"part_number": str(number)},
            files={"file": ("combined.txt", content, "text/plain")},
        )
        assert sent.status_code == 200 and sent.json()["status"] == "pending"
    completed = await client.post(upload["complete_url"], headers=headers)
    assert completed.status_code == 200 and completed.json()["content_length"] == 11
    page = await create_page(client)
    append = await client.patch(
        f"/v1/blocks/{page['id']}/children",
        headers=headers,
        json={
            "children": [
                {"file": {"type": "file_upload", "file_upload": {"id": upload["id"]}, "name": "combined.txt"}}
            ]
        },
    )
    assert append.status_code == 200, append.text
    file_url = append.json()["results"][0]["file"]["file"]["url"]
    assert (await client.get(file_url)).content == b"hello world"
    foreign = {**headers, **await delegated(client, "p018", client_id="foreign")}
    assert (await client.get("/v1/file_uploads/" + upload["id"], headers=foreign)).status_code == 404
    assert (
        await client.patch(
            f"/v1/blocks/{page['id']}/children",
            headers=foreign,
            json={"children": [{"file": {"type": "file_upload", "file_upload": {"id": upload["id"]}}}]},
        )
    ).status_code in (400, 403)
    pending = (await client.post("/v1/file_uploads", headers=headers, json={})).json()
    ctx = client.hub.context("notion")
    ctx.clock.advance(timedelta(hours=2))
    try:
        assert (await client.get("/v1/file_uploads/" + pending["id"], headers=headers)).json()[
            "status"
        ] == "expired"
    finally:
        ctx.clock.advance(timedelta(hours=-2))


async def test_lazy_seeded_pdf_and_file_signature(client: httpx.AsyncClient) -> None:
    doc = next(
        d
        for d in client.hub.world.documents
        if d.kind == "pdf" and d.space_id == "clients" and not d.restricted
    )
    blocks = (await client.get(f"/v1/blocks/{sid('document', doc.id)}/children")).json()["results"]
    assert blocks[0]["type"] == "pdf"
    url = blocks[0]["pdf"]["file"]["url"]
    assert "amazonaws" not in url and url.startswith("https://file.notion.so/")
    response = await client.get(url)
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert "attachment" in response.headers["content-disposition"]
    assert (await client.get(url)).content == response.content


async def test_webhooks_verification_and_signed_events(client: httpx.AsyncClient) -> None:
    received: list[tuple[dict[str, Any], bytes, str | None]] = []
    receiver = FastAPI()

    @receiver.post("/notion")
    async def receive(request: Request) -> dict[str, Any]:
        raw = await request.body()
        received.append((json.loads(raw), raw, request.headers.get("x-notion-signature")))
        return {}

    client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    configured = await client.post(
        "/_emulator/actions/configure_webhook", json={"url": "http://consumer.example/notion"}
    )
    assert configured.status_code == 200, configured.text
    secret = configured.json()["verification_token"]
    assert received[0][0] == {"verification_token": secret}
    page = await create_page(client)
    await client.patch(
        "/v1/pages/" + page["id"], json={"properties": {"title": {"title": [text("Changed")]}}}
    )
    await client.patch(f"/v1/blocks/{page['id']}/children", json={"children": [{"divider": {}}]})
    await client.post(
        "/v1/comments", json={"parent": {"page_id": page["id"]}, "rich_text": [text("Comment")]}
    )
    await client.patch("/v1/databases/" + CLIENTS, json={"properties": {"QA": {"checkbox": {}}}})
    types = [r[0].get("type") for r in received[1:]]
    assert types == [
        "page.created",
        "page.properties_updated",
        "page.content_updated",
        "comment.created",
        "database.schema_updated",
    ]
    for event, raw, signature in received[1:]:
        assert signature == "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        assert event["workspace_id"] and event["subscription_id"] == configured.json()["subscription_id"]
        assert event["entity"]["id"] and event["timestamp"].endswith("Z")
    journal = (await client.get("/_emulator/webhooks")).json()
    assert journal["count"] == 5 and all(d["status"] == "delivered" for d in journal["deliveries"])


async def test_mention_visibility_and_user_email_capability(client: httpx.AsyncClient) -> None:
    mentions = [
        {
            "type": "mention",
            "mention": {"type": "page", "page": {"id": PEOPLE}},
            "annotations": {"bold": True},
        },
        {"type": "mention", "mention": {"type": "user", "user": {"id": person_id("p018")}}},
    ]
    page = await create_page(client, children=[{"paragraph": {"rich_text": mentions}}])
    assert "has_children" not in page
    blocks = (await client.get(f"/v1/blocks/{page['id']}/children")).json()["results"]
    assert blocks[0]["paragraph"]["rich_text"][0]["plain_text"] == "Untitled"
    assert not blocks[0]["paragraph"]["rich_text"][0]["annotations"]["bold"]
    partner = await delegated(client, "p018", scopes=["read_content", "read_user"])
    visible = (await client.get(f"/v1/blocks/{page['id']}/children", headers=partner)).json()["results"]
    rich = visible[0]["paragraph"]["rich_text"]
    assert rich[0]["plain_text"] == "People & Culture"
    assert rich[1]["mention"]["user"]["person"] == {}
    assert (await client.get(f"/v1/pages/{page['id']}/properties/title")).status_code == 200


async def test_rich_text_people_relation_property_item_pagination(client: httpx.AsyncClient) -> None:
    target = (await client.get("/v1/databases/" + sid("database", "engagements"))).json()
    target_rows = (await client.post(f"/v1/databases/{target['id']}/query", json={})).json()["results"][:30]
    assert len(target_rows) == 30
    db_response = await client.post(
        "/v1/databases",
        json={
            "parent": {"page_id": KNOWLEDGE},
            "properties": {
                "Name": {"title": {}},
                "_Summary": {"rich_text": {}},
                "People": {"people": {}},
                "Related": {"relation": {"database_id": target["id"]}},
            },
        },
    )
    assert db_response.status_code == 200, db_response.text
    db = db_response.json()
    assert "_Summary" in db["properties"]
    result = await client.post(
        "/v1/pages",
        json={
            "parent": {"database_id": db["id"]},
            "properties": {
                "Name": {"title": [text("Long properties")]},
                "_Summary": {"rich_text": [text(str(i)) for i in range(30)]},
                "People": {
                    "people": [{"id": person_id(p.id)} for p in client.hub.world.active_employees[:30]]
                },
                "Related": {"relation": [{"id": p["id"]} for p in target_rows]},
            },
        },
    )
    assert result.status_code == 200, result.text
    page = result.json()
    assert page["properties"]["Related"]["has_more"]
    for name, kind in [("_Summary", "rich_text"), ("People", "people"), ("Related", "relation")]:
        prop = page["properties"][name]
        assert len(prop[kind]) == 25
        first = (
            await client.get(f"/v1/pages/{page['id']}/properties/{prop['id']}", params={"page_size": 20})
        ).json()
        assert first["has_more"] and len(first["results"]) == 20
        second = (await client.get(first["property_item"]["next_url"])).json()
        assert len(second["results"]) == 10 and not second["has_more"]


async def test_uploaded_file_page_icon_and_files_property_readback(client: httpx.AsyncClient) -> None:
    headers = {"Notion-Version": NEW}
    upload = (await client.post("/v1/file_uploads", headers=headers, json={"filename": "asset.png"})).json()
    assert (
        await client.post(
            upload["upload_url"],
            headers=headers,
            files={"file": ("asset.png", b"synthetic-image", "image/png")},
        )
    ).status_code == 200
    reference = {"type": "file_upload", "file_upload": {"id": upload["id"]}}
    page = await create_page(client, icon=reference, cover=reference)
    for field in ("icon", "cover"):
        assert page[field]["type"] == "file"
        assert (await client.get(page[field]["file"]["url"])).content == b"synthetic-image"
    db = (
        await client.post(
            "/v1/databases",
            json={
                "parent": {"page_id": KNOWLEDGE},
                "properties": {"Name": {"title": {}}, "Files": {"files": {}}},
            },
        )
    ).json()
    row = (
        await client.post(
            "/v1/pages",
            json={
                "parent": {"database_id": db["id"]},
                "properties": {
                    "Name": {"title": [text("Attachment")]},
                    "Files": {"files": [{**reference, "name": "asset.png"}]},
                },
            },
        )
    ).json()
    item = (await client.get(f"/v1/pages/{row['id']}/properties/{row['properties']['Files']['id']}")).json()
    assert item["object"] == "property_item" and item["files"][0]["name"] == "asset.png"
    assert (await client.get(item["files"][0]["file"]["url"])).content == b"synthetic-image"
    forged = await client.patch(
        f"/v1/blocks/{page['id']}/children",
        json={"children": [{"file": {"type": "file", "_file_id": upload["id"]}}]},
    )
    assert forged.status_code == 400


async def test_webhooks_honor_integration_sharing(client: httpx.AsyncClient) -> None:
    events = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> dict[str, Any]:
        events.append(await request.json())
        return {}

    client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    assert (
        await client.post(
            "/_emulator/actions/configure_webhook", json={"url": "http://consumer.example/hook"}
        )
    ).status_code == 200
    people_editor = await delegated(
        client, next(p.id for p in client.hub.world.active_employees if p.department_id == "people")
    )
    changed = await client.patch(
        "/v1/pages/" + PEOPLE, headers=people_editor, json={"icon": {"type": "emoji", "emoji": "📚"}}
    )
    assert changed.status_code == 200
    assert len(events) == 1  # Verification only: the internal integration cannot read People.
    await create_page(client)
    assert events[-1]["type"] == "page.created" and len(events) == 2
