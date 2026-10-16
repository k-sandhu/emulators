from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt

from emulators.testing.helpers import mint

from .conftest import API, BASE

IDENTITY = "https://identity.xero.com"
LOGIN = "https://login.xero.com/identity/connect/authorize"
SCOPES = "openid profile email offline_access accounting.transactions accounting.contacts accounting.settings accounting.reports.read"


async def authorize(client: httpx.AsyncClient, **changes: Any) -> str:
    response = await client.get(
        LOGIN,
        params={
            "response_type": "code",
            "client_id": "xero-test-client",
            "redirect_uri": "https://consumer.example/callback",
            "scope": SCOPES,
            "state": "original-state",
            "login_hint": "eleanor.whitfield@halcyonadvisory.example",
            **changes,
        },
    )
    assert response.status_code == 302, response.text
    params = parse_qs(urlparse(response.headers["location"]).query)
    assert params["state"] == ["original-state"]
    return params["code"][0]


async def exchange(client: httpx.AsyncClient, code: str, **extra: Any) -> httpx.Response:
    return await client.post(
        IDENTITY + "/connect/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://consumer.example/callback",
            **extra,
        },
        auth=("xero-test-client", "xero-test-secret"),
    )


async def test_code_flow_jwks_claims_and_single_use(client: httpx.AsyncClient) -> None:
    code = await authorize(client, nonce="nonce-123")
    response = await exchange(client, code)
    assert response.status_code == 200, response.text
    token = response.json()
    assert token["expires_in"] == 1800
    assert token["token_type"] == "Bearer"
    assert token["scope"] == SCOPES
    assert token["refresh_token"]
    jwks = (await client.get(IDENTITY + "/.well-known/openid-configuration/jwks")).json()
    key = jwt.PyJWK.from_dict(jwks["keys"][0])
    claims = jwt.decode(
        token["access_token"],
        key.key,
        algorithms=["RS256"],
        audience="https://identity.xero.com/resources",
        options={"verify_exp": False, "verify_nbf": False, "verify_iat": False},
    )
    assert claims["xero_userid"]
    assert claims["authentication_event_id"]
    assert claims["scope"] == SCOPES.split()
    identity = jwt.decode(
        token["id_token"],
        key.key,
        algorithms=["RS256"],
        audience="xero-test-client",
        options={"verify_exp": False, "verify_nbf": False, "verify_iat": False},
    )
    assert identity["email"] == "eleanor.whitfield@halcyonadvisory.example"
    assert identity["nonce"] == "nonce-123"
    assert (await exchange(client, code)).json()["error"] == "invalid_grant"
    connections = (
        await client.get(API + "/connections", headers={"Authorization": "Bearer " + token["access_token"]})
    ).json()
    assert len(connections) == 2
    assert connections[0]["authEventId"] == claims["authentication_event_id"]
    assert response.headers["cache-control"] == "no-store"
    journal = (await client.get("/_emulator/requests", params={"path": "/connect/token"})).json()["requests"]
    assert all(code not in str(e["body"]) for e in journal)
    assert all("xero-test-secret" not in str(e["body"]) for e in journal)
    next_tokens = (await exchange(client, await authorize(client))).json()
    next_claims = jwt.decode(next_tokens["access_token"], options={"verify_signature": False})
    recent = await client.get(
        "/connections",
        params={"authEventId": next_claims["authentication_event_id"]},
        headers={"Authorization": "Bearer " + next_tokens["access_token"]},
    )
    assert len(recent.json()) == 2


async def test_picker_pkce_redirect_and_client_binding(client: httpx.AsyncClient) -> None:
    picker = await client.get(
        LOGIN,
        params={
            "response_type": "code",
            "client_id": "picker",
            "redirect_uri": "https://consumer.example/callback",
            "scope": SCOPES,
        },
    )
    assert "Choose an account" in picker.text
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = await authorize(client, code_challenge=challenge, code_challenge_method="S256")
    assert (await exchange(client, code, code_verifier="wrong")).status_code == 400
    assert (
        await exchange(client, code, code_verifier=verifier, redirect_uri="https://wrong.example/")
    ).status_code == 400
    foreign = await client.post(
        IDENTITY + "/connect/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://consumer.example/callback",
            "code_verifier": verifier,
        },
        auth=("foreign", "secret"),
    )
    assert foreign.status_code == 400
    assert (await exchange(client, code, code_verifier=verifier)).status_code == 200
    bad = await client.get(
        LOGIN,
        params={"response_type": "token", "client_id": "a", "redirect_uri": "https://consumer.example/"},
    )
    assert bad.status_code == 400


async def test_rotating_refresh_grace_expiry_and_revocation(client: httpx.AsyncClient) -> None:
    tokens = (await exchange(client, await authorize(client))).json()

    async def refresh(value: str) -> httpx.Response:
        return await client.post(
            IDENTITY + "/connect/token",
            auth=("xero-test-client", "xero-test-secret"),
            data={"grant_type": "refresh_token", "refresh_token": value},
        )

    rotated = await refresh(tokens["refresh_token"])
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]
    assert (await refresh(tokens["refresh_token"])).status_code == 200
    client.hub.clock.advance(timedelta(seconds=1801))
    expired = await refresh(tokens["refresh_token"])
    assert expired.status_code == 400
    assert expired.json()["error"] == "invalid_grant"
    access = await client.get("/connections", headers={"Authorization": "Bearer " + tokens["access_token"]})
    assert access.status_code == 401
    fresh = (await refresh(rotated.json()["refresh_token"])).json()
    revoked = await client.post(
        IDENTITY + "/connect/revocation",
        auth=("xero-test-client", "xero-test-secret"),
        data={"token": fresh["refresh_token"]},
    )
    assert revoked.status_code == 200
    assert (await refresh(fresh["refresh_token"])).status_code == 400
    assert (
        await client.get("/connections", headers={"Authorization": "Bearer " + fresh["access_token"]})
    ).status_code == 401


async def test_custom_connection_and_connection_ownership(client: httpx.AsyncClient) -> None:
    result = await client.post(
        IDENTITY + "/connect/token",
        auth=("custom", "test-secret"),
        data={"grant_type": "client_credentials", "scope": "accounting.transactions accounting.settings"},
    )
    assert result.status_code == 200
    token = result.json()
    assert "refresh_token" not in token and "id_token" not in token
    headers = {"Authorization": "Bearer " + token["access_token"]}
    connections = (await client.get("/connections", headers=headers)).json()
    assert len(connections) == 1
    assert connections[0]["tenantName"] == "Halcyon Advisory Group"
    all_connections = (await client.get("/connections")).json()
    foreign = all_connections[0]["id"]
    assert (await client.delete("/connections/" + foreign, headers=headers)).status_code == 404
    cid = connections[0]["id"]
    assert (await client.delete("/connections/" + cid, headers=headers)).status_code == 204
    assert (await client.get(BASE + "/Invoices", headers=headers)).status_code == 403
    assert (await client.get("/connections", headers=headers)).json() == []


async def test_permissions_tenants_missing_and_expired_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get(BASE + "/Contacts", headers={"Authorization": "Bearer bad"})).status_code == 401
    response = await client.get(BASE + "/Contacts", headers={"xero-tenant-id": ""})
    assert response.status_code == 403
    assert response.json()["Title"] == "Forbidden"
    assert response.json()["Detail"] == "AuthenticationUnsuccessful"
    assert (
        await client.get(BASE + "/Contacts", headers={"xero-tenant-id": "not-a-tenant"})
    ).status_code == 403
    readonly = await mint(client, API, user="p005")
    headers = {"Authorization": "Bearer " + readonly}
    assert (await client.get(BASE + "/Contacts", headers=headers)).status_code == 200
    assert (await client.put(BASE + "/Contacts", headers=headers, json={"Name": "Denied"})).status_code == 403
    scoped = await mint(client, API, user="p001", scopes=["accounting.contacts.read"])
    auth = {"Authorization": "Bearer " + scoped}
    assert (await client.get(BASE + "/Contacts", headers=auth)).status_code == 200
    assert (await client.get(BASE + "/Invoices", headers=auth)).status_code == 401
    assert (await client.put(BASE + "/Contacts", headers=auth, json={"Name": "Denied"})).status_code == 401
    external = client.hub.world.external_people[0].email
    assert (await client.post("/_emulator/tokens", json={"user": external})).status_code == 403
    departed = next(p for p in client.hub.world.employees if not p.active)
    assert (await client.post("/_emulator/tokens", json={"user": departed.id})).status_code == 403
    tenants = (await client.get("/connections")).json()
    demo = tenants[1]["tenantId"]
    assert (await client.get(BASE + "/Contacts", headers={"xero-tenant-id": demo})).json()["Contacts"] == []
    invoice = (await client.get(BASE + "/Invoices", params={"page": 1})).json()["Invoices"][0]
    assert (
        await client.get(BASE + "/Invoices/" + invoice["InvoiceID"], headers={"xero-tenant-id": demo})
    ).status_code == 404


async def test_discovery_prefix_and_explicit_app_scope(client: httpx.AsyncClient) -> None:
    discovery = (await client.get(IDENTITY + "/.well-known/openid-configuration")).json()
    assert discovery["authorization_endpoint"] == LOGIN
    assert discovery["jwks_uri"].startswith(IDENTITY)
    transport = httpx.ASGITransport(app=client.hub.gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080/xero") as prefix:
        config = (await prefix.get("./.well-known/openid-configuration")).json()
        assert config["token_endpoint"] == "http://localhost:44080/xero/connect/token"
    app = await mint(client, API, app_only=True, client_id="app-read", scopes=["accounting.settings.read"])
    assert (
        await client.get(BASE + "/Accounts", headers={"Authorization": "Bearer " + app})
    ).status_code == 200
