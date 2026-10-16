"""Shared resource storage, account authorization, expansion, and event delivery."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from typing import Any

from fastapi import Request

from emulators.core.auth import basic_credentials, bearer_token
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext, external_base, url_for_host
from emulators.providers.stripe.wire import (
    PUBLIC_KEY,
    SECRET_KEY,
    VERSION,
    Record,
    clean,
    fail,
    integer,
    list_object,
    metadata,
    search_predicate,
)

RESOURCES = {
    "customers": ("cus", "customer"),
    "payment_methods": ("pm", "payment_method"),
    "payment_intents": ("pi", "payment_intent"),
    "setup_intents": ("seti", "setup_intent"),
    "charges": ("ch", "charge"),
    "refunds": ("re", "refund"),
    "disputes": ("dp", "dispute"),
    "products": ("prod", "product"),
    "prices": ("price", "price"),
    "subscriptions": ("sub", "subscription"),
    "subscription_items": ("si", "subscription_item"),
    "invoices": ("in", "invoice"),
    "invoiceitems": ("ii", "invoiceitem"),
    "invoice_lines": ("il", "line_item"),
    "events": ("evt", "event"),
    "webhook_endpoints": ("we", "webhook_endpoint"),
    "balance_transactions": ("txn", "balance_transaction"),
    "payouts": ("po", "payout"),
    "tokens": ("tok", "token"),
    "sources": ("card", "card"),
    "tax_ids": ("txi", "tax_id"),
    "customer_balance_transactions": ("cbtxn", "customer_balance_transaction"),
    "checkout/sessions": ("cs_test", "checkout.session"),
    "billing_portal/sessions": ("bps", "billing_portal.session"),
    "test_helpers/test_clocks": ("clock", "test_helpers.test_clock"),
    "files": ("file", "file"),
    "invoice_payments": ("inpay", "invoice_payment"),
}
PREFIX_COLLECTION = {prefix + "_": collection for collection, (prefix, _) in RESOURCES.items()}
CATALOG = {"products", "prices"}


class BaseAPI:
    def __init__(
        self,
        ctx: ProviderContext,
        request: Request | None = None,
        params: Record | None = None,
        *,
        seed: bool = False,
    ) -> None:
        self.ctx, self.request, self.params, self.seeding = ctx, request, params or {}, seed
        self.customer_scope: str | None = None
        self.publishable = False
        self.scopes: list[str] = []
        self.time_override = False
        self.now = int(ctx.world.anchor.timestamp()) if seed else ctx.clock.timestamp()

    def authorize(self, collection: str, operation: str = "read") -> None:
        if self.seeding or self.request is None:
            return
        key = bearer_token(self.request) or (basic_credentials(self.request) or ("", ""))[0]
        grant = self.ctx.tokens.resolve(key)
        if grant:
            self.scopes = grant.scopes
            if operation != "read" and grant.extra.get("stripe_scope") == "read_only":
                fail("This application has read-only access to your account.", status=403)
            if not grant.app_only:
                person = next((p for p in self.ctx.world.people if grant.user_id in (p.id, p.email)), None)
                if not person or not person.active:
                    fail("This API key does not have permission to access this resource.", status=403)
                if person.is_external:
                    customer = self.ctx.store.collection("customers").first(_client_id=person.company_id)
                    self.customer_scope = customer["id"] if customer else "_no_customer"
                elif not (person.is_admin or person.department_id == "finance"):
                    fail("This API key does not have permission to access this resource.", status=403)
            if self.scopes and not any(
                s in self.scopes
                for s in ("*", "stripe:*", f"{collection}:write", f"{collection}:{operation}")
            ):
                fail("This API key does not have the required permissions for this endpoint.", status=403)
        elif self.ctx.tokens.get(key):
            fail("Expired API Key provided.", status=401)
        elif key.startswith("pk_test_") and (self.ctx.tokens.lenient_clients or key == PUBLIC_KEY):
            self.publishable = True
            allowed = self.request.method == "POST" and collection in {"tokens", "payment_methods"}
            allowed |= (
                collection in {"payment_intents", "setup_intents"}
                and (self.request.method == "GET" or self.request.url.path.endswith("/confirm"))
                and bool(self.params.get("client_secret"))
            )
            if not allowed:
                fail(
                    "This API call cannot be made with a publishable API key. Please use a secret API key.",
                    status=403,
                )
        elif not (
            key.startswith(("sk_test_", "rk_test_"))
            and (self.ctx.tokens.lenient_clients or key == SECRET_KEY)
        ):
            masked = key[:8] + "****" + key[-4:] if key else ""
            fail(
                f"Invalid API Key provided: {masked}"
                if key
                else "You did not provide an API key. You need to provide your API key in the Authorization header, using Bearer auth (e.g. 'Authorization: Bearer YOUR_SECRET_KEY').",
                status=401,
            )
        account = self.request.headers.get("stripe-account")
        if account and account != self.ctx.world.org.stripe_account_id:
            fail(
                f"The provided key does not have access to account '{account}'.",
                status=403,
                code="account_invalid",
            )
        if self.customer_scope and collection not in CATALOG | {
            "customers",
            "payment_methods",
            "payment_intents",
            "setup_intents",
            "invoices",
            "subscriptions",
            "subscription_items",
            "invoiceitems",
            "sources",
            "tax_ids",
            "checkout/sessions",
        }:
            fail("This API key does not have permission to access this resource.", status=403)
        if self.customer_scope and collection in CATALOG and operation != "read":
            fail("This API key does not have permission to modify the product catalog.", status=403)

    def visible(self, collection: str, record: Record) -> bool:
        if not self.customer_scope or collection in CATALOG:
            return True
        customer = record["id"] if collection == "customers" else record.get("customer")
        customer = customer or record.get("_owner_customer")
        if not customer and collection == "subscription_items":
            sub = self.ctx.store.collection("subscriptions").get(record.get("subscription"))
            customer = sub.get("customer") if sub else None
        if not customer and collection in {"invoice_lines", "invoice_payments"}:
            invoice = self.ctx.store.collection("invoices").get(record.get("invoice"))
            customer = invoice.get("customer") if invoice else None
        if not customer and collection == "refunds":
            charge = self.ctx.store.collection("charges").get(record.get("charge"))
            customer = charge.get("customer") if charge else None
        if not customer and collection == "balance_transactions":
            source = record.get("source", "")
            related = next(
                (
                    self.ctx.store.collection(coll).get(source)
                    for prefix, coll in PREFIX_COLLECTION.items()
                    if source.startswith(prefix)
                ),
                None,
            )
            return bool(
                related and self.visible("charges" if source.startswith("ch_") else "refunds", related)
            )
        return customer == self.customer_scope

    def customer_time(self, customer: Record) -> None:
        if self.seeding or self.time_override or not customer.get("test_clock"):
            return
        clock = self.ctx.store.collection("test_helpers/test_clocks").get(customer["test_clock"])
        if clock:
            self.now = clock["frozen_time"]

    def get(self, collection: str, record_id: Any, param: str = "id") -> Record:
        record = self.ctx.store.collection(collection).get(record_id)
        if record is None or not self.visible(collection, record):
            _, obj = RESOURCES.get(collection, ("", collection.rstrip("s")))
            fail(
                f"No such {obj.replace('_', ' ')}: '{record_id}'",
                param,
                code="resource_missing",
                status=404,
                request_log_url="https://dashboard.stripe.com/test/logs/"
                + (
                    getattr(self.request.state, "stripe_request_id", "req_emulator")
                    if self.request
                    else "req_emulator"
                ),
            )
        if record.get("deleted"):
            return record
        return record

    def new(self, collection: str, **fields: Any) -> Record:
        prefix, obj = RESOURCES[collection]
        counter = self.ctx.store.collection("_counters")
        sequence = counter.get("objects") or {"id": "objects", "value": 0}
        sequence["value"] += 1
        counter.put(sequence)
        record: Record = {
            "id": self.ctx.ids.prefixed(prefix, 24),
            "object": obj,
            "created": self.now,
            "livemode": False,
            "metadata": {},
            "_order": sequence["value"],
            **copy.deepcopy(fields),
        }
        if self.customer_scope:
            record["_owner_customer"] = self.customer_scope
        if self.request:
            record["_origin"] = {
                "base": external_base(self.request),
                "host": self.request.headers.get(
                    "x-forwarded-host", self.request.headers.get("host", "api.stripe.com")
                ),
                "scheme": self.request.headers.get("x-forwarded-proto", self.request.url.scheme),
            }
        if collection in {
            "subscription_items",
            "sources",
            "customer_balance_transactions",
            "billing_portal/sessions",
        }:
            record.pop("livemode", None)
        if collection == "invoice_lines":
            record.pop("created", None)
        if collection in {"sources", "billing_portal/sessions", "test_helpers/test_clocks"}:
            record.pop("metadata", None)
        self.ctx.store.collection(collection).put(record)
        return record

    def update(self, record: Record, params: Record, fields: set[str]) -> Record:
        for key in fields & params.keys():
            value = params[key]
            if key in {"active", "cancel_at_period_end", "auto_advance"}:
                from emulators.providers.stripe.wire import boolean

                value = boolean(value)
            if isinstance(value, dict) and isinstance(record.get(key), dict):
                record[key].update(copy.deepcopy(value))
            else:
                record[key] = copy.deepcopy(value) if value != "" else None
        metadata(record, params)
        return record

    def required(self, params: Record, *names: str) -> None:
        for name in names:
            if params.get(name) in (None, ""):
                fail(f"Missing required param: {name}.", name, code="parameter_missing")

    def url(self, host: str, path: str) -> str:
        return url_for_host(self.request, self.ctx, host, path) if self.request else f"https://{host}{path}"

    async def emit(self, event_type: str, record: Record, previous: Record | None = None) -> Record:
        request_id = getattr(self.request.state, "stripe_request_id", None) if self.request else None
        event = self.new(
            "events",
            type=event_type,
            api_version=self.request.headers.get("stripe-version", VERSION) if self.request else VERSION,
            data={"object": self.view(record)},
            request={
                "id": request_id,
                "idempotency_key": self.request.headers.get("idempotency-key") if self.request else None,
            },
            pending_webhooks=0,
        )
        event.pop("metadata", None)
        if previous is not None:
            event["data"]["previous_attributes"] = {
                k: clean(v) for k, v in previous.items() if not k.startswith("_") and v != record.get(k)
            }
        endpoints = [
            e
            for e in self.ctx.store.collection("webhook_endpoints")
            if e.get("status") == "enabled"
            and ("*" in e["enabled_events"] or event_type in e["enabled_events"])
        ]
        event["pending_webhooks"] = len(endpoints)
        payload = json.dumps(clean(event), separators=(",", ":")).encode()
        for endpoint in endpoints:
            secret = endpoint["_secret"]

            def sign(body: bytes, attempt: int, secret: str = secret) -> dict[str, str]:
                timestamp = self.ctx.clock.timestamp()
                digest = hmac.new(
                    secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
                ).hexdigest()
                return {"Stripe-Signature": f"t={timestamp},v1={digest}"}

            result = await self.ctx.webhooks.deliver(
                endpoint["url"],
                payload,
                event_type=event_type,
                sign=sign,
                metadata={"event": event["id"], "endpoint": endpoint["id"]},
            )
            if result["status"] == "delivered":
                event["pending_webhooks"] -= 1
        return event

    def expand(self, record: Record) -> Record:
        result = clean(record)
        expansions = self.params.get("expand", [])
        if not isinstance(expansions, list):
            fail("Invalid array", "expand")
        for path in expansions:
            parts = path.split(".")
            if len(parts) > 4:
                fail("You cannot expand more than 4 levels of a property.", "expand")
            self._expand_at(result, parts)
        return result

    def view(self, value: Any) -> Any:
        return clean(value)

    def _expand_at(self, node: Any, parts: list[str]) -> None:
        if not parts or node is None:
            return
        if isinstance(node, list):
            for item in node:
                self._expand_at(item, parts)
            return
        if not isinstance(node, dict):
            return
        key, *rest = parts
        value = node.get(key)
        if isinstance(value, str):
            collection = next(
                (c for prefix, c in PREFIX_COLLECTION.items() if value.startswith(prefix)), None
            )
            if collection:
                value = clean(self.get(collection, value))
                node[key] = value
        self._expand_at(value, rest)

    def listing(
        self,
        collection: str,
        params: Record | None = None,
        *,
        rows: list[Record] | None = None,
        url: str | None = None,
    ) -> Record:
        p = params if params is not None else self.params
        records = rows if rows is not None else self.ctx.store.collection(collection).values()
        return list_object(
            [r for r in records if not r.get("deleted") and self.visible(collection, r)],
            p,
            url or "/v1/" + collection,
        )

    def search(self, collection: str) -> Record:
        self.required(self.params, "query")
        predicate = search_predicate(self.params["query"])
        rows = [
            r
            for r in self.ctx.store.collection(collection)
            if not r.get("deleted") and self.visible(collection, r) and predicate(r)
        ]
        rows.sort(key=lambda r: (r["created"], r.get("_order", 0)), reverse=True)
        limit = integer(self.params.get("limit", 10), "limit", minimum=1, maximum=100)
        offset = 0
        if self.params.get("page"):
            token = self.ctx.store.collection("_search_pages").get(self.params["page"])
            if not token or token["query"] != self.params["query"] or token["collection"] != collection:
                fail("Invalid page token", "page")
            offset = token["offset"]
        more = offset + limit < len(rows)
        next_page = None
        if more:
            next_page = self.ctx.ids.base62(24)
            self.ctx.store.collection("_search_pages").put(
                {
                    "id": next_page,
                    "offset": offset + limit,
                    "query": self.params["query"],
                    "collection": collection,
                }
            )
        return {
            "object": "search_result",
            "data": rows[offset : offset + limit],
            "has_more": more,
            "next_page": next_page,
            "url": f"/v1/{collection}/search",
            "total_count": len(rows),
        }

    def check_client_secret(self, record: Record) -> None:
        if self.publishable and self.params.get("client_secret") != record.get("client_secret"):
            fail(
                "The client_secret provided does not match the client_secret associated with the PaymentIntent.",
                "client_secret",
            )

    def invalid_state(self, message: str) -> None:
        fail(message, code="payment_intent_unexpected_state")


def copy_record(record: Record) -> Record:
    return copy.deepcopy(record)


def card_error(pi: Record, code: str, decline: str | None = None) -> EmulatorError:
    error: Record = {
        "type": "card_error",
        "code": code,
        "message": {
            "expired_card": "Your card has expired.",
            "incorrect_cvc": "Your card's security code is incorrect.",
            "processing_error": "An error occurred while processing your card. Try again in a little bit.",
        }.get(code, "Your card was declined."),
        "param": "",
        "charge": pi.get("latest_charge"),
        "payment_intent": clean(pi),
    }
    if decline:
        error["decline_code"] = decline
    return EmulatorError(402, {"error": error})
