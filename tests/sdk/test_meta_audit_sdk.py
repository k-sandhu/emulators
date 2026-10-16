"""Consumer HTTP contracts through the live interception fixture."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import requests

pytestmark = pytest.mark.sdk
BASE = "https://graph.facebook.com"
GRAPH = BASE + "/v21.0"


@pytest.fixture
def credentials(emulator_intercept: object) -> dict[str, Any]:
    return requests.get(BASE + "/_emulator/info", timeout=30).json()["credentials"]


def test_form_encoded_whatsapp_unicode_and_validation(credentials: dict[str, Any]) -> None:
    headers = {"Authorization": "Bearer " + credentials["system_user_token"]}
    url = f"{GRAPH}/{credentials['phone_number_id']}/messages"
    r = requests.post(
        url,
        headers=headers,
        data={
            "messaging_product": "whatsapp",
            "to": "13055550174",
            "text": json.dumps({"body": "雪 — received 👋"}),
        },
        timeout=30,
    )
    assert r.status_code == 200, r.text
    assert r.headers["facebook-api-version"] == "v21.0"
    invalid = requests.post(
        url,
        headers=headers,
        json={"messaging_product": "whatsapp", "to": "13055550174", "type": []},
        timeout=30,
    )
    assert invalid.status_code == 400 and invalid.json()["error"]["code"] == 131051


def test_restricted_system_token_cannot_read_business_assets(credentials: dict[str, Any]) -> None:
    token = requests.post(
        BASE + "/_emulator/tokens", json={"type": "system_user", "scopes": []}, timeout=30
    ).json()["access_token"]
    r = requests.get(
        f"{GRAPH}/{credentials['waba_id']}", headers={"Authorization": "Bearer " + token}, timeout=30
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == 200


def test_nested_message_paging_through_live_http(credentials: dict[str, Any]) -> None:
    with httpx.Client(
        timeout=30, headers={"Authorization": "Bearer " + credentials["page_access_token"]}
    ) as client:
        r = client.get(
            f"{GRAPH}/me/conversations", params={"fields": "messages.limit(1){message,created_time}"}
        )
        assert r.status_code == 200, r.text
        first = r.json()["data"][0]["messages"]
        second = client.get(first["paging"]["next"])
        assert second.status_code == 200
        assert first["data"][0]["id"] != second.json()["data"][0]["id"]
        assert "message" in second.json()["data"][0]


def test_messenger_profile_form_delete_and_missing_attachment_url(credentials: dict[str, Any]) -> None:
    headers = {"Authorization": "Bearer " + credentials["page_access_token"]}
    r = requests.delete(
        GRAPH + "/me/messenger_profile",
        headers=headers,
        data={"fields": json.dumps(["greeting"])},
        timeout=30,
    )
    assert r.json() == {"result": "success"}
    r = requests.post(
        GRAPH + "/me/message_attachments",
        headers=headers,
        json={"message": {"attachment": {"type": "image", "payload": {"is_reusable": True}}}},
        timeout=30,
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == 100
