from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint


async def test_oauth_picker_pkce_refresh_and_single_use(client: httpx.AsyncClient, hub: EmulatorHub) -> None:
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "client_id": "integration-test",
        "response_type": "code",
        "redirect_uri": "https://consumer.example/callback",
        "scope": "spark:all meeting:recordings_read meeting:transcripts_read",
        "state": "state & unicode é",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    picker = await client.get("/v1/authorize", params=params)
    assert picker.status_code == 200
    assert "Choose an account" in picker.text
    approved = await client.get(
        "/v1/authorize", params={**params, "login_hint": "sofia.alvarez@halcyonadvisory.example"}
    )
    assert approved.status_code == 302
    callback = parse_qs(urlparse(approved.headers["location"]).query)
    assert callback["state"] == [params["state"]]
    form = {
        "grant_type": "authorization_code",
        "client_id": params["client_id"],
        "client_secret": "test-only",
        "code": callback["code"][0],
        "redirect_uri": params["redirect_uri"],
        "code_verifier": "wrong",
    }
    assert (await client.post("/v1/access_token", data=form)).status_code == 400
    assert (
        await client.post("/v1/access_token", data={**form, "redirect_uri": "https://wrong.example"})
    ).status_code == 400
    form["code_verifier"] = verifier
    response = await client.post("/v1/access_token", data=form)
    assert response.status_code == 200, response.text
    token = response.json()
    assert token["expires_in"] == 1209600 and token["refresh_token_expires_in"] == 7776000
    assert token["token_type"] == "Bearer" and token["scope"] == params["scope"]
    assert response.headers["cache-control"] == "no-store"
    assert (await client.post("/v1/access_token", data=form)).status_code == 400
    me = await client.get("/v1/people/me", headers=bearer(token["access_token"]))
    assert me.json()["emails"] == ["sofia.alvarez@halcyonadvisory.example"]
    refresh = {
        "grant_type": "refresh_token",
        "client_id": params["client_id"],
        "refresh_token": token["refresh_token"],
    }
    assert (await client.post("/v1/access_token", data={**refresh, "client_id": "other"})).status_code == 400
    hub.clock.advance(timedelta(days=15))
    assert (await client.get("/v1/people/me", headers=bearer(token["access_token"]))).status_code == 401
    rotated = await client.post("/v1/access_token", data=refresh)
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != token["refresh_token"]
    assert (await client.post("/v1/access_token", data=refresh)).status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"response_type": "token", "client_id": "x", "redirect_uri": "https://consumer.example"},
        {
            "response_type": "code",
            "client_id": "x",
            "redirect_uri": "https://consumer.example",
            "login_hint": "unknown@example.invalid",
        },
    ],
)
async def test_bad_authorization(client: httpx.AsyncClient, payload: dict[str, str]) -> None:
    r = await client.get("/v1/authorize", params=payload)
    assert r.status_code == 400 and r.json()["trackingId"] == r.headers["trackingid"]


async def test_mint_identity_scope_expiry_and_admin_consent(
    client: httpx.AsyncClient, hub: EmulatorHub
) -> None:
    for user in ("p005", "sofia.alvarez@halcyonadvisory.example"):
        token = await mint(client, "https://webexapis.com", user=user)
        assert (await client.get("/v1/people/me", headers=bearer(token))).json()[
            "displayName"
        ] == "Sofia Alvarez"
    token = await mint(
        client, "https://webexapis.com", user="p005", scopes=["spark:people_read"], expires_in=1
    )
    assert (await client.get("/v1/recordings", headers=bearer(token))).status_code == 403
    hub.clock.advance(timedelta(seconds=2))
    assert (await client.get("/v1/people/me", headers=bearer(token))).status_code == 401
    assert (await client.post("/v1/access_token", json={"grant_type": "refresh_token"})).status_code == 400
    assert (
        await client.post("/v1/access_token", data={"client_id": "x", "grant_type": "client_credentials"})
    ).status_code == 400
    r = await client.get(
        "/v1/authorize",
        params={
            "client_id": "x",
            "redirect_uri": "https://consumer.example",
            "response_type": "code",
            "login_hint": "p005",
            "scope": "spark-admin:people_read",
        },
    )
    assert r.status_code == 403


async def test_strict_client_secret_and_redirect(client: httpx.AsyncClient, hub: EmulatorHub) -> None:
    ctx = hub.context("webex")
    ctx.tokens.lenient_clients = False
    ctx.tokens.register_client("strict", "expected", redirect_uris=["https://consumer.example/callback"])
    params = {
        "client_id": "strict",
        "response_type": "code",
        "login_hint": "p005",
        "redirect_uri": "https://other.example",
    }
    assert (await client.get("/v1/authorize", params=params)).status_code == 400
    params["redirect_uri"] = "https://consumer.example/callback"
    r = await client.get("/v1/authorize", params=params)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "client_id": "strict",
        "client_secret": "wrong",
        "code": code,
        "redirect_uri": params["redirect_uri"],
    }
    assert (await client.post("/v1/access_token", data=form)).status_code == 401
    assert (
        await client.post("/v1/access_token", data={**form, "client_secret": "expected"})
    ).status_code == 200


async def test_expired_and_disabled_control_tokens(client: httpx.AsyncClient) -> None:
    token = await mint(client, "https://webexapis.com", user="p005", expires_in=0)
    assert (await client.get("/v1/people/me", headers=bearer(token))).status_code == 401
    assert (await client.post("/_emulator/tokens", json={"user": 17})).status_code == 400


async def test_ended_seed_schedule_does_not_duplicate_artifacts(client: httpx.AsyncClient) -> None:
    instance = (
        await client.get(
            "/v1/meetings", params={"meetingType": "meeting", "from": "2026-09-01", "to": "2026-10-16"}
        )
    ).json()["items"][0]
    schedule = instance["scheduledMeetingId"]
    result = await client.post("/_emulator/actions/end_meeting", json={"meeting_id": schedule})
    assert result.status_code == 200 and result.json()["meeting"]["id"] == instance["id"]
    rows = (await client.get("/v1/recordings", params={"meetingId": instance["id"]})).json()["items"]
    assert len(rows) == 1
