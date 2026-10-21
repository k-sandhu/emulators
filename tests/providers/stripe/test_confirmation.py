"""Confirmation parameters must affect the payment attempt and its persisted side effects."""

from __future__ import annotations

import copy

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.providers.stripe.wire import PUBLIC_KEY
from tests.providers.stripe.conftest import get, post


@pytest.mark.parametrize("create_and_confirm", [False, True])
@pytest.mark.parametrize("flag", ["error_on_requires_action", "off_session"])
@pytest.mark.parametrize("card", ["pm_card_threeDSecure2Required", "pm_card_threeDSecureRequired"])
async def test_authentication_required_failure_is_persisted_and_replayed(
    client: httpx.AsyncClient, stripe_hub: EmulatorHub, create_and_confirm: bool, flag: str, card: str
) -> None:
    ctx = stripe_hub.context("stripe")
    intent = await post(
        client,
        "payment_intents",
        {"amount": 1200, "currency": "usd", "payment_method": card},
    )
    path = "payment_intents" if create_and_confirm else f"payment_intents/{intent['id']}/confirm"
    data = {flag: "true"}
    if create_and_confirm:
        data.update(amount="1200", currency="usd", payment_method=intent["payment_method"], confirm="true")
    headers = {"Idempotency-Key": "authentication-failure"}
    before_transactions = len(ctx.store.collection("balance_transactions"))
    response = await client.post("/v1/" + path, data=data, headers=headers)
    assert response.status_code == 402, response.text
    error = response.json()["error"]
    assert error["type"] == "card_error" and error["code"] == "authentication_required"
    failed = error["payment_intent"]
    assert failed["status"] == "requires_payment_method"
    assert failed["next_action"] is None and failed["amount_received"] == 0
    assert failed["last_payment_error"]["code"] == "authentication_required"
    assert (await get(client, f"payment_intents/{failed['id']}"))["status"] == "requires_payment_method"
    charge = await get(client, "charges/" + failed["latest_charge"])
    assert charge["status"] == "failed" and not charge["paid"] and not charge["captured"]
    assert charge["failure_code"] == "authentication_required"
    assert charge["balance_transaction"] is None
    assert len(ctx.store.collection("balance_transactions")) == before_transactions
    events = [
        event for event in ctx.store.collection("events") if event["data"]["object"]["id"] == failed["id"]
    ]
    assert events[-1]["type"] == "payment_intent.payment_failed"
    assert all(event["type"] != "payment_intent.requires_action" for event in events)
    counts = {name: len(ctx.store.collection(name)) for name in ("events", "charges", "payment_intents")}
    replay = await client.post("/v1/" + path, data=data, headers=headers)
    assert replay.status_code == 402 and replay.json() == response.json()
    assert replay.headers["idempotent-replayed"] == "true"
    assert counts == {name: len(ctx.store.collection(name)) for name in counts}
    recovered = await post(
        client, f"payment_intents/{failed['id']}/confirm", {"payment_method": "pm_card_visa"}
    )
    assert recovered["status"] == "succeeded" and recovered["last_payment_error"] is None
    assert (await get(client, "charges/" + charge["id"])) == charge


@pytest.mark.parametrize("flag", ["error_on_requires_action", "off_session"])
async def test_false_authentication_flags_allow_customer_action(client: httpx.AsyncClient, flag: str) -> None:
    intent = await post(
        client,
        "payment_intents",
        {
            "amount": 1200,
            "currency": "usd",
            "payment_method": "pm_card_threeDSecure2Required",
            "confirm": "true",
            flag: "false",
        },
    )
    assert intent["status"] == "requires_action" and intent["next_action"]


async def test_confirm_parameters_propagate_to_intent_charge_and_attachment(
    client: httpx.AsyncClient,
) -> None:
    customer = await post(client, "customers")
    intent = await post(
        client, "payment_intents", {"amount": 1400, "currency": "usd", "customer": customer["id"]}
    )
    confirmed = await post(
        client,
        f"payment_intents/{intent['id']}/confirm",
        {
            "payment_method": "pm_card_visa",
            "capture_method": "manual",
            "receipt_email": "billing@halcyonadvisory.example",
            "setup_future_usage": "off_session",
            "shipping[name]": "Halcyon Advisory Group",
            "shipping[address][line1]": "123 Example Street",
            "shipping[address][country]": "CA",
        },
    )
    assert confirmed["status"] == "requires_capture" and confirmed["amount_capturable"] == 1400
    assert confirmed["capture_method"] == "manual" and confirmed["setup_future_usage"] == "off_session"
    charge = await get(client, "charges/" + confirmed["latest_charge"])
    assert charge["receipt_email"] == confirmed["receipt_email"] == "billing@halcyonadvisory.example"
    assert charge["shipping"] == confirmed["shipping"]
    assert charge["shipping"]["address"]["line1"] == "123 Example Street"
    assert not charge["captured"] and charge["balance_transaction"] is None
    assert (await get(client, "payment_methods/" + confirmed["payment_method"]))["customer"] == customer["id"]
    captured = await post(client, f"payment_intents/{intent['id']}/capture")
    assert captured["status"] == "succeeded" and captured["amount_received"] == 1400


@pytest.mark.parametrize(
    "original,requested", [("off_session", "on_session"), ("off_session", ""), ("on_session", "")]
)
async def test_publishable_confirmation_cannot_reduce_future_usage(
    client: httpx.AsyncClient, original: str, requested: str
) -> None:
    intent = await post(
        client,
        "payment_intents",
        {
            "amount": 1400,
            "currency": "usd",
            "payment_method": "pm_card_visa",
            "setup_future_usage": original,
        },
    )
    response = await client.post(
        f"/v1/payment_intents/{intent['id']}/confirm",
        data={"client_secret": intent["client_secret"], "setup_future_usage": requested},
        headers={"Authorization": "Bearer " + PUBLIC_KEY},
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "setup_future_usage"
    assert (await get(client, "payment_intents/" + intent["id"])) == intent


@pytest.mark.parametrize(
    "original,requested", [("", "on_session"), ("on_session", "off_session"), ("off_session", "off_session")]
)
async def test_publishable_confirmation_accepts_permitted_future_usage(
    client: httpx.AsyncClient, original: str, requested: str
) -> None:
    customer = await post(client, "customers")
    params = {"amount": 1400, "currency": "usd", "customer": customer["id"], "payment_method": "pm_card_visa"}
    if original:
        params["setup_future_usage"] = original
    intent = await post(client, "payment_intents", params)
    confirmed = await post(
        client,
        f"payment_intents/{intent['id']}/confirm",
        {"client_secret": intent["client_secret"], "setup_future_usage": requested},
        headers={"Authorization": "Bearer " + PUBLIC_KEY},
    )
    assert confirmed["status"] == "succeeded" and confirmed["setup_future_usage"] == requested
    assert (await get(client, "payment_methods/" + intent["payment_method"]))["customer"] == customer["id"]


async def test_manual_authentication_needs_secret_reconfirmation(client: httpx.AsyncClient) -> None:
    intent = await post(
        client,
        "payment_intents",
        {
            "amount": 1600,
            "currency": "usd",
            "payment_method": "pm_card_threeDSecure2Required",
            "confirmation_method": "manual",
            "confirm": "true",
        },
    )
    assert intent["status"] == "requires_action"
    completed = await client.post("/_emulator/actions/complete_3ds", json={"payment_intent": intent["id"]})
    assert completed.status_code == 200 and completed.json()["status"] == "requires_confirmation"
    response = await client.post(
        f"/v1/payment_intents/{intent['id']}/confirm",
        data={"client_secret": intent["client_secret"]},
        headers={"Authorization": "Bearer " + PUBLIC_KEY},
    )
    assert response.status_code == 403
    paid = await post(client, f"payment_intents/{intent['id']}/confirm", {"error_on_requires_action": "true"})
    assert paid["status"] == "succeeded" and paid["next_action"] is None


async def test_declined_confirmation_does_not_rewrite_previous_charge_shipping(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/payment_intents",
        data={
            "amount": 1600,
            "currency": "usd",
            "payment_method": "pm_card_chargeDeclined",
            "confirm": "true",
            "shipping[name]": "Halcyon Advisory Group",
            "shipping[address][line1]": "123 Example Street",
        },
    )
    assert response.status_code == 402
    intent = response.json()["error"]["payment_intent"]
    charge = await get(client, "charges/" + intent["latest_charge"])
    original_shipping = copy.deepcopy(charge["shipping"])
    await post(client, "payment_intents/" + intent["id"], {"shipping[address][line1]": "456 Example Street"})
    assert (await get(client, "charges/" + charge["id"]))["shipping"] == original_shipping
