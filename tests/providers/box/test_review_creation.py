"""Folder options must be validated before creating a persisted item."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.parametrize(
    "options",
    [
        {"folder_upload_email": {"access": "invalid"}},
        {"folder_upload_email": "invalid"},
        {"sync_state": "invalid"},
    ],
)
async def test_invalid_folder_options_do_not_create_items(box: Any, options: dict[str, Any]) -> None:
    parent = await box.mkdir("0", "Review parent")
    before = (await box.get(f"/folders/{parent}")).json()
    response = await box.post("/folders", json={"name": "Rejected", "parent": {"id": parent}, **options})
    assert response.status_code == 400, response.text
    assert (await box.get(f"/folders/{parent}")).json() == before
    retry = await box.post("/folders", json={"name": "Rejected", "parent": {"id": parent}})
    assert retry.status_code == 201, retry.text


async def test_folder_create_preserves_requested_sync_state_and_upload_email(box: Any) -> None:
    response = await box.post(
        "/folders",
        params={"fields": "sync_state,folder_upload_email"},
        json={
            "name": "Synced folder",
            "parent": {"id": "0"},
            "sync_state": "synced",
            "folder_upload_email": {"access": "collaborators"},
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["sync_state"] == "synced"
    assert response.json()["folder_upload_email"]["access"] == "collaborators"
    folder = (await box.get("/folders/" + response.json()["id"], params={"fields": "sync_state"})).json()
    assert folder["sync_state"] == "synced"
