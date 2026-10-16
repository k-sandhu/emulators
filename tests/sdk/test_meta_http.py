"""Drive Meta messaging APIs with the HTTP clients available in the dev environment.

Plain sync ``httpx`` and ``requests`` calls to the real ``graph.facebook.com``
hostnames, rerouted to the emulator by ``emulator_intercept`` — exactly what a
hand-written WhatsApp/Messenger integration (the common case) sends.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import requests

from emulators.providers.meta.common import PAGE_ACCESS_TOKEN, SYSTEM_USER_TOKEN, USER_ACCESS_TOKEN

pytestmark = pytest.mark.sdk

GRAPH = "https://graph.facebook.com/v21.0"
SYS = {"Authorization": f"Bearer {SYSTEM_USER_TOKEN}"}
PAGE = {"Authorization": f"Bearer {PAGE_ACCESS_TOKEN}"}
USER = {"Authorization": f"Bearer {USER_ACCESS_TOKEN}"}
# Seeded WhatsApp contacts (see docs/providers/meta.md): Paula recently replied
# (window open), Hamid last replied weeks ago (window closed).
PAULA = "13055550174"
HAMID = "13125550176"


@pytest.fixture(autouse=True)
def _intercept(emulator_intercept: object) -> None:
    pass


def _ids() -> dict:
    return requests.get("https://graph.facebook.com/_emulator/info", timeout=30).json()["credentials"]


def test_whatsapp_templates_and_sends_with_httpx() -> None:
    ids = _ids()
    with httpx.Client(timeout=30) as client:
        templates = client.get(
            f"{GRAPH}/{ids['waba_id']}/message_templates",
            headers=SYS,
            params={"fields": "name,status,category,language,components"},
        )
        assert templates.status_code == 200
        names = {t["name"]: t["status"] for t in templates.json()["data"]}
        assert names["hello_world"] == "APPROVED" and names["year_end_promo"] == "REJECTED"

        text = client.post(
            f"{GRAPH}/{ids['phone_number_id']}/messages",
            headers=SYS,
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": PAULA,
                "type": "text",
                "text": {"body": "Hi Paula, thanks for uploading that."},
            },
        )
        assert text.status_code == 200, text.text
        assert text.json()["contacts"][0]["wa_id"] == PAULA
        assert text.json()["messages"][0]["message_status"] == "accepted"

        blocked = client.post(
            f"{GRAPH}/{ids['phone_number_id']}/messages",
            headers=SYS,
            json={
                "messaging_product": "whatsapp",
                "to": HAMID,
                "type": "text",
                "text": {"body": "ping"},
            },
        )
        assert blocked.status_code == 400 and blocked.json()["error"]["code"] == 131047

        template = client.post(
            f"{GRAPH}/{ids['phone_number_id']}/messages",
            headers=SYS,
            json={
                "messaging_product": "whatsapp",
                "to": HAMID,
                "type": "template",
                "template": {"name": "hello_world", "language": {"code": "en_US"}},
            },
        )
        assert template.status_code == 200, template.text


def test_messenger_conversations_and_send_with_requests() -> None:
    ids = _ids()
    conversations = requests.get(
        f"{GRAPH}/{ids['page_id']}/conversations",
        headers=PAGE,
        params={
            "platform": "messenger",
            "fields": "participants,updated_time,message_count,messages.limit(3){message,from,created_time}",
        },
        timeout=30,
    )
    assert conversations.status_code == 200, conversations.text
    data = conversations.json()["data"]
    assert len(data) == 6
    first = data[0]
    psid = next(p["id"] for p in first["participants"]["data"] if p["id"] != ids["page_id"])
    profile = requests.get(
        f"{GRAPH}/{psid}",
        headers=PAGE,
        params={"fields": "first_name,last_name,profile_pic"},
        timeout=30,
    )
    assert profile.json()["first_name"]
    # The first conversation (most recently updated) is inside the 24h window.
    sent = requests.post(
        f"{GRAPH}/me/messages",
        headers=PAGE,
        json={"recipient": {"id": psid}, "messaging_type": "RESPONSE", "message": {"text": "Sounds good!"}},
        timeout=30,
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["recipient_id"] == psid
    assert sent.json()["message_id"].startswith("m_")


def test_oauth_dialog_exchange_and_debug_token_with_requests() -> None:
    ids = _ids()
    dialog = requests.get(
        "https://www.facebook.com/v21.0/dialog/oauth",
        params={
            "client_id": ids["app_id"],
            "redirect_uri": "https://app.example/callback",
            "state": "state-1",
            "scope": "public_profile,email",
            "login_hint": "natalie.cho@halcyonadvisory.example",
        },
        allow_redirects=False,
        timeout=30,
    )
    assert dialog.status_code == 302
    code = parse_qs(urlsplit(dialog.headers["location"]).query)["code"][0]
    exchange = requests.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "client_id": ids["app_id"],
            "client_secret": ids["app_secret"],
            "redirect_uri": "https://app.example/callback",
            "code": code,
        },
        timeout=30,
    )
    token = exchange.json()["access_token"]
    me = requests.get(
        f"{GRAPH}/me?fields=id,name,email", headers={"Authorization": f"Bearer {token}"}, timeout=30
    )
    assert me.json()["email"] == "natalie.cho@halcyonadvisory.example"
    debug = requests.get(
        f"{GRAPH}/debug_token",
        params={"input_token": token},
        headers={"Authorization": f"Bearer {ids['app_id']}|{ids['app_secret']}"},
        timeout=30,
    )
    assert debug.json()["data"]["is_valid"] is True
    accounts = requests.get(f"{GRAPH}/me/accounts", headers=USER, timeout=30)
    page_token = accounts.json()["data"][0]["access_token"]
    assert (
        requests.get(
            f"{GRAPH}/{ids['page_id']}/conversations",
            headers={"Authorization": f"Bearer {page_token}"},
            timeout=30,
        ).status_code
        == 200
    )


def test_whatsapp_media_upload_and_download_with_httpx() -> None:
    ids = _ids()
    payload = b"not a real pdf but deterministic bytes " * 4
    with httpx.Client(timeout=30) as client:
        upload = client.post(
            f"{GRAPH}/{ids['phone_number_id']}/media",
            headers=SYS,
            files={"file": ("engagement-letter.pdf", payload, "application/pdf")},
            data={"type": "application/pdf", "messaging_product": "whatsapp"},
        )
        assert upload.status_code == 200, upload.text
        meta = client.get(f"{GRAPH}/{upload.json()['id']}", headers=SYS)
        url = meta.json()["url"]
        assert client.get(url).status_code == 401
        assert client.get(url, headers=SYS).content == payload
