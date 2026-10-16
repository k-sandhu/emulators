"""Deterministic projection of Halcyon's card customers, catalog, and payment history."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

from emulators.providers.stripe.billing import period_end
from emulators.providers.stripe.commerce import CommerceAPI
from emulators.providers.stripe.wire import VERSION, Record, clean


def seed(api: CommerceAPI) -> None:
    ctx = api.ctx
    anchor = int(ctx.world.anchor.timestamp())
    products: dict[str, Record] = {}
    prices: dict[str, Record] = {}
    customers: dict[str, Record] = {}
    methods: dict[str, Record] = {}
    for world_product in ctx.world.products:
        product = api.make_product(
            {
                "name": world_product.name,
                "description": world_product.description,
                "metadata": {"world_product_id": world_product.id},
            }
        )
        price = api.make_price(
            {
                "product": product["id"],
                "unit_amount": world_product.unit_amount_cents,
                "currency": ctx.world.org.currency.lower(),
                "lookup_key": world_product.lookup_key,
                **({"recurring": {"interval": world_product.interval}} if world_product.interval else {}),
            }
        )
        product["default_price"] = price["id"]
        products[world_product.id], prices[world_product.id] = product, price

    def customer_for(client_id: str, created: int) -> Record:
        if client_id in customers:
            return customers[client_id]
        client = ctx.world.client(client_id)
        contact = ctx.world.person(client.primary_contact_id)
        api.now = created
        customer = api.make_customer(
            {
                "name": client.legal_name,
                "email": contact.email,
                "phone": client.phone,
                "address": {
                    "line1": client.address.street,
                    "line2": None,
                    "city": client.address.city,
                    "state": client.address.state,
                    "postal_code": client.address.postal_code,
                    "country": client.address.country_code,
                },
                "metadata": {"client_id": client.id, "quickbooks_customer": client.id},
            }
        )
        customer["_client_id"] = client_id
        customers[client_id] = customer
        return customer

    def history_invoice(subscription: Record, status: str = "paid") -> Record:
        nonlocal invoice_number
        invoice = api.make_invoice(
            {
                "customer": subscription["customer"],
                "currency": subscription["currency"],
                "default_payment_method": subscription["default_payment_method"],
            },
            subscription=subscription,
        )
        invoice_number += 1
        invoice.update(
            status="open",
            number=f"HALCYON-{invoice_number:04d}",
            billing_reason="subscription_cycle",
            hosted_invoice_url=api.url(
                "invoice.stripe.com", f"/i/{ctx.world.org.stripe_account_id}/test_{invoice['id']}"
            ),
            invoice_pdf=api.url("pay.stripe.com", f"/invoice/{invoice['id']}/pdf"),
        )
        invoice["status_transitions"]["finalized_at"] = api.now
        pi = api.create_intent(
            {
                "amount": invoice["amount_due"],
                "currency": invoice["currency"],
                "customer": subscription["customer"],
                "payment_method": subscription["default_payment_method"],
                "metadata": {"invoice": invoice["id"]},
            }
        )
        pm = methods[subscription["customer"]]
        charge = api.make_charge(pi, pm, success=status == "paid")
        pi.update(
            latest_charge=charge["id"],
            status="succeeded" if status == "paid" else "requires_payment_method",
            amount_received=pi["amount"] if status == "paid" else 0,
        )
        invoice.update(
            attempt_count=1,
            attempted=True,
            _payment_intent=pi["id"],
            confirmation_secret={"type": "payment_intent", "client_secret": pi["client_secret"]},
        )
        payment = api.new(
            "invoice_payments",
            amount_paid=pi["amount"] if status == "paid" else None,
            amount_requested=pi["amount"],
            currency=pi["currency"],
            invoice=invoice["id"],
            is_default=True,
            payment={"type": "payment_intent", "payment_intent": pi["id"]},
            status="paid" if status == "paid" else "open",
            status_transitions={"paid_at": api.now if status == "paid" else None, "canceled_at": None},
        )
        payment.pop("metadata", None)
        invoice["payments"].update(data=[payment], total_count=1)
        if status == "paid":
            invoice.update(status="paid", amount_paid=invoice["amount_due"], amount_remaining=0)
            invoice["status_transitions"]["paid_at"] = api.now
        else:
            pi["last_payment_error"] = {
                "type": "card_error",
                "code": "card_declined",
                "decline_code": "insufficient_funds",
                "message": "Your card has insufficient funds.",
                "payment_method": clean(pm),
            }
            invoice["next_payment_attempt"] = anchor + 86400
        subscription["latest_invoice"] = invoice["id"]
        return invoice

    invoice_number = 0
    for card_customer in ctx.world.card_customers:
        customer = customer_for(card_customer.client_id, int(card_customer.created.timestamp()))
        pm = api.create_payment_method(
            {
                "card": {
                    "number": card_customer.test_card_number,
                    "exp_month": card_customer.card_exp_month,
                    "exp_year": card_customer.card_exp_year,
                },
                "billing_details": {
                    "name": customer["name"],
                    "email": customer["email"],
                    "address": customer["address"],
                },
            }
        )
        pm["customer"] = customer["id"]
        customer["invoice_settings"]["default_payment_method"] = pm["id"]
        methods[customer["id"]] = pm
        if not card_customer.subscription_product_id:
            continue
        price = prices[card_customer.subscription_product_id]
        start = int(card_customer.subscription_started.timestamp())
        api.now = start
        subscription = api.make_subscription(
            {
                "customer": customer["id"],
                "default_payment_method": pm["id"],
                "items": [{"price": price["id"]}],
            }
        )
        final_status = card_customer.subscription_status
        item = subscription["items"]["data"][0]
        end_anchor = anchor - 30 * 86400 if final_status == "canceled" else anchor
        iterations = 0
        while start <= end_anchor and iterations < 100:
            end = period_end(start, price["recurring"])
            item.update(current_period_start=start, current_period_end=end)
            api.now = start
            latest = end > end_anchor
            history_invoice(subscription, "failed" if latest and final_status == "past_due" else "paid")
            if latest:
                break
            start, iterations = end, iterations + 1
        subscription["status"] = final_status
        if final_status == "canceled":
            subscription.update(canceled_at=end_anchor, ended_at=end_anchor)
            subscription["cancellation_details"]["reason"] = "cancellation_requested"
        if final_status == "trialing":
            subscription.update(trial_start=anchor - 7 * 86400, trial_end=anchor + 7 * 86400)
            item.update(current_period_start=anchor - 7 * 86400, current_period_end=anchor + 7 * 86400)

    for payment in ctx.world.payments:
        if payment.method != "Credit Card":
            continue
        stamp = int(datetime.combine(payment.date, datetime.min.time(), UTC).timestamp())
        customer = customer_for(payment.client_id, stamp - 86400)
        api.now = stamp
        pm = methods.get(customer["id"])
        if pm is None:
            pm = api.create_payment_method({"card": {"token": "tok_visa"}})
            pm["customer"] = customer["id"]
            methods[customer["id"]] = pm
            customer["invoice_settings"]["default_payment_method"] = pm["id"]
        invoice = next(i for i in ctx.world.invoices if i.id == payment.invoice_id)
        pi = api.create_intent(
            {
                "amount": round(payment.amount * 100),
                "currency": ctx.world.org.currency.lower(),
                "customer": customer["id"],
                "payment_method": pm["id"],
                "metadata": {"invoice_number": invoice.number, "world_payment_id": payment.id},
            }
        )
        charge = api.make_charge(pi, pm)
        pi.update(status="succeeded", amount_received=pi["amount"], latest_charge=charge["id"])

    paid_charges = [c for c in ctx.store.collection("charges") if c["paid"]]
    for charge in paid_charges[-2:]:
        api.now = min(anchor - 2 * 86400, charge["created"] + 86400)
        amount = min(10000, charge["amount_captured"])
        refund = api.new(
            "refunds",
            amount=amount,
            balance_transaction=None,
            charge=charge["id"],
            currency=charge["currency"],
            description=None,
            payment_intent=charge["payment_intent"],
            reason="requested_by_customer",
            receipt_number=None,
            source_transfer_reversal=None,
            status="succeeded",
            transfer_reversal=None,
        )
        refund["balance_transaction"] = api.transaction(-amount, charge["currency"], refund["id"], "refund")[
            "id"
        ]
        charge.update(amount_refunded=amount, refunded=amount == charge["amount_captured"])
        charge["refunds"]["data"].append(copy.deepcopy(refund))
    if paid_charges:
        api.now = anchor - 3 * 86400
        api.make_dispute(paid_charges[-1])
    for week in range(8, 0, -1):
        api.now = anchor - week * 7 * 86400
        payout = api.new(
            "payouts",
            amount=100000,
            arrival_date=api.now + 172800,
            automatic=True,
            balance_transaction=None,
            currency="usd",
            description="STRIPE PAYOUT",
            destination="ba_Halcyon0000000000000000",
            failure_balance_transaction=None,
            failure_code=None,
            failure_message=None,
            method="standard",
            original_payout=None,
            reconciliation_status="completed",
            reversed_by=None,
            source_type="card",
            statement_descriptor=None,
            status="paid",
            type="bank_account",
        )
        payout["balance_transaction"] = api.transaction(-payout["amount"], "usd", payout["id"], "payout")[
            "id"
        ]
    ctx.store.collection("_counters").put({"id": "invoice_number", "value": invoice_number})
    for collection, event_type in {
        "charges": "charge.succeeded",
        "invoices": "invoice.paid",
        "refunds": "refund.created",
        "payouts": "payout.paid",
        "disputes": "charge.dispute.created",
    }.items():
        for record in ctx.store.collection(collection).values():
            if anchor - 30 * 86400 <= record["created"] <= anchor:
                api.now = record["created"]
                actual_type = (
                    "charge.failed"
                    if collection == "charges" and record["status"] == "failed"
                    else "invoice.payment_failed"
                    if collection == "invoices" and record["status"] == "open"
                    else event_type
                )
                event = api.new(
                    "events",
                    type=actual_type,
                    api_version=VERSION,
                    data={"object": clean(record)},
                    request={"id": None, "idempotency_key": None},
                    pending_webhooks=0,
                )
                event.pop("metadata", None)
    api.now = anchor
