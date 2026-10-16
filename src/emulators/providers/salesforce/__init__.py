"""Salesforce: OAuth 2.0 / OIDC, SOAP partner login, REST API (sObjects, SOQL, SOSL, composite),
Bulk API 2.0 and the sharing model, over the shared Halcyon Advisory world."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from emulators.core.provider import Provider, ProviderContext

_ERRORS: dict[int, tuple[str, str]] = {
    400: ("MALFORMED_QUERY", "Bad request."),
    401: ("INVALID_SESSION_ID", "Session expired or invalid"),
    403: ("REQUEST_LIMIT_EXCEEDED", "TotalRequests Limit exceeded."),
    404: ("NOT_FOUND", "The requested resource does not exist"),
    405: ("METHOD_NOT_ALLOWED", "HTTP Method not allowed for this resource"),
    409: ("UNABLE_TO_LOCK_ROW", "unable to obtain exclusive access to this record"),
    429: ("REQUEST_LIMIT_EXCEEDED", "Too many requests. Retry after the delay in the Retry-After header."),
    500: ("UNKNOWN_EXCEPTION", "An unexpected error has occurred. Your solution provider has been notified."),
    502: ("SERVER_UNAVAILABLE", "Server Unavailable"),
    503: ("SERVER_UNAVAILABLE", "Server Unavailable"),
    504: ("SERVER_UNAVAILABLE", "Request timed out"),
}


class SalesforceProvider(Provider):
    name = "salesforce"
    title = "Salesforce"
    hosts = (
        "login.salesforce.com",
        "test.salesforce.com",
        "halcyonadvisory.my.salesforce.com",
        "*.my.salesforce.com",
        "*.lightning.force.com",
        "*.my.salesforce-setup.com",
    )
    default_port = 44105
    description = (
        "OAuth 2.0, SOAP login, REST API v31-v66 (sObjects, SOQL, SOSL, composite) and Bulk API 2.0."
    )
    credentials = {
        "client_id": "any (lenient connected app consumer key)",
        "client_secret": "any",
        "username": "<first.last>@halcyonadvisory.example.crm (every employee) or integration@halcyonadvisory.example.crm",
        "password": "Halcyon2026!",
        "security_token": "EMULATORTOKEN",
        "admins": "arjun.mehta@halcyonadvisory.example.crm, sarah.johansson@halcyonadvisory.example.crm",
    }

    def __init__(self) -> None:
        super().__init__()
        from emulators.providers.salesforce import actions

        actions.install(self)

    def seed(self, ctx: ProviderContext) -> None:
        from emulators.providers.salesforce import seed

        seed.seed(ctx)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        from emulators.providers.salesforce import routes

        routes.register(app, ctx)

    def error_body(self, status: int, code: str, message: str) -> Any:
        """Salesforce error arrays; injected faults and unknown routes map to the codes Salesforce uses."""
        known = _ERRORS.get(status)
        if known is not None:
            return [{"message": known[1], "errorCode": known[0]}]
        return [{"message": message, "errorCode": code}]


PROVIDER = SalesforceProvider()
