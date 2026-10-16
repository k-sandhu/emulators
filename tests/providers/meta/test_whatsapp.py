"""WhatsApp Business Cloud API: objects, templates, send + 24h window, media, webhooks."""

from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace
from typing import Any

import httpx

from tests.providers.meta.conftest import (
    CLOSED_WA_NAME,
    GRAPH,
    OPEN_WA_NAME,
    PAGE,
    SYS,
    Receiver,
    action,
    wa_number,
)


def wa_send_body(to: str, **extra: Any) -> dict[str, Any]:
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **extra}


async def test_waba_phone_and_phone_numbers(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    waba = await client.get(
        f"{GRAPH}/{ids.waba_id}",
        headers=SYS,
        params={"fields": "id,name,currency,timezone_id,message_template_namespace,account_review_status"},
    )
    assert waba.status_code == 200, waba.text
    body = waba.json()
    assert body["name"] == "Halcyon Advisory Group"
    assert body["message_template_namespace"]
    assert body["account_review_status"] == "APPROVED"

    phones = await client.get(f"{GRAPH}/{ids.waba_id}/phone_numbers", headers=SYS)
    assert [p["id"] for p in phones.json()["data"]] == [ids.phone_number_id]
    phone = await client.get(
        f"{GRAPH}/{ids.phone_number_id}",
        headers=SYS,
        params={
            "fields": "display_phone_number,verified_name,quality_rating,code_verification_status,"
            "messaging_limit_tier,platform_type,throughput"
        },
    )
    body = phone.json()
    assert body["display_phone_number"] == "+14165550177"
    assert body["quality_rating"] == "GREEN"
    assert body["code_verification_status"] == "VERIFIED"
    assert body["messaging_limit_tier"] == "TIER_1K"
    assert body["platform_type"] == "CLOUD_API"
    assert body["throughput"] == {"level": "STANDARD"}


async def test_business_profile_get_and_post(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    url = f"{GRAPH}/{ids.phone_number_id}/whatsapp_business_profile"
    r = await client.get(url, headers=SYS, params={"fields": "about,email,vertical,websites"})
    profile = r.json()["data"][0]
    assert profile["vertical"] == "PROF_SERVICES"
    assert profile["messaging_product"] == "whatsapp"
    updated = await client.post(
        url,
        headers=SYS,
        json={
            "messaging_product": "whatsapp",
            "about": "New about text",
            "email": "team@halcyonadvisory.example",
        },
    )
    assert updated.json() == {"success": True}
    again = (await client.get(url, headers=SYS)).json()["data"][0]
    assert again["about"] == "New about text"
    assert again["profile_picture_url"]
    bad = await client.post(url, headers=SYS, json={"messaging_product": "whatsapp", "vertical": "WRONG"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == 100


async def test_templates_seed_create_approve_delete(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    url = f"{GRAPH}/{ids.waba_id}/message_templates"
    listing = await client.get(url, headers=SYS)
    by_name = {t["name"]: t for t in listing.json()["data"]}
    assert by_name["hello_world"]["status"] == "APPROVED"
    assert by_name["appointment_reminder"]["status"] == "APPROVED"
    assert by_name["year_end_promo"]["status"] == "REJECTED"
    assert by_name["year_end_promo"]["rejected_reason"]

    created = await client.post(
        url,
        headers=SYS,
        json={
            "name": "invoice_ready",
            "language": "en_US",
            "category": "MARKETING",
            "components": [{"type": "BODY", "text": "Hi {{1}}, your invoice is ready."}],
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "PENDING"
    assert created.json()["id"].isdigit()
    approved = await action(client, "approve_template", {"name": "invoice_ready"})
    assert approved["status"] == "APPROVED"
    listed = await client.get(url, headers=SYS, params={"name": "invoice_ready"})
    assert listed.json()["data"][0]["status"] == "APPROVED"

    utility = await client.post(
        url,
        headers=SYS,
        json={
            "name": "receipt_ready",
            "language": "en_US",
            "category": "UTILITY",
            "components": [{"type": "BODY", "text": "Your receipt for {{1}} is ready."}],
        },
    )
    assert utility.json()["status"] == "APPROVED"  # UTILITY auto-approves in lenient mode
    duplicate = await client.post(
        url,
        headers=SYS,
        json={
            "name": "receipt_ready",
            "language": "en_US",
            "category": "UTILITY",
            "components": [{"type": "BODY", "text": "Again {{1}}."}],
        },
    )
    assert duplicate.status_code == 400
    deleted = await client.delete(url, headers=SYS, params={"name": "invoice_ready"})
    assert deleted.json() == {"success": True}
    assert (await client.get(url, headers=SYS, params={"name": "invoice_ready"})).json()["data"] == []


async def test_send_text_within_open_window(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    to = wa_number(client, OPEN_WA_NAME)
    r = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/messages",
        headers=SYS,
        json=wa_send_body(f"+{to}", type="text", text={"body": "Hi Paula, thanks!", "preview_url": False}),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["messaging_product"] == "whatsapp"
    assert body["contacts"] == [{"input": f"+{to}", "wa_id": to}]
    assert body["messages"][0]["id"].startswith("wamid.")
    assert body["messages"][0]["message_status"] == "accepted"


async def test_send_text_outside_window_is_131047(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    to = wa_number(client, CLOSED_WA_NAME)
    r = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/messages",
        headers=SYS,
        json=wa_send_body(to, type="text", text={"body": "Are you there?"}),
    )
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == 131047
    assert error["error_data"]["details"]


async def test_template_send_outside_window_and_errors(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    to = wa_number(client, CLOSED_WA_NAME)
    url = f"{GRAPH}/{ids.phone_number_id}/messages"
    ok = await client.post(
        url,
        headers=SYS,
        json=wa_send_body(
            to,
            type="template",
            template={
                "name": "appointment_reminder",
                "language": {"code": "en_US"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": "Hamid"},
                            {"type": "text", "text": "Tuesday, October 20"},
                            {"type": "text", "text": "10:00 am"},
                        ],
                    }
                ],
            },
        ),
    )
    assert ok.status_code == 200, ok.text
    missing = await client.post(
        url,
        headers=SYS,
        json=wa_send_body(to, type="template", template={"name": "nope", "language": {"code": "en_US"}}),
    )
    assert missing.status_code == 400 and missing.json()["error"]["code"] == 132001
    rejected = await client.post(
        url,
        headers=SYS,
        json=wa_send_body(
            to, type="template", template={"name": "year_end_promo", "language": {"code": "en_US"}}
        ),
    )
    assert rejected.status_code == 400 and rejected.json()["error"]["code"] == 132001
    mismatch = await client.post(
        url,
        headers=SYS,
        json=wa_send_body(
            to,
            type="template",
            template={
                "name": "document_request",
                "language": {"code": "en_US"},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": "Hamid"}]}],
            },
        ),
    )
    assert mismatch.status_code == 400 and mismatch.json()["error"]["code"] == 132000


async def test_send_validation_errors(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    url = f"{GRAPH}/{ids.phone_number_id}/messages"
    invalid = await client.post(url, headers=SYS, json=wa_send_body("123", type="text", text={"body": "x"}))
    assert invalid.status_code == 400 and invalid.json()["error"]["code"] == 131026
    unsupported = await client.post(url, headers=SYS, json=wa_send_body("14165550100", type="carousel"))
    assert unsupported.status_code == 400 and unsupported.json()["error"]["code"] == 131051
    no_product = await client.post(
        url, headers=SYS, json={"to": "14165550100", "type": "text", "text": {"body": "x"}}
    )
    assert no_product.status_code == 400 and no_product.json()["error"]["code"] == 100
    denied = await client.post(
        url, headers=PAGE, json=wa_send_body("14165550100", type="text", text={"body": "x"})
    )
    assert denied.status_code == 403 and denied.json()["error"]["code"] == 200


async def test_mark_as_read_and_reply_context(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    to = wa_number(client, OPEN_WA_NAME)
    inbound = await action(client, "inbound_whatsapp", {"from": f"+{to}", "text": "One more question"})
    url = f"{GRAPH}/{ids.phone_number_id}/messages"
    read = await client.post(
        url,
        headers=SYS,
        json={"messaging_product": "whatsapp", "status": "read", "message_id": inbound["message_id"]},
    )
    assert read.json() == {"success": True}
    reply = await client.post(
        url,
        headers=SYS,
        json=wa_send_body(
            to, type="text", text={"body": "Answer"}, context={"message_id": inbound["message_id"]}
        ),
    )
    assert reply.status_code == 200
    reaction = await client.post(
        url,
        headers=SYS,
        json=wa_send_body(to, type="reaction", reaction={"message_id": inbound["message_id"], "emoji": "👍"}),
    )
    assert reaction.status_code == 200


async def test_media_round_trip_and_send_by_id(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000005000136e4c25f0000000049454e44ae426082"
    )
    upload = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/media",
        headers=SYS,
        files={"file": ("pixel.png", png, "image/png")},
        data={"type": "image/png", "messaging_product": "whatsapp"},
    )
    assert upload.status_code == 200, upload.text
    media_id = upload.json()["id"]
    assert media_id.isdigit()
    meta = await client.get(f"{GRAPH}/{media_id}", headers=SYS)
    body = meta.json()
    assert body["mime_type"] == "image/png"
    assert body["sha256"] == hashlib.sha256(png).hexdigest()
    assert body["file_size"] == len(png)
    assert "lookaside.fbsbx.com/whatsapp_business/attachments/" in body["url"]
    assert "mid=" in body["url"]
    unauthenticated = await client.get(body["url"])
    assert unauthenticated.status_code == 401
    downloaded = await client.get(body["url"], headers=SYS)
    assert downloaded.status_code == 200 and downloaded.content == png

    to = wa_number(client, OPEN_WA_NAME)
    sent = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/messages",
        headers=SYS,
        json=wa_send_body(to, type="image", image={"id": media_id, "caption": "A pixel"}),
    )
    assert sent.status_code == 200, sent.text
    deleted = await client.delete(f"{GRAPH}/{media_id}", headers=SYS)
    assert deleted.json() == {"success": True}
    gone = await client.get(f"{GRAPH}/{media_id}", headers=SYS)
    assert gone.status_code == 400


async def test_status_webhooks_after_send_are_signed(
    client: httpx.AsyncClient, ids: SimpleNamespace, receiver: Receiver
) -> None:
    configured = await receiver.configure(client, "whatsapp_business_account")
    assert configured["success"] is True
    to = wa_number(client, OPEN_WA_NAME)
    sent = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/messages",
        headers=SYS,
        json=wa_send_body(to, type="text", text={"body": "status please"}),
    )
    message_id = sent.json()["messages"][0]["id"]
    statuses = []
    for delivery in receiver.deliveries:
        expected = "sha256=" + hmac.new(ids.app_secret.encode(), delivery["body"], hashlib.sha256).hexdigest()
        assert delivery["headers"]["x-hub-signature-256"] == expected
        payload = delivery["json"]
        assert payload["object"] == "whatsapp_business_account"
        value = payload["entry"][0]["changes"][0]["value"]
        assert payload["entry"][0]["id"] == ids.waba_id
        assert value["metadata"]["phone_number_id"] == ids.phone_number_id
        assert value["metadata"]["display_phone_number"] == "14165550177"
        status = value["statuses"][0]
        assert status["id"] == message_id
        assert status["recipient_id"] == to
        assert status["pricing"]["pricing_model"] == "PMP"
        assert status["conversation"]["id"]
        assert status["conversation"]["origin"]["type"] == "service"
        statuses.append(status["status"])
    assert statuses == ["sent", "delivered", "read"]


async def test_inbound_action_opens_window_and_fires_webhook(
    client: httpx.AsyncClient, ids: SimpleNamespace, receiver: Receiver
) -> None:
    await receiver.configure(client, "whatsapp_business_account")
    to = wa_number(client, CLOSED_WA_NAME)
    result = await action(
        client, "inbound_whatsapp", {"from": "c012-2", "text": "Hi, I have a question about my return."}
    )
    assert result["wa_id"] == to
    assert result["message_id"].startswith("wamid.")
    payloads = receiver.payloads("whatsapp_business_account")
    assert len(payloads) == 1
    value = payloads[0]["entry"][0]["changes"][0]["value"]
    assert value["contacts"][0]["profile"]["name"] == CLOSED_WA_NAME
    message = value["messages"][0]
    assert message["from"] == to and message["type"] == "text"
    assert message["text"]["body"].startswith("Hi, I have a question")
    assert message["timestamp"].isdigit()
    send = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/messages",
        headers=SYS,
        json=wa_send_body(to, type="text", text={"body": "Of course, go ahead."}),
    )
    assert send.status_code == 200, send.text


async def test_deliver_statuses_action(
    client: httpx.AsyncClient, ids: SimpleNamespace, receiver: Receiver
) -> None:
    await receiver.configure(client, "whatsapp_business_account")
    to = wa_number(client, OPEN_WA_NAME)
    sent = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/messages",
        headers=SYS,
        json=wa_send_body(to, type="text", text={"body": "ping"}),
    )
    receiver.deliveries.clear()
    result = await action(
        client,
        "deliver_statuses",
        {"message_id": sent.json()["messages"][0]["id"], "status": "failed", "error_code": 131026},
    )
    assert result["status"] == "failed"
    value = receiver.payloads("whatsapp_business_account")[0]["entry"][0]["changes"][0]["value"]
    status = value["statuses"][0]
    assert status["status"] == "failed"
    assert status["errors"][0]["code"] == 131026


async def test_subscribed_apps_and_register(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    apps = await client.get(f"{GRAPH}/{ids.waba_id}/subscribed_apps", headers=SYS)
    assert apps.json()["data"][0]["whatsapp_business_api_data"]["id"] == ids.app_id
    removed = await client.delete(f"{GRAPH}/{ids.waba_id}/subscribed_apps", headers=SYS)
    assert removed.json() == {"success": True}
    assert (await client.get(f"{GRAPH}/{ids.waba_id}/subscribed_apps", headers=SYS)).json()["data"] == []
    added = await client.post(f"{GRAPH}/{ids.waba_id}/subscribed_apps", headers=SYS, json={})
    assert added.json() == {"success": True}
    registered = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/register",
        headers=SYS,
        json={"messaging_product": "whatsapp", "pin": "123456"},
    )
    assert registered.json() == {"success": True}
    deregistered = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/deregister", headers=SYS, json={"messaging_product": "whatsapp"}
    )
    assert deregistered.json() == {"success": True}
    owned = await client.get(f"{GRAPH}/{ids.business_id}/owned_whatsapp_business_accounts", headers=SYS)
    assert owned.json()["data"][0]["id"] == ids.waba_id


async def test_send_interactive_location_and_document_link(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    to = wa_number(client, OPEN_WA_NAME)
    url = f"{GRAPH}/{ids.phone_number_id}/messages"
    interactive = await client.post(
        url,
        headers=SYS,
        json=wa_send_body(
            to,
            type="interactive",
            interactive={
                "type": "button",
                "body": {"text": "Confirm tomorrow's call?"},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "confirm", "title": "Confirm"}},
                        {"type": "reply", "reply": {"id": "reschedule", "title": "Reschedule"}},
                    ]
                },
            },
        ),
    )
    assert interactive.status_code == 200, interactive.text
    location = await client.post(
        url,
        headers=SYS,
        json=wa_send_body(
            to,
            type="location",
            location={
                "latitude": 43.6465,
                "longitude": -79.38,
                "name": "Halcyon Toronto",
                "address": "200 Bay St",
            },
        ),
    )
    assert location.status_code == 200
    document = await client.post(
        url,
        headers=SYS,
        json=wa_send_body(
            to,
            type="document",
            document={
                "link": "https://www.halcyonadvisory.example/files/engagement-letter.pdf",
                "filename": "engagement-letter.pdf",
                "caption": "Please sign",
            },
        ),
    )
    assert document.status_code == 200


async def test_inbound_image_media_is_downloadable(
    client: httpx.AsyncClient, ids: SimpleNamespace, receiver: Receiver
) -> None:
    await receiver.configure(client, "whatsapp_business_account")
    result = await action(
        client,
        "inbound_whatsapp",
        {"from": "c011-4", "type": "image", "caption": "Signed letter attached"},
    )
    assert result["type"] == "image"
    value = receiver.payloads("whatsapp_business_account")[0]["entry"][0]["changes"][0]["value"]
    image = value["messages"][0]["image"]
    assert image["caption"] == "Signed letter attached"
    meta = await client.get(f"{GRAPH}/{image['id']}", headers=SYS)
    assert meta.json()["mime_type"] == "image/png"
    downloaded = await client.get(meta.json()["url"], headers=SYS)
    assert downloaded.content[:4] == b"\x89PNG"
