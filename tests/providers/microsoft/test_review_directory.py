from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from emulators.providers.microsoft import common as c
from emulators.providers.microsoft.directory import DEFAULT_USER
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


async def test_directory_object_default_projections_and_type_annotations(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        manager = (await client.get("/v1.0/me/manager")).json()
        assert manager["@odata.type"] == "#microsoft.graph.user"
        assert set(manager) == set(DEFAULT_USER) | {"@odata.context", "@odata.type"}
        selected = (await client.get("/v1.0/me/manager?$select=department")).json()
        assert set(selected) == {"id", "department", "@odata.context", "@odata.type"}
        uid = c.user_id(client.hub.world, "p001")
        group = client.hub.world.groups[0]
        gid = c.group_id(client.hub.world, group.id)
        for path in (
            f"/users/{uid}/directReports",
            "/me/memberOf",
            f"/groups/{gid}/members",
            f"/groups/{gid}/owners",
        ):
            response = await client.get("/v1.0" + path)
            assert response.status_code == 200, response.text
            for obj in response.json()["value"]:
                assert obj["@odata.type"] in ("#microsoft.graph.user", "#microsoft.graph.group")
                if obj["@odata.type"] == "#microsoft.graph.user":
                    assert set(obj) == set(DEFAULT_USER) | {"@odata.type"}


async def test_count_cast_and_key_addressing(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        ctx = client.hub.context("microsoft")
        gid = c.group_id(ctx.world, ctx.world.groups[0].id)
        for path, expected in (
            ("users", len(ctx.store["users"])),
            ("groups", len(ctx.store["groups"])),
            (f"groups/{gid}/members", len(ctx.store.meta["ms_group_members"][gid])),
        ):
            assert (await client.get(f"/v1.0/{path}/$count")).status_code == 400
            response = await client.get(f"/v1.0/{path}/$count", headers={"ConsistencyLevel": "eventual"})
            assert response.status_code == 200 and response.text == str(expected)
            assert response.headers["content-type"].startswith("text/plain")
        cast_headers = {"ConsistencyLevel": "eventual"}
        assert (await client.get(f"/v1.0/groups/{gid}/members/microsoft.graph.user")).status_code == 400
        members = (
            await client.get(
                f"/v1.0/groups/{gid}/members/microsoft.graph.user?$count=true", headers=cast_headers
            )
        ).json()["value"]
        assert members and all(m["@odata.type"] == "#microsoft.graph.user" for m in members)
        assert (
            await client.get("/v1.0/me/memberOf/microsoft.graph.group?$count=true", headers=cast_headers)
        ).json()["value"]
        direct = await client.get("/v1.0/me/mailFolders/Inbox/messages")
        keyed = await client.get("/v1.0/me/mailFolders('Inbox')/messages")
        assert direct.json() == keyed.json()
        keyed_user = await client.get("/v1.0/users('maya.chen@halcyonadvisory.example')")
        assert keyed_user.json()["mail"] == "maya.chen@halcyonadvisory.example"
        count = await client.get("/v1.0/me/messages/$count")
        assert int(count.text) == len(ctx.store["mail_messages"].where(_owner=c.user_id(ctx.world, "p018")))


async def test_self_update_and_membership_functions_with_privilege_denials(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        assert (await client.patch("/v1.0/me", json={"preferredLanguage": "en-CA"})).status_code == 204
        assert (await client.get("/v1.0/me")).json()["preferredLanguage"] == "en-CA"
        assert (await client.patch("/v1.0/me", json={"accountEnabled": False})).status_code == 403
        groups = (await client.post("/v1.0/me/getMemberGroups", json={"securityEnabledOnly": False})).json()[
            "value"
        ]
        assert groups
        objects = await client.post("/v1.0/me/getMemberObjects", json={"securityEnabledOnly": False})
        assert set(objects.json()["value"]) == set(groups)
        for operation, key in (("checkMemberGroups", "groupIds"), ("checkMemberObjects", "ids")):
            result = await client.post("/v1.0/me/" + operation, json={key: [groups[0], "absent"]})
            assert result.json()["value"] == [groups[0]]


async def test_group_crud_owner_refs_permissions_and_delta(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        client.headers.update(auth)
        payload = {
            "displayName": "Advisory QA",
            "mailNickname": "advisory-qa",
            "mailEnabled": False,
            "securityEnabled": True,
            "groupTypes": [],
            "owners@odata.bind": [BASE + "/v1.0/users/" + c.user_id(client.hub.world, "p018")],
        }
        assert (await client.post("/v1.0/groups", json=payload)).status_code == 403
        admin = bearer(await mint(client, BASE, app_only=True))
        invalid = await client.post(
            "/v1.0/groups", headers=admin, json={**payload, "owners@odata.bind": ["https://["]}
        )
        assert invalid.status_code == 400
        delta = (await client.get("/v1.0/groups/delta")).json()["@odata.deltaLink"]
        response = await client.post("/v1.0/groups", headers=admin, json=payload)
        assert response.status_code == 201, response.text
        gid = response.json()["id"]
        assert (await client.get(delta)).json()["value"][0]["id"] == gid
        assert (await client.patch(f"/v1.0/groups/{gid}", json={"description": "Updated"})).status_code == 204
        outsider = bearer(await mint(client, BASE, user="p019"))
        assert (
            await client.patch(f"/v1.0/groups/{gid}", headers=outsider, json={"description": "Denied"})
        ).status_code == 403
        uid = c.user_id(client.hub.world, "p035")
        ref = {"@odata.id": BASE + "/v1.0/users/" + uid}
        assert (await client.post(f"/v1.0/groups/{gid}/owners/$ref", json=ref)).status_code == 204
        assert uid in [r["id"] for r in (await client.get(f"/v1.0/groups/{gid}/owners")).json()["value"]]
        assert (await client.delete(f"/v1.0/groups/{gid}/owners/{uid}/$ref")).status_code == 204
        assert (await client.delete(f"/v1.0/groups/{gid}", headers=outsider)).status_code == 403
        assert (await client.delete(f"/v1.0/groups/{gid}")).status_code == 204
        assert (await client.get(f"/v1.0/groups/{gid}")).status_code == 404


@pytest.mark.parametrize("resource", ["organization", "domains", "subscribedSkus"])
async def test_directory_singletons_and_collection_match(
    make_client: Callable[..., Any], resource: str
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, app_only=True)))
        entity = (await client.get("/v1.0/" + resource)).json()["value"][0]
        response = await client.get(f"/v1.0/{resource}/{entity['id']}")
        assert response.status_code == 200
        assert {k: v for k, v in response.json().items() if not k.startswith("@")} == entity
        assert (await client.get(f"/v1.0/{resource}/absent")).status_code == 404


async def test_directory_delta_default_and_explicit_projections(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, app_only=True)))
        ctx = client.hub.context("microsoft")
        for group in ctx.store["groups"]:
            group["allowExternalSenders"] = False
        users = (await client.get("/v1.0/users/delta")).json()["value"]
        assert set(users[0]) == set(DEFAULT_USER)
        groups = (await client.get("/v1.0/groups/delta")).json()["value"]
        assert all("allowExternalSenders" not in group for group in groups)
        selected = (await client.get("/v1.0/groups/delta?$select=allowExternalSenders")).json()["value"]
        assert all(set(group) == {"id", "allowExternalSenders"} for group in selected)


async def test_created_users_have_mailbox_defaults_and_empty_contacts(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, app_only=True)))
        person = client.hub.world.person("p018")
        response = await client.post(
            "/v1.0/users",
            json={
                "accountEnabled": True,
                "displayName": person.display_name,
                "mailNickname": "maya.qa",
                "userPrincipalName": "maya.qa@halcyonadvisory.example",
                "passwordProfile": {"password": "synthetic"},
            },
        )
        assert response.status_code == 201, response.text
        uid = response.json()["id"]
        settings = await client.get(f"/v1.0/users/{uid}/mailboxSettings")
        assert settings.status_code == 200 and settings.json()["timeZone"] == "UTC"
        patched = await client.patch(
            f"/v1.0/users/{uid}/mailboxSettings", json={"timeZone": "Eastern Standard Time"}
        )
        assert patched.status_code == 200 and patched.json()["timeZone"] == "Eastern Standard Time"
        contacts = await client.get(f"/v1.0/users/{uid}/contacts")
        assert contacts.status_code == 200 and contacts.json()["value"] == []
