"""Cross-resource contracts and less common lifecycle paths."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.providers.stripe.wire import SECRET_KEY, decode
from tests.providers.stripe.conftest import get, post
from tests.providers.stripe.test_billing import customer_and_price


@pytest.mark.parametrize(
    "resource",
    [
        "customers",
        "products",
        "prices",
        "payment_intents",
        "setup_intents",
        "charges",
        "refunds",
        "disputes",
        "subscriptions",
        "invoices",
        "invoiceitems",
        "payouts",
        "balance_transactions",
        "events",
        "webhook_endpoints",
        "files",
        "checkout/sessions",
        "test_helpers/test_clocks",
    ],
)
async def test_list_and_retrieve_wire_contract(client: httpx.AsyncClient, resource: str) -> None:
    result = await get(client, resource)
    assert result["object"] == "list" and result["url"] == "/v1/" + resource
    assert isinstance(result["has_more"], bool) and len(result["data"]) <= 10
    if result["data"]:
        row = result["data"][0]
        retrieved = await get(client, resource + "/" + row["id"])
        assert retrieved == row
        assert not any(key.startswith("_") for key in retrieved)


@pytest.mark.parametrize(
    "resource", ["customers", "products", "prices", "payment_intents", "charges", "invoices", "subscriptions"]
)
async def test_all_search_endpoints(client: httpx.AsyncClient, resource: str) -> None:
    result = await get(client, resource + "/search", params={"query": "created>0", "limit": 2})
    assert result["object"] == "search_result" and result["total_count"] > 0
    assert isinstance(result["has_more"], bool)
    assert result["next_page"] is not None


async def test_validation_is_atomic_and_idempotent_expired_grant_rejected(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub
) -> None:
    customer = await post(client, "customers", {"name": "Original"})
    response = await client.post(
        "/v1/customers/" + customer["id"],
        data={"name": "Changed", "invoice_settings[default_payment_method]": "pm_missing"},
    )
    assert response.status_code == 404
    assert (await get(client, "customers/" + customer["id"]))["name"] == "Original"
    token = (await client.post("/_emulator/tokens", json={"expires_in": 1})).json()["access_token"]
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "expiry"}
    assert (await client.post("/v1/customers", data={"name": "First"}, headers=headers)).status_code == 200
    stripe_hub.clock.advance(timedelta(seconds=2))
    assert (await client.post("/v1/customers", data={"name": "First"}, headers=headers)).status_code == 401


async def test_delegated_customer_payment_methods_and_invoice_lines(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub
) -> None:
    customer = stripe_hub.context("stripe").store.collection("customers").values()[0]
    contact = stripe_hub.world.client(customer["_client_id"]).primary_contact_id
    token = (await client.post("/_emulator/tokens", json={"user": contact})).json()["access_token"]
    auth = {"Authorization": "Bearer " + token}
    pm = await post(client, "payment_methods", {"card[token]": "tok_visa", "type": "card"}, headers=auth)
    attached = await post(
        client, "payment_methods/" + pm["id"] + "/attach", {"customer": customer["id"]}, headers=auth
    )
    assert attached["customer"] == customer["id"]
    invoice = await post(client, "invoices", {"customer": customer["id"]}, headers=auth)
    await post(
        client,
        "invoiceitems",
        {"customer": customer["id"], "invoice": invoice["id"], "amount": 5000, "currency": "usd"},
        headers=auth,
    )
    assert len((await get(client, f"invoices/{invoice['id']}/lines", headers=auth))["data"]) == 1


async def test_invoice_credit_is_consumed_once_and_legacy_source_charged(client: httpx.AsyncClient) -> None:
    customer = await post(client, "customers", {"source": "tok_visa", "balance": -1000})
    source = await post(
        client,
        f"customers/{customer['id']}/sources/{customer['default_source']}",
        {"name": "Card owner", "exp_month": 10},
    )
    assert source["name"] == "Card owner" and source["exp_month"] == 10
    invoice = await post(client, "invoices", {"customer": customer["id"]})
    item = await post(
        client,
        "invoiceitems",
        {"customer": customer["id"], "invoice": invoice["id"], "amount": 5000, "currency": "usd"},
    )
    await post(client, "invoiceitems/" + item["id"], {"amount": 6000})
    invoice = await post(client, "invoices/" + invoice["id"] + "/finalize")
    assert invoice["amount_due"] == 5000
    assert (await get(client, "customers/" + customer["id"]))["balance"] == 0
    assert (await post(client, "invoices/" + invoice["id"] + "/pay"))["amount_paid"] == 5000


async def test_proration_and_incomplete_expiration(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub
) -> None:
    customer, price = await customer_and_price(client)
    sub = await post(client, "subscriptions", {"customer": customer["id"], "items[0][price]": price["id"]})
    stripe_hub.clock.advance(timedelta(days=15))
    item_id = sub["items"]["data"][0]["id"]
    updated = await post(
        client,
        "subscriptions/" + sub["id"],
        {"items[0][id]": item_id, "items[0][quantity]": 2, "proration_behavior": "always_invoice"},
    )
    proration = await get(client, "invoices/" + updated["latest_invoice"])
    assert proration["status"] == "paid" and 0 < proration["amount_due"] < price["unit_amount"]
    customer2 = await post(client, "customers", {"name": "Incomplete"})
    incomplete = await post(
        client, "subscriptions", {"customer": customer2["id"], "items[0][price]": price["id"]}
    )
    await client.post("/_emulator/actions/advance_time", json={"seconds": 24 * 3600})
    assert (await get(client, "subscriptions/" + incomplete["id"]))["status"] == "incomplete_expired"


async def test_clock_uses_frozen_time_after_advance_and_trial_pause_resume(client: httpx.AsyncClient) -> None:
    clock = await post(client, "test_helpers/test_clocks", {"frozen_time": 1700000000})
    customer = await post(client, "customers", {"test_clock": clock["id"]})
    price = (await get(client, "prices", params={"lookup_keys[]": "bookkeeping_monthly"}))["data"][0]
    sub = await post(
        client,
        "subscriptions",
        {
            "customer": customer["id"],
            "items[0][price]": price["id"],
            "trial_period_days": 1,
            "trial_settings[end_behavior][missing_payment_method]": "pause",
        },
    )
    assert sub["created"] == clock["frozen_time"]
    await post(
        client, f"test_helpers/test_clocks/{clock['id']}/advance", {"frozen_time": sub["trial_end"] + 1}
    )
    assert (await get(client, "subscriptions/" + sub["id"]))["status"] == "paused"
    assert (await post(client, "subscriptions/" + sub["id"] + "/resume"))["status"] == "active"
    pi = await post(client, "payment_intents", {"customer": customer["id"], "amount": 100, "currency": "usd"})
    assert pi["created"] == sub["trial_end"] + 1


async def test_checkout_setup_subscription_and_prefix_links(stripe_hub: EmulatorHub) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stripe_hub.gateway),
        base_url="http://localhost:44080/stripe",
        headers={"Authorization": "Bearer " + SECRET_KEY},
    ) as client:
        price = (await get(client, "prices", params={"lookup_keys[]": "bookkeeping_monthly"}))["data"][0]
        for mode in ("setup", "subscription"):
            session = await post(
                client,
                "checkout/sessions",
                {"mode": mode, **({"line_items[0][price]": price["id"]} if mode == "subscription" else {})},
            )
            assert session["url"].startswith("http://localhost:44080/stripe/")
            response = await client.post(
                "/_emulator/actions/complete_checkout", json={"session": session["id"]}
            )
            assert response.status_code == 200, response.text
            completed = response.json()
            assert completed["status"] == "complete"
            if mode == "setup":
                assert (await get(client, "setup_intents/" + completed["setup_intent"]))[
                    "status"
                ] == "succeeded"
            else:
                sub = await get(client, "subscriptions/" + completed["subscription"])
                invoice = await get(client, "invoices/" + sub["latest_invoice"])
                assert invoice["invoice_pdf"].startswith("http://localhost:44080/stripe/")
                assert (await client.get(invoice["invoice_pdf"])).content.startswith(b"%PDF")


def test_form_metadata_numeric_keys_and_unindexed_objects() -> None:
    assert decode("metadata[123]=value") == {"metadata": {"123": "value"}}
    assert decode("items[][price]=one&items[][quantity]=2&items[][price]=two") == {
        "items": [{"price": "one", "quantity": "2"}, {"price": "two"}]
    }
