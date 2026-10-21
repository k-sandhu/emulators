"""Card payment state machines and their accounting side effects."""

from __future__ import annotations

import copy
import hashlib
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from emulators.providers.stripe.common import BaseAPI, card_error
from emulators.providers.stripe.wire import Record, boolean, clean, fail, integer, metadata

MAGIC = {
    "visa": "4242424242424242",
    "mastercard": "5555555555554444",
    "amex": "378282246310005",
    "chargeDeclined": "4000000000000002",
    "chargeDeclinedInsufficientFunds": "4000000000009995",
    "chargeDeclinedLostCard": "4000000000009987",
    "chargeDeclinedExpiredCard": "4000000000000069",
    "chargeDeclinedIncorrectCvc": "4000000000000127",
    "chargeDeclinedProcessingError": "4000000000000119",
    "chargeCustomerFail": "4000000000000341",
    "threeDSecure2Required": "4000002500003155",
    "threeDSecureRequired": "4000002760003184",
    "createDispute": "4000000000000259",
    "refundFail": "4000000000005126",
}
DECLINES = {
    "4000000000000002": ("card_declined", "generic_decline"),
    "4000000000009995": ("card_declined", "insufficient_funds"),
    "4000000000009987": ("card_declined", "lost_card"),
    "4000000000000069": ("expired_card", None),
    "4000000000000127": ("incorrect_cvc", None),
    "4000000000000119": ("processing_error", None),
    "4000000000000341": ("card_declined", "generic_decline"),
}
THREE_DS = {"4000002500003155", "4000002760003184"}


class PaymentsAPI(BaseAPI):
    def make_card(self, data: Record) -> tuple[Record, str]:
        if data.get("token"):
            token = data["token"]
            if token.startswith("tok_") and token[4:] in MAGIC:
                number = MAGIC[token[4:]]
            else:
                record = self.get("tokens", token, "card[token]")
                if record["used"]:
                    fail("You cannot use a Stripe token more than once.", "card[token]")
                record["used"] = True
                return copy.deepcopy(record["_card"]), record["_scenario"]
        else:
            self.required(data, "number", "exp_month", "exp_year")
            number = str(data["number"]).replace(" ", "").replace("-", "")
        if not number.isascii() or not number.isdigit() or len(number) not in (15, 16, 19):
            fail("Your card number is incorrect.", "card[number]", code="incorrect_number")
        digits = [int(d) for d in number[::-1]]
        if sum(d if i % 2 == 0 else d * 2 - (9 if d * 2 > 9 else 0) for i, d in enumerate(digits)) % 10:
            fail("Your card number is incorrect.", "card[number]", code="incorrect_number")
        month = integer(data.get("exp_month", 12), "card[exp_month]", minimum=1, maximum=12)
        year = integer(data.get("exp_year", 2030), "card[exp_year]", minimum=0, maximum=9999)
        if year < 100:
            year += 2000
        brand = "visa" if number[0] == "4" else "amex" if number[0] == "3" else "mastercard"
        card: Record = {
            "brand": brand,
            "checks": {
                "address_line1_check": None,
                "address_postal_code_check": None,
                "cvc_check": "pass" if data.get("cvc") else None,
            },
            "country": "US",
            "display_brand": brand,
            "exp_month": month,
            "exp_year": year,
            "fingerprint": hashlib.sha256(number.encode()).hexdigest()[:16],
            "funding": "credit",
            "generated_from": None,
            "last4": number[-4:],
            "networks": {"available": [brand], "preferred": None},
            "three_d_secure_usage": {"supported": True},
            "wallet": None,
        }
        return card, number if number in DECLINES or number in THREE_DS or number in {
            MAGIC["createDispute"],
            MAGIC["refundFail"],
        } else "success"

    def create_payment_method(self, p: Record) -> Record:
        if p.get("type", "card") != "card":
            fail("Only card payment methods are supported by this emulator.", "type")
        self.required(p, "card")
        if not isinstance(p["card"], dict):
            fail("Invalid hash", "card")
        card, scenario = self.make_card(p["card"])
        pm = self.new(
            "payment_methods",
            type="card",
            card=card,
            customer=None,
            billing_details={
                "address": {
                    "city": None,
                    "country": None,
                    "line1": None,
                    "line2": None,
                    "postal_code": None,
                    "state": None,
                },
                "email": None,
                "name": None,
                "phone": None,
                "tax_id": None,
            },
            allow_redisplay=p.get("allow_redisplay", "unspecified"),
            _scenario=scenario,
        )
        self.update(pm, p, {"billing_details", "allow_redisplay"})
        return pm

    def payment_method(self, value: str) -> Record:
        if value.startswith("pm_card_") and value[8:] in MAGIC:
            return self.create_payment_method({"type": "card", "card": {"token": "tok_" + value[8:]}})
        return self.get("payment_methods", value, "payment_method")

    def payment_method_for(self, p: Record, customer: str | None = None) -> Record | None:
        if p.get("payment_method_data"):
            pm = self.create_payment_method(p["payment_method_data"])
        elif p.get("payment_method"):
            pm = self.payment_method(p["payment_method"])
        else:
            return None
        if pm.get("_detached"):
            fail("The payment method you provided has been detached and cannot be reused.", "payment_method")
        if pm["customer"] and pm["customer"] != customer:
            fail("The payment method belongs to a different Customer.", "payment_method")
        if self.customer_scope and customer != self.customer_scope:
            fail("This API key does not have permission to access this customer.", status=403)
        return pm

    def create_intent(self, p: Record, *, setup: bool = False) -> Record:
        if not setup:
            self.required(p, "amount", "currency")
        customer = p.get("customer") or None
        if customer:
            self.customer_time(self.get("customers", customer, "customer"))
        elif self.customer_scope:
            fail("A customer is required for this API key.", "customer", status=403)
        capture = p.get("capture_method", "automatic")
        if capture not in {"automatic", "automatic_async", "manual"}:
            fail("Invalid enum value", "capture_method")
        pm = self.payment_method_for(p, customer)
        collection = "setup_intents" if setup else "payment_intents"
        intent = self.new(
            collection,
            customer=customer,
            payment_method=pm["id"] if pm else None,
            status="requires_confirmation" if pm else "requires_payment_method",
            cancellation_reason=None,
            client_secret=None,
            description=p.get("description"),
            last_setup_error=None,
            next_action=None,
            payment_method_types=p.get("payment_method_types", ["card"]),
            automatic_payment_methods=copy.deepcopy(p.get("automatic_payment_methods")) or None,
            usage=p.get("usage", "off_session"),
        )
        intent["client_secret"] = intent["id"] + "_secret_" + self.ctx.ids.base62(24)
        if not setup:
            for key in ("last_setup_error", "usage"):
                intent.pop(key, None)
            amount = integer(p["amount"], "amount", minimum=1, maximum=99999999)
            intent.update(
                amount=amount,
                amount_capturable=0,
                amount_details={"tip": {}},
                amount_received=0,
                application=None,
                application_fee_amount=None,
                canceled_at=None,
                capture_method=capture,
                confirmation_method=p.get("confirmation_method", "automatic"),
                currency=str(p["currency"]).lower(),
                last_payment_error=None,
                latest_charge=None,
                on_behalf_of=None,
                payment_method_options={
                    "card": {
                        "installments": None,
                        "mandate_options": None,
                        "network": None,
                        "request_three_d_secure": "automatic",
                    }
                },
                processing=None,
                receipt_email=p.get("receipt_email"),
                review=None,
                setup_future_usage=p.get("setup_future_usage"),
                shipping=p.get("shipping"),
                source=None,
                statement_descriptor=None,
                statement_descriptor_suffix=None,
                transfer_data=None,
                transfer_group=None,
            )
        else:
            intent.update(
                application=None,
                attach_to_self=False,
                flow_directions=None,
                latest_attempt=None,
                mandate=None,
                on_behalf_of=None,
                payment_method_options={
                    "card": {"mandate_options": None, "network": None, "request_three_d_secure": "automatic"}
                },
                single_use_mandate=None,
            )
        if intent["automatic_payment_methods"] and "enabled" in intent["automatic_payment_methods"]:
            intent["automatic_payment_methods"]["enabled"] = boolean(
                intent["automatic_payment_methods"]["enabled"]
            )
        metadata(intent, p)
        return intent

    async def confirm_intent(
        self, intent: Record, p: Record, *, setup: bool = False, complete_3ds: bool = False
    ) -> Record:
        self.check_client_secret(intent)
        if intent["status"] not in {"requires_payment_method", "requires_confirmation", "requires_action"}:
            self.invalid_state(
                "This intent cannot be confirmed because it has already succeeded or been canceled.",
                setup=setup,
            )
        if self.publishable and intent.get("confirmation_method") == "manual":
            fail("This PaymentIntent requires confirmation with a secret key.", status=403)
        if not setup:
            if (
                self.publishable
                and intent.get("setup_future_usage")
                and "setup_future_usage" in p
                and p["setup_future_usage"] != intent["setup_future_usage"]
                and (intent["setup_future_usage"], p["setup_future_usage"]) != ("on_session", "off_session")
            ):
                fail(
                    "You can only update setup_future_usage from on_session to off_session with a publishable key.",
                    "setup_future_usage",
                )
            self.update(intent, p, {"capture_method", "receipt_email", "setup_future_usage", "shipping"})
        pm = self.payment_method_for(p, intent["customer"])
        if pm:
            if pm["id"] != intent["payment_method"]:
                intent.pop("_authenticated", None)
            intent["payment_method"] = pm["id"]
        elif intent["payment_method"]:
            pm = self.payment_method_for({"payment_method": intent["payment_method"]}, intent["customer"])
        if not pm:
            fail(
                "You cannot confirm this PaymentIntent because it's missing a payment method.",
                "payment_method",
            )
        scenario = pm["_scenario"]
        event_prefix = "setup_intent" if setup else "payment_intent"
        requires_authentication = (
            scenario in THREE_DS and not complete_3ds and not intent.get("_authenticated")
        )
        authentication_failed = (
            requires_authentication
            and not setup
            and (boolean(p.get("error_on_requires_action", False)) or boolean(p.get("off_session", False)))
        )
        if requires_authentication and not authentication_failed:
            intent.update(
                status="requires_action",
                next_action={
                    "type": "use_stripe_sdk",
                    "use_stripe_sdk": {
                        "type": "stripe_3ds2_fingerprint",
                        "three_d_secure_2_source": self.ctx.ids.prefixed("src", 24),
                    },
                },
            )
            if p.get("return_url"):
                intent["next_action"] = {
                    "type": "redirect_to_url",
                    "redirect_to_url": {
                        "return_url": p["return_url"],
                        "url": self.url("checkout.stripe.com", "/3ds/" + intent["id"]),
                    },
                }
            await self.emit(event_prefix + ".requires_action", intent)
            return intent
        if authentication_failed or (scenario in DECLINES and (not setup or scenario != "4000000000000341")):
            code, decline = (
                ("authentication_required", "authentication_required")
                if authentication_failed
                else DECLINES[scenario]
            )
            if not setup:
                charge = self.make_charge(intent, pm, success=False, failure_code=code, decline_code=decline)
                intent["latest_charge"] = charge["id"]
                await self.emit("charge.failed", charge)
            intent.update(status="requires_payment_method", next_action=None)
            error = card_error(intent, code, decline)
            detail = {k: v for k, v in error.body["error"].items() if k != "payment_intent"}
            detail["payment_method"] = clean(pm)
            intent["last_setup_error" if setup else "last_payment_error"] = detail
            await self.emit(
                event_prefix + ".setup_failed" if setup else event_prefix + ".payment_failed", intent
            )
            error.body["error"]["setup_intent" if setup else "payment_intent"] = clean(intent)
            if setup:
                error.body["error"].pop("payment_intent", None)
            raise error
        intent.update(next_action=None, status="succeeded")
        if setup:
            intent["last_setup_error"] = None
        else:
            intent["last_payment_error"] = None
            if intent["capture_method"] == "manual":
                intent.update(status="requires_capture", amount_capturable=intent["amount"])
            else:
                intent["amount_received"] = intent["amount"]
            charge = self.make_charge(intent, pm)
            intent["latest_charge"] = charge["id"]
            await self.emit("charge.succeeded", charge)
            if scenario == MAGIC["createDispute"]:
                dispute = self.make_dispute(charge)
                await self.emit("charge.dispute.created", dispute)
        if intent["customer"] and (setup or intent.get("setup_future_usage")) and not pm["customer"]:
            pm["customer"] = intent["customer"]
            await self.emit("payment_method.attached", pm)
        await self.emit(
            event_prefix
            + (".amount_capturable_updated" if intent["status"] == "requires_capture" else ".succeeded"),
            intent,
        )
        if not setup and intent["status"] == "succeeded":
            await self.payment_succeeded(intent)
        return intent

    async def payment_succeeded(self, intent: Record) -> None:
        """Billing overrides this to propagate a directly confirmed invoice payment."""

    def transaction(self, amount: int, currency: str, source: str, kind: str, *, fee: int = 0) -> Record:
        txn = self.new(
            "balance_transactions",
            amount=amount,
            available_on=self.now + 172800,
            currency=currency,
            description=None,
            exchange_rate=None,
            fee=fee,
            fee_details=[
                {
                    "amount": fee,
                    "application": None,
                    "currency": currency,
                    "description": "Stripe processing fees",
                    "type": "stripe_fee",
                }
            ]
            if fee
            else [],
            net=amount - fee,
            reporting_category="charge" if kind == "charge" else kind,
            source=source,
            status="available" if self.seeding else "pending",
            type=kind,
        )
        txn.pop("metadata", None)
        txn.pop("livemode", None)
        return txn

    def make_charge(
        self,
        pi: Record,
        pm: Record,
        *,
        success: bool = True,
        failure_code: str | None = None,
        decline_code: str | None = None,
    ) -> Record:
        captured = success and pi["capture_method"] != "manual"
        if not success:
            failure_code = failure_code or DECLINES.get(pm["_scenario"], ("card_declined", None))[0]
        failure_message = card_error(pi, failure_code).body["error"]["message"] if failure_code else None
        charge = self.new(
            "charges",
            amount=pi["amount"],
            amount_captured=pi["amount"] if captured else 0,
            amount_refunded=0,
            application=None,
            application_fee=None,
            application_fee_amount=None,
            balance_transaction=None,
            billing_details=copy.deepcopy(pm["billing_details"]),
            calculated_statement_descriptor="HALCYON ADVISORY",
            captured=captured,
            currency=pi["currency"],
            customer=pi["customer"],
            description=pi.get("description"),
            disputed=False,
            failure_balance_transaction=None,
            failure_code=failure_code,
            failure_message=failure_message,
            fraud_details={},
            on_behalf_of=None,
            outcome={
                "network_status": "approved_by_network" if success else "declined_by_network",
                "reason": None if success else decline_code or failure_code,
                "risk_level": "normal",
                "risk_score": 5,
                "seller_message": "Payment complete." if success else failure_message,
                "type": "authorized" if success else "issuer_declined",
            },
            paid=success,
            payment_intent=pi["id"],
            payment_method=pm["id"],
            payment_method_details={
                "card": {
                    k: copy.deepcopy(v)
                    for k, v in pm["card"].items()
                    if k not in {"generated_from", "networks", "display_brand", "three_d_secure_usage"}
                },
                "type": "card",
            },
            receipt_email=pi.get("receipt_email"),
            receipt_number=None,
            receipt_url=self.url("pay.stripe.com", "/receipts/" + pi["id"]),
            refunded=False,
            refunds={"object": "list", "data": [], "has_more": False, "url": ""},
            review=None,
            shipping=pi.get("shipping"),
            source=None,
            statement_descriptor=None,
            statement_descriptor_suffix=None,
            status="succeeded" if success else "failed",
            transfer_data=None,
            transfer_group=None,
            _scenario=pm["_scenario"],
        )
        charge["refunds"]["url"] = "/v1/charges/" + charge["id"] + "/refunds"
        charge["payment_method_details"]["card"].update(
            installments=None, network=pm["card"]["brand"], three_d_secure=None
        )
        charge["metadata"] = copy.deepcopy(pi["metadata"])
        if captured:
            self.capture_accounting(charge, charge["amount"])
        return charge

    def capture_accounting(self, charge: Record, amount: int) -> None:
        fee = int((Decimal(amount) * Decimal("0.029") + 30).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        txn = self.transaction(amount, charge["currency"], charge["id"], "charge", fee=fee)
        charge.update(balance_transaction=txn["id"], amount_captured=amount, captured=True)

    async def intent_operation(
        self, intent: Record, operation: str, p: Record, *, setup: bool = False
    ) -> Record:
        if operation == "confirm":
            return await self.confirm_intent(intent, p, setup=setup)
        previous = copy.deepcopy(intent)
        prefix = "setup_intent" if setup else "payment_intent"
        if operation == "cancel":
            allowed = {
                "requires_payment_method",
                "requires_confirmation",
                "requires_action",
            } | ({"requires_capture", "processing"} if not setup else set())
            if intent["status"] not in allowed:
                self.invalid_state(
                    "You cannot cancel this intent because it has a status of " + intent["status"] + ".",
                    setup=setup,
                )
            reason = p.get("cancellation_reason")
            reasons = {None, "duplicate", "requested_by_customer", "abandoned"} | (
                {"fraudulent"} if not setup else set()
            )
            if reason not in reasons:
                fail("Invalid enum value", "cancellation_reason")
            intent.update(status="canceled", cancellation_reason=reason, next_action=None)
            if not setup:
                intent.update(canceled_at=self.now, amount_capturable=0)
            await self.emit(prefix + ".canceled", intent, previous)
        elif not setup and operation == "capture":
            if intent["status"] != "requires_capture":
                self.invalid_state(
                    "This PaymentIntent could not be captured because it has a status of "
                    + intent["status"]
                    + "."
                )
            amount = integer(
                p.get("amount_to_capture", intent["amount_capturable"]),
                "amount_to_capture",
                minimum=1,
                maximum=intent["amount_capturable"],
            )
            charge = self.get("charges", intent["latest_charge"])
            self.capture_accounting(charge, amount)
            intent.update(status="succeeded", amount_received=amount, amount_capturable=0)
            await self.emit("charge.captured", charge)
            await self.emit("payment_intent.succeeded", intent, previous)
            await self.payment_succeeded(intent)
        elif not setup and operation == "increment_authorization":
            if intent["status"] != "requires_capture":
                self.invalid_state("Only uncaptured PaymentIntents can have their authorization incremented.")
            self.required(p, "amount")
            amount = integer(p["amount"], "amount", minimum=intent["amount"] + 1)
            intent.update(amount=amount, amount_capturable=amount)
            self.get("charges", intent["latest_charge"])["amount"] = amount
            await self.emit("payment_intent.amount_capturable_updated", intent, previous)
        else:
            fail("Unrecognized request URL.", status=404)
        return intent

    async def create_refund(self, p: Record) -> Record:
        if not p.get("charge") and not p.get("payment_intent"):
            fail("Must provide charge or payment_intent.", "charge", code="parameter_missing")
        charge_id = p.get("charge")
        if not charge_id:
            pi = self.get("payment_intents", p["payment_intent"], "payment_intent")
            charge_id = pi["latest_charge"]
        charge = self.get("charges", charge_id, "charge")
        remaining = charge["amount_captured"] - charge["amount_refunded"]
        if not charge["paid"] or not charge["captured"]:
            fail("This charge has not been captured.", "charge", code="charge_not_refundable")
        if remaining <= 0:
            fail("Charge has already been refunded.", "charge", code="charge_already_refunded")
        amount = integer(p.get("amount", remaining), "amount", minimum=1, maximum=remaining)
        if p.get("reason") not in (None, "duplicate", "fraudulent", "requested_by_customer"):
            fail("Invalid enum value", "reason")
        failed = charge.get("_scenario") == MAGIC["refundFail"]
        refund = self.new(
            "refunds",
            amount=amount,
            balance_transaction=None,
            charge=charge["id"],
            currency=charge["currency"],
            description=None,
            payment_intent=charge["payment_intent"],
            reason=p.get("reason"),
            receipt_number=None,
            source_transfer_reversal=None,
            status="failed" if failed else "succeeded",
            transfer_reversal=None,
            destination_details={
                "card": {
                    "reference": None,
                    "reference_status": "pending",
                    "reference_type": "acquirer_reference_number",
                    "type": "refund",
                },
                "type": "card",
            },
        )
        metadata(refund, p)
        previous = copy.deepcopy(charge)
        if failed:
            refund.update(failure_reason="declined", failure_balance_transaction=None)
        else:
            txn = self.transaction(-amount, charge["currency"], refund["id"], "refund")
            refund["balance_transaction"] = txn["id"]
            charge["amount_refunded"] += amount
            charge["refunded"] = charge["amount_refunded"] == charge["amount_captured"]
        charge["refunds"]["data"].insert(0, copy.deepcopy(refund))
        await self.emit("refund.created", refund)
        await self.emit("refund.updated", refund)
        if not failed:
            await self.emit("charge.refunded", charge, previous)
        else:
            await self.emit("refund.failed", refund)
        return refund

    def make_dispute(self, charge: Record) -> Record:
        charge["disputed"] = True
        dispute = self.new(
            "disputes",
            amount=charge["amount_captured"],
            balance_transactions=[],
            charge=charge["id"],
            currency=charge["currency"],
            evidence={},
            evidence_details={
                "due_by": self.now + 604800,
                "has_evidence": False,
                "past_due": False,
                "submission_count": 0,
            },
            is_charge_refundable=True,
            payment_intent=charge["payment_intent"],
            reason="fraudulent",
            status="needs_response",
            payment_method_details={
                "card": {
                    "brand": charge["payment_method_details"]["card"]["brand"],
                    "case_type": "chargeback",
                    "network_reason_code": "10.4",
                },
                "type": "card",
            },
        )
        txn = self.transaction(-dispute["amount"], dispute["currency"], dispute["id"], "adjustment", fee=1500)
        txn["reporting_category"] = "dispute"
        dispute["balance_transactions"] = [copy.deepcopy(txn)]
        return dispute

    async def payment_route(
        self, collection: str, record_id: str | None, operation: str | None, method: str, p: Record
    ) -> Any:
        if collection == "tokens" and method == "POST" and not record_id:
            card, scenario = self.make_card(p.get("card", {}))
            full_card = copy.deepcopy(card)
            card = {
                k: v
                for k, v in card.items()
                if k
                not in {
                    "display_brand",
                    "networks",
                    "three_d_secure_usage",
                    "wallet",
                    "checks",
                    "generated_from",
                }
            }
            card.update(
                id=self.ctx.ids.prefixed("card", 24),
                object="card",
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
            )
            token = self.new(
                "tokens",
                card=card,
                client_ip=None,
                type="card",
                used=False,
                _scenario=scenario,
                _card=full_card,
            )
            token.pop("metadata", None)
            return token
        if collection == "payment_methods":
            if method == "POST" and not record_id:
                self.required(p, "type")
                pm = self.create_payment_method(p)
                await self.emit("payment_method.created", pm)
                return pm
            if method == "GET":
                return None
            pm = self.payment_method(record_id) if record_id else None
            if method == "POST" and operation in {"attach", "detach"}:
                previous = copy.deepcopy(pm)
                if operation == "attach":
                    self.required(p, "customer")
                    self.get("customers", p["customer"], "customer")
                    if pm.get("_detached") or (pm["customer"] and pm["customer"] != p["customer"]):
                        fail("The payment method cannot be attached to this Customer.", "payment_method")
                    pm["customer"] = p["customer"]
                else:
                    if not pm["customer"]:
                        fail("The payment method is not attached to a customer.")
                    customer = self.get("customers", pm["customer"])
                    if customer["invoice_settings"]["default_payment_method"] == pm["id"]:
                        customer["invoice_settings"]["default_payment_method"] = None
                    pm.update(customer=None, _detached=True)
                await self.emit(
                    "payment_method.attached" if operation == "attach" else "payment_method.detached",
                    pm,
                    previous,
                )
                return pm
            if method == "POST" and pm and not operation:
                self.update(pm, p, {"billing_details", "allow_redisplay"})
                if "card" in p:
                    for key in {"exp_month", "exp_year"} & p["card"].keys():
                        pm["card"][key] = integer(
                            p["card"][key],
                            f"card[{key}]",
                            minimum=1,
                            maximum=12 if key == "exp_month" else 9999,
                        )
                await self.emit("payment_method.updated", pm)
                return pm
        if collection in {"payment_intents", "setup_intents"}:
            setup = collection == "setup_intents"
            if method == "POST" and not record_id:
                if boolean(p.get("confirm", False)) and not (
                    p.get("payment_method") or p.get("payment_method_data")
                ):
                    fail("You cannot confirm this intent without a payment method.", "payment_method")
                intent = self.create_intent(p, setup=setup)
                await self.emit(("setup_intent" if setup else "payment_intent") + ".created", intent)
                if boolean(p.get("confirm", False)):
                    await self.confirm_intent(intent, p, setup=setup)
                return intent
            if record_id:
                intent = self.get(collection, record_id)
                self.check_client_secret(intent)
                if method == "GET" and not operation:
                    return intent
                if method == "POST":
                    if operation:
                        return await self.intent_operation(intent, operation, p, setup=setup)
                    if intent["status"] in {"canceled", "succeeded"} and any(
                        key in p for key in {"amount", "payment_method", "customer"}
                    ):
                        self.invalid_state("This intent can no longer be updated.", setup=setup)
                    self.update(intent, p, {"description", "receipt_email", "setup_future_usage", "shipping"})
                    if "amount" in p and not setup:
                        intent["amount"] = integer(p["amount"], "amount", minimum=1, maximum=99999999)
                    if "customer" in p:
                        customer = p["customer"] or None
                        if customer:
                            self.get("customers", customer, "customer")
                        elif self.customer_scope:
                            fail("A customer is required for this API key.", "customer", status=403)
                        if intent["payment_method"] and "payment_method" not in p:
                            self.payment_method_for({"payment_method": intent["payment_method"]}, customer)
                        intent["customer"] = customer
                    if "payment_method" in p:
                        pm = self.payment_method_for(p, intent["customer"])
                        intent["payment_method"] = pm["id"] if pm else None
                        intent["status"] = "requires_confirmation" if pm else "requires_payment_method"
                        intent["next_action"] = None
                        intent.pop("_authenticated", None)
                    intent["last_setup_error" if setup else "last_payment_error"] = None
                    return intent
        if collection == "refunds" and method == "POST" and not record_id:
            return await self.create_refund(p)
        if collection == "disputes" and method == "POST" and record_id:
            dispute = self.get(collection, record_id)
            previous = copy.deepcopy(dispute)
            if dispute["status"] != "needs_response":
                fail("This dispute is no longer awaiting evidence.")
            if operation == "close":
                dispute["status"] = "lost"
                await self.emit("charge.dispute.closed", dispute, previous)
            elif not operation:
                self.update(dispute, p, {"evidence"})
                dispute["evidence_details"]["has_evidence"] = bool(dispute["evidence"])
                if boolean(p.get("submit", True)):
                    dispute["status"] = "under_review"
                    dispute["evidence_details"]["submission_count"] += 1
                await self.emit("charge.dispute.updated", dispute, previous)
            else:
                fail("Unrecognized request URL.", status=404)
            return dispute
        if collection in {"charges", "refunds"} and method == "POST" and record_id and not operation:
            record = self.get(collection, record_id)
            previous = copy.deepcopy(record)
            self.update(
                record, p, {"description", "receipt_email", "shipping"} if collection == "charges" else set()
            )
            await self.emit(
                "charge.updated" if collection == "charges" else "refund.updated", record, previous
            )
            return record
        return None
