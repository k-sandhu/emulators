"""Gmail wire contracts through the vendor's real HTTP hostnames."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from emulators.testing.helpers import bearer, mint

BASE = "https://gmail.googleapis.com"
ROOT = BASE + "/gmail/v1/users/me"
SENDER = "maya.chen@halcyonadvisory.example"
RECIPIENT = "eleanor.whitfield@halcyonadvisory.example"


def raw_message(
    subject: str = "Contract planning",
    text: str = "Exact phrase for the review.",
    *,
    attachment: bool = False,
    **headers: str,
) -> str:
    message = EmailMessage(policy=policy.SMTP)
    for name, value in {
        "From": SENDER,
        "To": RECIPIENT,
        "Cc": "priya.raman@halcyonadvisory.example",
        "Date": "Thu, 15 Oct 2026 10:00:00 +0000",
        "Subject": subject,
        "Message-ID": "<contract-seed@halcyonadvisory.example>",
        **headers,
    }.items():
        message[name] = value
    message.set_content(text)
    message.add_alternative("<p>" + text + "</p>", subtype="html")
    if attachment:
        message.add_attachment(
            b"%PDF-1.4\ncontract\n", maintype="application", subtype="pdf", filename="Review.pdf"
        )
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


@pytest.fixture
async def gmail(make_client: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client("google") as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        yield client


async def insert(client: httpx.AsyncClient, **kwargs: Any) -> dict[str, Any]:
    response = await client.post(
        ROOT + "/messages", json={"raw": raw_message(**kwargs), "labelIds": ["INBOX", "UNREAD"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_profile_auth_visibility_and_host_alias(gmail: httpx.AsyncClient) -> None:
    profile = (await gmail.get(ROOT + "/profile")).json()
    assert profile["emailAddress"] == SENDER and profile["messagesTotal"] > 0
    assert profile["threadsTotal"] <= profile["messagesTotal"]
    assert (
        profile
        == (await gmail.get("https://www.googleapis.com/gmail/v1/users/" + SENDER + "/profile")).json()
    )
    denied = await gmail.get(BASE + "/gmail/v1/users/" + RECIPIENT + "/profile")
    assert denied.status_code == 403 and denied.json()["error"]["errors"][0]["reason"] == "forbidden"
    assert (
        await gmail.get(ROOT + "/profile", headers={"Authorization": "Bearer invalid"})
    ).status_code == 401
    app_token = await mint(gmail, BASE, app_only=True)
    assert (await gmail.get(ROOT + "/messages", headers=bearer(app_token))).status_code == 403


async def test_paging_formats_and_labels(gmail: httpx.AsyncClient) -> None:
    first = (await gmail.get(ROOT + "/messages", params={"maxResults": 2})).json()
    second = (
        await gmail.get(ROOT + "/messages", params={"maxResults": 2, "pageToken": first["nextPageToken"]})
    ).json()
    assert not {r["id"] for r in first["messages"]}.intersection(r["id"] for r in second["messages"])
    message_id = first["messages"][0]["id"]
    assert len(message_id) == 16 and int(message_id, 16)
    full = (await gmail.get(ROOT + "/messages/" + message_id)).json()
    assert full["payload"]["mimeType"] in ("multipart/alternative", "multipart/mixed")
    assert {h["name"].lower() for h in full["payload"]["headers"]} >= {
        "date",
        "from",
        "to",
        "message-id",
        "mime-version",
        "content-type",
        "received",
        "delivered-to",
    }
    raw = (await gmail.get(ROOT + "/messages/" + message_id, params={"format": "raw"})).json()
    parsed = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw["raw"] + "=="))
    assert parsed["Subject"]
    minimal = (await gmail.get(ROOT + "/messages/" + message_id, params={"format": "minimal"})).json()
    assert "payload" not in minimal and "raw" not in minimal
    metadata = (
        await gmail.get(
            ROOT + "/messages/" + message_id,
            params=[("format", "metadata"), ("metadataHeaders", "Subject"), ("metadataHeaders", "From")],
        )
    ).json()
    assert {h["name"] for h in metadata["payload"]["headers"]} == {"Subject", "From"}
    label = (await gmail.get(ROOT + "/labels/INBOX")).json()
    assert label["type"] == "system" and label["messagesTotal"] > 0
    assert (await gmail.get(ROOT + "/messages", params={"pageToken": "bad"})).status_code == 400
    assert (await gmail.get(ROOT + "/messages", params={"pageToken": "e30"})).status_code == 400


@pytest.mark.parametrize(
    ("query", "matches"),
    [
        ('subject:"Contract planning"', True),
        ("from:maya.chen to:eleanor.whitfield", True),
        ("cc:priya.raman bcc:no-one", False),
        ('"Exact phrase"', True),
        ("(subject:Contract OR subject:absent) -subject:absent", True),
        ("{subject:absent subject:Contract}", True),
        ("subject:(absent OR Contract)", True),
        ("is:unread -is:starred in:inbox", True),
        ("is:read", False),
        ("has:attachment filename:pdf", True),
        ("filename:xlsx", False),
        ("after:2026/10/01 before:2026/10/16", True),
        ("after:2026/10/16", False),
        ("newer_than:1d older_than:7d", False),
        ("larger:10 smaller:100k", True),
        ("label:INBOX review", True),
        ('-"Exact phrase"', False),
    ],
)
async def test_search_operators(gmail: httpx.AsyncClient, query: str, matches: bool) -> None:
    record = await insert(gmail, attachment=True)
    response = await gmail.get(ROOT + "/messages", params={"q": query})
    assert response.status_code == 200, response.text
    assert (record["id"] in {r["id"] for r in response.json().get("messages", [])}) is matches


@pytest.mark.parametrize("query", ["after:invalid", "larger:no", "newer_than:1w", "(abc", "abc OR"])
async def test_invalid_search_returns_google_400(gmail: httpx.AsyncClient, query: str) -> None:
    response = await gmail.get(ROOT + "/messages", params={"q": query})
    assert response.status_code == 400 and response.json()["error"]["errors"]


async def test_seed_attachment_is_real_and_lazy(gmail: httpx.AsyncClient) -> None:
    result = (await gmail.get(ROOT + "/messages", params={"q": "has:attachment"})).json()
    assert result["messages"]
    message_id = result["messages"][0]["id"]
    message = (await gmail.get(ROOT + "/messages/" + message_id)).json()

    def attachments(part: dict[str, Any]) -> list[dict[str, Any]]:
        return ([part] if "attachmentId" in part["body"] else []) + [
            r for child in part.get("parts", []) for r in attachments(child)
        ]

    for part in attachments(message["payload"]):
        response = await gmail.get(
            ROOT + "/messages/" + message_id + "/attachments/" + part["body"]["attachmentId"]
        )
        data = base64.urlsafe_b64decode(response.json()["data"] + "==")
        assert len(data) == response.json()["size"] == part["body"]["size"]
        if part["filename"].endswith(("docx", "xlsx", "pptx")):
            assert data.startswith(b"PK")
        if part["filename"].endswith("pdf"):
            assert data.startswith(b"%PDF")


async def test_send_reply_internal_delivery_and_bcc_privacy(gmail: httpx.AsyncClient) -> None:
    raw = raw_message(Bcc="marcus.okafor@halcyonadvisory.example")
    sent = (await gmail.post(ROOT + "/messages/send", json={"raw": raw})).json()
    assert sent["labelIds"] == ["SENT"]
    recipient_token = await mint(gmail, BASE, user="p001")
    recipient = await gmail.get(ROOT + "/messages/" + sent["id"], headers=bearer(recipient_token))
    assert recipient.status_code == 200 and recipient.json()["labelIds"] == ["INBOX", "UNREAD"]
    assert not any(h["name"] == "Bcc" for h in recipient.json()["payload"]["headers"])
    reply_raw = raw_message(
        subject="Re: Contract planning", **{"In-Reply-To": "<contract-seed@halcyonadvisory.example>"}
    )
    reply = (
        await gmail.post(ROOT + "/messages/send", json={"raw": reply_raw, "threadId": sent["threadId"]})
    ).json()
    assert reply["threadId"] == sent["threadId"]
    thread = (await gmail.get(ROOT + "/threads/" + sent["threadId"])).json()
    assert len(thread["messages"]) == 2 and thread["snippet"]
    wrong = await gmail.post(ROOT + "/messages/send", json={"raw": raw_message(From=RECIPIENT)})
    assert wrong.status_code == 403


async def test_message_thread_mutations_history_and_label_crud(gmail: httpx.AsyncClient) -> None:
    initial = (await gmail.get(ROOT + "/profile")).json()["historyId"]
    record = await insert(gmail)
    label = (
        await gmail.post(
            ROOT + "/labels",
            json={
                "name": "Integration/Review",
                "color": {"backgroundColor": "#16a765", "textColor": "#ffffff"},
            },
        )
    ).json()
    assert label["id"].startswith("Label_")
    for method in ("PATCH", "PUT"):
        response = await gmail.request(
            method,
            ROOT + "/labels/" + label["id"],
            json={"name": "Integration/Review", "labelListVisibility": "labelShowIfUnread"},
        )
        assert response.status_code == 200
    modified = await gmail.post(
        ROOT + "/messages/" + record["id"] + "/modify",
        json={"addLabelIds": [label["id"], "STARRED"], "removeLabelIds": ["UNREAD"]},
    )
    assert "STARRED" in modified.json()["labelIds"] and "UNREAD" not in modified.json()["labelIds"]
    assert (await gmail.get(ROOT + "/labels/" + label["id"])).json()["messagesTotal"] == 1
    for operation in ("trash", "untrash"):
        response = await gmail.post(ROOT + "/threads/" + record["threadId"] + "/" + operation)
        assert response.status_code == 200
        assert ("TRASH" in response.json()["messages"][0]["labelIds"]) is (operation == "trash")
    assert (
        await gmail.post(
            ROOT + "/threads/" + record["threadId"] + "/modify", json={"addLabelIds": ["IMPORTANT"]}
        )
    ).status_code == 200
    history = (await gmail.get(ROOT + "/history", params={"startHistoryId": initial, "maxResults": 2})).json()
    assert len(history["history"]) == 2 and history["nextPageToken"]
    filtered = (
        await gmail.get(
            ROOT + "/history",
            params={"startHistoryId": initial, "historyTypes": "labelAdded", "labelId": label["id"]},
        )
    ).json()
    assert all("labelsAdded" in h for h in filtered["history"])
    assert (await gmail.get(ROOT + "/history", params={"startHistoryId": "1"})).status_code == 404
    assert (await gmail.delete(ROOT + "/labels/" + label["id"])).status_code == 204
    assert label["id"] not in (await gmail.get(ROOT + "/messages/" + record["id"])).json()["labelIds"]
    assert (await gmail.delete(ROOT + "/threads/" + record["threadId"])).status_code == 204
    assert (await gmail.get(ROOT + "/messages/" + record["id"])).status_code == 404


async def test_draft_lifecycle_and_batch_operations(gmail: httpx.AsyncClient) -> None:
    draft = (await gmail.post(ROOT + "/drafts", json={"message": {"raw": raw_message()}})).json()
    assert draft["message"]["labelIds"] == ["DRAFT"]
    assert draft["id"] in [r["id"] for r in (await gmail.get(ROOT + "/drafts")).json()["drafts"]]
    updated = (
        await gmail.put(
            ROOT + "/drafts/" + draft["id"], json={"message": {"raw": raw_message(subject="Updated")}}
        )
    ).json()
    assert updated["message"]["id"] != draft["message"]["id"]
    assert (
        "raw"
        in (await gmail.get(ROOT + "/drafts/" + draft["id"], params={"format": "raw"})).json()["message"]
    )
    sent = (await gmail.post(ROOT + "/drafts/send", json={"id": draft["id"]})).json()
    assert sent["labelIds"] == ["SENT"]
    assert (await gmail.get(ROOT + "/drafts/" + draft["id"])).status_code == 404
    other = (await gmail.post(ROOT + "/drafts", json={"message": {"raw": raw_message()}})).json()
    assert (await gmail.delete(ROOT + "/drafts/" + other["id"])).status_code == 204
    assert (
        await gmail.post(
            ROOT + "/messages/batchModify", json={"ids": [sent["id"]], "addLabelIds": ["STARRED"]}
        )
    ).status_code == 204
    assert "STARRED" in (await gmail.get(ROOT + "/messages/" + sent["id"])).json()["labelIds"]
    assert (await gmail.post(ROOT + "/messages/batchDelete", json={"ids": [sent["id"]]})).status_code == 204
    assert (await gmail.get(ROOT + "/messages/" + sent["id"])).status_code == 404


async def test_media_and_multipart_upload_import_and_settings(gmail: httpx.AsyncClient) -> None:
    raw = base64.urlsafe_b64decode(raw_message(attachment=True) + "==")
    media = await gmail.post(
        BASE + "/upload/gmail/v1/users/me/messages/send",
        params={"uploadType": "media"},
        content=raw,
        headers={"Content-Type": "message/rfc822"},
    )
    assert media.status_code == 200, media.text
    multipart = (
        b'--boundary\r\nContent-Type: application/json\r\n\r\n{"labelIds":["INBOX"]}\r\n--boundary\r\nContent-Type: message/rfc822\r\n\r\n'
        + raw
        + b"\r\n--boundary--\r\n"
    )
    imported = await gmail.post(
        BASE + "/upload/gmail/v1/users/me/messages/import",
        params={"uploadType": "multipart"},
        content=multipart,
        headers={"Content-Type": "multipart/related; boundary=boundary"},
    )
    assert imported.status_code == 200, imported.text
    assert (await gmail.get(ROOT + "/settings/sendAs")).json()["sendAs"][0]["isPrimary"] is True
    assert (await gmail.get(ROOT + "/settings/vacation")).json()["enableAutoReply"] is False
    assert (
        await gmail.post(
            ROOT + "/settings/filters",
            json={"criteria": {"from": RECIPIENT}, "action": {"addLabelIds": ["IMPORTANT"]}},
        )
    ).status_code == 200
    assert len((await gmail.get(ROOT + "/settings/filters")).json()["filter"]) == 1


async def test_pubsub_push_stop_and_reset(gmail: httpx.AsyncClient) -> None:
    received = []
    receiver = FastAPI()

    @receiver.post("/push")
    async def hook(request: Request) -> dict[str, Any]:
        received.append(await request.json())
        return {}

    gmail.hub.webhook_transport = httpx.ASGITransport(app=receiver)  # type: ignore[attr-defined]
    topic = "projects/test-project/topics/gmail"
    await gmail.post(
        BASE + "/_emulator/actions/configure_pubsub_push",
        json={"topicName": topic, "endpoint": "http://receiver.example/push"},
    )
    watch = await gmail.post(
        ROOT + "/watch", json={"topicName": topic, "labelIds": ["INBOX"], "labelFilterBehavior": "include"}
    )
    assert watch.status_code == 200 and watch.json()["expiration"].isdigit()
    await insert(gmail)
    assert len(received) >= 2
    decoded = json.loads(base64.b64decode(received[-1]["message"]["data"]))
    assert decoded["emailAddress"] == SENDER and int(decoded["historyId"]) > int(watch.json()["historyId"])
    assert (await gmail.post(ROOT + "/stop")).status_code == 204
    count = len(received)
    await insert(gmail)
    assert len(received) == count
    await gmail.post(BASE + "/_emulator/reset")
    token = await mint(gmail, BASE, user="p018")
    assert (await gmail.get(ROOT + "/profile", headers=bearer(token))).json()["historyId"] == "1000"


async def test_direct_message_trash_delete_threads_and_repeated_labels(gmail: httpx.AsyncClient) -> None:
    record = await insert(gmail)
    assert record["threadId"] in {
        r["id"]
        for r in (await gmail.get(ROOT + "/threads", params={"q": 'subject:"Contract planning"'})).json()[
            "threads"
        ]
    }
    ids = (
        await gmail.get(ROOT + "/messages", params=[("labelIds", "INBOX"), ("labelIds", "UNREAD")])
    ).json()["messages"]
    assert record["id"] in {r["id"] for r in ids}
    assert (await gmail.post(ROOT + "/messages/" + record["id"] + "/trash")).status_code == 200
    trashed = (await gmail.get(ROOT + "/messages", params={"q": "in:trash"})).json()["messages"]
    assert record["id"] in {r["id"] for r in trashed}
    assert (await gmail.post(ROOT + "/messages/" + record["id"] + "/untrash")).status_code == 200
    assert (await gmail.delete(ROOT + "/messages/" + record["id"])).status_code == 204
    assert (await gmail.get(ROOT + "/messages/" + record["id"])).status_code == 404


async def test_explicit_scopes_and_inbound_filter(gmail: httpx.AsyncClient) -> None:
    readonly = bearer(
        await mint(gmail, BASE, user="p018", scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    )
    assert (await gmail.get(ROOT + "/messages", headers=readonly)).status_code == 200
    assert (
        await gmail.post(ROOT + "/messages/send", headers=readonly, json={"raw": raw_message()})
    ).status_code == 403
    metadata = bearer(
        await mint(gmail, BASE, user="p018", scopes=["https://www.googleapis.com/auth/gmail.metadata"])
    )
    record = await insert(gmail)
    assert (await gmail.get(ROOT + "/messages/" + record["id"], headers=metadata)).status_code == 403
    assert (
        await gmail.get(ROOT + "/messages/" + record["id"], headers=metadata, params={"format": "metadata"})
    ).status_code == 200
    assert (await gmail.get(ROOT + "/messages", headers=metadata, params={"q": "review"})).status_code == 403
    recipient = bearer(await mint(gmail, BASE, user="p001"))
    response = await gmail.post(
        ROOT + "/settings/filters",
        headers=recipient,
        json={
            "criteria": {"from": SENDER, "subject": "Contract"},
            "action": {"addLabelIds": ["IMPORTANT"], "removeLabelIds": ["UNREAD"]},
        },
    )
    assert response.status_code == 200
    sent = (await gmail.post(ROOT + "/messages/send", json={"raw": raw_message()})).json()
    delivered = (await gmail.get(ROOT + "/messages/" + sent["id"], headers=recipient)).json()
    assert "IMPORTANT" in delivered["labelIds"] and "UNREAD" not in delivered["labelIds"]


@pytest.mark.parametrize("body", ["{", "[]", "null"])
async def test_malformed_json_returns_400(gmail: httpx.AsyncClient, body: str) -> None:
    response = await gmail.post(ROOT + "/labels", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["error"]["errors"]
