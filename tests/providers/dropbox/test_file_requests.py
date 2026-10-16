"""file_requests/*: lifecycle, deadlines and simulated uploads."""

from __future__ import annotations

from datetime import timedelta
from typing import Any


async def test_file_request_lifecycle(dbx: Any, hub: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    await dbx.ok(maya, "files/create_folder_v2", {"path": "/Requests/Receipts"})
    created = await dbx.ok(
        maya,
        "file_requests/create",
        {"title": "Q3 receipts", "destination": "/Requests/Receipts", "description": "Please upload"},
    )
    assert (
        created["is_open"] is True
        and created["file_count"] == 0
        and created["destination"] == "/Requests/Receipts"
    )
    assert created["url"] == f"https://www.dropbox.com/request/{created['id']}"
    page = await dbx.client.get(created["url"])
    assert page.status_code == 200 and "Q3 receipts" in page.text
    assert (await dbx.ok(maya, "file_requests/count"))["file_request_count"] == 1
    # an outsider "uploads" through the public page; the file lands in the destination
    r = await dbx.client.post(
        f"{dbx.API}/_emulator/actions/simulate_file_request_upload",
        json={"id": created["id"], "name": "receipt.pdf", "content": "pdf"},
    )
    assert r.status_code == 200 and r.json()["file_count"] == 1
    assert [e["name"] for e in await dbx.list_all(maya, "/Requests/Receipts")] == ["receipt.pdf"]
    got = await dbx.ok(maya, "file_requests/get", {"id": created["id"]})
    assert got["file_count"] == 1
    updated = await dbx.ok(
        maya,
        "file_requests/update",
        {
            "id": created["id"],
            "title": "Q3 receipts (final)",
            "deadline": {
                ".tag": "update",
                "update": {"deadline": "2030-01-01T00:00:00Z", "allow_late_uploads": {".tag": "seven_days"}},
            },
        },
    )
    assert updated["title"].endswith("(final)") and updated["deadline"]["allow_late_uploads"] == {
        ".tag": "seven_days"
    }
    stuck = await dbx.err(maya, "file_requests/delete", {"ids": [created["id"]]})
    assert stuck["error"][".tag"] == "file_request_open"
    closed = await dbx.ok(maya, "file_requests/update", {"id": created["id"], "open": False})
    assert closed["is_open"] is False
    assert (await dbx.client.get(created["url"])).status_code == 410
    rejected = await dbx.client.post(
        f"{dbx.API}/_emulator/actions/simulate_file_request_upload",
        json={"id": created["id"], "name": "late.pdf"},
    )
    assert rejected.status_code == 409
    listed = await dbx.ok(maya, "file_requests/list_v2", {"limit": 1})
    assert len(listed["file_requests"]) == 1 and listed["has_more"] is False
    assert (await dbx.ok(maya, "file_requests/list"))["file_requests"][0]["id"] == created["id"]
    gone = await dbx.ok(maya, "file_requests/delete_all_closed")
    assert [x["id"] for x in gone["file_requests"]] == [created["id"]]
    missing = await dbx.err(maya, "file_requests/get", {"id": created["id"]})
    assert missing["error"][".tag"] == "not_found"


async def test_file_request_validation_and_deadlines(dbx: Any, hub: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    await dbx.put(maya, "/Requests/file.txt", b"x")
    for body, tag in (
        ({"title": "x", "destination": "/nope"}, "not_found"),
        ({"title": "x", "destination": "/Requests/file.txt"}, "not_a_folder"),
        ({"title": " ", "destination": "/Requests"}, "validation_error"),
        (
            {"title": "x", "destination": "/Requests", "deadline": {"deadline": "2001-01-01T00:00:00Z"}},
            "validation_error",
        ),
    ):
        err = await dbx.err(maya, "file_requests/create", body)
        assert err["error"][".tag"] == tag, body
    root = await dbx.root(maya)
    denied = await dbx.err(
        maya, "file_requests/create", {"title": "x", "destination": "/Finance & Operations"}, path_root=root
    )
    assert denied["error"][".tag"] == "no_write_permission"
    soon = await dbx.ok(
        maya,
        "file_requests/create",
        {
            "title": "Soon",
            "destination": "/Requests",
            "deadline": {"deadline": (hub.clock.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        },
    )
    assert soon["is_open"] is True
    hub.clock.advance(timedelta(hours=2))
    try:
        assert (await dbx.ok(maya, "file_requests/get", {"id": soon["id"]}))["is_open"] is False
    finally:
        hub.clock.advance(timedelta(hours=-2))
