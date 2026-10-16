from __future__ import annotations

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint

from .test_confluence import DOCUMENT, collect, create_page


async def test_v1_expansions_last_updated_and_restrictions(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.get(
        "/wiki/rest/api/content/" + DOCUMENT,
        headers=auth,
        params={
            "expand": "body.storage,version,ancestors,space,restrictions.read.restrictions.user,history.lastUpdated"
        },
    )
    assert response.status_code == 200
    page = response.json()
    assert (
        page["type"] == "page" and page["space"]["key"] == "CLIENTS" and isinstance(page["space"]["id"], int)
    )
    assert page["body"]["storage"]["representation"] == "storage"
    assert page["history"]["lastUpdated"]["when"] == page["version"]["when"]
    assert page["history"]["lastUpdated"]["by"]["accountId"] == page["version"]["by"]["accountId"]
    assert page["restrictions"]["read"]["restrictions"]["user"]["results"] == []
    assert len(page["ancestors"]) == 3 and "body" not in page["ancestors"][0]
    spaces = await collect(client, "/wiki/rest/api/space", auth, limit=1)
    assert {"KB", "CLIENTS"} <= {space["key"] for space in spaces}
    denied = await client.get("/wiki/rest/api/content/missing", headers=auth)
    assert denied.status_code == 404 and denied.json()["message"] == "No content found with id: missing"
    assert denied.json()["statusCode"] == 404 and denied.json()["reason"] == "Not Found"


async def test_cql_compound_dates_fulltext_labels_ancestor_and_creator(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    page = await create_page(client, auth, "Revenue contract reference")
    uid = (await client.get("/wiki/rest/api/user/current", headers=auth)).json()["accountId"]
    date = page["version"]["createdAt"][:10].replace("-", "/")
    cql = f'type=page AND space=KB AND lastmodified >= "{date}" AND text ~ "revenue" AND creator="{uid}"'
    found = await client.get(
        "/wiki/rest/api/content/search", headers=auth, params={"cql": cql, "expand": "body.storage,version"}
    )
    assert found.status_code == 200, found.text
    assert page["id"] in {p["id"] for p in found.json()["results"]}
    ancestry = (await client.get(f"/wiki/api/v2/pages/{page['id']}/ancestors", headers=auth)).json()[
        "results"
    ]
    for clause in (
        f"ancestor={ancestry[0]['id']} AND id={page['id']}",
        f"creator=currentUser() AND id={page['id']}",
        f'(space=KB OR space=TECH) AND NOT title="Not this title" AND id IN ({page["id"]})',
    ):
        result = await client.get("/wiki/rest/api/content/search", headers=auth, params={"cql": clause})
        assert result.status_code == 200 and [p["id"] for p in result.json()["results"]] == [page["id"]]
    labels = await client.get(
        "/wiki/rest/api/content/search",
        headers=auth,
        params={"cql": "type=page AND label=knowledge ORDER BY lastmodified DESC", "limit": 250},
    )
    assert labels.status_code == 200 and labels.json()["results"]
    assert all(p["type"] == "page" for p in labels.json()["results"])
    excerpts = await client.get(
        "/wiki/rest/api/search",
        headers=auth,
        params={"cql": 'space=KB AND text ~ "revenue"', "expand": "content.body.storage,content.version"},
    )
    item = next(r for r in excerpts.json()["results"] if r["content"]["id"] == page["id"])
    assert "@@@hl@@@Revenue@@@endhl@@@" in item["excerpt"] or "@@@hl@@@revenue@@@endhl@@@" in item["excerpt"]
    assert item["lastModified"] == page["version"]["createdAt"]
    assert item["content"]["body"]["storage"]["value"] and item["content"]["version"]["number"] == 1


async def test_cql_offset_pagination_retains_expression(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    pages = await collect(
        client,
        "/wiki/rest/api/content/search",
        auth,
        cql="type=page AND space=CLIENTS ORDER BY title ASC",
        limit=19,
    )
    assert len(pages) > 200 and len(pages) == len({p["id"] for p in pages})
    assert [p["title"].casefold() for p in pages] == sorted(p["title"].casefold() for p in pages)


@pytest.mark.parametrize(
    "cql",
    [
        "type=page garbage",
        "type=",
        "type=page AND",
        'text ~ "unterminated',
        "unknown=anything",
        "space=KB OR unsupported=anything",
        "lastmodified > nonsense",
        "creator=unsupported()",
        "(type=page",
        "type IN ()",
    ],
)
async def test_cql_invalid_or_unsupported_clauses_fail_closed(
    client: httpx.AsyncClient, auth: dict[str, str], cql: str
) -> None:
    response = await client.get("/wiki/rest/api/content/search", headers=auth, params={"cql": cql})
    assert response.status_code == 400 and response.json()["statusCode"] == 400


async def test_attachment_acl_is_enforced_on_metadata_listing_and_bytes(
    client: httpx.AsyncClient, auth: dict[str, str], hub: EmulatorHub
) -> None:
    attachment = hub.context("atlassian").store["attachments"].first(_world_id="d0252")
    allowed = bearer(await mint(client, "https://halcyonadvisory.atlassian.net", user="p025"))
    page = attachment["pageId"]
    metadata = await client.get("/wiki/api/v2/attachments/" + attachment["id"], headers=allowed)
    assert metadata.status_code == 200
    for url in (
        "/wiki/api/v2/attachments/" + attachment["id"],
        f"/wiki/api/v2/pages/{page}/attachments",
        metadata.json()["downloadLink"],
    ):
        assert (await client.get(url, headers=allowed)).status_code == 200
        assert (await client.get(url, headers=auth)).status_code == 404
