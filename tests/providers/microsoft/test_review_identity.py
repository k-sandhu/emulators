from __future__ import annotations

from collections.abc import Callable
from typing import Any

import jwt
import pytest

from emulators.testing.helpers import bearer

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com"


async def test_discovered_userinfo_matches_id_token_subject(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        discovery = (await client.get(LOGIN + "/common/v2.0/.well-known/openid-configuration")).json()
        response = await client.post(
            LOGIN + "/common/oauth2/v2.0/token",
            data={
                "grant_type": "password",
                "client_id": "userinfo",
                "username": "maya.chen@halcyonadvisory.example",
                "password": "synthetic",
                "scope": "openid profile email User.Read",
            },
        )
        tokens = response.json()
        result = await client.get(discovery["userinfo_endpoint"], headers=bearer(tokens["access_token"]))
        assert result.status_code == 200, result.text
        assert result.json() == {
            "sub": jwt.decode(tokens["id_token"], options={"verify_signature": False})["sub"],
            "name": "Maya Chen",
            "given_name": "Maya",
            "family_name": "Chen",
            "email": "maya.chen@halcyonadvisory.example",
            "picture": GRAPH + "/v1.0/me/photo/$value",
        }
        assert (await client.get(discovery["userinfo_endpoint"])).status_code == 401
        assert (
            await client.post(discovery["userinfo_endpoint"], headers=bearer(tokens["access_token"]))
        ).json() == result.json()


@pytest.mark.parametrize(
    ("granted", "requested"),
    [
        ("openid offline_access https://graph.microsoft.com/.default", "User.Read"),
        ("openid offline_access User.Read Mail.Read", "https://graph.microsoft.com/User.Read"),
        ("openid offline_access https://graph.microsoft.com/User.Read", "User.Read"),
    ],
)
async def test_refresh_normalizes_scopes_and_rotates_original_offline_grant(
    make_client: Callable[..., Any],
    granted: str,
    requested: str,
) -> None:
    async with make_client("microsoft") as client:
        endpoint = LOGIN + "/common/oauth2/v2.0/token"
        original = (
            await client.post(
                endpoint,
                data={
                    "grant_type": "password",
                    "client_id": "refresh-contract",
                    "username": "maya.chen@halcyonadvisory.example",
                    "password": "synthetic",
                    "scope": granted,
                },
            )
        ).json()
        claims = jwt.decode(original["access_token"], options={"verify_signature": False})
        assert ".default" not in claims["scp"].split()
        assert "User.Read" in claims["scp"].split()
        response = await client.post(
            endpoint,
            data={
                "grant_type": "refresh_token",
                "client_id": "refresh-contract",
                "refresh_token": original["refresh_token"],
                "scope": requested,
            },
        )
        assert response.status_code == 200, response.text
        tokens = response.json()
        assert tokens["refresh_token"] != original["refresh_token"]
        assert jwt.decode(tokens["access_token"], options={"verify_signature": False})["scp"] == "User.Read"
        again = await client.post(
            endpoint,
            data={
                "grant_type": "refresh_token",
                "client_id": "refresh-contract",
                "refresh_token": tokens["refresh_token"],
                "scope": "User.Read",
            },
        )
        assert again.status_code == 200 and "refresh_token" in again.json()
        denied = await client.post(
            endpoint,
            data={
                "grant_type": "refresh_token",
                "client_id": "refresh-contract",
                "refresh_token": tokens["refresh_token"],
                "scope": "NoSuchPermission",
            },
        )
        assert denied.status_code == 400 and denied.json()["error_codes"] == [70011]


async def test_default_scope_uses_client_consent_configuration(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        client.hub.context("microsoft").tokens.register_client(
            "configured-consent", delegated_scopes=["User.Read", "Mail.Read"]
        )
        response = await client.post(
            LOGIN + "/common/oauth2/v2.0/token",
            data={
                "grant_type": "password",
                "client_id": "configured-consent",
                "username": "maya.chen@halcyonadvisory.example",
                "password": "synthetic",
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        assert (
            jwt.decode(response.json()["access_token"], options={"verify_signature": False})["scp"]
            == "User.Read Mail.Read"
        )


async def test_client_credentials_errors_and_response_shape(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        endpoint = LOGIN + "/common/oauth2/v2.0/token"
        response = await client.post(
            endpoint, data={"grant_type": "client_credentials", "client_id": "missing-scope"}
        )
        assert response.status_code == 400 and response.json()["error_codes"] == [90014]
        response = await client.post(
            endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": "invalid-assertion",
                "scope": GRAPH + "/.default",
                "client_assertion": "not-a-jwt",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            },
        )
        assert response.status_code == 401 and response.json()["error"] == "invalid_client"
        response = await client.post(
            endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": "app-contract",
                "scope": GRAPH + "/.default",
            },
        )
        assert response.status_code == 200
        assert set(response.json()) == {"access_token", "token_type", "expires_in", "ext_expires_in"}
        claims = jwt.decode(response.json()["access_token"], options={"verify_signature": False})
        assert claims["roles"] and "scp" not in claims


async def test_v1_token_string_fields_and_device_verification_url(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        response = await client.post(
            LOGIN + "/common/oauth2/token",
            data={"grant_type": "client_credentials", "client_id": "v1-contract", "resource": GRAPH},
        )
        assert response.status_code == 200, response.text
        for field in ("expires_in", "ext_expires_in", "expires_on", "not_before"):
            assert isinstance(response.json()[field], str)
        response = await client.post(
            LOGIN + "/common/oauth2/devicecode", data={"client_id": "v1-device", "resource": GRAPH}
        )
        assert response.json()["verification_url"] == "https://microsoft.com/devicelogin"
        assert "verification_uri" not in response.json()
