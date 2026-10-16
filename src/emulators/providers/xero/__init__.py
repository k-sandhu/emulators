"""Xero Accounting API 2.0 and OAuth identity emulator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from emulators.core.auth import bearer_token
from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext

from . import hooks, identity, routes
from .common import BASE, ID_FIELDS, NOT_FOUND, problem, tenant_id
from .journal import XeroJournal
from .reports import NAMES
from .seed import seed


class XeroProvider(Provider):
    name = "xero"
    title = "Xero"
    hosts = ("api.xero.com", "identity.xero.com", "login.xero.com", "go.xero.com")
    default_port = 44112
    description = "Tenant-aware Xero accounting, OAuth, reports, and signed webhooks."
    credentials = {
        "client_id": "xero-test-client",
        "client_secret": "xero-test-secret",
        "login_hint": "eleanor.whitfield@halcyonadvisory.example",
    }

    def __init__(self) -> None:
        super().__init__()
        self.actions["mint_token"] = identity.mint_token
        self.actions["configure_webhook"] = hooks.configure

    def seed(self, ctx: ProviderContext) -> None:
        seed(ctx)

    def error_body(self, status: int, code: str, message: str) -> Any:
        if status == 404:
            return NOT_FOUND
        if status in {401, 403, 429}:
            return problem(status)
        return {
            "ErrorNumber": 10,
            "Type": "ValidationException",
            "Message": "A validation exception occurred",
            "Elements": [{"ValidationErrors": [{"Message": message}]}],
        }

    def error_headers_for(self, scope: dict[str, Any] | None, status: int) -> dict[str, str]:
        return {"xero-correlation-id": str(uuid4())}

    def fault_response(
        self, rule: FaultRule, scope: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        if rule.status == 404 and rule.body is None:
            return (
                404,
                {"content-type": "text/plain", **self.error_headers_for(scope, 404), **rule.headers},
                NOT_FOUND.encode(),
            )
        status, headers, body = super().fault_response(rule, scope)
        if status == 429:
            headers = {
                "X-MinLimit-Remaining": "0",
                "X-DayLimit-Remaining": "4999",
                "X-AppMinLimit-Remaining": "9999",
                **headers,
                **rule.headers,
            }
            if not any(k.lower() == "retry-after" for k in rule.headers):
                headers["Retry-After"] = "60"
        return status, headers, body

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        ctx.journal = XeroJournal()

        @app.exception_handler(HTTPException)
        async def http_error(request: Request, exc: HTTPException) -> Response:
            if exc.status_code == 404:
                return Response(NOT_FOUND, status_code=404, media_type="text/plain")
            return JSONResponse(
                self.error_body(exc.status_code, "Error", str(exc.detail)), status_code=exc.status_code
            )

        @app.middleware("http")
        async def limits(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            path = request.url.path.removeprefix(request.scope.get("root_path", ""))
            if path.lower().startswith(BASE.lower() + "/"):
                parts = path[len(BASE) + 1 :].split("/")
                canonical = {v.lower(): v for v in [*ID_FIELDS, "Reports"]}
                parts[0] = canonical.get(parts[0].lower(), parts[0])
                if parts[0] == "Reports" and len(parts) > 1:
                    parts[1] = {n.lower(): n for n in NAMES}.get(parts[1].lower(), parts[1])
                if len(parts) > 2:
                    suffixes = {
                        n.lower(): n
                        for n in [
                            "OnlineInvoice",
                            "Email",
                            "Attachments",
                            "History",
                            "Contacts",
                            "Options",
                            "Allocations",
                        ]
                    }
                    parts[2] = suffixes.get(parts[2].lower(), parts[2])
                path = BASE + "/" + "/".join(parts)
                request.scope["path"] = request.scope.get("root_path", "") + path
            headers: dict[str, str] = {}
            grant = ctx.tokens.resolve(bearer_token(request))
            tenant = tenant_id(ctx, request, grant) if grant else ""
            correlation = {"xero-correlation-id": str(uuid4())}
            if path.startswith(BASE + "/") and grant and ctx.store.collection("_tenants").get(tenant):
                now = ctx.clock.timestamp()
                counters = ctx.store.collection("_limits")
                values = []
                for identifier, window, ceiling in [
                    (tenant + "/minute", 60, 60),
                    (tenant + "/day", 86400, 5000),
                    ("app/" + grant.client_id, 60, 10000),
                ]:
                    counter = counters.get(identifier)
                    if not counter or now >= counter["reset"]:
                        counter = counters.put({"id": identifier, "used": 0, "reset": now + window})
                    values.append((counter, ceiling))
                names = ["X-MinLimit-Remaining", "X-DayLimit-Remaining", "X-AppMinLimit-Remaining"]
                exhausted = [c for c, maximum in values if c["used"] >= maximum]
                if exhausted:
                    headers = {
                        name: str(max(0, maximum - c["used"]))
                        for name, (c, maximum) in zip(names, values, strict=True)
                    }
                    headers["Retry-After"] = str(max(1, max(c["reset"] - now for c in exhausted)))
                    headers["X-Rate-Limit-Problem"] = "minute" if values[0][0] in exhausted else "day"
                    return JSONResponse(problem(429), status_code=429, headers={**headers, **correlation})
                for counter, _ in values:
                    counter["used"] += 1
                headers = {
                    name: str(maximum - c["used"]) for name, (c, maximum) in zip(names, values, strict=True)
                }
            response = await call_next(request)
            response.headers.update({**headers, **correlation})
            return response

        identity.build(app, ctx)
        routes.build(app, ctx)


PROVIDER = XeroProvider()
