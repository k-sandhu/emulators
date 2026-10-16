from __future__ import annotations

import base64
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from typing import Any

import pytest

from emulators.providers.microsoft import common as c
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


async def test_mail_projection_queries_folders_and_body_preferences(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        result = await client.get("/v1.0/me/messages")
        rows = result.json()["value"]
        assert len(rows) == 10 and "@odata.nextLink" in result.json()
        assert (await client.get(result.json()["@odata.nextLink"])).json()["value"][0]["id"] != rows[0]["id"]
        mid = rows[0]["id"]
        result = await client.get(
            f"/v1.0/me/messages/{mid}", headers={"Prefer": 'outlook.body-content-type="text"'}
        )
        assert result.json()["body"]["contentType"] == "text"
        raw = await client.get(f"/v1.0/me/messages/{mid}/$value")
        parsed = BytesParser(policy=policy.default).parsebytes(raw.content)
        assert parsed["Subject"] == rows[0]["subject"]
        assert parsed["Message-ID"] == rows[0]["internetMessageId"]
        for expression in (
            "isRead eq false",
            "receivedDateTime ge 2025-01-01T00:00:00Z",
            "hasAttachments eq true",
            "from/emailAddress/address eq 'maya.chen@halcyonadvisory.example'",
        ):
            response = await client.get("/v1.0/me/messages", params={"$filter": expression})
            assert response.status_code == 200, response.text
        for search in ('"from:maya"', '"invoice"'):
            assert (await client.get("/v1.0/me/messages", params={"$search": search})).status_code == 200
        root = (await client.get("/v1.0/me/mailFolders/msgfolderroot/childFolders")).json()["value"]
        assert {f["displayName"] for f in root}.issuperset({"Inbox", "Sent Items", "Drafts", "Deleted Items"})
        inbox = (await client.get("/v1.0/me/mailFolders/inbox")).json()
        children = (await client.get("/v1.0/me/mailFolders/inbox/childFolders")).json()["value"]
        assert len(children) == inbox["childFolderCount"]
        for child in children:
            assert child["parentFolderId"] == inbox["id"]
            assert (await client.get(f"/v1.0/me/mailFolders/{child['id']}/messages")).status_code == 200
        settings = await client.get("/v1.0/me/mailboxSettings")
        assert settings.json()["timeZone"] == "Eastern Standard Time"
        updated = await client.patch("/v1.0/me/mailboxSettings", json={"timeZone": "Pacific Standard Time"})
        assert updated.json()["timeZone"] == "Pacific Standard Time"


async def test_attachments_are_real_files_and_lazy(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        result = await client.get(
            "/v1.0/me/messages", params={"$filter": "hasAttachments eq true", "$top": 1000}
        )
        assert result.json()["value"]
        mid = result.json()["value"][0]["id"]
        attachments = (await client.get(f"/v1.0/me/messages/{mid}/attachments")).json()["value"]
        assert attachments
        for item in attachments:
            content = base64.b64decode(item["contentBytes"])
            assert item["size"] == len(content) and len(content) > 100
            single = await client.get(f"/v1.0/me/messages/{mid}/attachments/{item['id']}")
            assert single.json()["contentBytes"] == item["contentBytes"]
            raw = await client.get(f"/v1.0/me/messages/{mid}/attachments/{item['id']}/$value")
            assert raw.content == content


async def test_drafts_send_delivery_actions_moves_and_delete(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        payload = {
            "subject": "Synthetic delivery contract",
            "body": {"contentType": "text", "content": "Hello colleague"},
            "toRecipients": [
                {"emailAddress": {"address": "arjun.mehta@halcyonadvisory.example", "name": "Arjun Mehta"}}
            ],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "note.txt",
                    "contentType": "text/plain",
                    "contentBytes": base64.b64encode(b"Synthetic attachment").decode(),
                }
            ],
        }
        result = await client.post("/v1.0/me/messages", json=payload)
        assert result.status_code == 201, result.text
        mid = result.json()["id"]
        assert result.json()["isDraft"]
        result = await client.patch(
            f"/v1.0/me/messages/{mid}",
            json={"isRead": True, "categories": ["Contract"], "flag": {"flagStatus": "flagged"}},
        )
        assert result.json()["flag"]["flagStatus"] == "flagged"
        assert (await client.post(f"/v1.0/me/messages/{mid}/send")).status_code == 202
        assert not (await client.get(f"/v1.0/me/messages/{mid}")).json()["isDraft"]
        recipient = bearer(await mint(client, BASE, user="p035"))
        received = await client.get(
            "/v1.0/me/mailFolders/inbox/messages",
            headers=recipient,
            params={"$filter": "subject eq 'Synthetic delivery contract'"},
        )
        assert len(received.json()["value"]) == 1
        received_id = received.json()["value"][0]["id"]
        for operation in ("createReply", "reply", "replyAll", "forward"):
            response = await client.post(
                f"/v1.0/me/messages/{received_id}/{operation}",
                headers=recipient,
                json={"comment": "Reply contract", "toRecipients": payload["toRecipients"]},
            )
            assert response.status_code == (201 if operation == "createReply" else 202), response.text
        copied = await client.post(f"/v1.0/me/messages/{mid}/copy", json={"destinationId": "archive"})
        assert copied.status_code == 201 and copied.json()["id"] != mid
        moved = await client.post(f"/v1.0/me/messages/{mid}/move", json={"destinationId": "archive"})
        assert moved.status_code == 201 and (await client.get(f"/v1.0/me/messages/{mid}")).status_code == 404
        moved_id = moved.json()["id"]
        assert (await client.delete(f"/v1.0/me/messages/{moved_id}")).status_code == 204
        deleted = (await client.get("/v1.0/me/mailFolders/deleteditems/messages")).json()["value"]
        assert any(r["subject"] == payload["subject"] for r in deleted)
        assert (
            await client.post("/v1.0/me/sendMail", json={"message": payload, "saveToSentItems": False})
        ).status_code == 202


async def test_mailbox_ownership_app_access_and_folder_delta(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        uid = c.user_id(client.hub.world, "p035")
        assert (await client.get(f"/v1.0/users/{uid}/messages")).status_code == 403
        app = bearer(await mint(client, BASE, app_only=True))
        assert (await client.get(f"/v1.0/users/{uid}/messages", headers=app)).status_code == 200
        delta = (await client.get("/v1.0/me/mailFolders/drafts/messages/delta")).json()["@odata.deltaLink"]
        draft = (await client.post("/v1.0/me/messages", json={"subject": "Delta draft"})).json()
        result = (await client.get(delta)).json()
        assert [r["id"] for r in result["value"]] == [draft["id"]]
        delta = result["@odata.deltaLink"]
        await client.delete(f"/v1.0/me/messages/{draft['id']}")
        assert (await client.get(delta)).json()["value"][0]["@removed"]["reason"] == "deleted"


@pytest.mark.parametrize(
    "folder",
    ["inbox", "drafts", "sentitems", "deleteditems", "junkemail", "archive", "outbox", "msgfolderroot"],
)
async def test_well_known_folders_beta(make_client: Callable[..., Any], folder: str) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        token = await mint(client, BASE, user="p018")
        assert (await client.get(f"/beta/me/mailFolders/{folder}", headers=bearer(token))).status_code == 200
