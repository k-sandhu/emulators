"""Zoom OAuth: authorize, token grants (code+PKCE, refresh rotation, S2S, chatbot), revoke, errors."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer
from tests.providers.zoom.conftest import API, HOST, OAUTH, V2

REDIRECT = "https://app.example.test/callback"
BASIC = ("client-123", "secret-456")


def jwt_payload(token: str) -> dict[str, Any]:
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def pkce() -> tuple[str, str]:
    verifier = "v" * 64
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def authorize(client: httpx.AsyncClient, challenge: str | None = None, **extra: str) -> httpx.Response:
    params = {
        "response_type": "code",
        "client_id": BASIC[0],
        "redirect_uri": REDIRECT,
        "state": "xyz",
        **extra,
    }
    if challenge:
        params.update({"code_challenge": challenge, "code_challenge_method": "S256"})
    return await client.get(f"{OAUTH}/oauth/authorize", params=params)


async def test_authorization_code_with_pkce_returns_zoom_token_shape(zoom: httpx.AsyncClient) -> None:
    verifier, challenge = pkce()
    r = await authorize(zoom, challenge, login_hint=HOST)
    assert r.status_code == 302
    target = urlparse(r.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == REDIRECT
    query = parse_qs(target.query)
    assert query["state"] == ["xyz"]
    code = query["code"][0]

    token = await zoom.post(
        f"{OAUTH}/oauth/token",
        auth=BASIC,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    body = token.json()
    assert list(body) == ["access_token", "token_type", "refresh_token", "expires_in", "scope", "api_url"]
    assert body["token_type"] == "bearer" and body["expires_in"] == 3599
    assert body["access_token"].startswith("eyJzdiI6IjAwMDAwMSIs")  # base64 of {"sv":"000001",...
    assert body["api_url"] == "https://api.zoom.us"
    assert "recording:read:admin" in body["scope"].split()
    claims = jwt_payload(body["access_token"])
    assert claims["aid"] == zoom.hub.world.org.zoom_account_id and claims["exp"] > claims["iat"]  # type: ignore[attr-defined]
    assert token.headers["x-zm-trackingid"].startswith("v=2.0;clid=")

    me = await zoom.get(f"{V2}/users/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200 and me.json()["email"] == HOST

    # codes are single use
    again = await zoom.post(
        f"{OAUTH}/oauth/token",
        auth=BASIC,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert again.status_code == 400
    assert again.json() == {"reason": "Invalid authorization code", "error": "invalid_grant"}


async def test_pkce_mismatch_and_redirect_mismatch_are_rejected(zoom: httpx.AsyncClient) -> None:
    _, challenge = pkce()
    code = parse_qs(urlparse((await authorize(zoom, challenge, login_hint=HOST)).headers["location"]).query)[
        "code"
    ][0]
    bad = await zoom.post(
        f"{OAUTH}/oauth/token",
        auth=BASIC,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": "wrong",
        },
    )
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_request"

    code = parse_qs(urlparse((await authorize(zoom, login_hint=HOST)).headers["location"]).query)["code"][0]
    other = await zoom.post(
        f"{OAUTH}/oauth/token",
        auth=BASIC,
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": "https://evil.example/cb"},
    )
    assert other.status_code == 400
    assert other.json() == {"reason": "Redirect URI mismatch.", "error": "invalid_request"}


async def test_refresh_tokens_rotate_and_old_ones_die(zoom: httpx.AsyncClient) -> None:
    code = parse_qs(urlparse((await authorize(zoom, login_hint=HOST)).headers["location"]).query)["code"][0]
    first = (
        await zoom.post(
            f"{OAUTH}/oauth/token",
            auth=BASIC,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT},
        )
    ).json()
    refreshed = await zoom.post(
        f"{OAUTH}/oauth/token",
        auth=BASIC,
        data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    )
    assert refreshed.status_code == 200
    second = refreshed.json()
    assert second["refresh_token"] != first["refresh_token"] and second["scope"] == first["scope"]
    assert (await zoom.get(f"{V2}/users/me", headers=bearer(second["access_token"]))).status_code == 200

    replay = await zoom.post(
        f"{OAUTH}/oauth/token",
        auth=BASIC,
        data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    )
    assert replay.status_code == 400
    assert replay.json() == {"reason": "Invalid Token!", "error": "invalid_request"}


async def test_server_to_server_params_may_ride_in_the_query_string(zoom: httpx.AsyncClient) -> None:
    account = zoom.hub.world.org.zoom_account_id  # type: ignore[attr-defined]
    r = await zoom.post(
        f"{OAUTH}/oauth/token?grant_type=account_credentials&account_id={account}", auth=BASIC
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "refresh_token" not in body and body["token_type"] == "bearer" and body["expires_in"] == 3599
    users = await zoom.get(f"{V2}/users", headers=bearer(body["access_token"]), params={"page_size": 1})
    assert users.status_code == 200 and users.json()["total_records"] == 45

    wrong = await zoom.post(f"{OAUTH}/oauth/token?grant_type=account_credentials&account_id=nope", auth=BASIC)
    assert wrong.status_code == 400
    assert wrong.json() == {"reason": "Invalid Token!", "error": "invalid_request"}
    missing = await zoom.post(f"{OAUTH}/oauth/token", auth=BASIC, data={"grant_type": "account_credentials"})
    assert missing.status_code == 400 and missing.json()["error"] == "invalid_request"


async def test_client_credentials_is_a_chatbot_token_without_api_scopes(zoom: httpx.AsyncClient) -> None:
    r = await zoom.post(f"{OAUTH}/oauth/token?grant_type=client_credentials", auth=BASIC)
    assert r.status_code == 200 and r.json()["scope"] == "imchat:bot"
    denied = await zoom.get(f"{V2}/users", headers=bearer(r.json()["access_token"]))
    assert denied.status_code == 401
    assert denied.json()["code"] == 4700
    assert denied.json()["message"].startswith("Invalid access token, does not contain scopes:[")


async def test_token_endpoint_errors(zoom: httpx.AsyncClient) -> None:
    no_auth = await zoom.post(f"{OAUTH}/oauth/token", data={"grant_type": "client_credentials"})
    assert no_auth.status_code == 401
    assert no_auth.json() == {"reason": "Invalid client_id or client_secret", "error": "invalid_client"}
    no_grant = await zoom.post(f"{OAUTH}/oauth/token", auth=BASIC, data={})
    assert no_grant.status_code == 400 and no_grant.json()["error"] == "invalid_request"
    odd = await zoom.post(f"{OAUTH}/oauth/token", auth=BASIC, data={"grant_type": "password"})
    assert odd.status_code == 400 and odd.json()["error"] == "unsupported_grant_type"


async def test_authorize_picker_unknown_and_departed_users(zoom: httpx.AsyncClient) -> None:
    picker = await authorize(zoom)
    assert picker.status_code == 200 and "text/html" in picker.headers["content-type"]
    assert HOST in picker.text and "paul.henderson" not in picker.text  # departed staff cannot sign in
    for hint in ("paul.henderson@halcyonadvisory.example", "nobody@halcyonadvisory.example"):
        denied = await authorize(zoom, login_hint=hint)
        assert denied.status_code == 302
        query = parse_qs(urlparse(denied.headers["location"]).query)
        assert query["error"] == ["access_denied"] and query["state"] == ["xyz"]
    missing = await zoom.get(f"{OAUTH}/oauth/authorize", params={"response_type": "code"})
    assert missing.status_code == 400


async def test_registered_app_pins_scopes_and_redirect_uris(zoom: httpx.AsyncClient) -> None:
    reg = await zoom.post(
        f"{API}/_emulator/actions/register_app",
        json={
            "client_id": "pinned",
            "client_secret": "s3cret",
            "redirect_uris": [REDIRECT],
            "scopes": "meeting:read recording:read",
        },
    )
    assert reg.status_code == 200 and "client_secret" not in reg.json()
    bad = await zoom.get(
        f"{OAUTH}/oauth/authorize",
        params={"response_type": "code", "client_id": "pinned", "redirect_uri": "https://evil.example/cb"},
    )
    assert bad.status_code == 400 and bad.json()["reason"] == "Redirect URI mismatch."
    ok = await zoom.get(
        f"{OAUTH}/oauth/authorize",
        params={"response_type": "code", "client_id": "pinned", "redirect_uri": REDIRECT, "login_hint": HOST},
    )
    code = parse_qs(urlparse(ok.headers["location"]).query)["code"][0]
    token = (
        await zoom.post(
            f"{OAUTH}/oauth/token",
            auth=("pinned", "s3cret"),
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT},
        )
    ).json()
    assert token["scope"] == "meeting:read recording:read"
    # the app has no user:read scope, so even the user's own profile is refused
    denied = await zoom.get(f"{V2}/users/me", headers=bearer(token["access_token"]))
    assert denied.status_code == 401 and denied.json()["code"] == 4700
    assert (
        await zoom.get(f"{V2}/users/me/meetings", headers=bearer(token["access_token"]))
    ).status_code == 200


async def test_revoke_kills_the_token_and_its_refresh_chain(zoom: httpx.AsyncClient) -> None:
    code = parse_qs(urlparse((await authorize(zoom, login_hint=HOST)).headers["location"]).query)["code"][0]
    tokens = (
        await zoom.post(
            f"{OAUTH}/oauth/token",
            auth=BASIC,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT},
        )
    ).json()
    revoked = await zoom.post(f"{OAUTH}/oauth/revoke", auth=BASIC, data={"token": tokens["refresh_token"]})
    assert revoked.status_code == 200 and revoked.json() == {"status": "success"}
    assert (await zoom.get(f"{V2}/users/me", headers=bearer(tokens["access_token"]))).status_code == 401
    refresh = await zoom.post(
        f"{OAUTH}/oauth/token",
        auth=BASIC,
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
    )
    assert refresh.status_code == 400


async def test_api_auth_errors_and_tracking_header(zoom: httpx.AsyncClient) -> None:
    none = await zoom.get(f"{V2}/users/me")
    assert none.status_code == 401
    assert none.json() == {"code": 124, "message": "Invalid access token."}
    assert none.headers["x-zm-trackingid"]
    junk = await zoom.get(f"{V2}/users/me", headers=bearer("garbage"))
    assert junk.status_code == 401 and junk.json()["code"] == 124
    expired = (await zoom.post(f"{API}/_emulator/tokens", json={"user": HOST, "expires_in": -5})).json()[
        "access_token"
    ]
    late = await zoom.get(f"{V2}/users/me", headers=bearer(expired))
    assert late.status_code == 401 and late.json() == {"code": 124, "message": "Access token is expired."}
    # unknown route and wrong method keep Zoom's envelope and tracking id
    token = (await zoom.post(f"{API}/_emulator/tokens", json={"user": HOST})).json()["access_token"]
    missing = await zoom.get(f"{V2}/nothing-here", headers=bearer(token))
    assert missing.status_code == 404 and set(missing.json()) == {"code", "message"}
    assert missing.headers["x-zm-trackingid"]


async def test_injected_throttling_looks_like_zoom(zoom: httpx.AsyncClient, host: dict[str, str]) -> None:
    await zoom.post(f"{API}/_emulator/faults", json={"path": "/v2/users/me", "status": 429, "times": 1})
    limited = await zoom.get(f"{V2}/users/me", headers=host)
    assert limited.status_code == 429
    assert limited.json() == {
        "code": 429,
        "message": "You have reached the maximum per-second rate limit for this API. Try again later.",
    }
    assert limited.headers["retry-after"] == "1"
    assert limited.headers["x-ratelimit-limit"] == "30" and limited.headers["x-ratelimit-remaining"] == "0"
    assert limited.headers["x-zm-trackingid"]
    assert (await zoom.get(f"{V2}/users/me", headers=host)).status_code == 200


async def test_strict_clients_need_the_registered_secret() -> None:
    hub = EmulatorHub(providers=["zoom"], anchor="2026-10-16", lenient_clients=False, webhook_mode="sync")
    account = hub.world.org.zoom_account_id
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        unknown = await client.post(
            f"{OAUTH}/oauth/token",
            params={"grant_type": "account_credentials", "account_id": account},
            auth=("ghost", "x"),
        )
        assert unknown.status_code == 401
        assert unknown.json() == {"reason": "Invalid client_id or client_secret", "error": "invalid_client"}
        await client.post(
            f"{API}/_emulator/actions/register_app",
            json={"client_id": "real-app", "client_secret": "s3cret", "scopes": "meeting:read:admin"},
        )
        wrong = await client.post(
            f"{OAUTH}/oauth/token",
            params={"grant_type": "account_credentials", "account_id": account},
            auth=("real-app", "nope"),
        )
        assert wrong.status_code == 401 and wrong.json()["error"] == "invalid_client"
        good = await client.post(
            f"{OAUTH}/oauth/token",
            params={"grant_type": "account_credentials", "account_id": account},
            auth=("real-app", "s3cret"),
        )
        assert good.status_code == 200 and good.json()["scope"] == "meeting:read:admin"
        headers = bearer(good.json()["access_token"])
        assert (await client.get(f"{V2}/users/{HOST}/meetings", headers=headers)).status_code == 200
        assert (await client.get(f"{V2}/users/{HOST}/recordings", headers=headers)).status_code == 401
