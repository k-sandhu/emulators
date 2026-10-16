"""Recurring billing reuses PaymentIntents so invoices and accounting reconcile."""

from __future__ import annotations

import calendar
import copy
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from emulators.core.errors import EmulatorError
from emulators.providers.stripe.payments import PaymentsAPI
from emulators.providers.stripe.wire import Record, boolean, fail, integer, metadata


def period_end(start: int, recurring: Record) -> int:
    dt = datetime.fromtimestamp(start, UTC)
    count = int(recurring.get("interval_count", 1))
    interval = recurring["interval"]
    if interval in {"day", "week"}:
        return start + count * (86400 if interval == "day" else 604800)
    months = count * (12 if interval == "year" else 1)
    year, month0 = divmod(dt.year * 12 + dt.month - 1 + months, 12)
    return int(
        dt.replace(
            year=year, month=month0 + 1, day=min(dt.day, calendar.monthrange(year, month0 + 1)[1])
        ).timestamp()
    )


class BillingAPI(PaymentsAPI):
    def make_invoice(self, p: Record, *, subscription: Record | None = None, preview: bool = False) -> Record:
        self.required(p, "customer")
        customer = self.get("customers", p["customer"], "customer")
        self.customer_time(customer)
        if customer.get("deleted"):
            fail("Cannot create an invoice for a deleted customer.", "customer")
        invoice = self.new(
            "invoices",
            account_country=self.ctx.world.org.address.country_code,
            account_name=self.ctx.world.org.name,
            account_tax_ids=None,
            amount_due=0,
            amount_paid=0,
            amount_overpaid=0,
            amount_remaining=0,
            amount_shipping=0,
            application=None,
            attempt_count=0,
            attempted=False,
            auto_advance=boolean(p.get("auto_advance", False)),
            automatic_tax={"enabled": False, "liability": None, "status": None},
            billing_reason="upcoming" if preview else "subscription_create" if subscription else "manual",
            collection_method=p.get("collection_method", "charge_automatically"),
            currency=p.get("currency", "usd"),
            customer=customer["id"],
            customer_address=copy.deepcopy(customer.get("address")),
            customer_email=customer.get("email"),
            customer_name=customer.get("name"),
            customer_phone=customer.get("phone"),
            customer_shipping=copy.deepcopy(customer.get("shipping")),
            customer_tax_exempt=customer["tax_exempt"],
            customer_tax_ids=[],
            default_payment_method=p.get("default_payment_method")
            or (subscription.get("default_payment_method") if subscription else None),
            default_source=None,
            default_tax_rates=[],
            description=p.get("description"),
            discounts=[],
            due_date=integer(p["due_date"], "due_date") if p.get("due_date") else None,
            ending_balance=None,
            footer=p.get("footer"),
            from_invoice=None,
            hosted_invoice_url=None,
            invoice_pdf=None,
            issuer={"type": "self"},
            last_finalization_error=None,
            latest_revision=None,
            next_payment_attempt=None,
            number=None,
            on_behalf_of=None,
            parent={
                "type": "subscription_details",
                "subscription_details": {
                    "metadata": copy.deepcopy(subscription["metadata"]),
                    "subscription": subscription["id"],
                },
            }
            if subscription
            else None,
            payment_settings={
                "default_mandate": None,
                "payment_method_options": None,
                "payment_method_types": None,
            },
            period_start=self.now,
            period_end=self.now,
            post_payment_credit_notes_amount=0,
            pre_payment_credit_notes_amount=0,
            receipt_number=None,
            shipping_cost=None,
            shipping_details=None,
            starting_balance=customer.get("balance", 0),
            statement_descriptor=None,
            status="draft",
            status_transitions={
                "finalized_at": None,
                "marked_uncollectible_at": None,
                "paid_at": None,
                "voided_at": None,
            },
            subtotal=0,
            subtotal_excluding_tax=0,
            test_clock=customer.get("test_clock"),
            total=0,
            total_discount_amounts=[],
            total_excluding_tax=0,
            total_taxes=[],
            transfer_data=None,
            webhooks_delivered_at=None,
            _payment_intent=None,
            _subscription=subscription["id"] if subscription else None,
            _line_ids=[],
        )
        if p.get("days_until_due"):
            invoice["due_date"] = self.now + integer(p["days_until_due"], "days_until_due", minimum=0) * 86400
        if invoice["collection_method"] not in {"charge_automatically", "send_invoice"}:
            fail("Invalid enum value", "collection_method")
        if invoice["collection_method"] == "send_invoice" and not invoice["due_date"]:
            fail(
                "You must specify days_until_due or due_date when collection_method=send_invoice.",
                "days_until_due",
            )
        metadata(invoice, p)
        invoice["lines"] = {
            "object": "list",
            "data": [],
            "has_more": False,
            "total_count": 0,
            "url": f"/v1/invoices/{invoice['id']}/lines",
        }
        invoice["payments"] = {
            "object": "list",
            "data": [],
            "has_more": False,
            "total_count": 0,
            "url": "/v1/invoice_payments",
        }
        if subscription:
            for item in subscription["items"]["data"]:
                price = self.get("prices", item["price"]["id"])
                self.add_line(
                    invoice,
                    price["unit_amount"] * item["quantity"],
                    price["currency"],
                    description=self.get("products", price["product"])["name"],
                    price=price,
                    quantity=item["quantity"],
                    subscription_item=item,
                )
            invoice["period_start"] = min(i["current_period_start"] for i in subscription["items"]["data"])
            invoice["period_end"] = max(i["current_period_end"] for i in subscription["items"]["data"])
        if p.get("pending_invoice_items_behavior", "exclude") == "include":
            for item in self.ctx.store.collection("invoiceitems"):
                if item["customer"] == customer["id"] and item["invoice"] is None:
                    item["invoice"] = invoice["id"]
                    line = self.add_line(
                        invoice,
                        item["amount"],
                        item["currency"],
                        description=item["description"],
                        invoice_item=item["id"],
                        quantity=item["quantity"],
                    )
                    line["period"] = copy.deepcopy(item["period"])
                    line["parent"]["invoice_item_details"]["proration"] = item["proration"]
            self.recalculate(invoice)
        return invoice

    def add_line(
        self,
        invoice: Record,
        amount: int,
        currency: str,
        *,
        description: str | None = None,
        price: Record | None = None,
        quantity: int = 1,
        subscription_item: Record | None = None,
        invoice_item: str | None = None,
    ) -> Record:
        line = self.new(
            "invoice_lines",
            amount=amount,
            currency=currency,
            description=description,
            discount_amounts=[],
            discountable=True,
            discounts=[],
            invoice=invoice["id"],
            parent={
                "type": "subscription_item_details",
                "subscription_item_details": {
                    "invoice_item": None,
                    "proration": False,
                    "proration_details": {"credited_items": None},
                    "subscription": subscription_item["subscription"],
                    "subscription_item": subscription_item["id"],
                },
            }
            if subscription_item
            else {
                "type": "invoice_item_details",
                "invoice_item_details": {
                    "invoice_item": invoice_item,
                    "proration": False,
                    "proration_details": {"credited_items": None},
                    "subscription": None,
                },
            },
            period={
                "start": subscription_item["current_period_start"] if subscription_item else self.now,
                "end": subscription_item["current_period_end"] if subscription_item else self.now,
            },
            pretax_credit_amounts=[],
            pricing={
                "type": "price_details",
                "price_details": {"price": price["id"], "product": price["product"]},
                "unit_amount_decimal": str(price["unit_amount"]),
            }
            if price
            else None,
            quantity=quantity,
            subtotal=amount,
            taxes=[],
        )
        invoice["_line_ids"].append(line["id"])
        self.recalculate(invoice)
        return line

    def recalculate(self, invoice: Record) -> None:
        lines = [self.get("invoice_lines", line_id) for line_id in invoice["_line_ids"]]
        total = sum(line["amount"] for line in lines)
        invoice.update(
            subtotal=total,
            subtotal_excluding_tax=total,
            total=total,
            total_excluding_tax=total,
            amount_due=max(0, total + invoice["starting_balance"]),
            amount_remaining=max(0, total + invoice["starting_balance"] - invoice["amount_paid"]),
        )
        invoice["lines"].update(
            data=copy.deepcopy(lines[:10]), has_more=len(lines) > 10, total_count=len(lines)
        )

    async def finalize_invoice(self, invoice: Record) -> Record:
        if invoice["status"] != "draft":
            if invoice["status"] in {"open", "paid"}:
                return invoice
            fail("This invoice cannot be finalized because it is not a draft invoice.")
        counter = self.ctx.store.collection("_counters").get("invoice_number") or {
            "id": "invoice_number",
            "value": 0,
        }
        counter["value"] += 1
        self.ctx.store.collection("_counters").put(counter)
        invoice.update(
            status="open",
            number=f"HALCYON-{counter['value']:04d}",
            hosted_invoice_url=self.url(
                "invoice.stripe.com", f"/i/{self.ctx.world.org.stripe_account_id}/test_{invoice['id']}"
            ),
            invoice_pdf=self.url("pay.stripe.com", f"/invoice/{invoice['id']}/pdf"),
        )
        invoice["status_transitions"]["finalized_at"] = self.now
        customer = self.get("customers", invoice["customer"])
        previous_balance = customer["balance"]
        customer["balance"] = min(0, invoice["total"] + invoice["starting_balance"])
        invoice["ending_balance"] = customer["balance"]
        if previous_balance != customer["balance"]:
            self.new(
                "customer_balance_transactions",
                amount=customer["balance"] - previous_balance,
                currency=invoice["currency"],
                customer=customer["id"],
                description=None,
                ending_balance=customer["balance"],
                invoice=invoice["id"],
                credit_note=None,
                type="applied_to_invoice",
            )
            await self.emit("customer.updated", customer)
        if invoice["amount_due"] > 0:
            pm = (
                invoice["default_payment_method"]
                or self.get("customers", invoice["customer"])["invoice_settings"]["default_payment_method"]
            )
            if not pm:
                customer = self.get("customers", invoice["customer"])
                if customer.get("default_source"):
                    source = self.get("sources", customer["default_source"])
                    method = self.new(
                        "payment_methods",
                        type="card",
                        customer=customer["id"],
                        billing_details={
                            "address": customer["address"],
                            "email": customer["email"],
                            "name": customer["name"],
                            "phone": customer["phone"],
                        },
                        card={
                            key: copy.deepcopy(source[key])
                            for key in {
                                "brand",
                                "country",
                                "exp_month",
                                "exp_year",
                                "fingerprint",
                                "funding",
                                "last4",
                            }
                        },
                        _scenario=source["_scenario"],
                    )
                    pm = method["id"]
            pi = self.create_intent(
                {
                    "amount": invoice["amount_due"],
                    "currency": invoice["currency"],
                    "customer": invoice["customer"],
                    **({"payment_method": pm} if pm else {}),
                    "metadata": {"invoice": invoice["id"]},
                }
            )
            invoice["_payment_intent"] = pi["id"]
            invoice["confirmation_secret"] = {"type": "payment_intent", "client_secret": pi["client_secret"]}
            payment = self.new(
                "invoice_payments",
                amount_paid=None,
                amount_requested=invoice["amount_due"],
                currency=invoice["currency"],
                invoice=invoice["id"],
                is_default=True,
                payment={"type": "payment_intent", "payment_intent": pi["id"]},
                status="open",
                status_transitions={"paid_at": None, "canceled_at": None},
            )
            payment.pop("metadata", None)
            invoice["payments"].update(data=[payment], total_count=1)
            await self.emit("payment_intent.created", pi)
        else:
            invoice["confirmation_secret"] = None
        await self.emit("invoice.finalized", invoice)
        if invoice["amount_due"] == 0:
            await self.mark_invoice_paid(invoice)
        return invoice

    async def mark_invoice_paid(self, invoice: Record) -> Record:
        invoice.update(
            status="paid",
            amount_paid=invoice["amount_due"],
            amount_remaining=0,
            attempted=True,
            next_payment_attempt=None,
        )
        invoice["status_transitions"]["paid_at"] = self.now
        for payment in invoice["payments"]["data"]:
            payment.update(status="paid", amount_paid=invoice["amount_paid"])
            payment["status_transitions"]["paid_at"] = self.now
        subscription = self.ctx.store.collection("subscriptions").get(invoice["_subscription"])
        if subscription and subscription["status"] in {"incomplete", "past_due", "unpaid"}:
            previous = copy.deepcopy(subscription)
            subscription["status"] = "active"
            await self.emit("customer.subscription.updated", subscription, previous)
        await self.emit("invoice.payment_succeeded", invoice)
        await self.emit("invoice.paid", invoice)
        return invoice

    async def pay_invoice(self, invoice: Record, p: Record) -> Record:
        if invoice["status"] == "paid":
            return invoice
        if invoice["status"] != "open":
            fail("Invoice is not open.", code="invoice_not_editable")
        if boolean(p.get("paid_out_of_band", False)):
            return await self.mark_invoice_paid(invoice)
        pi = self.get("payment_intents", invoice["_payment_intent"])
        customer = self.get("customers", invoice["customer"])
        pm = (
            p.get("payment_method")
            or invoice["default_payment_method"]
            or customer["invoice_settings"]["default_payment_method"]
        )
        pm = pm or pi["payment_method"]
        if not pm:
            fail(
                "Cannot charge a customer that has no active card",
                "payment_method",
                code="invoice_no_payment_method_types",
            )
        invoice["attempted"] = True
        invoice["attempt_count"] += 1
        try:
            if pi["status"] != "succeeded":
                await self.confirm_intent(pi, {"payment_method": pm})
        except EmulatorError:
            invoice["next_payment_attempt"] = self.now + 86400
            if invoice["_subscription"]:
                subscription = self.get("subscriptions", invoice["_subscription"])
                if subscription["status"] != "incomplete":
                    subscription["status"] = "past_due"
            await self.emit("invoice.payment_failed", invoice)
            raise
        if pi["status"] == "succeeded":
            await self.mark_invoice_paid(invoice)
        else:
            await self.emit("invoice.payment_action_required", invoice)
        return invoice

    def make_subscription(self, p: Record) -> Record:
        self.required(p, "customer", "items")
        customer = self.get("customers", p["customer"], "customer")
        self.customer_time(customer)
        if not isinstance(p["items"], list) or not p["items"]:
            fail("You must supply at least one subscription item.", "items")
        if p.get("default_payment_method"):
            self.payment_method_for({"payment_method": p["default_payment_method"]}, customer["id"])
        trial_end = p.get("trial_end")
        if trial_end == "now":
            trial_end = self.now
        elif trial_end:
            trial_end = integer(trial_end, "trial_end", minimum=self.now)
        elif p.get("trial_period_days"):
            trial_end = self.now + integer(p["trial_period_days"], "trial_period_days", minimum=1) * 86400
        trial = bool(trial_end and trial_end > self.now)
        subscription = self.new(
            "subscriptions",
            application=None,
            application_fee_percent=None,
            automatic_tax={"enabled": False, "disabled_reason": None, "liability": None},
            billing_cycle_anchor=trial_end if trial else self.now,
            billing_cycle_anchor_config=None,
            billing_mode={"type": "classic", "flexible": None},
            billing_thresholds=None,
            cancel_at=None,
            cancel_at_period_end=boolean(p.get("cancel_at_period_end", False)),
            canceled_at=None,
            cancellation_details={"comment": None, "feedback": None, "reason": None},
            collection_method=p.get("collection_method", "charge_automatically"),
            currency="usd",
            customer=customer["id"],
            days_until_due=p.get("days_until_due"),
            default_payment_method=p.get("default_payment_method"),
            default_source=None,
            default_tax_rates=[],
            description=p.get("description"),
            discounts=[],
            ended_at=None,
            invoice_settings={"account_tax_ids": None, "issuer": {"type": "self"}},
            latest_invoice=None,
            next_pending_invoice_item_invoice=None,
            on_behalf_of=None,
            pause_collection=None,
            payment_settings={
                "payment_method_options": None,
                "payment_method_types": None,
                "save_default_payment_method": "off",
            },
            pending_invoice_item_interval=None,
            pending_setup_intent=None,
            pending_update=None,
            schedule=None,
            start_date=self.now,
            status="trialing" if trial else "incomplete",
            test_clock=customer.get("test_clock"),
            transfer_data=None,
            trial_end=trial_end,
            trial_settings=p.get(
                "trial_settings", {"end_behavior": {"missing_payment_method": "create_invoice"}}
            ),
            trial_start=self.now if trial else None,
        )
        metadata(subscription, p)
        items = [self.make_subscription_item(subscription, i) for i in p["items"]]
        subscription["currency"] = items[0]["price"]["currency"]
        if any(item["price"]["currency"] != subscription["currency"] for item in items):
            fail("All prices must have the same currency.", "items")
        subscription["items"] = {
            "object": "list",
            "data": items,
            "has_more": False,
            "total_count": len(items),
            "url": "/v1/subscription_items?subscription=" + subscription["id"],
        }
        return subscription

    def make_subscription_item(self, subscription: Record, p: Record) -> Record:
        self.required(p, "price")
        price = self.get("prices", p["price"], "items[price]")
        if not price["recurring"] or not price["active"]:
            fail("The price specified is not a recurring price.", "items[price]")
        start = subscription.get("trial_end") or self.now
        return self.new(
            "subscription_items",
            billing_thresholds=None,
            current_period_start=self.now,
            current_period_end=start
            if subscription["status"] == "trialing"
            else period_end(start, price["recurring"]),
            discounts=[],
            price=copy.deepcopy(price),
            plan={
                "id": price["id"],
                "object": "plan",
                "active": price["active"],
                "amount": price["unit_amount"],
                "currency": price["currency"],
                "interval": price["recurring"]["interval"],
                "interval_count": price["recurring"]["interval_count"],
                "product": price["product"],
                "metadata": {},
            },
            quantity=integer(p.get("quantity", 1), "quantity", minimum=0),
            subscription=subscription["id"],
            tax_rates=[],
        )

    async def create_subscription(self, p: Record) -> Record:
        subscription = self.make_subscription(p)
        invoice = self.make_invoice({**p, "currency": subscription["currency"]}, subscription=subscription)
        subscription["latest_invoice"] = invoice["id"]
        if subscription["status"] == "trialing":
            for line_id in invoice["_line_ids"]:
                line = self.get("invoice_lines", line_id)
                line.update(amount=0, subtotal=0, description="Free trial")
            self.recalculate(invoice)
        await self.emit("invoice.created", invoice)
        await self.finalize_invoice(invoice)
        if invoice["status"] != "paid" and invoice["collection_method"] == "charge_automatically":
            try:
                await self.pay_invoice(invoice, {})
            except EmulatorError as exc:
                if p.get("payment_behavior") == "error_if_incomplete":
                    raise exc
        if (invoice["status"] == "paid" and subscription["status"] != "trialing") or invoice[
            "collection_method"
        ] == "send_invoice":
            subscription["status"] = "active"
        await self.emit("customer.subscription.created", subscription)
        return subscription

    async def renew(self, target: int, clock_id: str | None = None) -> int:
        self.time_override = True
        count = 0
        for subscription in self.ctx.store.collection("subscriptions"):
            if (
                subscription.get("test_clock") == clock_id
                and subscription["status"] == "incomplete"
                and subscription["created"] + 23 * 3600 <= target
            ):
                subscription.update(status="incomplete_expired", ended_at=subscription["created"] + 23 * 3600)
                invoice = self.get("invoices", subscription["latest_invoice"])
                invoice.update(status="void", amount_remaining=0)
                invoice["status_transitions"]["voided_at"] = subscription["ended_at"]
                self.now = subscription["ended_at"]
                await self.emit("invoice.voided", invoice)
                await self.emit("customer.subscription.updated", subscription)
            if subscription.get("test_clock") != clock_id or subscription["status"] not in {
                "active",
                "trialing",
                "past_due",
            }:
                continue
            items = subscription["items"]["data"]
            while items and min(i["current_period_end"] for i in items) <= target:
                end = min(i["current_period_end"] for i in items)
                self.now = end
                if subscription["cancel_at_period_end"] or (
                    subscription["cancel_at"] and subscription["cancel_at"] <= end
                ):
                    await self.cancel_subscription(subscription)
                    break
                previous = copy.deepcopy(subscription)
                if subscription["status"] == "trialing":
                    customer = self.get("customers", subscription["customer"])
                    pm = (
                        subscription["default_payment_method"]
                        or customer["invoice_settings"]["default_payment_method"]
                    )
                    behavior = subscription["trial_settings"]["end_behavior"]["missing_payment_method"]
                    if not pm and behavior in {"pause", "cancel"}:
                        if behavior == "cancel":
                            await self.cancel_subscription(subscription)
                        else:
                            subscription["status"] = "paused"
                            await self.emit("customer.subscription.paused", subscription)
                        break
                for item in items:
                    if item["current_period_end"] <= end:
                        item["current_period_start"] = end
                        item["current_period_end"] = period_end(end, item["price"]["recurring"])
                if subscription["status"] == "trialing":
                    subscription["status"] = "active"
                invoice = self.make_invoice(
                    {
                        "customer": subscription["customer"],
                        "currency": subscription["currency"],
                        "default_payment_method": subscription["default_payment_method"],
                        "pending_invoice_items_behavior": "include",
                    },
                    subscription=subscription,
                )
                invoice["billing_reason"] = "subscription_cycle"
                subscription["latest_invoice"] = invoice["id"]
                await self.emit("invoice.created", invoice)
                await self.finalize_invoice(invoice)
                try:
                    await self.pay_invoice(invoice, {})
                except EmulatorError:
                    subscription["status"] = "past_due"
                await self.emit("customer.subscription.updated", subscription, previous)
                count += 1
        self.now = target
        self.time_override = False
        return count

    async def prorate(self, subscription: Record, previous: Record, p: Record) -> None:
        behavior = p.get("proration_behavior", "create_prorations")
        if behavior not in {"none", "create_prorations", "always_invoice"}:
            fail("Invalid enum value", "proration_behavior")
        if behavior == "none" or subscription["status"] != "active":
            return
        old_items = {i["id"]: i for i in previous["items"]["data"]}
        new_items = {i["id"]: i for i in subscription["items"]["data"]}
        delta = Decimal(0)
        for item_id in old_items.keys() | new_items.keys():
            old, new = old_items.get(item_id), new_items.get(item_id)
            item = old or new
            remaining = max(0, item["current_period_end"] - self.now)
            duration = max(1, item["current_period_end"] - item["current_period_start"])
            cost_old = old["price"]["unit_amount"] * old["quantity"] if old else 0
            cost_new = new["price"]["unit_amount"] * new["quantity"] if new else 0
            delta += Decimal(cost_new - cost_old) * Decimal(remaining) / Decimal(duration)
        amount = int(delta.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if not amount:
            return
        item = self.new(
            "invoiceitems",
            amount=amount,
            currency=subscription["currency"],
            customer=subscription["customer"],
            date=self.now,
            description="Subscription proration",
            discountable=False,
            discounts=[],
            invoice=None,
            period={
                "start": self.now,
                "end": max(i["current_period_end"] for i in subscription["items"]["data"]),
            },
            pricing=None,
            proration=True,
            quantity=1,
            subscription=subscription["id"],
            tax_rates=[],
            test_clock=subscription["test_clock"],
            unit_amount=amount,
        )
        await self.emit("invoiceitem.created", item)
        if behavior == "always_invoice":
            invoice = self.make_invoice(
                {
                    "customer": subscription["customer"],
                    "currency": subscription["currency"],
                    "default_payment_method": subscription["default_payment_method"],
                    "pending_invoice_items_behavior": "include",
                }
            )
            invoice.update(_subscription=subscription["id"], billing_reason="subscription_update")
            subscription["latest_invoice"] = invoice["id"]
            await self.emit("invoice.created", invoice)
            await self.finalize_invoice(invoice)
            if invoice["status"] == "open":
                try:
                    await self.pay_invoice(invoice, {})
                except EmulatorError:
                    subscription["status"] = "past_due"

    async def cancel_subscription(self, subscription: Record) -> Record:
        if subscription["status"] == "canceled":
            fail("A canceled subscription can only update its cancellation_details.")
        previous = copy.deepcopy(subscription)
        subscription.update(
            status="canceled", canceled_at=self.now, ended_at=self.now, cancel_at_period_end=False
        )
        subscription["cancellation_details"]["reason"] = "cancellation_requested"
        await self.emit("customer.subscription.deleted", subscription, previous)
        return subscription

    async def billing_route(
        self, collection: str, record_id: str | None, operation: str | None, method: str, p: Record
    ) -> Any:
        if collection == "subscriptions":
            if method == "POST" and not record_id:
                return await self.create_subscription(p)
            if record_id and method in {"POST", "DELETE"}:
                subscription = self.get(collection, record_id)
                if method == "DELETE":
                    return await self.cancel_subscription(subscription)
                previous = copy.deepcopy(subscription)
                if operation == "resume":
                    if subscription["status"] != "paused":
                        fail("Only paused subscriptions can be resumed.")
                    subscription["status"] = "active"
                    for item in subscription["items"]["data"]:
                        item["current_period_start"] = self.now
                        item["current_period_end"] = period_end(self.now, item["price"]["recurring"])
                elif operation:
                    fail("Unrecognized request URL.", status=404)
                else:
                    if subscription["status"] == "canceled":
                        fail("A canceled subscription cannot be updated.")
                    self.update(
                        subscription,
                        p,
                        {"cancel_at_period_end", "default_payment_method", "description", "pause_collection"},
                    )
                    if "cancel_at" in p:
                        subscription["cancel_at"] = (
                            integer(p["cancel_at"], "cancel_at") if p["cancel_at"] else None
                        )
                    for item_params in p.get("items", []):
                        if item_params.get("id"):
                            item = self.get("subscription_items", item_params["id"])
                            if item["subscription"] != subscription["id"]:
                                fail("Subscription item does not belong to this subscription.", "items")
                            if boolean(item_params.get("deleted", False)):
                                subscription["items"]["data"].remove(item)
                                self.ctx.store.collection("subscription_items").delete(item["id"])
                            else:
                                if "quantity" in item_params:
                                    item["quantity"] = integer(item_params["quantity"], "quantity", minimum=0)
                                if "price" in item_params:
                                    price = self.get("prices", item_params["price"])
                                    if not price["recurring"]:
                                        fail("The price must be recurring.", "price")
                                    item["price"] = copy.deepcopy(price)
                        else:
                            subscription["items"]["data"].append(
                                self.make_subscription_item(subscription, item_params)
                            )
                    subscription["items"]["total_count"] = len(subscription["items"]["data"])
                    if not subscription["items"]["data"]:
                        fail("A subscription must have at least one item.", "items")
                    if p.get("items"):
                        await self.prorate(subscription, previous, p)
                await self.emit("customer.subscription.updated", subscription, previous)
                return subscription
        if collection == "subscription_items" and method in {"POST", "DELETE"}:
            if not record_id:
                self.required(p, "subscription")
                subscription = self.get("subscriptions", p["subscription"])
                item = self.make_subscription_item(subscription, p)
                subscription["items"]["data"].append(item)
            else:
                item = self.get(collection, record_id)
                subscription = self.get("subscriptions", item["subscription"])
                if method == "DELETE":
                    if len(subscription["items"]["data"]) <= 1:
                        fail("Cannot delete the last item in a subscription.")
                    subscription["items"]["data"].remove(item)
                    self.ctx.store.collection(collection).delete(record_id)
                    await self.emit("customer.subscription.updated", subscription)
                    return {"id": record_id, "object": "subscription_item", "deleted": True}
                if "quantity" in p:
                    item["quantity"] = integer(p["quantity"], "quantity", minimum=0)
                if "price" in p:
                    price = self.get("prices", p["price"])
                    if not price["recurring"]:
                        fail("The price must be recurring.", "price")
                    item["price"] = copy.deepcopy(price)
                metadata(item, p)
            subscription["items"]["total_count"] = len(subscription["items"]["data"])
            await self.emit("customer.subscription.updated", subscription)
            return item
        if collection == "invoices":
            if record_id in {"upcoming", "create_preview"}:
                subscription = self.get("subscriptions", p["subscription"]) if p.get("subscription") else None
                invoice = self.make_invoice(
                    {
                        **p,
                        "customer": p.get("customer") or (subscription["customer"] if subscription else None),
                    },
                    subscription=subscription,
                    preview=True,
                )
                self.ctx.store.collection("invoices").delete(invoice["id"])
                invoice["id"] = "upcoming_in_" + self.ctx.ids.base62(24)
                invoice["status"] = "draft"
                return invoice
            if method == "POST" and not record_id:
                invoice = self.make_invoice(p)
                await self.emit("invoice.created", invoice)
                return invoice
            if record_id:
                invoice = self.get("invoices", record_id)
                if method == "GET" and operation == "lines":
                    return self.listing(
                        "invoice_lines",
                        rows=[self.get("invoice_lines", i) for i in invoice["_line_ids"]],
                        url=f"/v1/invoices/{record_id}/lines",
                    )
                if method == "DELETE" and not operation:
                    if invoice["status"] != "draft":
                        fail("Only draft invoices can be deleted.")
                    self.ctx.store.collection(collection).delete(record_id)
                    await self.emit("invoice.deleted", invoice)
                    return {"id": record_id, "object": "invoice", "deleted": True}
                if method == "POST":
                    if operation == "finalize":
                        return await self.finalize_invoice(invoice)
                    if operation == "pay":
                        return await self.pay_invoice(invoice, p)
                    if operation in {"void", "mark_uncollectible", "send"}:
                        if invoice["status"] != "open":
                            fail("Invoice is not open.")
                        if operation == "send":
                            await self.emit("invoice.sent", invoice)
                        else:
                            invoice["status"] = "void" if operation == "void" else "uncollectible"
                            invoice["status_transitions"][
                                "voided_at" if operation == "void" else "marked_uncollectible_at"
                            ] = self.now
                            if operation == "void":
                                invoice["amount_remaining"] = 0
                                if invoice["_payment_intent"]:
                                    pi = self.get("payment_intents", invoice["_payment_intent"])
                                    if pi["status"] != "succeeded":
                                        await self.intent_operation(pi, "cancel", {})
                                for payment in invoice["payments"]["data"]:
                                    payment["status"] = "canceled"
                                    payment["status_transitions"]["canceled_at"] = self.now
                            await self.emit(
                                "invoice.voided" if operation == "void" else "invoice.marked_uncollectible",
                                invoice,
                            )
                        return invoice
                    if not operation:
                        self.update(
                            invoice,
                            p,
                            {"description", "metadata", "footer", "default_payment_method", "auto_advance"},
                        )
                        await self.emit("invoice.updated", invoice)
                        return invoice
        if collection == "invoiceitems" and method in {"POST", "DELETE"}:
            if record_id:
                item = self.get(collection, record_id)
                if item["invoice"] and self.get("invoices", item["invoice"])["status"] != "draft":
                    fail("The invoice is no longer editable.", code="invoice_not_editable")
                if method == "DELETE":
                    self.ctx.store.collection(collection).delete(record_id)
                    if item["invoice"]:
                        invoice = self.get("invoices", item["invoice"])
                        for line_id in list(invoice["_line_ids"]):
                            line = self.get("invoice_lines", line_id)
                            if (
                                line["parent"]["type"] == "invoice_item_details"
                                and line["parent"]["invoice_item_details"]["invoice_item"] == record_id
                            ):
                                invoice["_line_ids"].remove(line_id)
                        self.recalculate(invoice)
                    await self.emit("invoiceitem.deleted", item)
                    return {"id": record_id, "object": "invoiceitem", "deleted": True}
                self.update(item, p, {"description"})
                if "amount" in p:
                    item["amount"] = integer(p["amount"], "amount")
                if item["invoice"]:
                    invoice = self.get("invoices", item["invoice"])
                    for line_id in invoice["_line_ids"]:
                        line = self.get("invoice_lines", line_id)
                        details = line.get("parent", {}).get("invoice_item_details", {})
                        if details.get("invoice_item") == item["id"]:
                            line.update(
                                amount=item["amount"],
                                subtotal=item["amount"],
                                description=item["description"],
                            )
                    self.recalculate(invoice)
                await self.emit("invoiceitem.updated", item)
                return item
            self.required(p, "customer")
            self.get("customers", p["customer"], "customer")
            pricing = p.get("pricing", {})
            price_id = p.get("price") or pricing.get("price")
            price = self.get("prices", price_id, "price") if price_id else None
            quantity = integer(p.get("quantity", 1), "quantity", minimum=1)
            if price:
                amount, currency = price["unit_amount"] * quantity, price["currency"]
            else:
                self.required(p, "currency")
                amount = integer(
                    p.get("amount", integer(p.get("unit_amount", 0), "unit_amount") * quantity), "amount"
                )
                currency = p["currency"]
            invoice = self.get("invoices", p["invoice"], "invoice") if p.get("invoice") else None
            if invoice and (invoice["status"] != "draft" or invoice["customer"] != p["customer"]):
                fail("The invoice is not an editable draft for this customer.", "invoice")
            item = self.new(
                "invoiceitems",
                amount=amount,
                currency=currency,
                customer=p["customer"],
                date=self.now,
                description=p.get("description"),
                discountable=True,
                discounts=[],
                invoice=invoice["id"] if invoice else None,
                period=p.get("period", {"start": self.now, "end": self.now}),
                pricing={
                    "type": "price_details",
                    "price_details": {"price": price["id"], "product": price["product"]},
                    "unit_amount_decimal": str(price["unit_amount"]),
                }
                if price
                else None,
                proration=False,
                quantity=quantity,
                subscription=p.get("subscription"),
                tax_rates=[],
                test_clock=None,
                unit_amount=amount // quantity,
            )
            metadata(item, p)
            if invoice:
                self.add_line(
                    invoice,
                    amount,
                    currency,
                    description=item["description"],
                    price=price,
                    quantity=quantity,
                    invoice_item=item["id"],
                )
            await self.emit("invoiceitem.created", item)
            return item
        return None
