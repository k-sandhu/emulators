"""Messenger Platform: Send API windows/tags, conversations, profiles, webhooks."""

from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace
from typing import Any

import httpx

from tests.providers.meta.conftest import GRAPH, PAGE, SYS, USER, Receiver, action


async def conversations(client: httpx.AsyncClient, ids: SimpleNamespace) -> list[dict[str, Any]]:
    r = await client.get(
        f"{GRAPH}/{ids.page_id}/conversations",
        headers=PAGE,
        params={"platform": "messenger", "fields": "participants,updated_time,message_count,unread_count"},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


def psid_of(conversation: dict[str, Any], page_id: str) -> str:
    for participant in conversation["participants"]["data"]:
        if participant["id"] != page_id:
            return str(participant["id"])
    raise AssertionError("no non-page participant")


def by_name(data: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for conversation in data:
        names = [p.get("name") for p in conversation["participants"]["data"]]
        if name in names:
            return conversation
    raise AssertionError(f"no conversation with {name}")


async def test_conversations_list_with_nested_message_fields(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    r = await client.get(
        f"{GRAPH}/{ids.page_id}/conversations",
        headers=PAGE,
        params={
            "platform": "messenger",
            "fields": "participants,updated_time,message_count,unread_count,"
            "messages.limit(2){message,from,created_time}",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert len(data) == 6
    first = data[0]  # most recently updated first (Alice, with the recent follow-up)
    assert first["message_count"] >= 5
    assert len(first["messages"]["data"]) == 2
    message = first["messages"]["data"][0]
    assert set(message) >= {"id", "message", "from", "created_time"}
    assert message["created_time"].endswith("+0000")
    # Page token required: a user token is refused.
    denied = await client.get(f"{GRAPH}/{ids.page_id}/conversations", headers=USER)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == 200


async def test_conversation_and_message_objects(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    data = await conversations(client, ids)
    conv = by_name(data, "Alice Kaur")
    one = await client.get(f"{GRAPH}/{conv['id']}", headers=PAGE, params={"fields": "messages,participants"})
    assert one.json()["id"] == conv["id"]
    assert len(one.json()["messages"]["data"]) == conv["message_count"]
    edge = await client.get(f"{GRAPH}/{conv['id']}/messages", headers=PAGE, params={"limit": 2})
    stubs = edge.json()["data"]
    assert len(stubs) == 2 and stubs[0]["id"].startswith("m_")
    assert "message" not in stubs[0]  # edge stubs carry id + created_time by default
    assert edge.json()["paging"]["next"]
    page_two = await client.get(edge.json()["paging"]["next"], headers=PAGE)
    assert page_two.json()["data"][0]["id"] != stubs[0]["id"]
    full = await client.get(
        f"{GRAPH}/{stubs[0]['id']}",
        headers=PAGE,
        params={"fields": "id,created_time,from,to,message,attachments"},
    )
    body = full.json()
    assert body["message"]
    assert body["from"]["id"] == psid_of(conv, ids.page_id)
    assert body["to"]["data"][0]["id"] == ids.page_id


async def test_send_within_window_and_sender_actions(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    data = await conversations(client, ids)
    alice = by_name(data, "Alice Kaur")
    psid = psid_of(alice, ids.page_id)
    sent = await client.post(
        f"{GRAPH}/me/messages",
        headers=PAGE,
        json={
            "recipient": {"id": psid},
            "messaging_type": "RESPONSE",
            "message": {
                "text": "Tuesday morning works!",
                "quick_replies": [{"content_type": "text", "title": "Confirm", "payload": "CONFIRM"}],
            },
        },
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["recipient_id"] == psid
    assert sent.json()["message_id"].startswith("m_")
    typing = await client.post(
        f"{GRAPH}/me/messages", headers=PAGE, json={"recipient": {"id": psid}, "sender_action": "typing_on"}
    )
    assert typing.json() == {"recipient_id": psid}
    seen = await client.post(
        f"{GRAPH}/{ids.page_id}/messages",
        headers=PAGE,
        json={"recipient": {"id": psid}, "sender_action": "mark_seen"},
    )
    assert seen.status_code == 200
    refreshed = by_name(await conversations(client, ids), "Alice Kaur")
    assert refreshed["unread_count"] == 0
    assert refreshed["message_count"] == alice["message_count"] + 1


async def test_send_outside_window_and_tags(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    data = await conversations(client, ids)
    melissa = by_name(data, "Melissa Grimaldi")  # last inbound weeks before the anchor
    psid = psid_of(melissa, ids.page_id)
    url = f"{GRAPH}/me/messages"
    blocked = await client.post(
        url,
        headers=PAGE,
        json={"recipient": {"id": psid}, "messaging_type": "RESPONSE", "message": {"text": "Hello?"}},
    )
    assert blocked.status_code == 400
    error = blocked.json()["error"]
    assert error["code"] == 10 and error["error_subcode"] == 2018278
    assert "outside of allowed window" in error["message"]
    human_agent = await client.post(
        url,
        headers=PAGE,
        json={
            "recipient": {"id": psid},
            "messaging_type": "MESSAGE_TAG",
            "tag": "HUMAN_AGENT",
            "message": {"text": "Following up personally"},
        },
    )
    assert human_agent.status_code == 400 and human_agent.json()["error"]["error_subcode"] == 2018278
    tagged = await client.post(
        url,
        headers=PAGE,
        json={
            "recipient": {"id": psid},
            "messaging_type": "MESSAGE_TAG",
            "tag": "ACCOUNT_UPDATE",
            "message": {"text": "Your account details changed."},
        },
    )
    assert tagged.status_code == 200, tagged.text
    no_tag = await client.post(
        url,
        headers=PAGE,
        json={"recipient": {"id": psid}, "messaging_type": "MESSAGE_TAG", "message": {"text": "x"}},
    )
    assert no_tag.status_code == 400 and no_tag.json()["error"]["code"] == 100
    unknown = await client.post(
        url, headers=PAGE, json={"recipient": {"id": "1234567890123456"}, "message": {"text": "x"}}
    )
    assert unknown.status_code == 400 and unknown.json()["error"]["error_subcode"] == 2018001
    denied = await client.post(url, headers=USER, json={"recipient": {"id": psid}, "message": {"text": "x"}})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == 200


async def test_user_profile_api(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    data = await conversations(client, ids)
    psid = psid_of(by_name(data, "Alice Kaur"), ids.page_id)
    r = await client.get(
        f"{GRAPH}/{psid}",
        headers=PAGE,
        params={"fields": "first_name,last_name,name,profile_pic,locale,timezone"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["first_name"] == "Alice" and body["last_name"] == "Kaur"
    assert body["profile_pic"].endswith(".png")
    assert body["locale"] == "en_US"
    assert isinstance(body["timezone"], int)
    denied = await client.get(f"{GRAPH}/{psid}", headers=SYS)
    assert denied.status_code == 403


async def test_messenger_profile_and_reusable_attachment(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    got = await client.get(
        f"{GRAPH}/me/messenger_profile", headers=PAGE, params={"fields": "greeting,get_started"}
    )
    item = got.json()["data"][0]
    assert item["greeting"][0]["locale"] == "default"
    assert item["get_started"] == {"payload": "GET_STARTED"}
    updated = await client.post(
        f"{GRAPH}/me/messenger_profile",
        headers=PAGE,
        json={"greeting": [{"locale": "default", "text": "Welcome!"}]},
    )
    assert updated.json() == {"result": "success"}
    again = await client.get(f"{GRAPH}/me/messenger_profile", headers=PAGE, params={"fields": "greeting"})
    assert again.json()["data"][0]["greeting"][0]["text"] == "Welcome!"
    deleted = await client.request(
        "DELETE", f"{GRAPH}/me/messenger_profile", headers=PAGE, json={"fields": ["greeting"]}
    )
    assert deleted.json() == {"result": "success"}

    upload = await client.post(
        f"{GRAPH}/me/message_attachments",
        headers=PAGE,
        json={
            "message": {
                "attachment": {
                    "type": "image",
                    "payload": {"url": "https://www.halcyonadvisory.example/logo.png", "is_reusable": True},
                }
            }
        },
    )
    assert upload.status_code == 200, upload.text
    attachment_id = upload.json()["attachment_id"]
    data = await conversations(client, ids)
    psid = psid_of(by_name(data, "Alice Kaur"), ids.page_id)
    sent = await client.post(
        f"{GRAPH}/me/messages",
        headers=PAGE,
        json={
            "recipient": {"id": psid},
            "message": {"attachment": {"type": "image", "payload": {"attachment_id": attachment_id}}},
        },
    )
    assert sent.status_code == 200, sent.text


async def test_inbound_messenger_webhook_and_postback(
    client: httpx.AsyncClient, ids: SimpleNamespace, receiver: Receiver
) -> None:
    await receiver.configure(client, "page")
    data = await conversations(client, ids)
    melissa = by_name(data, "Melissa Grimaldi")
    psid = psid_of(melissa, ids.page_id)
    result = await action(client, "inbound_messenger", {"psid": psid, "text": "Do you offer tax planning?"})
    assert result["psid"] == psid
    payload = receiver.payloads("page")[0]
    assert payload["object"] == "page"
    entry = payload["entry"][0]
    assert entry["id"] == ids.page_id and isinstance(entry["time"], int)
    messaging = entry["messaging"][0]
    assert messaging["sender"] == {"id": psid}
    assert messaging["recipient"] == {"id": ids.page_id}
    assert messaging["message"]["text"] == "Do you offer tax planning?"
    assert messaging["message"]["mid"] == result["mid"]
    delivery = receiver.deliveries[0]
    expected = "sha256=" + hmac.new(ids.app_secret.encode(), delivery["body"], hashlib.sha256).hexdigest()
    assert delivery["headers"]["x-hub-signature-256"] == expected
    assert delivery["headers"]["x-hub-signature"].startswith("sha1=")

    postback = await action(
        client,
        "inbound_messenger",
        {"person": "c023-2", "postback": {"payload": "GET_STARTED", "title": "Get Started"}},
    )
    assert postback["psid"] == psid
    latest = receiver.payloads("page")[-1]["entry"][0]["messaging"][0]
    assert latest["postback"]["payload"] == "GET_STARTED"
    # The inbound opened the 24h window for Melissa.
    send = await client.post(
        f"{GRAPH}/me/messages", headers=PAGE, json={"recipient": {"id": psid}, "message": {"text": "We do!"}}
    )
    assert send.status_code == 200, send.text
    kinds = [
        next(k for k in ("delivery", "read") if k in m)
        for p in receiver.payloads("page")
        for m in p["entry"][0]["messaging"]
        if "delivery" in m or "read" in m
    ]
    assert kinds == ["delivery", "read"]
