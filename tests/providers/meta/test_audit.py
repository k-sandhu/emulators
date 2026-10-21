"""Adversarial regressions for Graph, WhatsApp, and Messenger wire contracts."""

from __future__ import annotations

import json
import struct
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from tests.providers.meta.conftest import GRAPH, GRAPH_HOST, PAGE, SYS, USER, Receiver, action, auth, mint


def wa(**updates: Any) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "to": "13055550174",
        "type": "text",
        "text": {"body": "雪 👋"},
        **updates,
    }


def error(response: httpx.Response, code: int = 100) -> None:
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["fbtrace_id"]
    assert response.headers["x-fb-request-id"]


@pytest.mark.parametrize(
    "kind,scopes",
    [("system_user", []), ("user", []), ("system_user", ["public_profile"]), ("user", ["public_profile"])],
)
async def test_scopes_are_required_for_all_whatsapp_tokens(
    client: httpx.AsyncClient, ids: SimpleNamespace, kind: str, scopes: list[str]
) -> None:
    token = (await mint(client, {"type": kind, "user": "p040", "scopes": scopes}))["access_token"]
    for method, path, payload in [
        ("GET", ids.waba_id, None),
        ("POST", ids.phone_number_id + "/messages", wa()),
    ]:
        r = await client.request(method, f"{GRAPH}/{path}", headers=auth(token), json=payload)
        assert r.status_code == 403 and r.json()["error"]["code"] == 200


async def test_business_assets_require_assignment(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    token = (await mint(client, {"type": "user", "user": "p001"}))["access_token"]
    for path in (ids.waba_id, ids.business_id + "/owned_pages", ids.phone_number_id):
        denied = await client.get(f"{GRAPH}/{path}", headers=auth(token))
        assert denied.status_code == 403, path
        allowed = await client.get(f"{GRAPH}/{path}", headers=USER)
        assert allowed.status_code == 200, allowed.text
    app = (await mint(client, {"type": "app"}))["access_token"]
    assert (await client.get(f"{GRAPH}/{ids.business_id}/owned_pages", headers=auth(app))).status_code == 403


async def test_page_scopes_and_page_scoped_ids(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    psid = (await action(client, "inbound_messenger", {"person": "c023-2", "text": "hello"}))["psid"]
    token = (await mint(client, {"type": "page", "scopes": []}))["access_token"]
    assert (await client.get(f"{GRAPH}/{psid}", headers=auth(token))).status_code == 403
    other = (await mint(client, {"type": "page", "page_id": "199999999999999"}))["access_token"]
    error(await client.get(f"{GRAPH}/{psid}", headers=auth(other)))
    error(
        await client.post(
            f"{GRAPH}/me/messages",
            headers=auth(other),
            json={"recipient": {"id": psid}, "message": {"text": "hi"}},
        ),
        100,
    )
    assert (await client.get(f"{GRAPH}/{psid}", headers=PAGE)).status_code == 200
    messaging_only = (await mint(client, {"type": "page", "scopes": ["pages_messaging"]}))["access_token"]
    assert (
        await client.get(f"{GRAPH}/{ids.page_id}/conversations", headers=auth(messaging_only))
    ).status_code == 403
    assert (
        await client.post(
            f"{GRAPH}/me/messages",
            headers=auth(messaging_only),
            json={"recipient": {"id": psid}, "message": {"text": "hello"}},
        )
    ).status_code == 200
    empty_user = (await mint(client, {"type": "user", "user": "p040", "scopes": []}))["access_token"]
    assert (
        await client.post(f"{GRAPH}/{ids.page_id}", headers=auth(empty_user), json={"about": "changed"})
    ).status_code == 403


async def test_accounts_cannot_escalate_permissions_or_cross_apps(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    token = (await mint(client, {"type": "user", "user": "p040", "scopes": ["pages_show_list"]}))[
        "access_token"
    ]
    accounts = (await client.get(f"{GRAPH}/me/accounts", headers=auth(token))).json()["data"]
    page_token = accounts[0]["access_token"]
    denied = await client.get(f"{GRAPH}/{ids.page_id}/conversations", headers=auth(page_token))
    assert denied.status_code == 403
    none = (await mint(client, {"type": "user", "user": "p040", "scopes": []}))["access_token"]
    assert (await client.get(f"{GRAPH}/me/accounts", headers=auth(none))).status_code == 403
    # A scope grant for one app cannot manage a different app's subscriptions.
    other = (await mint(client, {"type": "app", "client_id": "100000000000009"}))["access_token"]
    assert (await client.get(f"{GRAPH}/{ids.app_id}/subscriptions", headers=auth(other))).status_code == 403
    assert (await client.get(f"{GRAPH}/me", headers=auth(other))).json()["id"] == "100000000000009"
    debug = (
        await client.get(f"{GRAPH}/debug_token", headers=auth(other), params={"input_token": other})
    ).json()["data"]
    assert debug["app_id"] == debug["user_id"] == "100000000000009"


async def test_email_scope_applies_to_user_id_and_me(client: httpx.AsyncClient) -> None:
    token = await mint(client, {"type": "user", "user": "p040", "scopes": ["public_profile"]})
    for path in ("me", token["user_id"]):
        body = (
            await client.get(
                f"{GRAPH}/{path}", headers=auth(token["access_token"]), params={"fields": "email"}
            )
        ).json()
        assert "email" not in body


async def test_permission_revocation_and_deauthorization(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    token = await mint(client, {"type": "user", "user": "p040"})
    headers = auth(token["access_token"])
    page_token = (await client.get(f"{GRAPH}/me/accounts", headers=headers)).json()["data"][0]["access_token"]
    revoked = await client.delete(f"{GRAPH}/me/permissions/pages_messaging", headers=headers)
    assert revoked.json() == {"success": True}
    assert (
        await client.get(f"{GRAPH}/{ids.page_id}/conversations", headers=auth(page_token))
    ).status_code == 403
    other = await mint(client, {"type": "user", "user": "p001"})
    denied = await client.delete(
        f"{GRAPH}/{token['user_id']}/permissions", headers=auth(other["access_token"])
    )
    assert denied.status_code == 403
    deauthorized = await client.delete(f"{GRAPH}/me/permissions", headers=headers)
    assert deauthorized.json() == {"success": True}
    assert (await client.get(f"{GRAPH}/me", headers=headers)).status_code == 401
    assert (await client.get(f"{GRAPH}/me", headers=auth(page_token))).status_code == 401


async def oauth_code(client: httpx.AsyncClient, ids: SimpleNamespace) -> str:
    r = await client.get(
        "https://www.facebook.com/v21.0/dialog/oauth",
        params={
            "client_id": ids.app_id,
            "redirect_uri": "https://app.example/callback",
            "login_hint": "natalie.cho@halcyonadvisory.example",
        },
    )
    return parse_qs(urlsplit(r.headers["location"]).query)["code"][0]


async def test_oauth_requires_secret_and_exact_redirect(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    code = await oauth_code(client, ids)
    params = {
        "client_id": ids.app_id,
        "client_secret": ids.app_secret,
        "redirect_uri": "https://app.example/callback",
        "code": code,
    }
    for missing in ("client_secret", "redirect_uri"):
        error(
            await client.get(
                f"{GRAPH}/oauth/access_token", params={k: v for k, v in params.items() if k != missing}
            )
        )
    error(
        await client.get(
            f"{GRAPH}/oauth/access_token", params={**params, "redirect_uri": "https://app.example/other"}
        )
    )
    ok = await client.get(f"{GRAPH}/oauth/access_token", params=params)
    assert ok.status_code == 200
    other = await mint(client, {"type": "app", "client_id": "100000000000008"})
    secret = other["access_token"].partition("|")[2]
    error(
        await client.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": other["app_id"],
                "client_secret": secret,
                "fb_exchange_token": ok.json()["access_token"],
            },
        )
    )
    denied = await client.get(
        f"{GRAPH}/debug_token",
        headers=auth(other["access_token"]),
        params={"input_token": ok.json()["access_token"]},
    )
    assert denied.status_code == 403


async def test_lenient_oauth_client_retains_its_first_secret(client: httpx.AsyncClient) -> None:
    app_id = "100000000000006"
    callback = "https://app.example/callback"
    dialog = await client.get(
        "https://www.facebook.com/v21.0/dialog/oauth",
        params={
            "client_id": app_id,
            "redirect_uri": callback,
            "login_hint": "natalie.cho@halcyonadvisory.example",
        },
    )
    code = parse_qs(urlsplit(dialog.headers["location"]).query)["code"][0]
    exchanged = await client.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "client_id": app_id,
            "client_secret": "fixture-client-secret",
            "redirect_uri": callback,
            "code": code,
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    params = {
        "grant_type": "client_credentials",
        "client_id": app_id,
        "client_secret": "fixture-client-secret",
    }
    app_token = (await client.get(f"{GRAPH}/oauth/access_token", params=params)).json()["access_token"]
    assert (await client.get(f"{GRAPH}/me", headers=auth(app_token))).json()["id"] == app_id
    error(await client.get(f"{GRAPH}/oauth/access_token", params={**params, "client_secret": "wrong"}))


@pytest.mark.parametrize("proof", ["雪", "z" * 64, "0" * 63])
async def test_bad_proofs_never_crash(client: httpx.AsyncClient, proof: str) -> None:
    error(await client.get(f"{GRAPH}/me", headers=USER, params={"appsecret_proof": proof}))


async def test_unicode_app_secret_and_unknown_app_tokens(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    for token in (ids.app_id + "|雪", "100000000000007|unregistered"):
        r = await client.get(f"{GRAPH}/me", params={"access_token": token})
        assert r.status_code == 401


@pytest.mark.parametrize("cursor", ["!", "e30", "W10", "eyJvIjotMn0", "eyJvIjp0cnVlfQ", "eyJvIjoxLjV9"])
async def test_malformed_cursors_are_errors(
    client: httpx.AsyncClient, ids: SimpleNamespace, cursor: str
) -> None:
    error(await client.get(f"{GRAPH}/{ids.waba_id}/message_templates", headers=SYS, params={"after": cursor}))


async def test_paging_end_backward_zero_limit_and_projection(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    url = f"{GRAPH}/{ids.waba_id}/message_templates"
    first = (await client.get(url, headers=SYS, params={"fields": "status", "limit": 2})).json()
    second = (await client.get(first["paging"]["next"], headers=SYS)).json()
    back = (await client.get(second["paging"]["previous"], headers=SYS)).json()
    assert back["data"] == first["data"]
    last = (await client.get(second["paging"]["next"], headers=SYS)).json()
    assert "next" not in last["paging"]
    all_ids = [i["id"] for p in (first, second, last) for i in p["data"]]
    assert len(all_ids) == len(set(all_ids)) == 5
    empty = await client.get(url, headers=SYS, params={"limit": 0, "summary": "true"})
    assert empty.json() == {"data": [], "summary": {"total_count": 5}}
    error(await client.get(url, headers=SYS, params={"limit": -1}))
    error(
        await client.get(
            url,
            headers=SYS,
            params={
                "after": first["paging"]["cursors"]["after"],
                "before": second["paging"]["cursors"]["before"],
            },
        )
    )


async def test_nested_messages_have_followable_paging_and_newest_first(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    result = (
        await client.get(
            f"{GRAPH}/{ids.page_id}/conversations",
            headers=PAGE,
            params={"fields": "message_count,messages.limit(2).summary(true){message,created_time}"},
        )
    ).json()["data"][0]
    messages = result["messages"]
    assert messages["summary"]["total_count"] == result["message_count"]
    next_page = (await client.get(messages["paging"]["next"], headers=PAGE)).json()
    assert not {m["id"] for m in messages["data"]} & {m["id"] for m in next_page["data"]}
    assert messages["data"][0]["created_time"] >= next_page["data"][0]["created_time"]
    assert "message" in next_page["data"][0]
    assert (await client.get(f"{GRAPH}/me/conversations", headers=PAGE)).status_code == 200


@pytest.mark.parametrize(
    "fields",
    ["messages{" * 80 + "id" + "}" * 80, "messages" + ".x" * 1000, "name.limit(", "name.", "name{", ",,"],
)
async def test_hostile_field_expansions_return_100(client: httpx.AsyncClient, fields: str) -> None:
    error(await client.get(f"{GRAPH}/me", headers=USER, params={"fields": fields}))


async def test_unknown_fields_numeric_ids_and_aliases_are_distinct(client: httpx.AsyncClient) -> None:
    error(await client.get(f"{GRAPH}/me", headers=USER, params={"fields": "not_a_field"}))
    numeric = await client.get(f"{GRAPH}/199999999999999", headers=SYS)
    error(numeric)
    assert numeric.json()["error"]["error_subcode"] == 33
    error(await client.get(f"{GRAPH}/unknown_alias", headers=SYS), 803)


@pytest.mark.parametrize(
    "raw",
    [
        '{"extra": NaN}',
        '{"extra": Infinity}',
        '{"extra": "\\ud800"}',
        '{"extra":' + "[" * 70 + "0" + "]" * 70 + "}",
    ],
)
async def test_invalid_json_values_cannot_poison_stored_records(
    client: httpx.AsyncClient, ids: SimpleNamespace, raw: str
) -> None:
    error(
        await client.post(
            f"{GRAPH}/{ids.waba_id}/message_templates",
            headers={**SYS, "Content-Type": "application/json"},
            content=raw,
        )
    )


@pytest.mark.parametrize(
    "payload",
    [
        wa(type=[]),
        wa(context=True),
        wa(text={"body": 1}),
        wa(text={"body": "x" * 4097}),
        wa(text={"body": "hi", "preview_url": "yes"}),
        wa(type="template", template={"name": "hello_world", "language": "en_US"}),
        wa(type="template", template={"name": "hello_world", "language": {}}),
        wa(
            type="template",
            template={"name": "hello_world", "language": {"code": "en_US"}, "components": [True]},
        ),
        wa(type="location", location={"latitude": 91, "longitude": 0}),
        wa(type="location", location={"latitude": False, "longitude": []}),
        wa(type="image", image={"link": "file:///local.png"}),
        wa(type="image", image={"id": "123", "link": "https://halcyonadvisory.example/a.png"}),
        wa(type="contacts", contacts=[{}]),
        wa(type="interactive", interactive={"type": []}),
        wa(type="reaction", reaction={"message_id": "missing"}),
    ],
)
async def test_whatsapp_malformed_payloads(
    client: httpx.AsyncClient, ids: SimpleNamespace, payload: dict[str, Any]
) -> None:
    r = await client.post(f"{GRAPH}/{ids.phone_number_id}/messages", headers=SYS, json=payload)
    assert r.status_code == 400 and r.json()["error"]["code"] in (100, 131051), r.text


@pytest.mark.parametrize(
    "extra",
    [
        {"footer": {"text": "x" * 61}},
        {"footer": {"text": []}},
        {"header": {"type": "text", "text": "x" * 61}},
        {"header": {"type": "unknown", "text": "Title"}},
        {"header": {"type": "image", "image": {"link": "https://halcyonadvisory.example/a.png"}}},
        {
            "action": {
                "button": "Choose",
                "sections": [
                    {
                        "rows": [
                            {"id": "option", "title": "Option", "description": "x" * 73},
                        ]
                    }
                ],
            }
        },
    ],
)
async def test_interactive_optional_fields_validate_before_sending(
    client: httpx.AsyncClient, ids: SimpleNamespace, receiver: Receiver, extra: dict[str, Any]
) -> None:
    await receiver.configure(client, "whatsapp_business_account")
    ctx = client.hub.context("meta")  # type: ignore[attr-defined]
    before = len(ctx.store["wa_messages"])
    payload = {
        "type": "list",
        "body": {"text": "Select an option"},
        "action": {"button": "Choose", "sections": [{"rows": [{"id": "option", "title": "Option"}]}]},
        **extra,
    }
    response = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/messages",
        headers=SYS,
        json=wa(type="interactive", interactive=payload),
    )
    error(response)
    assert len(ctx.store["wa_messages"]) == before
    assert receiver.deliveries == []


async def test_interactive_optional_field_length_boundaries(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    response = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/messages",
        headers=SYS,
        json=wa(
            type="interactive",
            interactive={
                "type": "list",
                "body": {"text": "Select an option"},
                "header": {"type": "text", "text": "H" * 60},
                "footer": {"text": "F" * 60},
                "action": {
                    "button": "Choose",
                    "sections": [
                        {
                            "rows": [
                                {"id": "option", "title": "Option", "description": "D" * 72},
                            ]
                        }
                    ],
                },
            },
        ),
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("kind", ["image", "video", "document"])
async def test_interactive_button_header_media_reference(
    client: httpx.AsyncClient, ids: SimpleNamespace, kind: str
) -> None:
    payload = {
        "type": "button",
        "body": {"text": "Confirm"},
        "header": {"type": kind, kind: {"id": "missing-media"}},
        "action": {"buttons": [{"type": "reply", "reply": {"id": "confirm", "title": "Confirm"}}]},
    }
    url = f"{GRAPH}/{ids.phone_number_id}/messages"
    error(await client.post(url, headers=SYS, json=wa(type="interactive", interactive=payload)))
    payload["header"][kind] = {"link": "https://halcyonadvisory.example/media"}
    response = await client.post(url, headers=SYS, json=wa(type="interactive", interactive=payload))
    assert response.status_code == 200, response.text


async def test_template_translation_and_status_filters(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    error(
        await client.post(
            f"{GRAPH}/{ids.phone_number_id}/messages",
            headers=SYS,
            json=wa(type="template", template={"name": "hello_world", "language": {"code": "fr"}}),
        ),
        132001,
    )
    r = await client.get(
        f"{GRAPH}/{ids.waba_id}/message_templates", headers=SYS, params={"status": "REJECTED"}
    )
    assert [t["name"] for t in r.json()["data"]] == ["year_end_promo"]
    waba = (await client.get(f"{GRAPH}/{ids.waba_id}", headers=SYS)).json()
    assert waba["timezone_id"].isdigit()
    assert len(waba["message_template_namespace"].split("_")) == 5


async def test_template_edits_validate_before_mutating(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    templates = (await client.get(f"{GRAPH}/{ids.waba_id}/message_templates", headers=SYS)).json()["data"]
    template = templates[0]
    for payload in (
        {"components": True},
        {"components": [1]},
        {"components": [{"type": "BODY", "text": []}]},
        {"category": {}},
    ):
        error(await client.post(f"{GRAPH}/{template['id']}", headers=SYS, json=payload))
    assert (await client.get(f"{GRAPH}/{template['id']}", headers=SYS)).json() == template
    created = await client.post(
        f"{GRAPH}/{ids.waba_id}/message_templates",
        headers=SYS,
        json={
            "name": "hello_world",
            "language": "fr",
            "category": "UTILITY",
            "components": [{"type": "BODY", "text": "Bonjour"}],
        },
    )
    assert created.status_code == 200
    deleted = await client.delete(
        f"{GRAPH}/{ids.waba_id}/message_templates",
        headers=SYS,
        params={"name": "hello_world", "hsm_id": created.json()["id"]},
    )
    assert deleted.json() == {"success": True}
    assert (
        await client.get(
            f"{GRAPH}/{ids.waba_id}/message_templates", headers=SYS, params={"name": "hello_world"}
        )
    ).json()["data"][0]["language"] == "en_US"


async def upload(client: httpx.AsyncClient, ids: SimpleNamespace, filename: str = "雪.pdf") -> str:
    r = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/media",
        headers=SYS,
        data={"messaging_product": "whatsapp", "type": "application/pdf"},
        files={"file": (filename, b"%PDF-fixture", "application/pdf")},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_media_expiry_integrity_permissions_and_phone_scope(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    mid = await upload(client, ids)
    url = (await client.get(f"{GRAPH}/{mid}", headers=SYS)).json()["url"]
    downloaded = await client.get(url, headers=SYS)
    assert downloaded.content == b"%PDF-fixture"
    assert "filename*=UTF-8''" in downloaded.headers["content-disposition"]
    assert (await client.get(url, headers=PAGE)).status_code == 403
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    for invalid_hash in ("bad", "雪"):
        error_url = parts._replace(query=urlencode({**query, "hash": invalid_hash})).geturl()
        assert (await client.get(error_url, headers=SYS)).status_code == 404
    for method in ("GET", "DELETE"):
        error(
            await client.request(
                method, f"{GRAPH}/{mid}", headers=SYS, params={"phone_number_id": "199999999999999"}
            )
        )
    client.hub.clock.advance(timedelta(seconds=301))  # type: ignore[attr-defined]
    assert (await client.get(url, headers=SYS)).status_code == 404
    fresh = (await client.get(f"{GRAPH}/{mid}", headers=SYS)).json()["url"]
    assert (await client.get(fresh, headers=SYS)).status_code == 200


@pytest.mark.parametrize(
    "product,mime,data",
    [(None, "image/png", b"x"), ("whatsapp", "image/gif", b"x"), ("whatsapp", "application/pdf", b"")],
)
async def test_media_upload_validation(
    client: httpx.AsyncClient, ids: SimpleNamespace, product: str | None, mime: str, data: bytes
) -> None:
    form = {"type": mime}
    if product:
        form["messaging_product"] = product
    error(
        await client.post(
            f"{GRAPH}/{ids.phone_number_id}/media",
            headers=SYS,
            data=form,
            files={"file": ("file.bin", data, mime)},
        )
    )


async def test_register_deregister_and_read_direction(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    url = f"{GRAPH}/{ids.phone_number_id}"
    error(await client.post(url + "/register", headers=SYS, json={"messaging_product": "whatsapp"}))
    await client.post(url + "/deregister", headers=SYS)
    error(await client.post(url + "/messages", headers=SYS, json=wa()), 133010)
    assert (
        await client.post(
            url + "/register", headers=SYS, json={"messaging_product": "whatsapp", "pin": "123456"}
        )
    ).json() == {"success": True}
    sent = (await client.post(url + "/messages", headers=SYS, json=wa())).json()["messages"][0]["id"]
    error(
        await client.post(
            url + "/messages",
            headers=SYS,
            json={"messaging_product": "whatsapp", "status": "read", "message_id": sent},
        )
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"recipient": True},
        {"recipient": [1]},
        {"message": {"text": 12}},
        {"message": {"text": "x", "quick_replies": 3}},
        {"message": {"attachment": True}},
        {"message": {"attachment": {"type": "image", "payload": True}}},
        {"message": {"quick_replies": [{"content_type": "text"}]}},
        {
            "message": {
                "text": "hi",
                "attachment": {"type": "image", "payload": {"url": "https://halcyonadvisory.example/a.png"}},
            }
        },
        {"message": {"text": "hi"}, "sender_action": "typing_on"},
        {"message": {"text": "hi"}, "messaging_type": "MESSAGE_TAG", "tag": []},
    ],
)
async def test_messenger_malformed_payloads(client: httpx.AsyncClient, updates: dict[str, Any]) -> None:
    psid = (await action(client, "inbound_messenger", {"person": "c023-2", "text": "hello"}))["psid"]
    body = {"recipient": {"id": psid}, "message": {"text": "hello"}, **updates}
    error(await client.post(f"{GRAPH}/me/messages", headers=PAGE, json=body))


@pytest.mark.parametrize(
    "body",
    [
        {"message": True},
        {"message": {"attachment": True}},
        {"message": {"attachment": {"type": "image", "payload": True}}},
        {"message": {"attachment": {"type": "image", "payload": {"is_reusable": True}}}},
    ],
)
async def test_attachment_validation(client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    error(await client.post(f"{GRAPH}/me/message_attachments", headers=PAGE, json=body))


async def test_messenger_profile_fields_form_and_validation(client: httpx.AsyncClient) -> None:
    for body in ({"greeting": 1}, {"greeting": [True]}, {"get_started": []}, {"persistent_menu": [1]}):
        error(await client.post(f"{GRAPH}/me/messenger_profile", headers=PAGE, json=body))
    for value in (True, 1, {"x": "y"}):
        error(
            await client.request(
                "DELETE", f"{GRAPH}/me/messenger_profile", headers=PAGE, json={"fields": value}
            )
        )
    deleted = await client.request(
        "DELETE", f"{GRAPH}/me/messenger_profile", headers=PAGE, data={"fields": json.dumps(["greeting"])}
    )
    assert deleted.json() == {"result": "success"}
    assert (
        await client.get(f"{GRAPH}/me/messenger_profile", headers=PAGE, params={"fields": "greeting"})
    ).json() == {"data": []}


async def test_form_and_query_encoded_sends(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    result = await client.post(
        f"{GRAPH}/{ids.phone_number_id}/messages",
        headers=SYS,
        data={"messaging_product": "whatsapp", "to": "13055550174", "text": json.dumps({"body": "雪"})},
    )
    assert result.status_code == 200, result.text
    psid = (await action(client, "inbound_messenger", {"person": "c023-2", "text": "hello"}))["psid"]
    result = await client.post(
        f"{GRAPH}/me/messages",
        headers=PAGE,
        params={"recipient": json.dumps({"id": psid}), "message": json.dumps({"text": "hello"})},
    )
    assert result.status_code == 200, result.text


async def test_batch_uses_each_requests_token_and_contains_parameter_errors(
    client: httpx.AsyncClient,
) -> None:
    token = (await mint(client, {"type": "user", "user": "p001"}))["access_token"]
    batch = [
        {"method": "GET", "relative_url": "me?access_token=" + token},
        {"method": "POST", "relative_url": "me/messages", "body": "recipient=broken"},
    ]
    result = await client.post(f"{GRAPH}/", headers=USER, json={"batch": batch})
    assert result.status_code == 200, result.text
    assert json.loads(result.json()[0]["body"])["name"] == "Eleanor Whitfield"
    assert result.json()[1]["code"] == 400


async def test_batch_malformed_urls_and_versions_are_per_request_errors(client: httpx.AsyncClient) -> None:
    batch = [
        {"method": "GET", "relative_url": value}
        for value in ("http://[", "https://graph.facebook.com/me", [], "v99.0/me")
    ]
    result = await client.post(f"{GRAPH}/", headers=USER, json={"batch": batch})
    assert result.status_code == 200, result.text
    assert [r["code"] for r in result.json()] == [400] * 4
    assert all(json.loads(r["body"])["error"]["code"] == 100 for r in result.json())
    error(await client.post(f"{GRAPH}/", headers=USER, json={"batch": []}))


async def test_webhook_field_filters_resource_unsubscribe_and_query_preservation(
    client: httpx.AsyncClient, ids: SimpleNamespace, receiver: Receiver
) -> None:
    config = await action(
        client,
        "configure_webhook",
        {
            "object": "page",
            "callback_url": "http://consumer.test/hook?tenant=fixture",
            "verify_token": ids.verify_token,
            "fields": ["messages"],
        },
    )
    assert config["success"] is True
    assert receiver.verifications[-1]["tenant"] == "fixture"
    incoming = await action(
        client, "inbound_messenger", {"person": "c023-2", "postback": {"payload": "HELLO"}}
    )
    assert incoming["webhook_delivered"] is False
    assert receiver.deliveries == []
    assert (await action(client, "inbound_messenger", {"person": "c023-2", "text": "hello"}))[
        "webhook_delivered"
    ]
    await receiver.configure(client, "whatsapp_business_account")
    await client.delete(f"{GRAPH}/{ids.waba_id}/subscribed_apps", headers=SYS)
    incoming = await action(client, "inbound_whatsapp", {"from": "c011-4", "text": "hello"})
    assert incoming["webhook_delivered"] is False
    await client.post(f"{GRAPH}/{ids.waba_id}/subscribed_apps", headers=SYS)
    assert (await action(client, "inbound_whatsapp", {"from": "c011-4", "text": "hello"}))[
        "webhook_delivered"
    ]


async def test_failed_webhook_has_errors_without_billing(
    client: httpx.AsyncClient, ids: SimpleNamespace, receiver: Receiver
) -> None:
    await receiver.configure(client, "whatsapp_business_account")
    mid = (await client.post(f"{GRAPH}/{ids.phone_number_id}/messages", headers=SYS, json=wa())).json()[
        "messages"
    ][0]["id"]
    await action(client, "deliver_statuses", {"message_id": mid, "status": "failed"})
    status = receiver.deliveries[-1]["json"]["entry"][0]["changes"][0]["value"]["statuses"][0]
    assert status["errors"][0]["code"] == 131026
    assert "pricing" not in status and "conversation" not in status


async def test_invalid_inbound_action_does_not_open_messenger_window(client: httpx.AsyncClient) -> None:
    r = await action(client, "inbound_messenger", {"person": "c023-2"})
    assert r["success"] is False
    # Melissa is outside both messaging windows in the seed.
    records = (await client.get(f"{GRAPH_HOST}/_emulator/state/psids")).json()["records"]
    psid = next(p["id"] for p in records if p["name"] == "Melissa Grimaldi")
    r = await client.post(
        f"{GRAPH}/me/messages", headers=PAGE, json={"recipient": {"id": psid}, "message": {"text": "hi"}}
    )
    error(r, 10)


@pytest.mark.parametrize(
    "name,payload",
    [
        ("mint_token", {"expires_in": []}),
        ("mint_token", {"expires_in": "bad"}),
        ("mint_token", {"expires_in": 10**50}),
        ("mint_token", {"type": "unknown"}),
        ("inbound_whatsapp", {"from": "c011-4", "context": True}),
        ("inbound_whatsapp", {"from": "c011-4", "location": 1}),
        ("inbound_whatsapp", {"from": "c011-4", "reaction": "bad"}),
        ("inbound_whatsapp", {"from": "c011-4", "type": "image", "media": {"content_base64": "bad"}}),
        ("inbound_whatsapp", {"from": "c011-4", "type": "image", "media": {"content_base64": "雪"}}),
        ("inbound_whatsapp", {"from": "c011-4", "type": "document", "media": {"doc_id": "unknown"}}),
        ("deliver_statuses", {"message_id": "fixture", "error_code": []}),
    ],
)
async def test_action_malformed_input_returns_graph_error(
    client: httpx.AsyncClient, name: str, payload: dict[str, Any]
) -> None:
    r = await client.post(f"{GRAPH_HOST}/_emulator/actions/{name}", json=payload)
    assert r.status_code == 400 and r.json()["error"]["code"] == 100


async def test_multiple_image_attachments_persist(client: httpx.AsyncClient) -> None:
    psid = (await action(client, "inbound_messenger", {"person": "c023-2", "text": "hello"}))["psid"]
    images = [
        {"type": "image", "payload": {"url": f"https://halcyonadvisory.example/{i}.png"}} for i in range(2)
    ]
    result = await client.post(
        f"{GRAPH}/me/messages",
        headers=PAGE,
        json={"recipient": {"id": psid}, "message": {"attachments": images}},
    )
    assert result.status_code == 200, result.text
    message = await client.get(
        f"{GRAPH}/{result.json()['message_id']}", headers=PAGE, params={"fields": "attachments"}
    )
    assert len(message.json()["attachments"]["data"]) == 2


async def test_messenger_status_action_emits_only_requested_event(
    client: httpx.AsyncClient, receiver: Receiver
) -> None:
    await receiver.configure(client, "page")
    psid = (await action(client, "inbound_messenger", {"person": "c023-2", "text": "hello"}))["psid"]
    result = await client.post(
        f"{GRAPH}/me/messages", headers=PAGE, json={"recipient": {"id": psid}, "message": {"text": "hi"}}
    )
    receiver.deliveries.clear()
    await action(client, "deliver_statuses", {"message_id": result.json()["message_id"], "status": "read"})
    assert len(receiver.deliveries) == 1
    assert "read" in receiver.deliveries[0]["json"]["entry"][0]["messaging"][0]


async def test_inbound_contacts_array_and_supported_audio_sticker_media(
    client: httpx.AsyncClient, receiver: Receiver
) -> None:
    await receiver.configure(client, "whatsapp_business_account")
    contacts = [{"name": {"formatted_name": "Paula Iwasaki", "first_name": "Paula"}}]
    await action(client, "inbound_whatsapp", {"from": "c011-4", "type": "contacts", "contacts": contacts})
    value = receiver.deliveries[-1]["json"]["entry"][0]["changes"][0]["value"]
    assert value["messages"][0]["contacts"] == contacts
    for kind, mime, magic in (("audio", "audio/mp4", b"ftyp"), ("sticker", "image/webp", b"RIFF")):
        await action(client, "inbound_whatsapp", {"from": "c011-4", "type": kind})
        value = receiver.deliveries[-1]["json"]["entry"][0]["changes"][0]["value"]
        media = value["messages"][0][kind]
        assert media["mime_type"] == mime
        meta = (await client.get(f"{GRAPH}/{media['id']}", headers=SYS)).json()
        content = (await client.get(meta["url"], headers=SYS)).content
        assert magic in content[:12]


async def test_media_header_template_parameter_is_required(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    result = await client.post(
        f"{GRAPH}/{ids.waba_id}/message_templates",
        headers=SYS,
        json={
            "name": "audit_media_header",
            "language": "en_US",
            "category": "UTILITY",
            "components": [
                {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["fixture"]}},
                {"type": "BODY", "text": "Hello"},
            ],
        },
    )
    assert result.status_code == 200, result.text
    template = {"name": "audit_media_header", "language": {"code": "en_US"}}
    error(
        await client.post(
            f"{GRAPH}/{ids.phone_number_id}/messages",
            headers=SYS,
            json=wa(type="template", template=template),
        ),
        132000,
    )
    template["components"] = [
        {
            "type": "header",
            "parameters": [{"type": "image", "image": {"link": "https://halcyonadvisory.example/photo.png"}}],
        }
    ]
    assert (
        await client.post(
            f"{GRAPH}/{ids.phone_number_id}/messages",
            headers=SYS,
            json=wa(type="template", template=template),
        )
    ).status_code == 200


async def test_fault_responses_include_graph_headers(client: httpx.AsyncClient) -> None:
    await client.post(f"{GRAPH_HOST}/_emulator/faults", json={"path": "/v21.0/me$", "status": 429})
    r = await client.get(f"{GRAPH}/me", headers=USER)
    assert r.status_code == 429 and r.headers["retry-after"] == "2"
    assert r.headers["x-fb-request-id"] and r.headers["x-business-use-case-usage"]
    assert r.headers["x-fb-trace-id"] == r.json()["error"]["fbtrace_id"]


async def test_malformed_picture_dimensions(client: httpx.AsyncClient, ids: SimpleNamespace) -> None:
    for width in ("雪", "9" * 5000, "-1", "0"):
        error(
            await client.get(f"{GRAPH}/{ids.page_id}/picture", params={"redirect": "false", "width": width})
        )


async def test_picture_bytes_match_reported_dimensions(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    for params in ({"redirect": "false"}, {"redirect": "false", "width": "128", "height": "96"}):
        picture = (await client.get(f"{GRAPH}/{ids.page_id}/picture", params=params)).json()["data"]
        content = (await client.get(picture["url"])).content
        assert struct.unpack(">II", content[16:24]) == (picture["width"], picture["height"])
        redirect = await client.get(
            f"{GRAPH}/{ids.page_id}/picture", params={k: v for k, v in params.items() if k != "redirect"}
        )
        assert redirect.headers["location"] == picture["url"]


@pytest.mark.parametrize("version", ["v16.0", "v25.0", "v21.1", "vjunk"])
async def test_unsupported_versions_are_graph_errors(client: httpx.AsyncClient, version: str) -> None:
    error(await client.get(f"{GRAPH_HOST}/{version}/me", headers=USER))
    error(
        await client.post(f"{GRAPH_HOST}/{version}", headers=USER, json={"batch": [{"relative_url": "me"}]})
    )


async def test_seed_message_timestamps_are_deterministic_and_not_future() -> None:
    first = EmulatorHub(providers=["meta"], anchor="2026-10-16")
    second = EmulatorHub(providers=["meta"], anchor="2026-10-16")
    for collection in ("wa_messages", "ms_messages", "ms_conversations"):
        assert (
            first.context("meta").store.collection(collection).values()
            == second.context("meta").store.collection(collection).values()
        )
    anchor = int(first.world.anchor.timestamp())
    assert all(int(m["timestamp"]) <= anchor for m in first.context("meta").store.collection("wa_messages"))


async def test_webhook_retries_preserve_raw_body_and_signatures(
    client: httpx.AsyncClient, ids: SimpleNamespace
) -> None:
    seen: list[httpx.Request] = []

    async def endpoint(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=request.url.params["hub.challenge"])
        seen.append(request)
        return httpx.Response(503 if len(seen) < 3 else 200)

    client.hub.webhook_transport = httpx.MockTransport(endpoint)  # type: ignore[attr-defined]
    result = await action(
        client,
        "configure_webhook",
        {
            "object": "whatsapp_business_account",
            "callback_url": "http://consumer.test/hook",
            "verify_token": ids.verify_token,
        },
    )
    assert result["success"]
    await action(client, "inbound_whatsapp", {"from": "c011-4", "text": "retry 雪"})
    assert len(seen) == 3
    assert all(
        r.content == seen[0].content
        and r.headers["x-hub-signature-256"] == seen[0].headers["x-hub-signature-256"]
        for r in seen
    )
    journal = (await client.get(f"{GRAPH_HOST}/_emulator/webhooks")).json()["deliveries"]
    assert journal[-1]["attempts"] == 3 and journal[-1]["status"] == "delivered"


WRITE_PATHS = [
    ("POST", "phone/messages", "system_user"),
    ("POST", "phone/media", "system_user"),
    ("POST", "phone/whatsapp_business_profile", "system_user"),
    ("POST", "phone/register", "system_user"),
    ("POST", "phone/deregister", "system_user"),
    ("POST", "waba/message_templates", "system_user"),
    ("DELETE", "waba/message_templates", "system_user"),
    ("POST", "waba/subscribed_apps", "system_user"),
    ("DELETE", "waba/subscribed_apps", "system_user"),
    ("POST", "waba", "system_user"),
    ("POST", "page/messages", "page"),
    ("POST", "page/message_attachments", "page"),
    ("POST", "page/messenger_profile", "page"),
    ("DELETE", "page/messenger_profile", "page"),
    ("POST", "page/subscribed_apps", "page"),
    ("DELETE", "page/subscribed_apps", "page"),
    ("POST", "page", "page"),
    ("POST", "app/subscriptions", "app"),
    ("DELETE", "app/subscriptions", "app"),
    ("POST", "oauth/access_token", "system_user"),
    ("POST", "template", "system_user"),
    ("DELETE", "template", "system_user"),
    ("DELETE", "media", "system_user"),
    ("POST", "", "system_user"),
]


@pytest.mark.parametrize("method,path,kind", WRITE_PATHS)
@pytest.mark.parametrize("raw", [b"[]", b"true", b"1", b"null", b'"snow"', b"{", b"not-json", b"\xff"])
async def test_every_write_endpoint_rejects_malformed_root_bodies(
    client: httpx.AsyncClient, ids: SimpleNamespace, method: str, path: str, kind: str, raw: bytes
) -> None:
    mapping = {"phone": ids.phone_number_id, "waba": ids.waba_id, "page": ids.page_id, "app": ids.app_id}
    mapping["template"] = (await client.get(f"{GRAPH}/{ids.waba_id}/message_templates", headers=SYS)).json()[
        "data"
    ][0]["id"]
    mapping["media"] = await upload(client, ids)
    head, sep, tail = path.partition("/")
    path = mapping.get(head, head) + sep + tail
    token = (await mint(client, {"type": kind}))["access_token"]
    error(
        await client.request(
            method,
            f"{GRAPH}/{path}",
            headers={**auth(token), "Content-Type": "application/json"},
            content=raw,
        )
    )
