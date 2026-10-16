"""Stripe test-mode API emulator, projected from the shared Halcyon world."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from emulators.core.auth import OAuthError, basic_credentials
from emulators.core.errors import EmulatorError
from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext
from emulators.providers.stripe.api import StripeAPI, checkout_page, parameters
from emulators.providers.stripe.middleware import StripeMiddleware
from emulators.providers.stripe.seed import seed
from emulators.providers.stripe.wire import PUBLIC_KEY, SECRET_KEY, VERSION, Record, fail, integer


class StripeProvider(Provider):
    name = "stripe"
    title = "Stripe"
    hosts = (
        "api.stripe.com",
        "files.stripe.com",
        "uploads.stripe.com",
        "checkout.stripe.com",
        "connect.stripe.com",
        "billing.stripe.com",
        "invoice.stripe.com",
        "pay.stripe.com",
    )
    default_port = 44110
    description = "Stripe test-mode payments, recurring billing, Checkout, and signed webhooks."
    credentials = {"secret_key": SECRET_KEY, "publishable_key": PUBLIC_KEY, "card": "4242424242424242"}

    def __init__(self) -> None:
        super().__init__()
        self.action("complete_3ds")(complete_3ds)
        self.action("complete_checkout")(complete_checkout)
        self.action("advance_time")(advance_time)

    def seed(self, ctx: ProviderContext) -> None:
        seed(StripeAPI(ctx, seed=True))

    def error_body(self, status: int, code: str, message: str) -> Record:
        error_type = (
            "api_error" if status >= 500 else "rate_limit_error" if status == 429 else "invalid_request_error"
        )
        return {
            "error": {
                "type": error_type,
                "message": message,
                **({"code": "resource_missing"} if status == 404 else {}),
            }
        }

    def fault_response(self, rule: FaultRule) -> tuple[int, dict[str, str], bytes]:
        status, headers, body = super().fault_response(rule)
        from emulators.core.ids import stable_hash

        headers.update(
            {"Request-Id": "req_" + stable_hash(rule.id, rule.hits, length=24), "Stripe-Version": VERSION}
        )
        return status, headers, body

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        app.add_middleware(StripeMiddleware, ctx=ctx)
        mutation_lock = asyncio.Lock()

        @app.api_route("/v1/{resource:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
        async def api(request: Request, resource: str) -> Any:
            params = await parameters(request)
            handler = StripeAPI(ctx, request, params)

            async def execute() -> Any:
                snapshot = ctx.store.snapshot() if request.method in {"POST", "DELETE"} else None
                try:
                    handler.expand({})
                    result = await handler.dispatch(resource, request.method)
                    return result if isinstance(result, Response) else JSONResponse(handler.expand(result))
                except EmulatorError as exc:
                    if snapshot is not None and exc.status != 402 and exc.status < 500:
                        ctx.store.restore(snapshot)
                    if isinstance(exc.body, dict) and exc.status != 401:
                        exc.body.get("error", {}).setdefault(
                            "request_log_url",
                            "https://dashboard.stripe.com/test/logs/" + request.state.stripe_request_id,
                        )
                    raise

            if request.method in {"POST", "DELETE"}:
                async with mutation_lock:
                    return await execute()
            return await execute()

        @app.get("/c/pay/{session_id}")
        async def hosted_checkout(session_id: str) -> Response:
            return checkout_page(StripeAPI(ctx).get("checkout/sessions", session_id))

        @app.get("/invoice/{invoice_id}/pdf")
        async def pdf(invoice_id: str) -> Response:
            return StripeAPI(ctx).invoice_pdf(invoice_id)

        @app.get("/i/{account}/test_{invoice_id}")
        async def hosted_invoice(account: str, invoice_id: str) -> Response:
            import html

            if account != ctx.world.org.stripe_account_id:
                fail("No such account.", status=404)
            invoice = StripeAPI(ctx).get("invoices", invoice_id)
            return HTMLResponse(
                f"<!doctype html><html><head><title>Invoice</title></head><body><h1>{html.escape(invoice['number'] or invoice_id)}</h1><p>{invoice['amount_due'] / 100:.2f} {html.escape(invoice['currency'].upper())}</p></body></html>"
            )

        @app.get("/p/session/{session_id}")
        async def portal(session_id: str) -> Response:
            StripeAPI(ctx).get("billing_portal/sessions", session_id)
            return HTMLResponse(
                "<!doctype html><html><head><title>Customer portal</title></head><body><h1>Halcyon billing portal</h1></body></html>"
            )

        @app.get("/oauth/authorize")
        async def authorize(request: Request) -> Response:
            from emulators.core.auth import redirect_with_params

            p = dict(request.query_params)
            if not p.get("client_id") or not p.get("redirect_uri"):
                fail("Missing client_id or redirect_uri.")
            ctx.tokens.authenticate_client(p["client_id"], None, require_secret=False)
            grant = ctx.tokens.issue_code(
                client_id=p["client_id"],
                user_id=ctx.world.org.stripe_account_id,
                scopes=[p.get("scope", "read_write")],
                redirect_uri=p["redirect_uri"],
            )
            return RedirectResponse(
                redirect_with_params(p["redirect_uri"], {"code": grant.token, "state": p.get("state")}),
                status_code=302,
            )

        @app.post("/oauth/token")
        async def oauth_token(request: Request) -> Response:
            p = await parameters(request)
            client_id, secret = basic_credentials(request) or (
                p.get("client_id"),
                p.get("client_secret"),
            )
            try:
                if client_id and client_id.startswith("sk_test_"):
                    secret, client_id = client_id, None
                code_grant = ctx.tokens.get(p.get("code"), "code")
                client_id = client_id or (code_grant.client_id if code_grant else "emulator-client")
                if p.get("grant_type") == "authorization_code":
                    grant = ctx.tokens.redeem_code(
                        p.get("code"),
                        client_id=client_id,
                        redirect_uri=p.get("redirect_uri"),
                        code_verifier=None,
                    )
                elif p.get("grant_type") == "refresh_token":
                    grant = ctx.tokens.resolve(p.get("refresh_token"), "refresh")
                    if not grant:
                        raise OAuthError("invalid_grant", "Invalid refresh token.")
                else:
                    raise OAuthError(
                        "unsupported_grant_type", "Only authorization_code and refresh_token are supported."
                    )
                if secret and not secret.startswith("sk_test_"):
                    raise OAuthError("invalid_client", "Invalid API key.", 401)
            except OAuthError as exc:
                return JSONResponse(
                    {"error": exc.error, "error_description": exc.description}, status_code=exc.status
                )
            access = ctx.tokens.issue_access(
                client_id=grant.client_id,
                user_id=None,
                app_only=True,
                scopes=[],
                prefix="sk_test_",
                ttl=timedelta(hours=1),
                extra={
                    "stripe_scope": grant.extra.get(
                        "stripe_scope", grant.scopes[0] if grant.scopes else "read_write"
                    )
                },
            )
            refresh = ctx.tokens.issue_refresh(
                client_id=grant.client_id,
                user_id=None,
                app_only=True,
                scopes=[],
                extra={
                    "stripe_scope": grant.extra.get(
                        "stripe_scope", grant.scopes[0] if grant.scopes else "read_write"
                    )
                },
            )
            return JSONResponse(
                {
                    "access_token": access.token,
                    "livemode": False,
                    "refresh_token": refresh.token,
                    "token_type": "bearer",
                    "stripe_publishable_key": PUBLIC_KEY,
                    "stripe_user_id": ctx.world.org.stripe_account_id,
                    "scope": grant.scopes[0] if grant.scopes else "read_write",
                }
            )

        @app.post("/oauth/deauthorize")
        async def deauthorize(request: Request) -> Record:
            p = await parameters(request)
            StripeAPI(ctx, request, p).authorize("account", "write")
            if p.get("stripe_user_id") != ctx.world.org.stripe_account_id:
                fail("No such account.", "stripe_user_id")
            return {"stripe_user_id": ctx.world.org.stripe_account_id}


async def complete_3ds(ctx: ProviderContext, payload: Record) -> Record:
    """Complete or fail a PaymentIntent's pending test card authentication."""
    api = StripeAPI(ctx)
    api.required(payload, "payment_intent")
    pi = api.get("payment_intents", payload["payment_intent"])
    if pi["status"] != "requires_action":
        fail("This PaymentIntent does not require authentication.")
    if payload.get("success", True):
        pi["_authenticated"] = True
        if pi["confirmation_method"] == "manual":
            pi.update(status="requires_confirmation", next_action=None)
        else:
            await api.confirm_intent(pi, {}, complete_3ds=True)
    else:
        pi.update(
            status="requires_payment_method",
            next_action=None,
            last_payment_error={
                "type": "card_error",
                "code": "payment_intent_authentication_failure",
                "message": "The provided PaymentMethod has failed authentication.",
            },
        )
        await api.emit("payment_intent.payment_failed", pi)
    if pi["status"] == "succeeded":
        for invoice in ctx.store.collection("invoices"):
            if invoice.get("_payment_intent") == pi["id"] and invoice["status"] == "open":
                await api.mark_invoice_paid(invoice)
    return api.expand(pi)


async def complete_checkout(ctx: ProviderContext, payload: Record) -> Record:
    """Complete a test hosted Checkout Session and deliver its resulting events."""
    api = StripeAPI(ctx)
    api.required(payload, "session")
    session = api.get("checkout/sessions", payload["session"])
    origin = session.get("_origin")
    if origin:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": origin["scheme"],
                "path": "/_emulator/actions/complete_checkout",
                "query_string": b"",
                "headers": [(b"host", origin["host"].encode())],
                "server": ("localhost", 44110),
                "state": {"external_base": origin["base"]},
            }
        )
        api = StripeAPI(ctx, request)
    return api.expand(
        await api.complete_checkout(payload["session"], payload.get("card", "4242424242424242"))
    )


async def advance_time(ctx: ProviderContext, payload: Record) -> Record:
    """Advance the hub clock and process renewals for subscriptions without a test clock."""
    seconds = integer(payload.get("seconds", 0), "seconds", minimum=0)
    ctx.clock.advance(timedelta(seconds=seconds))
    api = StripeAPI(ctx)
    renewed = await api.renew(ctx.clock.timestamp())
    return {"now": ctx.clock.timestamp(), "invoices_created": renewed}


PROVIDER = StripeProvider()
