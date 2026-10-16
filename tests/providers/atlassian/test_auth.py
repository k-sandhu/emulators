from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest

from emulators.core.hub import EmulatorHub
from emulators.providers.atlassian.common import CLOUD_ID
from emulators.testing.helpers import bearer, mint

SITE = "https://halcyonadvisory.atlassian.net"
API = "https://api.atlassian.com"
AUTH = "https://auth.atlassian.com"


async def test_oauth_code_gateway_and_rotating_refresh(client: httpx.AsyncClient) -> None:
    redirect = "https://consumer.example/oauth/callback"
    scope = "read:confluence-content.all read:confluence-space.summary read:page:confluence read:jira-work offline_access"
    consent = await client.get(
        AUTH + "/authorize",
        params={
            "audience": "api.atlassian.com",
            "client_id": "lumen",
            "scope": scope,
            "redirect_uri": redirect,
            "state": "state with spaces",
            "response_type": "code",
            "prompt": "consent",
            "login_hint": "maya.chen@halcyonadvisory.example",
        },
    )
    assert consent.status_code == 302
    callback = parse_qs(urlsplit(consent.headers["location"]).query)
    assert callback["state"] == ["state with spaces"]
    body = {
        "grant_type": "authorization_code",
        "client_id": "lumen",
        "client_secret": "arbitrary-test-secret",
        "redirect_uri": redirect,
        "code": callback["code"][0],
    }
    result = await client.post(AUTH + "/oauth/token", json=body)
    assert result.status_code == 200, result.text
    tokens = result.json()
    assert tokens["expires_in"] == 3600 and tokens["token_type"] == "Bearer" and tokens["scope"] == scope
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    assert claims["sub"].startswith("712020:") and claims["aud"] == "api.atlassian.com"
    assert (await client.post(AUTH + "/oauth/token", json=body)).status_code == 400
    headers = bearer(tokens["access_token"])
    resources = (await client.get(API + "/oauth/token/accessible-resources", headers=headers)).json()
    assert (
        resources[0]["url"] == SITE
        and resources[0]["id"] == CLOUD_ID
        and resources[0]["scopes"] == scope.split()
    )
    assert (await client.get(API + "/me", headers=headers)).json()[
        "email"
    ] == "maya.chen@halcyonadvisory.example"
    pages = await client.get(
        API + f"/ex/confluence/{CLOUD_ID}/wiki/api/v2/pages", headers=headers, params={"limit": 2}
    )
    assert pages.status_code == 200 and len(pages.json()["results"]) == 2
    assert f"/ex/confluence/{CLOUD_ID}/wiki/api/v2/pages?" in pages.json()["_links"]["next"]
    assert (await client.get(pages.json()["_links"]["next"], headers=headers)).status_code == 200
    jira = await client.get(
        API + f"/ex/jira/{CLOUD_ID}/rest/api/3/search/jql", headers=headers, params={"jql": "project = OPS"}
    )
    assert jira.status_code == 200
    refreshed = await client.post(
        AUTH + "/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": "lumen",
            "client_secret": "any",
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refreshed.status_code == 200 and refreshed.json()["refresh_token"] != tokens["refresh_token"]
    replay = await client.post(
        AUTH + "/oauth/token",
        json={"grant_type": "refresh_token", "client_id": "lumen", "refresh_token": tokens["refresh_token"]},
    )
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"


async def test_authorize_picker_binding_pkce_and_no_client_credentials(client: httpx.AsyncClient) -> None:
    params = {
        "audience": "api.atlassian.com",
        "client_id": "test",
        "redirect_uri": "https://consumer.example/cb",
        "response_type": "code",
        "scope": "read:page:confluence",
        "code_challenge": "verifier",
    }
    picker = await client.get(AUTH + "/authorize", params=params)
    assert picker.status_code == 200 and "Choose an account" in picker.text
    approved = await client.get(AUTH + "/authorize", params={**params, "login_hint": "p018"})
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    payload = {
        "grant_type": "authorization_code",
        "client_id": "test",
        "code": code,
        "redirect_uri": params["redirect_uri"],
    }
    assert (await client.post(AUTH + "/oauth/token", json=payload)).status_code == 400
    assert (
        await client.post(
            AUTH + "/oauth/token",
            json={**payload, "code_verifier": "verifier", "redirect_uri": "https://other.example/cb"},
        )
    ).status_code == 400
    valid = await client.post(AUTH + "/oauth/token", json={**payload, "code_verifier": "verifier"})
    assert valid.status_code == 200 and "refresh_token" not in valid.json()
    unsupported = await client.post(
        AUTH + "/oauth/token", json={"grant_type": "client_credentials", "client_id": "test"}
    )
    assert unsupported.json()["error"] == "unsupported_grant_type"


@pytest.mark.parametrize(
    "path,envelope",
    [
        ("/wiki/api/v2/pages", "errors"),
        ("/wiki/rest/api/user/current", "statusCode"),
        ("/rest/api/3/myself", "errorMessages"),
    ],
)
async def test_missing_invalid_expired_auth_and_errors(
    client: httpx.AsyncClient, hub: EmulatorHub, path: str, envelope: str
) -> None:
    for headers in ({}, bearer("invalid")):
        response = await client.get(path, headers=headers)
        assert response.status_code == 401 and envelope in response.json()
    expired = await mint(client, SITE, user="p001", expires_in=-1)
    response = await client.get(path, headers=bearer(expired))
    assert response.status_code == 401 and envelope in response.json()
    assert "www-authenticate" in response.headers


async def test_basic_auth_wildcard_and_cloud_boundary(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    basic = httpx.BasicAuth("maya.chen@halcyonadvisory.example", "halcyon-api-token")
    response = await client.get(SITE + "/wiki/rest/api/user/current", auth=basic)
    assert response.status_code == 200 and response.json()["displayName"] == "Maya Chen"
    alias = await client.get("https://sandbox.atlassian.net/wiki/api/v2/spaces", auth=basic)
    assert alias.status_code == 200
    assert (
        await client.get(
            SITE + "/wiki/api/v2/pages", auth=httpx.BasicAuth("maya.chen@halcyonadvisory.example", "bad")
        )
    ).status_code == 401
    assert (
        await client.get(API + f"/ex/confluence/{CLOUD_ID}/wiki/api/v2/pages", auth=basic)
    ).status_code == 401
    assert (
        await client.get(API + "/ex/confluence/wrong-cloud/wiki/api/v2/pages", headers=auth)
    ).status_code == 404


async def test_scopes_inactive_users_and_app_permissions(client: httpx.AsyncClient, hub: EmulatorHub) -> None:
    token = await mint(client, SITE, user="p018", scopes=["read:page:confluence"])
    assert (await client.get("/wiki/api/v2/pages", headers=bearer(token))).status_code == 200
    assert (await client.get("/rest/api/3/myself", headers=bearer(token))).status_code == 403
    denied = await client.post("/wiki/api/v2/pages", headers=bearer(token), json={})
    assert denied.status_code == 403
    inactive = next(p.id for p in hub.world.employees if not p.active)
    token = await mint(client, SITE, user=inactive)
    assert (await client.get("/wiki/api/v2/pages", headers=bearer(token))).status_code == 401
    token = await mint(client, SITE, app_only=True)
    spaces = (await client.get("/wiki/api/v2/spaces", headers=bearer(token))).json()["results"]
    assert "TECH" in {s["key"] for s in spaces}
    assert "PARTNERS" not in {s["key"] for s in spaces}
    assert (await client.get(API + "/me", headers=bearer(token))).status_code == 403
    assert (await client.get("/rest/api/3/issue/ENG-1", headers=bearer(token))).status_code == 404
    tech = next(s for s in spaces if s["key"] == "TECH")
    prop = await client.post(
        f"/wiki/api/v2/spaces/{tech['id']}/properties",
        headers=bearer(token),
        json={"key": "service-checkpoint", "value": 1},
    )
    assert prop.status_code == 200 and prop.json()["version"]["authorId"].startswith("712020:")


async def test_expiry_follows_emulator_clock(client: httpx.AsyncClient, hub: EmulatorHub) -> None:
    token = await mint(client, SITE, user="p001", expires_in=20)
    hub.clock.advance(timedelta(seconds=21))
    try:
        assert (await client.get("/wiki/api/v2/spaces", headers=bearer(token))).status_code == 401
    finally:
        hub.clock.advance(timedelta(seconds=-21))


async def test_zero_lifetime_and_malformed_control_plane_tokens(client: httpx.AsyncClient) -> None:
    token = await mint(client, SITE, user="p001", expires_in=0)
    assert (await client.get("/wiki/api/v2/pages", headers=bearer(token))).status_code == 401
    for payload in ({"scopes": 123}, {"expires_in": "invalid"}, {"app_only": False}):
        response = await client.post("/_emulator/tokens", json=payload)
        assert response.status_code == 400 and response.json()["error"] == "invalid_request"


async def test_strict_client_credentials_are_seeded() -> None:
    hub = EmulatorHub(providers=["atlassian"], anchor="2026-10-16", lenient_clients=False)
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        response = await client.post(
            AUTH + "/oauth/token",
            json={"grant_type": "refresh_token", "client_id": "emulator-client", "client_secret": "wrong"},
        )
        assert response.status_code == 401 and response.json()["error"] == "invalid_client"
        params = {
            "audience": "api.atlassian.com",
            "client_id": "emulator-client",
            "redirect_uri": "https://consumer.example/cb",
            "response_type": "code",
            "scope": "read:page:confluence",
            "login_hint": "p001",
        }
        response = await client.get(AUTH + "/authorize", params=params)
        assert response.status_code == 302
        code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
        token = await client.post(
            AUTH + "/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": "emulator-client",
                "client_secret": "emulator-secret",
                "redirect_uri": params["redirect_uri"],
                "code": code,
            },
        )
        assert token.status_code == 200


async def test_granular_metadata_comment_and_property_scopes(client: httpx.AsyncClient) -> None:
    page_id = "22006646768537502"
    page_only = bearer(await mint(client, SITE, user="p001", scopes=["read:page:confluence"]))
    assert (await client.get(f"/wiki/api/v2/pages/{page_id}/ancestors", headers=page_only)).status_code == 403
    assert (
        await client.get(f"/wiki/api/v2/pages/{page_id}/footer-comments", headers=page_only)
    ).status_code == 403
    for endpoint, scope in (
        ("ancestors", "read:content.metadata:confluence"),
        ("footer-comments", "read:comment:confluence"),
    ):
        auth = bearer(await mint(client, SITE, user="p001", scopes=[scope]))
        assert (await client.get(f"/wiki/api/v2/pages/{page_id}/{endpoint}", headers=auth)).status_code == 200
    path = "/wiki/api/v2/spaces/38039918718755833/properties"
    write_only = bearer(await mint(client, SITE, user="p001", scopes=["write:space:confluence"]))
    assert (
        await client.post(path, headers=write_only, json={"key": "scope-test", "value": 1})
    ).status_code == 403
    auth = bearer(
        await mint(client, SITE, user="p001", scopes=["read:space:confluence", "write:space:confluence"])
    )
    assert (await client.post(path, headers=auth, json={"key": "scope-test", "value": 1})).status_code == 200
