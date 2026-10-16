"""Meta emulator: WhatsApp Business Cloud API + Messenger Platform (Graph API)."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext
from emulators.providers.meta.actions import (
    approve_template,
    configure_webhook,
    deliver_statuses,
    inbound_messenger,
    inbound_whatsapp,
    mint_token,
    reject_template,
)
from emulators.providers.meta.api import MetaAPI, read_body
from emulators.providers.meta.common import (
    APP_ID,
    APP_SECRET,
    DEFAULT_VERSION,
    OLDEST_VERSION,
    PAGE_ACCESS_TOKEN,
    SYSTEM_USER_TOKEN,
    USER_ACCESS_TOKEN,
    VERIFY_TOKEN,
)
from emulators.providers.meta.seed import seed as seed_meta

_VERSION_RE = re.compile(r"^v\d+\.\d+$")


class MetaHeadersMiddleware:
    """Adds Graph's usage/trace response headers (pure ASGI)."""

    def __init__(self, app: Any, ctx: ProviderContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")

        async def send_with_headers(message: Any) -> None:
            if message.get("type") == "http.response.start" and not path.startswith("/_emulator"):
                headers = list(message.get("headers") or [])
                existing = {k.lower() for k, _ in headers}
                meta = self.ctx.store.meta
                business_id = meta.get("business_id", "")
                additions = {
                    b"x-fb-trace-id": (self.ctx.ids.base62(1) + self.ctx.ids.urlsafe(12)).encode(),
                    b"x-app-usage": b'{"call_count":1,"total_cputime":1,"total_time":1}',
                    b"x-fb-request-id": self.ctx.ids.urlsafe(16).encode(),
                }
                if business_id:
                    usage = {
                        str(business_id): [
                            {
                                "type": "pages",
                                "call_count": 1,
                                "total_cputime": 1,
                                "total_time": 1,
                                "estimated_time_to_regain_access": 0,
                            }
                        ]
                    }
                    additions[b"x-business-use-case-usage"] = json.dumps(usage).encode()
                for key, value in additions.items():
                    if key not in existing:
                        headers.append((key, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class MetaProvider(Provider):
    name = "meta"
    title = "Meta (WhatsApp + Messenger)"
    hosts = (
        "graph.facebook.com",
        "www.facebook.com",
        "facebook.com",
        "lookaside.fbsbx.com",
        "scontent.whatsapp.net",
        "*.fbcdn.net",
        "graph.instagram.com",
    )
    default_port = 44109
    description = (
        "Meta Graph API: WhatsApp Business Cloud API (messages, media, templates, webhooks), "
        "Messenger Platform (Send API, conversations, profiles) and Facebook Login OAuth."
    )
    credentials = {
        "app_id": APP_ID,
        "app_secret": APP_SECRET,
        "webhook_verify_token": VERIFY_TOKEN,
        "system_user_token": SYSTEM_USER_TOKEN,
        "page_access_token": PAGE_ACCESS_TOKEN,
        "user_access_token": USER_ACCESS_TOKEN,
        "display_phone_number": "+14165550177",
    }

    def __init__(self) -> None:
        super().__init__()
        self.action("mint_token")(mint_token)
        self.action("configure_webhook")(configure_webhook)
        self.action("inbound_whatsapp")(inbound_whatsapp)
        self.action("deliver_statuses")(deliver_statuses)
        self.action("approve_template")(approve_template)
        self.action("reject_template")(reject_template)
        self.action("inbound_messenger")(inbound_messenger)

    def seed(self, ctx: ProviderContext) -> None:
        seed_meta(ctx)

    def info(self, ctx: ProviderContext) -> dict[str, Any]:
        info = super().info(ctx)
        meta = ctx.store.meta
        info["credentials"] = {
            **self.credentials,
            "business_id": meta.get("business_id"),
            "whatsapp_business_account_id": meta.get("waba_id"),
            "waba_id": meta.get("waba_id"),
            "phone_number_id": meta.get("phone_number_id"),
            "page_id": meta.get("page_id"),
            "system_user_id": meta.get("system_user_id"),
        }
        return info

    def error_body(self, status: int, code: str, message: str) -> dict[str, Any]:
        numeric = {400: 100, 401: 190, 403: 200, 404: 803, 405: 100, 429: 17, 500: 1, 502: 2, 503: 2, 504: 2}
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = numeric.get(status, 100)
        err_type = (
            "OAuthException"
            if code_int in (190, 200, 10, 803)
            else "APIUnknownError"
            if code_int == 1
            else "APIServiceError"
            if code_int == 2
            else "GraphMethodException"
            if code_int == 100
            else "OAuthException"
        )
        if status == 404 and code_int == 803:
            message = "(#803) Some of the aliases you requested do not exist"
        return {"error": {"message": message, "type": err_type, "code": code_int, "fbtrace_id": "Aemulator"}}

    def fault_response(self, rule: FaultRule) -> tuple[int, dict[str, str], bytes]:
        status, headers, body = super().fault_response(rule)
        from emulators.core.ids import stable_hash

        trace = stable_hash("meta-fault", rule.id, rule.hits, length=16)
        headers.update(
            {
                "x-fb-trace-id": trace,
                "x-app-usage": '{"call_count":1,"total_cputime":1,"total_time":1}',
            }
        )
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and "error" in parsed:
                parsed["error"]["fbtrace_id"] = trace
                body = json.dumps(parsed).encode()
        except ValueError:
            pass
        return status, headers, body

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        app.add_middleware(MetaHeadersMiddleware, ctx=ctx)

        async def handle(request: Request, version: str | None, rest: str) -> Any:
            body = await read_body(ctx, request)
            api = MetaAPI(ctx, request, version or OLDEST_VERSION, body)
            result = await api.dispatch(request.method, rest)
            if isinstance(result, Response):
                return result
            return JSONResponse(result)

        @app.api_route("/v{version}/{rest:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
        async def versioned(request: Request, version: str, rest: str) -> Any:
            if not _VERSION_RE.match(f"v{version}"):
                return JSONResponse(
                    self.error_body(400, "100", f"Unsupported API version v{version}"), status_code=400
                )
            return await handle(request, f"v{version}", rest)

        @app.api_route("/v{version}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
        async def versioned_root(request: Request, version: str) -> Any:
            return await handle(
                request, f"v{version}" if _VERSION_RE.match(f"v{version}") else DEFAULT_VERSION, ""
            )

        @app.api_route("/{rest:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
        async def unversioned(request: Request, rest: str) -> Any:
            if rest.startswith("_emulator"):
                return JSONResponse(self.error_body(404, "803", "Not found"), status_code=404)
            host = (request.headers.get("host") or "").split(":")[0].lower()
            if not rest and request.method == "GET" and host in ("www.facebook.com", "facebook.com"):
                return HTMLResponse(
                    "<!doctype html><html><head><title>Facebook</title></head>"
                    "<body><h1>Facebook (emulator)</h1>"
                    "<p>Use the Graph API at graph.facebook.com.</p></body></html>"
                )
            return await handle(request, None, rest)


PROVIDER = MetaProvider()
