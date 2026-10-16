"""Dispatch supported Stripe paths without silently accepting unsupported mutations."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from typing import Any

from fastapi import Request
from starlette.responses import HTMLResponse, Response

from emulators.providers.stripe.commerce import CommerceAPI
from emulators.providers.stripe.common import RESOURCES
from emulators.providers.stripe.wire import Record, clean, decode, fail
from emulators.world.documents import make_pdf

SEARCHABLE = {"customers", "payment_intents", "charges", "invoices", "subscriptions", "prices", "products"}
READABLE = set(RESOURCES) - {
    "invoice_lines",
    "customer_balance_transactions",
    "sources",
    "tax_ids",
    "billing_portal/sessions",
}


class StripeAPI(CommerceAPI):
    async def dispatch(self, resource: str, method: str) -> Any:
        parts = resource.strip("/").split("/")
        collection = parts.pop(0)
        if collection in {"checkout", "billing_portal", "test_helpers"} and parts:
            collection += "/" + parts.pop(0)
        record_id = parts.pop(0) if parts else None
        operation = parts.pop(0) if parts else None
        child_id = parts.pop(0) if parts else None
        if parts:
            fail("Unrecognized request URL.", status=404)
        self.authorize(collection, "read" if method == "GET" else "write")
        p = self.params
        record = self.ctx.store.collection(collection).get(record_id) if record_id else None
        customer_id = p.get("customer") or (record.get("customer") if record else None)
        if customer_id:
            self.customer_time(self.get("customers", customer_id, "customer"))
        if collection == "balance" and method == "GET" and not record_id:
            return self.balance()
        if collection == "account" and method == "GET" and not record_id:
            return {
                "id": self.ctx.world.org.stripe_account_id,
                "object": "account",
                "business_profile": {"name": self.ctx.world.org.name, "url": self.ctx.world.org.website},
                "country": self.ctx.world.org.address.country_code,
                "default_currency": self.ctx.world.org.currency.lower(),
                "charges_enabled": True,
                "payouts_enabled": True,
                "details_submitted": True,
                "type": "standard",
                "email": self.ctx.world.employees[0].email,
                "metadata": {},
            }
        if collection not in RESOURCES:
            fail("Unrecognized request URL.", status=404)
        if method == "GET" and record_id == "search" and collection in SEARCHABLE and not operation:
            return self.search(collection)
        if collection == "customers":
            result = await self.customer_route(record_id, operation, child_id, method, p)
            if result is not None:
                return result
        for handler in (self.payment_route, self.billing_route, self.commerce_route):
            result = await handler(collection, record_id, operation, method, p)
            if result is not None:
                return result
        if collection == "charges" and operation == "refunds" and method == "GET":
            self.get("charges", record_id)
            return self.listing(
                "refunds",
                rows=self.ctx.store.collection("refunds").where(charge=record_id),
                url=f"/v1/charges/{record_id}/refunds",
            )
        if collection == "files":
            if method == "POST" and not record_id:
                return await self.upload_file()
            if record_id and method == "GET" and operation == "contents":
                file = self.get("files", record_id)
                return Response(
                    self.ctx.store.blobs.get(file["_blob"]),
                    media_type=file["_mime"],
                    headers={"Content-Disposition": f'attachment; filename="{file["filename"]}"'},
                )
        if method == "GET" and collection in READABLE and not operation:
            if record_id:
                record = self.get(collection, record_id)
                self.check_client_secret(record)
                return record
            if collection == "subscriptions" and "status" not in p:
                rows = [r for r in self.ctx.store.collection(collection) if r["status"] != "canceled"]
                return self.listing(collection, rows=rows)
            if collection == "payment_methods" and not p.get("customer"):
                fail("Missing required param: customer.", "customer", code="parameter_missing")
            return self.listing(collection)
        fail("Unrecognized request URL.", status=404)

    async def upload_file(self) -> Record:
        assert self.request is not None
        content_type = self.request.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            fail("Invalid request: must be multipart/form-data.")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + content_type.encode()
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + await self.request.body()
        )
        purpose = None
        content = None
        filename, mime = "upload", "application/octet-stream"
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "purpose":
                purpose = part.get_payload(decode=True).decode()
            elif name == "file":
                content = part.get_payload(decode=True)
                filename = (
                    (part.get_filename() or "upload").replace('"', "").replace("\r", "").replace("\n", "")
                )
                mime = part.get_content_type()
        if purpose not in {
            "dispute_evidence",
            "identity_document",
            "business_logo",
            "customer_signature",
            "tax_document_user_upload",
        }:
            fail("Invalid file purpose.", "purpose")
        if content is None:
            fail("Missing required param: file.", "file", code="parameter_missing")
        file = self.new(
            "files",
            expires_at=None,
            filename=filename,
            links={"object": "list", "data": [], "has_more": False, "url": "/v1/file_links"},
            purpose=purpose,
            size=len(content),
            title=None,
            type=filename.rsplit(".", 1)[-1].lower(),
            url=None,
            _blob=self.ctx.store.blobs.put(content),
            _mime=mime,
        )
        file.pop("metadata", None)
        file.pop("livemode", None)
        file["url"] = self.url("files.stripe.com", f"/v1/files/{file['id']}/contents")
        await self.emit("file.created", file)
        return file

    def view(self, value: Any) -> Any:
        """Project breaking-version fields while retaining internal relationships for expansion."""
        if isinstance(value, list):
            return [self.view(v) for v in value]
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        result = {k: self.view(v) for k, v in value.items() if not k.startswith("_")}
        version = (
            self.request.headers.get("stripe-version", "2025-09-30.clover")
            if self.request
            else "2025-09-30.clover"
        )
        obj = value.get("object")
        if obj == "invoice":
            expansions = self.params.get("expand", [])
            legacy_expand = any("payment_intent" in path.split(".") for path in expansions)
            if version < "2025-03-31" or legacy_expand:
                result["payment_intent"] = value.get("_payment_intent")
                result["subscription"] = value.get("_subscription")
                if version < "2025-03-31":
                    for key in ("payments", "parent", "confirmation_secret"):
                        result.pop(key, None)
            elif not any("confirmation_secret" in path for path in expansions):
                result["confirmation_secret"] = None
        elif obj == "subscription" and version < "2025-03-31":
            items = value.get("items", {}).get("data", [])
            if items:
                result["current_period_start"] = min(i["current_period_start"] for i in items)
                result["current_period_end"] = max(i["current_period_end"] for i in items)
        elif obj == "payment_intent" and version < "2022-11-15":
            result["charges"] = clean(
                self.listing(
                    "charges",
                    params={},
                    rows=self.ctx.store.collection("charges").where(payment_intent=value["id"]),
                    url="/v1/charges?payment_intent=" + value["id"],
                )
            )
        elif obj == "event":
            deliveries = self.ctx.webhooks.deliveries.filter(
                lambda d: d.get("metadata", {}).get("event") == value["id"]
            )
            if deliveries:
                result["pending_webhooks"] = sum(d["status"] != "delivered" for d in deliveries)
        return result

    def expand(self, record: Record) -> Record:
        return super().expand(self.view(record))

    def _expand_at(self, node: Any, parts: list[str]) -> None:
        if not parts or node is None:
            return
        if isinstance(node, list):
            for item in node:
                self._expand_at(item, parts)
            return
        if not isinstance(node, dict):
            return
        from emulators.providers.stripe.common import PREFIX_COLLECTION

        key, *rest = parts
        value = node.get(key)
        if isinstance(value, str):
            collection = next(
                (c for prefix, c in PREFIX_COLLECTION.items() if value.startswith(prefix)), None
            )
            if collection:
                value = self.view(self.get(collection, value))
                node[key] = value
        self._expand_at(value, rest)

    def invoice_pdf(self, invoice_id: str) -> Response:
        invoice = self.get("invoices", invoice_id)
        if not invoice.get("_pdf_blob"):
            content = make_pdf(
                f"Invoice {invoice['number']}",
                [
                    self.ctx.world.org.legal_name,
                    invoice["customer_name"] or "",
                    *[
                        f"{line['description'] or 'Services'}: {line['amount'] / 100:.2f} {invoice['currency'].upper()}"
                        for line in invoice["lines"]["data"]
                    ],
                    f"Total: {invoice['total'] / 100:.2f} {invoice['currency'].upper()}",
                    f"Status: {invoice['status']}",
                ],
                author=self.ctx.world.org.name,
                created=datetime.fromtimestamp(invoice["created"], UTC),
            )
            invoice["_pdf_blob"] = self.ctx.store.blobs.put(content)
        return Response(self.ctx.store.blobs.get(invoice["_pdf_blob"]), media_type="application/pdf")


async def parameters(request: Request) -> Record:
    result = decode(request.query_params.multi_items())
    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            return result
        body = await request.body()
        if body:
            if "application/x-www-form-urlencoded" not in content_type:
                fail(
                    "Invalid request: check that your POST content type is application/x-www-form-urlencoded."
                )
            try:
                result.update(decode(body))
            except UnicodeDecodeError:
                fail("Invalid UTF-8 in request body.")
    return result


def checkout_page(session: Record) -> HTMLResponse:
    import html

    items = "".join(
        f"<li>{html.escape(i['description'])} × {i['quantity']}</li>" for i in session["_line_items"]
    )
    return HTMLResponse(
        f"<!doctype html><html><head><title>Stripe Checkout</title></head><body><h1>Halcyon Advisory Group</h1><ul>{items}</ul><p>Session: {html.escape(session['status'])}</p><p>Complete this test checkout using the complete_checkout emulator action.</p></body></html>"
    )
