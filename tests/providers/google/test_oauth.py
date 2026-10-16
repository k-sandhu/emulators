from __future__ import annotations

import base64
import hashlib
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

from emulators.testing.helpers import bearer, mint

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"


async def authorize(client: Any, **kwargs: Any) -> Any:
    params = {
        "client_id": "google-test-client",
        "redirect_uri": "https://consumer.example/callback",
        "response_type": "code",
        "scope": "openid email profile https://www.googleapis.com/auth/drive",
        "login_hint": "maya.chen@halcyonadvisory.example",
        **kwargs,
    }
    response = await client.get(AUTH, params=params)
    assert response.status_code == 302, response.text
    return parse_qs(urlparse(response.headers["location"]).query)


async def exchange(client: Any, code: Any, **kwargs: Any) -> Any:
    return await client.post(
        TOKEN,
        data={
            "client_id": "google-test-client",
            "client_secret": "test-secret",
            "redirect_uri": "https://consumer.example/callback",
            "grant_type": "authorization_code",
            "code": code,
            **kwargs,
        },
    )


async def test_offline_oidc_pkce_refresh_and_revoke(make_client: Any) -> None:
    async with make_client("google") as client:
        verifier = "verifier" * 12
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = await authorize(
            client,
            access_type="offline",
            state="opaque-state",
            nonce="nonce",
            code_challenge=challenge,
            code_challenge_method="S256",
            prompt="consent",
        )
        assert query["state"] == ["opaque-state"]
        assert query["authuser"] == ["0"]
        code = query["code"][0]
        assert (await exchange(client, code, code_verifier="wrong")).status_code == 400
        response = await exchange(client, code, code_verifier=verifier)
        assert response.status_code == 200, response.text
        tokens = response.json()
        assert tokens["access_token"].startswith("ya29.a0")
        assert len(tokens["access_token"]) >= 190
        assert tokens["refresh_token"].startswith("1//0g")
        assert tokens["expires_in"] == 3599
        assert (await exchange(client, code, code_verifier=verifier)).status_code == 400
        key = client.hub.context("google").signing_key.public_pem
        payload = jwt.decode(tokens["id_token"], key, algorithms=["RS256"], audience="google-test-client")
        assert payload["iss"] == "https://accounts.google.com"
        assert payload["nonce"] == "nonce"
        assert payload["email"] == "maya.chen@halcyonadvisory.example"
        assert payload["email_verified"] is True
        assert len(payload["sub"]) == 21 and payload["sub"].isdigit()
        assert (
            payload["at_hash"]
            == base64.urlsafe_b64encode(hashlib.sha256(tokens["access_token"].encode()).digest()[:16])
            .rstrip(b"=")
            .decode()
        )
        for name, value in (("access_token", tokens["access_token"]), ("id_token", tokens["id_token"])):
            info = await client.get("https://oauth2.googleapis.com/tokeninfo", params={name: value})
            assert info.status_code == 200
            assert info.json()["aud"] == "google-test-client"
        for path in (
            "https://openidconnect.googleapis.com/v1/userinfo",
            "https://www.googleapis.com/oauth2/v3/userinfo",
            "https://www.googleapis.com/oauth2/v2/userinfo",
        ):
            info = await client.get(path, headers=bearer(tokens["access_token"]))
            assert info.json()["email"] == payload["email"]
        refreshed = await client.post(
            TOKEN,
            data={
                "grant_type": "refresh_token",
                "client_id": "google-test-client",
                "client_secret": "test-secret",
                "refresh_token": tokens["refresh_token"],
            },
        )
        assert refreshed.status_code == 200
        assert "refresh_token" not in refreshed.json()
        assert refreshed.json()["access_token"] != tokens["access_token"]
        assert (
            await client.post(
                "https://oauth2.googleapis.com/revoke", params={"token": tokens["refresh_token"]}
            )
        ).status_code == 200
        assert (
            await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers=bearer(refreshed.json()["access_token"]),
            )
        ).status_code == 401


async def test_online_does_not_issue_refresh_picker_discovery_and_errors(make_client: Any) -> None:
    async with make_client("google") as client:
        query = await authorize(client)
        tokens = (await exchange(client, query["code"][0])).json()
        assert "refresh_token" not in tokens
        picker = await client.get(
            AUTH,
            params={
                "client_id": "picker",
                "redirect_uri": "https://consumer.example/callback",
                "response_type": "code",
                "scope": "email",
            },
        )
        assert picker.status_code == 200
        assert "maya.chen@halcyonadvisory.example" in picker.text
        denied = await authorize(client, login_hint="nobody@halcyonadvisory.example", state="s")
        assert denied == {"error": ["access_denied"], "state": ["s"]}
        discovery = (await client.get("https://accounts.google.com/.well-known/openid-configuration")).json()
        assert discovery["token_endpoint"] == TOKEN
        keys = (await client.get(discovery["jwks_uri"])).json()
        assert keys["keys"][0]["alg"] == "RS256"
        assert "BEGIN CERTIFICATE" in next(
            iter((await client.get("https://www.googleapis.com/oauth2/v1/certs")).json().values())
        )
        assert (
            await client.get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": "invalid"})
        ).status_code == 400
        response = await client.post(
            TOKEN, data={"grant_type": "refresh_token", "client_id": "c", "refresh_token": "bad"}
        )
        assert response.json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    "subject,expected",
    [(None, 403), ("maya.chen@halcyonadvisory.example", 403), ("arjun.mehta@halcyonadvisory.example", 200)],
)
async def test_service_account_structure_and_domain_delegation(
    make_client: Any, subject: Any, expected: Any
) -> None:
    async with make_client("google") as client:
        ctx = client.hub.context("google")
        now = ctx.clock.timestamp()
        claims = {
            "iss": "test-service@halcyonadvisory.example",
            "scope": "https://www.googleapis.com/auth/admin.directory.user.readonly",
            "aud": TOKEN,
            "iat": now,
            "exp": now + 3600,
        }
        if subject:
            claims["sub"] = subject
        assertion = ctx.signing_key.sign(claims)
        response = await client.post(
            TOKEN, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
        )
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        users = await client.get(
            "https://admin.googleapis.com/admin/directory/v1/users",
            params={"customer": "my_customer"},
            headers=bearer(token),
        )
        assert users.status_code == expected
        claims["exp"] = now - 1
        invalid = await client.post(
            TOKEN,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": ctx.signing_key.sign(claims),
            },
        )
        assert invalid.status_code == 400 and invalid.json()["error"] == "invalid_grant"


async def test_control_mint_query_auth_expiry_basic_and_redirect_binding(make_client: Any) -> None:
    async with make_client("google") as client:
        token = await mint(client, "https://www.googleapis.com", user="p018", expires_in=1)
        assert (
            await client.get("https://www.googleapis.com/oauth2/v3/userinfo", params={"access_token": token})
        ).status_code == 200
        from datetime import timedelta

        client.hub.clock.advance(timedelta(seconds=2))
        assert (
            await client.get("https://www.googleapis.com/oauth2/v3/userinfo", headers=bearer(token))
        ).status_code == 401
        code = (await authorize(client, scope="email"))["code"][0]
        mismatch = await exchange(client, code, redirect_uri="https://consumer.example/wrong")
        assert mismatch.status_code == 400
        response = await client.post(
            "https://www.googleapis.com/oauth2/v4/token",
            auth=("google-test-client", "test-secret"),
            data={
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": "https://consumer.example/callback",
            },
        )
        assert response.status_code == 200 and "id_token" not in response.json()
