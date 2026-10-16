from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from tests.providers.stripe.conftest import get, post


async def customer_and_price(client: httpx.AsyncClient) -> tuple[dict, dict]:
    customer = await post(
        client, "customers", {"name": "Billing test", "email": "billing@halcyonadvisory.example"}
    )
    pm = await post(client, "payment_methods", {"type": "card", "card[token]": "tok_visa"})
    await post(client, f"payment_methods/{pm['id']}/attach", {"customer": customer["id"]})
    customer = await post(
        client, "customers/" + customer["id"], {"invoice_settings[default_payment_method]": pm["id"]}
    )
    price = (await get(client, "prices", params={"lookup_keys[]": "bookkeeping_monthly"}))["data"][0]
    return customer, price


async def test_subscription_paid_update_cancel_and_legacy_versions(client: httpx.AsyncClient) -> None:
    customer, price = await customer_and_price(client)
    subscription = await post(
        client,
        "subscriptions",
        {
            "customer": customer["id"],
            "items[0][price]": price["id"],
            "items[0][quantity]": 2,
            "expand[]": "latest_invoice.payments.data.payment",
        },
    )
    assert subscription["status"] == "active"
    invoice = subscription["latest_invoice"]
    assert invoice["status"] == "paid" and invoice["amount_paid"] == price["unit_amount"] * 2
    assert subscription["items"]["data"][0]["current_period_end"] > subscription["created"]
    assert "current_period_end" not in subscription
    old = await get(
        client,
        "subscriptions/" + subscription["id"],
        headers={"Stripe-Version": "2024-06-20"},
        params={"expand[]": "latest_invoice.payment_intent"},
    )
    assert old["current_period_end"]
    assert old["latest_invoice"]["payment_intent"]["status"] == "succeeded"
    assert "payments" not in old["latest_invoice"]
    item = subscription["items"]["data"][0]
    updated = await post(
        client,
        "subscriptions/" + subscription["id"],
        {
            "items[0][id]": item["id"],
            "items[0][quantity]": 3,
            "cancel_at_period_end": "true",
            "proration_behavior": "none",
        },
    )
    assert updated["items"]["data"][0]["quantity"] == 3 and updated["cancel_at_period_end"]
    item2 = await post(
        client,
        "subscription_items",
        {"subscription": subscription["id"], "price": price["id"], "quantity": 1},
    )
    assert (await post(client, "subscription_items/" + item2["id"], {"quantity": 2}))["quantity"] == 2
    assert (
        len((await get(client, "subscription_items", params={"subscription": subscription["id"]}))["data"])
        == 2
    )
    assert (await client.delete("/v1/subscription_items/" + item2["id"])).json()["deleted"]
    canceled = await client.delete("/v1/subscriptions/" + subscription["id"])
    assert canceled.json()["status"] == "canceled"
    assert not (await get(client, "subscriptions", params={"customer": customer["id"]}))["data"]
    assert (await get(client, "subscriptions", params={"customer": customer["id"], "status": "all"}))["data"][
        0
    ]["status"] == "canceled"


async def test_subscription_incomplete_and_trial(client: httpx.AsyncClient) -> None:
    customer = await post(client, "customers", {"name": "No card"})
    price = (await get(client, "prices", params={"lookup_keys[]": "bookkeeping_monthly"}))["data"][0]
    subscription = await post(
        client,
        "subscriptions",
        {
            "customer": customer["id"],
            "items[0][price]": price["id"],
            "payment_behavior": "default_incomplete",
            "expand[]": "latest_invoice.confirmation_secret",
        },
    )
    assert subscription["status"] == "incomplete"
    invoice = subscription["latest_invoice"]
    assert invoice["status"] == "open" and invoice["confirmation_secret"]["client_secret"].startswith("pi_")
    pi_id = invoice["payments"]["data"][0]["payment"]["payment_intent"]
    await post(client, f"payment_intents/{pi_id}/confirm", {"payment_method": "pm_card_visa"})
    assert (await get(client, "invoices/" + invoice["id"]))["status"] == "paid"
    assert (await get(client, "subscriptions/" + subscription["id"]))["status"] == "active"
    trial = await post(
        client,
        "subscriptions",
        {"customer": customer["id"], "items[0][price]": price["id"], "trial_period_days": 7},
    )
    assert trial["status"] == "trialing" and trial["trial_end"] - trial["trial_start"] == 604800
    assert (await get(client, "invoices/" + trial["latest_invoice"]))["amount_due"] == 0


async def test_invoice_lifecycle_lines_and_real_pdf(client: httpx.AsyncClient) -> None:
    customer, _ = await customer_and_price(client)
    invoice = await post(client, "invoices", {"customer": customer["id"], "description": "Consulting"})
    item = await post(
        client,
        "invoiceitems",
        {
            "customer": customer["id"],
            "invoice": invoice["id"],
            "amount": 5000,
            "currency": "usd",
            "description": "Services",
        },
    )
    assert item["invoice"] == invoice["id"]
    lines = await get(client, f"invoices/{invoice['id']}/lines")
    assert lines["data"][0]["amount"] == 5000
    invoice = await post(client, "invoices/" + invoice["id"] + "/finalize")
    assert (
        invoice["status"] == "open"
        and invoice["amount_due"] == 5000
        and invoice["number"].startswith("HALCYON-")
    )
    assert (await post(client, "invoices/" + invoice["id"] + "/send"))["status"] == "open"
    pdf = await client.get(invoice["invoice_pdf"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-1.4")
    assert pdf.headers["content-type"] == "application/pdf"
    assert (await client.get(invoice["hosted_invoice_url"])).status_code == 200
    paid = await post(client, "invoices/" + invoice["id"] + "/pay")
    assert paid["status"] == "paid" and paid["amount_remaining"] == 0
    assert paid["amount_paid"] == paid["total"]
    assert (await client.delete("/v1/invoices/" + invoice["id"])).status_code == 400


@pytest.mark.parametrize(("operation", "status"), [("void", "void"), ("mark_uncollectible", "uncollectible")])
async def test_invoice_unpaid_transitions(client: httpx.AsyncClient, operation: str, status: str) -> None:
    customer, _ = await customer_and_price(client)
    invoice = await post(client, "invoices", {"customer": customer["id"]})
    await post(
        client,
        "invoiceitems",
        {"customer": customer["id"], "invoice": invoice["id"], "amount": 1000, "currency": "usd"},
    )
    await post(client, "invoices/" + invoice["id"] + "/finalize")
    assert (await post(client, f"invoices/{invoice['id']}/{operation}"))["status"] == status


async def test_pending_invoice_items_preview_and_delete(client: httpx.AsyncClient) -> None:
    customer, _ = await customer_and_price(client)
    item = await post(client, "invoiceitems", {"customer": customer["id"], "amount": 1000, "currency": "usd"})
    await post(client, "invoiceitems/" + item["id"], {"description": "Pending"})
    invoice = await post(
        client, "invoices", {"customer": customer["id"], "pending_invoice_items_behavior": "include"}
    )
    assert invoice["amount_due"] == 1000
    assert (await client.delete("/v1/invoiceitems/" + item["id"])).json()["deleted"]
    assert (await get(client, "invoices/" + invoice["id"]))["amount_due"] == 0
    assert (await client.delete("/v1/invoices/" + invoice["id"])).json()["deleted"]
    preview = await post(client, "invoices/create_preview", {"customer": customer["id"]})
    assert preview["id"].startswith("upcoming_in_")


async def test_test_clock_renews_and_scheduled_cancellation(client: httpx.AsyncClient) -> None:
    now = int(datetime(2026, 10, 16, tzinfo=UTC).timestamp())
    clock = await post(client, "test_helpers/test_clocks", {"frozen_time": now, "name": "Renewal"})
    customer, price = await customer_and_price(client)
    # Test clock must be attached at creation.
    customer2 = await post(client, "customers", {"test_clock": clock["id"], "name": customer["name"]})
    pm = await post(client, "payment_methods", {"type": "card", "card[token]": "tok_visa"})
    await post(client, f"payment_methods/{pm['id']}/attach", {"customer": customer2["id"]})
    subscription = await post(
        client,
        "subscriptions",
        {"customer": customer2["id"], "default_payment_method": pm["id"], "items[0][price]": price["id"]},
    )
    end = subscription["items"]["data"][0]["current_period_end"]
    await post(client, f"test_helpers/test_clocks/{clock['id']}/advance", {"frozen_time": end + 1})
    renewed = await get(client, "subscriptions/" + subscription["id"])
    assert renewed["latest_invoice"] != subscription["latest_invoice"] and renewed["status"] == "active"
    invoices = await get(client, "invoices", params={"customer": customer2["id"]})
    assert len(invoices["data"]) == 2 and all(i["status"] == "paid" for i in invoices["data"])
    await post(client, "subscriptions/" + subscription["id"], {"cancel_at_period_end": "true"})
    await post(
        client,
        f"test_helpers/test_clocks/{clock['id']}/advance",
        {"frozen_time": renewed["items"]["data"][0]["current_period_end"] + 1},
    )
    assert (await get(client, "subscriptions/" + subscription["id"]))["status"] == "canceled"
    assert (await client.delete("/v1/test_helpers/test_clocks/" + clock["id"])).json()["deleted"]
