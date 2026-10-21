from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from .conftest import BASE, project, task


async def test_legacy_public_flag_changes_project_visibility(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    row = await project(client, public=True)
    other = {"Authorization": "Bearer asana-dev-p035"}
    assert (await client.get(BASE + f"/projects/{row['gid']}", headers=other)).status_code == 200
    response = await client.put(BASE + f"/projects/{row['gid']}", json={"data": {"public": False}})
    assert response.status_code == 200 and response.json()["data"]["public"] is False
    assert (await client.get(BASE + f"/projects/{row['gid']}", headers=other)).status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"archived": 1},
        {"public": "false"},
        {"privacy_setting": []},
        {"owner": []},
        {"owner": False},
        {"due_on": "2026-02-30"},
        {"start_on": "2026-12-01", "due_on": "2026-11-01"},
        pytest.param({"start_on": "2026-11-01"}, id="start_on_requires_due"),
        pytest.param({"start_on": None}, id="clear_start_on_requires_due"),
        pytest.param({"start_on": "2026-11-01", "due_on": "2026-11-01"}, id="start_due_cannot_match"),
        {"name": "\ud800"},
        {"notes": float("inf")},
        {"default_view": "unsupported"},
    ],
)
async def test_invalid_project_changes_are_atomic(
    asana_client: httpx.AsyncClient, changes: dict[str, Any]
) -> None:
    client = asana_client
    row = await project(client)
    response = await client.put(
        BASE + f"/projects/{row['gid']}",
        content=json.dumps({"data": {"name": "Must not persist", **changes}}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400, response.text
    assert (await client.get(BASE + f"/projects/{row['gid']}")).json()["data"] == row


async def test_start_dates_can_be_cleared_with_due_dates_in_same_request(
    asana_client: httpx.AsyncClient,
) -> None:
    client = asana_client
    for collection, creator in (("projects", project), ("tasks", task)):
        row = await creator(client, start_on="2026-11-01", due_on="2026-11-02")
        cleared = await client.put(
            BASE + f"/{collection}/{row['gid']}", json={"data": {"start_on": None, "due_on": None}}
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["data"]["start_on"] is None and cleared.json()["data"]["due_on"] is None
    row = await task(client, start_at="2026-11-01T12:00:00Z", due_at="2026-11-01T13:00:00Z")
    cleared = await client.put(
        BASE + f"/tasks/{row['gid']}", json={"data": {"start_at": None, "due_at": None}}
    )
    assert cleared.status_code == 200 and cleared.json()["data"]["start_at"] is None
