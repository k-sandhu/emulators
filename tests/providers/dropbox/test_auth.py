"""OAuth 2, tokens, scopes and auth-related errors."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

AUTHORIZE = "https://www.dropbox.com/oauth2/authorize"
TOKEN = "https://api.dropboxapi.com/oauth2/token"
REDIRECT = "https://app.example/callback"


def pkce() -> tuple[str, str]:
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def authorize(dbx: Any, **params: str) -> Any:
    base = {"client_id": "halcyon-app", "response_type": "code", "redirect_uri": REDIRECT, "state": "xyz"}
    return await dbx.client.get(AUTHORIZE, params={**base, **params}, follow_redirects=False)


async def test_authorization_code_flow_with_pkce_and_refresh(dbx: Any) -> None:
    verifier, challenge = pkce()
    r = await authorize(
        dbx,
        login_hint=dbx.MAYA,
        token_access_type="offline",
        code_challenge=challenge,
        code_challenge_method="S256",
        scope="account_info.read files.metadata.read",
    )
    assert r.status_code == 302
    location = urlparse(r.headers["location"])
    query = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT and query["state"] == ["xyz"]
    code = query["code"][0]
    bad = await dbx.client.post(
        TOKEN,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "halcyon-app",
            "code_verifier": "wrong",
        },
    )
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    # the failed attempt must not burn... a wrong verifier does consume nothing: retry with the right one
    r = await authorize(
        dbx,
        login_hint=dbx.MAYA,
        token_access_type="offline",
        code_challenge=challenge,
        code_challenge_method="S256",
        scope="account_info.read files.metadata.read",
    )
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    ok = await dbx.client.post(
        TOKEN,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "halcyon-app",
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 14400
    assert body["access_token"].startswith("sl.") and body["refresh_token"]
    assert body["scope"] == "account_info.read files.metadata.read"
    assert (
        body["account_id"].startswith("dbid:")
        and body["team_id"].startswith("dbtid:")
        and body["uid"].isdigit()
    )
    me = await dbx.ok(body["access_token"], "users/get_current_account")
    assert me["email"] == dbx.MAYA
    # a code is single-use
    again = await dbx.client.post(
        TOKEN,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "halcyon-app",
            "code_verifier": verifier,
        },
    )
    assert again.status_code == 400
    # scope enforcement: files.content.read was not granted
    r = await dbx.upload(body["access_token"], "files/upload", {"path": "/x.txt"}, b"x")
    assert r.status_code == 401
    assert r.json()["error"] == {".tag": "missing_scope", "required_scope": "files.content.write"}
    # refresh
    refreshed = await dbx.client.post(
        TOKEN,
        data={
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": "halcyon-app",
        },
    )
    assert refreshed.status_code == 200
    assert (
        "refresh_token" not in refreshed.json() and refreshed.json()["access_token"] != body["access_token"]
    )
    await dbx.ok(refreshed.json()["access_token"], "users/get_current_account")
    nope = await dbx.client.post(
        TOKEN, data={"grant_type": "refresh_token", "refresh_token": "nope", "client_id": "halcyon-app"}
    )
    assert nope.status_code == 400 and nope.json()["error"] == "invalid_grant"
    # revoking the access token also kills its refresh token
    await dbx.ok(body["access_token"], "auth/token/revoke")
    revoked = await dbx.err(body["access_token"], "users/get_current_account", status=401)
    assert revoked["error"][".tag"] == "invalid_access_token"
    dead = await dbx.client.post(
        TOKEN,
        data={
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": "halcyon-app",
        },
    )
    assert dead.status_code == 400


async def test_online_flow_has_no_refresh_token_and_basic_client_auth(dbx: Any) -> None:
    r = await authorize(dbx, login_hint=dbx.MAYA)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    basic = base64.b64encode(b"halcyon-app:s3cret").decode()
    ok = await dbx.client.post(
        TOKEN,
        data={"grant_type": "authorization_code", "code": code},
        headers={"Authorization": f"Basic {basic}"},
    )
    assert ok.status_code == 200 and "refresh_token" not in ok.json()
    assert "files.content.read" in ok.json()["scope"]  # default scopes when none requested
    # api.dropbox.com serves the same endpoint
    r = await authorize(dbx, login_hint=dbx.MAYA)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    other = await dbx.client.post(
        "https://api.dropbox.com/oauth2/token",
        data={"grant_type": "authorization_code", "code": code, "client_id": "halcyon-app"},
    )
    assert other.status_code == 200
    unsupported = await dbx.client.post(TOKEN, data={"grant_type": "password", "client_id": "x"})
    assert unsupported.status_code == 400 and unsupported.json()["error"] == "unsupported_grant_type"
    missing = await dbx.client.post(TOKEN, data={"client_id": "x"})
    assert missing.json()["error"] == "invalid_request"


async def test_authorize_without_login_hint_shows_picker_and_no_redirect_shows_code(dbx: Any) -> None:
    page = await dbx.client.get(
        AUTHORIZE, params={"client_id": "app", "response_type": "code", "redirect_uri": REDIRECT}
    )
    assert page.status_code == 200 and "Maya Chen" in page.text and "login_hint=" in page.text
    code_page = await dbx.client.get(
        AUTHORIZE, params={"client_id": "app", "response_type": "code", "login_hint": dbx.MAYA}
    )
    assert code_page.status_code == 200 and "id='code'" in code_page.text
    code = code_page.text.split("id='code'>")[1].split("<")[0]
    ok = await dbx.client.post(
        TOKEN, data={"grant_type": "authorization_code", "code": code, "client_id": "app"}
    )
    assert ok.status_code == 200
    missing = await dbx.client.get(AUTHORIZE, params={"response_type": "code"})
    assert missing.status_code == 400
    wrong = await authorize(dbx, login_hint=dbx.MAYA, response_type="bogus")
    assert wrong.status_code == 302 and "error=unsupported_response_type" in wrong.headers["location"]


async def test_implicit_grant_returns_token_in_fragment(dbx: Any) -> None:
    r = await authorize(dbx, login_hint=dbx.MAYA, response_type="token")
    fragment = parse_qs(urlparse(r.headers["location"]).fragment)
    assert fragment["state"] == ["xyz"] and fragment["token_type"] == ["bearer"]
    await dbx.ok(fragment["access_token"][0], "users/get_current_account")


async def test_team_scopes_yield_a_team_token(dbx: Any) -> None:
    r = await authorize(
        dbx,
        client_id="team-app",
        login_hint=dbx.ARJUN,
        scope="members.read team_info.read team_data.member account_info.read",
    )
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    body = (
        await dbx.client.post(
            TOKEN, data={"grant_type": "authorization_code", "code": code, "client_id": "team-app"}
        )
    ).json()
    info = await dbx.ok(body["access_token"], "team/get_info")
    assert info["name"] == "Halcyon Advisory Group"
    maya_id = await dbx.member_id(dbx.MAYA)
    me = await dbx.ok(body["access_token"], "users/get_current_account", select_user=maya_id)
    assert me["email"] == dbx.MAYA
    listed = await dbx.ok(body["access_token"], "team/members/list_v2", {"limit": 1})
    assert listed["members"]
    groups = await dbx.err(body["access_token"], "team/groups/list", {}, status=401)
    assert groups["error"] == {".tag": "missing_scope", "required_scope": "groups.read"}


async def test_missing_invalid_expired_and_revoked_tokens(dbx: Any, hub: Any) -> None:
    r = await dbx.client.post(f"{dbx.API}/2/users/get_current_account")
    assert r.status_code == 400 and 'Must provide HTTP header "Authorization"' in r.text
    r = await dbx.client.post(
        f"{dbx.API}/2/users/get_current_account", headers={"Authorization": "Token abc"}
    )
    assert r.status_code == 400
    r = await dbx.client.post(
        f"{dbx.API}/2/users/get_current_account", headers={"Authorization": "Bearer nonsense"}
    )
    assert r.status_code == 401 and r.headers["content-type"] == "application/json"
    assert r.json() == {
        "error_summary": "invalid_access_token/...",
        "error": {".tag": "invalid_access_token"},
    }
    assert "x-dropbox-request-id" in r.headers
    # tokens can also be sent as ?authorization=
    token = await dbx.token(dbx.MAYA)
    q = await dbx.client.post(
        f"{dbx.API}/2/users/get_current_account", params={"authorization": f"Bearer {token}"}
    )
    assert q.status_code == 200
    short = (
        await dbx.client.post(f"{dbx.API}/_emulator/tokens", json={"user": dbx.MAYA, "expires_in": 60})
    ).json()["access_token"]
    await dbx.ok(short, "users/get_current_account")
    hub.clock.advance(timedelta(minutes=5))
    try:
        expired = await dbx.err(short, "users/get_current_account", status=401)
        assert expired["error"][".tag"] == "expired_access_token"
    finally:
        hub.clock.advance(timedelta(minutes=-5))


async def test_scoped_minted_tokens_and_check_routes(dbx: Any) -> None:
    ro = await dbx.token(dbx.MAYA, scopes=["files.metadata.read"])
    await dbx.ok(ro, "files/list_folder", {"path": ""})
    err = await dbx.err(ro, "files/create_folder_v2", {"path": "/nope"}, status=401)
    assert err["error"] == {".tag": "missing_scope", "required_scope": "files.content.write"}
    assert err["error_summary"] == "missing_scope/..."
    echo = await dbx.ok(ro, "check/user", {"query": "foo"})
    assert echo == {"result": "foo"}
    basic = base64.b64encode(b"key:secret").decode()
    r = await dbx.client.post(
        f"{dbx.API}/2/check/app",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/json"},
        content='{"query": "bar"}',
    )
    assert r.status_code == 200 and r.json() == {"result": "bar"}
    bad = await dbx.client.post(f"{dbx.API}/2/check/user", headers={"Authorization": f"Basic {basic}"})
    assert bad.status_code == 401


async def test_request_validation_messages_and_content_types(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    h = dbx.headers(token)
    r = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder", headers={**h, "Content-Type": "text/plain"}, content='{"path": ""}'
    )
    assert r.status_code == 400 and 'Bad HTTP "Content-Type" header' in r.text
    r = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder",
        headers={**h, "Content-Type": "application/json"},
        content="not json",
    )
    assert r.status_code == 400 and "could not decode input as JSON" in r.text
    r = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder",
        headers={**h, "Content-Type": "application/json"},
        content='{"path": ""}',
    )
    assert r.status_code == 200
    r = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder",
        headers={**h, "Content-Type": "application/json"},
        content='{"path": "", "limit": 5000}',
    )
    assert r.status_code == 400 and "limit" in r.text
    r = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder",
        headers={**h, "Content-Type": "application/json"},
        content='{"path": "", "recursive": "yes"}',
    )
    assert r.status_code == 400 and "recursive" in r.text
    r = await dbx.client.post(f"{dbx.API}/2/files/nope", headers=h)
    assert r.status_code == 404
    arg = dbx.client.build_request(
        "POST", f"{dbx.CONTENT}/2/files/download", headers=h, params={"arg": json.dumps({"path": "/missing"})}
    )
    resp = await dbx.client.send(arg)
    assert resp.status_code == 409
    r = await dbx.client.get(
        f"{dbx.API}/2/users/get_current_account", params={"authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    r = await dbx.client.post(
        f"{dbx.CONTENT}/2/files/upload",
        headers={**h, "Content-Type": "application/octet-stream"},
        content=b"x",
    )
    assert r.status_code == 400 and "Dropbox-API-Arg" in r.text


async def test_fault_injection_is_dropbox_shaped(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    await dbx.client.post(
        f"{dbx.API}/_emulator/faults", json={"path": "/2/files/list_folder$", "status": 429, "times": 1}
    )
    r = await dbx.rpc(token, "files/list_folder", {"path": ""})
    assert r.status_code == 429 and r.headers["retry-after"] == "1"
    assert r.json()["error"] == {"reason": {".tag": "too_many_requests"}, "retry_after": 1}
    await dbx.client.post(
        f"{dbx.API}/_emulator/faults", json={"path": "/2/files/list_folder$", "status": 503, "times": 1}
    )
    r = await dbx.rpc(token, "files/list_folder", {"path": ""})
    assert r.status_code == 503 and r.headers["content-type"].startswith("text/plain")
    await dbx.client.post(
        f"{dbx.API}/_emulator/faults", json={"path": "/2/files/list_folder$", "status": 401, "times": 1}
    )
    r = await dbx.rpc(token, "files/list_folder", {"path": ""})
    assert r.status_code == 401 and r.json()["error"][".tag"] == "invalid_access_token"
    assert (await dbx.rpc(token, "files/list_folder", {"path": ""})).status_code == 200


async def test_users_routes(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    me = await dbx.ok(maya, "users/get_current_account")
    other = await dbx.ok(maya, "users/get_account", {"account_id": await dbx.account_id(dbx.ELEANOR)})
    assert (
        other["is_teammate"] is True
        and other["email"] == dbx.ELEANOR
        and other["team_member_id"].startswith("dbmid:")
    )
    batch = await dbx.ok(
        maya, "users/get_account_batch", {"account_ids": [me["account_id"], other["account_id"]]}
    )
    assert [b["email"] for b in batch] == [dbx.MAYA, dbx.ELEANOR]
    missing = await dbx.err(maya, "users/get_account", {"account_id": "dbid:" + "Z" * 35})
    assert missing["error"] == {".tag": "no_account"}
    usage = await dbx.ok(maya, "users/get_space_usage")
    assert usage["used"] > 0 and usage["allocation"][".tag"] == "team"
    feats = await dbx.ok(
        maya,
        "users/features/get_values",
        {"features": [{".tag": "file_locking"}, {".tag": "paper_as_files"}]},
    )
    assert len(feats["values"]) == 2
    empty = await dbx.err(maya, "users/features/get_values", {"features": []})
    assert empty["error"][".tag"] == "empty_features_list"
    # a client contact is a non-team basic account
    chris = await dbx.token(dbx.CHRISTOPHER)
    acct = await dbx.ok(chris, "users/get_current_account")
    assert (
        acct["account_type"] == {".tag": "basic"}
        and "team" not in acct
        and acct["root_info"][".tag"] == "user"
    )
    as_other = await dbx.ok(chris, "users/get_account", {"account_id": me["account_id"]})
    assert as_other["is_teammate"] is False and "team_member_id" not in as_other
    usage = await dbx.ok(chris, "users/get_space_usage")
    assert usage["allocation"][".tag"] == "individual"


async def test_cors_preflight_and_headers(dbx: Any) -> None:
    pre = await dbx.client.options(
        f"{dbx.API}/2/files/list_folder",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "*"
    assert "authorization" in pre.headers["access-control-allow-headers"]
    token = await dbx.token(dbx.MAYA)
    # the browser SDK posts JSON as text/plain; charset=dropbox-cors-hack
    r = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/plain; charset=dropbox-cors-hack",
            "Origin": "https://app.example",
        },
        content='{"path": ""}',
    )
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    err = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder", headers={"Authorization": "Bearer nope"}, content="{}"
    )
    assert err.status_code == 401 and err.headers["access-control-allow-origin"] == "*"
