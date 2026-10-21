"""OAuth PKCE rotation and webhook payload/signature contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.providers.airtable.common import OWNER_TOKEN

from .conftest import base_id, table_url
from .test_api import mint

AUTH = "https://airtable.com/oauth2/v1/authorize"
TOKEN = "https://airtable.com/oauth2/v1/token"
REVOKE = "https://airtable.com/oauth2/v1/revoke"
CLIENT = ("airtable-emulator", "airtable-emulator-secret")
REDIRECT = "https://consumer.example/callback"


async def authorize(client: httpx.AsyncClient, **changes: Any) -> httpx.Response:
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return await client.get(
        AUTH,
        params={
            "client_id": CLIENT[0],
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": "data.records:read schema.bases:read",
            "state": "state_with_sixteen_chars",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "login_hint": "p001",
            **changes,
        },
    )


async def exchange(client: httpx.AsyncClient, code: str, **changes: str) -> httpx.Response:
    return await client.post(
        TOKEN,
        auth=CLIENT,
        data={
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT,
            "code": code,
            "code_verifier": "v" * 64,
            **changes,
        },
    )


async def test_oauth_pkce_replay_rotation_scopes_and_revocation(client: httpx.AsyncClient) -> None:
    response = await authorize(client)
    assert response.status_code == 302, response.text
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["state"] == ["state_with_sixteen_chars"]
    code = query["code"][0]
    assert (await exchange(client, code, code_verifier="x" * 64)).status_code == 400
    token = await exchange(client, code)
    assert token.status_code == 200, token.text
    assert token.json()["expires_in"] == 3600 and token.headers["cache-control"] == "no-store"
    assert (await exchange(client, code)).status_code == 400
    old = token.json()
    assert (
        await client.get("/v0/meta/bases", headers={"Authorization": "Bearer " + old["access_token"]})
    ).status_code == 200
    widened = await client.post(
        TOKEN,
        auth=CLIENT,
        data={
            "grant_type": "refresh_token",
            "refresh_token": old["refresh_token"],
            "scope": "data.records:write",
        },
    )
    assert widened.status_code == 400
    rotated = await client.post(
        TOKEN, auth=CLIENT, data={"grant_type": "refresh_token", "refresh_token": old["refresh_token"]}
    )
    assert rotated.status_code == 200
    assert (
        await client.get("/v0/meta/bases", headers={"Authorization": "Bearer " + old["access_token"]})
    ).status_code == 401
    assert (
        await client.post(
            TOKEN, auth=CLIENT, data={"grant_type": "refresh_token", "refresh_token": old["refresh_token"]}
        )
    ).status_code == 400
    assert (
        await client.post(REVOKE, auth=CLIENT, data={"token": rotated.json()["refresh_token"]})
    ).status_code == 200
    assert (
        await client.get(
            "/v0/meta/bases", headers={"Authorization": "Bearer " + rotated.json()["access_token"]}
        )
    ).status_code == 401


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "short"},
        {"code_challenge_method": "plain"},
        {"scope": "unknown"},
        {"scope": "data.records:read data.records:read"},
        {"redirect_uri": "http://evil.example/callback"},
        {"login_hint": "unknown"},
        {"code_challenge": "bad"},
    ],
)
async def test_invalid_oauth_authorization(client: httpx.AsyncClient, changes: dict[str, Any]) -> None:
    response = await authorize(client, **changes)
    assert response.status_code == 400 and isinstance(response.json()["error"], str)


async def test_whoami_email_scope_and_public_client_refresh(client: httpx.AsyncClient) -> None:
    identity = (await client.get("/v0/meta/whoami")).json()
    assert identity["id"].startswith("usr") and identity["email"].endswith(".example")
    limited = await mint(client, user="p001", scopes=["data.records:read"])
    assert (
        "email"
        not in (await client.get("/v0/meta/whoami", headers={"Authorization": "Bearer " + limited})).json()
    )
    response = await authorize(client, client_id="public-airtable")
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    issued = await client.post(
        TOKEN,
        data={
            "client_id": "public-airtable",
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT,
            "code": code,
            "code_verifier": "v" * 64,
        },
    )
    assert issued.status_code == 200, issued.text
    assert (
        await client.post(
            TOKEN,
            data={
                "client_id": "public-airtable",
                "grant_type": "refresh_token",
                "refresh_token": issued.json()["refresh_token"],
            },
        )
    ).status_code == 200


async def webhook(client: httpx.AsyncClient, bid: str, **extra: Any) -> dict[str, Any]:
    response = await client.post(
        f"/v0/bases/{bid}/webhooks",
        json={"specification": {"options": {"filters": {"dataTypes": ["tableData"]}}}, **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_webhook_signature_payloads_lifecycle_and_atomic_batches(client: httpx.AsyncClient) -> None:
    receiver = FastAPI()
    received = []

    @receiver.post("/hook")
    async def accept(request: Request) -> Response:
        received.append((await request.body(), dict(request.headers)))
        return Response(status_code=204)

    client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    bid = await base_id(client)
    created_hook = await webhook(client, bid, notificationUrl="https://consumer.example/hook")
    path = f"/v0/bases/{bid}/webhooks/{created_hook['id']}"
    url = await table_url(client)
    created = (await client.post(url, json={"fields": {"Name": "Hook test"}})).json()
    assert len(received) == 1
    raw, headers = received[0]
    expected = (
        "hmac-sha256="
        + hmac.new(base64.b64decode(created_hook["macSecretBase64"]), raw, hashlib.sha256).hexdigest()
    )
    assert headers["x-airtable-content-mac"] == expected
    payloads = (await client.get(path + "/payloads")).json()
    assert payloads["cursor"] == 2 and payloads["mightHaveMore"] is False
    table_change = next(iter(payloads["payloads"][0]["changedTablesById"].values()))
    assert created["id"] in table_change["createdRecordsById"]
    assert payloads["payloads"][0]["actionMetadata"]["source"] == "publicApi"
    failed = await client.patch(
        url,
        json={
            "records": [
                {"id": created["id"], "fields": {"Name": "Rejected"}},
                {"id": "recMissing00000000", "fields": {"Name": "Missing"}},
            ]
        },
    )
    assert failed.status_code == 404 and len(received) == 1
    assert (await client.get(path + "/payloads", params={"cursor": 2})).json()["payloads"] == []
    await client.patch(url + "/" + created["id"], json={"fields": {"Name": "Edited"}})
    await client.delete(url + "/" + created["id"])
    first = (await client.get(path + "/payloads", params={"cursor": 2, "limit": 1})).json()
    assert first["mightHaveMore"] and first["cursor"] == 3
    last = (await client.get(path + "/payloads", params={"cursor": first["cursor"]})).json()
    assert (
        created["id"] in next(iter(last["payloads"][0]["changedTablesById"].values()))["destroyedRecordIds"]
    )
    listing = (await client.get(f"/v0/bases/{bid}/webhooks")).json()["webhooks"][0]
    assert "macSecretBase64" not in listing and "_secret" not in listing
    assert listing["cursorForNextPayload"] == 4
    assert (await client.post(path + "/enableNotifications", json={"enable": False})).json() == {}
    await client.post(
        "/_emulator/actions/replay_webhook_notification", json={"webhook_id": created_hook["id"]}
    )
    assert len(received) == 3
    assert (await client.post(path + "/refresh")).status_code == 200
    assert (await client.delete(path)).json() == {}
    assert (await client.get(path + "/payloads")).status_code == 404


async def test_webhook_filters_previous_values_expiry_and_permissions(client: httpx.AsyncClient) -> None:
    bid = await base_id(client)
    table = next(
        t
        for t in (await client.get(f"/v0/meta/bases/{bid}/tables")).json()["tables"]
        if t["name"] == "Clients"
    )
    name = next(f["id"] for f in table["fields"] if f["name"] == "Name")
    created_hook = await webhook(
        client,
        bid,
        specification={
            "options": {
                "filters": {
                    "dataTypes": ["tableData"],
                    "recordChangeScope": table["id"],
                    "watchDataInFieldIds": [name],
                    "changeTypes": ["update"],
                },
                "includes": {"includePreviousCellValues": True},
            }
        },
    )
    path = f"/v0/bases/{bid}/webhooks/{created_hook['id']}"
    url = await table_url(client)
    created = (await client.post(url, json={"fields": {"Name": "Before"}})).json()
    await client.patch(url + "/" + created["id"], json={"fields": {"Notes": "No matching change"}})
    assert (await client.get(path + "/payloads")).json()["cursor"] == 1
    await client.patch(url + "/" + created["id"], json={"fields": {"Name": "After"}})
    payload = (await client.get(path + "/payloads")).json()["payloads"][0]
    change = payload["changedTablesById"][table["id"]]["changedRecordsById"][created["id"]]
    assert change["previous"]["cellValuesByFieldId"][name] == "Before"
    assert change["current"]["cellValuesByFieldId"][name] == "After"
    client.hub.clock.advance(timedelta(days=7, seconds=1))
    assert (await client.post(path + "/refresh")).status_code == 422
    assert (await client.get(path + "/payloads")).json()["payloads"] == []
    token = await mint(client, user="p008")
    assert (
        await client.post(
            f"/v0/bases/{bid}/webhooks",
            headers={"Authorization": "Bearer " + token},
            json={"specification": {"options": {"filters": {"dataTypes": ["tableData"]}}}},
        )
    ).status_code == 403
    client.headers["Authorization"] = "Bearer " + OWNER_TOKEN


async def test_webhook_attachment_values_and_base_transaction_numbers(client: httpx.AsyncClient) -> None:
    bid = await base_id(client)
    url = await table_url(client, "Documents")
    created = (await client.post(url, json={"fields": {"Name": "Before hook"}})).json()
    first = await webhook(client, bid)
    uploaded = await client.post(
        f"https://content.airtable.com/v0/{bid}/{created['id']}/"
        + next(
            f["id"]
            for table in (await client.get(f"/v0/meta/bases/{bid}/tables")).json()["tables"]
            if table["name"] == "Documents"
            for f in table["fields"]
            if f["name"] == "Files"
        )
        + "/uploadAttachment",
        json={
            "file": base64.b64encode(b"Webhook bytes").decode(),
            "filename": "hook.txt",
            "contentType": "text/plain",
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    second = await webhook(client, bid)
    await client.patch(url + "/" + created["id"], json={"fields": {"Name": "After upload"}})
    first_payloads = (await client.get(f"/v0/bases/{bid}/webhooks/{first['id']}/payloads")).json()["payloads"]
    second_payloads = (await client.get(f"/v0/bases/{bid}/webhooks/{second['id']}/payloads")).json()[
        "payloads"
    ]
    assert [p["baseTransactionNumber"] for p in first_payloads] == [2, 3]
    assert [p["baseTransactionNumber"] for p in second_payloads] == [3]
    cells = next(iter(first_payloads[0]["changedTablesById"].values()))["changedRecordsById"][created["id"]][
        "current"
    ]["cellValuesByFieldId"]
    attachment = next(value[0] for value in cells.values() if isinstance(value, list))
    assert attachment["filename"] == "hook.txt" and attachment["type"] == "text/plain"
    assert attachment["size"] == len(b"Webhook bytes")
    assert (await client.get(attachment["url"])).content == b"Webhook bytes"
    changed = next(iter(first_payloads[1]["changedTablesById"].values()))["changedRecordsById"][
        created["id"]
    ]["current"]["cellValuesByFieldId"]
    assert all(not isinstance(value, list) for value in changed.values())


async def test_webhook_personal_token_visibility_and_oauth_integration_quota(
    client: httpx.AsyncClient,
) -> None:
    bid = await base_id(client)
    pat_hook = await webhook(client, bid)
    reader = await mint(client, user="p005")
    client.headers["Authorization"] = "Bearer " + reader
    listed = (await client.get(f"/v0/bases/{bid}/webhooks")).json()["webhooks"]
    assert [hook["id"] for hook in listed] == [pat_hook["id"]]
    assert (await client.delete(f"/v0/bases/{bid}/webhooks/{pat_hook['id']}")).status_code == 403

    async def access(user: str) -> str:
        response = await authorize(client, login_hint=user, scope="data.records:read webhook:manage")
        code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
        issued = await client.post(
            TOKEN,
            auth=CLIENT,
            data={
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT,
                "code": code,
                "code_verifier": "v" * 64,
            },
        )
        assert issued.status_code == 200, issued.text
        return issued.json()["access_token"]

    client.headers["Authorization"] = "Bearer " + await access("p001")
    first = await webhook(client, bid)
    client.headers["Authorization"] = "Bearer " + await access("p018")
    second = await webhook(client, bid)
    listed = (await client.get(f"/v0/bases/{bid}/webhooks")).json()["webhooks"]
    assert {hook["id"] for hook in listed} == {first["id"], second["id"]}
    rejected = await client.post(
        f"/v0/bases/{bid}/webhooks",
        json={"specification": {"options": {"filters": {"dataTypes": ["tableData"]}}}},
    )
    assert rejected.status_code == 422 and rejected.json()["error"]["type"] == "TOO_MANY_WEBHOOKS"
    client.headers["Authorization"] = "Bearer " + OWNER_TOKEN
    assert len((await client.get(f"/v0/bases/{bid}/webhooks")).json()["webhooks"]) == 3


@pytest.mark.parametrize(
    "specification",
    [
        None,
        [],
        {"options": []},
        {"options": {"filters": {"dataTypes": ["tableMetadata"]}}},
        {"options": {"filters": {"dataTypes": ["tableData"], "changeTypes": ["invalid"]}}},
        {"options": {"filters": {"dataTypes": ["tableData"]}, "includes": {"includePreviousCellValues": 1}}},
    ],
)
async def test_invalid_hook_specifications(client: httpx.AsyncClient, specification: Any) -> None:
    bid = await base_id(client)
    assert (
        await client.post(f"/v0/bases/{bid}/webhooks", json={"specification": specification})
    ).status_code == 422
