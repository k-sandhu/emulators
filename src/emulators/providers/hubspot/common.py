"""Shared HubSpot wire shapes and permission checks."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, NoReturn
from uuid import uuid4

from fastapi import Request

from emulators.core.auth import Grant, bearer_token
from emulators.core.clock import isoformat_z
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_guid, stable_hash
from emulators.core.provider import ProviderContext

PORTAL_ID = 24681357
APP_ID = 135790
PRIVATE_TOKEN = "pat-na1-" + stable_guid("hubspot", "private-app")
OBJECT_IDS = {
    "contacts": "0-1",
    "companies": "0-2",
    "deals": "0-3",
    "tickets": "0-5",
    "products": "0-7",
    "line_items": "0-8",
    "calls": "0-48",
    "emails": "0-49",
    "meetings": "0-47",
    "notes": "0-46",
    "tasks": "0-27",
}
ACTIVITIES = {"calls", "emails", "meetings", "notes", "tasks"}
DEFAULTS = {
    "contacts": ["email", "firstname", "lastname"],
    "companies": ["name", "domain"],
    "deals": ["dealname", "amount", "closedate", "dealstage", "pipeline"],
    "tickets": ["subject", "content", "hs_pipeline", "hs_pipeline_stage", "hs_ticket_priority"],
    "products": ["name", "description", "price"],
    "line_items": ["name", "quantity", "price", "amount", "hs_product_id"],
    "meetings": ["hs_meeting_title", "hs_meeting_start_time", "hs_meeting_end_time"],
    "emails": ["hs_email_subject", "hs_email_text", "hs_email_direction"],
    "notes": ["hs_note_body", "hs_timestamp"],
    "tasks": ["hs_task_subject", "hs_task_status", "hs_timestamp"],
    "calls": ["hs_call_title", "hs_call_body", "hs_timestamp"],
}


def object_type(value: str) -> str:
    for name, type_id in OBJECT_IDS.items():
        if value in (name, type_id, name.removesuffix("s")) or (name == "companies" and value == "company"):
            return name
    fail(404, f"Unable to infer object type from: {value}")


def error_body(status: int, message: str, **extra: Any) -> dict[str, Any]:
    category = {
        401: "INVALID_AUTHENTICATION",
        403: "MISSING_SCOPES",
        404: "OBJECT_NOT_FOUND",
        409: "CONFLICT",
        429: "RATE_LIMITS",
    }.get(status, "VALIDATION_ERROR")
    return {
        "status": "error",
        "message": message,
        "correlationId": str(uuid4()),
        "category": category,
        "errors": [],
        "context": {},
        **extra,
    }


def fail(status: int, message: str, **extra: Any) -> NoReturn:
    raise EmulatorError(status, error_body(status, message, **extra))


def stamp(value: datetime) -> str:
    return isoformat_z(value, timespec="milliseconds")


def numeric_id(kind: str, world_id: str) -> str:
    if kind == "users":
        return str(1_000_000 + int(stable_hash("hubspot", kind, world_id, length=8), 16) % 100_000_000)
    return str(10_000_000_000 + int(stable_hash("hubspot", kind, world_id, length=12), 16))


def scopes_for(kind: str, mode: str = "read") -> list[str]:
    if kind in ACTIVITIES:
        return [f"crm.objects.contacts.{mode}", *([f"crm.objects.{kind}.read"] if mode == "read" else [])]
    if kind == "tickets":
        return ["tickets", f"crm.objects.tickets.{mode}"]
    return [f"crm.objects.{kind}.{mode}"]


def all_scopes() -> list[str]:
    return [
        "oauth",
        "crm.objects.owners.read",
        "account-info.security.read",
        "tickets",
        "e-commerce",
        *[s for k in OBJECT_IDS for m in ("read", "write") for s in scopes_for(k, m)],
        *[f"crm.schemas.{k}.{m}" for k in OBJECT_IDS for m in ("read", "write")],
    ]


def owner_for(ctx: ProviderContext, user: str | None) -> dict[str, Any] | None:
    return next(
        (r for r in ctx.store["owners"] if user in (r["_world_id"], r["email"], str(r["userId"]), r["id"])),
        None,
    )


def require(request: Request, ctx: ProviderContext, scopes: list[str] | None = None) -> Grant:
    grant = ctx.tokens.resolve(bearer_token(request))
    if grant is None:
        fail(401, "Authentication credentials not found or invalid.")
    if scopes and not any(s in grant.scopes for s in (*scopes, "*")):
        fail(
            403,
            "This app hasn't been granted all required scopes to make this call.",
            errors=[
                {
                    "message": "One or more of the following scopes are required.",
                    "context": {"requiredGranularScopes": scopes},
                }
            ],
            context={"requiredGranularScopes": scopes},
        )
    if not grant.app_only and (
        owner_for(ctx, grant.user_id) is None or owner_for(ctx, grant.user_id)["archived"]
    ):
        fail(403, "The user does not have CRM access.", category="FORBIDDEN")
    return grant


def visible(ctx: ProviderContext, grant: Grant, kind: str, record: dict[str, Any]) -> bool:
    if grant.app_only:
        return True
    owner = owner_for(ctx, grant.user_id)
    if owner is None or owner["archived"]:
        return False
    if not owner.get("_owned_only") or kind == "products":
        return True
    return record["properties"].get("hubspot_owner_id") == owner["id"]


def fetch(
    ctx: ProviderContext,
    grant: Grant,
    kind: str,
    record_id: str,
    id_property: str | None = None,
    archived: bool = False,
) -> dict[str, Any]:
    if id_property and id_property != "hs_object_id":
        definition = ctx.store[f"properties:{kind}"].get(id_property)
        if definition is None or not definition.get("hasUniqueValue"):
            fail(400, f"Property {id_property} is not a unique property.")
        record = next(
            (
                r
                for r in ctx.store[kind]
                if str(r["properties"].get(id_property, "")).casefold() == record_id.casefold()
            ),
            None,
        )
    else:
        record = ctx.store[kind].get(record_id)
    if record is None or bool(record["archived"]) != archived or not visible(ctx, grant, kind, record):
        fail(404, f"Object not found. objectId={record_id}", context={"id": [record_id]})
    return record


def names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p for p in value.split(",") if p]
    if not isinstance(value, list) or any(not isinstance(p, str) for p in value):
        fail(400, "Expected a list of property names.")
    return [p for item in value for p in item.split(",") if p]


def query_names(request: Request, name: str) -> list[str] | None:
    return names(request.query_params.getlist(name)) if name in request.query_params else None


def integer(value: Any, default: int, maximum: int, *, minimum: int = 1) -> int:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not re.fullmatch(r"\d+", str(value))
        or not minimum <= int(value) <= maximum
    ):
        fail(400, f"Expected an integer between {minimum} and {maximum}.")
    return int(value)


def flag(value: str | None) -> bool:
    if value not in (None, "true", "false"):
        fail(400, "Expected true or false.")
    return value == "true"


async def body(request: Request) -> dict[str, Any]:
    try:
        result = await request.json()
    except (ValueError, UnicodeDecodeError):
        fail(400, "Invalid JSON request body.")
    if not isinstance(result, dict):
        fail(400, "Expected a JSON object.")
    return result


def inputs(payload: dict[str, Any], maximum: int = 100) -> list[dict[str, Any]]:
    items = payload.get("inputs")
    if (
        not isinstance(items, list)
        or not items
        or len(items) > maximum
        or any(not isinstance(i, dict) for i in items)
    ):
        fail(400, f"inputs must contain between 1 and {maximum} objects.")
    return items
