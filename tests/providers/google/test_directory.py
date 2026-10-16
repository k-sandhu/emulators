from __future__ import annotations

from typing import Any

import pytest

from emulators.testing.helpers import bearer, mint

BASE = "https://admin.googleapis.com/admin/directory/v1"


async def test_directory_user_shape_pagination_queries_and_public_view(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p035"))
        first = await client.get(
            BASE + "/users",
            params={"customer": "my_customer", "maxResults": 3, "orderBy": "email"},
            headers=auth,
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert len(body["users"]) == 3
        second = (
            await client.get(
                BASE + "/users",
                params={"customer": "my_customer", "maxResults": 3, "pageToken": body["nextPageToken"]},
                headers=auth,
            )
        ).json()
        assert not {r["id"] for r in body["users"]} & {r["id"] for r in second["users"]}
        for query, expected in (
            ("name:'Maya*'", ["maya.chen"]),
            ("email:maya*", ["maya.chen"]),
            ("orgUnitPath='/Advisory/London'", ["nadia.petrova", "samuel.osei"]),
            ("isAdmin=true", ["arjun.mehta", "sarah.johansson"]),
            ("isSuspended=true", ["paul.henderson", "yuki.tanaka"]),
            ("directManager='maya.chen@halcyonadvisory.example'", ["benjamin.adler"]),
        ):
            response = await client.get(
                BASE + "/users", params={"customer": "my_customer", "query": query}, headers=auth
            )
            assert response.status_code == 200, response.text
            assert sorted(
                r["primaryEmail"].split("@")[0] for r in response.json().get("users", [])
            ) == sorted(expected)
        maya = (await client.get(BASE + "/users/maya.chen@halcyonadvisory.example", headers=auth)).json()
        assert maya["kind"] == "admin#directory#user" and len(maya["id"]) == 21
        assert maya["relations"] == [
            {"type": "manager", "value": "eleanor.whitfield@halcyonadvisory.example"}
        ]
        assert maya["organizations"][0]["department"] == "Advisory"
        assert (await client.get(BASE + "/users/" + maya["id"], headers=auth)).json() == maya
        user_auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        assert (
            await client.get(BASE + "/users", params={"customer": "my_customer"}, headers=user_auth)
        ).status_code == 403
        public = await client.get(
            BASE + "/users",
            params={"domain": "halcyonadvisory.example", "viewType": "domain_public"},
            headers=user_auth,
        )
        assert public.status_code == 200
        assert "isAdmin" not in public.json()["users"][0]
        denied = await client.patch(
            BASE + "/users/" + maya["id"], json={"suspended": True}, headers=user_auth
        )
        assert denied.status_code == 403
        alias_host = await client.get(
            BASE.replace("admin.googleapis.com", "www.googleapis.com") + "/users/" + maya["id"], headers=auth
        )
        assert alias_host.status_code == 200


async def test_user_mutations_aliases_makeadmin_deleted_and_reset(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p035"))
        created = await client.post(
            BASE + "/users",
            headers=auth,
            json={
                "primaryEmail": "test.account@halcyonadvisory.example",
                "password": "fictional-only",
                "name": {"givenName": "Test", "familyName": "Account"},
                "orgUnitPath": "/Technology",
            },
        )
        assert created.status_code == 200, created.text
        row = created.json()
        assert "password" not in row
        uid = row["id"]
        response = await client.patch(
            BASE + "/users/" + uid, headers=auth, json={"name": {"givenName": "Updated"}}
        )
        assert response.json()["name"]["fullName"] == "Updated Account"
        alias = "test.alias@halcyonadvisory.example"
        assert (
            await client.post(BASE + "/users/" + uid + "/aliases", headers=auth, json={"alias": alias})
        ).status_code == 200
        assert (await client.get(BASE + "/users/" + alias, headers=auth)).json()["id"] == uid
        assert (await client.get(BASE + "/users/" + uid + "/aliases", headers=auth)).json()["aliases"][0][
            "alias"
        ] == alias
        assert (
            await client.delete(BASE + "/users/" + uid + "/aliases/" + alias, headers=auth)
        ).status_code == 204
        assert (
            await client.post(BASE + "/users/" + uid + "/makeAdmin", headers=auth, json={"status": True})
        ).status_code == 200
        assert (await client.get(BASE + "/users/" + uid, headers=auth)).json()["isAdmin"] is True
        assert (await client.delete(BASE + "/users/" + uid, headers=auth)).status_code == 204
        assert (await client.get(BASE + "/users/" + uid, headers=auth)).status_code == 404
        deleted = (
            await client.get(
                BASE + "/users", params={"customer": "my_customer", "showDeleted": "true"}, headers=auth
            )
        ).json()["users"]
        assert deleted[0]["id"] == uid


async def test_orgunit_tree_mutations_and_resources(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p035"))
        path = BASE + "/customer/my_customer/orgunits"
        rows = (await client.get(path, params={"type": "all"}, headers=auth)).json()["organizationUnits"]
        london = next(r for r in rows if r["orgUnitPath"] == "/Advisory/London")
        assert london["parentOrgUnitPath"] == "/Advisory"
        children = (
            await client.get(path, params={"orgUnitPath": "/Advisory", "type": "children"}, headers=auth)
        ).json()["organizationUnits"]
        assert [r["name"] for r in children] == ["London"]
        assert (await client.get(path + "/Advisory/London", headers=auth)).json() == london
        created = await client.post(
            path, headers=auth, json={"name": "Test OU", "parentOrgUnitPath": "/Technology"}
        )
        assert created.status_code == 200
        moved = await client.patch(path + "/Technology/Test OU", headers=auth, json={"name": "Renamed"})
        assert moved.json()["orgUnitPath"] == "/Technology/Renamed"
        assert (await client.delete(path + "/Technology/Renamed", headers=auth)).status_code == 204
        assert (await client.delete(path + "/Advisory", headers=auth)).status_code == 412
        customer = (await client.get(BASE + "/customers/my_customer", headers=auth)).json()
        assert customer["id"] == client.hub.world.org.google_customer_id
        domains = (await client.get(BASE + "/customer/my_customer/domains", headers=auth)).json()
        assert domains["domains"][0]["domainName"] == "halcyonadvisory.example"
        rooms = (await client.get(BASE + "/customer/my_customer/resources/calendars", headers=auth)).json()[
            "items"
        ]
        assert len(rooms) == 8
        assert rooms[0]["resourceName"] == "Toronto - Boardroom (12)"
        assert (await client.get(BASE + "/users/p035/photos/thumbnail", headers=auth)).status_code == 404
        photo = (
            await client.get(
                BASE + "/users/arjun.mehta@halcyonadvisory.example/photos/thumbnail", headers=auth
            )
        ).json()
        assert photo["mimeType"] == "image/png" and photo["photoData"].startswith("iVBOR")


async def test_groups_nested_members_crud_and_roles(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p035"))
        groups = (
            await client.get(BASE + "/groups", params={"customer": "my_customer"}, headers=auth)
        ).json()["groups"]
        all_staff = next(r for r in groups if r["name"] == client.hub.world.group("g-all").name)
        direct = (await client.get(BASE + "/groups/" + all_staff["id"] + "/members", headers=auth)).json()[
            "members"
        ]
        assert all(r["type"] == "GROUP" for r in direct)
        expanded = (
            await client.get(
                BASE + "/groups/" + all_staff["id"] + "/members",
                params={"includeDerivedMembership": "true"},
                headers=auth,
            )
        ).json()["members"]
        assert any(r["email"] == "maya.chen@halcyonadvisory.example" for r in expanded)
        membership = await client.get(
            BASE + "/groups/" + all_staff["id"] + "/hasMember/maya.chen@halcyonadvisory.example", headers=auth
        )
        assert membership.json() == {"isMember": True}
        created = (
            await client.post(
                BASE + "/groups",
                headers=auth,
                json={"email": "test.group@halcyonadvisory.example", "name": "Test Group"},
            )
        ).json()
        path = BASE + "/groups/" + created["id"]
        member = await client.post(
            path + "/members",
            headers=auth,
            json={"email": "maya.chen@halcyonadvisory.example", "role": "MANAGER"},
        )
        assert member.status_code == 200
        mid = member.json()["id"]
        assert (await client.get(path + "/members/" + mid, headers=auth)).json()["role"] == "MANAGER"
        assert (await client.patch(path + "/members/" + mid, headers=auth, json={"role": "OWNER"})).json()[
            "role"
        ] == "OWNER"
        assert (await client.delete(path + "/members/" + mid, headers=auth)).status_code == 204
        assert (await client.patch(path, headers=auth, json={"name": "Updated"})).json()["name"] == "Updated"
        assert (await client.delete(path, headers=auth)).status_code == 204


@pytest.mark.parametrize(
    "params",
    [{"maxResults": 501}, {"pageToken": "garbage"}, {"query": "unknown = value"}, {"orderBy": "unknown"}],
)
async def test_directory_validation(make_client: Any, params: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p035"))
        response = await client.get(
            BASE + "/users", params={"customer": "my_customer", **params}, headers=auth
        )
        assert response.status_code == 400, response.text
