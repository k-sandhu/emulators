"""Adversarial cases checked against the Atlassian Cloud REST schemas."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from emulators.core.clock import parse_instant
from emulators.core.hub import EmulatorHub
from emulators.core.ids import b64url
from emulators.providers.atlassian.common import CLOUD_ID, numeric_id
from emulators.testing.helpers import bearer, mint

from .test_confluence import CLIENTS, DOCUMENT, KB, V2, create_page

SITE = "https://halcyonadvisory.atlassian.net"
AUTH = "https://auth.atlassian.com"
BODY = {"representation": "storage", "value": "<p>Audit guidance — 測試.</p>"}


def update_payload(page: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {
        "id": page["id"],
        "title": page["title"],
        "status": "current",
        "body": BODY,
        "version": {"number": page["version"]["number"] + 1},
        **overrides,
    }


@pytest.mark.parametrize("resource", ["pages", "blogposts"])
async def test_private_published_content_does_not_escape_any_read_surface(
    client: httpx.AsyncClient, auth: dict[str, str], resource: str
) -> None:
    created = await client.post(
        f"{V2}/{resource}?private=true",
        headers=auth,
        json={"spaceId": KB, "title": "Private audit guidance", "body": BODY},
    )
    assert created.status_code == 200
    page = created.json()
    path = f"{V2}/{resource}/{page['id']}"
    other = bearer(await mint(client, SITE, user="p018"))
    app = bearer(await mint(client, SITE, app_only=True))
    assert (await client.get(path, headers=auth)).status_code == 200
    for denied in (other, app):
        assert (await client.get(path, headers=denied)).status_code == 404
        assert (await client.get(f"{V2}/{resource}", params={"id": page["id"]}, headers=denied)).json()[
            "results"
        ] == []
        assert (
            await client.get(
                "/wiki/rest/api/content/search", params={"cql": f"id={page['id']}"}, headers=denied
            )
        ).json()["results"] == []
        assert (await client.get(f"/wiki/rest/api/content/{page['id']}", headers=denied)).status_code == 404
        assert (await client.put(path, headers=denied, json=update_payload(page))).status_code == 404
    if resource == "pages":
        child = await create_page(client, auth, "Private child", parentId=page["id"])
        assert (await client.get(f"{V2}/pages/{child['id']}", headers=other)).status_code == 404


@pytest.mark.parametrize("resource", ["pages", "blogposts"])
async def test_untitled_draft_update_version_and_restore_semantics(
    client: httpx.AsyncClient, auth: dict[str, str], resource: str
) -> None:
    draft = await client.post(f"{V2}/{resource}", headers=auth, json={"spaceId": KB, "status": "draft"})
    assert draft.status_code == 200 and draft.json()["version"]["number"] == 1
    page = draft.json()
    path = f"{V2}/{resource}/{page['id']}"
    changed = await client.put(
        path,
        headers=auth,
        json=update_payload(page, title="Draft review", status="draft", version={"number": 1}),
    )
    assert changed.status_code == 200 and changed.json()["version"]["number"] == 1
    published = await client.put(path, headers=auth, json=update_payload(changed.json()))
    assert published.status_code == 200 and published.json()["version"]["number"] == 2
    assert (await client.delete(path, headers=auth)).status_code == 204
    restored = await client.put(
        path, headers=auth, json=update_payload(published.json(), title="Must not replace restored text")
    )
    assert restored.status_code == 200
    assert restored.json()["title"] == "Draft review" and restored.json()["version"]["number"] == 2
    assert (
        await client.put(path, headers=auth, json=update_payload(restored.json(), status="archived"))
    ).status_code == 400


async def test_page_move_constraints_and_ownership_history(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    page = await create_page(client, auth, "Owned audit page")
    path = f"{V2}/pages/{page['id']}"
    assert page["lastOwnerId"] is None
    wrong_space = await client.put(
        path, headers=auth, json=update_payload(page, spaceId=CLIENTS, parentId=None)
    )
    assert wrong_space.status_code == 400
    conflicting_parent = await client.post(
        f"{V2}/pages?root-level=true",
        headers=auth,
        json={"spaceId": KB, "parentId": page["id"], "title": "Contradictory root"},
    )
    assert conflicting_parent.status_code == 400
    changed = await client.put(path, headers=auth, json=update_payload(page))
    assert changed.status_code == 200 and changed.json()["lastOwnerId"] is None
    owner = (await client.get("/rest/api/3/user/search?query=maya", headers=auth)).json()[0]["accountId"]
    transferred = await client.put(path, headers=auth, json=update_payload(changed.json(), ownerId=owner))
    assert transferred.status_code == 200 and transferred.json()["ownerId"] == owner
    assert transferred.json()["lastOwnerId"] == page["ownerId"]
    edited = await client.put(path, headers=auth, json=update_payload(transferred.json()))
    assert edited.json()["lastOwnerId"] == page["ownerId"]
    invalid_owner = await client.put(path, headers=auth, json=update_payload(edited.json(), ownerId="p018"))
    assert invalid_owner.status_code == 400


@pytest.mark.parametrize("parent", [False, 0, [], {}, ""])
async def test_empty_wrong_typed_parents_are_not_silently_replaced_by_homepage(
    client: httpx.AsyncClient, auth: dict[str, str], parent: Any
) -> None:
    response = await client.post(
        f"{V2}/pages", headers=auth, json={"spaceId": KB, "title": "Bad parent", "parentId": parent}
    )
    assert response.status_code == 400


async def test_blog_metadata_dates_and_page_title_namespace(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    title = "A page and a blog can share this title"
    created = await client.post(
        f"{V2}/blogposts",
        headers=auth,
        json={"spaceId": KB, "title": title, "createdAt": "2026-10-01T12:30:00.000Z"},
    )
    assert created.status_code == 200 and created.json()["createdAt"] == "2026-10-01T12:30:00.000Z"
    assert not set(created.json()) & {
        "ownerId",
        "lastOwnerId",
        "parentId",
        "parentType",
        "position",
        "subtype",
    }
    page = await create_page(client, auth, title)
    assert page["title"] == title
    invalid = await client.post(
        f"{V2}/blogposts", headers=auth, json={"spaceId": KB, "title": "Invalid date", "createdAt": []}
    )
    assert invalid.status_code == 400


async def test_subtype_filters_and_write_field_types(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    created = await client.post(
        f"{V2}/pages", headers=auth, json={"spaceId": KB, "title": "Audit live document", "subtype": "live"}
    )
    assert created.status_code == 200 and created.json()["subtype"] == "live"
    found = await client.get(
        f"{V2}/pages", headers=auth, params={"id": created.json()["id"], "subtype": "page"}
    )
    assert found.json()["results"] == []
    assert (await client.get(f"{V2}/pages", headers=auth, params={"subtype": "invalid"})).status_code == 400
    for fields in ({"spaceId": int(KB)}, {"title": "a" * 256}):
        response = await client.post(
            f"{V2}/pages", headers=auth, json={"spaceId": KB, "title": "Bad field", **fields}
        )
        assert response.status_code == 400
    page = await create_page(client, auth)
    for version in ({"number": 2, "message": 123}, {"number": 2, "minorEdit": "false"}):
        response = await client.put(
            f"{V2}/pages/{page['id']}", headers=auth, json=update_payload(page, version=version)
        )
        assert response.status_code == 400


@pytest.mark.parametrize("resource", ["pages", "blogposts"])
@pytest.mark.parametrize("nested", [123, "bad", [1], True])
async def test_malformed_nested_body_is_a_400(
    client: httpx.AsyncClient, auth: dict[str, str], resource: str, nested: Any
) -> None:
    response = await client.post(
        f"{V2}/{resource}",
        headers=auth,
        json={"spaceId": KB, "title": "Bad nested body", "body": {"storage": nested}},
    )
    assert response.status_code == 400 and response.json()["errors"][0]["code"] == "BAD_REQUEST"


async def test_space_properties_need_admin_and_use_space_property_wire_shape(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    path = f"{V2}/spaces/{KB}/properties"
    denied = await client.post(path, headers=auth, json={"key": "audit", "value": None})
    assert denied.status_code == 403  # Eleanor can edit KB pages but cannot administer its space.
    owner = bearer(await mint(client, SITE, user="p002"))
    created = await client.post(path, headers=owner, json={"key": "audit", "value": None})
    assert created.status_code == 201 and created.headers["location"].startswith(SITE + path)
    prop = created.json()
    assert prop["createdBy"] == prop["version"]["createdBy"] and "authorId" not in prop["version"]
    fetched = await client.get(created.headers["location"], headers=auth)
    assert fetched.json() == prop
    updated = await client.put(
        created.headers["location"],
        headers=owner,
        json={"key": "audit.renamed", "value": ["測試"], "version": {"number": 2, "message": "Reviewed"}},
    )
    assert updated.status_code == 200
    assert updated.json()["key"] == "audit.renamed" and updated.json()["version"]["message"] == "Reviewed"
    assert (
        updated.json()["createdBy"] == prop["createdBy"] and updated.json()["createdAt"] == prop["createdAt"]
    )
    assert (await client.delete(created.headers["location"], headers=auth)).status_code == 403


@pytest.mark.parametrize("version", [True, 2.0, "2", [], {}, None])
async def test_property_versions_are_integers_and_validation_is_atomic(
    client: httpx.AsyncClient, auth: dict[str, str], version: Any
) -> None:
    page = await create_page(client, auth)
    path = f"{V2}/pages/{page['id']}/properties"
    prop = (await client.post(path, headers=auth, json={"key": "audit", "value": 1})).json()
    response = await client.put(
        path + "/" + prop["id"], headers=auth, json={"value": 2, "version": {"number": version}}
    )
    assert response.status_code == 400
    assert (await client.get(path + "/" + prop["id"], headers=auth)).json()["value"] == 1


async def test_property_size_key_sort_and_optional_continuation(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    page = await create_page(client, auth)
    path = f"{V2}/pages/{page['id']}/properties"
    for payload in ({"key": "k" * 256, "value": 1}, {"key": "large", "value": "a" * 32769}):
        assert (await client.post(path, headers=auth, json=payload)).status_code == 400
    for i in reversed(range(51)):
        assert (
            await client.post(path, headers=auth, json={"key": f"audit.{i:02}", "value": i})
        ).status_code == 200
    included = (await client.get(f"{V2}/pages/{page['id']}?include-properties=true", headers=auth)).json()[
        "properties"
    ]
    assert included["meta"]["hasMore"] and len(included["results"]) == 50
    tail = await client.get(
        included["_links"]["self"], params={"cursor": included["meta"]["cursor"]}, headers=auth
    )
    assert tail.status_code == 200 and [p["key"] for p in tail.json()["results"]] == ["audit.50"]
    descending = await client.get(path, params={"sort": "-key", "limit": 1}, headers=auth)
    assert descending.json()["results"][0]["key"] == "audit.50"
    first = included["results"][0]
    duplicate = await client.put(
        path + "/" + first["id"], headers=auth, json={"key": "audit.50", "value": 0, "version": {"number": 2}}
    )
    assert duplicate.status_code == 400


async def test_renamed_property_survives_reuse_of_its_original_key(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    page = await create_page(client, auth)
    path = f"{V2}/pages/{page['id']}/properties"
    original = (await client.post(path, headers=auth, json={"key": "original", "value": 1})).json()
    renamed = await client.put(
        path + "/" + original["id"],
        headers=auth,
        json={"key": "renamed", "value": 2, "version": {"number": 2}},
    )
    assert renamed.status_code == 200
    replacement = await client.post(path, headers=auth, json={"key": "original", "value": 3})
    assert replacement.status_code == 200 and replacement.json()["id"] != original["id"]
    records = (await client.get(path, headers=auth)).json()["results"]
    assert {p["key"]: p["value"] for p in records} == {"renamed": 2, "original": 3}
    assert (await client.get(path + "/" + original["id"], headers=auth)).json()["value"] == 2


@pytest.mark.parametrize("resource", ["pages", "blogposts"])
async def test_status_query_enums_match_collection_and_single_content_contracts(
    client: httpx.AsyncClient, auth: dict[str, str], resource: str
) -> None:
    assert (await client.get(f"{V2}/{resource}?status=draft", headers=auth)).status_code == 400
    page_id = DOCUMENT if resource == "pages" else numeric_id("blog", "knowledge")
    assert (await client.get(f"{V2}/{resource}/{page_id}?status=invalid", headers=auth)).status_code == 400


@pytest.mark.parametrize("path", ["/wiki/rest/api/content/search", "/wiki/rest/api/search"])
async def test_v1_search_zero_limit_returns_empty_page_without_looping_next_link(
    client: httpx.AsyncClient, auth: dict[str, str], path: str
) -> None:
    response = await client.get(path, headers=auth, params={"cql": "type=page", "limit": 0})
    assert response.status_code == 200 and response.json()["results"] == []
    assert response.json()["limit"] == 0 and "next" not in response.json()["_links"]


@pytest.mark.parametrize(
    "path,params",
    [
        (f"{V2}/pages", {"title": "does-not-exist", "body-format": "bogus"}),
        (f"{V2}/spaces", {"keys": "does-not-exist", "description-format": "bogus"}),
    ],
)
async def test_invalid_format_is_rejected_even_for_empty_collections(
    client: httpx.AsyncClient, auth: dict[str, str], path: str, params: dict[str, str]
) -> None:
    assert (await client.get(path, headers=auth, params=params)).status_code == 400


@pytest.mark.parametrize(
    "path,query_name,expression",
    [
        ("/wiki/rest/api/content/search", "cql", 'type=page AND created > "999999999999999999d"'),
        ("/rest/api/3/search/jql", "jql", 'project=OPS AND updated > startOfDay("999999999999999d")'),
        ("/wiki/rest/api/content/search", "cql", " OR ".join(["type=page"] * 1100)),
        ("/rest/api/3/search/jql", "jql", " OR ".join(["project=OPS"] * 1100)),
        ("/rest/api/3/search/jql", "jql", "project=currentUser()"),
    ],
)
async def test_pathological_queries_return_vendor_errors(
    client: httpx.AsyncClient, auth: dict[str, str], path: str, query_name: str, expression: str
) -> None:
    response = await client.get(path, headers=auth, params={query_name: expression})
    assert response.status_code == 400
    assert "errorMessages" in response.json() if query_name == "jql" else "statusCode" in response.json()


async def test_jql_issue_keys_sort_numerically_and_cql_ids_compare_as_integers(
    client: httpx.AsyncClient, auth: dict[str, str], hub: EmulatorHub
) -> None:
    result = await client.get(
        "/rest/api/3/search/jql",
        params={"jql": "project=OPS ORDER BY key", "fields": "summary"},
        headers=auth,
    )
    assert [r["key"] for r in result.json()["issues"]] == [f"OPS-{i}" for i in range(1, 16)]
    ids = [
        p["id"]
        for p in hub.context("atlassian").store["pages"]
        if p["spaceId"] == KB and p["status"] == "current"
    ]
    threshold = sorted(ids, key=int)[len(ids) // 2]
    response = await client.get(
        "/wiki/rest/api/content/search",
        params={"cql": f"space=KB AND id > {threshold} ORDER BY id", "limit": 250},
        headers=auth,
    )
    assert [r["id"] for r in response.json()["results"]] == sorted(
        (i for i in ids if int(i) > int(threshold)), key=int
    )


async def test_search_cursors_bind_caller_preserve_query_and_stop_at_last_page(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    for path, params in (
        (f"{V2}/pages", {"space-id": KB}),
        ("/wiki/rest/api/content/search", {"cql": "space=KB ORDER BY title"}),
    ):
        first = await client.get(path, headers=auth, params={**params, "limit": 1})
        link = first.json()["_links"]["next"]
        assert "cursor" in parse_qs(urlsplit(link).query)
        assert (await client.get(link, headers=auth)).status_code == 200
        other = bearer(await mint(client, SITE, user="p018"))
        assert (await client.get(link, headers=other)).status_code == 400
        changed = await client.get(
            link, headers=auth, params={"cursor": parse_qs(urlsplit(link).query)["cursor"][0], "limit": 1}
        )
        assert changed.status_code == 400
    excerpts = await client.get(
        "/wiki/rest/api/search", headers=auth, params={"cql": "space=KB", "excerpt": "none"}
    )
    assert all(r["excerpt"] == "" for r in excerpts.json()["results"])


@pytest.mark.parametrize("initial", [None, 1, {}, "bad"])
async def test_invalid_label_update_sequences_do_not_crash_or_partially_write(
    client: httpx.AsyncClient, auth: dict[str, str], initial: Any
) -> None:
    path = "/rest/api/3/issue/OPS-1"
    before = (await client.get(path, headers=auth)).json()["fields"]
    response = await client.put(
        path,
        headers=auth,
        json={
            "fields": {"summary": "Must not persist"},
            "update": {"labels": [{"set": initial}, {"add": "audit"}]},
        },
    )
    assert response.status_code == 400 and "errorMessages" in response.json()
    assert (await client.get(path, headers=auth)).json()["fields"] == before


@pytest.mark.parametrize("value", [True, 1.5, [], {}, 10**100])
async def test_jira_numeric_paging_rejects_non_integer_json_values(
    client: httpx.AsyncClient, auth: dict[str, str], value: Any
) -> None:
    response = await client.post(
        "/rest/api/3/search/jql", headers=auth, json={"jql": "project=OPS", "maxResults": value}
    )
    assert response.status_code == 400


async def test_jira_create_honors_update_and_rejects_invalid_adf_or_due_dates(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    created = await client.post(
        "/rest/api/3/issue",
        headers=auth,
        json={
            "fields": {"project": {"key": "OPS"}, "summary": "Unicode — 測試", "issuetype": {"name": "Task"}},
            "update": {"labels": [{"set": ["audit"]}, {"add": "triaged"}]},
        },
    )
    assert created.status_code == 201
    path = created.json()["self"]
    assert (await client.get(path, headers=auth)).json()["fields"]["labels"] == ["audit", "triaged"]
    for fields in (
        {"duedate": "20261016"},
        {"labels": ["bad\tlabel"]},
        {"description": {"type": "doc", "version": True, "content": []}},
        {"description": {"type": "doc", "version": 1, "content": [123]}},
    ):
        assert (await client.put(path, headers=auth, json={"fields": fields})).status_code == 400


@pytest.mark.parametrize("restriction_type", ["group", "role"])
async def test_jira_comment_restrictions_apply_to_lists_issue_reads_and_search(
    client: httpx.AsyncClient, auth: dict[str, str], hub: EmulatorHub, restriction_type: str
) -> None:
    group = hub.context("atlassian").store["groups"].first(_world_id="g-it")
    author = bearer(await mint(client, SITE, user="p035"))
    visibility = {
        "type": restriction_type,
        "value": group["name"] if restriction_type == "group" else "Administrators",
    }
    response = await client.post(
        "/rest/api/3/issue/OPS-1/comment",
        headers=author,
        json={
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Restricted audit details"}]}
                ],
            },
            "visibility": visibility,
        },
    )
    assert response.status_code == 201
    comment = response.json()
    assert comment["visibility"]["value"] == visibility["value"]
    assert (await client.get(comment["self"], headers=author)).status_code == 200
    assert (await client.get(comment["self"], headers=auth)).status_code == 404
    for headers, expected_total in ((author, 2), (auth, 1)):
        comments = (
            await client.get("/rest/api/3/issue/OPS-1/comment", params={"maxResults": 1}, headers=headers)
        ).json()
        assert comments["total"] == expected_total
        issue = (await client.get("/rest/api/3/issue/OPS-1", headers=headers)).json()
        assert issue["fields"]["comment"]["total"] == expected_total
        search = (
            await client.get(
                "/rest/api/3/search/jql", params={"jql": "key=OPS-1", "fields": "comment"}, headers=headers
            )
        ).json()
        assert search["issues"][0]["fields"]["comment"]["total"] == expected_total
    for value in (123, {"type": "group", "value": "missing"}, {"type": "role", "value": []}):
        denied = await client.post(
            "/rest/api/3/issue/OPS-1/comment",
            headers=author,
            json={"body": {"type": "doc", "version": 1, "content": []}, "visibility": value},
        )
        assert denied.status_code == 400


async def test_hidden_parent_issue_details_are_not_disclosed(
    client: httpx.AsyncClient, auth: dict[str, str], hub: EmulatorHub
) -> None:
    parent = hub.context("atlassian").store["issues"].first(key="ENG-1")
    parent["_read"] = ["p001"]
    reader = bearer(await mint(client, SITE, user="p026"))
    assert (await client.get("/rest/api/3/issue/ENG-1", headers=reader)).status_code == 404
    assert "parent" not in (await client.get("/rest/api/3/issue/ENG-2", headers=reader)).json()["fields"]
    assert "parent" in (await client.get("/rest/api/3/issue/ENG-2", headers=auth)).json()["fields"]


async def test_returned_resource_self_links_are_fetchable(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    issue = (await client.get("/rest/api/3/issue/OPS-1", headers=auth)).json()
    for path in (
        issue["fields"]["project"]["self"],
        issue["fields"]["assignee"]["self"],
        issue["fields"]["comment"]["comments"][0]["self"],
    ):
        assert (await client.get(path, headers=auth)).status_code == 200
    page = (await client.get(f"/wiki/rest/api/content/{DOCUMENT}?expand=space,version", headers=auth)).json()
    for path in (page["space"]["_links"]["self"], page["version"]["by"]["_links"]["self"]):
        assert (await client.get(path, headers=auth)).status_code == 200


@pytest.mark.parametrize(
    "path,key",
    [
        (f"{V2}/pages", "errors"),
        ("/wiki/rest/api/content/search?cql=type=page", "statusCode"),
        ("/rest/api/3/myself", "errorMessages"),
    ],
)
async def test_generic_fault_uses_actual_request_family_and_request_headers(
    client: httpx.AsyncClient, auth: dict[str, str], path: str, key: str
) -> None:
    await client.post("/_emulator/faults", json={"path": ".*", "status": 429, "times": 1})
    response = await client.get(path, headers=auth)
    assert response.status_code == 429 and key in response.json()
    assert response.headers["x-ratelimit-remaining"] == "0" and response.headers["retry-after"] == "2"
    assert response.headers["x-arequestid"] and response.headers["atl-traceid"]
    ok = await client.get(path, headers=auth)
    assert ok.status_code == 200 and ok.headers["x-arequestid"] != response.headers["x-arequestid"]


async def test_profile_scope_and_unicode_basic_password(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    no_profile = bearer(await mint(client, SITE, user="p001", scopes=["read:page:confluence"]))
    assert (await client.get("https://api.atlassian.com/me", headers=no_profile)).status_code == 403
    profile = bearer(await mint(client, SITE, user="p001", scopes=["read:me"]))
    assert (await client.get("https://api.atlassian.com/me", headers=profile)).status_code == 200
    denied = await client.get(
        f"{V2}/spaces", auth=httpx.BasicAuth("maya.chen@halcyonadvisory.example", "測試")
    )
    assert denied.status_code == 401


async def test_oauth_wrong_typed_pkce_does_not_consume_code(client: httpx.AsyncClient) -> None:
    consent = await client.get(
        AUTH + "/authorize",
        params={
            "audience": "api.atlassian.com",
            "client_id": "audit",
            "redirect_uri": "https://consumer.example/cb",
            "scope": "read:page:confluence",
            "response_type": "code",
            "login_hint": "p001",
            "code_challenge": b64url(hashlib.sha256(b"verifier").digest()),
            "code_challenge_method": "S256",
        },
    )
    code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
    payload = {
        "grant_type": "authorization_code",
        "client_id": "audit",
        "redirect_uri": "https://consumer.example/cb",
        "code": code,
        "code_verifier": {"bad": 1},
    }
    response = await client.post(AUTH + "/oauth/token", json=payload)
    assert response.status_code == 400 and response.json()["error"] == "invalid_request"
    successful = await client.post(AUTH + "/oauth/token", json={**payload, "code_verifier": "verifier"})
    assert successful.status_code == 200


@pytest.mark.parametrize(
    "pkce",
    [{"code_challenge_method": "unknown", "code_challenge": "verifier"}, {"code_challenge_method": "S256"}],
)
async def test_authorize_rejects_pkce_methods_that_would_bypass_verification(
    client: httpx.AsyncClient, pkce: dict[str, str]
) -> None:
    response = await client.get(
        AUTH + "/authorize",
        params={
            "audience": "api.atlassian.com",
            "client_id": "audit",
            "redirect_uri": "https://consumer.example/cb",
            "response_type": "code",
            "login_hint": "p001",
            **pkce,
        },
    )
    assert response.status_code == 400 and response.json()["error"] == "invalid_request"


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, 1),
        (401, 1),
        (403, 1),
        (404, 1),
        (408, 6),
        (409, 6),
        (425, 6),
        (429, 6),
        (500, 6),
        (503, 6),
        (0, 6),
        (200, 1),
    ],
)
async def test_jira_webhook_retry_statuses_count_headers_and_signature(
    client: httpx.AsyncClient, auth: dict[str, str], hub: EmulatorHub, status: int, expected: int
) -> None:
    received: list[httpx.Request] = []

    def receiver(request: httpx.Request) -> httpx.Response:
        received.append(request)
        if status == 0:
            raise httpx.ConnectError("Receiver unavailable", request=request)
        return httpx.Response(status)

    hub.webhook_transport = httpx.MockTransport(receiver)
    configured = await client.post(
        "/_emulator/actions/configure_webhook",
        json={
            "url": "https://consumer.example/hook",
            "events": ["jira:issue_created"],
            "secret": "audit-signing-key",
        },
    )
    assert configured.status_code == 200
    created = await client.post(
        "/rest/api/3/issue",
        headers={**auth, "X-Atlassian-Webhook-Trace": "audit-trace"},
        json={
            "fields": {"project": {"key": "OPS"}, "summary": "Webhook audit", "issuetype": {"name": "Task"}}
        },
    )
    assert created.status_code == 201 and len(received) == expected
    assert len({r.headers["x-atlassian-webhook-identifier"] for r in received}) == 1
    assert len({r.content for r in received}) == 1
    assert "x-atlassian-webhook-retry" not in received[0].headers
    for retry, request in enumerate(received):
        assert request.headers["x-atlassian-webhook-flow"] == "Primary"
        assert request.headers["x-atlassian-webhook-trace"] == "audit-trace"
        if retry:
            assert request.headers["x-atlassian-webhook-retry"] == str(retry)
        assert (
            request.headers["x-hub-signature"]
            == "sha256=" + hmac.new(b"audit-signing-key", request.content, hashlib.sha256).hexdigest()
        )
    deliveries = (await client.get("/_emulator/webhooks")).json()["deliveries"]
    assert deliveries[0]["attempts"] == expected
    assert deliveries[0]["status"] == ("delivered" if status == 200 else "failed")


async def test_jira_webhook_stops_retrying_after_success(
    client: httpx.AsyncClient, auth: dict[str, str], hub: EmulatorHub
) -> None:
    calls = 0

    def receiver(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls < 3 else 204)

    hub.webhook_transport = httpx.MockTransport(receiver)
    await client.post(
        "/_emulator/actions/configure_webhook",
        json={"url": "https://consumer.example/hook", "events": ["jira:issue_created"]},
    )
    response = await client.post(
        "/rest/api/3/issue",
        headers=auth,
        json={
            "fields": {
                "project": {"key": "OPS"},
                "summary": "Recovered webhook",
                "issuetype": {"name": "Task"},
            }
        },
    )
    assert response.status_code == 201 and calls == 3
    delivery = (await client.get("/_emulator/webhooks")).json()["deliveries"][0]
    assert delivery["status"] == "delivered" and delivery["error"] is None


async def test_deep_json_and_embedded_adf_are_rejected_without_recursion_crashes(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.post(
        f"{V2}/pages",
        headers={**auth, "Content-Type": "application/json"},
        content=b'{"body":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}",
    )
    assert response.status_code == 400
    node: dict[str, Any] = {"type": "paragraph", "content": []}
    for _ in range(80):
        node = {"type": "blockquote", "content": [node]}
    response = await client.post(
        f"{V2}/pages",
        headers=auth,
        json={
            "spaceId": KB,
            "title": "Deep ADF",
            "body": {
                "representation": "atlas_doc_format",
                "value": json.dumps({"type": "doc", "version": 1, "content": [node]}),
            },
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize("field", ["created", "id"])
async def test_huge_query_literals_do_not_overflow_python_integer_limits(
    client: httpx.AsyncClient, auth: dict[str, str], field: str
) -> None:
    value = "9" * 5000 + ("d" if field == "created" else "")
    response = await client.get(
        "/wiki/rest/api/content/search", headers=auth, params={"cql": f'type=page AND {field} > "{value}"'}
    )
    assert response.status_code == 400


def test_seeded_published_history_has_no_future_dates(hub: EmulatorHub) -> None:
    for page in hub.context("atlassian").store["pages"]:
        assert parse_instant(page["createdAt"]) <= hub.world.anchor
        for history in page["_history"]:
            assert (
                parse_instant(page["createdAt"])
                <= parse_instant(history["version"]["createdAt"])
                <= hub.world.anchor
            )


@pytest.mark.parametrize("prefix", [V2, f"/ex/confluence/{CLOUD_ID}/wiki/api/v2"])
async def test_historical_versions_report_historical_status(
    client: httpx.AsyncClient, auth: dict[str, str], prefix: str
) -> None:
    host = "https://api.atlassian.com" if prefix.startswith("/ex/") else SITE
    response = await client.get(f"{host}{prefix}/pages/{DOCUMENT}?version=1", headers=auth)
    assert response.status_code == 200 and response.json()["status"] == "historical"
    legacy = await client.get(f"/wiki/rest/api/content/{DOCUMENT}?version=1", headers=auth)
    assert legacy.status_code == 200 and legacy.json()["status"] == "historical"


WRITE_PATHS = [
    ("POST", f"{V2}/pages"),
    ("PUT", f"{V2}/pages/{DOCUMENT}"),
    ("POST", f"{V2}/blogposts"),
    ("PUT", f"{V2}/blogposts/{numeric_id('blog', 'knowledge')}"),
    ("POST", f"{V2}/users-bulk"),
    *[
        (
            method,
            f"{V2}/{resource}/{pid}/properties"
            + (f"/{numeric_id('property', pid)}" if method == "PUT" else ""),
        )
        for resource, pid in (
            ("pages", DOCUMENT),
            ("spaces", CLIENTS),
            ("blogposts", numeric_id("blog", "knowledge")),
        )
        for method in ("POST", "PUT")
    ],
    ("POST", "/rest/api/3/issue"),
    ("PUT", "/rest/api/3/issue/OPS-1"),
    ("POST", "/rest/api/3/issue/OPS-1/transitions"),
    ("POST", "/rest/api/3/issue/OPS-1/comment"),
    ("POST", "/rest/api/3/search/jql"),
    ("POST", "/oauth/token"),
]


@pytest.mark.parametrize("method,path", WRITE_PATHS)
@pytest.mark.parametrize(
    "content", [b"", b"not-json", b"[]", b"null", b'"text"', b"123", b'{"truncated":', b'{"invalid": NaN}']
)
async def test_every_write_endpoint_rejects_malformed_http_bodies(
    client: httpx.AsyncClient, auth: dict[str, str], method: str, path: str, content: bytes
) -> None:
    response = await client.request(
        method, path, headers={**auth, "Content-Type": "application/json"}, content=content
    )
    assert response.status_code == 400
    assert (
        "errorMessages" in response.json()
        if path.startswith("/rest/")
        else "error" in response.json()
        if path == "/oauth/token"
        else "errors" in response.json()
    )
