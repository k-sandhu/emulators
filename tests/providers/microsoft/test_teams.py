from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.clock import isoformat_z
from emulators.providers.microsoft import common
from emulators.providers.microsoft import teams_model as tm
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


@pytest.mark.parametrize("version", ["v1.0", "beta"])
async def test_team_members_channels_private_visibility_and_messages(
    make_client: Callable[..., Any], version: str
) -> None:
    async with make_client("microsoft") as client:
        ctx = client.hub.context("microsoft")
        private = next(c for c in ctx.store[tm.CHANNELS] if c["membershipType"] == "private")
        team = ctx.store[tm.TEAMS].get(private["_team"])
        member = private["_members"][0]
        nonmember = next(p for p in team["_members"] if p not in private["_members"])
        auth = bearer(await mint(client, BASE, user=member))
        other = bearer(await mint(client, BASE, user=nonmember))
        base = BASE + "/" + version
        url = base + f"/teams/{team['id']}"
        teams = (await client.get(base + "/me/joinedTeams", headers=auth)).json()["value"]
        assert team["id"] in [t["id"] for t in teams]
        uid = common.user_id(ctx.world, member)
        assert (await client.get(base + f"/users/{uid}/joinedTeams", headers=auth)).status_code == 200
        assert (await client.get(url, headers=auth)).json()["displayName"] == team["displayName"]
        members = (await client.get(url + "/members", headers=auth)).json()["value"]
        assert members[0]["@odata.type"] == "#microsoft.graph.aadUserConversationMember"
        assert any("owner" in m["roles"] for m in members)
        channels = (await client.get(url + "/channels", headers=other)).json()["value"]
        assert private["id"] not in [c["id"] for c in channels]
        channel_url = url + "/channels/" + private["id"]
        assert (await client.get(channel_url, headers=auth)).status_code == 200
        assert (await client.get(channel_url, headers=other)).status_code == 404
        assert (await client.get(channel_url + "/members", headers=auth)).json()["value"]
        assert (await client.get(channel_url + "/messages", headers=other)).status_code == 404
        primary = (await client.get(url + "/primaryChannel", headers=auth)).json()
        assert primary["displayName"] == "General"
        message_url = url + f"/channels/{primary['id']}/messages"
        messages = (await client.get(message_url + "?$top=2&$expand=replies", headers=auth)).json()
        assert len(messages["value"]) == 2
        assert all(m["replyToId"] is None for m in messages["value"])
        assert all("replies" in m for m in messages["value"])
        assert (await client.get(messages["@odata.nextLink"], headers=auth)).status_code == 200
        first = messages["value"][0]
        assert first["body"]["contentType"] == "html"
        assert first["from"]["user"]["@odata.type"] == "#microsoft.graph.teamworkUserIdentity"
        assert (await client.get(message_url + "/" + first["id"], headers=auth)).json()["id"] == first["id"]


async def test_channel_post_reply_edit_delete_delta_and_app_export(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        teams = (await client.get(BASE + "/v1.0/me/joinedTeams", headers=auth)).json()["value"]
        tid = teams[0]["id"]
        channel = (await client.get(BASE + f"/v1.0/teams/{tid}/primaryChannel", headers=auth)).json()
        url = BASE + f"/v1.0/teams/{tid}/channels/{channel['id']}/messages"
        delta = (await client.get(url + "/delta?token=latest", headers=auth)).json()
        post = await client.post(
            url, headers=auth, json={"body": {"contentType": "html", "content": "<p>Contract test</p>"}}
        )
        assert post.status_code == 201, post.text
        message = post.json()
        assert message["id"].isdigit()
        assert message["from"]["user"]["id"] == common.user_id(client.hub.world, "p018")
        reply = await client.post(
            url + f"/{message['id']}/replies",
            headers=auth,
            json={"body": {"contentType": "text", "content": "Reply"}},
        )
        assert reply.status_code == 201 and reply.json()["replyToId"] == message["id"]
        replies = (await client.get(url + f"/{message['id']}/replies", headers=auth)).json()["value"]
        assert replies[0]["id"] == reply.json()["id"]
        assert (
            await client.get(url + f"/{message['id']}/replies/{reply.json()['id']}", headers=auth)
        ).status_code == 200
        edited = await client.patch(
            url + "/" + message["id"],
            headers=auth,
            json={"body": {"contentType": "html", "content": "<p>Edited</p>"}},
        )
        assert edited.status_code == 204
        get = (await client.get(url + "/" + message["id"], headers=auth)).json()
        assert get["lastEditedDateTime"] and get["messageHistory"]
        other = bearer(await mint(client, BASE, user="p001"))
        assert (
            await client.patch(
                url + "/" + message["id"], headers=other, json={"body": {"content": "not mine"}}
            )
        ).status_code == 403
        assert (await client.post(url + "/" + message["id"] + "/softDelete", headers=auth)).status_code == 204
        deleted = (await client.get(url + "/" + message["id"], headers=auth)).json()
        assert deleted["deletedDateTime"] and deleted["body"]["content"] == ""
        changes = (await client.get(delta["@odata.deltaLink"], headers=auth)).json()
        assert {m["id"] for m in changes["value"]} == {message["id"], reply.json()["id"]}
        assert (await client.get(changes["@odata.deltaLink"], headers=auth)).json()["value"] == []
        assert (
            await client.post(url + "/" + message["id"] + "/undoSoftDelete", headers=auth)
        ).status_code == 204
        assert (await client.get(url + "/delta?token=invalid", headers=auth)).status_code == 410
        export = BASE + f"/v1.0/teams/{tid}/channels/getAllMessages"
        assert (await client.get(export, headers=auth)).status_code == 403
        app = bearer(await mint(client, BASE, app_only=True))
        assert (await client.get(export, headers=app)).status_code == 200
        assert (await client.post(url, headers=app, json={"body": {"content": "app"}})).status_code == 403


async def test_chat_members_expansion_posts_and_export_permissions(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        ctx = client.hub.context("microsoft")
        chat = next(iter(ctx.store[tm.CHATS]))
        pid = chat["_members"][0]
        outsider = next(p for p in ctx.world.active_employees if p.id not in chat["_members"])
        auth = bearer(await mint(client, BASE, user=pid))
        other = bearer(await mint(client, BASE, user=outsider.id))
        chats = (
            await client.get(BASE + "/beta/me/chats?$expand=members,lastMessagePreview", headers=auth)
        ).json()["value"]
        expanded = next(c for c in chats if c["id"] == chat["id"])
        assert expanded["members"] and expanded["lastMessagePreview"]["body"]["content"]
        url = BASE + "/beta/chats/" + chat["id"]
        assert (await client.get(url, headers=auth)).status_code == 200
        assert (await client.get(url, headers=other)).status_code == 404
        assert (
            (await client.get(url + "/members", headers=auth))
            .json()["value"][0]["email"]
            .endswith(".example")
        )
        posted = await client.post(
            url + "/messages",
            headers=auth,
            json={"body": {"contentType": "html", "content": "<p>Chat test</p>"}},
        )
        assert posted.status_code == 201, posted.text
        assert posted.json()["chatId"] == chat["id"] and posted.json()["channelIdentity"] is None
        assert (await client.get(url + "/messages?$top=1", headers=auth)).json()["value"][0][
            "id"
        ] == posted.json()["id"]
        assert (
            await client.post(url + "/messages", headers=other, json={"body": {"content": "denied"}})
        ).status_code == 404
        uid = common.user_id(ctx.world, pid)
        assert (await client.get(BASE + f"/beta/users/{uid}/chats", headers=auth)).status_code == 200
        assert (
            await client.get(BASE + f"/beta/users/{uid}/chats/getAllMessages", headers=auth)
        ).status_code == 403
        app = bearer(await mint(client, BASE, app_only=True))
        assert (
            await client.get(BASE + f"/beta/users/{uid}/chats/getAllMessages", headers=app)
        ).status_code == 200


async def test_channel_change_notifications_and_full_delta_paging(make_client: Callable[..., Any]) -> None:
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
        team = (await client.get(BASE + "/v1.0/me/joinedTeams", headers=auth)).json()["value"][0]
        channel = (await client.get(BASE + f"/v1.0/teams/{team['id']}/primaryChannel", headers=auth)).json()
        resource = f"teams/{team['id']}/channels/{channel['id']}/messages"
        subscribed = await client.post(
            BASE + "/v1.0/subscriptions",
            headers=auth,
            json={
                "changeType": "created",
                "resource": resource,
                "notificationUrl": "https://receiver.example/hook",
                "expirationDateTime": isoformat_z(client.hub.clock.now() + timedelta(minutes=30)),
                "clientState": "channel-contract",
            },
        )
        assert subscribed.status_code == 201, subscribed.text
        posted = await client.post(
            BASE + "/v1.0/" + resource,
            headers=auth,
            json={"body": {"contentType": "html", "content": "<p>notify</p>"}},
        )
        assert posted.status_code == 201
        assert len(received) == 1
        assert received[0]["value"][0]["resourceData"]["id"] == posted.json()["id"]
        delta = (await client.get(BASE + "/v1.0/" + resource + "/delta?$top=5", headers=auth)).json()
        ids = [r["id"] for r in delta["value"]]
        while "@odata.nextLink" in delta:
            delta = (await client.get(delta["@odata.nextLink"], headers=auth)).json()
            ids.extend(r["id"] for r in delta["value"])
        assert len(ids) == len(set(ids)) and "@odata.deltaLink" in delta
