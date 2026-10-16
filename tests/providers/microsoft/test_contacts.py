from __future__ import annotations

from collections.abc import Callable
from typing import Any

from emulators.providers.microsoft import common as c
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


async def test_contact_projection_folders_crud_and_visibility(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        rows = (await client.get("/v1.0/me/contacts", params={"$top": 1000})).json()["value"]
        assert any(r["categories"] == ["Clients"] for r in rows)
        assert any(r["categories"] == ["Colleagues"] for r in rows)
        folders = (await client.get("/v1.0/me/contactFolders")).json()["value"]
        fid = folders[0]["id"]
        assert (await client.get(f"/v1.0/me/contactFolders/{fid}")).json()["displayName"] == "Contacts"
        assert (await client.get(f"/v1.0/me/contactFolders/{fid}/contacts")).json()["value"]
        created_folder = await client.post(
            f"/v1.0/me/contactFolders/{fid}/childFolders", json={"displayName": "Test contacts"}
        )
        assert created_folder.status_code == 201
        assert len((await client.get(f"/v1.0/me/contactFolders/{fid}/childFolders")).json()["value"]) == 1
        target = created_folder.json()["id"]
        contact = await client.post(
            f"/v1.0/me/contactFolders/{target}/contacts",
            json={
                "givenName": "Synthetic",
                "surname": "Contact",
                "emailAddresses": [{"address": "synthetic@customer.example", "name": "Synthetic Contact"}],
            },
        )
        assert contact.status_code == 201, contact.text
        cid = contact.json()["id"]
        assert (await client.get(f"/beta/me/contacts/{cid}")).json()["displayName"] == "Synthetic Contact"
        assert (await client.patch(f"/v1.0/me/contacts/{cid}", json={"jobTitle": "Director"})).json()[
            "jobTitle"
        ] == "Director"
        other = c.user_id(client.hub.world, "p035")
        assert (await client.get(f"/v1.0/users/{other}/contacts")).status_code == 403
        assert (
            await client.get(
                f"/v1.0/users/{other}/contacts", headers=bearer(await mint(client, BASE, app_only=True))
            )
        ).status_code == 200
        assert (await client.delete(f"/v1.0/me/contacts/{cid}")).status_code == 204
        assert (await client.get(f"/v1.0/me/contacts/{cid}")).status_code == 404
