"""OAuth grant binding, scope boundaries and malformed JWT regressions."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest

from emulators.core.hub import EmulatorHub

from .conftest import API, BASE, LOGIN, Actor, username

TOKEN_URL = f"{LOGIN}/services/oauth2/token"
CLIENT = "audit-client"
CALLBACK = "https://consumer.example/callback"
ADMIN = username("arjun.mehta")


async def code(client: httpx.AsyncClient, **kwargs: str) -> str:
    response = await client.get(
        f"{LOGIN}/services/oauth2/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT,
            "redirect_uri": CALLBACK,
            "login_hint": ADMIN,
            "scope": "api refresh_token",
            **kwargs,
        },
    )
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["location"]).query)["code"][0]


async def exchange(client: httpx.AsyncClient, authorization_code: str, **kwargs: str) -> httpx.Response:
    return await client.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT,
            "client_secret": "emulator-only",
            "redirect_uri": CALLBACK,
            "code": authorization_code,
            **kwargs,
        },
    )


async def test_code_redirect_uri_is_required_and_bound_without_consuming_code(
    client: httpx.AsyncClient,
) -> None:
    value = await code(client)
    for redirect in ("", "https://other.example/callback"):
        response = await exchange(client, value, redirect_uri=redirect)
        assert response.status_code == 400 and response.json()["error"] == "invalid_grant"
    assert (await exchange(client, value)).status_code == 200


async def test_verifier_without_a_challenge_is_rejected(client: httpx.AsyncClient) -> None:
    value = await code(client)
    response = await exchange(client, value, code_verifier="v" * 50)
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant"
    assert (await exchange(client, value)).status_code == 200


async def test_refresh_token_cannot_be_redeemed_by_another_client(client: httpx.AsyncClient) -> None:
    response = await exchange(client, await code(client))
    refresh = response.json()["refresh_token"]
    form = {"grant_type": "refresh_token", "client_id": "different-client", "refresh_token": refresh}
    denied = await client.post(TOKEN_URL, data=form)
    assert denied.status_code == 400 and denied.json()["error"] == "invalid_grant"
    form["client_id"] = CLIENT
    refreshed = await client.post(TOKEN_URL, data=form)
    assert refreshed.status_code == 200 and "refresh_token" not in refreshed.json()
    assert (
        await client.get(
            f"{API}/limits", headers={"Authorization": "Bearer " + refreshed.json()["access_token"]}
        )
    ).status_code == 200


@pytest.mark.parametrize("scope", ["id", "openid", "web", "chatter_api"])
async def test_oauth_scopes_gate_data_api_but_identity_still_works(
    client: httpx.AsyncClient, scope: str
) -> None:
    issued = await exchange(client, await code(client, scope=scope))
    assert issued.status_code == 200, issued.text
    headers = {"Authorization": "Bearer " + issued.json()["access_token"]}
    denied = await client.get(f"{API}/query", params={"q": "SELECT Id FROM Account"}, headers=headers)
    assert denied.status_code == 401
    assert denied.json() == [
        {"message": "This session is not valid for use with the REST API", "errorCode": "INVALID_SESSION_ID"}
    ]
    assert (await client.get(f"{LOGIN}/services/oauth2/userinfo", headers=headers)).status_code == 200
    if scope == "chatter_api":
        assert (await client.get(f"{API}/chatter/users/me", headers=headers)).status_code == 200


async def test_full_scope_allows_data_and_unknown_authorization_scopes_are_rejected(
    client: httpx.AsyncClient,
) -> None:
    issued = await exchange(client, await code(client, scope="full"))
    headers = {"Authorization": "Bearer " + issued.json()["access_token"]}
    assert (await client.get(f"{API}/limits", headers=headers)).status_code == 200
    denied = await client.get(
        f"{LOGIN}/services/oauth2/authorize",
        params={
            "client_id": CLIENT,
            "redirect_uri": CALLBACK,
            "scope": "made_up_scope",
            "login_hint": ADMIN,
        },
    )
    assert parse_qs(urlsplit(denied.headers["location"]).query)["error"] == ["invalid_scope"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("aud", 5),
        ("aud", True),
        ("aud", {"host": "login.salesforce.com"}),
        ("aud", [5]),
        ("aud", "http://login.salesforce.com"),
        ("aud", "https://evil.example/.salesforce.com"),
        ("aud", "https://login.salesforce.com/evil"),
        ("sub", [ADMIN]),
        ("iss", [CLIENT]),
        ("exp", []),
        ("exp", "Infinity"),
        ("exp", "NaN"),
        ("exp", True),
        ("exp", None),
        ("jti", []),
    ],
)
async def test_malformed_jwt_claims_are_oauth_errors(
    client: httpx.AsyncClient, field: str, value: Any
) -> None:
    payload = {"iss": CLIENT, "sub": ADMIN, "aud": LOGIN, "exp": 4_000_000_000, field: value}
    assertion = jwt.api_jws.encode(
        json.dumps(payload).encode(), "emulator-audit-signing-material-32-bytes", algorithm="HS256"
    )
    response = await client.post(
        TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    )
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant", response.text


async def test_jwt_principal_clock_skew_and_replay_id(client: httpx.AsyncClient, hub: EmulatorHub) -> None:
    payload = {
        "iss": CLIENT,
        "prn": ADMIN,
        "aud": LOGIN,
        "exp": hub.clock.timestamp() - 60,
        "jti": "audit-once",
    }
    assertion = jwt.encode(payload, "emulator-audit-signing-material-32-bytes", algorithm="HS256")
    form = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    assert (await client.post(TOKEN_URL, data=form)).status_code == 200
    denied = await client.post(TOKEN_URL, data=form)
    assert denied.status_code == 400 and denied.json()["error"] == "invalid_grant"


async def test_introspection_requires_client_and_obeys_token_ownership(client: httpx.AsyncClient) -> None:
    issued = (await exchange(client, await code(client))).json()
    form = {"token": issued["access_token"]}
    assert (await client.post(f"{LOGIN}/services/oauth2/introspect", data=form)).status_code == 401
    form["client_id"] = "different-client"
    denied = await client.post(f"{LOGIN}/services/oauth2/introspect", data=form)
    assert denied.json() == {"active": False}
    form["client_id"] = CLIENT
    assert (await client.post(f"{LOGIN}/services/oauth2/introspect", data=form)).json()["active"] is True


async def test_deactivated_users_are_inactive_during_introspection(
    actor: Callable[..., Awaitable[Actor]],
    admin: Actor,
) -> None:
    user = await actor("p011")
    assert (await admin.patch(f"/sobjects/User/{user.user_id}", {"IsActive": False})).status_code == 204
    form = {"token": user.token, "client_id": "emulator-client"}
    assert (await admin.client.post(f"{LOGIN}/services/oauth2/introspect", data=form)).json() == {
        "active": False
    }


async def test_strict_unknown_authorization_client_has_oauth_error() -> None:
    hub = EmulatorHub(providers=["salesforce"], anchor="2026-10-16", lenient_clients=False)
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        response = await client.get(
            f"{LOGIN}/services/oauth2/authorize",
            params={
                "client_id": "unknown",
                "redirect_uri": CALLBACK,
                "login_hint": ADMIN,
            },
        )
    assert response.status_code == 400 and response.json()["error"] == "invalid_client_id"


@pytest.mark.parametrize(
    "payload", [{"expires_in": 10**40}, {"expires_in": 1.5}, {"app_only": "false"}, {"app_only": []}]
)
async def test_control_token_rejects_expiry_overflow_and_wrong_boolean_types(
    client: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    response = await client.post(f"{BASE}/_emulator/tokens", json={"user": "p011", **payload})
    assert response.status_code == 400, response.text


async def test_explicit_empty_control_token_scopes_do_not_gain_full_access(client: httpx.AsyncClient) -> None:
    token = (await client.post(f"{BASE}/_emulator/tokens", json={"user": "p011", "scopes": []})).json()
    assert token["scope"] == ""
    response = await client.get(f"{API}/limits", headers={"Authorization": "Bearer " + token["access_token"]})
    assert response.status_code == 401 and response.json()[0]["errorCode"] == "INVALID_SESSION_ID"


async def test_control_token_urls_follow_gateway_prefix(hub: EmulatorHub) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hub.gateway)) as client:
        response = await client.post(
            "http://localhost:44080/salesforce/_emulator/tokens", json={"user": "p035"}
        )
        body = response.json()
        assert body["instance_url"] == "http://localhost:44080/salesforce"
        assert body["id"].startswith("http://localhost:44080/salesforce/id/")
        assert (
            await client.get(body["id"], headers={"Authorization": "Bearer " + body["access_token"]})
        ).status_code == 200
