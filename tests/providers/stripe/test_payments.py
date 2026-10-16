from __future__ import annotations

import httpx
import pytest

from emulators.providers.stripe.wire import PUBLIC_KEY
from tests.providers.stripe.conftest import get, post


async def test_success_manual_capture_increment_and_refund(client: httpx.AsyncClient) -> None:
    pi = await post(
        client,
        "payment_intents",
        {
            "amount": 2000,
            "currency": "usd",
            "payment_method": "pm_card_visa",
            "confirm": "true",
            "capture_method": "manual",
            "expand[]": "latest_charge",
        },
    )
    assert pi["status"] == "requires_capture" and pi["amount_capturable"] == 2000
    assert not pi["latest_charge"]["captured"]
    pi = await post(client, f"payment_intents/{pi['id']}/increment_authorization", {"amount": 2500})
    assert pi["amount"] == 2500
    pi = await post(
        client,
        f"payment_intents/{pi['id']}/capture",
        {"amount_to_capture": 2000, "expand[]": "latest_charge.balance_transaction"},
    )
    assert pi["status"] == "succeeded" and pi["amount_received"] == 2000 and pi["amount_capturable"] == 0
    txn = pi["latest_charge"]["balance_transaction"]
    assert txn["fee"] == 88 and txn["net"] == 1912
    assert (await client.post(f"/v1/payment_intents/{pi['id']}/capture")).status_code == 400
    refund = await post(
        client, "refunds", {"payment_intent": pi["id"], "amount": 500, "reason": "requested_by_customer"}
    )
    assert refund["status"] == "succeeded" and refund["amount"] == 500
    final = await post(client, "refunds", {"charge": pi["latest_charge"]["id"]})
    assert final["amount"] == 1500
    charge = await get(client, "charges/" + pi["latest_charge"]["id"])
    assert charge["refunded"] and charge["amount_refunded"] == 2000
    assert len((await get(client, f"charges/{charge['id']}/refunds"))["data"]) == 2
    assert (await client.post("/v1/refunds", data={"charge": charge["id"]})).status_code == 400


@pytest.mark.parametrize(
    ("number", "code", "decline"),
    [
        ("4000000000000002", "card_declined", "generic_decline"),
        ("4000000000009995", "card_declined", "insufficient_funds"),
        ("4000000000009987", "card_declined", "lost_card"),
        ("4000000000000069", "expired_card", None),
        ("4000000000000127", "incorrect_cvc", None),
        ("4000000000000119", "processing_error", None),
        ("4000000000000341", "card_declined", "generic_decline"),
    ],
)
async def test_declines_preserve_intent_state(
    client: httpx.AsyncClient, number: str, code: str, decline: str | None
) -> None:
    pm = await post(
        client,
        "payment_methods",
        {"type": "card", "card[number]": number, "card[exp_month]": 12, "card[exp_year]": 2030},
    )
    response = await client.post(
        "/v1/payment_intents",
        data={"amount": 1000, "currency": "usd", "payment_method": pm["id"], "confirm": "true"},
        headers={"Idempotency-Key": "decline"},
    )
    assert response.status_code == 402
    error = response.json()["error"]
    assert error["type"] == "card_error" and error["code"] == code
    assert error.get("decline_code") == decline
    pi = await get(client, "payment_intents/" + error["payment_intent"]["id"])
    assert pi["status"] == "requires_payment_method" and pi["amount_received"] == 0
    assert pi["last_payment_error"]["code"] == code and pi["latest_charge"]
    recovered = await post(client, f"payment_intents/{pi['id']}/confirm", {"payment_method": "pm_card_visa"})
    assert recovered["status"] == "succeeded" and recovered["last_payment_error"] is None


@pytest.mark.parametrize("card", ["4000002500003155", "4000002760003184"])
async def test_three_ds_publishable_and_completion(client: httpx.AsyncClient, card: str) -> None:
    pm = await post(
        client,
        "payment_methods",
        {"type": "card", "card[number]": card, "card[exp_month]": 12, "card[exp_year]": 2030},
        headers={"Authorization": "Bearer " + PUBLIC_KEY},
    )
    pi = await post(
        client, "payment_intents", {"amount": 1200, "currency": "usd", "payment_method": pm["id"]}
    )
    response = await client.post(
        f"/v1/payment_intents/{pi['id']}/confirm",
        data={"client_secret": pi["client_secret"]},
        headers={"Authorization": "Bearer " + PUBLIC_KEY},
    )
    assert response.status_code == 200 and response.json()["status"] == "requires_action"
    assert response.json()["next_action"]["type"] == "use_stripe_sdk"
    assert (
        await client.get(
            f"/v1/payment_intents/{pi['id']}",
            params={"client_secret": "wrong"},
            headers={"Authorization": "Bearer " + PUBLIC_KEY},
        )
    ).status_code == 400
    response = await client.post("/_emulator/actions/complete_3ds", json={"payment_intent": pi["id"]})
    assert response.json()["status"] == "succeeded" and response.json()["next_action"] is None


async def test_setup_attach_detach_tokens_and_customer_boundaries(client: httpx.AsyncClient) -> None:
    customer = await post(client, "customers", {"name": "Card owner"})
    token = await post(
        client,
        "tokens",
        {"card[number]": "4242424242424242", "card[exp_month]": 12, "card[exp_year]": 2030},
        headers={"Authorization": "Bearer " + PUBLIC_KEY},
    )
    pm = await post(client, "payment_methods", {"type": "card", "card[token]": token["id"]})
    setup = await post(
        client, "setup_intents", {"customer": customer["id"], "payment_method": pm["id"], "confirm": "true"}
    )
    assert setup["status"] == "succeeded"
    assert (await get(client, "payment_methods/" + pm["id"]))["customer"] == customer["id"]
    assert len((await get(client, f"customers/{customer['id']}/payment_methods"))["data"]) == 1
    other = await post(client, "customers", {"name": "Other"})
    assert (
        await client.post(
            "/v1/payment_intents",
            data={"amount": 2000, "currency": "usd", "customer": other["id"], "payment_method": pm["id"]},
        )
    ).status_code == 400
    detached = await post(client, f"payment_methods/{pm['id']}/detach")
    assert detached["customer"] is None
    assert (
        await client.post(f"/v1/payment_methods/{pm['id']}/attach", data={"customer": customer["id"]})
    ).status_code == 400
    assert (
        await client.post("/v1/payment_methods", data={"type": "card", "card[token]": token["id"]})
    ).status_code == 400
    intent = await post(client, "setup_intents", {"customer": customer["id"]})
    assert (await post(client, f"setup_intents/{intent['id']}/cancel"))["status"] == "canceled"


async def test_disputed_and_failed_refund_cards(client: httpx.AsyncClient) -> None:
    disputed = await post(
        client,
        "payment_intents",
        {"amount": 3000, "currency": "usd", "payment_method": "pm_card_createDispute", "confirm": "true"},
    )
    disputes = await get(client, "disputes", params={"payment_intent": disputed["id"]})
    dispute = disputes["data"][0]
    assert dispute["status"] == "needs_response"
    updated = await post(
        client, "disputes/" + dispute["id"], {"evidence[customer_name]": "Test", "submit": "false"}
    )
    assert updated["evidence_details"]["has_evidence"]
    assert (await post(client, "disputes/" + dispute["id"] + "/close"))["status"] == "lost"
    pi = await post(
        client,
        "payment_intents",
        {"amount": 3000, "currency": "usd", "payment_method": "pm_card_refundFail", "confirm": "true"},
    )
    refund = await post(client, "refunds", {"payment_intent": pi["id"]})
    assert refund["status"] == "failed" and refund["failure_reason"] == "declined"
    assert not (await get(client, "charges/" + pi["latest_charge"]))["refunded"]


async def test_intent_updates_cancel_and_charge_metadata(client: httpx.AsyncClient) -> None:
    pi = await post(client, "payment_intents", {"amount": 1500, "currency": "usd"})
    assert pi["status"] == "requires_payment_method"
    pi = await post(
        client,
        "payment_intents/" + pi["id"],
        {"amount": 2500, "description": "Invoice", "payment_method": "pm_card_mastercard"},
    )
    assert pi["status"] == "requires_confirmation" and pi["amount"] == 2500
    canceled = await post(
        client, f"payment_intents/{pi['id']}/cancel", {"cancellation_reason": "requested_by_customer"}
    )
    assert canceled["status"] == "canceled" and canceled["canceled_at"]
    assert (await client.post(f"/v1/payment_intents/{pi['id']}/confirm")).status_code == 400
    paid = await post(
        client,
        "payment_intents",
        {"amount": 1500, "currency": "usd", "payment_method": "pm_card_visa", "confirm": "true"},
    )
    charge = await post(client, "charges/" + paid["latest_charge"], {"metadata[order]": "1"})
    assert charge["metadata"] == {"order": "1"}
    assert (await get(client, "charges", params={"payment_intent": paid["id"]}))["data"][0]["id"] == charge[
        "id"
    ]
