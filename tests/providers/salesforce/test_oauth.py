"""OAuth 2.0 / OIDC, SOAP login and session handling."""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from .conftest import BASE, LOGIN, PASSWORD, TOKEN, Actor, soap_login_xml, username

TOKEN_URL = f"{LOGIN}/services/oauth2/token"
ARJUN_LOGIN = username("arjun.mehta")


async def password_grant(
    client: httpx.AsyncClient, user: str = ARJUN_LOGIN, password: str = PASSWORD + TOKEN
):
    return await client.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "3MVG9-emulator-key",
            "client_secret": "secret",
            "username": user,
            "password": password,
        },
    )


async def test_password_grant_returns_salesforce_token_shape(client: httpx.AsyncClient) -> None:
    r = await password_grant(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert list(body) == [
        "access_token",
        "signature",
        "scope",
        "instance_url",
        "id",
        "token_type",
        "issued_at",
    ]
    assert body["access_token"].startswith("00D5g0000049673!")
    assert body["instance_url"] == "https://halcyonadvisory.my.salesforce.com"
    assert body["id"].startswith("https://login.salesforce.com/id/00D5g0000049673EAA/005")
    assert body["token_type"] == "Bearer"
    assert body["issued_at"].isdigit() and len(body["issued_at"]) == 13
    expected = base64.b64encode(
        __import__("hmac").new(b"secret", (body["id"] + body["issued_at"]).encode(), hashlib.sha256).digest()
    ).decode()
    assert body["signature"] == expected


async def test_password_without_security_token_is_accepted_and_bad_password_is_not(
    client: httpx.AsyncClient,
) -> None:
    assert (await password_grant(client, password=PASSWORD)).status_code == 200
    r = await password_grant(client, password="wrong")
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_grant", "error_description": "authentication failure"}
    assert (await password_grant(client, user="nobody@example.crm")).status_code == 400


async def test_departed_employee_cannot_log_in(client: httpx.AsyncClient) -> None:
    r = await password_grant(client, user=username("paul.henderson"))
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


async def test_missing_client_and_unknown_grant(client: httpx.AsyncClient) -> None:
    r = await client.post(
        TOKEN_URL, data={"grant_type": "password", "username": ARJUN_LOGIN, "password": PASSWORD}
    )
    assert r.json() == {"error": "invalid_client_id", "error_description": "client identifier invalid"}
    r = await client.post(TOKEN_URL, data={"grant_type": "telepathy", "client_id": "x"})
    assert r.json()["error"] == "unsupported_grant_type"
    r = await client.post(TOKEN_URL, data={})
    assert r.status_code == 400


async def test_issued_token_works_and_bad_tokens_get_invalid_session(client: httpx.AsyncClient) -> None:
    token = (await password_grant(client)).json()["access_token"]
    ok = await client.get(f"{BASE}/services/data/v62.0/limits", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    # the legacy "OAuth" scheme is accepted too
    legacy = await client.get(
        f"{BASE}/services/data/v62.0/limits", headers={"Authorization": f"OAuth {token}"}
    )
    assert legacy.status_code == 200
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}):
        r = await client.get(f"{BASE}/services/data/v62.0/limits", headers=headers)
        assert r.status_code == 401
        assert r.json() == [{"message": "Session expired or invalid", "errorCode": "INVALID_SESSION_ID"}]


async def test_session_expires_after_two_hours(client: httpx.AsyncClient) -> None:
    token = (await password_grant(client)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.hub.clock.advance(timedelta(hours=1, minutes=59))  # type: ignore[attr-defined]
    assert (await client.get(f"{BASE}/services/data/v62.0/limits", headers=headers)).status_code == 200
    client.hub.clock.advance(timedelta(minutes=2))  # type: ignore[attr-defined]
    r = await client.get(f"{BASE}/services/data/v62.0/limits", headers=headers)
    assert r.status_code == 401 and r.json()[0]["errorCode"] == "INVALID_SESSION_ID"


async def test_client_credentials_runs_as_integration_user(client: httpx.AsyncClient) -> None:
    basic = base64.b64encode(b"consumer-key:consumer-secret").decode()
    r = await client.post(
        f"{BASE}/services/oauth2/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {basic}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "refresh_token" not in body
    info = await client.get(
        f"{BASE}/services/oauth2/userinfo", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert info.json()["preferred_username"] == "integration@halcyonadvisory.example.crm"
    assert info.json()["is_salesforce_integration_user"] is True


def make_assertion(private_key: str, **claims: object) -> str:
    payload = {
        "iss": "3MVG9-jwt-key",
        "sub": ARJUN_LOGIN,
        "aud": "https://login.salesforce.com",
        "exp": 4_000_000_000,
        **claims,
    }
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, private_key, algorithm="RS256")


async def test_jwt_bearer_flow(client: httpx.AsyncClient) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.PEM,
        format=__import__("cryptography").hazmat.primitives.serialization.PrivateFormat.PKCS8,
        encryption_algorithm=__import__("cryptography").hazmat.primitives.serialization.NoEncryption(),
    ).decode()
    grant = "urn:ietf:params:oauth:grant-type:jwt-bearer"
    r = await client.post(TOKEN_URL, data={"grant_type": grant, "assertion": make_assertion(pem)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"access_token", "scope", "instance_url", "id", "token_type"}
    me = await client.get(
        f"{BASE}/services/oauth2/userinfo", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.json()["preferred_username"] == ARJUN_LOGIN
    bad_aud = await client.post(
        TOKEN_URL, data={"grant_type": grant, "assertion": make_assertion(pem, aud="https://evil.example")}
    )
    assert bad_aud.json() == {"error": "invalid_grant", "error_description": "audience is invalid"}
    bad_user = await client.post(
        TOKEN_URL, data={"grant_type": grant, "assertion": make_assertion(pem, sub="ghost@x.crm")}
    )
    assert bad_user.json()["error"] == "invalid_grant"
    expired = await client.post(
        TOKEN_URL, data={"grant_type": grant, "assertion": make_assertion(pem, exp=1000)}
    )
    assert expired.json()["error"] == "invalid_grant"
    garbage = await client.post(TOKEN_URL, data={"grant_type": grant, "assertion": "not-a-jwt"})
    assert garbage.json()["error"] == "invalid_grant"


def pkce_pair() -> tuple[str, str]:
    verifier = "v" * 50
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def authorize(client: httpx.AsyncClient, **extra: str) -> httpx.Response:
    params = {
        "response_type": "code",
        "client_id": "3MVG9-web",
        "redirect_uri": "https://consumer.example/callback",
        "state": "st-1",
        "login_hint": ARJUN_LOGIN,
        **extra,
    }
    return await client.get(f"{LOGIN}/services/oauth2/authorize", params=params, follow_redirects=False)


async def test_web_server_flow_with_pkce_refresh_revoke_and_introspect(client: httpx.AsyncClient) -> None:
    verifier, challenge = pkce_pair()
    r = await authorize(
        client,
        scope="api refresh_token openid",
        code_challenge=challenge,
        code_challenge_method="S256",
        nonce="n-1",
    )
    assert r.status_code == 302
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query["state"] == ["st-1"] and query["code"][0].startswith("aPrx")
    form = {
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "client_id": "3MVG9-web",
        "client_secret": "s",
        "redirect_uri": "https://consumer.example/callback",
    }
    wrong = await client.post(TOKEN_URL, data={**form, "code_verifier": "x" * 50})
    assert wrong.json() == {"error": "invalid_grant", "error_description": "invalid code verifier"}
    r = await authorize(
        client,
        scope="api refresh_token openid",
        code_challenge=challenge,
        code_challenge_method="S256",
        nonce="n-1",
    )
    form["code"] = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    tokens = await client.post(TOKEN_URL, data={**form, "code_verifier": verifier})
    assert tokens.status_code == 200, tokens.text
    body = tokens.json()
    assert body["refresh_token"].startswith("5Aep861")
    assert body["scope"] == "refresh_token api openid"
    # the code is single use
    again = await client.post(TOKEN_URL, data={**form, "code_verifier": verifier})
    assert again.json() == {"error": "invalid_grant", "error_description": "expired authorization code"}
    bad_redirect = await client.post(TOKEN_URL, data={**form, "redirect_uri": "https://other.example/cb"})
    assert bad_redirect.json()["error"] == "invalid_grant"
    # id_token verifies against the published JWKS and carries the nonce
    jwks = (await client.get(f"{LOGIN}/id/keys")).json()
    key = jwt.PyJWK(jwks["keys"][0]).key
    claims = jwt.decode(
        body["id_token"],
        key,
        algorithms=["RS256"],
        audience="3MVG9-web",
        issuer="https://login.salesforce.com",
        options={"verify_exp": False, "verify_iat": False},  # the hub clock is frozen, not real time
    )
    assert claims["nonce"] == "n-1" and claims["sub"] == body["id"]
    # refresh
    refreshed = await client.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": "3MVG9-web",
            "client_secret": "s",
        },
    )
    assert refreshed.status_code == 200
    new_token = refreshed.json()["access_token"]
    assert new_token != body["access_token"] and "refresh_token" not in refreshed.json()
    # introspect
    intro = await client.post(
        f"{BASE}/services/oauth2/introspect",
        data={"token": new_token, "token_type_hint": "access_token", "client_id": "3MVG9-web"},
    )
    assert intro.json()["active"] is True and intro.json()["username"] == ARJUN_LOGIN
    # revoking the refresh token also kills the access tokens minted from it
    revoke = await client.post(f"{BASE}/services/oauth2/revoke", data={"token": body["refresh_token"]})
    assert revoke.status_code == 200
    for dead in (body["access_token"], new_token):
        r = await client.get(
            f"{BASE}/services/data/v62.0/limits", headers={"Authorization": f"Bearer {dead}"}
        )
        assert r.status_code == 401
    after = await client.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": "3MVG9-web",
        },
    )
    assert after.json() == {"error": "invalid_grant", "error_description": "expired access/refresh token"}
    inactive = await client.post(f"{BASE}/services/oauth2/introspect", data={"token": new_token})
    assert inactive.json() == {"active": False}


async def test_authorize_picker_unknown_user_and_implicit_flow(client: httpx.AsyncClient) -> None:
    picker = await authorize(client, login_hint="")
    assert picker.status_code == 200 and "text/html" in picker.headers["content-type"]
    assert (
        "arjun.mehta%40halcyonadvisory.example.crm" in picker.text and "Integration User" not in picker.text
    )
    assert "Paul Henderson" not in picker.text  # departed employees cannot sign in
    denied = await authorize(client, login_hint="ghost@x.crm")
    assert denied.status_code == 302
    assert parse_qs(urlsplit(denied.headers["location"]).query)["error"] == ["access_denied"]
    implicit = await authorize(client, response_type="token")
    fragment = parse_qs(urlsplit(implicit.headers["location"]).fragment)
    assert fragment["access_token"][0].startswith("00D5g0000049673!")
    assert fragment["instance_url"] == ["https://halcyonadvisory.my.salesforce.com"]
    missing = await client.get(f"{LOGIN}/services/oauth2/authorize", params={"response_type": "code"})
    assert missing.status_code == 400 and missing.json()["error"] == "invalid_request"


async def test_userinfo_identity_and_discovery(client: httpx.AsyncClient, admin: Actor) -> None:
    info = await client.get(f"{BASE}/services/oauth2/userinfo", headers=admin.headers)
    body = info.json()
    assert body["user_id"] == admin.user_id and body["organization_id"] == "00D5g0000049673EAA"
    assert body["name"] == "Arjun Mehta" and body["email"] == "arjun.mehta@halcyonadvisory.example"
    assert body["zoneinfo"] == "America/Toronto" and body["utcOffset"] == -4 * 3600 * 1000
    assert body["sub"] == f"https://login.salesforce.com/id/00D5g0000049673EAA/{admin.user_id}"
    ident = await client.get(body["sub"], headers=admin.headers)
    assert ident.status_code == 200
    assert ident.json()["username"] == ARJUN_LOGIN
    assert ident.json()["urls"]["rest"].endswith("/services/data/v{version}/")
    assert (await client.get(body["sub"])).status_code == 401
    wrong_org = await client.get(f"{LOGIN}/id/00Dxxxxxxxxxxxxxxx/{admin.user_id}", headers=admin.headers)
    assert wrong_org.status_code == 404
    conf = (await client.get(f"{LOGIN}/.well-known/openid-configuration")).json()
    assert conf["issuer"] == "https://login.salesforce.com"
    assert conf["token_endpoint"] == "https://login.salesforce.com/services/oauth2/token"
    assert conf["jwks_uri"] == "https://login.salesforce.com/id/keys"


async def test_mint_token_action_accepts_world_ids_emails_and_usernames(client: httpx.AsyncClient) -> None:
    for ident in ("p018", "maya.chen@halcyonadvisory.example", "maya.chen@halcyonadvisory.example.crm"):
        r = await client.post(f"{BASE}/_emulator/tokens", json={"user": ident})
        assert r.status_code == 200, r.text
        assert r.json()["username"] == "maya.chen@halcyonadvisory.example.crm"
        assert r.json()["access_token"].startswith("00D5g0000049673!")
    assert (await client.post(f"{BASE}/_emulator/tokens", json={"user": "ghost"})).status_code == 404
    app_only = (await client.post(f"{BASE}/_emulator/tokens", json={"app_only": True})).json()
    assert app_only["username"] == "integration@halcyonadvisory.example.crm"


async def test_soap_login_success_and_invalid_login(client: httpx.AsyncClient) -> None:
    headers = {"content-type": "text/xml", "SOAPAction": "login"}
    ok = await client.post(
        f"{LOGIN}/services/Soap/u/62.0",
        content=soap_login_xml(ARJUN_LOGIN, PASSWORD + TOKEN),
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert "text/xml" in ok.headers["content-type"]
    import xml.etree.ElementTree as ET

    root = ET.fromstring(ok.text)
    ns = {"p": "urn:partner.soap.sforce.com"}
    result = root.find(".//p:result", ns)
    assert result is not None
    assert result.findtext("p:serverUrl", namespaces=ns) == f"{BASE}/services/Soap/u/62.0/00D5g0000049673EAA"
    assert result.findtext("p:metadataServerUrl", namespaces=ns).startswith(BASE + "/services/Soap/m/62.0/")
    assert result.findtext("p:sessionId", namespaces=ns).startswith("00D5g0000049673!")
    info = result.find("p:userInfo", ns)
    assert info.findtext("p:userEmail", namespaces=ns) == "arjun.mehta@halcyonadvisory.example"
    assert info.findtext("p:userFullName", namespaces=ns) == "Arjun Mehta"
    assert info.findtext("p:organizationId", namespaces=ns) == "00D5g0000049673EAA"
    session = result.findtext("p:sessionId", namespaces=ns)
    assert (
        await client.get(f"{BASE}/services/data/v62.0/limits", headers={"Authorization": f"Bearer {session}"})
    ).status_code == 200
    bad = await client.post(
        f"{LOGIN}/services/Soap/u/62.0", content=soap_login_xml(ARJUN_LOGIN, "nope"), headers=headers
    )
    assert bad.status_code == 500
    assert "<sf:exceptionCode>INVALID_LOGIN</sf:exceptionCode>" in bad.text
    assert "Invalid username, password, security token; or user locked out." in bad.text
    malformed = await client.post(f"{LOGIN}/services/Soap/u/62.0", content="<not xml", headers=headers)
    assert malformed.status_code == 500


async def test_soap_get_user_info_and_logout(client: httpx.AsyncClient, admin: Actor) -> None:
    def envelope(op: str) -> str:
        return (
            '<?xml version="1.0"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:urn="urn:partner.soap.sforce.com"><soapenv:Header><urn:SessionHeader>'
            f"<urn:sessionId>{admin.token}</urn:sessionId></urn:SessionHeader></soapenv:Header>"
            f"<soapenv:Body><urn:{op}/></soapenv:Body></soapenv:Envelope>"
        )

    r = await client.post(
        f"{BASE}/services/Soap/u/62.0", content=envelope("getUserInfo"), headers={"content-type": "text/xml"}
    )
    assert r.status_code == 200 and "<userFullName>Arjun Mehta</userFullName>" in r.text
    r = await client.post(
        f"{BASE}/services/Soap/u/62.0", content=envelope("logout"), headers={"content-type": "text/xml"}
    )
    assert r.status_code == 200
    assert (await admin.get("/limits")).status_code == 401
    r = await client.post(
        f"{BASE}/services/Soap/u/62.0", content=envelope("getUserInfo"), headers={"content-type": "text/xml"}
    )
    assert r.status_code == 500 and "INVALID_SESSION_ID" in r.text


async def test_lenient_passwords_setting(client: httpx.AsyncClient) -> None:
    client.hub.contexts["salesforce"].settings["LENIENT_PASSWORDS"] = "1"  # type: ignore[attr-defined]
    try:
        assert (await password_grant(client, password="anything-at-all")).status_code == 200
    finally:
        client.hub.contexts["salesforce"].settings.pop("LENIENT_PASSWORDS")  # type: ignore[attr-defined]
    assert (await password_grant(client, password="anything-at-all")).status_code == 400


@pytest.mark.parametrize(
    "host", ["login.salesforce.com", "test.salesforce.com", "acme.my.salesforce.com", "x.lightning.force.com"]
)
async def test_every_documented_host_is_served(client: httpx.AsyncClient, host: str) -> None:
    r = await client.get(f"https://{host}/.well-known/openid-configuration")
    assert r.status_code == 200


async def test_enterprise_soap_login_and_bad_userinfo_token(client: httpx.AsyncClient) -> None:
    r = await client.post(
        f"{LOGIN}/services/Soap/c/62.0",
        content=soap_login_xml(ARJUN_LOGIN, PASSWORD + TOKEN).replace(
            "urn:partner.soap.sforce.com", "urn:enterprise.soap.sforce.com"
        ),
        headers={"content-type": "text/xml", "SOAPAction": "login"},
    )
    assert r.status_code == 200
    assert (
        'xmlns="urn:enterprise.soap.sforce.com"' in r.text
        and f"{BASE}/services/Soap/c/62.0/00D5g0000049673EAA" in r.text
    )
    other = await client.post(
        f"{LOGIN}/services/Soap/m/62.0", content="<x/>", headers={"content-type": "text/xml"}
    )
    assert other.status_code == 500 and "sf:UNSUPPORTED_API_VERSION" in other.text
    assert (
        await client.get(f"{BASE}/services/oauth2/userinfo", headers={"Authorization": "Bearer nope"})
    ).status_code == 401


async def test_logins_update_last_login_and_failed_login_counters(
    client: httpx.AsyncClient, admin: Actor
) -> None:
    hannah = "hannah.schmidt@halcyonadvisory.example.crm"
    before = (
        await admin.rows(f"SELECT LastLoginDate, NumberOfFailedLogins FROM User WHERE Username = '{hannah}'")
    )[0]
    assert before["NumberOfFailedLogins"] == 0 and before["LastLoginDate"] != "2026-10-16T12:00:00.000+0000"
    for _ in range(2):
        assert (await password_grant(client, user=hannah, password="nope")).status_code == 400
    soap = await client.post(
        f"{LOGIN}/services/Soap/u/62.0",
        content=soap_login_xml(hannah, "nope"),
        headers={"content-type": "text/xml"},
    )
    assert soap.status_code == 500
    mid = (await admin.rows(f"SELECT NumberOfFailedLogins FROM User WHERE Username = '{hannah}'"))[0]
    assert mid["NumberOfFailedLogins"] == 3
    assert (await password_grant(client, user=hannah, password=PASSWORD)).status_code == 200
    after = (
        await admin.rows(f"SELECT LastLoginDate, NumberOfFailedLogins FROM User WHERE Username = '{hannah}'")
    )[0]
    assert after == {
        "attributes": after["attributes"],
        "LastLoginDate": "2026-10-16T12:00:00.000+0000",
        "NumberOfFailedLogins": 0,
    }
