from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest

from emulators.testing.helpers import bearer, mint


async def oauth(
    client: httpx.AsyncClient, scopes: str = "oauth crm.objects.contacts.read", user: str = "p018"
) -> dict:
    redirect = "https://consumer.example/callback?return=crm"
    authorized = await client.get(
        "https://app.hubspot.com/oauth/authorize",
        params={
            "client_id": "connector",
            "redirect_uri": redirect,
            "scope": scopes,
            "state": "state & value",
            "login_hint": user,
        },
    )
    assert authorized.status_code == 302
    query = parse_qs(urlparse(authorized.headers["location"]).query)
    assert query["state"] == ["state & value"]
    response = await client.post(
        "/oauth/v1/token",
        data={
            "grant_type": "authorization_code",
            "client_id": "connector",
            "client_secret": "fictional-secret",
            "redirect_uri": redirect,
            "code": query["code"][0],
        },
    )
    assert response.status_code == 200, response.text
    replay = await client.post(
        "/oauth/v1/token",
        data={
            "grant_type": "authorization_code",
            "client_id": "connector",
            "client_secret": "fictional-secret",
            "redirect_uri": redirect,
            "code": query["code"][0],
        },
    )
    assert replay.status_code == 400
    return response.json()


async def test_oauth_picker_exchange_metadata_refresh_and_revoke(client: httpx.AsyncClient) -> None:
    picker = await client.get(
        "https://app.hubspot.com/oauth/authorize",
        params={
            "client_id": "connector",
            "redirect_uri": "https://consumer.example/callback",
            "scope": "oauth",
        },
    )
    assert picker.status_code == 200
    assert "maya.chen@halcyonadvisory.example" in picker.text
    tokens = await oauth(client)
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 1800
    assert tokens["access_token"].startswith("CJ") and len(tokens["access_token"]) >= 190
    UUID(tokens["refresh_token"])
    metadata = (await client.get("/oauth/v1/access-tokens/" + tokens["access_token"])).json()
    assert metadata["hub_id"] == 24681357
    assert metadata["user"] == "maya.chen@halcyonadvisory.example"
    assert metadata["token_type"] == "access"
    assert metadata["scopes"] == ["oauth", "crm.objects.contacts.read"]
    assert 1790 <= metadata["expires_in"] <= 1800
    refresh_metadata = (await client.get("/oauth/v1/refresh-tokens/" + tokens["refresh_token"])).json()
    assert refresh_metadata["token_type"] == "refresh"
    wrong_client = await client.post(
        "/oauth/v1/token",
        data={
            "grant_type": "refresh_token",
            "client_id": "other",
            "client_secret": "secret",
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert wrong_client.status_code == 400
    refreshed = await client.post(
        "/oauth/v1/token",
        data={
            "grant_type": "refresh_token",
            "client_id": "connector",
            "client_secret": "secret",
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != tokens["access_token"]
    assert refreshed.json()["refresh_token"] == tokens["refresh_token"]
    assert (await client.delete("/oauth/v1/refresh-tokens/" + tokens["refresh_token"])).status_code == 204
    assert (
        await client.get("/crm/v3/objects/contacts", headers=bearer(tokens["access_token"]))
    ).status_code == 200
    revoked = await client.post(
        "/oauth/v1/token",
        data={
            "grant_type": "refresh_token",
            "client_id": "connector",
            "client_secret": "secret",
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert revoked.status_code == 400 and revoked.json()["status"] == "BAD_REFRESH_TOKEN"


async def test_scope_errors_and_expiry(client: httpx.AsyncClient) -> None:
    tokens = await oauth(client)
    denied = await client.get("/crm/v3/objects/deals", headers=bearer(tokens["access_token"]))
    assert denied.status_code == 403
    assert denied.json()["category"] == "MISSING_SCOPES"
    assert denied.json()["context"]["requiredGranularScopes"] == ["crm.objects.deals.read"]
    client.hub.clock.advance(timedelta(seconds=1801))
    expired = await client.get("/crm/v3/objects/contacts", headers=bearer(tokens["access_token"]))
    assert expired.status_code == 401 and expired.json()["category"] == "INVALID_AUTHENTICATION"
    client.hub.clock.advance(timedelta(seconds=-1801))
    assert (await client.get("/crm/v3/objects/deals")).status_code == 200


@pytest.mark.parametrize(
    "token", ["", "bad", "pat-na1-" + "-".join(["0" * 8, "0" * 4, "0" * 4, "0" * 4, "0" * 12])]
)
async def test_invalid_tokens(client: httpx.AsyncClient, token: str) -> None:
    result = await client.get("/crm/v3/objects/contacts", headers=bearer(token))
    assert result.status_code == 401
    assert result.json()["category"] == "INVALID_AUTHENTICATION"
    UUID(result.json()["correlationId"])


async def test_code_binding_and_content_type(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/oauth/authorize",
        params={
            "client_id": "bound",
            "redirect_uri": "https://consumer.example/cb",
            "scope": "oauth",
            "login_hint": "p018",
        },
    )
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    for client_id, redirect in [
        ("other", "https://consumer.example/cb"),
        ("bound", "https://evil.example/cb"),
        ("bound", ""),
    ]:
        rejected = await client.post(
            "/oauth/v1/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": "secret",
                "code": code,
                "redirect_uri": redirect,
            },
        )
        assert rejected.status_code == 400
    result = await client.post("/oauth/v1/token", json={"grant_type": "authorization_code", "code": code})
    assert result.status_code == 400
    missing = await client.get("/oauth/authorize", params={"client_id": "bound"})
    assert missing.status_code == 400


async def test_mint_defaults_explicit_empty_scopes_and_inactive_user(client: httpx.AsyncClient) -> None:
    token = await mint(client, "https://api.hubapi.com", user="maya.chen@halcyonadvisory.example")
    assert (await client.get("/crm/v3/objects/deals", headers=bearer(token))).status_code == 200
    empty = await mint(client, "https://api.hubapi.com", scopes=[])
    assert (await client.get("/crm/v3/objects/contacts", headers=bearer(empty))).status_code == 403
    denied = await client.post("/_emulator/tokens", json={"user": "c001-1"})
    assert denied.status_code == 400


async def test_oauth_credentials_are_redacted_in_request_journal(client: httpx.AsyncClient) -> None:
    tokens = await oauth(client)
    await client.get("/oauth/v1/access-tokens/" + tokens["access_token"])
    await client.get("/oauth/v1/refresh-tokens/" + tokens["refresh_token"])
    entries = (await client.get("/_emulator/requests")).json()["requests"]
    journal = json.dumps(entries)
    assert tokens["access_token"] not in journal
    assert tokens["refresh_token"] not in journal
    assert "fictional-secret" not in journal
    assert any(e["path"] == "/oauth/v1/access-tokens/[redacted]" for e in entries)


async def test_owned_only_user_cannot_read_modify_search_or_batch_others(client: httpx.AsyncClient) -> None:
    owner = (
        await client.get("/crm/v3/owners", params={"email": "zoe.anderson@halcyonadvisory.example"})
    ).json()["results"][0]
    owned = (
        await client.post(
            "/crm/v3/objects/deals",
            json={
                "properties": {
                    "dealname": "Owned opportunity",
                    "pipeline": "default",
                    "dealstage": "appointmentscheduled",
                    "hubspot_owner_id": owner["id"],
                }
            },
        )
    ).json()
    other = (await client.get("/crm/v3/objects/deals", params={"limit": 100})).json()["results"][0]
    tokens = await oauth(client, "crm.objects.deals.read crm.objects.deals.write", "p042")
    auth = bearer(tokens["access_token"])
    visible = (await client.get("/crm/v3/objects/deals", headers=auth)).json()["results"]
    assert [r["id"] for r in visible] == [owned["id"]]
    assert (await client.get("/crm/v3/objects/deals/" + owned["id"], headers=auth)).status_code == 200
    for method, data in [("GET", None), ("PATCH", {"properties": {"amount": "1"}}), ("DELETE", None)]:
        result = await client.request(method, "/crm/v3/objects/deals/" + other["id"], headers=auth, json=data)
        assert result.status_code == 404
    search = (await client.post("/crm/v3/objects/deals/search", headers=auth, json={})).json()
    assert search["total"] == 1 and search["results"][0]["id"] == owned["id"]
    batch = await client.post(
        "/crm/v3/objects/deals/batch/read",
        headers=auth,
        json={"inputs": [{"id": other["id"]}, {"id": owned["id"]}]},
    )
    assert batch.status_code == 207 and len(batch.json()["results"]) == 1
    reassigned = await client.patch(
        "/crm/v3/objects/deals/" + owned["id"], headers=auth, json={"properties": {"hubspot_owner_id": ""}}
    )
    assert reassigned.status_code == 403
    created = await client.post(
        "/crm/v3/objects/deals",
        headers=auth,
        json={"properties": {"dealname": "Rep's own", "pipeline": "default", "dealstage": "qualifiedtobuy"}},
    )
    assert created.status_code == 201 and created.json()["properties"]["hubspot_owner_id"] == owner["id"]
