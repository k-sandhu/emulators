"""files/list_folder, get_metadata, download family against the seeded Halcyon team."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from typing import Any

import pytest


def content_hash(data: bytes) -> str:
    blocks = [
        hashlib.sha256(data[i : i + 4 * 1024 * 1024]).digest() for i in range(0, len(data), 4 * 1024 * 1024)
    ]
    return hashlib.sha256(b"".join(blocks)).hexdigest()


async def test_home_root_listing(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    page = await dbx.ok(token, "files/list_folder", {"path": ""})
    names = {e["name"] for e in page["entries"]}
    assert names == {"Notes", "Expenses"}
    assert page["has_more"] is False
    for e in page["entries"]:
        assert e[".tag"] == "folder"
        assert e["path_lower"] == "/" + e["name"].lower()
        assert re.fullmatch(r"id:[A-Za-z0-9_-]{22}", e["id"])


async def test_team_root_shows_only_accessible_team_folders(dbx: Any) -> None:
    # Maya (partner, Advisory) sees all seven team folders plus her own member folder ...
    maya = await dbx.token(dbx.MAYA)
    root = await dbx.root(maya)
    names = {e["name"] for e in await dbx.list_all(maya, "", path_root=root)}
    assert {"Client Engagements", "Partner Group", "Maya Chen"} <= names
    assert "Eleanor Whitfield" not in names  # other members' folders are private
    # ... but a junior in a non-leadership department does not see Partner Group / Finance.
    junior = await dbx.token("ryan.murphy@halcyonadvisory.example")
    junior_root = await dbx.root(junior)
    junior_names = {e["name"] for e in await dbx.list_all(junior, "", path_root=junior_root)}
    assert "Partner Group" not in junior_names
    assert "Client Engagements" in junior_names  # audit staff are in g-audit (editor)
    denied = await dbx.err(junior, "files/list_folder", {"path": "/Partner Group"}, path_root=junior_root)
    assert denied["error_summary"].startswith("path/not_found/")


async def test_default_path_root_is_home_not_team_root(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    err = await dbx.err(token, "files/list_folder", {"path": "/Client Engagements"})
    assert err["error"] == {".tag": "path", "path": {".tag": "not_found"}}


async def test_recursive_listing_paginates_and_matches_world(dbx: Any, hub: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    root = await dbx.root(token)
    page = await dbx.ok(
        token,
        "files/list_folder",
        {"path": "/Client Engagements", "recursive": True, "limit": 50},
        path_root=root,
    )
    assert len(page["entries"]) >= 50 and page["has_more"] is True
    entries = await dbx.list_all(token, "/Client Engagements", recursive=True, limit=50, path_root=root)
    files = [e for e in entries if e[".tag"] == "file"]
    world_docs = hub.world.documents_in("clients")
    assert len(files) == len(world_docs)
    keys = [e["path_lower"] for e in entries]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    # parents always come before their children
    seen: set[str] = set()
    for e in entries:
        parent = e["path_lower"].rsplit("/", 1)[0]
        assert parent in seen or parent == "/client engagements"
        if e[".tag"] == "folder":
            seen.add(e["path_lower"])


async def test_metadata_shapes_and_ids(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    root = await dbx.root(token)
    folder = "/Client Engagements/Northwind Logistics/FY2025 Review"
    entries = await dbx.list_all(token, folder, path_root=root)
    file = next(e for e in entries if e[".tag"] == "file")
    assert file["is_downloadable"] is True
    assert len(file["content_hash"]) == 64
    assert file["sharing_info"]["parent_shared_folder_id"]
    assert file["sharing_info"]["modified_by"].startswith("dbid:")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", file["client_modified"])
    # lookups: case-insensitive path, id, and id with sub path all agree
    by_path = await dbx.ok(
        token, "files/get_metadata", {"path": file["path_display"].upper()}, path_root=root
    )
    by_id = await dbx.ok(token, "files/get_metadata", {"path": file["id"]}, path_root=root)
    assert by_path == by_id
    by_rev = await dbx.ok(token, "files/get_metadata", {"path": "rev:" + file["rev"]}, path_root=root)
    assert by_rev["rev"] == file["rev"]


async def test_get_metadata_errors(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    err = await dbx.err(token, "files/get_metadata", {"path": "/nope/missing.txt"})
    assert err["error_summary"].startswith("path/not_found/")
    root = await dbx.err(token, "files/get_metadata", {"path": "/"})
    assert root["error"]["path"][".tag"] == "malformed_path"
    bad = await dbx.err(token, "files/get_metadata", {"path": "no-leading-slash"}, status=400)
    assert bad.startswith('Error in call to API function "files/get_metadata": request body: path:')
    missing = await dbx.err(token, "files/get_metadata", {}, status=400)
    assert "path" in missing


async def test_download_returns_real_document_bytes(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    root = await dbx.root(token)
    entries = await dbx.list_all(
        token, "/Client Engagements/Northwind Logistics", recursive=True, path_root=root
    )
    docx = next(e for e in entries if e["name"].endswith(".docx"))
    r, result = await dbx.download(token, docx["path_display"], path_root=root)
    assert r.status_code == 200
    assert result["id"] == docx["id"]
    assert r.headers["content-type"] == "application/octet-stream"
    assert content_hash(r.content) == result["content_hash"] == docx["content_hash"]
    assert len(r.content) == result["size"]
    docx_mod = pytest.importorskip("docx")
    document = docx_mod.Document(io.BytesIO(r.content))
    assert any(p.text for p in document.paragraphs)


async def test_download_supports_range_and_header_unicode(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    body = bytes(range(256)) * 10
    await dbx.put(token, "/r\u00e9sum\u00e9 \u2013 notes.bin", body)
    r, result = await dbx.download(token, "/r\u00e9sum\u00e9 \u2013 notes.bin", range_="bytes=10-19")
    assert r.status_code == 206
    assert r.content == body[10:20]
    assert r.headers["content-range"] == f"bytes 10-19/{len(body)}"
    raw = r.headers["dropbox-api-result"]
    assert raw.isascii() and "\\u00e9" in raw
    assert result["name"] == "r\u00e9sum\u00e9 \u2013 notes.bin"
    r2, _ = await dbx.download(token, "/r\u00e9sum\u00e9 \u2013 notes.bin", range_="bytes=-5")
    assert r2.content == body[-5:]
    r3, _ = await dbx.download(token, "/r\u00e9sum\u00e9 \u2013 notes.bin", range_="bytes=99999-")
    assert r3.status_code == 416


async def test_download_errors(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    r, _ = await dbx.download(token, "/Notes")
    assert r.status_code == 409
    assert r.json()["error"]["path"][".tag"] == "not_file"
    r, _ = await dbx.download(token, "/missing.txt")
    assert r.json()["error_summary"].startswith("path/not_found/")


async def test_download_zip(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    r, result = await dbx.download(token, "/Notes", route="files/download_zip")
    assert r.status_code == 200
    assert ".tag" not in result["metadata"] and result["metadata"]["name"] == "Notes"  # typed FolderMetadata
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
        assert any(n.endswith(".docx") for n in names)
        assert all(n.startswith("Notes/") for n in names)


async def test_temporary_link_round_trip(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    expenses = await dbx.list_all(token, "/Expenses")
    path = expenses[0]["path_display"]
    data = await dbx.ok(token, "files/get_temporary_link", {"path": path})
    assert data["link"].startswith("https://dl.dropboxusercontent.com/apitl/1/")
    r = await dbx.client.get(data["link"])
    assert r.status_code == 200
    assert content_hash(r.content) == data["metadata"]["content_hash"]
    assert "attachment" in r.headers["content-disposition"]
    assert (await dbx.client.get(data["link"].replace("apitl/1/", "apitl/1/x"))).status_code == 404


async def test_thumbnails_and_preview(dbx: Any) -> None:
    pil = pytest.importorskip("PIL.Image")
    token = await dbx.token(dbx.MAYA)
    root = await dbx.root(token)
    photos = await dbx.list_all(token, "/Marketing & BD/Brand Assets/Photos", path_root=root)
    png = next(e for e in photos if e["name"].endswith(".png"))
    for fmt, size, expected in (("png", "w128h128", (128, 128)), ("jpeg", "w64h64", (64, 64))):
        headers = dbx.headers(token, path_root=root)
        headers["Dropbox-API-Arg"] = json.dumps(
            {
                "resource": {".tag": "path", "path": png["path_display"]},
                "format": {".tag": fmt},
                "size": {".tag": size},
            }
        )
        r = await dbx.client.post(f"{dbx.CONTENT}/2/files/get_thumbnail_v2", headers=headers)
        assert r.status_code == 200, r.text
        assert pil.open(io.BytesIO(r.content)).size == expected
        assert r.headers["content-type"] == f"image/{fmt}"
        dbx.check("files/get_thumbnail_v2", "result", json.loads(r.headers["dropbox-api-result"]))
    # a docx is not thumbnailable; a docx previews as PDF
    docs = await dbx.list_all(token, "/Notes")
    docx = next(e for e in docs if e["name"].endswith(".docx"))
    r, _ = await dbx.download(token, docx["path_display"], route="files/get_thumbnail")
    assert r.json()["error"][".tag"] == "unsupported_extension"
    r, meta = await dbx.download(token, docx["path_display"], route="files/get_preview")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert meta["id"] == docx["id"]


async def test_paper_docs_are_not_downloadable_but_exportable(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    root = await dbx.root(token)
    entries = await dbx.list_all(token, "/Knowledge Base/Paper", path_root=root)
    paper = entries[0]
    assert paper["is_downloadable"] is False
    assert paper["export_info"] == {"export_as": "markdown", "export_options": ["html", "markdown"]}
    hidden = await dbx.list_all(
        token, "/Knowledge Base/Paper", include_non_downloadable_files=False, path_root=root
    )
    assert hidden == []
    r, _ = await dbx.download(token, paper["path_display"], path_root=root)
    assert r.json()["error"][".tag"] == "unsupported_file"
    r, result = await dbx.download(token, paper["path_display"], route="files/export", path_root=root)
    assert r.status_code == 200 and r.text.startswith("# ")
    assert result["export_metadata"]["name"].endswith(".md")
    headers = dbx.headers(token, path_root=root)
    headers["Dropbox-API-Arg"] = json.dumps({"path": paper["path_display"], "export_format": "html"})
    r = await dbx.client.post(f"{dbx.CONTENT}/2/files/export", headers=headers)
    assert "<h1>" in r.text
    plain = (await dbx.list_all(token, "/Notes"))[0]
    r, _ = await dbx.download(token, plain["path_display"], route="files/export")
    assert r.json()["error"][".tag"] == "non_exportable"


async def test_content_hash_algorithm_spans_blocks(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    data = b"x" * (4 * 1024 * 1024 + 123)
    meta = await dbx.put(token, "/big.bin", data)
    assert meta["content_hash"] == content_hash(data)
    assert meta["size"] == len(data)
