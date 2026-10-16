"""Atlassian Cloud: Confluence v2/v1 and Jira Software v3."""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from emulators.core.provider import Provider, ProviderContext

from .auth import build_auth, mint_token
from .common import API_TOKEN, CLOUD_ID, error_body, family
from .confluence import build_downloads, build_v2
from .delivery import AtlassianWebhooks
from .hooks import configure_webhook
from .jira import build_jira
from .legacy import build_v1
from .seed import seed


def invalid_json_constant(value: str) -> NoReturn:
    raise ValueError(f"Invalid JSON constant: {value}")


class AtlassianProvider(Provider):
    name = "atlassian"
    title = "Atlassian Cloud"
    hosts = ("auth.atlassian.com", "api.atlassian.com", "halcyonadvisory.atlassian.net", "*.atlassian.net")
    default_port = 44116
    description = "Confluence page trees, permissions and CQL; Jira issues and JQL; Atlassian OAuth 3LO."
    credentials = {
        "cloud_id": CLOUD_ID,
        "site": "https://halcyonadvisory.atlassian.net",
        "email": "eleanor.whitfield@halcyonadvisory.example",
        "api_token": API_TOKEN,
        "client_id": "emulator-client",
        "client_secret": "emulator-secret",
    }

    def __init__(self) -> None:
        super().__init__()
        self.action("mint_token")(mint_token)
        self.action("configure_webhook")(configure_webhook)

    def seed(self, ctx: ProviderContext) -> None:
        seed(ctx)

    def error_body(self, status: int, code: str, message: str) -> Any:
        return error_body(status, message)

    def error_body_for(self, scope: dict[str, Any] | None, status: int, code: str, message: str) -> Any:
        return error_body(status, message, family(Request(scope)) if scope else "v2")

    def error_headers_for(self, scope: dict[str, Any] | None, status: int) -> dict[str, str]:
        headers = {"X-AREQUESTID": secrets.token_hex(12), "Atl-Traceid": secrets.token_hex(16)}
        if status == 429:
            headers.update({"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "0"})
        return headers

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        dispatcher = ctx.webhooks
        ctx.webhooks = AtlassianWebhooks(
            ctx.name,
            ctx.store,
            ctx.clock,
            ctx.ids.fork("webhooks"),
            mode=dispatcher.mode,
            transport_factory=dispatcher.transport_factory,
            max_attempts=dispatcher.max_attempts,
            timeout_s=dispatcher.timeout_s,
        )

        @app.middleware("http")
        async def response_headers(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
            if request.method in ("POST", "PUT", "PATCH") and "json" in request.headers.get(
                "content-type", ""
            ):
                try:
                    # The decoder can exhaust its stack before FastAPI validates a body.
                    raw = await request.body()
                    if raw:
                        json.loads(raw, parse_constant=invalid_json_constant)
                except (ValueError, UnicodeError, RecursionError):
                    return JSONResponse(
                        error_body(400, "Invalid JSON request body.", family(request)),
                        status_code=400,
                        headers=self.error_headers_for(request.scope, 400),
                    )
            response = await call_next(request)
            for key, value in self.error_headers_for(request.scope, response.status_code).items():
                response.headers.setdefault(key, value)
            return response

        @app.exception_handler(StarletteHTTPException)
        async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
            return JSONResponse(
                error_body(exc.status_code, str(exc.detail), family(request)), status_code=exc.status_code
            )

        @app.exception_handler(RequestValidationError)
        async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
            return JSONResponse(
                error_body(400, "Invalid request body or parameters.", family(request)), status_code=400
            )

        build_auth(app, ctx)
        v2, v1, downloads, jira = build_v2(ctx), build_v1(ctx), build_downloads(ctx), build_jira(ctx)
        app.include_router(v2, prefix="/wiki/api/v2")
        app.include_router(v1, prefix="/wiki/rest/api")
        app.include_router(downloads)
        app.include_router(jira, prefix="/rest/api/3")
        app.include_router(v2, prefix="/ex/confluence/{cloud_id}/wiki/api/v2")
        app.include_router(v1, prefix="/ex/confluence/{cloud_id}/wiki/rest/api")
        app.include_router(downloads, prefix="/ex/confluence/{cloud_id}")
        app.include_router(jira, prefix="/ex/jira/{cloud_id}/rest/api/3")


PROVIDER = AtlassianProvider()
