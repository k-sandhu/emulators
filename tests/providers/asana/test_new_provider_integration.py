"""Reconcile the new projections through their public APIs and shared gateway."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from emulators.core.hub import discover_providers
from emulators.testing.helpers import bearer, mint

ASANA = "https://app.asana.com"
TRELLO = "https://api.trello.com"
AIRTABLE = "https://api.airtable.com"
NEW_PROVIDERS = ("asana", "trello", "airtable")
pytestmark = pytest.mark.e2e


async def api(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
    response = await client.request(method, url, **kwargs)
    assert response.is_success, f"{method} {url}: {response.status_code} {response.text[:1000]}"
    return response.json()


async def test_new_providers_share_world_people_and_have_distinct_routing(
    make_client: Callable[..., Any],
) -> None:
    discovered = discover_providers()
    assert set(NEW_PROVIDERS) <= set(discovered)
    ports = [provider.default_port for provider in discovered.values() if provider.default_port]
    assert len(ports) == len(set(ports))
    assert {name: discovered[name].default_port for name in NEW_PROVIDERS} == {
        "asana": 44117,
        "trello": 44118,
        "airtable": 44119,
    }
    async with make_client(*NEW_PROVIDERS) as client:
        world = client.hub.world
        expected_emails = {person.email for person in world.active_employees}
        expected_names = {person.display_name for person in world.active_employees}
        auth = {host: bearer(await mint(client, host, user="p001")) for host in (ASANA, TRELLO, AIRTABLE)}

        workspaces = await api(client, "GET", ASANA + "/api/1.0/workspaces", headers=auth[ASANA])
        workspace = next(row for row in workspaces["data"] if row["name"] == world.org.name)
        asana_people = await api(
            client,
            "GET",
            ASANA + f"/api/1.0/workspaces/{workspace['gid']}/users",
            headers=auth[ASANA],
            params={"limit": 100, "opt_fields": "name,email"},
        )
        assert {row["email"] for row in asana_people["data"]} == expected_emails

        organizations = await api(client, "GET", TRELLO + "/1/members/me/organizations", headers=auth[TRELLO])
        organization = next(row for row in organizations if row["displayName"] == world.org.name)
        trello_people = await api(
            client,
            "GET",
            TRELLO + f"/1/organizations/{organization['id']}/members",
            headers=auth[TRELLO],
            params={"fields": "fullName"},
        )
        assert {row["fullName"] for row in trello_people} == expected_names

        bases = await api(client, "GET", AIRTABLE + "/v0/meta/bases", headers=auth[AIRTABLE])
        operations = next(row for row in bases["bases"] if row["name"].endswith("Client Operations"))
        airtable_people = await api(
            client,
            "GET",
            AIRTABLE + f"/v0/{operations['id']}/Employees",
            headers=auth[AIRTABLE],
            params={"pageSize": 100},
        )
        assert {
            row["fields"]["Email"] for row in airtable_people["records"] if row["fields"].get("Active")
        } == expected_emails
        employee_emails = {row["id"]: row["fields"]["Email"] for row in airtable_people["records"]}
        world_people = {person.email: person for person in world.employees}
        for row in airtable_people["records"]:
            person = world_people[row["fields"]["Email"]]
            manager_emails = [employee_emails[key] for key in row["fields"].get("Manager", [])]
            assert manager_emails == ([world.person(person.manager_id).email] if person.manager_id else [])

        for source, target, path in (
            (ASANA, TRELLO, "/1/members/me"),
            (TRELLO, AIRTABLE, "/v0/meta/bases"),
            (AIRTABLE, ASANA, "/api/1.0/users/me"),
        ):
            response = await client.get(target + path, headers=auth[source])
            assert response.status_code == 401, response.text


async def test_gateway_reset_restores_all_three_provider_projections(make_client: Callable[..., Any]) -> None:
    async with make_client(*NEW_PROVIDERS) as client:
        auth = {host: bearer(await mint(client, host, user="p001")) for host in (ASANA, TRELLO, AIRTABLE)}
        workspaces = await api(client, "GET", ASANA + "/api/1.0/workspaces", headers=auth[ASANA])
        workspace = next(row for row in workspaces["data"] if row["name"] == client.hub.world.org.name)
        project = await api(
            client,
            "POST",
            ASANA + "/api/1.0/projects",
            headers=auth[ASANA],
            json={"data": {"workspace": workspace["gid"], "name": "Cross-provider reset check"}},
        )
        board = await api(
            client,
            "POST",
            TRELLO + "/1/boards",
            headers=auth[TRELLO],
            json={"name": "Cross-provider reset check"},
        )
        bases = await api(client, "GET", AIRTABLE + "/v0/meta/bases", headers=auth[AIRTABLE])
        operations = next(row for row in bases["bases"] if row["name"].endswith("Client Operations"))
        records_url = AIRTABLE + f"/v0/{operations['id']}/Clients"
        record = await api(
            client,
            "POST",
            records_url,
            headers=auth[AIRTABLE],
            json={"fields": {"Name": "Cross-provider reset check"}},
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.hub.gateway)) as gateway:
            reset = await api(gateway, "POST", "http://localhost/_emulator/reset")
        assert set(reset["providers"]) == set(NEW_PROVIDERS)
        # Reset revokes the newly issued grants too; remint before checking the restored stores.
        auth = {host: bearer(await mint(client, host, user="p001")) for host in (ASANA, TRELLO, AIRTABLE)}
        for host, path in (
            (ASANA, "/api/1.0/projects/" + project["data"]["gid"]),
            (TRELLO, "/1/boards/" + board["id"]),
            (AIRTABLE, f"/v0/{operations['id']}/Clients/" + record["id"]),
        ):
            response = await client.get(host + path, headers=auth[host])
            assert response.status_code == 404, response.text


async def test_asana_pagination_stays_on_the_gateway_prefix(make_hub: Callable[..., Any]) -> None:
    hub = make_hub(*NEW_PROVIDERS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hub.gateway)) as client:
        base = "http://localhost/asana"
        auth = bearer(await mint(client, base, user="p001"))
        workspaces = await api(client, "GET", base + "/api/1.0/workspaces", headers=auth)
        workspace = next(row for row in workspaces["data"] if row["name"] == hub.world.org.name)
        page = await api(
            client,
            "GET",
            base + f"/api/1.0/workspaces/{workspace['gid']}/users",
            headers=auth,
            params={"limit": 1, "opt_fields": "name,email"},
        )
        assert page["next_page"]["uri"].startswith(base + "/api/1.0/")
        following = await api(client, "GET", page["next_page"]["uri"], headers=auth)
        assert page["data"][0]["gid"] != following["data"][0]["gid"]
        assert following["data"][0]["email"] in {p.email for p in hub.world.active_employees}
