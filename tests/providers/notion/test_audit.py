"""Adversarial HTTP regressions for the Notion fidelity audit."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.clock import parse_instant
from emulators.providers.notion.common import BOT_ID, NEW, OLD, person_id, record_by_id
from emulators.providers.notion.seed import sid

from .conftest import delegated
from .test_workspace import CLIENTS, KNOWLEDGE, create_page, text


@pytest.mark.parametrize("wrong", [None, False, 7, "bad", [], ["bad"]])
async def test_malformed_attachment_and_sync_values_are_errors(client: httpx.AsyncClient, wrong: Any) -> None:
    cases = [
        {"icon": {"type": "external", "external": wrong}},
        {"cover": {"external": wrong}},
        {"children": [{"synced_block": {"synced_from": wrong}}]},
    ]
    for extra in cases:
        response = await client.post("/v1/pages", json={"parent": {"page_id": KNOWLEDGE}, **extra})
        if wrong is None and "children" in extra:
            assert response.status_code == 200  # Original synced blocks explicitly use null.
        else:
            assert response.status_code == 400, response.text
            assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize("wrong", [None, False, 0, "bad", {}, [None]])
async def test_rich_text_requires_array_of_objects(client: httpx.AsyncClient, wrong: Any) -> None:
    for payload in (
        {"properties": {"title": {"title": wrong}}},
        {"children": [{"paragraph": {"rich_text": wrong}}]},
        {"children": wrong} if wrong != [None] else {"children": [{"paragraph": wrong}]},
    ):
        response = await client.post("/v1/pages", json={"parent": {"page_id": KNOWLEDGE}, **payload})
        assert response.status_code == 400, response.text


@pytest.mark.parametrize(
    "item",
    [
        {"type": []},
        text("x", annotations={"bold": "true"}),
        text("x", annotations={"color": []}),
        {"mention": {"type": [], "user": {"id": BOT_ID}}, "type": "mention"},
        {"equation": {"expression": "x" * 1001}, "type": "equation"},
        {"text": {"content": "x", "link": {"url": "https://files.example/" + "x" * 2000}}},
    ],
)
async def test_invalid_rich_text_fields(client: httpx.AsyncClient, item: Any) -> None:
    response = await client.post(
        "/v1/pages", json={"parent": {"page_id": KNOWLEDGE}, "properties": {"title": {"title": [item]}}}
    )
    assert response.status_code == 400, response.text


async def test_block_patch_is_atomic_and_respects_archive_ancestors(client: httpx.AsyncClient) -> None:
    page = await create_page(client, children=[{"paragraph": {"rich_text": [text("Before")]}}])
    block = (await client.get(f"/v1/blocks/{page['id']}/children")).json()["results"][0]
    route = f"/v1/blocks/{block['id']}"
    before = (await client.get(route)).json()
    bad = await client.patch(route, json={"paragraph": {"rich_text": [text("After")]}, "archived": "bad"})
    assert bad.status_code == 400
    assert (await client.get(route)).json() == before
    await client.patch(f"/v1/pages/{page['id']}", json={"archived": True})
    denied = await client.patch(route, json={"paragraph": {"rich_text": [text("Hidden write")]}})
    assert denied.status_code == 400
    assert (await client.get(route)).json() == before
    assert (await client.patch(f"/v1/pages/{page['id']}", json={"in_trash": False})).status_code == 200
    assert (
        await client.patch(route, json={"paragraph": {"rich_text": [text("Restored")]}})
    ).status_code == 200


async def test_failed_page_write_does_not_change_select_schema_or_upload_expiry(
    client: httpx.AsyncClient,
) -> None:
    headers = {"Notion-Version": NEW}
    upload = (await client.post("/v1/file_uploads", headers=headers, json={"filename": "audit.png"})).json()
    await client.post(
        upload["upload_url"], headers=headers, files={"file": ("audit.png", b"fixture", "image/png")}
    )
    source = (await client.get(f"/v1/databases/{CLIENTS}", headers=headers)).json()["data_sources"][0]["id"]
    before = (await client.get(f"/v1/data_sources/{source}", headers=headers)).json()["properties"]
    ref = {"type": "file_upload", "file_upload": {"id": upload["id"]}}
    failed = await client.post(
        "/v1/pages",
        headers=headers,
        json={
            "parent": {"data_source_id": source},
            "properties": {"Industry": {"select": {"name": "Audit option"}}},
            "icon": ref,
            "children": [{"paragraph": {"rich_text": 7}}],
        },
    )
    assert failed.status_code == 400
    after = (await client.get(f"/v1/data_sources/{source}", headers=headers)).json()["properties"]
    assert after == before
    assert (await client.get(f"/v1/file_uploads/{upload['id']}", headers=headers)).json()["expiry_time"]
    page = await create_page(client, icon=ref)
    assert page["icon"]["type"] == "file"
    assert (await client.get(f"/v1/file_uploads/{upload['id']}", headers=headers)).json()[
        "expiry_time"
    ] is None


async def test_empty_scopes_deny_content_and_selected_app_pages_are_enforced(
    client: httpx.AsyncClient,
) -> None:
    empty = await delegated(client, "p018", scopes=[])
    for url in (f"/v1/pages/{KNOWLEDGE}", "/v1/users"):
        response = await client.get(url, headers=empty)
        assert response.status_code == 403 and response.json()["code"] == "restricted_resource"
    assert (await client.get("/v1/users/me", headers=empty)).status_code == 200
    minted = (await client.post("/_emulator/tokens", json={"shared_page_ids": [KNOWLEDGE]})).json()
    limited = {"Authorization": "Bearer " + minted["access_token"]}
    assert (await client.get(f"/v1/pages/{KNOWLEDGE}", headers=limited)).status_code == 200
    assert (await client.get(f"/v1/databases/{CLIENTS}", headers=limited)).status_code == 404


async def test_user_metadata_respects_capabilities_everywhere(client: httpx.AsyncClient) -> None:
    ref = {"type": "mention", "mention": {"type": "user", "user": {"id": person_id("p001")}}}
    page = await create_page(client, properties={"title": {"title": [ref]}})
    auth = await delegated(client, "p018", scopes=["read_content"])
    record = (await client.get(f"/v1/pages/{page['id']}", headers=auth)).json()
    mention = record["properties"]["title"]["title"][0]
    assert mention["mention"]["user"] == {"object": "user", "id": person_id("p001")}
    assert mention["plain_text"] == "@Anonymous"
    owner = (await client.get("/v1/users/me", headers=auth)).json()["bot"]["owner"]["user"]
    assert owner == {"object": "user", "id": person_id("p018")}
    row_id = sid("client", "c001")
    full = (await client.get(f"/v1/pages/{row_id}")).json()["properties"]["Owner"]["people"][0]
    assert full["type"] == "person" and full["person"]["email"].endswith(".example")
    no_email = await delegated(client, "p018", scopes=["read_content", "read_user"])
    limited = (await client.get(f"/v1/pages/{row_id}", headers=no_email)).json()["properties"]["Owner"][
        "people"
    ][0]
    assert limited["person"] == {} and limited["name"]


@pytest.mark.parametrize("version,list_type", [(OLD, "page_or_database"), (NEW, "page_or_data_source")])
async def test_versioned_list_shapes_and_cursor_reuse(
    client: httpx.AsyncClient, version: str, list_type: str
) -> None:
    headers = {"Notion-Version": version}
    payload = {"query": "", "page_size": 3}
    first = (await client.post("/v1/search", headers=headers, json=payload)).json()
    assert first["type"] == list_type and first[list_type] == {}
    payload["start_cursor"] = first["next_cursor"]
    again = (await client.post("/v1/search", headers=headers, json=payload)).json()
    replay = (await client.post("/v1/search", headers=headers, json=payload)).json()
    assert again["results"] == replay["results"]
    assert not {r["id"] for r in first["results"]} & {r["id"] for r in again["results"]}
    for cursor in (False, 7, [], {}, ""):
        bad = await client.post("/v1/search", headers=headers, json={"start_cursor": cursor})
        assert bad.status_code == 400
    for target in (None, {}, [], False):
        assert (await client.post("/v1/search", headers=headers, json={"filter": target})).status_code == 400


@pytest.mark.parametrize("route", ["/v1/oauth/introspect", "/v1/oauth/revoke"])
@pytest.mark.parametrize("token", [None, False, [], {}, 7, ""])
async def test_oauth_token_parameter_validation(client: httpx.AsyncClient, route: str, token: Any) -> None:
    response = await client.post(
        route, auth=httpx.BasicAuth("notion-client", "notion-secret"), json={"token": token}
    )
    assert response.status_code == 400 and response.json()["code"] == "invalid_request"


@pytest.mark.parametrize("content_type", [None, False, 7, [], {}, "bad", "text/plain\r\ninjected: yes"])
async def test_upload_content_type_validation(client: httpx.AsyncClient, content_type: Any) -> None:
    response = await client.post(
        "/v1/file_uploads", headers={"Notion-Version": NEW}, json={"content_type": content_type}
    )
    assert response.status_code == 400, response.text


async def test_upload_metadata_and_part_counts(client: httpx.AsyncClient) -> None:
    headers = {"Notion-Version": NEW}
    response = await client.post(
        "/v1/file_uploads", headers=headers, json={"filename": "audit", "content_type": "text/plain"}
    )
    assert response.status_code == 200
    assert response.json()["filename"] == "audit.txt"
    assert response.json()["created_by"] == {"id": BOT_ID, "type": "bot"}
    assert response.json()["in_trash"] is False
    too_long = await client.post(
        "/v1/file_uploads", headers=headers, json={"filename": "a" * 899, "content_type": "text/plain"}
    )
    assert too_long.status_code == 400
    assert (
        await client.post(
            "/v1/file_uploads", headers=headers, json={"mode": "multi_part", "filename": "audit.txt"}
        )
    ).status_code == 400
    inferred = (await client.post("/v1/file_uploads", headers=headers, json={})).json()
    named = await client.post(
        inferred["upload_url"],
        headers=headers,
        files={"file": ('audit"draft"\n2026.txt', b"fixture", "text/plain")},
    )
    assert named.json()["filename"] == "auditdraft_2026.txt"
    multi = (
        await client.post(
            "/v1/file_uploads",
            headers=headers,
            json={"mode": "multi_part", "number_of_parts": 2, "filename": "audit.txt"},
        )
    ).json()
    assert multi["number_of_parts"] == {"total": 2, "sent": 0}
    sent = await client.post(
        multi["upload_url"],
        headers=headers,
        data={"part_number": "2"},
        files={"file": ("audit.txt", b"second", "text/plain")},
    )
    assert sent.json()["number_of_parts"] == {"total": 2, "sent": 1}
    assert (await client.post(multi["complete_url"], headers=headers, content=b"bad json")).json()[
        "code"
    ] == "invalid_json"


async def test_request_ids_cover_success_errors_routes_and_faults(client: httpx.AsyncClient) -> None:
    responses = [
        await client.get("/v1/users/me"),
        await client.get("/v1/pages/invalid"),
        await client.get("/v1/missing"),
    ]
    await client.post("/_emulator/faults", json={"path": "/v1/search", "status": 429})
    responses.append(await client.post("/v1/search", json={}))
    for response in responses:
        UUID(response.headers["x-request-id"])
        if response.is_error:
            assert response.headers["x-request-id"] == response.json()["request_id"]
    assert responses[-1].headers["retry-after"].isdigit()


@pytest.mark.parametrize(
    "raw", [b'{"query":"\\ud800"}', b'{"page_size":NaN}', b'{"page_size":1e309}', b"[", b"null", b""]
)
async def test_invalid_json_and_unicode_do_not_crash(client: httpx.AsyncClient, raw: bytes) -> None:
    response = await client.post("/v1/search", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 400 and response.json()["object"] == "error"


async def test_formula_mismatched_result_types_do_not_crash(client: httpx.AsyncClient) -> None:
    db = (
        await client.post(
            "/v1/databases",
            json={
                "parent": {"page_id": KNOWLEDGE},
                "properties": {"Name": {"title": {}}, "Formula": {"formula": {"expression": '"hello"'}}},
            },
        )
    ).json()
    await client.post("/v1/pages", json={"parent": {"database_id": db["id"]}})
    response = await client.post(
        f"/v1/databases/{db['id']}/query",
        json={"filter": {"property": "Formula", "formula": {"number": {"greater_than": 7}}}},
    )
    assert response.status_code == 200 and response.json()["results"] == []


async def test_timestamp_filter_does_not_use_same_named_property(client: httpx.AsyncClient) -> None:
    db = (
        await client.post(
            "/v1/databases",
            json={
                "parent": {"page_id": KNOWLEDGE},
                "properties": {"Name": {"title": {}}, "created_time": {"date": {}}},
            },
        )
    ).json()
    await client.post(
        "/v1/pages",
        json={
            "parent": {"database_id": db["id"]},
            "properties": {"created_time": {"date": {"start": "2000-01-01"}}},
        },
    )
    response = await client.post(
        f"/v1/databases/{db['id']}/query",
        json={"filter": {"timestamp": "created_time", "created_time": {"after": "2020-01-01"}}},
    )
    assert response.status_code == 200 and len(response.json()["results"]) == 1


async def test_webhook_retry_payload_and_signature(client: httpx.AsyncClient) -> None:
    received: list[tuple[dict[str, Any], bytes, str]] = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> Response:
        raw = await request.body()
        data = json.loads(raw)
        if "verification_token" not in data:
            received.append((data, raw, request.headers["x-notion-signature"]))
        return Response(status_code=503 if len(received) in (1, 2) else 200)

    client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    subscription = (
        await client.post(
            "/_emulator/actions/configure_webhook",
            json={"url": "http://consumer.example/hook", "api_version": NEW},
        )
    ).json()
    await create_page(client)
    assert [event[0]["attempt_number"] for event in received] == [1, 2, 3]
    assert len({event[0]["id"] for event in received}) == 1
    for event, raw, signature in received:
        assert event["authors"] == [{"id": BOT_ID, "type": "bot"}]
        assert event["workspace_name"] == "Halcyon Advisory Group"
        assert event["data"]["parent"] == {"id": KNOWLEDGE, "type": "page"}
        assert (
            signature
            == "sha256="
            + hmac.new(subscription["verification_token"].encode(), raw, hashlib.sha256).hexdigest()
        )


async def test_schema_name_type_validation_and_title_invariants(client: httpx.AsyncClient) -> None:
    for spec in (
        {"Name": {"title": {}, "name": []}},
        {"Name": {"type": []}},
        {"Name": {"title": {}}, "R": {"relation": {"type": []}}},
        {"Name": {"title": {}}, "Number": {"number": {"format": []}}},
        {"Name": {"title": {}}, "Number": {"number": {"format": "unknown-currency"}}},
        {"Name": {"title": {}, "number": {}}},
    ):
        response = await client.post(
            "/v1/databases", json={"parent": {"page_id": KNOWLEDGE}, "properties": spec}
        )
        assert response.status_code == 400
    before = (await client.get(f"/v1/databases/{CLIENTS}")).json()
    response = await client.patch(
        f"/v1/databases/{CLIENTS}", json={"properties": {"Name": None, "Replacement": {"title": {}}}}
    )
    assert response.status_code == 400
    assert (await client.get(f"/v1/databases/{CLIENTS}")).json() == before


async def test_invalid_date_value_cannot_corrupt_page(client: httpx.AsyncClient) -> None:
    row = (await client.get(f"/v1/pages/{sid('client', 'c001')}")).json()
    before = copy.deepcopy(row)
    for value in (
        {"start": "2026-01-01", "end": 7},
        {"start": "2026-01-02", "end": "2026-01-01"},
        {"start": "2026-01-01", "time_zone": []},
    ):
        response = await client.patch(
            f"/v1/pages/{row['id']}", json={"properties": {"Client Since": {"date": value}}}
        )
        assert response.status_code == 400
        assert (await client.get(f"/v1/pages/{row['id']}")).json() == before


async def test_all_write_routes_reject_non_json_and_oversized_payloads(client: httpx.AsyncClient) -> None:
    page = await create_page(client, children=[{"paragraph": {"rich_text": []}}])
    block = (await client.get(f"/v1/blocks/{page['id']}/children")).json()["results"][0]["id"]
    source = (await client.get(f"/v1/databases/{CLIENTS}", headers={"Notion-Version": NEW})).json()[
        "data_sources"
    ][0]["id"]
    upload = (
        await client.post(
            "/v1/file_uploads", json={"mode": "multi_part", "filename": "audit.txt", "number_of_parts": 2}
        )
    ).json()["id"]
    routes = [
        ("POST", "/v1/pages"),
        ("PATCH", f"/v1/pages/{page['id']}"),
        ("PATCH", f"/v1/blocks/{block}"),
        ("PATCH", f"/v1/blocks/{page['id']}/children"),
        ("POST", "/v1/databases"),
        ("PATCH", f"/v1/databases/{CLIENTS}"),
        ("POST", "/v1/data_sources"),
        ("PATCH", f"/v1/data_sources/{source}"),
        ("POST", f"/v1/data_sources/{source}/query"),
        ("POST", "/v1/search"),
        ("POST", "/v1/comments"),
        ("POST", "/v1/file_uploads"),
        ("POST", f"/v1/file_uploads/{upload}/send"),
        ("POST", f"/v1/file_uploads/{upload}/complete"),
        ("POST", "/v1/oauth/token"),
        ("POST", "/v1/oauth/introspect"),
        ("POST", "/v1/oauth/revoke"),
    ]
    for method, route in routes:
        for raw in (
            b"",
            b"not-json",
            b"[]",
            b"null",
            b"false",
            b'"text"',
            b'{"value":"\\ud800"}',
            b'{"huge":"' + b"x" * (501 * 1024) + b'"}',
        ):
            response = await client.request(
                method,
                route,
                headers={"Notion-Version": NEW, "Content-Type": "application/json"},
                auth=httpx.BasicAuth("notion-client", "notion-secret") if "/oauth/" in route else None,
                content=raw,
            )
            assert response.status_code == 400, (route, response.text[:150])
            assert response.json()["object"] == "error"


async def test_block_layout_and_table_update_validation(client: httpx.AsyncClient) -> None:
    page = await create_page(client)
    route = f"/v1/blocks/{page['id']}/children"
    for block in (
        {"column_list": {"children": []}},
        {"column_list": {"children": [{"column": {"children": []}}, {"column": {"children": []}}]}},
        {"column": {"children": [{"paragraph": {"rich_text": []}}]}},
        {"table_row": {"cells": [[]]}},
        {"code": {"language": "unsupported-language", "rich_text": []}},
    ):
        assert (await client.patch(route, json={"children": [block]})).status_code == 400
    table = (
        await client.patch(
            route,
            json={
                "children": [{"table": {"table_width": 2, "children": [{"table_row": {"cells": [[], []]}}]}}]
            },
        )
    ).json()["results"][0]
    row = (await client.get(f"/v1/blocks/{table['id']}/children")).json()["results"][0]
    assert (
        await client.patch(f"/v1/blocks/{table['id']}", json={"table": {"table_width": 3}})
    ).status_code == 400
    assert (
        await client.patch(f"/v1/blocks/{row['id']}", json={"table_row": {"cells": [[]]}})
    ).status_code == 400
    assert (await client.get(f"/v1/blocks/{row['id']}")).json() == row


async def test_insert_only_returns_partial_objects(client: httpx.AsyncClient) -> None:
    auth = (await client.post("/_emulator/tokens", json={"scopes": ["insert_content"]})).json()
    headers = {"Authorization": "Bearer " + auth["access_token"]}
    response = await client.post("/v1/pages", headers=headers, json={"parent": {"page_id": KNOWLEDGE}})
    assert response.status_code == 200 and set(response.json()) == {"object", "id"}
    assert (await client.get(f"/v1/pages/{response.json()['id']}", headers=headers)).status_code == 403


async def test_old_api_supports_file_uploads_and_rejects_multi_source_writes_atomically(
    client: httpx.AsyncClient,
) -> None:
    upload = (await client.post("/v1/file_uploads", json={"filename": "audit.txt"})).json()
    sent = await client.post(upload["upload_url"], files={"file": ("audit.txt", b"fixture", "text/plain")})
    assert sent.status_code == 200 and sent.json()["status"] == "uploaded"
    await client.post(
        "/v1/data_sources", headers={"Notion-Version": NEW}, json={"parent": {"database_id": CLIENTS}}
    )
    before = (await client.get(f"/v1/databases/{CLIENTS}", headers={"Notion-Version": NEW})).json()
    failed = await client.patch(f"/v1/databases/{CLIENTS}", json={"title": [text("Must not persist")]})
    assert failed.status_code == 400
    assert (await client.get(f"/v1/databases/{CLIENTS}", headers={"Notion-Version": NEW})).json() == before


async def test_webhook_schema_versions_and_comment_capabilities(client: httpx.AsyncClient) -> None:
    events = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> dict[str, Any]:
        events.append(await request.json())
        return {}

    client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    token = (await client.post("/_emulator/tokens", json={"scopes": ["read_content"]})).json()["access_token"]
    await client.post(
        "/_emulator/actions/configure_webhook",
        json={"url": "http://consumer.example/hook", "api_version": NEW, "token": token},
    )
    response = await client.patch(
        f"/v1/databases/{CLIENTS}", json={"properties": {"Audit": {"checkbox": {}}}}
    )
    assert response.status_code == 200
    event = events[-1]
    source_id = client.hub.context("notion").store["databases"].get(CLIENTS)["_sources"][0]
    assert event["type"] == "data_source.schema_updated"
    assert event["entity"] == {"id": source_id, "type": "data_source"}
    assert event["data"]["parent"] == {"id": CLIENTS, "type": "database"}
    assert event["data"]["updated_properties"] == [
        {"id": response.json()["properties"]["Audit"]["id"], "name": "Audit", "action": "created"}
    ]
    page = await create_page(client)
    count = len(events)
    await client.post("/v1/comments", json={"parent": {"page_id": page["id"]}, "rich_text": [text("Review")]})
    assert len(events) == count
    await client.patch(f"/v1/pages/{page['id']}", json={"in_trash": True})
    assert events[-1]["type"] == "page.deleted"
    await client.patch(f"/v1/pages/{page['id']}", json={"in_trash": False})
    assert events[-1]["type"] == "page.undeleted"


async def test_seed_timestamp_and_parent_integrity(client: httpx.AsyncClient) -> None:
    ctx = client.hub.context("notion")
    for collection in ("pages", "blocks", "databases", "data_sources"):
        for record in ctx.store[collection]:
            assert record["created_time"] <= record["last_edited_time"]
            assert parse_instant(record["created_time"]) <= ctx.world.anchor
            parent = record["parent"]
            if parent["type"] != "workspace":
                parent_record = record_by_id(ctx, parent[parent["type"]])
                assert parent_record is not None
                assert parent_record["created_time"] <= record["created_time"]


async def test_webhook_connection_failure_is_validation_error(client: httpx.AsyncClient) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Synthetic unreachable receiver", request=request)

    client.hub.webhook_transport = httpx.MockTransport(unreachable)
    for url in ("http://", "http://receiver.example/hook"):
        response = await client.post("/_emulator/actions/configure_webhook", json={"url": url})
        assert response.status_code == 400 and response.json()["code"] == "validation_error"
    assert not client.hub.context("notion").store["_subscriptions"].values()


async def test_date_sort_uses_instants_and_empty_filters_require_true(client: httpx.AsyncClient) -> None:
    db = (
        await client.post(
            "/v1/databases",
            json={
                "parent": {"page_id": KNOWLEDGE},
                "properties": {"Name": {"title": {}}, "Date": {"date": {}}, "Count": {"number": {}}},
            },
        )
    ).json()
    ids = []
    for date in ("2026-01-01T10:00:00-05:00", "2026-01-01T12:00:00Z"):
        created = await client.post(
            "/v1/pages",
            json={"parent": {"database_id": db["id"]}, "properties": {"Date": {"date": {"start": date}}}},
        )
        ids.append(created.json()["id"])
    ordered = await client.post(
        f"/v1/databases/{db['id']}/query", json={"sorts": [{"property": "Date", "direction": "ascending"}]}
    )
    assert [row["id"] for row in ordered.json()["results"]] == ids[::-1]
    for property_name, kind in (("Name", "title"), ("Date", "date"), ("Count", "number")):
        bad = await client.post(
            f"/v1/databases/{db['id']}/query",
            json={"filter": {"property": property_name, kind: {"is_empty": False}}},
        )
        assert bad.status_code == 400


async def test_formula_scalar_response_types(client: httpx.AsyncClient) -> None:
    db = (
        await client.post(
            "/v1/databases",
            json={
                "parent": {"page_id": KNOWLEDGE},
                "properties": {
                    "Name": {"title": {}},
                    "Status": {"select": {}},
                    "Label": {"formula": {"expression": 'prop("Status")'}},
                },
            },
        )
    ).json()
    created = await client.post(
        "/v1/pages",
        json={
            "parent": {"database_id": db["id"]},
            "properties": {"Status": {"select": {"name": "Published"}}},
        },
    )
    assert created.status_code == 200
    assert created.json()["properties"]["Label"]["formula"] == {"type": "string", "string": "Published"}


async def test_explicit_property_values_cannot_be_missing_or_null_checkbox(client: httpx.AsyncClient) -> None:
    db = (
        await client.post(
            "/v1/databases",
            json={
                "parent": {"page_id": KNOWLEDGE},
                "properties": {"Name": {"title": {}}, "Done": {"checkbox": {}}},
            },
        )
    ).json()
    for props in ({"Name": {}}, {"Name": {"type": "title"}}, {"Done": {"checkbox": None}}):
        response = await client.post(
            "/v1/pages", json={"parent": {"database_id": db["id"]}, "properties": props}
        )
        assert response.status_code == 400


async def test_huge_numbers_and_formula_overflow_do_not_crash(client: httpx.AsyncClient) -> None:
    db = (
        await client.post(
            "/v1/databases",
            json={
                "parent": {"page_id": KNOWLEDGE},
                "properties": {
                    "Name": {"title": {}},
                    "Count": {"number": {}},
                    "Double": {"formula": {"expression": 'prop("Count") * 2'}},
                },
            },
        )
    ).json()
    oversized = await client.post(
        "/v1/pages", json={"parent": {"database_id": db["id"]}, "properties": {"Count": {"number": 10**1000}}}
    )
    assert oversized.status_code == 400
    overflow = await client.post(
        "/v1/pages", json={"parent": {"database_id": db["id"]}, "properties": {"Count": {"number": 1e308}}}
    )
    assert overflow.status_code == 200
    assert overflow.json()["properties"]["Double"]["formula"] == {"type": "number", "number": None}
