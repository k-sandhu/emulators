from __future__ import annotations

from typing import Any

import httpx
import pytest

from emulators.providers.asana.common import gid

from .conftest import BASE, project, workspace


async def test_seed_projects_shared_world_staff_and_stable_ids(
    asana_client: httpx.AsyncClient, make_hub: Any
) -> None:
    client = asana_client
    me = (await client.get(BASE + "/users/me")).json()["data"]
    assert me["gid"] == gid("user", "p018") and me["email"] == "maya.chen@halcyonadvisory.example"
    wid = await workspace(client)
    rows = (await client.get(BASE + f"/workspaces/{wid}/users", params={"opt_fields": "name,email"})).json()[
        "data"
    ]
    assert {row["email"] for row in rows} == {person.email for person in client.hub.world.active_employees}
    assert all(set(row) == {"gid", "name", "email"} and row["gid"].isdigit() for row in rows)
    assert (await client.get(BASE + f"/workspaces/{wid}")).json()["data"]["name"] == client.hub.world.org.name
    other = make_hub("asana")
    assert {u["gid"] for u in other.context("asana").store["users"]} == {row["gid"] for row in rows}
    assert len(other.context("asana").store["projects"]) == len(other.world.engagements)


async def test_nested_opt_fields_compact_defaults_and_group_syntax(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    created = await project(client)
    full = await client.get(
        BASE + f"/projects/{created['gid']}", params={"opt_fields": "name,owner.email,workspace.name"}
    )
    row = full.json()["data"]
    assert set(row) == {"gid", "name", "owner", "workspace"}
    assert row["owner"] == {"gid": gid("user", "p018"), "email": "maya.chen@halcyonadvisory.example"}
    assert set(row["workspace"]) == {"gid", "name"}
    listed = (await client.get(BASE + "/projects")).json()["data"]
    assert all(set(row) == {"gid", "resource_type", "name"} for row in listed)
    grouped = await client.get(
        BASE + f"/projects/{created['gid']}", params={"opt_fields": "(owner|followers).email"}
    )
    assert (
        grouped.status_code == 200
        and grouped.json()["data"]["followers"][0]["email"] == "maya.chen@halcyonadvisory.example"
    )


@pytest.mark.parametrize("value", ["0", "101", "-1", "true", "1.5", "99999999999999999"])
async def test_invalid_paging_limits(asana_client: httpx.AsyncClient, value: str) -> None:
    response = await asana_client.get(BASE + "/users", params={"limit": value})
    assert response.status_code == 400 and "errors" in response.json()


async def test_opaque_offsets_are_query_and_user_bound_and_reset_invalidates(
    asana_client: httpx.AsyncClient,
) -> None:
    client = asana_client
    first = (await client.get(BASE + "/users", params={"limit": 2, "opt_fields": "email"})).json()
    assert len(first["data"]) == 2 and first["next_page"]["path"].startswith("/api/1.0/users?")
    second = (await client.get(first["next_page"]["uri"])).json()
    assert {row["gid"] for row in first["data"]}.isdisjoint(row["gid"] for row in second["data"])
    assert (
        await client.get(first["next_page"]["uri"], headers={"Authorization": "Bearer asana-dev-p035"})
    ).status_code == 400
    assert (
        await client.get(BASE + "/users", params={"offset": first["next_page"]["offset"], "limit": 2})
    ).status_code == 400
    assert (await client.get(BASE + "/users", params={"offset": "fabricated"})).status_code == 400
    assert (await client.post("https://app.asana.com/_emulator/reset")).status_code == 200
    assert (await client.get(first["next_page"]["uri"])).status_code == 400


async def test_team_membership_controls_private_team_access(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    created = await client.post(
        BASE + "/teams", json={"data": {"name": "Private team", "organization": await workspace(client)}}
    )
    assert created.status_code == 201, created.text
    tid = created.json()["data"]["gid"]
    other = {"Authorization": "Bearer asana-dev-p035"}
    assert (await client.get(BASE + f"/teams/{tid}", headers=other)).status_code == 403
    assert (
        await client.post(BASE + f"/teams/{tid}/addUser", json={"data": {"user": gid("user", "p035")}})
    ).status_code == 200
    assert (await client.get(BASE + f"/teams/{tid}/users", headers=other)).status_code == 200
    assert (
        await client.post(BASE + f"/teams/{tid}/removeUser", json={"data": {"user": gid("user", "p035")}})
    ).status_code == 200
    assert (await client.get(BASE + f"/teams/{tid}", headers=other)).status_code == 403


@pytest.mark.parametrize("field", ["unknown", "name,", "owner..email", "owner.unknown", "(owner|).email"])
async def test_invalid_projection_is_rejected_before_project_creation(
    asana_client: httpx.AsyncClient, field: str
) -> None:
    client = asana_client
    before = len(client.hub.context("asana").store["projects"])
    response = await client.post(
        BASE + "/projects",
        params={"opt_fields": field},
        json={"data": {"name": "Rejected", "workspace": await workspace(client)}},
    )
    assert response.status_code == 400
    assert len(client.hub.context("asana").store["projects"]) == before
