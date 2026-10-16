"""OAuth/OpenID flows, token minting, realm binding and permissions."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlparse

from tests.providers.quickbooks.conftest import API, REALM, mint_user

FINANCE = "victoria.hale@halcyonadvisory.example"  # CFO, full access
AUDITOR = "sofia.alvarez@halcyonadvisory.example"  # read-only delegated user
DEPARTED = "paul.henderson@halcyonadvisory.example"  # inactive employee


def basic(client_id: str, secret: str) -> dict[str, str]:
    raw = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {raw}", "Content-Type": "application/x-www-form-urlencoded"}


async def test_discovery_documents(qb_client) -> None:
    for host in ("https://developer.intuit.com", "https://developer.api.intuit.com"):
        r = await qb_client.get(f"{host}/.well-known/openid_sandbox_configuration/")
        assert r.status_code == 200, r.text
        doc = r.json()
        assert doc["authorization_endpoint"] == "https://appcenter.intuit.com/connect/oauth2"
        assert doc["token_endpoint"].endswith("/oauth2/v1/tokens/bearer")
        assert doc["jwks_uri"].endswith("/op/v1/jwks")
        assert "sandbox-accounts.platform.intuit.com" in doc["userinfo_endpoint"]
    r = await qb_client.get("https://developer.intuit.com/.well-known/openid_configuration/")
    assert "accounts.platform.intuit.com" in r.json()["userinfo_endpoint"]


async def test_authorize_code_exchange_refresh_and_userinfo(qb_client) -> None:
    r = await qb_client.get(
        "https://appcenter.intuit.com/connect/oauth2",
        params={
            "client_id": "AB0Emu1atorQuickBooksC1ientId000001",
            "response_type": "code",
            "scope": "com.intuit.quickbooks.accounting openid profile email phone address",
            "redirect_uri": "https://localhost/callback",
            "state": "csrf-123",
            "login_hint": FINANCE,
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["state"] == ["csrf-123"]
    assert query["realmId"] == [REALM]
    code = query["code"][0]

    r = await qb_client.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers=basic("AB0Emu1atorQuickBooksC1ientId000001", "emu1ator-quickbooks-secret"),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://localhost/callback",
        },
    )
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 3600
    assert tokens["x_refresh_token_expires_in"] == 8726400
    assert tokens["access_token"].startswith("eyJlbmMiOiJBMTI4Q0JDLUhTMjU2IiwiYWxnIjoiZGlyIn0..")
    assert tokens["refresh_token"].startswith("AB11")
    assert tokens["id_token"].count(".") == 2

    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = await qb_client.get(
        "https://sandbox-accounts.platform.intuit.com/v1/openid_connect/userinfo", headers=headers
    )
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["email"] == FINANCE
    assert info["givenName"] == "Victoria"
    assert info["emailVerified"] is True

    # JWKS validates the id_token signature
    r = await qb_client.get("https://oauth.platform.intuit.com/op/v1/jwks")
    assert r.status_code == 200
    assert r.json()["keys"][0]["kty"] == "RSA"

    # code is single-use
    r = await qb_client.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers=basic("AB0Emu1atorQuickBooksC1ientId000001", "emu1ator-quickbooks-secret"),
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": "https://localhost/callback"},
    )
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_grant"}

    # refresh rotates; the old refresh token still works until the new one is used
    old_refresh = tokens["refresh_token"]
    r = await qb_client.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers=basic("cid", "secret"),
        data={"grant_type": "refresh_token", "refresh_token": old_refresh},
    )
    assert r.status_code == 200, r.text
    new_refresh = r.json()["refresh_token"]
    assert new_refresh != old_refresh
    r = await qb_client.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers=basic("cid", "secret"),
        data={"grant_type": "refresh_token", "refresh_token": old_refresh},
    )
    assert r.status_code == 200, r.text  # grace period: previous token accepted
    r = await qb_client.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers=basic("cid", "secret"),
        data={"grant_type": "refresh_token", "refresh_token": new_refresh},
    )
    assert r.status_code == 200, r.text
    r = await qb_client.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers=basic("cid", "secret"),
        data={"grant_type": "refresh_token", "refresh_token": old_refresh},
    )
    assert r.status_code == 400  # successor used -> predecessor dead
    assert r.json() == {"error": "invalid_grant"}


async def test_authorize_without_hint_renders_picker(qb_client) -> None:
    r = await qb_client.get(
        "https://appcenter.intuit.com/connect/oauth2",
        params={
            "client_id": "cid",
            "response_type": "code",
            "scope": "com.intuit.quickbooks.accounting",
            "redirect_uri": "https://localhost/callback",
            "state": "s",
        },
    )
    assert r.status_code == 200
    assert FINANCE in r.text


async def test_revoke(qb_client) -> None:
    headers = await mint_user(qb_client, FINANCE)
    token = headers["Authorization"].split(" ", 1)[1]
    r = await qb_client.get(f"{API}/customer/1", headers=headers)
    assert r.status_code == 200
    r = await qb_client.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/revoke",
        headers={**basic("cid", "secret"), "Content-Type": "application/json"},
        content=json.dumps({"token": token}),
    )
    assert r.status_code == 200
    r = await qb_client.get(f"{API}/customer/1", headers=headers)
    assert r.status_code == 401


async def test_missing_and_bad_tokens(qb) -> None:
    client, _headers = qb
    r = await client.get(f"{API}/customer/1")
    assert r.status_code == 401
    body = r.json()
    assert body["fault"]["type"] == "AUTHENTICATION"
    assert body["fault"]["error"][0]["code"] == "3200"
    assert "Bearer" in r.headers["www-authenticate"]
    r = await client.get(f"{API}/customer/1", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


async def test_wrong_realm_rejected(qb) -> None:
    client, headers = qb
    r = await client.get(
        "https://sandbox-quickbooks.api.intuit.com/v3/company/123456/customer/1", headers=headers
    )
    assert r.status_code in (401, 403)


async def test_delegated_write_permissions(qb_client) -> None:
    finance = await mint_user(qb_client, FINANCE)
    r = await qb_client.post(f"{API}/customer", headers=finance, json={"DisplayName": "Finance Made"})
    assert r.status_code == 200, r.text

    auditor = await mint_user(qb_client, AUDITOR)
    r = await qb_client.get(f"{API}/customer/1", headers=auditor)
    assert r.status_code == 200  # read-only users can read
    r = await qb_client.post(f"{API}/customer", headers=auditor, json={"DisplayName": "Auditor Made"})
    assert r.status_code == 403
    assert r.json()["Fault"]["Error"][0]["code"] == "110"


async def test_departed_employee_token_rejected(qb_client) -> None:
    departed = await mint_user(qb_client, DEPARTED)
    r = await qb_client.get(f"{API}/customer/1", headers=departed)
    assert r.status_code == 401


async def test_prefix_mode_routing(qb_client) -> None:
    hub = qb_client.hub
    import httpx

    transport = httpx.ASGITransport(app=hub.gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080") as client:
        r = await client.post("/quickbooks/_emulator/tokens", json={"app_only": True})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        r = await client.get(f"/quickbooks/v3/company/{REALM}/companyinfo/{REALM}", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["CompanyInfo"]["CompanyName"] == "Halcyon Advisory Group"
