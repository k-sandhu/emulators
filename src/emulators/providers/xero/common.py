"""Wire shapes and tenant boundaries shared by Xero resource handlers."""

from __future__ import annotations

import copy
import re
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, NoReturn

from fastapi import Request

from emulators.core.auth import Grant, bearer_token
from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_guid
from emulators.core.provider import ProviderContext

BASE = "/api.xro/2.0"
NOT_FOUND = "The resource you're looking for cannot be found"
FULL_SCOPES = [
    "openid",
    "profile",
    "email",
    "offline_access",
    "accounting.transactions",
    "accounting.contacts",
    "accounting.settings",
    "accounting.reports.read",
    "accounting.attachments",
    "files",
]
ID_FIELDS = {
    "Organisation": "OrganisationID",
    "Accounts": "AccountID",
    "Contacts": "ContactID",
    "ContactGroups": "ContactGroupID",
    "Items": "ItemID",
    "Invoices": "InvoiceID",
    "Payments": "PaymentID",
    "BankTransactions": "BankTransactionID",
    "BankTransfers": "BankTransferID",
    "CreditNotes": "CreditNoteID",
    "ManualJournals": "ManualJournalID",
    "Quotes": "QuoteID",
    "TrackingCategories": "TrackingCategoryID",
    "TaxRates": "TaxType",
    "Currencies": "Code",
    "BrandingThemes": "BrandingThemeID",
    "Users": "UserID",
}
READ_ONLY = {"Organisation", "Currencies", "BrandingThemes", "Users", "TaxRates"}


def guid(kind: str, world_id: str) -> str:
    return stable_guid("xero", kind, world_id)


def main_tenant(ctx: ProviderContext) -> str:
    return guid("tenant", ctx.world.org.domain)


def msdate(value: date | datetime, *, offset: bool = True) -> str:
    instant = value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time(), UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return f"/Date({int(instant.timestamp() * 1000)}{'+0000' if offset else ''})/"


def instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), UTC)
    match = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", str(value))
    if match:
        return datetime.fromtimestamp(int(match[1]) / 1000, UTC)
    return parse_instant(str(value))


def money(value: Any) -> float:
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError
        return float(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (ValueError, TypeError, InvalidOperation):
        validation("A valid monetary amount is required.")


def public(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


def problem(status: int, detail: str = "AuthenticationUnsuccessful") -> dict[str, Any]:
    return {
        "Type": "https://httpstatuses.com/" + str(status),
        "Title": {401: "Unauthorized", 403: "Forbidden", 429: "Too Many Requests"}.get(status, "Error"),
        "Status": status,
        "Detail": detail,
    }


def validation(message: str, record: dict[str, Any] | None = None) -> NoReturn:
    element = {**public(copy.deepcopy(record or {})), "ValidationErrors": [{"Message": message}]}
    raise EmulatorError(
        400,
        {
            "ErrorNumber": 10,
            "Type": "ValidationException",
            "Message": "A validation exception occurred",
            "Elements": [element],
        },
    )


def missing() -> NoReturn:
    raise EmulatorError(404, NOT_FOUND, media_type="text/plain")


def rows(ctx: ProviderContext, resource: str, tenant: str) -> list[dict[str, Any]]:
    return ctx.store.collection(resource).where(_tenant=tenant)


def find(ctx: ProviderContext, resource: str, tenant: str, identifier: str) -> dict[str, Any]:
    field = ID_FIELDS[resource]
    for record in rows(ctx, resource, tenant):
        if str(record.get(field, "")).lower() == identifier.lower():
            return record
        if resource in {"Invoices", "Quotes", "Items", "Accounts"}:
            alternate = {
                "Invoices": "InvoiceNumber",
                "Quotes": "QuoteNumber",
                "Items": "Code",
                "Accounts": "Code",
            }[resource]
            if record.get(alternate) == identifier:
                return record
    missing()


def put(ctx: ProviderContext, resource: str, tenant: str, record: dict[str, Any]) -> dict[str, Any]:
    record["_tenant"] = tenant
    ctx.store.collection(resource).put_as(tenant + "/" + str(record[ID_FIELDS[resource]]), record)
    return record


def envelope(
    ctx: ProviderContext, resource: str, records: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    return {
        "Id": ctx.ids.guid(),
        "Status": "OK",
        "ProviderName": "Emulators",
        "DateTimeUTC": msdate(ctx.clock.now(), offset=False),
        resource: public(records),
        **extra,
    }


def actor(ctx: ProviderContext, request: Request) -> Grant:
    grant = ctx.tokens.resolve(bearer_token(request))
    if grant is None:
        raise EmulatorError(401, problem(401), headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
    if not grant.app_only:
        user = ctx.store.collection("_users").get(grant.user_id)
        if user is None or not user["active"]:
            raise EmulatorError(403, problem(403))
    return grant


def access(
    ctx: ProviderContext, request: Request, resource: str, *, write: bool = False
) -> tuple[Grant, str]:
    grant = actor(ctx, request)
    tenant = request.headers.get("xero-tenant-id", "").lower()
    connections = ctx.store.collection("_connections").filter(
        lambda c: c["_client"] == grant.client_id and c["_user"] == grant.user_id and c["tenantId"] == tenant
    )
    if not connections:
        raise EmulatorError(403, problem(403))
    if write and not grant.app_only:
        user = ctx.store.collection("_users").get(grant.user_id)
        if user is None or user["role"] == "READONLY":
            raise EmulatorError(403, problem(403, "You do not have permission to access this resource"))
    if resource.startswith("Reports"):
        scopes = {
            "accounting.reports.read",
            "accounting.reports." + resource.split("/")[-1].lower() + ".read",
        }
    elif resource in {"Contacts", "ContactGroups"}:
        scopes = {"accounting.contacts"}
    elif resource in {"Attachments"}:
        scopes = {"accounting.attachments", "files"}
    elif resource in {
        "Organisation",
        "Accounts",
        "TrackingCategories",
        "TaxRates",
        "Currencies",
        "Users",
        "BrandingThemes",
        "Items",
    }:
        scopes = {"accounting.settings"}
    else:
        scopes = {"accounting.transactions", "accounting." + resource.lower()}
    if not write:
        scopes |= {s + ".read" for s in list(scopes) if not s.endswith(".read")}
    if not scopes.intersection(grant.scopes):
        raise EmulatorError(401, problem(401, "InsufficientScope"))
    return grant, tenant


def stamp(ctx: ProviderContext, record: dict[str, Any]) -> None:
    record["UpdatedDateUTC"] = msdate(ctx.clock.now())


def history(
    ctx: ProviderContext,
    tenant: str,
    resource: str,
    identifier: str,
    message: str,
    user: str = "Emulators",
    changes: str = "Updated",
) -> None:
    ctx.store.collection("_history").put(
        {
            "id": ctx.ids.guid(),
            "_tenant": tenant,
            "_resource": resource,
            "_parent": identifier,
            "Changes": changes,
            "DateUTC": msdate(ctx.clock.now()),
            "User": user,
            "Details": message,
        }
    )


def utc_text(ctx: ProviderContext) -> str:
    return isoformat_z(ctx.clock.now())
