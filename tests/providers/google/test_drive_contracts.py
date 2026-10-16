from __future__ import annotations

from typing import Any

import pytest

from emulators.providers.google.common import drive_id
from emulators.providers.google.drive_seed import DOC, FOLDER, shared_drive_id
from emulators.testing.helpers import bearer, mint

HOST = "https://www.googleapis.com"
BASE = HOST + "/drive/v3"
DIRECTORY = "https://admin.googleapis.com/admin/directory/v1"


async def test_explicit_scopes_reject_writes_content_and_wrong_api(make_client: Any) -> None:
    async with make_client("google") as client:
        readonly = bearer(
            await mint(client, HOST, user="p018", scopes=["https://www.googleapis.com/auth/drive.readonly"])
        )
        assert (await client.get(BASE + "/files", headers=readonly)).status_code == 200
        assert (
            await client.post(BASE + "/files", headers=readonly, json={"name": "Denied"})
        ).status_code == 403
        owner = bearer(await mint(client, HOST, user="p018"))
        fid = (await client.post(BASE + "/files", headers=owner, json={"name": "Metadata"})).json()["id"]
        metadata = bearer(
            await mint(
                client, HOST, user="p018", scopes=["https://www.googleapis.com/auth/drive.metadata.readonly"]
            )
        )
        assert (await client.get(BASE + "/files/" + fid, headers=metadata)).status_code == 200
        denied = await client.get(BASE + "/files/" + fid, params={"alt": "media"}, headers=metadata)
        assert (
            denied.status_code == 403
            and denied.json()["error"]["errors"][0]["reason"] == "insufficientPermissions"
        )
        directory_read = bearer(
            await mint(
                client,
                HOST,
                user="p035",
                scopes=["https://www.googleapis.com/auth/admin.directory.user.readonly"],
            )
        )
        assert (
            await client.get(DIRECTORY + "/users", params={"customer": "my_customer"}, headers=directory_read)
        ).status_code == 200
        assert (
            await client.patch(
                DIRECTORY + "/users/maya.chen@halcyonadvisory.example",
                headers=directory_read,
                json={"suspended": True},
            )
        ).status_code == 403
        assert (
            await client.get(BASE + "/about", params={"fields": "user"}, headers=directory_read)
        ).status_code == 403
        docs_auth = bearer(
            await mint(client, HOST, user="p018", scopes=["https://www.googleapis.com/auth/documents"])
        )
        assert (
            await client.post(
                "https://docs.googleapis.com/v1/documents", headers=docs_auth, json={"title": "Docs Scope"}
            )
        ).status_code == 200


async def test_directory_group_membership_changes_drive_visibility_and_manager_chain(
    make_client: Any,
) -> None:
    async with make_client("google") as client:
        admin = bearer(await mint(client, HOST, user="p035"))
        staff = bearer(await mint(client, HOST, user="p039"))
        ctx = client.hub.context("google")
        did = shared_drive_id(ctx, "clients")
        assert (await client.get(BASE + "/drives/" + did, headers=staff)).status_code == 404
        group = next(g for g in ctx.store.collection("directory_groups") if g["_world"] == "g-audit")
        member = await client.post(
            DIRECTORY + "/groups/" + group["id"] + "/members",
            headers=admin,
            json={"email": "liam.fitzgerald@halcyonadvisory.example"},
        )
        assert member.status_code == 200
        assert (await client.get(BASE + "/drives/" + did, headers=staff)).status_code == 200
        assert (
            await client.delete(
                DIRECTORY + "/groups/" + group["id"] + "/members/" + member.json()["id"], headers=admin
            )
        ).status_code == 204
        assert (await client.get(BASE + "/drives/" + did, headers=staff)).status_code == 404
        reports = await client.get(
            DIRECTORY + "/users",
            params={"customer": "my_customer", "query": "manager='maya.chen@halcyonadvisory.example'"},
            headers=admin,
        )
        assert len(reports.json()["users"]) == 7


async def test_link_sharing_known_id_domain_sharing_and_ownership_transfer(make_client: Any) -> None:
    async with make_client("google") as client:
        owner = bearer(await mint(client, HOST, user="p018"))
        reader = bearer(await mint(client, HOST, user="p031"))
        fid = (await client.post(BASE + "/files", headers=owner, json={"name": "Link file"})).json()["id"]
        perm = await client.post(
            BASE + "/files/" + fid + "/permissions",
            headers=owner,
            params={"fields": "*"},
            json={"type": "anyone", "role": "reader", "allowFileDiscovery": False},
        )
        assert perm.json()["id"] == "anyoneWithLink"
        assert (await client.get(BASE + "/files/" + fid, headers=reader)).status_code == 200
        listed = (
            await client.get(
                BASE + "/files", params={"q": "name = 'Link file'", "fields": "files(id)"}, headers=reader
            )
        ).json()
        assert listed["files"] == []
        permission = (
            await client.get(BASE + "/files/" + fid + "/permissions/anyoneWithLink", headers=owner)
        ).json()
        assert permission["allowFileDiscovery"] is False
        assert (
            await client.delete(BASE + "/files/" + fid + "/permissions/anyoneWithLink", headers=owner)
        ).status_code == 204
        domain = await client.post(
            BASE + "/files/" + fid + "/permissions",
            headers=owner,
            params={"fields": "*"},
            json={"type": "domain", "role": "reader", "domain": "halcyonadvisory.example"},
        )
        assert domain.json()["domain"] == "halcyonadvisory.example"
        assert (await client.get(BASE + "/files/" + fid, headers=reader)).status_code == 200
        transfer = {"type": "user", "role": "owner", "emailAddress": "olivia.grant@halcyonadvisory.example"}
        assert (
            await client.post(BASE + "/files/" + fid + "/permissions", headers=owner, json=transfer)
        ).status_code == 403
        assert (
            await client.post(
                BASE + "/files/" + fid + "/permissions",
                headers=owner,
                params={"transferOwnership": "true"},
                json=transfer,
            )
        ).status_code == 200
        assert (await client.delete(BASE + "/files/" + fid, headers=owner)).status_code == 403
        assert (await client.delete(BASE + "/files/" + fid, headers=reader)).status_code == 204


async def test_folder_trash_inheritance_change_paging_and_removal_visibility(make_client: Any) -> None:
    async with make_client("google") as client:
        owner = bearer(await mint(client, HOST, user="p018"))
        start = (await client.get(BASE + "/changes/startPageToken", headers=owner)).json()["startPageToken"]
        folder = (
            await client.post(BASE + "/files", headers=owner, json={"name": "Parent", "mimeType": FOLDER})
        ).json()["id"]
        fid = (
            await client.post(BASE + "/files", headers=owner, json={"name": "Child", "parents": [folder]})
        ).json()["id"]
        await client.patch(BASE + "/files/" + folder, headers=owner, json={"trashed": True})
        child = (
            await client.get(
                BASE + "/files/" + fid, params={"fields": "trashed,explicitlyTrashed"}, headers=owner
            )
        ).json()
        assert child == {"trashed": True, "explicitlyTrashed": False}
        listed = await client.get(
            BASE + "/files", params={"q": f"'{folder}' in parents and trashed = false"}, headers=owner
        )
        assert listed.json()["files"] == []
        await client.patch(BASE + "/files/" + folder, headers=owner, json={"trashed": False})
        assert (
            await client.get(BASE + "/files/" + fid, params={"fields": "trashed"}, headers=owner)
        ).json() == {"trashed": False}
        page = (
            await client.get(
                BASE + "/changes",
                params={
                    "pageToken": start,
                    "pageSize": 1,
                    "fields": "nextPageToken,changes(fileId,file(id,name))",
                },
                headers=owner,
            )
        ).json()
        assert page["changes"][0]["fileId"] == folder
        second = (
            await client.get(
                BASE + "/changes", params={"pageToken": page["nextPageToken"], "pageSize": 1}, headers=owner
            )
        ).json()
        assert second["changes"][0]["fileId"] == fid
        assert (
            await client.get(BASE + "/changes", params={"pageToken": "garbage"}, headers=owner)
        ).status_code == 400


async def test_inherited_permissions_cannot_be_deleted_and_native_metadata_has_no_checksums(
    make_client: Any,
) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, HOST, user="p018"))
        ctx = client.hub.context("google")
        doc = next(
            d
            for d in ctx.world.documents
            if d.space_id == "knowledge" and d.kind == "docx" and not d.restricted
        )
        fid = drive_id(ctx.world, "file", doc.id)
        metadata = (
            await client.get(
                BASE + "/files/" + fid, params={"supportsAllDrives": "true", "fields": "*"}, headers=auth
            )
        ).json()
        assert metadata["mimeType"] == DOC
        assert not {"owners", "size", "md5Checksum"}.intersection(metadata)
        perm = next(p for p in metadata["permissions"] if p["permissionDetails"][0]["inherited"])
        denied = await client.delete(
            BASE + "/files/" + fid + "/permissions/" + perm["id"],
            params={"supportsAllDrives": "true"},
            headers=auth,
        )
        assert (
            denied.status_code == 403
            and denied.json()["error"]["errors"][0]["reason"] == "cannotModifyInheritedTeamDrivePermission"
        )
        assert (await client.get(BASE + "/about", headers=auth)).status_code == 400


@pytest.mark.parametrize(
    "fields", ["files(unknownField)", "unknownField", "files/permissions/bad", "files(id", "files/id/"]
)
async def test_unknown_and_malformed_selectors(make_client: Any, fields: str) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, HOST, user="p018"))
        result = await client.get(BASE + "/files", params={"fields": fields}, headers=auth)
        assert result.status_code == 400
        assert result.json()["error"]["errors"][0]["location"] == "fields"


async def test_invalid_json_objects_and_invalid_fields_do_not_create_files(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, HOST, user="p018"))
        for data in (b"{bad", b"[]", b"null"):
            response = await client.post(
                BASE + "/files", headers={**auth, "Content-Type": "application/json"}, content=data
            )
            assert response.status_code == 400
        before = len(client.hub.context("google").store.collection("drive_files"))
        invalid = await client.post(
            BASE + "/files", headers=auth, params={"fields": "bad"}, json={"name": "Do not create"}
        )
        assert invalid.status_code == 400
        assert len(client.hub.context("google").store.collection("drive_files")) == before
