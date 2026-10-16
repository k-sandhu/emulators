from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from emulators.core.clock import parse_instant
from emulators.providers.microsoft import common
from emulators.providers.microsoft import teams_model as tm
from emulators.testing.helpers import bearer, mint
from emulators.world.documents import make_png

BASE = "https://graph.microsoft.com"


def channel_path(ctx: Any, pid: str = "p018") -> str:
    channel = next(ch for ch in ctx.store[tm.CHANNELS] if pid in ch["_members"])
    return f"/v1.0/teams/{channel['_team']}/channels/{channel['id']}/messages"


@pytest.mark.parametrize(
    "payload",
    [
        {"attachments": False},
        {"attachments": [False]},
        {"attachments": [{}]},
        {"mentions": {}},
        {"mentions": [None]},
        {"mentions": [{"id": False}]},
        {"importance": 2},
        {"subject": []},
        {"summary": False},
        {"locale": {}},
        {"hostedContents": 0},
        {"hostedContents": [{}]},
        {
            "hostedContents": [
                {"@microsoft.graph.temporaryId": "1", "contentType": "image/雪", "contentBytes": "YQ=="}
            ]
        },
        {
            "hostedContents": [
                {"@microsoft.graph.temporaryId": "1", "contentType": "image/png", "contentBytes": "!!!"}
            ]
        },
    ],
)
async def test_message_invalid_nested_types_are_atomic(
    make_client: Callable[..., Any], payload: dict[str, Any]
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        path = channel_path(ctx)
        count = len(ctx.store[tm.MESSAGES])
        response = await client.post(path, json={"body": {"content": "Audit"}, **payload})
        assert response.status_code == 400, response.text
        assert len(ctx.store[tm.MESSAGES]) == count
        assert response.headers["request-id"] == response.json()["error"]["innerError"]["request-id"]


@pytest.mark.parametrize("location", ["channel", "reply", "chat"])
async def test_archiver_hosted_content_and_membership(make_client: Callable[..., Any], location: str) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        private = next(ch for ch in ctx.store[tm.CHANNELS] if ch["membershipType"] == "private")
        pid = private["_members"][0]
        path = f"/beta/teams/{private['_team']}/channels/{private['id']}/messages"
        outsider = next(p.id for p in ctx.world.active_employees if p.id not in private["_members"])
        if location == "chat":
            chat = next(iter(ctx.store[tm.CHATS]))
            pid = chat["_members"][0]
            outsider = next(p.id for p in ctx.world.active_employees if p.id not in chat["_members"])
            path = f"/beta/chats/{chat['id']}/messages"
        auth = bearer(await mint(client, BASE, user=pid))
        denied = bearer(await mint(client, BASE, user=outsider))
        if location == "reply":
            parent = (await client.post(path, headers=auth, json={"body": {"content": "Thread"}})).json()
            path += f"/{parent['id']}/replies"
        image = make_png()
        response = await client.post(
            path,
            headers=auth,
            json={
                "body": {
                    "contentType": "html",
                    "content": '<p>Image 雪</p><img src="../hostedContents/1/$value">',
                },
                "hostedContents": [
                    {
                        "@microsoft.graph.temporaryId": "1",
                        "contentType": "image/png",
                        "contentBytes": base64.b64encode(image).decode(),
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
        message = response.json()
        hosted_path = path + f"/{message['id']}/hostedContents"
        contents = (await client.get(hosted_path, headers=auth)).json()["value"]
        assert len(contents) == 1
        assert contents[0]["contentBytes"] is None and contents[0]["contentType"] is None
        entity = (await client.get(hosted_path + "/" + contents[0]["id"], headers=auth)).json()
        assert entity["id"] == contents[0]["id"]
        value_url = hosted_path + "/" + contents[0]["id"] + "/$value"
        value = await client.get(value_url, headers=auth)
        assert value.content == image and value.headers["content-type"] == "image/png"
        assert BASE + value_url in message["body"]["content"]
        assert (await client.get(value_url, headers=denied)).status_code == 404
        assert (await client.get(value_url)).status_code == 401
        assert (await client.get(hosted_path + "/unknown/$value", headers=auth)).status_code == 404
        assert (
            await client.patch(
                path + "/" + message["id"],
                headers=auth,
                json={"body": {"content": "Attempted replacement"}, "hostedContents": []},
            )
        ).status_code == 400


async def test_thread_order_reply_edit_and_repeat_softdelete(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        ctx.clock.freeze(parse_instant("2026-10-16T12:00:00.123Z"))
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        path = channel_path(ctx)
        first = (await client.post(path, json={"body": {"content": "First"}})).json()
        assert first["id"] == str(int(ctx.clock.now().timestamp() * 1000))
        ctx.clock.advance(timedelta(seconds=1))
        second = (await client.post(path, json={"body": {"content": "Second"}})).json()
        assert (await client.get(path + "?$top=1")).json()["value"][0]["id"] == second["id"]
        ctx.clock.advance(timedelta(seconds=1))
        reply_path = path + f"/{first['id']}/replies"
        reply = (await client.post(reply_path, json={"body": {"content": "Reply"}})).json()
        assert (await client.get(path + "?$top=1")).json()["value"][0]["id"] == first["id"]
        first_path = path + "/" + first["id"]
        for _ in range(2):
            assert (await client.post(first_path + "/softDelete")).status_code == 204
        assert (await client.get(reply_path)).json()["value"][0]["id"] == reply["id"]
        assert (await client.post(first_path + "/undoSoftDelete")).status_code == 204
        assert (await client.get(first_path)).json()["body"]["content"] == "First"
        assert (await client.post(first_path + "/undoSoftDelete")).status_code == 204
        assert (await client.get(first_path)).json()["body"]["content"] == "First"
        app = bearer(await mint(client, BASE, app_only=True))
        assert (await client.post(first_path + "/softDelete", headers=app)).status_code == 403
        assert (await client.get(path + "/delta?$top=0")).status_code == 400


async def test_expanded_replies_follow_continuation_without_loss(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        path = channel_path(ctx)
        parent = (await client.post(path, json={"body": {"content": "Long thread"}})).json()
        expected = []
        for index in range(203):
            response = await client.post(
                path + f"/{parent['id']}/replies", json={"body": {"content": str(index)}}
            )
            assert response.status_code == 201
            expected.append(response.json()["id"])
        expanded = (await client.get(path + "/" + parent["id"] + "?$expand=replies")).json()
        assert len(expanded["replies"]) == 200
        remainder = (await client.get(expanded["replies@odata.nextLink"])).json()
        assert [r["id"] for r in expanded["replies"] + remainder["value"]] == expected
        assert "@odata.nextLink" not in remainder


async def test_chat_user_alias_and_no_channel_only_operations(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        chat = next(iter(ctx.store[tm.CHATS]))
        pid = chat["_members"][0]
        client.headers.update(bearer(await mint(client, BASE, user=pid)))
        path = f"/v1.0/chats/{chat['id']}/messages"
        posted = (await client.post(path, json={"body": {"content": "Chat"}})).json()
        alias = f"/v1.0/users/{common.user_id(ctx.world, pid)}/chats/{chat['id']}/messages/{posted['id']}"
        assert (await client.post(alias + "/softDelete")).status_code == 204
        assert (await client.post(alias + "/undoSoftDelete")).status_code == 204
        assert (await client.get(path + "/delta")).status_code == 404
        assert (
            await client.post(path + f"/{posted['id']}/replies", json={"body": {"content": "No"}})
        ).status_code == 404


async def test_mention_and_attachment_edits_are_persisted(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        path = channel_path(ctx)
        message = (await client.post(path, json={"body": {"content": "Before"}})).json()
        path += "/" + message["id"]
        mentions = [
            {
                "id": 0,
                "mentionText": "Eleanor Whitfield",
                "mentioned": {"user": {"id": common.user_id(ctx.world, "p001")}},
            }
        ]
        attachments = [
            {
                "id": "audit",
                "contentType": "reference",
                "contentUrl": "https://halcyonadvisory.sharepoint.com/audit",
                "name": "Audit",
            }
        ]
        assert (
            await client.patch(
                path,
                json={
                    "body": {"content": '<at id="0">Eleanor Whitfield</at>'},
                    "mentions": mentions,
                    "attachments": attachments,
                },
            )
        ).status_code == 204
        edited = (await client.get(path)).json()
        assert edited["mentions"] == mentions and edited["attachments"] == attachments
        assert edited["body"]["contentType"] == "html"
