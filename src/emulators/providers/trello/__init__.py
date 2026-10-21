"""Trello REST: Halcyon workspaces, boards, cards, collaboration and webhooks."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse
from starlette.exceptions import HTTPException

from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext

from .actions import move_card, post_comment
from .auth import mint_token
from .common import KEY, SECRET, TOKEN
from .hooks import TrelloWebhooks
from .routes import build
from .seed import seed


class TrelloProvider(Provider):
    name = "trello"
    title = "Trello"
    hosts = ("api.trello.com", "trello.com", "www.trello.com")
    default_port = 44118
    description = (
        "Trello workspaces, private boards, cards, checklists, attachments and signed action webhooks."
    )
    credentials = {
        "key": KEY,
        "token": TOKEN,
        "application_secret": SECRET,
        "user": "eleanor.whitfield@halcyonadvisory.example",
    }

    def __init__(self) -> None:
        super().__init__()
        self.action("mint_token")(mint_token)
        self.action("post_comment")(post_comment)
        self.action("move_card")(move_card)

    def seed(self, ctx: ProviderContext) -> None:
        seed(ctx)

    def error_body(self, status: int, code: str, message: str) -> Any:
        if status == 429:
            return {"error": "API_TOKEN_LIMIT_EXCEEDED", "message": "Rate limit exceeded"}
        return "not found" if status == 404 else "invalid token" if status == 401 else message

    def fault_response(
        self, rule: FaultRule, scope: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        status = rule.status or 500
        headers = {"content-type": "text/plain; charset=utf-8", **rule.headers}
        if status == 429:
            headers.setdefault("Retry-After", "2")
            headers["content-type"] = "application/json"
        value = (
            rule.body
            if rule.body is not None
            else self.error_body(
                status, "", "API rate limit exceeded" if status == 429 else "internal server error"
            )
        )
        if isinstance(value, bytes):
            content = value
        elif isinstance(value, str):
            content = value.encode()
        else:
            content = json.dumps(value).encode()
        return status, headers, content

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        dispatcher = ctx.webhooks
        ctx.webhooks = TrelloWebhooks(
            ctx.name,
            ctx.store,
            ctx.clock,
            ctx.ids.fork("webhooks"),
            mode=dispatcher.mode,
            transport_factory=dispatcher.transport_factory,
            max_attempts=dispatcher.max_attempts,
            timeout_s=dispatcher.timeout_s,
        )

        @app.exception_handler(HTTPException)
        async def http_error(_: Request, exc: HTTPException) -> PlainTextResponse:
            return PlainTextResponse(
                "not found" if exc.status_code == 404 else str(exc.detail),
                status_code=exc.status_code,
                headers=exc.headers,
            )

        @app.exception_handler(RequestValidationError)
        async def validation_error(_: Request, exc: RequestValidationError) -> PlainTextResponse:
            return PlainTextResponse("invalid request", status_code=400)

        build(app, ctx)


PROVIDER = TrelloProvider()
