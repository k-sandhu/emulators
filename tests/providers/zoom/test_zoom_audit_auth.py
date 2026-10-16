"""OAuth grants must stay bound to their client, redirect URI, scopes and active account user."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer
from tests.providers.zoom.conftest import API, HOST, OAUTH, V2, token_for
from tests.providers.zoom.test_zoom_auth import BASIC, REDIRECT, authorize


async def code_for(zoom: httpx.AsyncClient, **extra: str) -> str:
    response = await authorize(zoom, login_hint=HOST, **extra)
    assert response.status_code == 302, response.text
    return parse_qs(urlparse(response.headers["location"]).query)["code"][0]


async def exchange(zoom: httpx.AsyncClient, code: str) -> httpx.Response:
    return await zoom.post(
        OAUTH + "/oauth/token",
        auth=BASIC,
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT},
    )


async def test_authorize_query_cannot_increase_registered_scopes(zoom: httpx.AsyncClient) -> None:
    await zoom.post(
        API + "/_emulator/actions/register_app",
        json={
            "client_id": BASIC[0],
            "client_secret": BASIC[1],
            "scopes": ["meeting:read"],
        },
    )
    code = await code_for(zoom, scope="user:read:admin meeting:write:admin")
    response = await exchange(zoom, code)
    assert response.status_code == 200
    assert response.json()["scope"] == "meeting:read"
    denied = await zoom.get(V2 + "/users", headers=bearer(response.json()["access_token"]))
    assert denied.status_code == 401 and denied.json()["code"] == 4700


async def test_empty_registered_scope_set_never_becomes_unrestricted(zoom: httpx.AsyncClient) -> None:
    await zoom.post(API + "/_emulator/actions/register_app", json={"client_id": BASIC[0], "scopes": []})
    response = await exchange(zoom, await code_for(zoom))
    assert response.status_code == 200 and response.json()["scope"] == ""
    denied = await zoom.get(V2 + "/users/me/meetings", headers=bearer(response.json()["access_token"]))
    assert denied.status_code == 401 and denied.json()["code"] == 4700


async def test_code_exchange_requires_redirect_uri_and_failed_attempt_does_not_consume_code(
    zoom: httpx.AsyncClient,
) -> None:
    code = await code_for(zoom)
    missing = await zoom.post(
        OAUTH + "/oauth/token",
        auth=BASIC,
        data={"grant_type": "authorization_code", "code": code},
    )
    assert missing.status_code == 400 and missing.json()["error"] == "invalid_request"
    assert (await exchange(zoom, code)).status_code == 200


async def test_revoke_cannot_revoke_another_client_token(zoom: httpx.AsyncClient) -> None:
    tokens = (await exchange(zoom, await code_for(zoom))).json()
    wrong = await zoom.post(
        OAUTH + "/oauth/revoke",
        auth=("other-client", "other-test-secret"),
        data={"token": tokens["access_token"]},
    )
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_request"
    assert (await zoom.get(V2 + "/users/me", headers=bearer(tokens["access_token"]))).status_code == 200
    revoked = await zoom.post(OAUTH + "/oauth/revoke", auth=BASIC, data={"token": tokens["access_token"]})
    assert revoked.status_code == 200
    assert (await zoom.get(V2 + "/users/me", headers=bearer(tokens["access_token"]))).status_code == 401


@pytest.mark.parametrize("action", ["delete", "deactivate"])
async def test_directory_state_prevents_authorize_code_and_refresh_after_user_removal(
    zoom: httpx.AsyncClient, s2s: dict[str, str], action: str
) -> None:
    tokens = (await exchange(zoom, await code_for(zoom))).json()
    code = await code_for(zoom)
    if action == "delete":
        removed = await zoom.delete(f"{V2}/users/{HOST}", headers=s2s)
    else:
        removed = await zoom.put(f"{V2}/users/{HOST}/status", headers=s2s, json={"action": action})
    assert removed.status_code == 204
    response = await authorize(zoom, login_hint=HOST)
    assert parse_qs(urlparse(response.headers["location"]).query)["error"] == ["access_denied"]
    assert (await exchange(zoom, code)).status_code == 400
    refreshed = await zoom.post(
        OAUTH + "/oauth/token",
        auth=BASIC,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refreshed.status_code == 400


async def test_strict_unknown_authorize_client_returns_oauth_error() -> None:
    hub = EmulatorHub(providers=["zoom"], anchor="2026-10-16", lenient_clients=False)
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        response = await authorize(client, login_hint=HOST)
    assert response.status_code == 401
    assert response.json() == {"reason": "Invalid client_id or client_secret", "error": "invalid_client"}
    assert response.headers["x-zm-trackingid"]


async def test_unsupported_pkce_method_returns_oauth_error(zoom: httpx.AsyncClient) -> None:
    response = await authorize(zoom, login_hint=HOST, code_challenge="challenge", code_challenge_method="md5")
    assert response.status_code == 400 and response.json()["error"] == "invalid_request"


@pytest.mark.parametrize("path", ["token", "revoke"])
async def test_oauth_invalid_utf8_never_crashes(zoom: httpx.AsyncClient, path: str) -> None:
    response = await zoom.post(OAUTH + "/oauth/" + path, auth=BASIC, content=b"\xff")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


async def test_zero_ttl_token_is_expired_immediately(zoom: httpx.AsyncClient) -> None:
    headers = await token_for(zoom, HOST, expires_in=0)
    response = await zoom.get(V2 + "/users/me", headers=headers)
    assert response.status_code == 401 and response.json()["code"] == 124


async def test_granular_user_scopes_and_classic_write_scope_support_reads(
    zoom: httpx.AsyncClient, ctx: Any
) -> None:
    for scopes, path in [
        (["user:read:email"], "/users/email?email=" + HOST),
        (["user:read:list_permissions"], "/users/me/permissions"),
        (["user:write"], "/users/me"),
    ]:
        response = await zoom.get(V2 + path, headers=await token_for(zoom, HOST, scopes=scopes))
        assert response.status_code == 200, response.text
    status = await zoom.put(
        f"{V2}/users/{HOST}/status",
        json={"action": "activate"},
        headers=await token_for(zoom, scopes=["user:update:status:admin"]),
    )
    assert status.status_code == 204
    user = (await zoom.get(V2 + "/users/me", headers=await token_for(zoom, HOST))).json()
    email = await zoom.put(
        V2 + "/users/me/email",
        json={"email": "renamed@halcyonadvisory.example"},
        headers=await token_for(zoom, HOST, scopes=["user:update:email"]),
    )
    assert email.status_code == 204
    assert all(
        m["host_email"] == "renamed@halcyonadvisory.example"
        for m in ctx.store.collection("meetings")
        if m["host_id"] == user["id"]
    )
    signed_in = await authorize(zoom, login_hint="renamed@halcyonadvisory.example")
    assert signed_in.status_code == 302 and "code=" in signed_in.headers["location"]
