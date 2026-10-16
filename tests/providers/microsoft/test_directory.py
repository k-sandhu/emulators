from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from emulators.providers.microsoft import common as c
from emulators.providers.microsoft.directory import DEFAULT_USER
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


async def test_default_selection_advanced_queries_and_paging(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        result = await client.get("/v1.0/users", params={"$top": 3, "$orderby": "displayName"})
        assert set(result.json()["value"][0]) == set(DEFAULT_USER)
        assert len(result.json()["value"]) == 3
        assert (await client.get(result.json()["@odata.nextLink"])).json()["value"][0] != result.json()[
            "value"
        ][0]
        for query in ({"$search": '"displayName:maya"'}, {"$count": "true"}):
            assert (await client.get("/v1.0/users", params=query)).status_code == 400
            assert (
                await client.get("/beta/users", params=query, headers={"ConsistencyLevel": "eventual"})
            ).status_code == 200
        for expression in (
            "startswith(displayName,'Ma')",
            "mail eq 'maya.chen@halcyonadvisory.example'",
            "accountEnabled eq false",
            "userType eq 'Guest'",
            "department eq 'Advisory'",
        ):
            result = await client.get(
                "/v1.0/users",
                params={"$filter": expression, "$select": "id,accountEnabled,userType,department,mail"},
            )
            assert result.status_code == 200 and result.json()["value"], result.text
        result = await client.get("/v1.0/users", params={"$filter": "not valid expression"})
        assert result.status_code == 400
        assert (await client.get("/v1.0/users", params={"$top": 0})).json()["value"] == []


async def test_directory_relationships_photos_and_objects(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        uid = c.user_id(client.hub.world, "p018")
        for path in ("/me", "/users/maya.chen@halcyonadvisory.example", f"/users/{uid}"):
            result = await client.get("/v1.0" + path, params={"$expand": "manager($select=id,displayName)"})
            assert result.status_code == 200 and result.json()["manager"]["displayName"], result.text
        result = await client.get(f"/v1.0/users/{uid}", params={"$expand": "manager($levels=max)"})
        assert result.json()["manager"]
        for suffix in (
            "manager",
            "directReports",
            "memberOf",
            "transitiveMemberOf",
            "ownedObjects",
            "photo",
            "photos",
            "photo/$value",
        ):
            result = await client.get(f"/v1.0/users/{uid}/{suffix}")
            assert result.status_code == 200, (suffix, result.text)
            if suffix == "photo/$value":
                assert result.content.startswith(b"\x89PNG")
        absent = c.user_id(client.hub.world, "p047")
        assert (await client.get(f"/v1.0/users/{absent}/photo/$value")).status_code == 404
        for suffix in ("organization", "domains", "subscribedSkus"):
            assert (await client.get("/v1.0/" + suffix)).json()["value"]
        group = (await client.get("/v1.0/me/memberOf")).json()["value"][0]
        assert (await client.get(f"/v1.0/directoryObjects/{uid}")).json()[
            "@odata.type"
        ] == "#microsoft.graph.user"
        response = await client.post(
            "/v1.0/directoryObjects/getByIds", json={"ids": [uid, group["id"]], "types": ["user"]}
        )
        assert len(response.json()["value"]) == 1
        checked = await client.post(f"/v1.0/users/{uid}/checkMemberGroups", json={"groupIds": [group["id"]]})
        assert checked.json()["value"] == [group["id"]]
        assert (
            await client.post(f"/v1.0/users/{uid}/getMemberGroups", json={"securityEnabledOnly": False})
        ).json()["value"]


async def test_admin_crud_and_resource_scoped_delta(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        uid = c.user_id(client.hub.world, "p018")
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        assert (await client.patch(f"/v1.0/users/{uid}", json={"jobTitle": "Principal"})).status_code == 403
        client.headers.update(bearer(await mint(client, BASE, user="p035")))
        delta = (await client.get("/v1.0/users/delta")).json()["@odata.deltaLink"]
        await client.patch(f"/v1.0/users/{uid}", json={"jobTitle": "Principal"})
        await client.post(
            "/v1.0/users/maya.chen@halcyonadvisory.example/messages", json={"subject": "Delta isolation"}
        )
        response = (await client.get(delta)).json()
        assert [r["id"] for r in response["value"]] == [uid]
        payload = {
            "accountEnabled": True,
            "displayName": "Synthetic Test",
            "mailNickname": "synthetic",
            "userPrincipalName": "synthetic@halcyonadvisory.example",
            "passwordProfile": {"password": "synthetic"},
        }
        created = await client.post("/v1.0/users", json=payload)
        assert created.status_code == 201, created.text
        new_id = created.json()["id"]
        assert "passwordProfile" not in created.json()
        assert (await client.delete(f"/v1.0/users/{new_id}")).status_code == 204
        assert (await client.get(f"/v1.0/users/{new_id}")).status_code == 404


async def test_group_membership_owner_and_admin_denials(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        world = client.hub.world
        group = world.groups[0]
        gid = c.group_id(world, group.id)
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        for suffix in ("", "/members", "/owners", "/transitiveMembers"):
            assert (await client.get(f"/v1.0/groups/{gid}{suffix}")).status_code == 200
        unified = await client.get("/v1.0/groups", params={"$filter": "groupTypes/any(c:c eq 'Unified')"})
        assert all(g["groupTypes"] == ["Unified"] for g in unified.json()["value"])
        uid = c.user_id(world, "p018")
        payload = {"@odata.id": f"{BASE}/v1.0/directoryObjects/{uid}"}
        assert (await client.post(f"/v1.0/groups/{gid}/members/$ref", json=payload)).status_code == 403
        client.headers.update(bearer(await mint(client, BASE, user=group.owner_ids[0])))
        link = (await client.get("/v1.0/groups/delta")).json()["@odata.deltaLink"]
        assert (await client.post(f"/v1.0/groups/{gid}/members/$ref", json=payload)).status_code == 204
        assert (await client.get(link)).json()["value"][0]["id"] == gid
        assert (await client.delete(f"/v1.0/groups/{gid}/members/{uid}/$ref")).status_code == 204


async def test_delta_paging_snapshot_and_cross_resource_token_rejection(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, app_only=True)))
        response = (await client.get("/v1.0/users/delta", params={"$top": 10})).json()
        first = list(response["value"])
        uid = first[0]["id"]
        await client.patch(f"/v1.0/users/{uid}", json={"jobTitle": "Changed during paging"})
        assert (
            await client.get(response["@odata.nextLink"].replace("users/delta", "groups/delta"))
        ).status_code == 400
        while "@odata.nextLink" in response:
            response = (await client.get(response["@odata.nextLink"])).json()
            first.extend(response["value"])
        assert len(first) == 68 and len({r["id"] for r in first}) == 68
        changed = (await client.get(response["@odata.deltaLink"])).json()
        assert [r["id"] for r in changed["value"]] == [uid]
        assert changed["value"][0]["jobTitle"] == "Changed during paging"


@pytest.mark.parametrize("version", ["v1.0", "beta"])
async def test_headers_auth_faults_and_guest_denial(make_client: Callable[..., Any], version: str) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        result = await client.get(f"/{version}/users", headers={"client-request-id": "correlation"})
        assert result.status_code == 401 and result.headers["client-request-id"] == "correlation"
        access = await mint(client, BASE, user="p018")
        client.headers.update(bearer(access))
        await client.post("/_emulator/faults", json={"path": "/users", "status": 429})
        result = await client.get(f"/{version}/users", headers={"client-request-id": "throttle"})
        assert result.status_code == 429 and result.headers["request-id"]
        assert result.headers["client-request-id"] == "throttle"
        guest = next(cl.primary_contact_id for cl in client.hub.world.clients if cl.status == "Active")
        result = await client.get(f"/{version}/users", headers=bearer(await mint(client, BASE, user=guest)))
        assert result.status_code == 403
