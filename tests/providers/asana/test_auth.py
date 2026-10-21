from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from .conftest import BASE, task, workspace

OAUTH = "https://app.asana.com/-"
CONTROL = "https://app.asana.com/_emulator"


@pytest.mark.parametrize("token", ["", "invalid", "Bearer invalid"])
async def test_missing_invalid_auth_is_vendor_shaped(asana_client: httpx.AsyncClient, token: str) -> None:
    response = await asana_client.get(BASE + "/users/me", headers={"Authorization": token})
    assert response.status_code == 401
    assert set(response.json()) == {"errors"} and response.headers["www-authenticate"] == "Bearer"


async def test_control_token_scopes_expiry_and_app_only(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    minted = await client.post(CONTROL + "/tokens", json={"user": "p018", "scopes": ["tasks:read"]})
    assert minted.status_code == 200
    headers = {"Authorization": "Bearer " + minted.json()["access_token"]}
    row = await task(client)
    assert (await client.get(BASE + f"/tasks/{row['gid']}", headers=headers)).status_code == 200
    assert (
        await client.put(BASE + f"/tasks/{row['gid']}", headers=headers, json={"data": {"name": "Denied"}})
    ).status_code == 403
    assert (await client.get(BASE + "/users/me", headers=headers)).status_code == 403
    client.hub.clock.advance(timedelta(seconds=3601))
    assert (await client.get(BASE + f"/tasks/{row['gid']}", headers=headers)).status_code == 401
    assert (await client.get(BASE + f"/tasks/{row['gid']}")).status_code == 200
    assert (await client.post(CONTROL + "/tokens", json={"app_only": True})).status_code == 400
    assert (await client.post(CONTROL + "/tokens", json={"scopes": ["invented:read"]})).status_code == 400


async def authorization_code(client: httpx.AsyncClient, **extra: Any) -> str:
    response = await client.get(
        OAUTH + "/oauth_authorize",
        params={
            "client_id": "contract-client",
            "redirect_uri": "https://consumer.example/callback",
            "response_type": "code",
            "state": "state ✓",
            "login_hint": "maya.chen@halcyonadvisory.example",
            **extra,
        },
    )
    assert response.status_code == 302, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["state ✓"]
    return query["code"][0]


async def test_oauth_pkce_code_refresh_revoke_and_client_binding(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    verifier = "verifier-with-at-least-43-characters-01234567890123"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    code = await authorization_code(
        client, code_challenge=challenge, code_challenge_method="S256", scope="tasks:read users:read"
    )
    form = {
        "grant_type": "authorization_code",
        "client_id": "contract-client",
        "client_secret": "any-local-secret",
        "redirect_uri": "https://consumer.example/callback",
        "code": code,
    }
    bad = await client.post(OAUTH + "/oauth_token", data={**form, "code_verifier": "wrong"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    code = await authorization_code(
        client, code_challenge=challenge, code_challenge_method="S256", scope="tasks:read users:read"
    )
    form["code"] = code
    response = await client.post(OAUTH + "/oauth_token", data={**form, "code_verifier": verifier})
    assert response.status_code == 200, response.text
    pair = response.json()
    assert pair["expires_in"] == 3600 and pair["data"]["email"] == "maya.chen@halcyonadvisory.example"
    assert pair["data"]["id"] == int(pair["data"]["gid"])
    assert (
        await client.post(OAUTH + "/oauth_token", data={**form, "code_verifier": verifier})
    ).status_code == 400
    assert (
        await client.get(BASE + "/users/me", headers={"Authorization": "Bearer " + pair["access_token"]})
    ).status_code == 200
    refresh = {
        "grant_type": "refresh_token",
        "client_id": "contract-client",
        "client_secret": "local-secret",
        "refresh_token": pair["refresh_token"],
    }
    assert (
        await client.post(OAUTH + "/oauth_token", data={**refresh, "client_id": "wrong-client"})
    ).status_code == 400
    newer = (await client.post(OAUTH + "/oauth_token", data=refresh)).json()
    assert newer["refresh_token"] == pair["refresh_token"] and newer["access_token"] != pair["access_token"]
    revoke = {"client_id": "contract-client", "client_secret": "local-secret", "token": pair["refresh_token"]}
    assert (await client.post(OAUTH + "/oauth_revoke", data=revoke)).status_code == 200
    for token in (pair["access_token"], newer["access_token"]):
        assert (
            await client.get(BASE + "/users/me", headers={"Authorization": "Bearer " + token})
        ).status_code == 401
    assert (await client.post(OAUTH + "/oauth_token", data=refresh)).status_code == 400


async def test_oauth_requires_form_and_preserves_code_after_bad_redirect(
    asana_client: httpx.AsyncClient,
) -> None:
    client = asana_client
    assert (await client.post(OAUTH + "/oauth_token", json={"grant_type": "authorization_code"})).json()[
        "error"
    ] == "invalid_request"
    code = await authorization_code(client)
    form = {
        "client_id": "contract-client",
        "client_secret": "secret",
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://wrong.example/callback",
    }
    assert (await client.post(OAUTH + "/oauth_token", data=form)).status_code == 400
    form["redirect_uri"] = "https://consumer.example/callback"
    assert (await client.post(OAUTH + "/oauth_token", data=form)).status_code == 200
    picker = await client.get(
        OAUTH + "/oauth_authorize",
        params={
            "client_id": "contract-client",
            "redirect_uri": form["redirect_uri"],
            "response_type": "code",
        },
    )
    assert picker.status_code == 200 and "maya.chen@halcyonadvisory.example" in picker.text


async def test_scoped_mutations_reject_before_persistence(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    token = (await client.post(CONTROL + "/tokens", json={"scopes": ["projects:read"]})).json()[
        "access_token"
    ]
    count = len(client.hub.context("asana").store["tasks"])
    response = await client.post(
        BASE + "/tasks",
        headers={"Authorization": "Bearer " + token},
        json={"data": {"workspace": await workspace(client), "name": "Denied"}},
    )
    assert response.status_code == 403 and len(client.hub.context("asana").store["tasks"]) == count
