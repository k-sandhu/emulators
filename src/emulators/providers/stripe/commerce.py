"""Customer, catalog, hosted checkout, webhook configuration, and test helper routes."""

from __future__ import annotations

import copy
from typing import Any

from emulators.providers.stripe.billing import BillingAPI, period_end
from emulators.providers.stripe.validation import timestamp
from emulators.providers.stripe.wire import VERSION, Record, boolean, fail, integer, metadata


class CommerceAPI(BillingAPI):
    def make_customer(self, p: Record) -> Record:
        clock_id = p.get("test_clock")
        if clock_id:
            clock = self.get("test_helpers/test_clocks", clock_id, "test_clock")
            self.now = clock["frozen_time"]
        customer = self.new(
            "customers",
            address=None,
            balance=0,
            currency=None,
            default_source=None,
            delinquent=False,
            description=None,
            discount=None,
            email=None,
            invoice_prefix=self.ctx.ids.base62(8).upper(),
            invoice_settings={
                "custom_fields": None,
                "default_payment_method": None,
                "footer": None,
                "rendering_options": None,
            },
            name=None,
            next_invoice_sequence=1,
            phone=None,
            preferred_locales=[],
            shipping=None,
            tax_exempt="none",
            test_clock=clock_id,
        )
        self.update(
            customer,
            p,
            {
                "address",
                "description",
                "email",
                "name",
                "phone",
                "shipping",
                "preferred_locales",
                "tax_exempt",
                "invoice_settings",
            },
        )
        if customer["address"]:
            customer["address"] = {
                "city": None,
                "country": None,
                "line1": None,
                "line2": None,
                "postal_code": None,
                "state": None,
                **customer["address"],
            }
        if p.get("balance"):
            customer["balance"] = integer(p["balance"], "balance")
        if p.get("payment_method"):
            pm = self.payment_method_for(p, customer["id"])
            pm["customer"] = customer["id"]
            customer["_attached_payment_method"] = pm["id"]
            if customer["invoice_settings"]["default_payment_method"] == p["payment_method"]:
                customer["invoice_settings"]["default_payment_method"] = pm["id"]
        self.customer_defaults(customer)
        return customer

    def customer_defaults(self, customer: Record) -> None:
        """Default payment instruments must belong to the customer being changed."""
        pm_id = customer["invoice_settings"]["default_payment_method"]
        if pm_id and self.payment_method(pm_id)["customer"] != customer["id"]:
            fail(
                "The customer does not have this payment method.", "invoice_settings[default_payment_method]"
            )
        source_id = customer.get("default_source")
        if source_id and self.get("sources", source_id, "default_source")["customer"] != customer["id"]:
            fail("The customer does not have this source.", "default_source")

    def make_product(self, p: Record) -> Record:
        self.required(p, "name")
        product = self.new(
            "products",
            active=boolean(p.get("active", True)),
            default_price=None,
            description=p.get("description"),
            images=p.get("images", []),
            marketing_features=[],
            name=p["name"],
            package_dimensions=None,
            shippable=None,
            statement_descriptor=None,
            tax_code=None,
            type="service",
            unit_label=p.get("unit_label"),
            updated=self.now,
            url=p.get("url"),
        )
        if p.get("id"):
            if self.ctx.store.collection("products").get(p["id"]):
                fail("A product with this ID already exists.", "id", code="resource_already_exists")
            self.ctx.store.collection("products").delete(product["id"])
            product["id"] = p["id"]
            self.ctx.store.collection("products").put(product)
        metadata(product, p)
        if p.get("default_price_data"):
            price = self.make_price({**p["default_price_data"], "product": product["id"]})
            product["default_price"] = price["id"]
        return product

    def make_price(self, p: Record) -> Record:
        self.required(p, "currency")
        if p.get("product_data"):
            product = self.make_product(p["product_data"])
        else:
            self.required(p, "product")
            product = self.get("products", p["product"], "product")
        if "unit_amount" not in p and "unit_amount_decimal" not in p:
            fail("Missing required param: unit_amount.", "unit_amount", code="parameter_missing")
        amount = integer(p.get("unit_amount", p.get("unit_amount_decimal")), "unit_amount", minimum=0)
        recurring = p.get("recurring")
        if recurring:
            self.required(recurring, "interval")
            if recurring["interval"] not in {"day", "week", "month", "year"}:
                fail("Invalid enum value", "recurring[interval]")
            recurring = {
                "interval": recurring["interval"],
                "interval_count": integer(
                    recurring.get("interval_count", 1),
                    "recurring[interval_count]",
                    minimum=1,
                    maximum={"day": 1095, "week": 156, "month": 36, "year": 3}[recurring["interval"]],
                ),
                "meter": None,
                "trial_period_days": None,
                "usage_type": "licensed",
            }
        lookup = p.get("lookup_key")
        existing = self.ctx.store.collection("prices").first(lookup_key=lookup) if lookup else None
        if existing:
            if not boolean(p.get("transfer_lookup_key", False)):
                fail("Lookup key already used by another price.", "lookup_key")
            existing["lookup_key"] = None
        price = self.new(
            "prices",
            active=boolean(p.get("active", True)),
            billing_scheme="per_unit",
            currency=str(p["currency"]).lower(),
            custom_unit_amount=None,
            lookup_key=lookup,
            nickname=p.get("nickname"),
            product=product["id"],
            recurring=recurring,
            tax_behavior=p.get("tax_behavior", "unspecified"),
            tiers_mode=None,
            transform_quantity=None,
            type="recurring" if recurring else "one_time",
            unit_amount=amount,
            unit_amount_decimal=str(amount),
        )
        metadata(price, p)
        return price

    async def customer_route(
        self, record_id: str | None, operation: str | None, child_id: str | None, method: str, p: Record
    ) -> Any:
        if method == "POST" and not record_id:
            if self.customer_scope:
                fail("This API key cannot create additional customers.", status=403)
            customer = self.make_customer(p)
            if p.get("source"):
                await self.customer_route(customer["id"], "sources", None, "POST", {"source": p["source"]})
            attached = customer.pop("_attached_payment_method", None)
            if attached:
                await self.emit("payment_method.attached", self.get("payment_methods", attached))
            await self.emit("customer.created", customer)
            return customer
        if not record_id:
            return None
        customer = self.get("customers", record_id, allow_deleted=method == "GET" and not operation)
        if method == "GET" and not operation:
            return customer
        if operation:
            nested = {
                "payment_methods": "payment_methods",
                "sources": "sources",
                "tax_ids": "tax_ids",
                "balance_transactions": "customer_balance_transactions",
            }
            collection = nested.get(operation)
            if not collection:
                return None
            if method == "GET":
                if child_id:
                    item = self.get(collection, child_id)
                    if item.get("customer") != record_id:
                        fail(f"No such {operation}: '{child_id}'", status=404, code="resource_missing")
                    return item
                return self.listing(
                    collection,
                    rows=self.ctx.store.collection(collection).where(customer=record_id),
                    url=f"/v1/customers/{record_id}/{operation}",
                )
            if method == "DELETE" and child_id and operation in {"sources", "tax_ids"}:
                item = self.get(collection, child_id)
                if item["customer"] != record_id:
                    fail("Resource not found.", status=404, code="resource_missing")
                self.ctx.store.collection(collection).delete(child_id)
                if operation == "sources" and customer["default_source"] == child_id:
                    remaining = self.ctx.store.collection("sources").where(customer=record_id)
                    customer["default_source"] = remaining[0]["id"] if remaining else None
                await self.emit(
                    "customer.source.deleted" if operation == "sources" else "customer.tax_id.deleted", item
                )
                return {"id": child_id, "object": item["object"], "deleted": True}
            if method == "POST" and child_id and operation in {"sources", "balance_transactions"}:
                item = self.get(collection, child_id)
                if item["customer"] != record_id:
                    fail("Resource not found.", status=404, code="resource_missing")
                previous = copy.deepcopy(item)
                self.update(
                    item,
                    p,
                    {"description"}
                    if operation == "balance_transactions"
                    else {
                        "name",
                        "address_city",
                        "address_country",
                        "address_line1",
                        "address_line2",
                        "address_state",
                        "address_zip",
                    },
                )
                if operation == "sources":
                    for key in {"exp_month", "exp_year"} & p.keys():
                        item[key] = integer(
                            p[key], key, minimum=1, maximum=12 if key == "exp_month" else None
                        )
                await self.emit(
                    "customer.source.updated" if operation == "sources" else "customer.updated",
                    item if operation == "sources" else customer,
                    previous if operation == "sources" else None,
                )
                return item
            if method == "POST" and not child_id:
                if operation == "sources":
                    source = p.get("source")
                    card, scenario = self.make_card(
                        {"token": source} if isinstance(source, str) else source or {}
                    )
                    item = self.new(
                        "sources",
                        **{
                            k: v
                            for k, v in card.items()
                            if k
                            not in {
                                "networks",
                                "checks",
                                "wallet",
                                "three_d_secure_usage",
                                "display_brand",
                                "generated_from",
                            }
                        },
                        customer=record_id,
                        address_city=None,
                        address_country=None,
                        address_line1=None,
                        address_line1_check=None,
                        address_line2=None,
                        address_state=None,
                        address_zip=None,
                        address_zip_check=None,
                        cvc_check="pass",
                        dynamic_last4=None,
                        name=None,
                        tokenization_method=None,
                        _scenario=scenario,
                    )
                    customer["default_source"] = customer["default_source"] or item["id"]
                    await self.emit("customer.source.created", item)
                    return item
                if operation == "tax_ids":
                    self.required(p, "type", "value")
                    item = self.new(
                        collection,
                        customer=record_id,
                        type=p["type"],
                        value=p["value"],
                        country=customer.get("address", {}).get("country")
                        if customer.get("address")
                        else None,
                        verification={"status": "verified", "verified_address": None, "verified_name": None},
                    )
                    item.pop("metadata", None)
                    await self.emit("customer.tax_id.created", item)
                    return item
                if operation == "balance_transactions":
                    self.required(p, "amount", "currency")
                    amount = integer(p["amount"], "amount")
                    customer["balance"] += amount
                    item = self.new(
                        collection,
                        amount=amount,
                        currency=p["currency"],
                        customer=record_id,
                        description=p.get("description"),
                        ending_balance=customer["balance"],
                        invoice=None,
                        credit_note=None,
                        type="adjustment",
                    )
                    metadata(item, p)
                    await self.emit("customer.updated", customer)
                    return item
            return None
        if method == "DELETE":
            for subscription in self.ctx.store.collection("subscriptions").where(customer=record_id):
                if subscription["status"] != "canceled":
                    await self.cancel_subscription(subscription)
            for pm in self.ctx.store.collection("payment_methods").where(customer=record_id):
                pm.update(customer=None, _detached=True)
            deleted = {
                "id": record_id,
                "object": "customer",
                "deleted": True,
                **{k: v for k, v in customer.items() if k in {"_client_id", "_owner_customer"}},
            }
            self.ctx.store.collection("customers").put(deleted)
            await self.emit("customer.deleted", deleted)
            return deleted
        if method == "POST":
            previous = copy.deepcopy(customer)
            self.update(
                customer,
                p,
                {
                    "address",
                    "description",
                    "email",
                    "name",
                    "phone",
                    "shipping",
                    "preferred_locales",
                    "tax_exempt",
                    "invoice_settings",
                    "default_source",
                },
            )
            if "balance" in p:
                customer["balance"] = integer(p["balance"], "balance")
            self.customer_defaults(customer)
            await self.emit("customer.updated", customer, previous)
            return customer
        return None

    async def create_checkout(self, p: Record) -> Record:
        mode = p.get("mode", "payment")
        if mode not in {"payment", "subscription", "setup"}:
            fail("Invalid enum value", "mode")
        if mode != "setup":
            self.required(p, "line_items")
        if p.get("customer"):
            self.get("customers", p["customer"], "customer")
        rows = []
        total = 0
        for item in p.get("line_items", []):
            price = (
                self.make_price(item["price_data"])
                if item.get("price_data")
                else self.get("prices", item.get("price"), "line_items[price]")
            )
            quantity = integer(item.get("quantity", 1), "quantity", minimum=1)
            if mode == "payment" and price["recurring"]:
                fail("Recurring prices cannot be used in payment mode.", "line_items")
            if rows and price["currency"] != rows[0]["currency"]:
                fail("All prices must have the same currency.", "line_items")
            total += price["unit_amount"] * quantity
            rows.append(
                {
                    "id": self.ctx.ids.prefixed("li", 24),
                    "object": "item",
                    "amount_discount": 0,
                    "amount_subtotal": price["unit_amount"] * quantity,
                    "amount_tax": 0,
                    "amount_total": price["unit_amount"] * quantity,
                    "currency": price["currency"],
                    "description": self.get("products", price["product"])["name"],
                    "price": copy.deepcopy(price),
                    "quantity": quantity,
                }
            )
        session = self.new(
            "checkout/sessions",
            adaptive_pricing={"enabled": False},
            after_expiration=None,
            allow_promotion_codes=None,
            amount_subtotal=total if mode != "setup" else None,
            amount_total=total if mode != "setup" else None,
            automatic_tax={"enabled": False, "liability": None, "status": None},
            billing_address_collection=None,
            cancel_url=p.get("cancel_url"),
            client_reference_id=p.get("client_reference_id"),
            client_secret=None,
            consent=None,
            consent_collection=None,
            currency=rows[0]["currency"] if rows else None,
            custom_fields=[],
            custom_text={
                "after_submit": None,
                "shipping_address": None,
                "submit": None,
                "terms_of_service_acceptance": None,
            },
            customer=p.get("customer"),
            customer_creation=p.get("customer_creation", "if_required"),
            customer_details=None,
            customer_email=p.get("customer_email"),
            expires_at=integer(
                p.get("expires_at", self.now + 86400),
                "expires_at",
                minimum=self.now + 1800,
                maximum=self.now + 86400,
            ),
            invoice=None,
            invoice_creation=None,
            locale=None,
            mode=mode,
            payment_intent=None,
            payment_link=None,
            payment_method_collection="always",
            payment_method_options={},
            payment_method_types=p.get("payment_method_types", ["card"]),
            payment_status="unpaid",
            phone_number_collection={"enabled": False},
            recovered_from=None,
            saved_payment_method_options=None,
            setup_intent=None,
            shipping_address_collection=None,
            shipping_cost=None,
            shipping_options=[],
            status="open",
            submit_type=None,
            subscription=None,
            success_url=p.get("success_url"),
            total_details={"amount_discount": 0, "amount_shipping": 0, "amount_tax": 0},
            ui_mode="hosted",
            url=None,
            _line_items=rows,
            _subscription_data=copy.deepcopy(p.get("subscription_data", {})),
            _payment_intent_data=copy.deepcopy(p.get("payment_intent_data", {})),
        )
        metadata(session, p)
        session["url"] = self.url("checkout.stripe.com", "/c/pay/" + session["id"])
        return session

    async def complete_checkout(self, session_id: str, card: str = "4242424242424242") -> Record:
        session = self.get("checkout/sessions", session_id)
        if session["status"] != "open":
            fail("This Checkout Session is no longer open.")
        if session["expires_at"] <= self.now:
            session["status"] = "expired"
            fail("This Checkout Session has expired.")
        customer_id = session["customer"]
        if not customer_id:
            customer = self.make_customer({"email": session["customer_email"]})
            customer_id = customer["id"]
            session["customer"] = customer_id
            await self.emit("customer.created", customer)
        pm = self.create_payment_method({"card": {"number": card, "exp_month": 12, "exp_year": 2030}})
        pm["customer"] = customer_id
        customer = self.get("customers", customer_id)
        session["customer_details"] = {
            "address": customer["address"],
            "email": customer["email"],
            "name": customer["name"],
            "phone": customer["phone"],
            "tax_exempt": customer["tax_exempt"],
            "tax_ids": [],
        }
        if session["mode"] == "subscription":
            subscription = await self.create_subscription(
                {
                    **session["_subscription_data"],
                    "customer": customer_id,
                    "default_payment_method": pm["id"],
                    "items": [
                        {"price": i["price"]["id"], "quantity": i["quantity"]} for i in session["_line_items"]
                    ],
                }
            )
            session["subscription"] = subscription["id"]
            if subscription["status"] not in {"active", "trialing"}:
                return session
        else:
            setup = session["mode"] == "setup"
            intent = self.create_intent(
                {
                    **session["_payment_intent_data"],
                    "customer": customer_id,
                    "payment_method": pm["id"],
                    "amount": session["amount_total"],
                    "currency": session["currency"],
                },
                setup=setup,
            )
            session["setup_intent" if setup else "payment_intent"] = intent["id"]
            await self.confirm_intent(intent, {}, setup=setup)
            if intent["status"] != "succeeded":
                return session
        if session["status"] == "complete":
            return session
        session.update(
            status="complete",
            payment_status="no_payment_required" if session["mode"] == "setup" else "paid",
            url=None,
        )
        await self.emit("checkout.session.completed", session)
        return session

    async def commerce_route(
        self, collection: str, record_id: str | None, operation: str | None, method: str, p: Record
    ) -> Any:
        if collection in {"products", "prices"}:
            if method == "POST" and not record_id:
                record = self.make_product(p) if collection == "products" else self.make_price(p)
                await self.emit(record["object"] + ".created", record)
                return record
            if record_id and method in {"POST", "DELETE"} and not operation:
                record = self.get(collection, record_id)
                previous = copy.deepcopy(record)
                if method == "DELETE":
                    if collection != "products":
                        fail("Unrecognized request URL.", status=404)
                    if self.ctx.store.collection("prices").first(product=record_id):
                        fail("This product cannot be deleted because it has one or more user-created prices.")
                    self.ctx.store.collection(collection).delete(record_id)
                    await self.emit("product.deleted", record)
                    return {"id": record_id, "object": "product", "deleted": True}
                if collection == "products" and p.get("default_price"):
                    price = self.get("prices", p["default_price"], "default_price")
                    if price["product"] != record_id:
                        fail("The default price must belong to this product.", "default_price")
                if collection == "prices":
                    for field in {
                        "unit_amount",
                        "unit_amount_decimal",
                        "currency",
                        "product",
                        "recurring",
                    } & p.keys():
                        fail("Received unknown parameter: " + field, field, code="parameter_unknown")
                    if (
                        "tax_behavior" in p
                        and record["tax_behavior"] != "unspecified"
                        and p["tax_behavior"] != record["tax_behavior"]
                    ):
                        fail("You cannot change tax_behavior after it has been specified.", "tax_behavior")
                    if p.get("lookup_key"):
                        other = next(
                            (
                                r
                                for r in self.ctx.store.collection("prices")
                                if r["id"] != record_id and r["lookup_key"] == p["lookup_key"]
                            ),
                            None,
                        )
                        if other:
                            if not boolean(p.get("transfer_lookup_key", False)):
                                fail("Lookup key already used by another price.", "lookup_key")
                            other["lookup_key"] = None
                self.update(
                    record,
                    p,
                    {"active", "name", "description", "images", "url", "unit_label", "default_price"}
                    if collection == "products"
                    else {"active", "nickname", "lookup_key", "tax_behavior"},
                )
                if collection == "products":
                    record["updated"] = self.now
                await self.emit(record["object"] + ".updated", record, previous)
                return record
        if collection == "webhook_endpoints" and method in {"POST", "DELETE"}:
            if not record_id:
                self.required(p, "url", "enabled_events")
                if not isinstance(p["enabled_events"], list) or not p["enabled_events"]:
                    fail("Invalid array", "enabled_events")
                self.webhook_parameters(p)
                endpoint = self.new(
                    collection,
                    api_version=p.get("api_version", VERSION),
                    application=None,
                    description=p.get("description"),
                    enabled_events=p["enabled_events"],
                    status="enabled",
                    url=p["url"],
                    _secret="whsec_" + self.ctx.ids.base62(32),
                )
                metadata(endpoint, p)
                await self.emit("webhook_endpoint.created", endpoint)
                return {**endpoint, "secret": endpoint["_secret"]}
            endpoint = self.get(collection, record_id)
            if method == "DELETE":
                self.ctx.store.collection(collection).delete(record_id)
                return {"id": record_id, "object": "webhook_endpoint", "deleted": True}
            self.webhook_parameters(p)
            self.update(endpoint, p, {"url", "description", "enabled_events"})
            if "disabled" in p:
                endpoint["status"] = "disabled" if boolean(p["disabled"]) else "enabled"
            return endpoint
        if collection == "checkout/sessions":
            if method == "POST" and not record_id:
                return await self.create_checkout(p)
            if record_id:
                session = self.get(collection, record_id)
                if method == "GET" and operation == "line_items":
                    return self.listing(
                        collection,
                        rows=session["_line_items"],
                        url=f"/v1/checkout/sessions/{record_id}/line_items",
                    )
                if method == "POST" and operation == "expire":
                    if session["status"] != "open":
                        fail("Only open Checkout Sessions can be expired.")
                    session["status"] = "expired"
                    await self.emit("checkout.session.expired", session)
                    return session
                if method == "POST" and not operation:
                    metadata(session, p)
                    return session
        if collection == "billing_portal/sessions" and method == "POST" and not record_id:
            self.required(p, "customer")
            self.get("customers", p["customer"], "customer")
            session = self.new(
                collection,
                configuration=p.get("configuration", "bpc_" + self.ctx.ids.base62(24)),
                customer=p["customer"],
                flow=p.get("flow_data"),
                locale=p.get("locale"),
                on_behalf_of=None,
                return_url=p.get("return_url"),
                url=None,
            )
            session["url"] = self.url("billing.stripe.com", "/p/session/" + session["id"])
            return session
        if collection == "test_helpers/test_clocks":
            if method == "POST" and not record_id:
                self.required(p, "frozen_time")
                return self.new(
                    collection,
                    frozen_time=timestamp(p["frozen_time"], "frozen_time"),
                    name=p.get("name"),
                    deletes_after=self.now + 2592000,
                    status="ready",
                    status_details={},
                )
            if record_id and method == "DELETE" and not operation:
                clock = self.get(collection, record_id)
                for customer in list(self.ctx.store.collection("customers").where(test_clock=record_id)):
                    await self.customer_route(customer["id"], None, None, "DELETE", {})
                self.ctx.store.collection(collection).delete(clock["id"])
                return {"id": record_id, "object": "test_helpers.test_clock", "deleted": True}
            if record_id and method == "POST" and operation == "advance":
                clock = self.get(collection, record_id)
                self.required(p, "frozen_time")
                target = timestamp(p["frozen_time"], "frozen_time")
                if target <= clock["frozen_time"]:
                    fail("The frozen_time must be later than the current frozen_time.", "frozen_time")
                bounds = []
                for sub in self.ctx.store.collection("subscriptions").where(test_clock=record_id):
                    if sub["status"] in {"canceled", "incomplete_expired"}:
                        continue
                    for item in sub["items"]["data"]:
                        recurring = item["price"]["recurring"]
                        once = period_end(clock["frozen_time"], recurring)
                        bounds.append(period_end(once, recurring, anchor=clock["frozen_time"]))
                maximum = (
                    min(bounds)
                    if bounds
                    else period_end(clock["frozen_time"], {"interval": "year", "interval_count": 2})
                )
                if target > maximum:
                    fail(
                        "The frozen_time cannot be more than two subscription intervals in the future.",
                        "frozen_time",
                    )
                clock["status"] = "advancing"
                await self.emit("test_helpers.test_clock.advancing", clock)
                await self.renew(target, record_id)
                clock.update(frozen_time=target, status="ready")
                await self.emit("test_helpers.test_clock.ready", clock)
                return clock
        if collection == "payouts" and method == "POST":
            if not record_id:
                self.required(p, "amount", "currency")
                amount = integer(p["amount"], "amount", minimum=1)
                available = self.balance()["available"]
                funds = next((r["amount"] for r in available if r["currency"] == p["currency"]), 0)
                if amount > funds:
                    fail(
                        "You have insufficient funds in your Stripe account for this transfer.",
                        code="balance_insufficient",
                    )
                payout = self.new(
                    collection,
                    amount=amount,
                    arrival_date=self.now + 172800,
                    automatic=False,
                    balance_transaction=None,
                    currency=p["currency"],
                    description=p.get("description"),
                    destination=p.get("destination", "ba_" + self.ctx.ids.base62(24)),
                    failure_balance_transaction=None,
                    failure_code=None,
                    failure_message=None,
                    method=p.get("method", "standard"),
                    original_payout=None,
                    reconciliation_status="not_applicable",
                    reversed_by=None,
                    source_type="card",
                    statement_descriptor=None,
                    status="pending",
                    type="bank_account",
                )
                txn = self.transaction(-amount, p["currency"], payout["id"], "payout")
                payout["balance_transaction"] = txn["id"]
                metadata(payout, p)
                await self.emit("payout.created", payout)
                return payout
            if operation == "cancel":
                payout = self.get(collection, record_id)
                if payout["status"] != "pending":
                    fail("Only pending payouts can be canceled.")
                payout["status"] = "canceled"
                self.transaction(payout["amount"], payout["currency"], payout["id"], "payout_cancel")
                await self.emit("payout.canceled", payout)
                return payout
            if not operation:
                payout = self.get(collection, record_id)
                previous = copy.deepcopy(payout)
                self.update(payout, p, set())
                await self.emit("payout.updated", payout, previous)
                return payout
        return None

    def webhook_parameters(self, p: Record) -> None:
        from urllib.parse import urlsplit

        if "enabled_events" in p and not p["enabled_events"]:
            fail("You must specify at least one event.", "enabled_events")
        if "url" in p:
            try:
                url = urlsplit(p["url"])
                if url.scheme not in {"http", "https"} or not url.hostname or url.fragment:
                    fail("Not a valid URL", "url")
                _ = url.port
            except ValueError:
                fail("Not a valid URL", "url")

    def balance(self) -> Record:
        buckets: dict[str, dict[str, int]] = {"available": {}, "pending": {}}
        for txn in self.ctx.store.collection("balance_transactions"):
            status = (
                "available"
                if txn["available_on"] <= self.now or txn["type"] in {"payout", "payout_cancel"}
                else "pending"
            )
            currency = txn["currency"]
            buckets[status][currency] = buckets[status].get(currency, 0) + txn["net"]
        if not buckets["available"]:
            buckets["available"]["usd"] = 0
        if not buckets["pending"]:
            buckets["pending"]["usd"] = 0
        return {
            "object": "balance",
            "livemode": False,
            **{
                status: [
                    {"amount": amount, "currency": currency, "source_types": {"card": amount}}
                    for currency, amount in sorted(rows.items())
                ]
                for status, rows in buckets.items()
            },
        }
