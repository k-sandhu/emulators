"""Trello inputs, permissions and wire errors shared by all resources."""

from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime
from typing import Any, NoReturn
from urllib.parse import urlsplit

from fastapi import Request

from emulators.core.auth import Grant
from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext

Record = dict[str, Any]
KEY = stable_hash("trello", "halcyon", "key", length=32)
SECRET = "halcyon-trello-application-secret"
TOKEN = stable_hash("trello", "p001", "token", length=64)
COLORS = {"green", "yellow", "orange", "red", "purple", "blue", "sky", "lime", "pink", "black"}


def fail(status: int, message: str) -> NoReturn:
    raise EmulatorError(status, message, media_type="text/plain")


def text(value: Any, name: str, maximum: int = 16384, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum:
        fail(400, f"invalid value for {name}")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]", value):
        fail(400, f"invalid value for {name}")
    return value


def flag(value: Any, name: str) -> bool:
    if type(value) is bool:
        return value
    if isinstance(value, str) and value in ("true", "false"):
        return value == "true"
    fail(400, f"invalid value for {name}")


def integer(value: Any, name: str, default: int, maximum: int = 1000, minimum: int = 1) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,10}", str(value)):
        fail(400, f"invalid value for {name}")
    result = int(value)
    if not minimum <= result <= maximum:
        fail(400, f"invalid value for {name}")
    return result


def names(value: Any, name: str = "fields") -> list[str]:
    if isinstance(value, str):
        return [v for v in value.split(",") if v]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(dict.fromkeys(value))
    fail(400, f"invalid value for {name}")


def date_value(value: Any, name: str) -> str | None:
    if value is None or value == "null":
        return None
    try:
        return isoformat_z(parse_instant(text(value, name)), timespec="milliseconds")
    except (ValueError, OverflowError):
        fail(400, f"invalid value for {name}")


def http_url(value: Any, name: str) -> str:
    value = text(value, name, 4096)
    try:
        parts = urlsplit(value)
        if parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password:
            return value
    except ValueError:
        pass
    fail(400, f"invalid value for {name}")


def reject_constant(value: str) -> NoReturn:
    raise ValueError(value)


async def parameters(request: Request) -> Record:
    result: Record = dict(request.query_params)
    if request.method in ("POST", "PUT", "PATCH"):
        content_type = request.headers.get("content-type", "").lower()
        try:
            if "application/json" in content_type:
                raw = await request.body()
                body = json.loads(raw, parse_constant=reject_constant) if raw else {}
                if not isinstance(body, dict):
                    fail(400, "invalid JSON")
                result.update(body)
            elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
                form = await request.form()
                result.update(dict(form))
            elif await request.body():
                fail(400, "invalid request body")
        except (ValueError, UnicodeError, RecursionError):
            fail(400, "invalid JSON")
    pending = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            fail(400, "invalid JSON")
        if isinstance(value, str):
            text(value, "parameter", 1_000_000, empty=True)
        elif isinstance(value, float) and not math.isfinite(value):
            fail(400, "invalid JSON")
        elif isinstance(value, dict):
            pending.extend((part, depth + 1) for pair in value.items() for part in pair)
        elif isinstance(value, list):
            pending.extend((part, depth + 1) for part in value)
    return result


def oid(kind: str, value: object, when: datetime) -> str:
    return f"{int(when.timestamp()):08x}" + stable_hash("trello", kind, value, length=16)


def new_id(ctx: ProviderContext, kind: str) -> str:
    with ctx.store.lock:
        seq = ctx.store.meta.get("sequence", 0) + 1
        ctx.store.meta["sequence"] = seq
    return f"{int(ctx.clock.timestamp()):08x}" + stable_hash("trello", kind, length=10) + f"{seq:06x}"


def clean(record: Record) -> Record:
    return copy.deepcopy({k: v for k, v in record.items() if not k.startswith("_")})


def project(record: Record, fields: Any = "all") -> Record:
    value = clean(record)
    wanted = names(fields)
    if wanted == ["none"]:
        return {"id": value["id"]}
    if "all" in wanted:
        return value
    if any(name not in value for name in wanted):
        fail(400, "invalid field")
    return {"id": value["id"], **{key: value[key] for key in wanted if key != "id"}}


def member(ctx: ProviderContext, value: Any) -> Record:
    if not isinstance(value, str):
        fail(400, "invalid member id")
    found = ctx.store["members"].get(value) or next(
        (m for m in ctx.store["members"] if value in (m["username"], m["_person"], m["_email"])), None
    )
    if not found:
        fail(404, "member not found")
    return found


def lookup(ctx: ProviderContext, kind: str, value: Any) -> Record:
    if not isinstance(value, str):
        fail(400, "invalid id")
    record = ctx.store[kind].get(value) or next(
        (
            r
            for r in ctx.store[kind]
            if value in (r.get("shortLink"), r.get("name") if kind == "organizations" else None)
        ),
        None,
    )
    if not record:
        fail(404, f"{kind.removesuffix('s')} not found")
    return record


def can_view(ctx: ProviderContext, board: Record, grant: Grant | None) -> bool:
    uid = grant.user_id if grant else None
    if board["prefs"]["permissionLevel"] == "public" or uid in board["_members"]:
        return True
    org = ctx.store["organizations"].get(board["idOrganization"])
    return bool(board["prefs"]["permissionLevel"] == "org" and org and uid in org["_members"])


def board_access(
    ctx: ProviderContext, board: Record, grant: Grant | None, *, write: bool = False, admin: bool = False
) -> None:
    if not can_view(ctx, board, grant):
        fail(401, "unauthorized permission requested")
    role = board["_members"].get(grant.user_id) if grant else None
    if admin and role != "admin":
        fail(401, "unauthorized permission requested")
    if write and (role not in ("normal", "admin") or board["closed"]):
        fail(401, "unauthorized permission requested")


def get_board(
    ctx: ProviderContext, value: Any, grant: Grant | None, *, write: bool = False, admin: bool = False
) -> Record:
    board = lookup(ctx, "boards", value)
    board_access(ctx, board, grant, write=write, admin=admin)
    return board


def resource(
    ctx: ProviderContext, kind: str, value: Any, grant: Grant | None, *, write: bool = False
) -> Record:
    record = lookup(ctx, kind, value)
    get_board(ctx, record["idBoard"], grant, write=write)
    return record


def position(value: Any, peers: list[Record]) -> float:
    positions = [p["pos"] for p in peers]
    if value == "top":
        return min(positions) / 2 if positions else 16384.0
    if value == "bottom":
        return max(positions, default=0) + 16384.0
    try:
        if isinstance(value, (bool, dict, list)):
            raise ValueError
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError):
        fail(400, "invalid value for pos")


def page(items: list[Record], params: Record, *, default: int = 1000, maximum: int = 1000) -> list[Record]:
    limit = integer(params.get("limit"), "limit", default, maximum)
    result = sorted(items, key=lambda r: r["id"], reverse=True)
    for field, before in (("before", True), ("since", False)):
        if field not in params:
            continue
        if params[field] is None or params[field] == "null":
            continue
        cursor = text(params[field], field)
        if re.fullmatch("[0-9a-f]{24}", cursor):
            result = [r for r in result if (r["id"] < cursor if before else r["id"] > cursor)]
        else:
            stamp = date_value(cursor, field)
            result = [
                r
                for r in result
                if (
                    r.get("date", r.get("_created", "")) < stamp
                    if before
                    else r.get("date", r.get("_created", "")) > stamp
                )
            ]
    return result[:limit]
