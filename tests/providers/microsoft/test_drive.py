from __future__ import annotations

import base64
import hashlib
import re
import struct
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.clock import isoformat_z
from emulators.providers.microsoft import common
from emulators.providers.microsoft import drive_model as dm
from emulators.providers.microsoft.quickxor import quickxor_hash
from emulators.testing.helpers import bearer, mint
from emulators.world import documents

BASE = "https://graph.microsoft.com"


async def test_drive_authentication_and_user_context(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        url = BASE + "/v1.0/me/drive"
        for auth in ({}, bearer("invalid"), bearer(await mint(client, BASE, user="p018", expires_in=-1))):
            response = await client.get(url, headers=auth)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "InvalidAuthenticationToken"
        app = bearer(await mint(client, BASE, app_only=True))
        assert (await client.get(url, headers=app)).status_code == 400


@pytest.mark.parametrize("version", ["v1.0", "beta"])
async def test_personal_drive_folders_paging_and_paths(make_client: Callable[..., Any], version: str) -> None:
    async with make_client("microsoft") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        root = f"{BASE}/{version}/me/drive"
        drive = (await client.get(root, headers=auth)).json()
        assert re.fullmatch(r"b![\w-]{64}", drive["id"])
        assert drive["driveType"] == "business"
        assert "-my.sharepoint.com/personal/" in drive["webUrl"]
        assert (await client.get(f"{BASE}/{version}/me/drives", headers=auth)).json()["value"][0][
            "id"
        ] == drive["id"]
        uid = common.user_id(client.hub.world, "p018")
        assert (await client.get(f"{BASE}/{version}/users/{uid}/drive", headers=auth)).json()["id"] == drive[
            "id"
        ]
        folder = (
            await client.post(
                root + "/root/children", headers=auth, json={"name": "Contract tests", "folder": {}}
            )
        ).json()
        assert re.fullmatch(r"01[A-Z2-7]{32}", folder["id"])
        for name in ("Bravo.txt", "Alpha.txt", "Charlie.txt"):
            response = await client.put(
                root + f"/items/{folder['id']}:/{name}:/content", headers=auth, content=name.encode()
            )
            assert response.status_code == 201, response.text
        first = (
            await client.get(
                root + f"/items/{folder['id']}/children",
                headers=auth,
                params={"$top": 2, "$orderby": "name", "$select": "name,parentReference"},
            )
        ).json()
        assert [i["name"] for i in first["value"]] == ["Alpha.txt", "Bravo.txt"]
        second = (await client.get(first["@odata.nextLink"], headers=auth)).json()
        assert [i["name"] for i in second["value"]] == ["Charlie.txt"]
        path = root + "/root:/Contract tests/Alpha.txt:"
        found = await client.get(path, headers=auth)
        assert found.status_code == 200, found.text
        item = found.json()
        assert item["parentReference"]["path"] == "/drive/root:/Contract tests"
        assert item["size"] == len(b"Alpha.txt")
        assert item["file"]["hashes"]["sha256Hash"] == hashlib.sha256(b"Alpha.txt").hexdigest().upper()
        assert item["file"]["hashes"]["quickXorHash"] == quickxor_hash(b"Alpha.txt")
        assert not any(k.startswith("_") for k in item)
        folder_get = (await client.get(root + f"/items/{folder['id']}", headers=auth)).json()
        assert folder_get["folder"]["childCount"] == 3
        assert (await client.get(root + "/root:/Contract tests:/children", headers=auth)).status_code == 200
        assert (await client.get(root + f"/items/{folder['id']}:/Alpha.txt:", headers=auth)).json()[
            "id"
        ] == item["id"]
        special = (await client.get(root + "/special/documents", headers=auth)).json()
        assert "root" in special
        assert (await client.get(root + "/special/approot", headers=auth)).json()["folder"]["childCount"] == 0


async def test_download_capability_range_expiry_versions_thumbnails(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        root = BASE + "/v1.0/me/drive"
        item = (
            await client.put(root + "/root:/range.txt:/content", headers=auth, content=b"0123456789")
        ).json()
        location = (await client.get(root + f"/items/{item['id']}/content", headers=auth)).headers["location"]
        assert "sharepoint.com/" in location
        downloaded = await client.get(location)
        assert downloaded.content == b"0123456789"
        assert downloaded.headers["content-length"] == "10"
        part = await client.get(location, headers={"Range": "bytes=2-5"})
        assert part.status_code == 206 and part.content == b"2345"
        assert part.headers["content-range"] == "bytes 2-5/10"
        assert (await client.get(location, headers={"Range": "bytes=-3"})).content == b"789"
        assert (await client.get(location, headers={"Range": "bytes=100-"})).status_code == 416
        assert (await client.get(location, headers={"If-None-Match": item["eTag"]})).status_code == 304
        stale = await client.put(
            root + f"/items/{item['id']}/content", headers={**auth, "If-Match": '"stale"'}, content=b"new"
        )
        assert stale.status_code == 412
        replaced = await client.put(root + f"/items/{item['id']}/content", headers=auth, content=b"new")
        assert replaced.status_code == 200
        versions = (await client.get(root + f"/items/{item['id']}/versions", headers=auth)).json()["value"]
        assert len(versions) >= 2
        historic = await client.get(root + f"/items/{item['id']}/versions/2.0/content", headers=auth)
        assert historic.content == b"0123456789"
        thumbs = (await client.get(root + f"/items/{item['id']}/thumbnails", headers=auth)).json()["value"]
        image = await client.get(thumbs[0]["small"]["url"], headers=auth)
        assert image.content.startswith(b"\x89PNG")
        assert struct.unpack(">II", image.content[16:24]) == (96, 96)
        assert (await client.get(root + f"/items/{item['id']}/thumbnails/0", headers=auth)).json()["small"][
            "width"
        ] == 96
        expanded = (
            await client.get(root + f"/items/{item['id']}", headers=auth, params={"$expand": "thumbnails"})
        ).json()
        assert expanded["thumbnails"][0]["id"] == "0"
        client.hub.clock.advance(timedelta(hours=2))
        assert (await client.get(location)).status_code == 401
        client.hub.clock.advance(timedelta(hours=-2))


async def test_world_document_conversion_is_lazy(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        ctx = client.hub.context("microsoft")
        item = next(i for i in ctx.store[dm.ITEMS] if i.get("_doc") and i["name"].endswith(".docx"))
        assert "_blob" not in item
        auth = bearer(await mint(client, BASE, app_only=True))
        url = BASE + f"/v1.0/drives/{item['_drive']}/items/{item['id']}/content"
        redirect = await client.get(url, headers=auth)
        assert redirect.status_code == 302
        assert "_blob" in item
        binary = await client.get(redirect.headers["location"])
        assert binary.content.startswith(b"PK")
        assert "wordprocessingml" in binary.headers["content-type"]
        converted = await client.get(url + "?format=pdf", headers=auth)
        pdf = await client.get(converted.headers["location"])
        assert pdf.content.startswith(b"%PDF")
        assert pdf.headers["content-type"] == "application/pdf"
        denied = await client.get(url + "?format=invalid", headers=auth)
        assert denied.status_code == 400


async def test_folders_conflicts_move_copy_search_delete_and_delta(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        root = BASE + "/v1.0/me/drive"
        baseline = (await client.get(root + "/root/delta?token=latest", headers=auth)).json()
        assert baseline["value"] == []
        folder = (
            await client.post(root + "/root/children", headers=auth, json={"name": "Budget", "folder": {}})
        ).json()
        assert (
            await client.post(root + "/root/children", headers=auth, json={"name": "Budget", "folder": {}})
        ).status_code == 409
        renamed = (
            await client.post(
                root + "/root/children",
                headers=auth,
                json={"name": "Budget", "folder": {}, "@microsoft.graph.conflictBehavior": "rename"},
            )
        ).json()
        assert renamed["name"] == "Budget (1)"
        file = (
            await client.put(
                root + f"/items/{folder['id']}:/budget.txt:/content", headers=auth, content=b"budget"
            )
        ).json()
        conflict = await client.put(
            root + f"/items/{folder['id']}:/budget.txt:/content?@microsoft.graph.conflictBehavior=fail",
            headers=auth,
            content=b"x",
        )
        assert conflict.status_code == 409
        moved = await client.patch(
            root + f"/items/{file['id']}",
            headers=auth,
            json={"name": "budget-2026.txt", "parentReference": {"id": renamed["id"]}},
        )
        assert moved.status_code == 200
        assert moved.json()["parentReference"]["id"] == renamed["id"]
        search = (await client.get(root + "/root/search(q='budget')", headers=auth)).json()["value"]
        assert any(i["id"] == file["id"] for i in search)
        assert (await client.get(root + "/recent", headers=auth)).status_code == 200
        copied = await client.post(
            root + f"/items/{file['id']}/copy",
            headers=auth,
            json={"name": "copy.txt", "parentReference": {"id": folder["id"]}},
        )
        assert copied.status_code == 202
        monitor = (await client.get(copied.headers["location"])).json()
        assert monitor["status"] == "completed"
        assert (await client.get(monitor["resourceLocation"], headers=auth)).json()["name"] == "copy.txt"
        assert (await client.delete(root + f"/items/{file['id']}", headers=auth)).status_code == 204
        assert (await client.get(root + f"/items/{file['id']}", headers=auth)).status_code == 404
        changes = (await client.get(baseline["@odata.deltaLink"], headers=auth)).json()
        assert any(i["id"] == file["id"] and "deleted" in i for i in changes["value"])
        assert (await client.get(changes["@odata.deltaLink"], headers=auth)).json()["value"] == []
        assert (await client.get(root + "/root/delta?token=garbage", headers=auth)).status_code == 410


async def test_drive_acl_direct_share_read_only_and_revocation(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        owner = bearer(await mint(client, BASE, user="p018"))
        other = bearer(await mint(client, BASE, user="p025"))
        root = BASE + "/v1.0/me/drive"
        item = (
            await client.put(root + "/root:/shared-contract.txt:/content", headers=owner, content=b"shared")
        ).json()
        absolute = BASE + f"/v1.0/drives/{item['parentReference']['driveId']}/items/{item['id']}"
        assert (await client.get(absolute, headers=other)).status_code == 404
        invitation = await client.post(
            absolute + "/invite",
            headers=owner,
            json={
                "recipients": [{"email": client.hub.world.person("p025").email}],
                "roles": ["read"],
                "requireSignIn": True,
                "sendInvitation": False,
            },
        )
        assert invitation.status_code == 200, invitation.text
        permission = invitation.json()["value"][0]
        assert permission["grantedToV2"]["user"]["id"] == common.user_id(client.hub.world, "p025")
        assert (await client.get(absolute, headers=other)).status_code == 200
        assert (await client.put(absolute + "/content", headers=other, content=b"bad")).status_code == 403
        assert (await client.get(root + "/sharedWithMe", headers=other)).json()["value"][0]["remoteItem"][
            "id"
        ]
        assert (
            await client.patch(
                absolute + f"/permissions/{permission['id']}", headers=owner, json={"roles": ["write"]}
            )
        ).status_code == 200
        assert (
            await client.patch(absolute, headers=other, json={"description": "Shared annotation"})
        ).status_code == 200
        assert (
            await client.put(absolute + "/content", headers=other, content=b"shared writer update")
        ).status_code == 200
        assert (
            await client.delete(absolute + f"/permissions/{permission['id']}", headers=owner)
        ).status_code == 204
        assert (await client.get(absolute, headers=other)).status_code == 404
        link = await client.post(
            absolute + "/createLink",
            headers=owner,
            json={"type": "view", "scope": "organization", "expirationDateTime": "2099-01-01T00:00:00Z"},
        )
        assert link.status_code == 201
        assert (
            await client.post(
                absolute + "/createLink", headers=owner, json={"type": "view", "scope": "organization"}
            )
        ).status_code == 200
        assert (await client.get(absolute, headers=other)).status_code == 200
        perms = (await client.get(absolute + "/permissions", headers=owner)).json()["value"]
        inherited = next(p for p in perms if "inheritedFrom" in p)
        assert (
            await client.delete(absolute + "/permissions/" + inherited["id"], headers=owner)
        ).status_code == 403


async def test_seeded_restricted_documents_break_inheritance(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        ctx = client.hub.context("microsoft")
        restricted = next(i for i in ctx.store[dm.ITEMS] if i.get("_restricted"))
        drive = ctx.store[dm.DRIVES].get(restricted["_drive"])
        excluded = next(
            person
            for person in ctx.world.active_employees
            if dm.role_for(
                ctx,
                dm.root_item(ctx, drive),
                common.Principal(common.user_id(ctx.world, person.id), person.id, False, "test"),
            )
            and not dm.role_for(
                ctx,
                restricted,
                common.Principal(common.user_id(ctx.world, person.id), person.id, False, "test"),
            )
        )
        auth = bearer(await mint(client, BASE, user=excluded.id))
        url = BASE + f"/v1.0/drives/{restricted['_drive']}/items/{restricted['id']}"
        assert (await client.get(url, headers=auth)).status_code == 404
        app = bearer(await mint(client, BASE, app_only=True))
        assert (await client.get(url, headers=app)).status_code == 200


async def test_prefix_links_and_full_delta_pagination(make_hub: Callable[..., Any]) -> None:
    hub = make_hub("microsoft")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hub.gateway), base_url="http://localhost:44080"
    ) as client:
        base = "/microsoft"
        auth = bearer(await mint(client, base, user="p018"))
        first = (await client.get(base + "/v1.0/me/drive/root/children?$top=1", headers=auth)).json()
        assert first["@odata.nextLink"].startswith("http://localhost:44080/microsoft/v1.0/")
        assert (await client.get(first["@odata.nextLink"], headers=auth)).status_code == 200
        page = (await client.get(base + "/v1.0/me/drive/root/delta?$top=2", headers=auth)).json()
        ids = [r["id"] for r in page["value"]]
        while "@odata.nextLink" in page:
            page = (await client.get(page["@odata.nextLink"], headers=auth)).json()
            ids.extend(r["id"] for r in page["value"])
        assert len(ids) == len(set(ids)) and "@odata.deltaLink" in page


def test_quickxor_reference_vectors() -> None:
    assert quickxor_hash(b"") == "AAAAAAAAAAAAAAAAAAAAAAAAAAA="
    assert quickxor_hash(b"a") == base64.b64encode(b"a" + bytes(11) + b"\x01" + bytes(7)).decode()
    # Independently exercise wrap-around and the 64-bit length suffix by bit placement.
    data = bytes(range(256)) * 3
    expected = bytearray(20)
    for index, value in enumerate(data):
        for bit in range(8):
            position = (index * 11 + bit) % 160
            expected[position // 8] ^= ((value >> bit) & 1) << (position % 8)
    length = len(data).to_bytes(8, "little")
    for index in range(8):
        expected[index + 12] ^= length[index]
    assert quickxor_hash(data) == base64.b64encode(expected).decode()


async def test_drive_root_change_notification_and_reset(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        received: list[dict[str, Any]] = []
        receiver = FastAPI()

        @receiver.post("/hook")
        async def hook(request: Request) -> Response:
            if token := request.query_params.get("validationToken"):
                return Response(token, media_type="text/plain")
            received.append(await request.json())
            return Response(status_code=202)

        client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
        auth = bearer(await mint(client, BASE, user="p018"))
        drive = (await client.get(BASE + "/v1.0/me/drive", headers=auth)).json()
        subscription = await client.post(
            BASE + "/v1.0/subscriptions",
            headers=auth,
            json={
                "changeType": "updated",
                "notificationUrl": "https://receiver.example/hook",
                "resource": f"drives/{drive['id']}/root",
                "expirationDateTime": isoformat_z(client.hub.clock.now() + timedelta(hours=2)),
                "clientState": "drive-contract",
            },
        )
        assert subscription.status_code == 201, subscription.text
        created = await client.put(
            BASE + "/v1.0/me/drive/root:/notification.txt:/content", headers=auth, content=b"notify"
        )
        assert created.status_code == 201
        assert len(received) == 1
        notification = received[0]["value"][0]
        assert notification["subscriptionId"] == subscription.json()["id"]
        assert notification["clientState"] == "drive-contract"
        assert notification["resourceData"]["@odata.type"] == "#microsoft.graph.driveItem"
        url = created.json()["@microsoft.graph.downloadUrl"]
        await client.post(BASE + "/_emulator/reset")
        assert (await client.get(url)).status_code == 401


async def test_group_invites_and_bad_json_have_graph_errors(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        base = BASE + "/v1.0/me/drive"
        for body in (b"{broken", b"[]", b"null"):
            invalid = await client.post(
                base + "/root/children", headers={**auth, "Content-Type": "application/json"}, content=body
            )
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "invalidRequest"
        item = (
            await client.put(base + "/root:/group-share.txt:/content", headers=auth, content=b"group grant")
        ).json()
        url = base + "/items/" + item["id"]
        group = client.hub.world.group("g-finance")
        invitation = await client.post(
            url + "/invite",
            headers=auth,
            json={
                "recipients": [{"objectId": common.group_id(client.hub.world, group.id)}],
                "roles": ["read"],
            },
        )
        assert invitation.status_code == 200, invitation.text
        permission = invitation.json()["value"][0]
        assert "group" in permission["grantedToV2"]
        assert (await client.get(url + "/permissions/" + permission["id"], headers=auth)).status_code == 200
        reader = bearer(await mint(client, BASE, user=group.member_ids[0]))
        did = item["parentReference"]["driveId"]
        assert (
            await client.get(BASE + f"/v1.0/drives/{did}/items/{item['id']}", headers=reader)
        ).status_code == 200
        assert (await client.patch(url, headers=auth, json={"parentReference": "invalid"})).status_code == 400


@pytest.mark.parametrize("kind", ["docx", "xlsx", "pptx"])
async def test_uploaded_office_files_convert_from_actual_content(
    make_client: Callable[..., Any], kind: str
) -> None:
    import io

    from pypdf import PdfReader

    async with make_client("microsoft") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        data, mime = documents.render(kind, "Uploaded contract", ["Distinct uploaded document text."])
        uploaded = await client.put(
            BASE + f"/v1.0/me/drive/root:/uploaded.{kind}:/content",
            headers={**auth, "Content-Type": mime},
            content=data,
        )
        assert uploaded.status_code == 201
        redirect = await client.get(
            BASE + f"/v1.0/me/drive/items/{uploaded.json()['id']}/content?format=pdf", headers=auth
        )
        assert redirect.status_code == 302, redirect.text
        pdf = await client.get(redirect.headers["location"])
        text = "".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.content)).pages)
        assert "Distinct uploaded document text." in text
