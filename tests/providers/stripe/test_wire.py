from __future__ import annotations

import asyncio
from datetime import timedelta
from time import perf_counter

import httpx
import pytest

from emulators.core.errors import EmulatorError
from emulators.core.hub import EmulatorHub
from emulators.providers.stripe.wire import PUBLIC_KEY, SECRET_KEY, decode
from tests.providers.stripe.conftest import get, post


def test_nested_form() -> None:
    assert decode(
        "metadata[order_id]=6735&items[0][price]=price_x&items[0][quantity]=2&items[1][price]=price_y&expand[]=customer&expand[]=latest_charge&address[line1]="
    ) == {
        "metadata": {"order_id": "6735"},
        "items": [{"price": "price_x", "quantity": "2"}, {"price": "price_y"}],
        "expand": ["customer", "latest_charge"],
        "address": {"line1": ""},
    }
    for malformed in ("items[0][a]=x&items[x]=y", "a[x=1", "a=1&a[b]=2", "items[10000]=x"):
        with pytest.raises(EmulatorError):
            decode(malformed)


def test_seed_fast_deterministic_and_world_projection() -> None:
    start = perf_counter()
    a = EmulatorHub(providers=["stripe"], anchor="2026-10-16")
    assert perf_counter() - start < 2
    b = EmulatorHub(providers=["stripe"], anchor="2026-10-16")
    assert a.context("stripe").store.snapshot() == b.context("stripe").store.snapshot()
    ctx = a.context("stripe")
    assert len(ctx.store.collection("products")) == len(ctx.world.products)
    assert len(ctx.store.collection("customers")) == len(ctx.world.card_customers)
    assert len(ctx.store.blobs) == 0
    for card in ctx.world.card_customers:
        customer = ctx.store.collection("customers").first(_client_id=card.client_id)
        assert customer["name"] == ctx.world.client(card.client_id).legal_name
        assert customer["email"] == ctx.world.person(card.contact_id).email
        pm = ctx.store.collection("payment_methods").get(
            customer["invoice_settings"]["default_payment_method"]
        )
        assert pm["card"]["last4"] == card.card_last4
    credit_payments = [p for p in ctx.world.payments if p.method == "Credit Card"]
    assert len(
        [p for p in ctx.store.collection("payment_intents") if p["metadata"].get("world_payment_id")]
    ) == len(credit_payments)


async def test_auth_headers_and_error_envelope(client: httpx.AsyncClient) -> None:
    for token in ("", "sk_live_1234", "nonsense"):
        response = await client.get("/v1/customers", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert response.headers["request-id"].startswith("req_")
    response = await client.get(
        "/v1/customers", auth=(SECRET_KEY, ""), headers={"Stripe-Version": "2024-06-20"}
    )
    assert response.status_code == 200
    assert response.headers["stripe-version"] == "2024-06-20"
    assert (
        await client.get("/v1/customers", headers={"Authorization": "Bearer rk_test_anything"})
    ).status_code == 200
    missing = await client.get("/v1/customers/cus_missing")
    assert missing.status_code == 404
    error = missing.json()["error"]
    assert error["code"] == "resource_missing" and error["param"] == "id"
    assert error["doc_url"].endswith("resource-missing")
    assert error["message"] == "No such customer: 'cus_missing'"
    assert error["request_log_url"].endswith(missing.headers["request-id"])
    assert (
        await client.get("/v1/customers", headers={"Authorization": "Bearer " + PUBLIC_KEY})
    ).status_code == 403
    assert (await client.get("/v1/customers", headers={"Stripe-Account": "acct_other"})).status_code == 403


async def test_metadata_unset_idempotency_and_concurrency(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub
) -> None:
    data = {"email": "checkout@halcyonadvisory.example", "metadata[order_id]": "6735"}

    async def create() -> httpx.Response:
        return await client.post("/v1/customers", data=data, headers={"Idempotency-Key": "create-customer"})

    first, second = await asyncio.gather(create(), create())
    assert first.content == second.content
    assert second.headers["idempotent-replayed"] == "true"
    assert first.headers["request-id"] == second.headers["request-id"]
    customer = first.json()
    assert (
        await post(
            client, "customers/" + customer["id"], {"metadata[order_id]": "", "metadata[other]": "yes"}
        )
    )["metadata"] == {"other": "yes"}
    assert (await post(client, "customers/" + customer["id"], {"metadata": ""}))["metadata"] == {}
    mismatch = await client.post(
        "/v1/customers",
        data={"email": "other@halcyonadvisory.example"},
        headers={"Idempotency-Key": "create-customer"},
    )
    assert mismatch.status_code == 400 and mismatch.json()["error"]["type"] == "idempotency_error"
    stripe_hub.clock.advance(timedelta(days=2))
    third = await create()
    assert third.json()["id"] != customer["id"]


async def test_pagination_search_expand_and_invalid_queries(client: httpx.AsyncClient) -> None:
    first = await get(client, "customers", params={"limit": 2})
    second = await get(client, "customers", params={"limit": 2, "starting_after": first["data"][-1]["id"]})
    assert first["object"] == "list" and first["has_more"]
    assert not {r["id"] for r in first["data"]} & {r["id"] for r in second["data"]}
    before = await get(client, "customers", params={"limit": 2, "ending_before": second["data"][0]["id"]})
    assert [r["id"] for r in before["data"]] == [r["id"] for r in first["data"]]
    for value in (0, 101, "invalid"):
        assert (await client.get("/v1/customers", params={"limit": value})).status_code == 400
    created = await post(
        client,
        "customers",
        {"email": "search@halcyonadvisory.example", "name": "Test Client", "metadata[order_id]": "6735"},
    )
    search = await get(
        client, "customers/search", params={"query": "metadata['order_id']:'6735' AND name~'client'"}
    )
    assert search["data"][0]["id"] == created["id"] and search["total_count"] == 1
    assert (
        await get(
            client,
            "customers/search",
            params={"query": "-email:'search@halcyonadvisory.example'", "limit": 1},
        )
    )["has_more"]
    page = await get(client, "customers/search", params={"query": "created>0", "limit": 1})
    next_page = await get(
        client, "customers/search", params={"query": "created>0", "page": page["next_page"], "limit": 1}
    )
    assert page["data"][0]["id"] != next_page["data"][0]["id"]
    for query in ("bad", "email:'x' AND", "created>1 OR created<2 AND created>0"):
        assert (await client.get("/v1/customers/search", params={"query": query})).status_code == 400
    rows = await get(client, "payment_intents", params={"limit": 1, "expand[]": "data.customer"})
    assert rows["data"][0]["customer"]["object"] == "customer"
    assert (await client.get("/v1/customers", params={"expand[]": "a.b.c.d.e"})).status_code == 400


async def test_control_plane_permissions_and_expiry(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub
) -> None:
    customers = stripe_hub.context("stripe").store.collection("customers").values()
    own, other = customers[:2]
    contact = stripe_hub.world.client(own["_client_id"]).primary_contact_id
    response = await client.post("/_emulator/tokens", json={"user": contact})
    auth = {"Authorization": "Bearer " + response.json()["access_token"]}
    assert (await client.get("/v1/customers/" + own["id"], headers=auth)).status_code == 200
    assert (await client.get("/v1/customers/" + other["id"], headers=auth)).status_code == 404
    visible = await client.get("/v1/customers", headers=auth)
    assert [r["id"] for r in visible.json()["data"]] == [own["id"]]
    assert (await client.get("/v1/balance", headers=auth)).status_code == 403
    own_pi = await post(
        client, "payment_intents", {"amount": 1000, "currency": "usd", "customer": own["id"]}, headers=auth
    )
    assert own_pi["customer"] == own["id"]
    assert (
        await client.post(
            "/v1/payment_intents",
            data={"amount": 1000, "currency": "usd", "customer": other["id"]},
            headers=auth,
        )
    ).status_code == 404
    token = (
        await client.post(
            "/_emulator/tokens", json={"user": "p025", "scopes": ["customers:read"], "expires_in": 5}
        )
    ).json()["access_token"]
    auth = {"Authorization": "Bearer " + token}
    assert (await client.get("/v1/customers", headers=auth)).status_code == 200
    assert (await client.post("/v1/customers", data={"name": "No"}, headers=auth)).status_code == 403
    stripe_hub.clock.advance(timedelta(seconds=6))
    assert (await client.get("/v1/customers", headers=auth)).status_code == 401
    blocked = (await client.post("/_emulator/tokens", json={"user": "p001"})).json()["access_token"]
    assert (
        await client.get("/v1/customers", headers={"Authorization": "Bearer " + blocked})
    ).status_code == 403


async def test_prefix_routing_reset_journal_and_faults(stripe_hub: EmulatorHub) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stripe_hub.gateway),
        base_url="http://localhost:44080/stripe",
        headers={"Authorization": "Bearer " + SECRET_KEY},
    ) as client:
        await client.post("/_emulator/faults", json={"path": "/v1/customers", "status": 429, "times": 1})
        response = await client.get("/v1/customers")
        assert response.status_code == 429 and response.headers["retry-after"] == "2"
        assert response.headers["request-id"]
        await post(client, "customers", {"name": "Test"})
        assert len((await get(client, "customers", params={"limit": 100}))["data"]) == 11
        journal = (await client.get("/_emulator/requests")).json()["requests"]
        assert journal[-1]["status"] == 200
        await client.post("/_emulator/reset")
        assert len((await get(client, "customers", params={"limit": 100}))["data"]) == 10
