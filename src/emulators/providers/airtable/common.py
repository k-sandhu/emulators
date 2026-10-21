"""Airtable envelopes, identifiers and base-level authorization."""

from __future__ import annotations

import json
import math
import re
from datetime import timedelta
from typing import Any, NoReturn

from fastapi import Request

from emulators.core.auth import Grant, bearer_token
from emulators.core.clock import isoformat_z
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext

SCOPES = ["data.records:read", "data.records:write", "schema.bases:read", "webhook:manage", "user.email:read"]
OWNER_TOKEN = "pat-airtable-owner"
READER_TOKEN = "pat-airtable-reader"


def fail(status: int, kind: str, message: str) -> NoReturn:
    raise EmulatorError(status, {"error": {"type": kind, "message": message}})


def invalid(message: str, kind: str = "INVALID_REQUEST_UNKNOWN") -> NoReturn:
    fail(422, kind, message)


def identifier(ctx: ProviderContext, prefix: str, *parts: object) -> str:
    return prefix + stable_hash("airtable", ctx.world.seed, *parts, length=14)


def new_id(ctx: ProviderContext, prefix: str) -> str:
    return prefix + ctx.ids.hex(14)


def timestamp(ctx: ProviderContext) -> str:
    return isoformat_z(ctx.clock.now(), timespec="milliseconds")


def text(value: Any, name: str, *, maximum: int = 100000, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        invalid(f"{name} must be a valid string.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        invalid(f"{name} must contain valid UTF-8.")
    return value


def finite(value: Any, name: str) -> int | float:
    if type(value) not in (int, float) or abs(value) > 1e15 or not math.isfinite(value):
        invalid(f"{name} must be a finite number.", "INVALID_VALUE_FOR_COLUMN")
    return value


def integer(value: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
        value = int(value)
    if type(value) is not int or not lo <= value <= hi:
        invalid(f"{name} must be an integer between {lo} and {hi}.")
    return value


async def body(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/json":
        fail(415, "INVALID_REQUEST_BODY", "Request body must use application/json.")
    raw = await request.body()
    if len(raw) > 8 * 1024 * 1024:
        fail(413, "REQUEST_TOO_LARGE", "Request body is too large.")
    try:
        value = json.loads(raw)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        fail(400, "INVALID_REQUEST_BODY", "Request body must be valid UTF-8 JSON.")
    if not isinstance(value, dict):
        fail(400, "INVALID_REQUEST_BODY", "Request body must be a JSON object.")
    return value


def authenticate(ctx: ProviderContext, request: Request, scope: str | None = None) -> Grant:
    grant = ctx.tokens.resolve(bearer_token(request))
    if request.headers.get("authorization", "").partition(" ")[0].lower() != "bearer" or grant is None:
        fail(401, "AUTHENTICATION_REQUIRED", "Authentication required")
    user = ctx.store.collection("users").get(grant.user_id)
    if user is None or not user["_active"]:
        fail(401, "AUTHENTICATION_REQUIRED", "Authentication required")
    if scope and scope not in grant.scopes:
        fail(
            403,
            "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND",
            "Invalid permissions, or the requested model was not found.",
        )
    return grant


def role(ctx: ProviderContext, grant: Grant, base: dict[str, Any]) -> str:
    allowed = grant.extra.get("bases")
    if allowed is not None and base["id"] not in allowed:
        return "none"
    return base["_acl"].get(grant.user_id, "none")


def base_access(
    ctx: ProviderContext, request: Request, base_id: str, scope: str, *, level: str = "read"
) -> tuple[Grant, dict[str, Any]]:
    grant = authenticate(ctx, request, scope)
    base = ctx.store.collection("bases").get(base_id)
    levels = {"none": 0, "read": 1, "comment": 2, "edit": 3, "create": 4}
    if base is None or levels[role(ctx, grant, base)] < levels[level]:
        fail(
            403,
            "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND",
            "Invalid permissions, or the requested model was not found.",
        )
    return grant, base


def table_for(ctx: ProviderContext, base_id: str, key: str) -> dict[str, Any]:
    table = next(
        (t for t in ctx.store.collection("tables") if t["_base"] == base_id and key in (t["id"], t["name"])),
        None,
    )
    if table is None:
        fail(404, "NOT_FOUND", "Could not find what you are looking for")
    return table


def field_for(table: dict[str, Any], key: str) -> dict[str, Any]:
    field = next((f for f in table["fields"] if key in (f["id"], f["name"])), None)
    if field is None:
        invalid(f'Unknown field name: "{key}"', "UNKNOWN_FIELD_NAME")
    return field


def records(ctx: ProviderContext, table: dict[str, Any]) -> list[dict[str, Any]]:
    return ctx.store.collection("records").where(_table=table["id"])


def record_for(ctx: ProviderContext, table: dict[str, Any], key: str) -> dict[str, Any]:
    record = ctx.store.collection("records").get(key)
    if record is None or record["_table"] != table["id"]:
        fail(404, "NOT_FOUND", "Could not find what you are looking for")
    return record


def public(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    user_key = payload.get("user", "p001")
    user = next(
        (u for u in ctx.store.collection("users") if user_key in (u["id"], u["email"], u["_world"])), None
    )
    if user is None or not user["_active"] or payload.get("app_only"):
        invalid("A token must identify an active Airtable user.")
    scopes = payload.get("scopes", SCOPES)
    if not isinstance(scopes, list) or any(not isinstance(s, str) or s not in SCOPES for s in scopes):
        invalid("scopes must contain supported Airtable scope names.")
    bases = payload.get("bases")
    if bases is not None and (
        not isinstance(bases, list)
        or any(not isinstance(b, str) or b not in ctx.store.collection("bases") for b in bases)
    ):
        invalid("bases must be an array of base IDs.")
    ttl = payload.get("expires_in", 3600)
    if type(ttl) is not int or not -86400 <= ttl <= 31536000:
        invalid("expires_in must be an integer in the supported range.")
    grant = ctx.tokens.issue_access(
        client_id="airtable-control",
        user_id=user["id"],
        scopes=scopes,
        ttl=timedelta(seconds=ttl),
        extra={"bases": bases, "auth_type": "pat"},
        prefix="pat",
    )
    return {"access_token": grant.token, "token_type": "Bearer", "expires_in": ttl}
