"""Shared wire conventions for the QuickBooks Online emulator.

QBO quirks centralised here: every JSON body wraps the entity and carries a
``time`` stamp in Pacific offset, errors are ``Fault`` envelopes, ids are
per-entity numeric strings, and references are ``{"value", "name"}`` pairs.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, NoReturn
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from fastapi import Request
from starlette.responses import JSONResponse, Response

from emulators.core.auth import Grant, bearer_token
from emulators.core.clock import parse_instant, utc
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext, external_base

PACIFIC = timezone(timedelta(hours=-7))

# canonical entity name -> path segment (all lower case, as QBO uses them)
ENTITIES: dict[str, str] = {
    "Account": "account",
    "Attachable": "attachable",
    "Bill": "bill",
    "BillPayment": "billpayment",
    "Class": "class",
    "CompanyInfo": "companyinfo",
    "CreditMemo": "creditmemo",
    "Customer": "customer",
    "Department": "department",
    "Deposit": "deposit",
    "Employee": "employee",
    "Estimate": "estimate",
    "Invoice": "invoice",
    "Item": "item",
    "JournalEntry": "journalentry",
    "Payment": "payment",
    "PaymentMethod": "paymentmethod",
    "Preferences": "preferences",
    "Purchase": "purchase",
    "SalesReceipt": "salesreceipt",
    "TaxAgency": "taxagency",
    "TaxCode": "taxcode",
    "TaxRate": "taxrate",
    "Term": "term",
    "TimeActivity": "timeactivity",
    "Vendor": "vendor",
}
BY_PATH = {v: k for k, v in ENTITIES.items()}
BY_LOWER = {k.lower(): k for k in ENTITIES}

NAME_LIST = {
    "Account",
    "Class",
    "Customer",
    "Department",
    "Employee",
    "Item",
    "PaymentMethod",
    "TaxAgency",
    "TaxCode",
    "TaxRate",
    "Term",
    "Vendor",
}
TRANSACTIONS = {
    "Bill",
    "BillPayment",
    "CreditMemo",
    "Deposit",
    "Estimate",
    "Invoice",
    "JournalEntry",
    "Payment",
    "Purchase",
    "SalesReceipt",
    "TimeActivity",
}
VOIDABLE = {"Invoice", "Payment", "SalesReceipt", "BillPayment"}
# Field whose uniqueness QBO enforces per name-list entity (error 6240).
UNIQUE_NAME: dict[str, str] = {
    "Account": "Name",
    "Class": "Name",
    "Customer": "DisplayName",
    "Department": "Name",
    "Employee": "DisplayName",
    "Item": "Name",
    "PaymentMethod": "Name",
    "TaxAgency": "DisplayName",
    "TaxCode": "Name",
    "TaxRate": "Name",
    "Term": "Name",
    "Vendor": "DisplayName",
}
# Minimum payload QBO accepts on create; missing -> code 2020 / 6000.
REQUIRED: dict[str, list[str]] = {
    "Account": ["Name", "AccountType"],
    "Bill": ["VendorRef", "Line"],
    "BillPayment": ["VendorRef", "Line"],
    "Class": ["Name"],
    "CreditMemo": ["CustomerRef", "Line"],
    "Customer": ["DisplayName"],
    "Department": ["Name"],
    "Deposit": ["Line"],
    "Employee": ["GivenName", "FamilyName"],
    "Estimate": ["CustomerRef", "Line"],
    "Invoice": ["CustomerRef", "Line"],
    "Item": ["Name", "Type"],
    "JournalEntry": ["Line"],
    "Payment": ["CustomerRef"],
    "PaymentMethod": ["Name"],
    "Purchase": ["AccountRef", "Line"],
    "SalesReceipt": ["CustomerRef", "Line"],
    "TaxAgency": ["DisplayName"],
    "TaxCode": ["Name"],
    "TaxRate": ["Name", "RateValue"],
    "Term": ["Name"],
    "TimeActivity": ["NameOf"],
    "Vendor": ["DisplayName"],
}


def realm(ctx: ProviderContext) -> str:
    return ctx.world.org.quickbooks_realm_id


def qbo_time(instant: datetime) -> str:
    """``2026-10-16T07:12:45.123-07:00`` — QBO's timestamp format."""
    local = utc(instant).astimezone(PACIFIC)
    return local.strftime("%Y-%m-%dT%H:%M:%S.") + f"{local.microsecond // 1000:03d}-07:00"


def now_text(ctx: ProviderContext) -> str:
    return qbo_time(ctx.clock.now())


def date_time(d: date, hour: int = 9) -> str:
    return qbo_time(datetime(d.year, d.month, d.day, hour, tzinfo=UTC))


def parse_time(value: Any) -> datetime | None:
    try:
        return parse_instant(str(value))
    except (ValueError, TypeError):
        return None


def money(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def public(value: Any) -> Any:
    """Deep copy with internal ``_``-prefixed keys removed."""
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [public(v) for v in value]
    return copy.deepcopy(value)


def ref(record: dict[str, Any] | None, name_field: str = "Name") -> dict[str, Any] | None:
    if record is None:
        return None
    out: dict[str, Any] = {"value": str(record.get("Id", ""))}
    name = record.get(name_field) or record.get("DisplayName") or record.get("Name") or ""
    if name:
        out["name"] = str(name)
    return out


def ref_value(value: Any) -> str | None:
    if isinstance(value, dict):
        v = value.get("value")
        return str(v) if v is not None else None
    if value is None:
        return None
    return str(value)


# -- faults -----------------------------------------------------------------------


def fault_body(
    ctx: ProviderContext,
    message: str,
    detail: str,
    code: str,
    *,
    fault_type: str = "ValidationFault",
    element: str = "",
) -> dict[str, Any]:
    return {
        "Fault": {
            "Error": [{"Message": message, "Detail": detail, "code": code, "element": element}],
            "type": fault_type,
        },
        "time": now_text(ctx),
    }


def fail(
    ctx: ProviderContext,
    status: int,
    message: str,
    detail: str,
    code: str,
    *,
    fault_type: str = "ValidationFault",
    element: str = "",
    headers: dict[str, str] | None = None,
) -> NoReturn:
    raise EmulatorError(
        status,
        fault_body(ctx, message, detail, code, fault_type=fault_type, element=element),
        headers=headers or {},
    )


def not_found(ctx: ProviderContext, entity: str) -> NoReturn:
    fail(
        ctx,
        400,
        "Object Not Found",
        "Object Not Found : Something you're trying to use has been made inactive. "
        "Check the fields with accounts, customers, items, vendors or employees.",
        "610",
    )


def auth_failure(ctx: ProviderContext, detail: str = "Token expired") -> NoReturn:
    body = {
        "fault": {
            "error": [
                {
                    "message": "message=AuthenticationFailed; errorCode=003200; statusCode=401",
                    "detail": detail,
                    "code": "3200",
                }
            ],
            "type": "AUTHENTICATION",
        },
        "time": now_text(ctx),
    }
    raise EmulatorError(
        401,
        body,
        headers={
            "WWW-Authenticate": 'Bearer [REDACTED]"Intuit"',
            "intuit_tid": ctx.ids.hex(32),
        },
    )


# -- auth / realm ------------------------------------------------------------------


def require_actor(ctx: ProviderContext, request: Request, realm_id: str) -> Grant:
    """Resolve the bearer token and bind it to the realm in the path."""
    token = bearer_token(request)
    grant = ctx.tokens.resolve(token)
    if grant is None:
        if token and ctx.tokens.is_expired(token):
            auth_failure(ctx, "Token expired: access token has expired")
        auth_failure(ctx, "AuthenticationFailed: invalid or missing access token")
    assert grant is not None
    token_realm = (grant.extra or {}).get("realm_id")
    if token_realm and str(token_realm) != str(realm_id):
        auth_failure(ctx, "Token is not valid for this company (realm mismatch)")
    if str(realm_id) != realm(ctx):
        fail(
            ctx,
            403,
            "Authorization Failure",
            "Authorization Failure : The realm ID in the request does not match the company "
            "the access token was issued for.",
            "110",
            fault_type="AuthenticationFault",
        )
    # Delegated tokens for departed employees lose access, like a revoked user.
    if not grant.app_only and grant.user_id:
        emp = ctx.store.collection("_users").get(grant.user_id)
        if emp is not None and not emp.get("active", True):
            auth_failure(ctx, "User is inactive")
    return grant


def user_for_grant(ctx: ProviderContext, grant: Grant) -> dict[str, Any] | None:
    if not grant.user_id:
        return None
    users = ctx.store.collection("_users")
    record = users.get(grant.user_id)
    if record is not None:
        return record
    for record in users:
        if record.get("email", "").lower() == str(grant.user_id).lower():
            return record
    return None


# -- responses ---------------------------------------------------------------------


def wants_json(request: Request) -> bool:
    # QBO defaults to XML in production; leniently only an explicit XML Accept,
    # without JSON, gets XML (httpx's default Accept is */*).
    accept = request.headers.get("accept", "")
    wants_xml = "application/xml" in accept or "text/xml" in accept
    wants_json_type = "application/json" in accept or "text/json" in accept
    return wants_json_type or not wants_xml


def wrap(ctx: ProviderContext, entity: str, record: dict[str, Any]) -> dict[str, Any]:
    return {entity: public(record), "time": now_text(ctx)}


def respond(ctx: ProviderContext, request: Request, body: dict[str, Any], status: int = 200) -> Response:
    headers = {"intuit_tid": ctx.ids.hex(32)}
    if wants_json(request):
        return JSONResponse(body, status_code=status, headers=headers)
    return Response(to_xml(body), status_code=status, headers=headers, media_type="application/xml")


def _xml_value(tag: str, value: Any) -> str:
    tag = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)
    if isinstance(value, dict):
        inner = "".join(_xml_value(k, v) for k, v in value.items())
        return f"<{tag}>{inner}</{tag}>"
    if isinstance(value, list):
        return "".join(_xml_value(tag, v) for v in value)
    if value is None:
        return f"<{tag}/>"
    text = ("true" if value else "false") if isinstance(value, bool) else xml_escape(str(value))
    return f"<{tag}>{text}</{tag}>"


def to_xml(body: dict[str, Any]) -> str:
    time_value = body.get("time", "")
    inner = "".join(_xml_value(k, v) for k, v in body.items() if k != "time")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<IntuitResponse xmlns="http://schema.intuit.com/finance/v3" time="{xml_escape(str(time_value))}">'
        f"{inner}</IntuitResponse>"
    )


def invoice_link(ctx: ProviderContext, request: Request | None, invoice_id: str) -> str:
    base = external_base(request) if request is not None else ""
    return f"{base}/v3/company/{realm(ctx)}/invoice/{quote(str(invoice_id))}/pdf"


def stamp_new(ctx: ProviderContext, record: dict[str, Any], when: datetime | None = None) -> None:
    text = qbo_time(when or ctx.clock.now())
    record.setdefault("MetaData", {"CreateTime": text, "LastUpdatedTime": text})
    record["MetaData"]["CreateTime"] = record["MetaData"].get("CreateTime", text)
    record["MetaData"]["LastUpdatedTime"] = text


def touch(ctx: ProviderContext, record: dict[str, Any]) -> None:
    meta = record.setdefault("MetaData", {"CreateTime": now_text(ctx)})
    meta["LastUpdatedTime"] = now_text(ctx)
    try:
        record["SyncToken"] = str(int(record.get("SyncToken", 0) or 0) + 1)
    except (TypeError, ValueError):
        record["SyncToken"] = "1"
