"""Unmodified official SDK requests routed through the gateway by emulator_intercept."""

from __future__ import annotations

import io
from collections.abc import Iterator

import httpx
import pytest
import stripe
from fastapi import FastAPI, Request

from emulators.core.hub import EmulatorHub
from emulators.providers.stripe.wire import SECRET_KEY

pytestmark = pytest.mark.sdk


@pytest.fixture(autouse=True)
def stripe_settings(emulator_intercept: object) -> Iterator[None]:
    old_key, old_version = stripe.api_key, stripe.api_version
    stripe.api_key = SECRET_KEY
    stripe.api_version = "2025-09-30.clover"
    yield
    stripe.api_key, stripe.api_version = old_key, old_version


def test_customers_crud_search_auto_paging_and_idempotency() -> None:
    customer = stripe.Customer.create(
        email="sdk@halcyonadvisory.example",
        name="SDK customer",
        metadata={"order_id": "6735"},
        idempotency_key="sdk-customer",
    )
    replay = stripe.Customer.create(
        email="sdk@halcyonadvisory.example",
        name="SDK customer",
        metadata={"order_id": "6735"},
        idempotency_key="sdk-customer",
    )
    assert replay.id == customer.id
    assert replay.last_response.headers["idempotent-replayed"] == "true"
    assert stripe.Customer.retrieve(customer.id).email == customer.email
    assert stripe.Customer.modify(
        customer.id, metadata={"order_id": "", "sdk": "yes"}
    ).metadata.to_dict() == {"sdk": "yes"}
    found = stripe.Customer.search(query="email:'sdk@halcyonadvisory.example'")
    assert found.data[0].id == customer.id
    customers = list(stripe.Customer.list(limit=2).auto_paging_iter())
    assert len(customers) >= 11 and len({c.id for c in customers}) == len(customers)
    assert stripe.Customer.delete(customer.id).deleted


def test_payment_intents_capture_refund_and_expansion() -> None:
    customer = stripe.Customer.create(email="sdk-payments@halcyonadvisory.example")
    pi = stripe.PaymentIntent.create(
        amount=2000,
        currency="usd",
        customer=customer.id,
        payment_method="pm_card_visa",
        capture_method="manual",
        confirm=True,
        metadata={"order": "one"},
    )
    assert pi.status == "requires_capture"
    pi = stripe.PaymentIntent.capture(
        pi.id, amount_to_capture=1800, expand=["latest_charge.balance_transaction", "customer"]
    )
    assert pi.status == "succeeded" and pi.customer.email == customer.email
    assert pi.latest_charge.balance_transaction.fee == 82
    refund = stripe.Refund.create(payment_intent=pi.id, amount=500)
    assert refund.amount == 500 and refund.status == "succeeded"
    assert stripe.Charge.retrieve(pi.latest_charge.id).amount_refunded == 500
    assert stripe.Refund.list(payment_intent=pi.id).data[0].id == refund.id


def test_sdk_exception_mapping() -> None:
    with pytest.raises(stripe.CardError) as declined:
        stripe.PaymentIntent.create(
            amount=1000, currency="usd", payment_method="pm_card_chargeDeclined", confirm=True
        )
    assert declined.value.code == "card_declined"
    assert declined.value.json_body["error"]["decline_code"] == "generic_decline"
    with pytest.raises(stripe.InvalidRequestError) as missing:
        stripe.Customer.retrieve("cus_missing")
    assert missing.value.http_status == 404 and missing.value.code == "resource_missing"
    with pytest.raises(stripe.AuthenticationError):
        stripe.Customer.list(api_key="sk_live_invalid")


def test_setup_and_payment_method_attach_detach() -> None:
    customer = stripe.Customer.create(email="sdk-setup@halcyonadvisory.example")
    token = stripe.Token.create(
        card={"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"}
    )
    pm = stripe.PaymentMethod.create(type="card", card={"token": token.id})
    setup = stripe.SetupIntent.create(customer=customer.id, payment_method=pm.id, confirm=True)
    assert setup.status == "succeeded"
    stripe.Customer.modify(customer.id, invoice_settings={"default_payment_method": pm.id})
    assert stripe.Customer.list_payment_methods(customer.id).data[0].card.last4 == "4242"
    assert stripe.PaymentMethod.detach(pm.id).customer is None


def test_products_prices_subscription_and_invoice_payment() -> None:
    customer = stripe.Customer.create(email="sdk-billing@halcyonadvisory.example")
    pm = stripe.PaymentMethod.create(type="card", card={"token": "tok_visa"})
    stripe.PaymentMethod.attach(pm.id, customer=customer.id)
    stripe.Customer.modify(customer.id, invoice_settings={"default_payment_method": pm.id})
    product = stripe.Product.create(name="SDK retainer")
    price = stripe.Price.create(
        product=product.id,
        unit_amount=1200,
        currency="usd",
        recurring={"interval": "month"},
        lookup_key="sdk_retainer",
    )
    assert stripe.Price.list(lookup_keys=["sdk_retainer"]).data[0].id == price.id
    subscription = stripe.Subscription.create(
        customer=customer.id, items=[{"price": price.id, "quantity": 2}], expand=["latest_invoice"]
    )
    assert subscription.status == "active" and subscription.latest_invoice.amount_paid == 2400
    item = subscription["items"].data[0]
    stripe.Subscription.modify(
        subscription.id, items=[{"id": item.id, "quantity": 3}], proration_behavior="none"
    )
    invoice = stripe.Invoice.create(customer=customer.id)
    stripe.InvoiceItem.create(
        customer=customer.id, invoice=invoice.id, amount=5000, currency="usd", description="Services"
    )
    invoice = stripe.Invoice.finalize_invoice(invoice.id)
    assert invoice.status == "open" and invoice.amount_due == 5000
    invoice = stripe.Invoice.pay(invoice.id)
    assert invoice.status == "paid" and invoice.amount_remaining == 0
    assert stripe.Invoice.list_lines(invoice.id).data[0].amount == 5000
    assert stripe.Subscription.cancel(subscription.id).status == "canceled"


def test_checkout_webhooks_files_balance_and_events() -> None:
    product = stripe.Product.create(name="SDK checkout")
    price = stripe.Price.create(product=product.id, unit_amount=500, currency="usd")
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": price.id, "quantity": 2}],
        success_url="https://halcyonadvisory.example/success",
        cancel_url="https://halcyonadvisory.example/cancel",
    )
    assert session.amount_total == 1000
    assert stripe.checkout.Session.list_line_items(session.id).data[0].quantity == 2
    assert stripe.checkout.Session.expire(session.id).status == "expired"
    endpoint = stripe.WebhookEndpoint.create(
        url="https://consumer.example/webhook", enabled_events=["invoice.paid"]
    )
    assert endpoint.secret.startswith("whsec_")
    assert stripe.WebhookEndpoint.modify(endpoint.id, disabled=True).status == "disabled"
    assert stripe.WebhookEndpoint.delete(endpoint.id).deleted
    upload = io.BytesIO(b"%PDF-1.4 evidence")
    upload.name = "evidence.pdf"
    file = stripe.File.create(purpose="dispute_evidence", file=upload)
    assert stripe.File.retrieve(file.id).purpose == "dispute_evidence"
    assert stripe.Balance.retrieve().available[0].currency == "usd"
    assert stripe.Event.retrieve(stripe.Event.list(limit=1).data[0].id).object == "event"


def test_modern_stripe_client_and_test_clock() -> None:
    client = stripe.StripeClient(SECRET_KEY)
    customer = client.v1.customers.create({"email": "client@halcyonadvisory.example"})
    assert client.v1.customers.retrieve(customer.id).email == customer.email
    clock = client.v1.test_helpers.test_clocks.create({"frozen_time": 1792108800, "name": "SDK clock"})
    assert clock.status == "ready"
    assert (
        client.v1.test_helpers.test_clocks.advance(clock.id, {"frozen_time": 1792195200}).frozen_time
        == 1792195200
    )
    assert client.v1.test_helpers.test_clocks.delete(clock.id).deleted


def test_webhook_signature_verified_by_official_sdk(emulators: EmulatorHub) -> None:
    received: list[tuple[bytes, str]] = []
    receiver = FastAPI()

    @receiver.post("/stripe")
    async def hook(request: Request) -> dict:
        received.append((await request.body(), request.headers["stripe-signature"]))
        return {}

    emulators.webhook_transport = httpx.ASGITransport(app=receiver)
    endpoint = stripe.WebhookEndpoint.create(
        url="https://consumer.example/stripe", enabled_events=["customer.created"]
    )
    customer = stripe.Customer.create(email="signed-webhook@halcyonadvisory.example")
    payload, signature = received[-1]
    event = stripe.Webhook.construct_event(payload, signature, endpoint.secret)
    assert event.type == "customer.created" and event.data.object.id == customer.id
    stripe.WebhookEndpoint.delete(endpoint.id)
    emulators.webhook_transport = None


def test_connect_oauth_read_only_and_refresh(emulators: EmulatorHub) -> None:
    ctx = emulators.context("stripe")
    code = ctx.tokens.issue_code(
        client_id="ca_sdk",
        user_id=ctx.world.org.stripe_account_id,
        scopes=["read_only"],
        redirect_uri="https://halcyonadvisory.example/oauth",
    )
    token = stripe.OAuth.token(grant_type="authorization_code", code=code.token, client_secret=SECRET_KEY)
    assert token.stripe_user_id == ctx.world.org.stripe_account_id
    assert stripe.Customer.list(api_key=token.access_token, limit=1).data
    with pytest.raises(stripe.PermissionError):
        stripe.Customer.create(api_key=token.access_token, name="Denied")
    refreshed = stripe.OAuth.token(
        grant_type="refresh_token", refresh_token=token.refresh_token, client_secret=SECRET_KEY
    )
    with pytest.raises(stripe.PermissionError):
        stripe.Customer.create(api_key=refreshed.access_token, name="Denied")
    stripe.OAuth.deauthorize(client_id="ca_sdk", stripe_user_id=ctx.world.org.stripe_account_id)
    with pytest.raises(stripe.AuthenticationError):
        stripe.Customer.list(api_key=refreshed.access_token)
