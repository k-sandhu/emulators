from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest

from emulators.providers.asana.common import gid

from .conftest import BASE, project, task, workspace


async def test_task_lifecycle_completed_dates_subtasks_and_delete(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    row = await task(client, name="Prepare audit", assignee="me", due_on="2026-11-20", start_on="2026-11-01")
    tid = row["gid"]
    assert row["completed"] is False and row["assignee"]["gid"] == gid("user", "p018")
    done = await client.put(BASE + f"/tasks/{tid}", json={"data": {"completed": True, "name": "Reviewed"}})
    assert done.status_code == 200, done.text
    assert done.json()["data"]["completed_at"] and done.json()["data"]["completed_by"]["gid"] == gid(
        "user", "p018"
    )
    child = await client.post(BASE + f"/tasks/{tid}/subtasks", json={"data": {"name": "Check evidence"}})
    assert child.status_code == 201, child.text
    assert child.json()["data"]["projects"] == []
    assert child.json()["data"]["parent"]["gid"] == tid
    assert (await client.get(BASE + f"/tasks/{tid}")).json()["data"]["num_subtasks"] == 1
    cycle = await client.post(
        BASE + f"/tasks/{tid}/setParent", json={"data": {"parent": child.json()["data"]["gid"]}}
    )
    assert cycle.status_code == 400
    reopened = await client.put(
        BASE + f"/tasks/{tid}", json={"data": {"completed": False, "due_at": "2026-11-20T14:00:00Z"}}
    )
    assert reopened.json()["data"]["completed_at"] is None and reopened.json()["data"]["due_on"] is None
    assert (await client.delete(BASE + f"/tasks/{tid}")).json() == {"data": {}}
    assert (await client.get(BASE + f"/tasks/{tid}")).status_code == 404


async def test_private_project_visibility_and_task_sharing_redacts_memberships(
    asana_client: httpx.AsyncClient,
) -> None:
    client = asana_client
    p = await project(client, privacy_setting="private")
    t = await task(client, projects=[p["gid"]])
    other = {"Authorization": "Bearer asana-dev-p035"}
    assert (await client.get(BASE + f"/projects/{p['gid']}", headers=other)).status_code == 403
    assert (await client.get(BASE + f"/tasks/{t['gid']}", headers=other)).status_code == 403
    assert (
        await client.put(BASE + f"/tasks/{t['gid']}", headers=other, json={"data": {"completed": True}})
    ).status_code == 403
    shared = await client.post(
        BASE + f"/tasks/{t['gid']}/addFollowers", json={"data": {"followers": [gid("user", "p035")]}}
    )
    assert shared.status_code == 200
    visible = (await client.get(BASE + f"/tasks/{t['gid']}", headers=other)).json()["data"]
    assert visible["projects"] == [] and visible["memberships"] == []
    assert (await client.get(BASE + f"/projects/{p['gid']}", headers=other)).status_code == 403
    assert (
        await client.post(
            BASE + f"/projects/{p['gid']}/addMembers", json={"data": {"members": [gid("user", "p035")]}}
        )
    ).status_code == 200
    assert (await client.get(BASE + f"/tasks/{t['gid']}", headers=other)).json()["data"]["projects"][0][
        "gid"
    ] == p["gid"]


async def test_project_section_moves_ordering_members_and_deletion(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    p = await project(client)
    pid = p["gid"]
    sections = []
    for name in ("Backlog", "Done"):
        response = await client.post(BASE + f"/projects/{pid}/sections", json={"data": {"name": name}})
        assert response.status_code == 201, response.text
        sections.append(response.json()["data"]["gid"])
    first = await task(client, name="First", projects=[pid])
    second = await task(client, name="Second", projects=[pid])
    moved = await client.post(
        BASE + f"/sections/{sections[1]}/addTask",
        json={"data": {"task": second["gid"], "insert_before": first["gid"]}},
    )
    assert moved.status_code == 200, moved.text
    listed = (await client.get(BASE + f"/projects/{pid}/tasks")).json()["data"]
    assert [t["gid"] for t in listed] == [second["gid"], first["gid"]]
    selected = (await client.get(BASE + f"/sections/{sections[1]}/tasks")).json()["data"]
    assert [t["gid"] for t in selected] == [second["gid"]]
    assert (await client.delete(BASE + f"/sections/{sections[1]}")).status_code == 200
    assert (await client.get(BASE + f"/tasks/{second['gid']}")).json()["data"]["memberships"][0][
        "section"
    ] is None
    assert (await client.delete(BASE + f"/projects/{pid}")).json() == {"data": {}}
    assert (await client.get(BASE + f"/tasks/{first['gid']}")).json()["data"]["projects"] == []


@pytest.mark.parametrize("params", [{}, {"workspace": "@workspace"}, {"assignee": "me"}, {"section": "1"}])
async def test_task_list_requires_vendor_filter_combination(
    asana_client: httpx.AsyncClient, params: dict[str, str]
) -> None:
    client = asana_client
    if params.get("workspace") == "@workspace":
        params = {"workspace": await workspace(client)}
    response = await client.get(BASE + "/tasks", params=params)
    assert response.status_code == 400, response.text


async def test_task_list_assignee_workspace_tag_and_modified_filters(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    wid = await workspace(client)
    row = await task(client, assignee="me", completed=True)
    listed = (
        await client.get(
            BASE + "/tasks",
            params={"workspace": wid, "assignee": "me", "opt_fields": "name,completed,assignee.email"},
        )
    ).json()["data"]
    assert row["gid"] in {item["gid"] for item in listed}
    future = await client.get(
        BASE + "/tasks",
        params={"workspace": wid, "assignee": "me", "completed_since": "2099-01-01T00:00:00Z"},
    )
    assert row["gid"] not in {item["gid"] for item in future.json()["data"]}
    assert (
        await client.get(
            BASE + "/tasks", params={"workspace": wid, "assignee": "me", "modified_since": "bad"}
        )
    ).status_code == 400
    tag = (await client.post(BASE + "/tags", json={"data": {"name": "Reviewed", "workspace": wid}})).json()[
        "data"
    ]
    assert (
        await client.post(BASE + f"/tasks/{row['gid']}/addTag", json={"data": {"tag": tag["gid"]}})
    ).status_code == 200
    assert [
        item["gid"] for item in (await client.get(BASE + "/tasks", params={"tag": tag["gid"]})).json()["data"]
    ] == [row["gid"]]


@pytest.mark.parametrize(
    "changes",
    [
        {"completed": "false"},
        {"completed": 1},
        {"completed": None},
        {"assignee": []},
        {"due_on": "2026-02-30"},
        {"due_at": "2026-11-01"},
        {"due_at": "2026-11-01T00:00:00"},
        {"due_on": "2026-11-01", "due_at": "2026-11-01T00:00:00Z"},
        {"start_on": "2026-12-01", "due_on": "2026-11-01"},
        pytest.param({"start_on": "2026-11-01"}, id="start_on_requires_due"),
        pytest.param({"start_on": None}, id="clear_start_on_requires_due"),
        pytest.param({"start_at": "2026-11-01T12:00:00Z"}, id="start_at_requires_due_at"),
        {"followers": [None]},
        {"name": 100},
        {"name": "\ud800"},
        {"notes": float("nan")},
        {"notes": float("inf")},
        {"html_notes": "<body>broken"},
        {"resource_subtype": "milestone", "start_on": "2026-01-01"},
        {"projects": []},
        {"workspace": "other"},
        {"custom_fields": {}},
    ],
)
async def test_invalid_task_changes_are_atomic(
    asana_client: httpx.AsyncClient, changes: dict[str, Any]
) -> None:
    client = asana_client
    row = await task(client, name="Original")
    response = await client.put(
        BASE + f"/tasks/{row['gid']}",
        content=json.dumps({"data": {"name": "Must not persist", **changes}}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400, response.text
    assert (await client.get(BASE + f"/tasks/{row['gid']}")).json()["data"] == row


async def test_dependency_cycles_and_missing_bulk_targets_are_atomic(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    a, b, z = await task(client, name="A"), await task(client, name="B"), await task(client, name="C")
    for source, target in ((a, b), (b, z)):
        response = await client.post(
            BASE + f"/tasks/{source['gid']}/addDependencies", json={"data": {"dependencies": [target["gid"]]}}
        )
        assert response.status_code == 200, response.text
    assert (
        await client.post(
            BASE + f"/tasks/{z['gid']}/addDependencies", json={"data": {"dependencies": [a["gid"]]}}
        )
    ).status_code == 400
    assert (await client.get(BASE + f"/tasks/{z['gid']}/dependencies")).json()["data"] == []
    before = copy.deepcopy((await client.get(BASE + f"/tasks/{z['gid']}")).json()["data"])
    assert (
        await client.post(
            BASE + f"/tasks/{z['gid']}/addDependencies",
            json={"data": {"dependencies": [b["gid"], "missing"]}},
        )
    ).status_code == 404
    assert (await client.get(BASE + f"/tasks/{z['gid']}")).json()["data"] == before
    assert [t["gid"] for t in (await client.get(BASE + f"/tasks/{z['gid']}/dependents")).json()["data"]] == [
        b["gid"]
    ]


async def test_form_encoded_tasks_and_body_field_options(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    created = await client.post(
        BASE + "/tasks", data={"name": "Form ✓", "workspace": await workspace(client), "completed": "true"}
    )
    assert created.status_code == 201 and created.json()["data"]["completed"] is True
    row = await client.put(
        BASE + f"/tasks/{created.json()['data']['gid']}",
        json={"data": {"name": "JSON ✓"}, "options": {"fields": ["name", "completed"]}},
    )
    assert row.json()["data"] == {"gid": created.json()["data"]["gid"], "name": "JSON ✓", "completed": True}


async def test_approval_status_and_completion_stay_synchronized(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    row = await task(client, resource_subtype="approval")
    assert row.get("approval_status") == "pending" and row["completed"] is False
    for status in ("approved", "rejected", "changes_requested", "pending"):
        response = await client.put(BASE + f"/tasks/{row['gid']}", json={"data": {"approval_status": status}})
        assert response.status_code == 200, response.text
        current = response.json()["data"]
        assert current["completed"] is (status != "pending")
        assert bool(current["completed_at"]) is (status != "pending")
    for completed in (True, False):
        current = (
            await client.put(BASE + f"/tasks/{row['gid']}", json={"data": {"completed": completed}})
        ).json()["data"]
        assert current["approval_status"] == ("approved" if completed else "pending")
    created = await task(client, resource_subtype="approval", completed=True)
    assert created["approval_status"] == "approved" and created["completed_at"]


async def test_private_task_creator_loses_access_after_unfollowing(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    token = (await client.post("https://app.asana.com/_emulator/tokens", json={"user": "p001"})).json()[
        "access_token"
    ]
    creator = {"Authorization": "Bearer " + token}
    created = await client.post(
        BASE + "/tasks",
        headers=creator,
        json={
            "data": {
                "name": "Private handoff",
                "workspace": await workspace(client),
                "assignee": gid("user", "p018"),
            }
        },
    )
    assert created.status_code == 201
    tid = created.json()["data"]["gid"]
    removed = await client.post(
        BASE + f"/tasks/{tid}/removeFollowers",
        headers=creator,
        json={"data": {"followers": [gid("user", "p001")]}},
    )
    assert removed.status_code == 200 and removed.json()["data"]["followers"] == []
    assert (await client.get(BASE + f"/tasks/{tid}", headers=creator)).status_code == 403
    assert (await client.get(BASE + f"/tasks/{tid}")).status_code == 200


async def test_dependency_limit_applies_to_both_sides_and_bulk_additions_are_atomic(
    asana_client: httpx.AsyncClient,
) -> None:
    client = asana_client
    center = await task(client)
    others = [await task(client, name=f"Dependent {i}") for i in range(31)]
    response = await client.post(
        BASE + f"/tasks/{center['gid']}/addDependents",
        json={"data": {"dependents": [row["gid"] for row in others]}},
    )
    assert response.status_code == 400, response.text
    assert (await client.get(BASE + f"/tasks/{center['gid']}/dependents")).json()["data"] == []
    response = await client.post(
        BASE + f"/tasks/{center['gid']}/addDependents",
        json={"data": {"dependents": [row["gid"] for row in others[:30]]}},
    )
    assert response.status_code == 200
    response = await client.post(
        BASE + f"/tasks/{others[30]['gid']}/addDependencies", json={"data": {"dependencies": [center["gid"]]}}
    )
    assert response.status_code == 400, response.text
    assert (await client.get(BASE + f"/tasks/{others[30]['gid']}/dependencies")).json()["data"] == []


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null", b'{"data":null}', b'{"data":[]}', b"{}"])
async def test_malformed_create_payloads_do_not_add_tasks(
    asana_client: httpx.AsyncClient, body: bytes
) -> None:
    client = asana_client
    before = len(client.hub.context("asana").store["tasks"])
    response = await client.post(BASE + "/tasks", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and "errors" in response.json()
    assert len(client.hub.context("asana").store["tasks"]) == before
