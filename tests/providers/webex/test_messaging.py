from __future__ import annotations

import httpx
import pytest

from emulators.testing.helpers import bearer, mint


async def test_seeded_private_room_visibility_and_messages(client: httpx.AsyncClient) -> None:
    rooms = (await client.get("/v1/rooms")).json()["items"]
    audit = next(r for r in rooms if r["title"] == "Audit & Assurance - General")
    messages = await client.get("/v1/messages", params={"roomId": audit["id"], "max": 2})
    assert messages.status_code == 200 and len(messages.json()["items"]) == 2
    assert "next" in messages.links
    before = await client.get(
        "/v1/messages", params={"roomId": audit["id"], "before": messages.json()["items"][0]["created"]}
    )
    assert all(m["created"] < messages.json()["items"][0]["created"] for m in before.json()["items"])
    previous = await client.get(
        "/v1/messages", params={"roomId": audit["id"], "beforeMessage": messages.json()["items"][0]["id"]}
    )
    assert messages.json()["items"][0]["id"] not in {m["id"] for m in previous.json()["items"]}
    other = bearer(await mint(client, "https://webexapis.com", user="p018"))
    assert audit["id"] not in {
        r["id"] for r in (await client.get("/v1/rooms", headers=other)).json()["items"]
    }
    for url in (f"/v1/rooms/{audit['id']}", f"/v1/messages/{messages.json()['items'][0]['id']}"):
        assert (await client.get(url, headers=other)).status_code == 404
    assert (
        await client.get("/v1/messages", params={"roomId": audit["id"]}, headers=other)
    ).status_code == 404
    assert (
        await client.get("/v1/memberships", params={"roomId": audit["id"]}, headers=other)
    ).status_code == 404
    assert (
        await client.post("/v1/messages", json={"roomId": audit["id"], "text": "Denied"}, headers=other)
    ).status_code == 404


async def test_room_membership_message_lifecycle_and_mentions(client: httpx.AsyncClient) -> None:
    created = await client.post("/v1/rooms", json={"title": "Meeting import review"})
    assert created.status_code == 200
    room = created.json()
    assert (await client.get(f"/v1/rooms/{room['id']}")).json() == room
    assert (
        await client.put(f"/v1/rooms/{room['id']}", json={"title": "Updated review", "isLocked": True})
    ).status_code == 200
    p = (await client.get("/v1/people", params={"email": "maya.chen@halcyonadvisory.example"})).json()[
        "items"
    ][0]
    member = await client.post("/v1/memberships", json={"roomId": room["id"], "personId": p["id"]})
    assert member.status_code == 200
    membership = member.json()
    assert (await client.get(f"/v1/memberships/{membership['id']}")).json() == membership
    maya = bearer(await mint(client, "https://webexapis.com", user="p018"))
    assert (await client.get(f"/v1/rooms/{room['id']}", headers=maya)).status_code == 200
    assert (
        await client.put(f"/v1/rooms/{room['id']}", headers=maya, json={"title": "Denied"})
    ).status_code == 403
    assert (
        await client.post(
            "/v1/memberships",
            headers=maya,
            json={"roomId": room["id"], "personEmail": "arjun.mehta@halcyonadvisory.example"},
        )
    ).status_code == 403
    assert (
        await client.post("/v1/memberships", json={"roomId": room["id"], "personId": p["id"]})
    ).status_code == 409
    sent = await client.post(
        "/v1/messages",
        json={
            "roomId": room["id"],
            "markdown": f"Hello <@personId:{p['id']}>",
            "files": ["https://files.example/agenda.pdf"],
        },
    )
    assert sent.status_code == 200
    message = sent.json()
    mentioned = (
        await client.get("/v1/messages", headers=maya, params={"roomId": room["id"], "mentionedPeople": "me"})
    ).json()["items"]
    assert [m["id"] for m in mentioned] == [message["id"]]
    assert (await client.get(f"/v1/messages/{message['id']}")).json()["files"] == [
        "https://files.example/agenda.pdf"
    ]
    assert (await client.delete(f"/v1/messages/{message['id']}", headers=maya)).status_code == 403
    assert (await client.delete(f"/v1/messages/{message['id']}")).status_code == 204
    assert (await client.get(f"/v1/messages/{message['id']}")).status_code == 404
    assert (
        await client.put(f"/v1/memberships/{membership['id']}", json={"isModerator": True})
    ).status_code == 200
    assert (await client.delete(f"/v1/memberships/{membership['id']}")).status_code == 204
    assert (await client.get(f"/v1/rooms/{room['id']}", headers=maya)).status_code == 404
    assert (await client.delete(f"/v1/rooms/{room['id']}")).status_code == 204
    assert (await client.get(f"/v1/rooms/{room['id']}")).status_code == 404


async def test_direct_bot_conversation_and_room_reuse(client: httpx.AsyncClient) -> None:
    payload = {"toPersonEmail": "halcyon-notes-bot@halcyonadvisory.example", "text": "Import this meeting"}
    message = (await client.post("/v1/messages", json=payload)).json()
    assert message["roomType"] == "direct"
    repeat = (await client.post("/v1/messages", json=payload)).json()
    assert repeat["roomId"] == message["roomId"]
    bot = bearer("webex-bot-token")
    rooms = (await client.get("/v1/rooms", headers=bot, params={"type": "direct"})).json()["items"]
    assert len(rooms) == 1 and rooms[0]["id"] == message["roomId"]
    assert rooms[0]["title"] == "Sofia Alvarez"
    assert (await client.get(f"/v1/messages/{message['id']}", headers=bot)).status_code == 200
    reply = await client.post(
        "/v1/messages",
        headers=bot,
        json={"roomId": message["roomId"], "text": "Ready", "parentId": message["id"]},
    )
    assert reply.status_code == 200
    assert (await client.get("/v1/meetings", headers=bot)).status_code == 403


async def test_bot_group_visibility_requires_mention(client: httpx.AsyncClient) -> None:
    room = (await client.post("/v1/rooms", json={"title": "Notes bot room"})).json()
    assert (
        await client.post(
            "/v1/memberships",
            json={"roomId": room["id"], "personEmail": "halcyon-notes-bot@halcyonadvisory.example"},
        )
    ).status_code == 200
    bot_auth = bearer("webex-bot-token")
    bot = (await client.get("/v1/people/me", headers=bot_auth)).json()
    plain = (
        await client.post("/v1/messages", json={"roomId": room["id"], "text": "Unaddressed conversation"})
    ).json()
    assert (await client.get("/v1/messages", params={"roomId": room["id"]}, headers=bot_auth)).json() == {
        "items": []
    }
    assert (await client.get(f"/v1/messages/{plain['id']}", headers=bot_auth)).status_code == 404
    mention = await client.post(
        "/v1/messages", json={"roomId": room["id"], "markdown": f"<@personId:{bot['id']}> import this"}
    )
    assert mention.status_code == 200
    assert [
        m["id"]
        for m in (await client.get("/v1/messages", params={"roomId": room["id"]}, headers=bot_auth)).json()[
            "items"
        ]
    ] == [mention.json()["id"]]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"roomId": "missing", "text": "x"},
        {"toPersonEmail": "missing@example.invalid", "text": "x"},
        {"toPersonEmail": "maya.chen@halcyonadvisory.example", "files": "invalid"},
        {"toPersonEmail": "maya.chen@halcyonadvisory.example", "text": 17},
    ],
)
async def test_invalid_messages(client: httpx.AsyncClient, payload: dict[str, object]) -> None:
    assert (await client.post("/v1/messages", json=payload)).status_code in (400, 404)
