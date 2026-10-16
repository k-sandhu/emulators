from __future__ import annotations

import io
import json
import zipfile
from typing import Any
from urllib.parse import urljoin

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint

SITE = "https://halcyonadvisory.atlassian.net"
V2 = "/wiki/api/v2"
KB = "38039918718755833"
CLIENTS = "68980425353704209"
DOCUMENT = "22006646768537502"


async def collect(
    client: httpx.AsyncClient, path: str, headers: dict[str, str], **params: Any
) -> list[dict[str, Any]]:
    result = []
    url = SITE + path
    while url:
        response = await client.get(url, headers=headers, params=params or None)
        response.raise_for_status()
        body = response.json()
        result.extend(body["results"])
        url = urljoin(str(response.url), body["_links"]["next"]) if body.get("_links", {}).get("next") else ""
        params = {}
    return result


async def create_page(
    client: httpx.AsyncClient, auth: dict[str, str], title: str = "Connector integration test", **fields: Any
) -> dict[str, Any]:
    response = await client.post(
        V2 + "/pages",
        headers=auth,
        json={
            "spaceId": KB,
            "title": title,
            "body": {"representation": "storage", "value": "<p>Revenue reconciliation &amp; controls.</p>"},
            **fields,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_space_keys_permissions_properties_and_personal_visibility(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    spaces = await collect(client, V2 + "/spaces", auth, limit=2)
    keys = {s["key"] for s in spaces}
    assert {"KB", "CLIENTS", "TECH", "PEOPLE", "GROWTH", "PARTNERS", "FINANCE"} <= keys
    assert len(spaces) == 8  # Seven team spaces and the caller's own personal space.
    assert sum(s["type"] == "personal" for s in spaces) == 1
    filtered = await client.get(
        V2 + "/spaces", headers=auth, params={"keys": "KB,PEOPLE", "description-format": "view"}
    )
    assert {s["key"] for s in filtered.json()["results"]} == {"KB", "PEOPLE"}
    assert filtered.json()["results"][0]["description"]["view"]["representation"] == "view"
    detail = await client.get(
        V2 + "/spaces/" + KB, headers=auth, params={"include-permissions": "true", "include-icon": "true"}
    )
    assert detail.status_code == 200 and detail.json()["homepageId"]
    permissions = await collect(client, V2 + f"/spaces/{KB}/permissions", auth, limit=2)
    assert any(
        p["operation"] == {"key": "read", "targetType": "space"} and p["principal"]["type"] == "group"
        for p in permissions
    )
    assert {p["operation"]["key"] for p in permissions} >= {"create", "read", "update", "delete"}
    properties = (await client.get(V2 + f"/spaces/{KB}/properties", headers=auth)).json()["results"]
    assert properties[0]["key"] == "halcyon.area" and properties[0]["value"] == "knowledge"
    assert (
        (await client.get(V2 + "/spaces", headers=auth, params={"type": "personal"}))
        .json()["results"][0]["key"]
        .startswith("~712020")
    )
    user = bearer(await mint(client, SITE, user="p002"))
    assert not (await client.get(V2 + "/spaces", headers=user, params={"keys": "PARTNERS"})).json()["results"]


async def test_page_projection_body_formats_versions_and_labels(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.get(
        V2 + "/pages/" + DOCUMENT,
        headers=auth,
        params={"body-format": "storage", "include-labels": "true", "include-versions": "true"},
    )
    assert response.status_code == 200
    page = response.json()
    assert page["spaceId"] == CLIENTS and page["parentType"] == "page"
    assert page["title"].startswith("Engagement Letter") and page["authorId"].startswith("712020:")
    assert (
        page["body"]["storage"]["value"].startswith("<h2>")
        and "Northwind Logistics" in page["body"]["storage"]["value"]
    )
    assert len(page["versions"]["results"]) == 6 and page["version"]["number"] == 6
    assert {label["name"] for label in page["labels"]["results"]} == {"clients", "client-engagement"}
    assert all(not key.startswith("_") or key == "_links" for key in page)
    adf = (
        await client.get(V2 + "/pages/" + DOCUMENT, headers=auth, params={"body-format": "atlas_doc_format"})
    ).json()
    assert json.loads(adf["body"]["atlas_doc_format"]["value"])["type"] == "doc"
    view = (await client.get(V2 + "/pages/" + DOCUMENT, headers=auth, params={"body-format": "view"})).json()
    assert view["body"]["view"]["representation"] == "view"
    history = await collect(
        client, V2 + f"/pages/{DOCUMENT}/versions", auth, limit=2, **{"body-format": "storage"}
    )
    assert [h["number"] for h in history] == [1, 2, 3, 4, 5, 6]
    first = await client.get(
        V2 + "/pages/" + DOCUMENT, headers=auth, params={"version": 1, "include-version": "false"}
    )
    assert first.status_code == 200 and "version" not in first.json()
    label_id = page["labels"]["results"][0]["id"]
    labels = await client.get(V2 + f"/pages/{DOCUMENT}/labels", headers=auth)
    assert labels.json()["results"] == page["labels"]["results"]
    labeled = await client.get(V2 + f"/labels/{label_id}/pages", headers=auth)
    assert DOCUMENT in {p["id"] for p in labeled.json()["results"]} or labeled.json()["_links"].get("next")


async def test_page_paging_filters_sort_and_complete_tree(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.get(
        V2 + "/pages",
        headers=auth,
        params={"space-id": CLIENTS, "limit": 2, "sort": "-modified-date", "body-format": "storage"},
    )
    assert response.status_code == 200 and len(response.json()["results"]) == 2
    assert response.headers["link"].endswith('; rel="next"')
    pages = await collect(
        client, V2 + "/pages", auth, **{"space-id": CLIENTS, "limit": 250, "sort": "-modified-date"}
    )
    assert len({p["id"] for p in pages}) == len(pages)
    assert [p["version"]["createdAt"] for p in pages] == sorted(
        (p["version"]["createdAt"] for p in pages), reverse=True
    )
    roots = (
        await client.get(V2 + f"/spaces/{CLIENTS}/pages", headers=auth, params={"depth": "root"})
    ).json()["results"]
    assert len(roots) == 1 and "parentId" not in roots[0]
    root = roots[0]["id"]
    direct = await collect(client, V2 + f"/pages/{root}/children", auth, limit=2)
    assert len(direct) >= 20 and all("childPosition" in p and "body" not in p for p in direct)
    descendants = await collect(client, V2 + f"/pages/{root}/descendants", auth, depth=10, limit=25)
    assert {p["id"] for p in descendants} == {p["id"] for p in pages if p["id"] != root}
    assert all(p["type"] == "page" and p["depth"] >= 1 for p in descendants)
    ancestry = (await client.get(V2 + f"/pages/{DOCUMENT}/ancestors", headers=auth)).json()["results"]
    assert ancestry[0] == {"id": root, "type": "page"} and len(ancestry) == 3
    children = (await client.get(V2 + f"/pages/{ancestry[-1]['id']}/direct-children", headers=auth)).json()[
        "results"
    ]
    assert DOCUMENT in {p["id"] for p in children} and all(p["type"] == "page" for p in children)
    exact = await client.get(V2 + "/pages", headers=auth, params={"title": "Client Engagements", "id": root})
    assert [p["id"] for p in exact.json()["results"]] == [root]


async def test_versioned_mutations_incremental_cql_and_trash(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    page = await create_page(client, auth)
    page_id = page["id"]
    assert page["version"]["number"] == 1
    duplicate = await client.post(V2 + "/pages", headers=auth, json={"spaceId": KB, "title": page["title"]})
    assert duplicate.status_code == 400
    payload = {
        "id": page_id,
        "status": "current",
        "title": "Updated revenue guide",
        "version": {"number": 2, "message": "Revenue update"},
        "body": {"representation": "storage", "value": "<p>Revenue controls after migration.</p>"},
    }
    updated = await client.put(V2 + "/pages/" + page_id, headers=auth, json=payload)
    assert updated.status_code == 200 and updated.json()["version"]["number"] == 2
    assert (await client.put(V2 + "/pages/" + page_id, headers=auth, json=payload)).status_code == 409
    old = await client.get(
        V2 + "/pages/" + page_id, headers=auth, params={"version": 1, "body-format": "storage"}
    )
    assert old.json()["title"] == page["title"] and "reconciliation" in old.json()["body"]["storage"]["value"]
    last_modified = updated.json()["version"]["createdAt"][:10]
    search = await client.get(
        "/wiki/rest/api/content/search",
        headers=auth,
        params={"cql": f'type=page AND space=KB AND lastmodified >= "{last_modified}" AND text ~ "revenue"'},
    )
    assert search.status_code == 200 and page_id in {p["id"] for p in search.json()["results"]}
    assert (await client.delete(V2 + "/pages/" + page_id, headers=auth)).status_code == 204
    assert (await client.get(V2 + "/pages/" + page_id, headers=auth)).status_code == 404
    assert (await client.get(V2 + "/pages/" + page_id, headers=auth, params={"status": "trashed"})).json()[
        "status"
    ] == "trashed"
    assert (await client.delete(V2 + "/pages/" + page_id + "?purge=true", headers=auth)).status_code == 204
    assert (
        await client.get(V2 + "/pages/" + page_id, headers=auth, params={"status": "trashed"})
    ).status_code == 404


async def test_adf_draft_and_hierarchy_cycle_validation(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    page = await create_page(
        client,
        auth,
        "Private draft",
        status="draft",
        body={
            "representation": "atlas_doc_format",
            "value": json.dumps(
                {
                    "version": 1,
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Draft guidance"}]}
                    ],
                }
            ),
        },
    )
    page_id = page["id"]
    assert (await client.get(V2 + "/pages/" + page_id, headers=auth)).status_code == 404
    assert (
        await client.get(V2 + "/pages/" + page_id, headers=auth, params={"get-draft": "true"})
    ).status_code == 200
    other = bearer(await mint(client, SITE, user="p018"))
    assert (
        await client.get(V2 + "/pages/" + page_id, headers=other, params={"get-draft": "true"})
    ).status_code == 404
    parent = await create_page(client, auth, "Parent guide")
    child = await create_page(client, auth, "Child guide", parentId=parent["id"])
    invalid = await client.put(
        V2 + "/pages/" + parent["id"],
        headers=auth,
        json={
            "id": parent["id"],
            "title": parent["title"],
            "status": "current",
            "parentId": child["id"],
            "body": {"representation": "storage", "value": "<p>test</p>"},
            "version": {"number": 2},
        },
    )
    assert invalid.status_code == 400
    wrong_space = await client.post(
        V2 + "/pages",
        headers=auth,
        json={"spaceId": CLIENTS, "parentId": parent["id"], "title": "Bad parent"},
    )
    assert wrong_space.status_code == 400
    assert (await client.delete(V2 + "/pages/" + page_id + "?draft=true", headers=auth)).status_code == 204


async def test_restrictions_operations_and_ancestor_filtering(
    client: httpx.AsyncClient, auth: dict[str, str], hub: EmulatorHub
) -> None:
    restricted = hub.context("atlassian").store["pages"].first(_world_id="d0260")
    page_id = restricted["id"]
    hr = bearer(await mint(client, SITE, user="p031"))
    denied = bearer(await mint(client, SITE, user="p018"))
    assert (await client.get(V2 + "/pages/" + page_id, headers=hr)).status_code == 200
    assert (await client.get(V2 + "/pages/" + page_id, headers=denied)).status_code == 404
    children = await client.get(V2 + f"/pages/{restricted['parentId']}/children", headers=denied)
    assert page_id not in {p["id"] for p in children.json()["results"]}
    results = await client.get(
        "/wiki/rest/api/content/search", headers=denied, params={"cql": f"id={page_id}"}
    )
    assert results.json()["results"] == []
    restrictions = (
        await client.get(f"/wiki/rest/api/content/{page_id}/restriction/byOperation", headers=hr)
    ).json()
    assert len(restrictions["read"]["restrictions"]["user"]["results"]) == 3
    assert (await client.get(V2 + f"/pages/{page_id}/operations", headers=hr)).json()["operations"] == [
        {"operation": "read", "targetType": "page"},
        {"operation": "update", "targetType": "page"},
        {"operation": "delete", "targetType": "page"},
    ]
    public = await create_page(client, auth, "Staff guidance")
    ops = (await client.get(V2 + f"/pages/{public['id']}/operations", headers=denied)).json()["operations"]
    # Maya is a partner, so choose a staff reader for the no-edit case.
    reader = bearer(await mint(client, SITE, user="p039"))
    assert (await client.get(V2 + f"/pages/{public['id']}/operations", headers=reader)).json()[
        "operations"
    ] == [{"operation": "read", "targetType": "page"}]
    assert ops
    assert (await client.delete(V2 + "/pages/" + public["id"], headers=reader)).status_code == 403
    private = await create_page(client, auth, "Ancestor restriction")
    record = hub.context("atlassian").store["pages"].get(private["id"])
    await client.put("/_emulator/state/pages", json={**record, "_read": ["p001"]})
    child = await create_page(client, auth, "Inherited restriction", parentId=private["id"])
    assert (await client.get(V2 + "/pages/" + child["id"], headers=denied)).status_code == 404
    search = await client.get(
        "/wiki/rest/api/content/search", headers=denied, params={"cql": f"ancestor={private['id']}"}
    )
    assert search.json()["results"] == []


@pytest.mark.parametrize("resource", ["pages", "spaces", "blogposts"])
async def test_content_property_crud_and_optimistic_version(
    client: httpx.AsyncClient, auth: dict[str, str], resource: str
) -> None:
    parent = (await create_page(client, auth))["id"] if resource == "pages" else CLIENTS
    if resource == "blogposts":
        parent = (
            await client.post(
                V2 + "/blogposts", headers=auth, json={"spaceId": KB, "title": "Property test blog"}
            )
        ).json()["id"]
    path = V2 + f"/{resource}/{parent}/properties"
    response = await client.post(
        path, headers=auth, json={"key": "connector.checkpoint", "value": {"cursor": 7}}
    )
    assert response.status_code == (201 if resource == "spaces" else 200)
    prop = response.json()
    assert prop["version"]["number"] == 1
    assert (await client.get(path, headers=auth, params={"key": "connector.checkpoint"})).json()["results"][
        0
    ]["id"] == prop["id"]
    assert (await client.get(path + "/" + prop["id"], headers=auth)).json()["value"] == {"cursor": 7}
    payload = {"value": {"cursor": 8}, "version": {"number": 2}}
    assert (await client.put(path + "/" + prop["id"], headers=auth, json=payload)).json()["version"][
        "number"
    ] == 2
    assert (await client.put(path + "/" + prop["id"], headers=auth, json=payload)).status_code == 409
    assert (
        await client.post(path, headers=auth, json={"key": "connector.checkpoint", "value": None})
    ).status_code == 400
    assert (await client.delete(path + "/" + prop["id"], headers=auth)).status_code == 204
    assert (await client.get(path + "/" + prop["id"], headers=auth)).status_code == 404


async def test_comments_users_blogposts_and_all_attachment_formats(
    client: httpx.AsyncClient, auth: dict[str, str], hub: EmulatorHub
) -> None:
    ctx = hub.context("atlassian")
    comment = await client.get(
        V2 + f"/pages/{DOCUMENT}/footer-comments", headers=auth, params={"body-format": "storage"}
    )
    assert comment.status_code == 200 and comment.json()["results"][0]["version"]["authorId"].startswith(
        "712020:"
    )
    account_id = (await client.get("/wiki/rest/api/user/current", headers=auth)).json()["accountId"]
    bulk = await client.post(V2 + "/users-bulk", headers=auth, json={"accountIds": [account_id, "missing"]})
    assert (
        bulk.status_code == 200
        and len(bulk.json()["results"]) == 1
        and bulk.json()["results"][0]["email"].endswith(".example")
    )
    assert (await client.post(V2 + "/users-bulk", headers=auth, json={"accountIds": []})).status_code == 400
    blogs = (await client.get(V2 + "/blogposts", headers=auth)).json()["results"]
    assert blogs
    assert (await client.get(V2 + "/blogposts/" + blogs[0]["id"], headers=auth)).status_code == 200
    created = await client.post(
        V2 + "/blogposts", headers=auth, json={"spaceId": KB, "title": "Connector release update"}
    )
    assert created.status_code == 200
    blog_id = created.json()["id"]
    changed = await client.put(
        V2 + "/blogposts/" + blog_id,
        headers=auth,
        json={
            "id": blog_id,
            "title": "Updated release notes",
            "status": "current",
            "version": {"number": 2},
            "body": {"representation": "storage", "value": "<p>New release.</p>"},
        },
    )
    assert changed.status_code == 200 and changed.json()["version"]["number"] == 2
    assert (await client.delete(V2 + "/blogposts/" + blog_id, headers=auth)).status_code == 204
    assert (await client.get(V2 + "/blogposts/" + blog_id, headers=auth)).status_code == 404
    for kind, signature in (("xlsx", b"PK"), ("pptx", b"PK"), ("pdf", b"%PDF")):
        doc = next(d for d in hub.world.documents if d.space_id == "clients" and d.kind == kind)
        attachment = ctx.store["attachments"].first(_world_id=doc.id)
        assert attachment["_blob"] is None
        metadata = await client.get(V2 + "/attachments/" + attachment["id"], headers=auth)
        assert metadata.status_code == 200, metadata.text
        file = await client.get(metadata.json()["downloadLink"], headers=auth)
        assert file.status_code == 200 and file.content.startswith(signature)
        assert metadata.json()["fileSize"] == len(file.content)
        assert file.content == (await client.get(metadata.json()["downloadLink"], headers=auth)).content
        if kind != "pdf":
            with zipfile.ZipFile(io.BytesIO(file.content)) as archive:
                assert "[Content_Types].xml" in archive.namelist()
        listing = await client.get(V2 + f"/pages/{attachment['pageId']}/attachments", headers=auth)
        assert listing.json()["results"][0]["downloadLink"] == metadata.json()["downloadLink"]
        old = await client.get(
            f"/wiki/rest/api/content/{attachment['pageId']}/child/attachment", headers=auth
        )
        assert old.json()["results"][0]["type"] == "attachment"


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 251},
        {"limit": "bad"},
        {"cursor": "bad"},
        {"sort": "unknown"},
        {"status": "unknown"},
    ],
)
async def test_invalid_list_options_return_vendor_errors(
    client: httpx.AsyncClient, auth: dict[str, str], params: dict[str, Any]
) -> None:
    response = await client.get(V2 + "/pages", headers=auth, params=params)
    assert response.status_code == 400 and response.json()["errors"][0]["code"] == "BAD_REQUEST"


async def test_prefix_links_and_gateway_downloads(hub: EmulatorHub) -> None:
    from emulators.providers.atlassian.common import CLOUD_ID

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hub.gateway), base_url="http://localhost:44080"
    ) as client:
        auth = bearer(await mint(client, "http://localhost:44080/atlassian", user="p001"))
        response = await client.get("/atlassian/wiki/api/v2/pages?limit=1", headers=auth)
        assert response.json()["_links"]["next"].startswith(
            "http://localhost:44080/atlassian/wiki/api/v2/pages?"
        )
        assert (await client.get(response.json()["_links"]["next"], headers=auth)).status_code == 200
        attachment = hub.context("atlassian").store["attachments"].values()[0]
        metadata = await client.get("/atlassian/wiki/api/v2/attachments/" + attachment["id"], headers=auth)
        assert metadata.json()["downloadLink"].startswith("http://localhost:44080/atlassian/wiki/download/")
        assert (await client.get(metadata.json()["downloadLink"], headers=auth)).status_code == 200
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        api = f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}"
        metadata = await client.get(api + "/wiki/api/v2/attachments/" + attachment["id"], headers=auth)
        assert metadata.json()["downloadLink"].startswith(api)
        assert (await client.get(metadata.json()["downloadLink"], headers=auth)).status_code == 200
