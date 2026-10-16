from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from emulators.core.clock import isoformat_z, parse_instant
from emulators.providers.microsoft import drive_model as dm
from emulators.providers.microsoft import sites
from emulators.providers.microsoft.quickxor import quickxor_hash
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


async def test_sync_session_conflicts_preconditions_and_expiry(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        hub = client.hub
        hub.clock.freeze(parse_instant("2026-10-16T12:00:00Z"))
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        path = "/v1.0/me/drive/root:/sync.txt:"
        item = (await client.put(path + "/content", content=b"original")).json()
        assert (await client.post(path + "/createUploadSession", json={})).status_code == 409
        replace = {"item": {"@microsoft.graph.conflictBehavior": "replace"}}
        for header in ({"If-Match": "stale"}, {"If-None-Match": item["eTag"]}):
            assert (
                await client.post(path + "/createUploadSession", json=replace, headers=header)
            ).status_code == 412
        session = (
            await client.post(path + "/createUploadSession", json=replace, headers={"If-Match": item["cTag"]})
        ).json()
        hub.clock.advance(timedelta(minutes=45))
        fragment = await client.put(
            session["uploadUrl"], content=b"a" * 327680, headers={"Content-Range": "bytes 0-327679/327681"}
        )
        assert fragment.status_code == 202
        assert parse_instant(fragment.json()["expirationDateTime"]) > parse_instant(
            session["expirationDateTime"]
        )
        hub.clock.advance(timedelta(minutes=30))
        final = await client.put(
            session["uploadUrl"], content=b"z", headers={"Content-Range": "bytes 327680-327680/327681"}
        )
        assert final.status_code == 200 and final.json()["id"] == item["id"]
        assert (await client.get(final.json()["@microsoft.graph.downloadUrl"])).content.endswith(b"z")


@pytest.mark.parametrize(
    "payload", [{"deferCommit": 0}, {"deferCommit": None}, {"item": {"name": "different.txt"}}]
)
async def test_upload_session_options_rejected(
    make_client: Callable[..., Any], payload: dict[str, Any]
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        response = await client.post("/beta/me/drive/root:/named.txt:/createUploadSession", json=payload)
        assert response.status_code == 400


@pytest.mark.parametrize("name", ["x" * 256, "bad\x00name", "bad\nname"])
async def test_drive_invalid_names_fail_before_mutation(make_client: Callable[..., Any], name: str) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        response = await client.post("/v1.0/me/drive/root/children", json={"name": name, "folder": {}})
        assert response.status_code == 400


async def test_drive_null_description_search_and_runtime_dates(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.hub.clock.freeze(parse_instant("2026-10-20T12:34:56Z"))
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        file = (
            await client.put("/v1.0/me/drive/root:/null-description.txt:/content", content=b"text")
        ).json()
        path = "/v1.0/me/drive/items/" + file["id"]
        assert (await client.patch(path, json={"description": None})).status_code == 200
        assert (await client.get("/v1.0/me/drive/root/search(q='null-description')")).status_code == 200
        folder = (
            await client.post("/v1.0/me/drive/root/children", json={"name": "Now", "folder": {}})
        ).json()
        assert folder["createdDateTime"] == "2026-10-20T12:34:56Z"
        assert (await client.get("/v1.0/me/drive/root/delta?$top=0")).status_code == 400
        assert (
            await client.put(path + "/content", content=b"new", headers={"If-None-Match": file["id"]})
        ).status_code == 200
        current = (await client.get(path)).json()
        assert (
            await client.put(path + "/content", content=b"x", headers={"If-None-Match": current["cTag"]})
        ).status_code == 412


async def test_nonowner_permissions_hide_other_grants_and_redemption_urls(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        owner = bearer(await mint(client, BASE, user="p018"))
        reader = bearer(await mint(client, BASE, user="p001"))
        created = (
            await client.put("/v1.0/me/drive/root:/acl-audit.txt:/content", headers=owner, content=b"acl")
        ).json()
        drive = (await client.get("/v1.0/me/drive", headers=owner)).json()
        path = f"/v1.0/drives/{drive['id']}/items/{created['id']}"
        original_count = len((await client.get(path + "/permissions", headers=owner)).json()["value"])
        perms = []
        for pid in ("p001", "p002"):
            grant = await client.post(
                path + "/invite",
                headers=owner,
                json={"roles": ["read"], "recipients": [{"email": client.hub.world.person(pid).email}]},
            )
            assert grant.status_code == 200
            perms.append(grant.json()["value"][0])
        assert (
            await client.post(path + "/createLink", headers=owner, json={"scope": "organization"})
        ).status_code == 201
        visible = (await client.get(path + "/permissions", headers=reader)).json()["value"]
        assert perms[0]["id"] in [p["id"] for p in visible]
        assert perms[1]["id"] not in [p["id"] for p in visible]
        assert all("webUrl" not in p.get("link", {}) for p in visible)
        assert (await client.get(path + "/permissions/" + perms[1]["id"], headers=reader)).status_code == 404
        assert (
            len((await client.get(path + "/permissions", headers=owner)).json()["value"])
            == original_count + 3
        )


@pytest.mark.parametrize("value", [False, 0, [], {}, "invalid"])
async def test_permission_expiry_type_validation(make_client: Callable[..., Any], value: Any) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        for operation in ("invite", "createLink"):
            payload = {
                "roles": ["read"],
                "recipients": [{"email": client.hub.world.person("p001").email}],
                "expirationDateTime": value,
            }
            assert (await client.post("/v1.0/me/drive/root/" + operation, json=payload)).status_code == 400


async def test_invitation_expiry_is_enforced(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        owner = bearer(await mint(client, BASE, user="p018"))
        reader = bearer(await mint(client, BASE, user="p001"))
        file = (
            await client.put("/v1.0/me/drive/root:/expires.txt:/content", headers=owner, content=b"private")
        ).json()
        drive = dm.personal_drive(ctx, "p018")
        path = f"/v1.0/drives/{drive['id']}/items/{file['id']}"
        expiry = isoformat_z(ctx.clock.now() + timedelta(minutes=5))
        response = await client.post(
            path + "/invite",
            headers=owner,
            json={
                "roles": ["read"],
                "recipients": [{"email": ctx.world.person("p001").email}],
                "expirationDateTime": expiry,
            },
        )
        assert response.json()["value"][0]["expirationDateTime"] == expiry
        assert (await client.get(path, headers=reader)).status_code == 200
        ctx.clock.advance(timedelta(minutes=6))
        assert (await client.get(path, headers=reader)).status_code == 404


async def test_file_content_tags_and_folder_wire_shape(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        root = (await client.get("/v1.0/me/drive/root")).json()
        assert "cTag" not in root
        file = (await client.put("/v1.0/me/drive/root:/tag.txt:/content", content=b"one")).json()
        path = "/v1.0/me/drive/items/" + file["id"]
        renamed = (await client.patch(path, json={"name": "tag-renamed.txt"})).json()
        assert renamed["eTag"] != file["eTag"] and renamed["cTag"] == file["cTag"]
        conditional = await client.get(path + "/content", headers={"If-None-Match": file["cTag"]})
        assert conditional.status_code == 304 and conditional.content == b""
        assert "Location" not in conditional.headers
        replaced = (await client.put(path + "/content", content=b"two")).json()
        assert replaced["cTag"] != file["cTag"]


async def test_folder_rename_delta_only_reports_folder_with_no_parent_path(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        folder = (
            await client.post("/v1.0/me/drive/root/children", json={"name": "Delta before", "folder": {}})
        ).json()
        path = "/v1.0/me/drive/items/" + folder["id"]
        child = (await client.put(path + ":/child.txt:/content", content=b"sync")).json()
        baseline = (await client.get("/v1.0/me/drive/root/delta?token=latest")).json()
        assert (await client.patch(path, json={"name": "Delta after"})).status_code == 200
        changes = (await client.get(baseline["@odata.deltaLink"])).json()
        assert [i["id"] for i in changes["value"]] == [folder["id"]]
        assert "path" not in changes["value"][0]["parentReference"]
        current = (await client.get("/v1.0/me/drive/items/" + child["id"])).json()
        assert current["eTag"] == child["eTag"]
        assert current["parentReference"]["path"].endswith("Delta after")


async def test_site_permission_listing_requires_application_or_fullcontrol(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ordinary = bearer(await mint(client, BASE, user="p018"))
        admin = bearer(await mint(client, BASE, user="p018", scopes=["Sites.FullControl.All"]))
        assert (await client.get("/v1.0/sites/root/permissions", headers=ordinary)).status_code == 403
        assert (await client.get("/v1.0/sites/root/permissions", headers=admin)).json()["value"] == []


async def test_folder_deletion_does_not_redelete_recycled_descendants(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        folder = (
            await client.post("/v1.0/me/drive/root/children", json={"name": "Recycle audit", "folder": {}})
        ).json()
        path = "/v1.0/me/drive/items/" + folder["id"]
        child = (await client.put(path + ":/child.txt:/content", content=b"child")).json()
        assert (await client.delete("/v1.0/me/drive/items/" + child["id"])).status_code == 204
        assert (await client.delete(path)).status_code == 204
        changes = (await client.get("/_emulator/state/ms_drive_changes")).json()["records"]
        deleted = [c for c in changes if c["item"]["id"] == child["id"] and c["item"].get("_deleted")]
        assert len(deleted) == 1


async def test_permission_mutations_reject_owner_role_and_bad_patch_expiry(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        path = "/v1.0/me/drive/root/invite"
        recipient = {"email": client.hub.world.person("p001").email}
        assert (
            await client.post(path, json={"roles": ["owner"], "recipients": [recipient]})
        ).status_code == 400
        permission = (await client.post(path, json={"roles": ["read"], "recipients": [recipient]})).json()[
            "value"
        ][0]
        path = "/v1.0/me/drive/root/permissions/" + permission["id"]
        for payload in ({"expirationDateTime": False}, {"roles": ["owner"]}):
            assert (await client.patch(path, json=payload)).status_code == 400


@pytest.mark.parametrize(
    "fields", [{"StartDate": []}, {"EndDate": False}, {"StartDate": "not-a-date"}, {"Title": "x" * 256}]
)
async def test_sharepoint_field_types(make_client: Callable[..., Any], fields: dict[str, Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        client.headers.update(bearer(await mint(client, BASE, app_only=True)))
        listing = ctx.store[sites.LISTS].first(name="EngagementTracker")
        path = f"/v1.0/sites/{listing['_site']}/lists/{listing['id']}/items"
        response = await client.post(path, json={"fields": {"Title": "Audit", **fields}})
        assert response.status_code == 400
        assert (await client.post(path.rsplit("/", 2)[0], json={})).status_code == 405
        assert (await client.get("/v1.0/sites/root/permissions")).json()["value"] == []


@pytest.mark.parametrize("length", [0, 1, 14, 15, 19, 20, 21, 159, 160, 161, 4097])
def test_quickxor_wrap_and_length_against_bit_reference(length: int) -> None:
    data = bytes((index * 71 + 255) % 256 for index in range(length))
    digest = bytearray(20)
    for index, byte in enumerate(data):
        for bit in range(8):
            if byte & (1 << bit):
                position = (index * 11 + bit) % 160
                digest[position // 8] ^= 1 << (position % 8)
    for index, byte in enumerate(length.to_bytes(8, "little")):
        digest[12 + index] ^= byte
    assert quickxor_hash(data) == base64.b64encode(digest).decode()
