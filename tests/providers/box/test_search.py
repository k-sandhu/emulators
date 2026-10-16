"""Search and metadata queries."""

from __future__ import annotations

from datetime import datetime


async def names(box, who="p035", **params) -> list[str]:
    r = await box.get("/search", who, params={"limit": 200, "fields": "name", **params})
    assert r.status_code == 200, r.text
    return [e["name"] for e in r.json()["entries"]]


async def test_search_envelope_and_defaults(box):
    r = await box.get("/search", params={"query": "engagement letter", "limit": 5})
    body = r.json()
    assert body["type"] == "search_results_items"
    assert body["limit"] == 5 and body["offset"] == 0 and body["total_count"] > 5
    assert len(body["entries"]) == 5
    assert all(e["type"] in ("file", "folder") for e in body["entries"])
    first = body["entries"][0]
    assert "path_collection" in first and "parent" in first and "modified_at" in first  # standard fields
    assert (await box.get("/search")).status_code == 400
    assert (await box.get("/search", params={"query": "x", "limit": 201})).status_code == 400


async def test_search_matches_names_with_word_prefixes_case_insensitively(box):
    hits = await names(box, query="engag", type="file")
    assert hits and all("engagement" in n.lower() for n in hits[:10])
    assert await names(box, query="ENGAGEMENT LETTER", type="file") == await names(
        box, query="engagement letter", type="file"
    )
    exact = await names(box, query='"Engagement Letter - Signed"', type="file", content_types="name")
    assert exact and all(n.startswith("Engagement Letter - Signed") for n in exact)


async def test_search_boolean_operators(box):
    both = set(await names(box, query="northwind AND letter", content_types="name"))
    either = set(await names(box, query="northwind OR letter", content_types="name"))
    only_northwind = set(await names(box, query="northwind NOT letter", content_types="name"))
    assert both and both <= either
    assert only_northwind.isdisjoint(both)
    assert only_northwind and all("letter" not in n.lower() for n in only_northwind)
    # Box reads a multi-word query as OR unless AND joins the terms.
    implicit = set(await names(box, query="northwind letter", content_types="name"))
    assert implicit == either and implicit > both
    assert set(await names(box, query="northwind AND NOT letter", content_types="name")) == only_northwind


async def test_search_in_file_content_and_content_types(box):
    # The seeded PBC request list mentions "Prepared-by-client".
    content = await names(box, query="Prepared-by-client", content_types="file_content", type="file")
    assert "PBC Request List.xlsx" in content
    assert "PBC Request List.xlsx" not in await names(box, query="Prepared-by-client", content_types="name")
    assert (await box.get("/search", params={"query": "x", "content_types": "bogus"})).status_code == 400


async def test_search_filters(box):
    pdfs = await names(box, query="signed", file_extensions="pdf")
    assert pdfs and all(n.endswith(".pdf") for n in pdfs)
    clients = await box.find("Client Engagements")
    inside = await box.get(
        "/search", params={"query": "letter", "ancestor_folder_ids": clients, "fields": "path_collection"}
    )
    for entry in inside.json()["entries"]:
        assert clients in [f["id"] for f in entry["path_collection"]["entries"]]
    other = await box.find("Technology")
    assert await names(box, query="letter", ancestor_folder_ids=other) == []
    folders = await box.get("/search", params={"query": "northwind", "type": "folder", "fields": "name"})
    assert {e["type"] for e in folders.json()["entries"]} == {"folder"}
    owner = (await box.get("/users/me", "p035")).json()["id"]
    owned = await box.get(
        "/search", params={"query": "letter", "owner_user_ids": owner, "fields": "owned_by"}
    )
    assert owned.json()["total_count"] > 0
    assert all(e["owned_by"]["id"] == owner for e in owned.json()["entries"])
    nobody = await box.get("/search", params={"query": "letter", "owner_user_ids": "1"})
    assert nobody.json()["total_count"] == 0
    recent = await box.get(
        "/search",
        params={"query": "letter", "updated_at_range": "2026-10-01T00:00:00-07:00,", "fields": "modified_at"},
    )
    assert all(e["modified_at"] >= "2026-10-01" for e in recent.json()["entries"])
    old = await box.get("/search", params={"query": "letter", "created_at_range": ",2021-01-01T00:00:00Z"})
    assert old.status_code == 200
    assert (
        await box.get("/search", params={"query": "x", "created_at_range": "yesterday,"})
    ).status_code == 400
    small = await box.get(
        "/search", params={"query": "letter", "type": "file", "size_range": ",1000", "fields": "size"}
    )
    assert all(e["size"] <= 1000 for e in small.json()["entries"])


async def test_search_sorting_and_paging(box):
    def stamps(body):
        return [datetime.fromisoformat(e["modified_at"]) for e in body["entries"]]

    desc = await box.get(
        "/search",
        params={"query": "letter", "sort": "modified_at", "direction": "DESC", "fields": "modified_at"},
    )
    assert stamps(desc.json()) == sorted(stamps(desc.json()), reverse=True)
    asc = await box.get(
        "/search",
        params={"query": "letter", "sort": "modified_at", "direction": "ASC", "fields": "modified_at"},
    )
    assert stamps(asc.json()) == sorted(stamps(asc.json()))
    assert stamps(asc.json())[0] < stamps(asc.json())[-1]
    page1 = (await box.get("/search", params={"query": "letter", "limit": 4, "fields": "name"})).json()
    page2 = (
        await box.get("/search", params={"query": "letter", "limit": 4, "offset": 4, "fields": "name"})
    ).json()
    assert page2["offset"] == 4
    assert {e["id"] for e in page1["entries"]}.isdisjoint({e["id"] for e in page2["entries"]})
    assert (await box.get("/search", params={"query": "x", "sort": "alphabetical"})).status_code == 400


async def test_search_only_returns_items_the_caller_can_see(box):
    # Growth staff cannot see client files, so they get no hits; partners do.
    assert await names(box, "p044", query='"engagement letter"', type="file") == []
    assert await names(box, "p004", query='"engagement letter"', type="file")
    # Restricted salary bands: only the owner and the two people it was shared with can find it.
    assert await names(box, "p031", query='"salary bands"')
    assert await names(box, "p001", query='"salary bands"')
    assert await names(box, "p032", query='"salary bands"') == []
    assert await names(box, "p035", query='"salary bands"') == []
    assert (
        await box.get("/search", "p018", params={"query": "x", "scope": "enterprise_content"})
    ).status_code == 403


async def test_search_trash_handling(box):
    parent = await box.find("Technology")
    fid = (await box.upload(parent, "zebra-crossing-plan.txt", b"stripes")).json()["entries"][0]["id"]
    assert "zebra-crossing-plan.txt" in await names(box, query="zebra")
    await box.delete(f"/files/{fid}")
    assert await names(box, query="zebra") == []
    assert await names(box, query="zebra", trash_content="trashed_only") == ["zebra-crossing-plan.txt"]
    assert await names(box, query="zebra", trash_content="all_items") == ["zebra-crossing-plan.txt"]
    assert (await box.get("/search", params={"query": "x", "trash_content": "maybe"})).status_code == 400


async def test_search_by_metadata_filters(box):
    filters = '[{"scope": "enterprise", "templateKey": "clientInfo", "filters": {"clientName": "Northwind Logistics"}}]'
    r = await box.get(
        "/search", params={"mdfilters": filters, "fields": "name,path_collection", "limit": 200}
    )
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert entries and all(
        "Northwind Logistics" in [p["name"] for p in e["path_collection"]["entries"]] for e in entries
    )
    numeric = (
        '[{"scope": "enterprise", "templateKey": "clientInfo", "filters": {"fiscalYear": {"gt": 2025}}}]'
    )
    r = await box.get("/search", params={"mdfilters": numeric, "limit": 200})
    assert r.json()["total_count"] > 0
    assert (await box.get("/search", params={"mdfilters": "nope"})).status_code == 400


async def test_metadata_query_language(box):
    body = {
        "from": "enterprise_336533359.clientInfo",
        "ancestor_folder_id": "0",
        "query": "clientName = :name AND fiscalYear >= :year",
        "query_params": {"name": "Northwind Logistics", "year": 2025},
        "fields": ["name", "metadata.enterprise_336533359.clientInfo"],
    }
    r = await box.post("/metadata_queries/execute_read", json=body)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["entries"] and result["next_marker"] is None
    for entry in result["entries"]:
        info = entry["metadata"]["enterprise_336533359"]["clientInfo"]
        assert info["clientName"] == "Northwind Logistics" and info["fiscalYear"] >= 2025
    ordered = await box.post(
        "/metadata_queries/execute_read",
        json={
            "from": "enterprise_336533359.clientInfo",
            "ancestor_folder_id": "0",
            "query": "clientName LIKE :pat OR engagementType = :type",
            "query_params": {"pat": "Blue%", "type": "Tax Compliance"},
            "order_by": [{"field_key": "fiscalYear", "direction": "desc"}],
            "limit": 3,
            "fields": ["metadata.enterprise_336533359.clientInfo"],
        },
    )
    entries = ordered.json()["entries"]
    years = [e["metadata"]["enterprise_336533359"]["clientInfo"]["fiscalYear"] for e in entries]
    assert len(entries) == 3 and years == sorted(years, reverse=True)
    assert ordered.json()["next_marker"]
    nxt = await box.post(
        "/metadata_queries/execute_read",
        json={
            "from": "enterprise_336533359.clientInfo", "ancestor_folder_id": "0",
            "query": "clientName LIKE :pat OR engagementType = :type",
            "query_params": {"pat": "Blue%", "type": "Tax Compliance"},
            "order_by": [{"field_key": "fiscalYear", "direction": "desc"}],
            "limit": 3, "marker": ordered.json()["next_marker"],
        },
    )  # fmt: skip
    assert {e["id"] for e in nxt.json()["entries"]}.isdisjoint({e["id"] for e in entries})
    failures = (
        ({**body, "query": "clientName ="}, 400),
        ({**body, "query": "clientName = :missing"}, 400),
        ({**body, "from": "enterprise_336533359.nothing"}, 404),
        ({**body, "limit": 500}, 400),
    )
    for payload, expected in failures:
        assert (await box.post("/metadata_queries/execute_read", json=payload)).status_code == expected, (
            payload
        )
    scoped = await box.post(
        "/metadata_queries/execute_read", "p044", json={**body, "ancestor_folder_id": "0"}
    )
    assert scoped.json()["entries"] == []  # growth staff cannot see client files
