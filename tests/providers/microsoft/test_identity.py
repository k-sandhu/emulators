from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest

from emulators.testing.helpers import bearer, mint

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com"


async def test_discovery_aliases_and_msal_discovery(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        tid = client.hub.world.org.microsoft_tenant_id
        for tenant in (
            tid,
            "common",
            "organizations",
            "halcyonadvisory.example",
            "halcyonadvisory.onmicrosoft.com",
        ):
            for version in ("v2.0/", ""):
                result = await client.get(f"{LOGIN}/{tenant}/{version}.well-known/openid-configuration")
                assert result.status_code == 200, result.text
                discovery = result.json()
                assert (await client.get(discovery["jwks_uri"])).json()["keys"][0]["kty"] == "RSA"
        assert (
            await client.get(f"{LOGIN}/consumers/v2.0/.well-known/openid-configuration")
        ).status_code == 400
        assert (await client.get("https://login.windows.net/common/discovery/v2.0/keys")).status_code == 200
        result = await client.get(
            f"{LOGIN}/common/discovery/instance",
            params={"api-version": "1.1", "authorization_endpoint": f"{LOGIN}/{tid}/oauth2/v2.0/authorize"},
        )
        assert result.json()["metadata"][0]["preferred_network"] == "login.microsoftonline.com"
        result = await client.get(f"{LOGIN}/common/userrealm/maya.chen@halcyonadvisory.example")
        assert result.json()["account_type"] == "Managed"


async def test_authorization_code_pkce_refresh_and_replay(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        verifier = "a" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = {
            "client_id": "test-client",
            "redirect_uri": "https://consumer.example/callback",
            "response_type": "code",
            "scope": "openid profile offline_access User.Read Mail.Read",
            "state": "opaque-state",
            "nonce": "opaque-nonce",
            "login_hint": "maya.chen@halcyonadvisory.example",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        auth = await client.get(f"{LOGIN}/common/oauth2/v2.0/authorize", params=params)
        query = parse_qs(urlsplit(auth.headers["location"]).query)
        assert query["state"] == ["opaque-state"]
        code = query["code"][0]
        data = {
            "client_id": "test-client",
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "code_verifier": "wrong",
        }
        bad = await client.post(f"{LOGIN}/common/oauth2/v2.0/token", data=data)
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
        data["code_verifier"] = verifier
        result = await client.post(f"{LOGIN}/common/oauth2/v2.0/token", data=data)
        assert result.status_code == 200, result.text
        body = result.json()
        claims = jwt.decode(
            body["access_token"],
            client.hub.context("microsoft").signing_key.public_pem,
            algorithms=["RS256"],
            audience=GRAPH,
            options={"verify_iat": False, "verify_nbf": False, "verify_exp": False},
        )
        assert claims["scp"] == "User.Read Mail.Read"
        assert claims["tid"] == client.hub.world.org.microsoft_tenant_id
        assert claims["exp"] - claims["iat"] == 3600
        assert abs(claims["iat"] - client.hub.clock.timestamp()) <= 1
        id_claims = jwt.decode(body["id_token"], options={"verify_signature": False})
        assert id_claims["nonce"] == "opaque-nonce"
        assert (await client.get(f"{GRAPH}/v1.0/me", headers=bearer(body["access_token"]))).json()[
            "mail"
        ] == params["login_hint"]
        assert (await client.post(f"{LOGIN}/common/oauth2/v2.0/token", data=data)).status_code == 400
        refreshed = await client.post(
            f"{LOGIN}/common/oauth2/v2.0/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "test-client",
                "refresh_token": body["refresh_token"],
            },
        )
        assert "access_token" in refreshed.json()
        assert (
            await client.post(
                f"{LOGIN}/common/oauth2/v2.0/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": "another-client",
                    "refresh_token": body["refresh_token"],
                },
            )
        ).status_code == 400


@pytest.mark.parametrize("mode", ["query", "fragment", "form_post"])
async def test_hybrid_modes_picker_and_disabled_user(make_client: Callable[..., Any], mode: str) -> None:
    async with make_client("microsoft") as client:
        params = {
            "client_id": "hybrid",
            "redirect_uri": "https://consumer.example/callback",
            "response_type": "code id_token",
            "response_mode": mode,
            "scope": "openid",
            "nonce": "nonce",
        }
        picker = await client.get(f"{LOGIN}/common/oauth2/v2.0/authorize", params=params)
        assert "Choose an account" in picker.text
        params["login_hint"] = "maya.chen@halcyonadvisory.example"
        result = await client.get(f"{LOGIN}/common/oauth2/v2.0/authorize", params=params)
        if mode == "form_post":
            assert result.status_code == 200 and 'name="id_token"' in result.text
            assert "document.forms[0].submit()" in result.text
        else:
            url = urlsplit(result.headers["location"])
            query = parse_qs(url.fragment if mode == "fragment" else url.query)
            assert "code" in query and "id_token" in query and ("session_state" in query)
        params["login_hint"] = "paul.henderson@halcyonadvisory.example"
        result = await client.get(f"{LOGIN}/common/oauth2/v2.0/authorize", params=params)
        assert "AADSTS50057" in (result.text if mode == "form_post" else result.headers["location"])


async def test_password_app_obo_assertion_and_v1(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        endpoint = f"{LOGIN}/organizations/oauth2/v2.0/token"
        data = {
            "grant_type": "password",
            "client_id": "ropc",
            "username": "maya.chen@halcyonadvisory.example",
            "password": "synthetic",
            "scope": "openid offline_access User.Read",
            "client_info": "1",
        }
        delegated = (await client.post(endpoint, data=data)).json()
        assert "client_info" in delegated
        result = await client.post(
            endpoint,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "client_id": "downstream",
                "requested_token_use": "on_behalf_of",
                "assertion": delegated["access_token"],
                "scope": "Mail.Read",
            },
        )
        assert result.status_code == 200, result.text
        timestamp = client.hub.clock.timestamp()
        assertion = client.hub.context("microsoft").signing_key.sign(
            {"iss": "app", "sub": "app", "exp": timestamp + 600}
        )
        result = await client.post(
            endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": "app",
                "scope": f"{GRAPH}/.default",
                "client_assertion": assertion,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            },
        )
        claims = jwt.decode(result.json()["access_token"], options={"verify_signature": False})
        assert claims["roles"] and "scp" not in claims
        assert (
            await client.get(f"{GRAPH}/v1.0/me", headers=bearer(result.json()["access_token"]))
        ).status_code == 400
        v1 = await client.post(
            f"{LOGIN}/common/oauth2/token",
            data={"grant_type": "client_credentials", "client_id": "v1", "resource": GRAPH},
        )
        assert v1.json()["resource"] == GRAPH
        data["username"] = "paul.henderson@halcyonadvisory.example"
        assert (await client.post(endpoint, data=data)).json()["error_codes"] == [50057]
        assert (await client.get(f"{LOGIN}/common/oauth2/v2.0/logout")).status_code == 200


async def test_device_grant_approval_and_expiry(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        endpoint = f"{LOGIN}/common/oauth2/v2.0"
        device = (
            await client.post(
                endpoint + "/devicecode", data={"client_id": "device", "scope": "openid User.Read"}
            )
        ).json()
        data = {
            "client_id": "device",
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device["device_code"],
        }
        assert (await client.post(endpoint + "/token", data=data)).json()["error"] == "authorization_pending"
        approval = await client.post(
            f"{GRAPH}/_emulator/actions/approve_device_code",
            json={"user_code": device["user_code"], "user": "p018"},
        )
        assert approval.json()["status"] == "approved"
        result = await client.post(endpoint + "/token", data=data)
        assert result.status_code == 200, result.text
        assert (await client.post(endpoint + "/token", data=data)).status_code == 400
        access = await mint(client, GRAPH, user="p018", expires_in=5)
        client.hub.clock.advance(timedelta(seconds=10))
        expired = await client.get(f"{GRAPH}/beta/me", headers=bearer(access))
        assert expired.status_code == 401
        client.hub.clock.advance(timedelta(seconds=-10))


async def test_prefix_discovery_and_jwt_mint(make_hub: Callable[..., Any]) -> None:
    hub = make_hub("microsoft")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hub.gateway), base_url="http://localhost:44080"
    ) as client:
        discovery = (await client.get("/microsoft/common/v2.0/.well-known/openid-configuration")).json()
        assert discovery["token_endpoint"].startswith("http://localhost:44080/microsoft/common/")
        access = await mint(client, "/microsoft", user="p018")
        assert jwt.get_unverified_header(access)["alg"] == "RS256"
        result = await client.get(
            "/microsoft/beta/me", headers={**bearer(access), "client-request-id": "client-correlation"}
        )
        assert result.headers["client-request-id"] == "client-correlation"
        assert result.headers["request-id"]
