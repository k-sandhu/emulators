"""list_folder cursors (delta), long-polling and search."""

from __future__ import annotations

import asyncio
from typing import Any


async def test_cursor_delta_reports_adds_edits_moves_and_deletes(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    await dbx.put(token, "/watch/a.txt", b"a")
    page = await dbx.ok(token, "files/list_folder", {"path": "/watch", "recursive": True})
    cursor = page["cursor"]
    quiet = await dbx.ok(token, "files/list_folder/continue", {"cursor": cursor})
    assert quiet["entries"] == [] and quiet["has_more"] is False
    cursor = quiet["cursor"]

    await dbx.put(token, "/watch/b.txt", b"b")
    await dbx.put(token, "/watch/a.txt", b"a2", mode="overwrite")
    await dbx.ok(token, "files/move_v2", {"from_path": "/watch/a.txt", "to_path": "/watch/sub/a.txt"})
    delta = await dbx.ok(token, "files/list_folder/continue", {"cursor": cursor})
    summary = {(e[".tag"], e["path_lower"]) for e in delta["entries"]}
    assert ("file", "/watch/b.txt") in summary
    assert ("deleted", "/watch/a.txt") in summary
    assert ("file", "/watch/sub/a.txt") in summary
    assert ("folder", "/watch/sub") in summary
    cursor = delta["cursor"]

    await dbx.ok(token, "files/delete_v2", {"path": "/watch/b.txt"})
    delta = await dbx.ok(token, "files/list_folder/continue", {"cursor": cursor})
    assert [(e[".tag"], e["path_lower"]) for e in delta["entries"]] == [("deleted", "/watch/b.txt")]
    # the cursor does not report the same change twice
    again = await dbx.ok(token, "files/list_folder/continue", {"cursor": delta["cursor"]})
    assert again["entries"] == []


async def test_get_latest_cursor_skips_existing_content(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    await dbx.put(token, "/latest/old.txt", b"o")
    cur = await dbx.ok(token, "files/list_folder/get_latest_cursor", {"path": "/latest", "recursive": True})
    assert (await dbx.ok(token, "files/list_folder/continue", {"cursor": cur["cursor"]}))["entries"] == []
    await dbx.put(token, "/latest/new.txt", b"n")
    delta = await dbx.ok(token, "files/list_folder/continue", {"cursor": cur["cursor"]})
    assert [e["name"] for e in delta["entries"]] == ["new.txt"]


async def test_delta_is_scoped_to_the_folder_and_non_recursive_depth(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    await dbx.ok(token, "files/create_folder_v2", {"path": "/scope/in"})
    await dbx.ok(token, "files/create_folder_v2", {"path": "/other"})
    cur = await dbx.ok(token, "files/list_folder/get_latest_cursor", {"path": "/scope"})
    await dbx.put(token, "/other/x.txt", b"x")
    await dbx.put(token, "/scope/in/deep.txt", b"d")
    await dbx.put(token, "/scope/top.txt", b"t")
    delta = await dbx.ok(token, "files/list_folder/continue", {"cursor": cur["cursor"]})
    assert [e["name"] for e in delta["entries"]] == ["top.txt"]
    rec = await dbx.ok(token, "files/list_folder/get_latest_cursor", {"path": "/scope", "recursive": True})
    await dbx.put(token, "/scope/in/deep2.txt", b"d")
    delta = await dbx.ok(token, "files/list_folder/continue", {"cursor": rec["cursor"]})
    assert [e["name"] for e in delta["entries"]] == ["deep2.txt"]


async def test_delta_pagination(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    await dbx.ok(token, "files/create_folder_v2", {"path": "/many"})
    cur = await dbx.ok(token, "files/list_folder/get_latest_cursor", {"path": "/many", "limit": 2})
    for i in range(5):
        await dbx.put(token, f"/many/f{i}.txt", b"x")
    names: list[str] = []
    page = await dbx.ok(token, "files/list_folder/continue", {"cursor": cur["cursor"]})
    pages = 1
    names += [e["name"] for e in page["entries"]]
    while page["has_more"]:
        page = await dbx.ok(token, "files/list_folder/continue", {"cursor": page["cursor"]})
        names += [e["name"] for e in page["entries"]]
        pages += 1
    assert sorted(names) == [f"f{i}.txt" for i in range(5)] and pages == 3


async def test_invalid_cursor_and_reset(dbx: Any, hub: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    r = await dbx.rpc(token, "files/list_folder/continue", {"cursor": "garbage"})
    assert r.status_code == 400 and "cursor" in r.text
    await dbx.put(token, "/rst/a.txt", b"a")
    cur = await dbx.ok(token, "files/list_folder/get_latest_cursor", {"path": "/rst"})
    await dbx.put(token, "/rst/b.txt", b"b")
    hub.reset("dropbox")  # the cursor now points past the restored state
    dbx.tokens.clear()
    token = await dbx.token(dbx.MAYA)
    err = await dbx.err(token, "files/list_folder/continue", {"cursor": cur["cursor"]})
    assert err["error"] == {".tag": "reset"}


async def test_longpoll(dbx: Any, hub: Any, monkeypatch: Any) -> None:
    monkeypatch.setitem(hub.contexts["dropbox"].settings, "LONGPOLL_MAX_WAIT_S", "0.5")
    token = await dbx.token(dbx.MAYA)
    await dbx.ok(token, "files/create_folder_v2", {"path": "/poll"})
    cur = (await dbx.ok(token, "files/list_folder/get_latest_cursor", {"path": "/poll"}))["cursor"]
    # no auth header, notify host
    r = await dbx.client.post(
        f"{dbx.NOTIFY}/2/files/list_folder/longpoll", json={"cursor": cur, "timeout": 30}
    )
    assert r.status_code == 200 and r.json() == {"changes": False}
    dbx.check("files/list_folder/longpoll", "result", r.json())

    async def write_later() -> None:
        await asyncio.sleep(0.15)
        await dbx.put(token, "/poll/x.txt", b"x")

    poll = asyncio.create_task(
        dbx.client.post(f"{dbx.NOTIFY}/2/files/list_folder/longpoll", json={"cursor": cur, "timeout": 30})
    )
    await write_later()
    r = await poll
    assert r.json() == {"changes": True}
    bad = await dbx.client.post(
        f"{dbx.NOTIFY}/2/files/list_folder/longpoll", json={"cursor": "nope", "timeout": 30}
    )
    assert bad.status_code == 409 and bad.json()["error"] == {".tag": "reset"}


async def test_search_by_filename_content_and_filters(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    root = await dbx.root(token)
    found = await dbx.ok(token, "files/search_v2", {"query": "engagement letter"}, path_root=root)
    assert found["matches"] and all(
        mt["metadata"][".tag"] == "metadata"
        and mt["match_type"][".tag"] in ("filename", "filename_and_content")
        for mt in found["matches"][:5]
    )
    names = {mt["metadata"]["metadata"]["name"] for mt in found["matches"]}
    assert any("Engagement Letter" in n for n in names)
    # folder scoping, extension and category filters
    scoped = await dbx.ok(
        token,
        "files/search_v2",
        {
            "query": "northwind",
            "options": {
                "path": "/Client Engagements/Northwind Logistics",
                "file_extensions": ["pdf"],
                "max_results": 3,
            },
        },
        path_root=root,
    )
    assert 0 < len(scoped["matches"]) <= 3
    assert all(mt["metadata"]["metadata"]["name"].endswith(".pdf") for mt in scoped["matches"])
    cats = await dbx.ok(
        token,
        "files/search_v2",
        {"query": "northwind", "options": {"file_categories": [{".tag": "folder"}]}},
        path_root=root,
    )
    assert cats["matches"] and all(mt["metadata"]["metadata"][".tag"] == "folder" for mt in cats["matches"])
    hl = await dbx.ok(
        token,
        "files/search_v2",
        {
            "query": "northwind",
            "match_field_options": {"include_highlights": True},
            "options": {"max_results": 2},
        },
        path_root=root,
    )
    spans = hl["matches"][0]["highlight_spans"]
    assert any(s["is_highlighted"] for s in spans)
    nothing = await dbx.ok(token, "files/search_v2", {"query": "zzzqqqxxx"}, path_root=root)
    assert nothing["matches"] == [] and nothing["has_more"] is False


async def test_search_pagination_content_and_visibility(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    root = await dbx.root(token)
    first = await dbx.ok(
        token, "files/search_v2", {"query": "request list", "options": {"max_results": 5}}, path_root=root
    )
    seen = [mt["metadata"]["metadata"]["id"] for mt in first["matches"]]
    page = first
    while page["has_more"]:
        page = await dbx.ok(token, "files/search/continue_v2", {"cursor": page["cursor"]})
        seen.extend(mt["metadata"]["metadata"]["id"] for mt in page["matches"])
    assert len(seen) == len(set(seen)) and len(seen) > 5
    # content search finds paper docs by body text
    body = await dbx.ok(token, "files/search_v2", {"query": "laptop and accounts"}, path_root=root)
    assert any(mt["match_type"][".tag"] in ("file_content", "filename_and_content") for mt in body["matches"])
    # a junior cannot find partner-only content (M&A target screen lives in Partner Group)
    ryan = await dbx.token("ryan.murphy@halcyonadvisory.example")
    ryan_root = await dbx.root(ryan)
    hits = await dbx.ok(ryan, "files/search_v2", {"query": "Project Lighthouse"}, path_root=ryan_root)
    assert hits["matches"] == []
    partner_hits = await dbx.ok(token, "files/search_v2", {"query": "Project Lighthouse"}, path_root=root)
    assert partner_hits["matches"]
    bad = await dbx.err(token, "files/search_v2", {"query": "   "})
    assert bad["error"][".tag"] == "invalid_argument"
    deleted = await dbx.ok(
        token,
        "files/search_v2",
        {"query": "draft", "options": {"file_status": {".tag": "deleted"}}},
        path_root=root,
    )
    assert deleted["matches"] == []
