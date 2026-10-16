"""OAuth 2.0 flows, token errors, As-User and downscoped tokens."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import jwt

TOKEN_URL = "https://api.box.com/oauth2/token"
AUTHORIZE_URL = "https://account.box.com/api/oauth2/authorize"
ENTERPRISE_ID = "336533359"
CLIENT = {"client_id": "my-box-app", "client_secret": "whatever"}


async def authorize(box, **params):
    query = {
        "response_type": "code",
        "client_id": CLIENT["client_id"],
        "redirect_uri": "https://app.example.test/callback",
        "state": "xyz",
        **params,
    }
    return await box.client.get(AUTHORIZE_URL, params=query)


async def login(box, email="maya.chen@halcyonadvisory.example") -> dict:
    r = await authorize(box, login_hint=email)
    assert r.status_code == 302, r.text
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query["state"] == ["xyz"]
    token = await box.client.post(
        TOKEN_URL,
        data={"grant_type": "authorization_code", "code": query["code"][0], **CLIENT},
    )
    assert token.status_code == 200, token.text
    return token.json()


async def test_authorization_code_flow_returns_box_token_shape(box):
    tokens = await login(box)
    assert re.fullmatch(r"[A-Za-z0-9]{32}", tokens["access_token"])
    assert re.fullmatch(r"[A-Za-z0-9]{64}", tokens["refresh_token"])
    assert tokens["token_type"] == "bearer"
    assert tokens["restricted_to"] == []
    assert 3000 <= tokens["expires_in"] <= 4200
    me = await box.get("/users/me", tokens["access_token"])
    assert me.status_code == 200
    assert me.json()["login"] == "maya.chen@halcyonadvisory.example"


async def test_authorize_without_login_hint_renders_account_picker(box):
    r = await authorize(box)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "maya.chen@halcyonadvisory.example" in r.text


async def test_authorize_errors(box):
    bad_type = await authorize(box, response_type="token", login_hint="maya.chen@halcyonadvisory.example")
    assert bad_type.status_code == 302
    assert parse_qs(urlsplit(bad_type.headers["location"]).query)["error"] == ["unsupported_response_type"]
    unknown = await authorize(box, login_hint="nobody@halcyonadvisory.example")
    assert parse_qs(urlsplit(unknown.headers["location"]).query)["error"] == ["access_denied"]
    missing = await box.client.get(AUTHORIZE_URL, params={"response_type": "code"})
    assert missing.status_code == 400


async def test_authorization_code_is_single_use_and_bound_to_client(box):
    r = await authorize(box, login_hint="maya.chen@halcyonadvisory.example")
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    first = await box.client.post(
        TOKEN_URL, data={"grant_type": "authorization_code", "code": code, **CLIENT}
    )
    assert first.status_code == 200
    again = await box.client.post(
        TOKEN_URL, data={"grant_type": "authorization_code", "code": code, **CLIENT}
    )
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"
    assert "error_description" in again.json()


async def test_refresh_tokens_rotate_and_are_single_use(box):
    tokens = await login(box)
    refreshed = await box.client.post(
        TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], **CLIENT}
    )
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["refresh_token"] != tokens["refresh_token"]
    assert body["access_token"] != tokens["access_token"]
    # The old refresh token is dead, and so is the access token it was paired with.
    reuse = await box.client.post(
        TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], **CLIENT}
    )
    assert reuse.status_code == 400
    assert reuse.json() == {"error": "invalid_grant", "error_description": "Refresh token has expired"}
    assert (await box.get("/users/me", tokens["access_token"])).status_code == 401
    assert (await box.get("/users/me", body["access_token"])).status_code == 200


async def test_client_credentials_enterprise_uses_the_service_account(box):
    r = await box.client.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "box_subject_type": "enterprise",
            "box_subject_id": ENTERPRISE_ID,
            **CLIENT,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "refresh_token" not in body
    me = (await box.get("/users/me", body["access_token"])).json()
    assert me["name"] == "Halcyon Box Automation"
    assert re.fullmatch(r"AutomationUser_\d+_\w+@boxdevedition\.com", me["login"])
    assert me["enterprise"]["id"] == ENTERPRISE_ID


async def test_client_credentials_for_a_user_and_bad_subjects(box):
    users = (await box.get("/users", params={"filter_term": "Maya Chen"})).json()["entries"]
    maya = users[0]["id"]
    r = await box.client.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "box_subject_type": "user",
            "box_subject_id": maya,
            **CLIENT,
        },
    )
    assert r.status_code == 200
    assert (await box.get("/users/me", r.json()["access_token"])).json()["id"] == maya
    wrong_enterprise = await box.client.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "box_subject_type": "enterprise",
            "box_subject_id": "1",
            **CLIENT,
        },
    )
    assert wrong_enterprise.status_code == 400
    assert wrong_enterprise.json()["error"] == "invalid_grant"
    no_user = await box.client.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "box_subject_type": "user",
            "box_subject_id": "42",
            **CLIENT,
        },
    )
    assert no_user.json()["error"] == "invalid_grant"


def make_assertion(sub: str, sub_type: str = "enterprise", **claims) -> str:
    payload = {
        "iss": CLIENT["client_id"],
        "sub": sub,
        "box_sub_type": sub_type,
        "aud": TOKEN_URL,
        "jti": "abc123",
        "exp": int((datetime.now(UTC) + timedelta(seconds=30)).timestamp()),
        **claims,
    }
    return jwt.encode(payload, "client-private-key-0123456789abcdef", algorithm="HS256")


async def test_jwt_bearer_grant(box):
    grant = "urn:ietf:params:oauth:grant-type:jwt-bearer"
    ok = await box.client.post(
        TOKEN_URL, data={"grant_type": grant, "assertion": make_assertion(ENTERPRISE_ID), **CLIENT}
    )
    assert ok.status_code == 200, ok.text
    assert (await box.get("/users/me", ok.json()["access_token"])).json()["name"] == "Halcyon Box Automation"
    users = (await box.get("/users", params={"filter_term": "Maya"})).json()["entries"]
    as_user = await box.client.post(
        TOKEN_URL,
        data={"grant_type": grant, "assertion": make_assertion(users[0]["id"], "user"), **CLIENT},
    )
    assert (await box.get("/users/me", as_user.json()["access_token"])).json()["id"] == users[0]["id"]
    bad = await box.client.post(TOKEN_URL, data={"grant_type": grant, "assertion": "not-a-jwt", **CLIENT})
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_grant"
    missing = jwt.encode({"iss": "x", "sub": "1"}, "k" * 32, algorithm="HS256")
    assert (
        await box.client.post(TOKEN_URL, data={"grant_type": grant, "assertion": missing, **CLIENT})
    ).status_code == 400


async def test_token_endpoint_rejects_unknown_grant_types(box):
    r = await box.client.post(TOKEN_URL, data={"grant_type": "password", **CLIENT})
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"
    assert (await box.client.post(TOKEN_URL, data=CLIENT)).json()["error"] == "invalid_request"


async def test_revoke_invalidates_the_access_token(box):
    tokens = await login(box)
    r = await box.client.post(
        "https://api.box.com/oauth2/revoke", data={"token": tokens["access_token"], **CLIENT}
    )
    assert r.status_code == 200
    after = await box.get("/users/me", tokens["access_token"])
    assert after.status_code == 401


async def test_unauthorized_errors_use_the_box_envelope(box):
    missing = await box.client.get("https://api.box.com/2.0/users/me")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith('Bearer realm="Service"')
    body = missing.json()
    assert body["type"] == "error"
    assert body["status"] == 401
    assert body["code"] == "unauthorized"
    assert body["help_url"] == "http://developers.box.com/docs/#errors"
    assert body["request_id"]
    invalid = await box.get("/users/me", "box-bogus-token-value")
    assert invalid.status_code == 401
    assert 'error="invalid_token"' in invalid.headers["www-authenticate"]


async def test_expired_tokens_are_rejected(box):
    minted = await box.action("mint_token", {"user": "p018", "expires_in": 60})
    assert (await box.get("/users/me", minted["access_token"])).status_code == 200
    box.hub.clock.advance(timedelta(seconds=120))
    expired = await box.get("/users/me", minted["access_token"])
    assert expired.status_code == 401
    assert 'error="invalid_token"' in expired.headers["www-authenticate"]


async def test_dev_tokens_and_control_plane_minting(box):
    admin = await box.get("/users/me", "p035")
    assert admin.json()["role"] == "admin"
    coadmin = await box.get("/users/me", "p036")
    assert coadmin.json()["role"] == "coadmin"
    assert (await box.get("/users/me", "p018")).json()["role"] == "user"
    minted = await box.client.post(
        "https://api.box.com/_emulator/tokens", json={"user": "maya.chen@halcyonadvisory.example"}
    )
    assert minted.status_code == 200
    assert (await box.get("/users/me", minted.json()["access_token"])).json()["name"] == "Maya Chen"
    service = await box.client.post("https://api.box.com/_emulator/tokens", json={"app_only": True})
    assert (await box.get("/users/me", service.json()["access_token"])).json()[
        "name"
    ] == "Halcyon Box Automation"


async def test_as_user_requires_an_admin_or_service_account(box):
    maya = (await box.get("/users/me", "p018")).json()["id"]
    as_user = {"As-User": maya}
    for who in ("p035", "p036", "enterprise"):
        r = await box.get("/users/me", who, headers=as_user)
        assert r.status_code == 200, (who, r.text)
        assert r.json()["id"] == maya
    denied = await box.get("/users/me", "p019", headers=as_user)
    assert denied.status_code == 403
    assert denied.json()["code"] == "access_denied_insufficient_permissions"
    unknown = await box.get("/users/me", "p035", headers={"As-User": "999"})
    assert unknown.status_code == 404


async def test_downscoped_token_exchange_limits_access_to_the_resource(box):
    designated = await box.find("Client Engagements/Northwind Logistics/FY2025 Review/Deliverables", "p004")
    files = await box.items(designated, "p004")
    target, other = files[0], files[1]
    parent = await box.get("/users/me", "p004")
    assert parent.status_code == 200
    base_token = (await box.action("mint_token", {"user": "p004"}))["access_token"]
    exchange = await box.client.post(
        TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": base_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "scope": "item_preview",
            "resource": f"https://api.box.com/2.0/files/{target['id']}",
            **CLIENT,
        },
    )
    assert exchange.status_code == 200, exchange.text
    body = exchange.json()
    assert body["issued_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert body["restricted_to"][0]["scope"] == "item_preview"
    assert body["restricted_to"][0]["object"]["id"] == target["id"]
    scoped = body["access_token"]
    assert (await box.get(f"/files/{target['id']}", scoped)).status_code == 200
    assert (await box.get(f"/files/{other['id']}", scoped)).status_code == 404
    assert (await box.get(f"/files/{target['id']}/content", scoped)).status_code == 403
    assert (await box.put(f"/files/{target['id']}", scoped, json={"description": "x"})).status_code == 403
    invalid = await box.client.post(
        TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": base_token,
            "scope": "not_a_scope",
            **CLIENT,
        },
    )
    assert invalid.json()["error"] == "invalid_scope"


async def test_sign_in_activity_is_only_visible_in_admin_logs(box):
    start = (await box.get("/events", params={"stream_type": "admin_logs", "stream_position": "now"})).json()
    tokens = await login(box)
    refreshed = await box.client.post(
        TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], **CLIENT}
    )
    await box.client.post(
        "https://api.box.com/oauth2/revoke", data={"token": refreshed.json()["access_token"], **CLIENT}
    )
    logs = (
        await box.get(
            "/events", params={"stream_type": "admin_logs", "stream_position": start["next_stream_position"]}
        )
    ).json()
    kinds = [e["event_type"] for e in logs["entries"]]
    assert kinds == ["LOGIN", "USER_AUTHENTICATE_OAUTH2_TOKEN_REFRESH", "OAUTH2_ACCESS_TOKEN_REVOKE"]
    assert logs["entries"][0]["created_by"]["login"] == "maya.chen@halcyonadvisory.example"
    assert logs["entries"][0]["source"]["type"] == "user"
    mine = (await box.get("/events", "p018", params={"stream_position": "0", "limit": 500})).json()
    assert not {"LOGIN", "OAUTH2_ACCESS_TOKEN_REVOKE"} & {e["event_type"] for e in mine["entries"]}


async def test_enterprise_token_sees_the_team_folders_it_co_owns(box):
    token = (await box.action("mint_token", {"user": "enterprise"}))["access_token"]
    names = {e["name"] for e in await box.items("0", token)}
    assert {"Client Engagements", "Finance & Operations", "Partner Group"} <= names
    me = (await box.get("/users/me", token)).json()
    clients = next(e["id"] for e in await box.items("0", token) if e["name"] == "Client Engagements")
    folder = (await box.get(f"/folders/{clients}", token, params={"fields": "permissions,owned_by"})).json()
    assert folder["permissions"]["can_upload"] is True and folder["owned_by"]["login"].startswith(
        "arjun.mehta"
    )
    assert me["name"] == "Halcyon Box Automation"
    # ... but not the employees' personal folders: that needs As-User.
    assert "Notes" not in names
    maya = (await box.get("/users/me", "p018")).json()["id"]
    acting = await box.get("/folders/0/items", token, headers={"As-User": maya})
    assert "Notes" in {e["name"] for e in acting.json()["entries"]}
