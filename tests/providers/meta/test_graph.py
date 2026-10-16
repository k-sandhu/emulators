"""Graph API conventions: auth, errors, /me, debug_token, fields, paging, OAuth."""

from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx

from emulators.providers.meta.common import APP_SECRET
from tests.providers.meta.conftest import GRAPH, PAGE, SYS, USER, mint


async def test_missing_and_invalid_token_are_190(client: httpx.AsyncClient) -> None:
    missing = await client.get(f"{GRAPH}/me")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == 190
    assert missing.json()["error"]["type"] == "OAuthException"
    assert missing.json()["error"]["fbtrace_id"]

    invalid = await client.get(f"{GRAPH}/me", headers={"Authorization": "Bearer not-a-token"})
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == 190
    assert invalid.json()["error"]["error_subcode"] == 467


async def test_expired_token_is_190_subcode_463(client: httpx.AsyncClient) -> None:
    token = (await mint(client, {"type": "user", "user": "p040", "expires_in": -3600}))["access_token"]
    r = await client.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == 190
    assert r.json()["error"]["error_subcode"] == 463


async def test_access_token_query_param_and_response_headers(client: httpx.AsyncClient) -> None:
    token = (await mint(client, {"type": "system_user"}))["access_token"]
    r = await client.get(f"{GRAPH}/me", params={"access_token": token})
    assert r.status_code == 200, r.text
    assert r.headers["x-fb-trace-id"]
    assert "call_count" in r.headers["x-app-usage"]
    assert r.headers["x-business-use-case-usage"]


async def test_me_for_each_token_type(client: httpx.AsyncClient) -> None:
    system = await client.get(f"{GRAPH}/me", headers=SYS)
    assert system.json()["name"] == "Halcyon System User"
    user = await client.get(f"{GRAPH}/me", headers=USER)
    assert user.json()["name"] == "Natalie Cho"
    assert user.json()["id"].isdigit()
    page = await client.get(f"{GRAPH}/me", headers=PAGE)
    assert page.json()["name"] == "Halcyon Advisory Group"
    assert page.json()["username"] == "halcyonadvisory"


async def test_fields_expansion_selects_and_defaults(client: httpx.AsyncClient) -> None:
    r = await client.get(f"{GRAPH}/me", headers=USER, params={"fields": "id,name,email,first_name"})
    body = r.json()
    assert body["email"] == "natalie.cho@halcyonadvisory.example"
    assert body["first_name"] == "Natalie"
    assert "link" not in body
    default = await client.get(f"{GRAPH}/me", headers=USER)
    assert set(default.json()) == {"id", "name"}
    # Malformed fields values are tolerated, never a hang or a 500.
    for weird in ("name.", "name{", ",,", "name.limit(5"):
        resp = await client.get(f"{GRAPH}/me", headers=USER, params={"fields": weird})
        assert resp.status_code == 200, weird


async def test_unknown_object_is_803(client: httpx.AsyncClient) -> None:
    r = await client.get(f"{GRAPH}/999999999999999", headers=SYS)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == 803
    assert "do not exist" in r.json()["error"]["message"]


async def test_appsecret_proof_verified_when_present(client: httpx.AsyncClient) -> None:
    token = (await mint(client, {"type": "user", "user": "p040"}))["access_token"]
    good = hmac.new(APP_SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()
    ok = await client.get(f"{GRAPH}/me", params={"access_token": token, "appsecret_proof": good})
    assert ok.status_code == 200
    bad = await client.get(f"{GRAPH}/me", params={"access_token": token, "appsecret_proof": "deadbeef"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == 100


async def test_me_accounts_lists_managed_page_with_page_token(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    r = await client.get(
        f"{GRAPH}/me/accounts", headers=USER, params={"fields": "name,access_token,category,tasks"}
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert len(data) == 1
    entry = data[0]
    assert entry["id"] == ids.page_id
    assert entry["category"] == "Accountant"
    assert "MANAGE" in entry["tasks"]
    assert entry["access_token"]
    # The page token handed out here works against Messenger endpoints.
    convs = await client.get(
        f"{GRAPH}/{ids.page_id}/conversations",
        headers={"Authorization": f"Bearer {entry['access_token']}"},
    )
    assert convs.status_code == 200
    # A token for an employee who does not manage the Page sees no accounts.
    other = (await mint(client, {"type": "user", "user": "p001"}))["access_token"]
    r2 = await client.get(f"{GRAPH}/me/accounts", headers={"Authorization": f"Bearer {other}"})
    assert r2.json()["data"] == []
    # Page tokens cannot call /me/accounts.
    denied = await client.get(f"{GRAPH}/me/accounts", headers=PAGE)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == 200


async def test_me_permissions(client: httpx.AsyncClient) -> None:
    r = await client.get(f"{GRAPH}/me/permissions", headers=USER)
    perms = {p["permission"]: p["status"] for p in r.json()["data"]}
    assert perms["whatsapp_business_messaging"] == "granted"
    assert perms["pages_show_list"] == "granted"


async def test_debug_token(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    token = (await mint(client, {"type": "user", "user": "p040"}))["access_token"]
    app_token = f"{ids.app_id}|{ids.app_secret}"
    r = await client.get(f"{GRAPH}/debug_token", params={"input_token": token, "access_token": app_token})
    data = r.json()["data"]
    assert data["is_valid"] is True
    assert data["type"] == "USER"
    assert data["app_id"] == ids.app_id
    assert data["expires_at"] > 0
    assert data["data_access_expires_at"] > data["expires_at"]
    assert "email" in data["scopes"]
    assert {g["scope"] for g in data["granular_scopes"]} >= {"whatsapp_business_messaging"}
    bad = await client.get(
        f"{GRAPH}/debug_token", params={"input_token": "garbage", "access_token": app_token}
    )
    assert bad.json()["data"]["is_valid"] is False
    assert bad.json()["data"]["error"]["code"] == 190
    page = await client.get(
        f"{GRAPH}/debug_token",
        params={
            "input_token": (await mint(client, {"type": "page"}))["access_token"],
            "access_token": app_token,
        },
    )
    assert page.json()["data"]["type"] == "PAGE"
    assert page.json()["data"]["profile_id"] == ids.page_id
    assert page.json()["data"]["expires_at"] == 0


async def test_unversioned_path_and_page_username(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    r = await client.get("https://graph.facebook.com/me", headers=USER)
    assert r.status_code == 200 and r.json()["name"] == "Natalie Cho"
    for version in ("v17.0", "v21.0", "v24.0"):
        ok = await client.get(f"https://graph.facebook.com/{version}/me", headers=USER)
        assert ok.status_code == 200
    page = await client.get(f"{GRAPH}/halcyonadvisory", headers=SYS, params={"fields": "name,fan_count,link"})
    assert page.json()["id"] == ids.page_id
    assert page.json()["fan_count"] > 0


async def test_cursor_paging(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    first = await client.get(f"{GRAPH}/{ids.waba_id}/message_templates", headers=SYS, params={"limit": 2})
    body = first.json()
    assert len(body["data"]) == 2
    assert body["paging"]["cursors"]["after"]
    assert "next" in body["paging"]
    assert body["paging"]["next"].startswith("https://graph.facebook.com/v21.0/")
    second = await client.get(body["paging"]["next"], headers=SYS)
    assert [t["name"] for t in second.json()["data"]] != [t["name"] for t in body["data"]]
    names = [t["name"] for t in body["data"]] + [t["name"] for t in second.json()["data"]]
    assert len(names) == len(set(names))


async def test_oauth_dialog_code_exchange_and_long_lived(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    redirect_uri = "https://app.example/callback"
    dialog = await client.get(
        "https://www.facebook.com/v21.0/dialog/oauth",
        params={
            "client_id": ids.app_id,
            "redirect_uri": redirect_uri,
            "state": "s3cret-state",
            "scope": "public_profile,email,pages_show_list,whatsapp_business_messaging",
            "response_type": "code",
            "login_hint": "zoe.anderson@halcyonadvisory.example",
        },
        follow_redirects=False,
    )
    assert dialog.status_code == 302
    query = parse_qs(urlsplit(dialog.headers["location"]).query)
    assert query["state"] == ["s3cret-state"]
    code = query["code"][0]
    exchange = await client.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "client_id": ids.app_id,
            "client_secret": ids.app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    assert exchange.status_code == 200, exchange.text
    short = exchange.json()
    assert short["token_type"] == "bearer" and short["expires_in"] == 3600
    me = await client.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {short['access_token']}"})
    assert me.json()["name"] == "Zoe Anderson"
    # Codes are single-use.
    replay = await client.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "client_id": ids.app_id,
            "client_secret": ids.app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    assert replay.status_code == 400 and replay.json()["error"]["code"] == 100
    long_lived = await client.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": ids.app_id,
            "client_secret": ids.app_secret,
            "fb_exchange_token": short["access_token"],
        },
    )
    assert long_lived.json()["expires_in"] == 5184000
    wrong_secret = await client.get(
        f"{GRAPH}/oauth/access_token",
        params={"client_id": ids.app_id, "client_secret": "nope", "code": "whatever"},
    )
    assert wrong_secret.status_code == 400


async def test_client_credentials_app_token(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    r = await client.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "client_id": ids.app_id,
            "client_secret": ids.app_secret,
            "grant_type": "client_credentials",
        },
    )
    assert r.json()["access_token"] == f"{ids.app_id}|{ids.app_secret}"
    assert r.json()["token_type"] == "bearer"
    me = await client.get(f"{GRAPH}/me", params={"access_token": r.json()["access_token"]})
    assert me.json()["id"] == ids.app_id


async def test_dialog_without_hint_renders_account_picker(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    r = await client.get(
        "https://facebook.com/v21.0/dialog/oauth",
        params={"client_id": ids.app_id, "redirect_uri": "https://app.example/cb"},
    )
    assert r.status_code == 200
    assert "Natalie Cho" in r.text


async def test_mint_token_types(client: httpx.AsyncClient) -> None:
    default = await mint(client, {})
    assert default["type"] == "SYSTEM_USER"
    page = await mint(client, {"type": "page"})
    me = await client.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {page['access_token']}"})
    assert me.json()["username"] == "halcyonadvisory"
    app = await mint(client, {"type": "app"})
    assert "|" in app["access_token"]


async def test_batch_requests(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    import json as json_module

    token = (await mint(client, {"type": "system_user"}))["access_token"]
    batch = [
        {"method": "GET", "relative_url": "me?fields=id,name"},
        {"method": "GET", "relative_url": f"{ids.waba_id}?fields=id,name"},
        {"method": "GET", "relative_url": "999999999999999"},
    ]
    r = await client.post(f"{GRAPH}/", data={"batch": json_module.dumps(batch), "access_token": token})
    assert r.status_code == 200, r.text
    results = r.json()
    assert [entry["code"] for entry in results] == [200, 200, 400]
    assert json_module.loads(results[0]["body"])["name"] == "Halcyon System User"
    assert json_module.loads(results[1]["body"])["id"] == ids.waba_id
    assert json_module.loads(results[2]["body"])["error"]["code"] == 803


async def test_picture_endpoint_and_cdn_bytes(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    r = await client.get(f"{GRAPH}/{ids.page_id}/picture", params={"redirect": "false"})
    assert r.status_code == 200
    url = r.json()["data"]["url"]
    assert "fbcdn.net" in url
    image = await client.get(url)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content[:4] == b"\x89PNG"
    redirect = await client.get(f"{GRAPH}/{ids.page_id}/picture", follow_redirects=False)
    assert redirect.status_code == 302


async def test_prefix_routing_through_gateway(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    hub = client.hub  # type: ignore[attr-defined]
    transport = httpx.ASGITransport(app=hub.gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080") as gateway:
        me = await gateway.get("/meta/v21.0/me", headers=SYS)
        assert me.status_code == 200, me.text
        first = await gateway.get(
            f"/meta/v21.0/{ids.waba_id}/message_templates", headers=SYS, params={"limit": 2}
        )
        next_link = first.json()["paging"]["next"]
        assert next_link.startswith("http://localhost:44080/meta/v21.0/")
        second = await gateway.get(next_link, headers=SYS)
        assert second.status_code == 200
