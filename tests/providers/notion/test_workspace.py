from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.providers.notion.common import BOT_ID, INTERNAL_TOKEN, LEGACY_TOKEN, NEW, OLD, person_id
from emulators.providers.notion.seed import sid

from .conftest import delegated

KNOWLEDGE = sid("space", "knowledge")
PEOPLE = sid("space", "people")
CLIENTS = sid("database", "clients")


def text(content: str, **extra: Any) -> dict[str, Any]:
    return {"type": "text", "text": {"content": content}, **extra}


async def create_page(client: httpx.AsyncClient, **kwargs: Any) -> dict[str, Any]:
    payload = {"parent": {"page_id": KNOWLEDGE}, "properties": {"title": {"title": [text("Contract page")]}}}
    payload.update(kwargs)
    result = await client.post("/v1/pages", json=payload)
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.parametrize(
    "headers,code,status",
    [
        ({"Notion-Version": ""}, "missing_version", 400),
        ({"Notion-Version": "2021-01-01"}, "validation_error", 400),
        ({"Authorization": "Bearer invalid"}, "unauthorized", 401),
        ({"Authorization": ""}, "unauthorized", 401),
    ],
)
async def test_headers(client: httpx.AsyncClient, headers: dict[str, str], code: str, status: int) -> None:
    response = await client.get("/v1/users/me", headers=headers)
    assert response.status_code == status
    assert response.json()["object"] == "error"
    assert response.json()["code"] == code
    UUID(response.json()["request_id"])


async def test_users_pagination_and_bot(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/users", params={"page_size": 10})
    first = response.json()
    assert first["object"] == "list" and first["type"] == "user"
    assert first["has_more"] and len(first["results"]) == 10
    assert first["results"][0]["person"]["email"] == "eleanor.whitfield@halcyonadvisory.example"
    rows = list(first["results"])
    while first["has_more"]:
        first = (
            await client.get("/v1/users", params={"page_size": 10, "start_cursor": first["next_cursor"]})
        ).json()
        rows.extend(first["results"])
    assert len(rows) == 46 and len({r["id"] for r in rows}) == 46
    assert (await client.get("/v1/users/" + person_id("p018").replace("-", ""))).json()["name"] == "Maya Chen"
    me = (await client.get("/v1/users/me")).json()
    assert me["id"] == BOT_ID and me["bot"]["workspace_name"] == "Halcyon Advisory Group"
    assert me["bot"]["owner"] == {"type": "workspace", "workspace": True}
    assert (await client.get("/v1/users/00000000-0000-0000-0000-000000000000")).status_code == 404


@pytest.mark.parametrize("size", [0, 101, -1, "abc", "2.0"])
async def test_invalid_pagination(client: httpx.AsyncClient, size: Any) -> None:
    response = await client.get("/v1/users", params={"page_size": size})
    assert response.status_code == 400 and response.json()["code"] == "validation_error"


async def test_cursor_resource_and_token_binding(client: httpx.AsyncClient) -> None:
    users = (await client.get("/v1/users", params={"page_size": 1})).json()
    bad = await client.post("/v1/search", json={"start_cursor": users["next_cursor"]})
    assert bad.status_code == 400
    other = await delegated(client, "p018")
    assert (
        await client.get("/v1/users", params={"start_cursor": users["next_cursor"]}, headers=other)
    ).status_code == 400
    assert (await client.get("/v1/users", params={"start_cursor": "garbage"})).status_code == 400


async def test_capabilities_and_email_redaction(client: httpx.AsyncClient) -> None:
    auth = await delegated(client, "p018", scopes=["read_user", "read_content"])
    user = (await client.get("/v1/users/" + person_id("p001"), headers=auth)).json()
    assert user["person"] == {}
    assert (await client.get("/v1/pages/" + KNOWLEDGE, headers=auth)).status_code == 200
    denied = await client.post("/v1/pages", headers=auth, json={"parent": {"page_id": KNOWLEDGE}})
    assert denied.status_code == 403 and denied.json()["code"] == "restricted_resource"


async def test_integration_sharing_and_user_acl(client: httpx.AsyncClient) -> None:
    for page_id in (PEOPLE, sid("space", "it"), sid("space", "growth")):
        hidden = await client.get("/v1/pages/" + page_id)
        assert hidden.status_code == 404
        assert "shared with your integration" in hidden.json()["message"]
    assert (await client.get("/v1/pages/" + KNOWLEDGE)).status_code == 200
    maya = await delegated(client, "maya.chen@halcyonadvisory.example")
    assert (await client.get("/v1/pages/" + PEOPLE, headers=maya)).status_code == 200  # partner
    reader = await delegated(client, "p008")
    assert (await client.get("/v1/pages/" + PEOPLE, headers=reader)).status_code == 404
    assert (await client.get("/v1/pages/" + KNOWLEDGE, headers=reader)).status_code == 200
    denied = await client.patch(
        "/v1/pages/" + KNOWLEDGE, headers=reader, json={"icon": {"type": "emoji", "emoji": "🧪"}}
    )
    assert denied.status_code == 403
    for token in (INTERNAL_TOKEN, LEGACY_TOKEN):
        assert (
            await client.get("/v1/users/me", headers={"Authorization": "Bearer " + token})
        ).status_code == 200


async def test_restricted_document_and_team_permissions(client: httpx.AsyncClient) -> None:
    world = client.hub.world
    doc = next(d for d in world.documents if d.restricted)
    target = "/v1/pages/" + sid("document", doc.id)
    assert (await client.get(target)).status_code == 404
    owner = await delegated(client, doc.owner_id)
    assert (await client.get(target, headers=owner)).status_code == 200
    unknown = next(
        p.id
        for p in world.active_employees
        if p.id != doc.owner_id
        and all(s.principal_id != p.id for s in doc.shares)
        and p.id not in client.hub.context("notion").store["pages"].get(sid("document", doc.id))["_acl"]
    )
    assert (await client.get(target, headers=await delegated(client, unknown))).status_code == 404
    # Non-shared Technology root does not conceal explicitly shared IT Runbooks database.
    assert (await client.get("/v1/databases/" + sid("database", "runbooks"))).status_code == 200


async def test_document_owner_can_edit_under_read_only_teamspace(client: httpx.AsyncClient) -> None:
    doc = next(d for d in client.hub.world.documents if d.space_id == "knowledge" and not d.restricted)
    headers = await delegated(client, doc.owner_id)
    response = await client.patch(
        "/v1/pages/" + sid("document", doc.id),
        headers=headers,
        json={"icon": {"type": "emoji", "emoji": "📚"}},
    )
    assert response.status_code == 200, response.text


async def test_search_versions_scoping_sorting_and_pagination(client: httpx.AsyncClient) -> None:
    result = (
        await client.post(
            "/v1/search", json={"query": "Clients", "filter": {"property": "object", "value": "database"}}
        )
    ).json()
    assert result["results"][0]["id"] == CLIENTS
    assert result["type"] == "page_or_database"
    newer = await client.post(
        "/v1/search",
        headers={"Notion-Version": NEW},
        json={"query": "Clients", "filter": {"property": "object", "value": "data_source"}},
    )
    assert newer.status_code == 200 and newer.json()["results"][0]["object"] == "data_source"
    assert (
        await client.post(
            "/v1/search",
            headers={"Notion-Version": NEW},
            json={"filter": {"property": "object", "value": "database"}},
        )
    ).status_code == 400
    assert not (await client.post("/v1/search", json={"query": "People & Culture"})).json()["results"]
    reader = await delegated(client, "p008")
    assert not (await client.post("/v1/search", headers=reader, json={"query": "People & Culture"})).json()[
        "results"
    ]
    first = (
        await client.post(
            "/v1/search",
            json={"page_size": 3, "sort": {"direction": "ascending", "timestamp": "last_edited_time"}},
        )
    ).json()
    assert first["has_more"] and len(first["results"]) == 3
    second = (
        await client.post(
            "/v1/search",
            json={
                "page_size": 3,
                "start_cursor": first["next_cursor"],
                "sort": {"direction": "ascending", "timestamp": "last_edited_time"},
            },
        )
    ).json()
    assert not {r["id"] for r in first["results"]} & {r["id"] for r in second["results"]}
    assert [r["last_edited_time"] for r in first["results"]] == sorted(
        r["last_edited_time"] for r in first["results"]
    )


async def test_page_create_retrieve_update_archive_restore_and_rich_text(client: httpx.AsyncClient) -> None:
    title = [text("Linked", annotations={"bold": True}, **{}), text(" text", annotations={"italic": True})]
    title[0]["text"]["link"] = {"url": "https://halcyonadvisory.example"}
    page = await create_page(
        client,
        properties={"title": {"title": title}},
        icon={"type": "emoji", "emoji": "📚"},
        cover={"type": "external", "external": {"url": "https://assets.halcyonadvisory.example/cover.png"}},
        children=[{"paragraph": {"rich_text": [text("hello")]}}],
    )
    UUID(page["id"])
    assert page["parent"] == {"type": "page_id", "page_id": KNOWLEDGE}
    assert page["properties"]["title"]["title"][0]["annotations"]["bold"]
    assert page["properties"]["title"]["title"][0]["href"] == "https://halcyonadvisory.example"
    assert page["created_time"].endswith("Z") and len(page["created_time"].split(".")[1]) == 4
    assert not any(key.startswith("_") for key in page)
    result = (await client.get("/v1/pages/" + page["id"].replace("-", ""))).json()
    assert result["id"] == page["id"]
    changed = await client.patch(
        "/v1/pages/" + page["id"],
        json={"properties": {"title": {"title": [text("Updated")]}}, "icon": None, "archived": True},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["in_trash"] and changed.json()["icon"] is None
    assert not (await client.post("/v1/search", json={"query": "Updated"})).json()["results"]
    conflict = await client.patch(
        "/v1/blocks/" + page["id"] + "/children", json={"children": [{"divider": {}}]}
    )
    assert conflict.status_code == 409
    assert (await client.patch("/v1/pages/" + page["id"], json={"in_trash": False})).json()[
        "archived"
    ] is False


async def test_long_title_property_pagination(client: httpx.AsyncClient) -> None:
    page = await create_page(client, properties={"title": {"title": [text(str(i)) for i in range(40)]}})
    assert len(page["properties"]["title"]["title"]) == 25
    route = f"/v1/pages/{page['id']}/properties/title"
    first = (await client.get(route, params={"page_size": 30})).json()
    assert first["type"] == "property_item" and len(first["results"]) == 30
    assert first["results"][0]["title"]["plain_text"] == "0"
    rest = (await client.get(first["property_item"]["next_url"])).json()
    assert len(rest["results"]) == 10 and not rest["has_more"]
    assert (await client.get(f"/v1/pages/{page['id']}/properties/missing")).status_code == 404


async def test_hierarchy_seeded_document_content(client: httpx.AsyncClient) -> None:
    root = (await client.get(f"/v1/blocks/{sid('space', 'clients')}/children")).json()
    folder = next(b for b in root["results"] if b["type"] == "child_page")
    assert folder["child_page"]["title"] and folder["has_children"]
    doc = client.hub.world.documents[0]
    page_id = sid("document", doc.id)
    page = (await client.get("/v1/pages/" + page_id)).json()
    assert page["parent"]["type"] == "page_id"
    contents = (await client.get(f"/v1/blocks/{page_id}/children")).json()["results"]
    assert contents[0]["type"] == "heading_1"
    assert contents[1]["paragraph"]["rich_text"][0]["plain_text"] == doc.paragraphs[0]
    block = (await client.get("/v1/blocks/" + page_id)).json()
    assert block["type"] == "child_page" and block["id"] == page_id


async def test_versioned_database_objects_and_rows(client: httpx.AsyncClient) -> None:
    old = (await client.get("/v1/databases/" + CLIENTS)).json()
    assert "properties" in old and "data_sources" not in old
    assert old["properties"]["Name"]["type"] == "title"
    assert "database_id" in old["properties"]["Engagements"]["relation"]
    new = (await client.get("/v1/databases/" + CLIENTS, headers={"Notion-Version": NEW})).json()
    assert "properties" not in new and len(new["data_sources"]) == 1
    source_id = new["data_sources"][0]["id"]
    source = (await client.get("/v1/data_sources/" + source_id, headers={"Notion-Version": NEW})).json()
    assert source["parent"] == {"type": "database_id", "database_id": CLIENTS}
    old_row = (await client.post(f"/v1/databases/{CLIENTS}/query", json={"page_size": 1})).json()["results"][
        0
    ]
    new_row = (
        await client.post(
            f"/v1/data_sources/{source_id}/query", headers={"Notion-Version": NEW}, json={"page_size": 1}
        )
    ).json()["results"][0]
    assert old_row["id"] == new_row["id"]
    assert old_row["parent"] == {"type": "database_id", "database_id": CLIENTS}
    assert new_row["parent"]["data_source_id"] == source_id
    item = (
        await client.get(f"/v1/pages/{old_row['id']}/properties/{old['properties']['Annual Revenue']['id']}")
    ).json()
    assert item["object"] == "property_item" and isinstance(item["number"], int)
    assert (
        await client.post(f"/v1/databases/{CLIENTS}/query", headers={"Notion-Version": NEW}, json={})
    ).json()["code"] == "invalid_request_url"


async def test_oauth_code_refresh_introspection_and_revoke(client: httpx.AsyncClient) -> None:
    params = {
        "client_id": "notion-client",
        "redirect_uri": "https://consumer.example/callback",
        "response_type": "code",
        "owner": "user",
        "state": "state with spaces",
        "login_hint": "p008",
        "shared_page_ids": KNOWLEDGE,
    }
    response = await client.get("/v1/oauth/authorize", params=params)
    assert response.status_code == 302
    callback = parse_qs(urlparse(response.headers["location"]).query)
    assert callback["state"] == [params["state"]]
    payload = {
        "grant_type": "authorization_code",
        "code": callback["code"][0],
        "redirect_uri": params["redirect_uri"],
    }
    auth = httpx.BasicAuth("notion-client", "notion-secret")
    wrong = await client.post(
        "/v1/oauth/token", auth=auth, json={**payload, "redirect_uri": "https://other.example"}
    )
    assert wrong.status_code == 400 and wrong.json()["code"] == "invalid_grant"
    token = (await client.post("/v1/oauth/token", auth=auth, json=payload)).json()
    assert token["access_token"].startswith("ntn_") and token["refresh_token"]
    assert token["workspace_name"] == "Halcyon Advisory Group"
    assert token["owner"]["user"]["person"]["email"] == "ryan.murphy@halcyonadvisory.example"
    assert token["duplicated_template_id"] is None
    assert (await client.post("/v1/oauth/token", auth=auth, json=payload)).status_code == 400
    headers = {"Authorization": "Bearer " + token["access_token"]}
    assert (await client.get("/v1/users/me", headers=headers)).json()["bot"]["owner"]["type"] == "user"
    assert (await client.get("/v1/databases/" + CLIENTS, headers=headers)).status_code == 404
    assert (await client.get("/v1/pages/" + KNOWLEDGE, headers=headers)).status_code == 200
    info = (
        await client.post("/v1/oauth/introspect", auth=auth, json={"token": token["access_token"]})
    ).json()
    assert info["active"] and info["iat"] and "read_content" in info["scope"]
    refreshed = (
        await client.post(
            "/v1/oauth/token",
            auth=auth,
            json={"grant_type": "refresh_token", "refresh_token": token["refresh_token"]},
        )
    ).json()
    assert refreshed["access_token"] != token["access_token"]
    assert (await client.get("/v1/users/me", headers=headers)).status_code == 401
    revoked = await client.post("/v1/oauth/revoke", auth=auth, json={"token": refreshed["access_token"]})
    assert revoked.json() == {}
    assert not (
        await client.post("/v1/oauth/introspect", auth=auth, json={"token": refreshed["access_token"]})
    ).json()["active"]
    assert (
        await client.post(
            "/v1/oauth/token",
            auth=auth,
            json={"grant_type": "refresh_token", "refresh_token": refreshed["refresh_token"]},
        )
    ).status_code == 400


async def test_oauth_picker_auth_validation_and_expired_tokens(client: httpx.AsyncClient) -> None:
    params = {
        "client_id": "notion-client",
        "redirect_uri": "https://consumer.example/cb",
        "response_type": "code",
    }
    picker = await client.get("/v1/oauth/authorize", params=params)
    assert picker.status_code == 200 and "maya.chen" in picker.text
    assert (
        await client.get("/v1/oauth/authorize", params={**params, "login_hint": "unknown"})
    ).status_code == 400
    assert (
        await client.get(
            "/v1/oauth/authorize", params={**params, "login_hint": "p008", "shared_page_ids": PEOPLE}
        )
    ).status_code == 403
    assert (
        await client.post("/v1/oauth/token", json={"grant_type": "authorization_code", "code": "bad"})
    ).status_code == 401
    expired = await delegated(client, "p018", expires_in=-1)
    assert (await client.get("/v1/users/me", headers=expired)).status_code == 401


async def test_oauth_tokens_are_redacted_from_request_journal(client: httpx.AsyncClient) -> None:
    await client.post(
        "/v1/oauth/introspect",
        auth=httpx.BasicAuth("notion-client", "notion-secret"),
        json={"token": INTERNAL_TOKEN},
    )
    journal = (await client.get("/_emulator/requests", params={"path": "/v1/oauth/introspect"})).json()
    assert journal["requests"][0]["body"] == {"token": "[REDACTED]"}
    assert INTERNAL_TOKEN not in str(journal)


async def test_malformed_json_unknown_routes_faults_and_reset(client: httpx.AsyncClient) -> None:
    bad = await client.post("/v1/search", content=b"{", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_json"
    unknown = await client.get("/v1/unknown")
    assert unknown.status_code == 404 and unknown.json()["object"] == "error"
    await client.post("/_emulator/faults", json={"path": "/v1/search", "status": 429})
    throttled = await client.post("/v1/search", json={})
    assert throttled.status_code == 429 and throttled.headers["retry-after"] == "2"
    assert throttled.json()["code"] == "rate_limited"
    page = await create_page(client)
    await client.post("/_emulator/reset")
    assert (await client.get("/v1/pages/" + page["id"])).status_code == 404
    assert (await client.get("/v1/users/me")).status_code == 200


@pytest.mark.parametrize("mode", ["prefix", "port"])
async def test_urls_in_routing_modes(make_hub: Any, mode: str) -> None:
    hub = make_hub("notion")
    prefix = "/notion" if mode == "prefix" else ""
    transport = httpx.ASGITransport(app=hub.gateway if prefix else hub.apps["notion"])
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://localhost:44115" + prefix,
        headers={"Authorization": "Bearer " + INTERNAL_TOKEN, "Notion-Version": OLD},
    ) as client:
        page = (await client.get("v1/pages/" + KNOWLEDGE)).json()
        assert page["url"].startswith("http://localhost:44115" + prefix + "/")
        pdf = next(
            d for d in hub.world.documents if d.kind == "pdf" and d.space_id == "clients" and not d.restricted
        )
        blocks = (await client.get(f"v1/blocks/{sid('document', pdf.id)}/children")).json()["results"]
        url = blocks[0]["pdf"]["file"]["url"]
        assert url.startswith("http://localhost:44115" + prefix + "/secure/")
        assert (await client.get(url)).content.startswith(b"%PDF")


def test_deterministic_seed_and_safe_hosts() -> None:
    a = EmulatorHub(providers=["notion"], anchor="2026-10-16")
    b = EmulatorHub(providers=["notion"], anchor="2026-10-16")
    assert a.context("notion").store["pages"].ids() == b.context("notion").store["pages"].ids()
    assert a.context("notion").store["pages"].values() == b.context("notion").store["pages"].values()
    assert a.provider_for_host("file.notion.so") == "notion"
    assert a.provider_for_host("prod-files-secure.s3.us-west-2.amazonaws.com") is None
    assert not a.context("notion").store.blobs._blobs
