"""Atlassian Cloud: Confluence v2/v1 and Jira Software v3."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from emulators.core.middleware import FaultRule
from emulators.core.provider import FAULT_DEFAULTS, Provider, ProviderContext

from .auth import build_auth, mint_token
from .common import API_TOKEN, CLOUD_ID, error_body, family
from .confluence import build_downloads, build_v2
from .hooks import configure_webhook
from .jira import build_jira
from .legacy import build_v1
from .seed import seed


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

    def fault_response(self, rule: FaultRule) -> tuple[int, dict[str, str], bytes]:
        status, headers, body = super().fault_response(rule)
        path = rule.path.replace("\\", "")
        kind = (
            "jira"
            if "/rest/api/3" in path or "/ex/jira/" in path
            else "v1"
            if "/wiki/rest/" in path
            else "v2"
        )
        if rule.body is None and kind != "v2":
            message = FAULT_DEFAULTS.get(status, ("Error", "Injected fault."))[1]
            body = json.dumps(error_body(status, message, kind)).encode()
        if status == 429:
            headers = {
                "X-RateLimit-Limit": "1000",
                "X-RateLimit-Remaining": "0",
                **headers,
            }
        return status, headers, body

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
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
