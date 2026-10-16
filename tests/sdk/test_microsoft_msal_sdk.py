from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import msal
import pytest
import requests

pytestmark = pytest.mark.sdk
CLIENT_ID = "00000000-0000-0000-0000-00000000c0de"
AUTHORITY = "https://login.microsoftonline.com/halcyonadvisory.example"


def test_msal_client_credentials_and_graph(emulator_intercept: Any) -> None:
    application = msal.ConfidentialClientApplication(
        CLIENT_ID, client_credential="synthetic-secret", authority=AUTHORITY
    )
    result = application.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    assert "access_token" in result, result
    users = requests.get(
        "https://graph.microsoft.com/v1.0/users",
        headers={"Authorization": f"Bearer {result['access_token']}"},
    )
    assert users.status_code == 200 and users.json()["value"], users.text


def test_msal_ropc_cached_account_and_refresh(emulator_intercept: Any) -> None:
    application = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY)
    result = application.acquire_token_by_username_password(
        "maya.chen@halcyonadvisory.example", "synthetic-password", scopes=["User.Read"]
    )
    assert "access_token" in result, result
    accounts = application.get_accounts(username="maya.chen@halcyonadvisory.example")
    assert accounts
    result = application.acquire_token_silent(["User.Read"], account=accounts[0], force_refresh=True)
    assert "access_token" in result, result
    me = requests.get(
        "https://graph.microsoft.com/v1.0/me", headers={"Authorization": f"Bearer {result['access_token']}"}
    )
    assert me.json()["mail"] == "maya.chen@halcyonadvisory.example"


def test_msal_auth_code_flow_pkce_and_nonce(emulator_intercept: Any) -> None:
    application = msal.ConfidentialClientApplication(
        CLIENT_ID, client_credential="synthetic-secret", authority=AUTHORITY
    )
    flow = application.initiate_auth_code_flow(
        ["User.Read"],
        redirect_uri="https://consumer.example/callback",
        login_hint="maya.chen@halcyonadvisory.example",
    )
    authorize = requests.get(flow["auth_uri"], allow_redirects=False)
    response = {
        key: values[0] for key, values in parse_qs(urlsplit(authorize.headers["Location"]).query).items()
    }
    result = application.acquire_token_by_auth_code_flow(flow, response)
    assert "access_token" in result, result
    assert result["id_token_claims"]["preferred_username"] == "maya.chen@halcyonadvisory.example"


def test_msal_default_consent_refresh_and_discovered_userinfo(emulator_intercept: Any) -> None:
    application = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY)
    initial = application.acquire_token_by_username_password(
        "maya.chen@halcyonadvisory.example",
        "synthetic-password",
        scopes=["https://graph.microsoft.com/.default"],
    )
    assert "access_token" in initial, initial
    account = application.get_accounts(username="maya.chen@halcyonadvisory.example")[0]
    refreshed = application.acquire_token_silent(["User.Read"], account=account, force_refresh=True)
    assert "access_token" in refreshed, refreshed
    discovery = requests.get(AUTHORITY + "/v2.0/.well-known/openid-configuration").json()
    userinfo = requests.get(
        discovery["userinfo_endpoint"], headers={"Authorization": f"Bearer {refreshed['access_token']}"}
    )
    assert userinfo.status_code == 200
    assert userinfo.json()["email"] == "maya.chen@halcyonadvisory.example"
    assert userinfo.json()["sub"] == initial["id_token_claims"]["sub"]
