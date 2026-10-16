"""Unchanged httpx call sequences used by Notion connectors over the live gateway."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.sdk
BASE = "https://api.notion.com/v1"


def internal_token() -> str:
    response = httpx.get("https://api.notion.com/_emulator/info")
    response.raise_for_status()
    return response.json()["credentials"]["internal_token"]


@pytest.mark.parametrize("version", ["2022-06-28", "2025-09-03"])
def test_connector_search_database_query_and_recursive_ingestion(
    emulator_intercept: Any, version: str
) -> None:
    with httpx.Client(
        headers={"Authorization": "Bearer " + internal_token(), "Notion-Version": version}, timeout=30
    ) as client:
        me = client.get(BASE + "/users/me")
        me.raise_for_status()
        assert me.json()["type"] == "bot"
        database_type = "database" if version == "2022-06-28" else "data_source"
        results = client.post(
            BASE + "/search",
            json={"query": "Clients", "filter": {"property": "object", "value": database_type}},
        )
        results.raise_for_status()
        database = results.json()["results"][0]
        resource = "databases" if database_type == "database" else "data_sources"
        schema = client.get(f"{BASE}/{resource}/{database['id']}")
        schema.raise_for_status()
        assert schema.json()["properties"]["Name"]["type"] == "title"
        rows = []
        cursor = None
        while True:
            payload: dict[str, Any] = {
                "page_size": 7,
                "sorts": [{"property": "Name", "direction": "ascending"}],
            }
            if cursor:
                payload["start_cursor"] = cursor
            result = client.post(f"{BASE}/{resource}/{database['id']}/query", json=payload)
            result.raise_for_status()
            data = result.json()
            rows.extend(data["results"])
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
        assert len(rows) == 24
        names = ["".join(t["plain_text"] for t in row["properties"]["Name"]["title"]) for row in rows]
        assert "Northwind Logistics" in names
        root = client.post(
            BASE + "/search",
            json={"query": "Knowledge Base", "filter": {"property": "object", "value": "page"}},
        ).json()["results"][0]
        queue = [root["id"]]
        text_blocks = []
        while queue and len(text_blocks) < 8:
            parent = queue.pop(0)
            result = client.get(f"{BASE}/blocks/{parent}/children")
            result.raise_for_status()
            for block in result.json()["results"]:
                if block["has_children"]:
                    queue.append(block["id"])
                text_blocks.extend(t["plain_text"] for t in block[block["type"]].get("rich_text", []))
        assert text_blocks
        hidden = client.post(BASE + "/search", json={"query": "People & Culture"})
        assert hidden.json()["results"] == []


async def test_async_connector_page_blocks_comments_and_file_download(emulator_intercept: Any) -> None:
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + internal_token(), "Notion-Version": "2025-09-03"}, timeout=30
    ) as client:
        root = (
            await client.post(
                BASE + "/search",
                json={"query": "Knowledge Base", "filter": {"property": "object", "value": "page"}},
            )
        ).json()["results"][0]
        created = await client.post(
            BASE + "/pages",
            json={
                "parent": {"page_id": root["id"]},
                "properties": {"title": {"title": [{"text": {"content": "Connector-created page"}}]}},
                "children": [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"text": {"content": "Connector content"}}]},
                    }
                ],
            },
        )
        created.raise_for_status()
        page_id = created.json()["id"]
        child = (await client.get(f"{BASE}/blocks/{page_id}/children")).json()["results"][0]
        assert child["paragraph"]["rich_text"][0]["plain_text"] == "Connector content"
        upload = await client.post(BASE + "/file_uploads", json={"filename": "connector.txt"})
        upload.raise_for_status()
        sent = await client.post(
            upload.json()["upload_url"], files={"file": ("connector.txt", b"Connector fixture", "text/plain")}
        )
        sent.raise_for_status()
        appended = await client.patch(
            f"{BASE}/blocks/{page_id}/children",
            json={
                "children": [
                    {
                        "type": "file",
                        "file": {
                            "type": "file_upload",
                            "file_upload": {"id": upload.json()["id"]},
                            "name": "connector.txt",
                        },
                    }
                ]
            },
        )
        appended.raise_for_status()
        file_url = appended.json()["results"][0]["file"]["file"]["url"]
        assert file_url.startswith("https://file.notion.so/")
        assert (await client.get(file_url)).content == b"Connector fixture"
        comment = await client.post(
            BASE + "/comments",
            json={"parent": {"page_id": page_id}, "rich_text": [{"text": {"content": "Ingested"}}]},
        )
        comment.raise_for_status()
        comments = (await client.get(BASE + "/comments", params={"block_id": page_id})).json()["results"]
        assert comments[0]["id"] == comment.json()["id"]
        archived = await client.patch(BASE + "/pages/" + page_id, json={"in_trash": True})
        archived.raise_for_status()
        assert archived.json()["archived"]


def test_connector_versioned_shapes_errors_and_capabilities(emulator_intercept: Any) -> None:
    with httpx.Client(
        headers={"Authorization": "Bearer " + internal_token(), "Notion-Version": "2025-09-03"}
    ) as client:
        response = client.post(
            BASE + "/search",
            json={"query": "Clients", "filter": {"property": "object", "value": "data_source"}},
        )
        response.raise_for_status()
        assert response.json()["type"] == "page_or_data_source"
        assert response.headers["x-request-id"]
        source = response.json()["results"][0]
        queried = client.post(f"{BASE}/data_sources/{source['id']}/query", json={"page_size": 100})
        queried.raise_for_status()
        assert queried.json()["type"] == "page_or_data_source"
        assert queried.json()["next_cursor"] is None and not queried.json()["has_more"]
        bad = client.post(BASE + "/search", json={"filter": False})
        assert bad.status_code == 400 and bad.json()["code"] == "validation_error"
        assert bad.headers["x-request-id"] == bad.json()["request_id"]
        limited = client.post("https://api.notion.com/_emulator/tokens", json={"scopes": []}).json()
        denied = client.post(
            BASE + "/search", headers={"Authorization": "Bearer " + limited["access_token"]}, json={}
        )
        assert denied.status_code == 403 and denied.json()["code"] == "restricted_resource"


def test_connector_oauth_rotation_and_client_binding(emulator_intercept: Any) -> None:
    from urllib.parse import parse_qs, urlparse

    with httpx.Client(headers={"Notion-Version": "2022-06-28"}) as client:
        authorize = client.get(
            BASE + "/oauth/authorize",
            params={
                "client_id": "notion-client",
                "redirect_uri": "https://consumer.example/callback",
                "response_type": "code",
                "owner": "user",
                "login_hint": "maya.chen@halcyonadvisory.example",
            },
        )
        code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
        credentials = httpx.BasicAuth("notion-client", "notion-secret")
        wrong = client.post(
            BASE + "/oauth/token",
            auth=httpx.BasicAuth("other-client", "other-secret"),
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://consumer.example/callback",
            },
        )
        assert wrong.status_code == 400
        result = client.post(
            BASE + "/oauth/token",
            auth=credentials,
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://consumer.example/callback",
            },
        )
        result.raise_for_status()
        pair = result.json()
        refreshed = client.post(
            BASE + "/oauth/token",
            auth=credentials,
            json={"grant_type": "refresh_token", "refresh_token": pair["refresh_token"]},
        )
        refreshed.raise_for_status()
        active = refreshed.json()
        assert (
            client.get(
                BASE + "/users/me", headers={"Authorization": "Bearer " + pair["access_token"]}
            ).status_code
            == 401
        )
        assert (
            client.post(
                BASE + "/oauth/token",
                auth=credentials,
                json={"grant_type": "refresh_token", "refresh_token": pair["refresh_token"]},
            ).status_code
            == 400
        )
        client.post(
            BASE + "/oauth/revoke", auth=credentials, json={"token": active["access_token"]}
        ).raise_for_status()
        assert (
            client.post(
                BASE + "/oauth/introspect", auth=credentials, json={"token": active["access_token"]}
            ).json()["active"]
            is False
        )
