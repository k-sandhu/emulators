"""QuickBooks Online Accounting API emulator, projected from the Halcyon world."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from fastapi import FastAPI

from emulators.core.middleware import FaultRule
from emulators.core.provider import Provider, ProviderContext
from emulators.providers.quickbooks.api import emit_webhooks, register_api
from emulators.providers.quickbooks.common import fault_body, realm
from emulators.providers.quickbooks.identity import (
    CLIENT_ID,
    CLIENT_SECRET,
    SCOPES,
    default_user,
    find_user,
    issue_tokens,
    register_identity,
)
from emulators.providers.quickbooks.seed import seed as seed_world

HOSTS = (
    "quickbooks.api.intuit.com",
    "sandbox-quickbooks.api.intuit.com",
    "appcenter.intuit.com",
    "oauth.platform.intuit.com",
    "developer.api.intuit.com",
    "developer.intuit.com",
    "accounts.platform.intuit.com",
    "sandbox-accounts.platform.intuit.com",
    "accounts.intuit.com",
)


class QuickBooksProvider(Provider):
    name = "quickbooks"
    title = "QuickBooks Online"
    hosts = HOSTS
    default_port = 44111
    description = (
        "QuickBooks Online Accounting API: OAuth/OpenID, full entity CRUD, QBO query language, "
        "batch, CDC, reports computed from a double-entry ledger, signed webhooks and PDFs."
    )
    credentials = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "realm_id": "9341569385067351",
        "scopes": SCOPES,
        "redirect_uri_example": "https://localhost/callback",
        "admin_user_hint": "any Finance/partner login_hint, e.g. eleanor.whitfield@halcyonadvisory.example",
    }

    def __init__(self) -> None:
        super().__init__()
        self.action("mint_token")(mint_token)
        self.action("configure_webhook")(configure_webhook)
        self.action("simulate_merge")(simulate_merge)

    def seed(self, ctx: ProviderContext) -> None:
        ctx.tokens.register_client(
            CLIENT_ID,
            CLIENT_SECRET,
            name="Emulator test app",
            redirect_uris=["https://localhost/callback", "http://localhost/callback"],
        )
        seed_world(ctx)

    def error_body(self, status: int, code: str, message: str) -> Any:
        if status == 401:
            return {
                "fault": {
                    "error": [
                        {
                            "message": "message=AuthenticationFailed; errorCode=003200; statusCode=401",
                            "detail": message,
                            "code": "3200",
                        }
                    ],
                    "type": "AUTHENTICATION",
                }
            }
        fault_code = {"NotFound": "610", "BadRequest": "6000"}.get(code, "6000")
        fault_msg = "Object Not Found" if code == "NotFound" else message
        return {
            "Fault": {
                "Error": [{"Message": fault_msg, "Detail": message, "code": fault_code, "element": ""}],
                "type": "ValidationFault",
            }
        }

    def fault_response(self, rule: FaultRule) -> tuple[int, dict[str, str], bytes]:
        status = rule.status or 500
        headers = {"content-type": "application/json", **rule.headers}
        if rule.body is not None:
            body = rule.body if isinstance(rule.body, (bytes, str)) else json.dumps(rule.body)
            return status, headers, body.encode() if isinstance(body, str) else body
        if status == 429:
            body = {
                "Fault": {
                    "Error": [
                        {
                            "Message": "ThrottleExceeded",
                            "Detail": "The API is receiving too many requests; retry later.",
                            "code": "3001",
                            "element": "",
                        }
                    ],
                    "type": "ThrottledFault",
                }
            }
            headers.setdefault("Retry-After", "2")
            return status, headers, json.dumps(body).encode()
        return super().fault_response(rule)

    def info(self, ctx: ProviderContext) -> dict[str, Any]:
        info = super().info(ctx)
        info["credentials"] = {**self.credentials, "realm_id": realm(ctx)}
        return info

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        register_identity(app, ctx, self)
        register_api(app, ctx)


def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Mint QBO-shaped access+refresh tokens via ``POST /_emulator/tokens``.

    ``{"user": "<pNNN|email>", "scopes": [...], "expires_in": 3600, "app_only": false}``.
    App-only tokens act for the company (full access); delegated tokens inherit
    the user's role (Finance/partners/admins write, others read-only).
    """
    scopes = list(payload.get("scopes") or SCOPES)
    expires_in = int(payload.get("expires_in", 3600))
    if payload.get("app_only") or not payload.get("user"):
        grant = ctx.tokens.issue_access(
            client_id=str(payload.get("client_id") or CLIENT_ID),
            user_id=None,
            scopes=scopes,
            ttl=timedelta(seconds=expires_in),
            app_only=True,
            extra={"realm_id": realm(ctx)},
        )
        return {
            "access_token": grant.token,
            "token_type": "bearer",
            "expires_in": expires_in,
            "realm_id": realm(ctx),
        }
    user = find_user(ctx, str(payload.get("user"))) or default_user(ctx)
    tokens = issue_tokens(
        ctx,
        client_id=str(payload.get("client_id") or CLIENT_ID),
        user=user,
        scopes=scopes,
    )
    tokens["realm_id"] = realm(ctx)
    tokens["user"] = {"id": user["id"], "email": user["email"], "role": user["role"]}
    return tokens


def configure_webhook(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Register a QBO webhook endpoint: ``{url, verifier_token, entities?}``."""
    url = payload.get("url")
    if not url:
        return {"error": "url is required"}
    record = {
        "id": str(url),
        "url": str(url),
        "verifier_token": str(payload.get("verifier_token") or "emulator-verifier-token"),
        "entities": payload.get("entities"),
    }
    ctx.store.collection("_webhook_config").put_as(str(url), record)
    return {"status": "configured", "url": str(url), "entities": payload.get("entities") or "*"}


async def simulate_merge(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Emit a Merge webhook for ``{entity, id}`` without changing data."""
    entity = str(payload.get("entity") or "Customer")
    record_id = str(payload.get("id") or "1")
    await emit_webhooks(ctx, [(entity, record_id, "Merge")])
    return {"status": "merge event queued", "entity": entity, "id": record_id}


PROVIDER = QuickBooksProvider()

__all__ = ["PROVIDER", "QuickBooksProvider"]

_ = fault_body
