"""Shared wire shapes, permissions and URL handling for the two Atlassian products."""

from __future__ import annotations

import copy
import json
import re
from http import HTTPStatus
from typing import Any, NoReturn
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse

from emulators.core.auth import Grant
from emulators.core.errors import EmulatorError
from emulators.core.ids import b64url, b64url_decode, stable_guid, stable_hash
from emulators.core.provider import ProviderContext, external_base, url_for_host

Record = dict[str, Any]
SITE = "halcyonadvisory.atlassian.net"
CLOUD_ID = stable_guid("atlassian", "halcyonadvisory", "cloud")
API_TOKEN = "halcyon-api-token"
DEFAULT_SCOPES = [
    "read:confluence-content.all",
    "read:confluence-space.summary",
    "search:confluence",
    "read:confluence-user",
    "write:confluence-content",
    "read:page:confluence",
    "read:space:confluence",
    "read:jira-work",
    "write:jira-work",
    "read:jira-user",
    "offline_access",
]


def account_id(person_id: str) -> str:
    return "712020:" + stable_guid("atlassian", "account", person_id)


def numeric_id(*parts: object) -> str:
    return str(10000000 + int(stable_hash("atlassian", *parts, length=14), 16))


def error_body(status: int, message: str, family: str = "v2") -> Record:
    title = HTTPStatus(status).phrase
    if family == "jira":
        return {"errorMessages": [message], "errors": {}}
    if family == "v1":
        return {
            "statusCode": status,
            "data": {"authorized": status != 401, "valid": False},
            "message": message,
            "reason": title,
        }
    codes = {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
        429: "RATE_LIMIT_EXCEEDED",
    }
    return {
        "errors": [
            {
                "status": status,
                "code": codes.get(status, title.upper().replace(" ", "_")),
                "title": title,
                "detail": message if status != 404 else None,
            }
        ]
    }


def family(request: Request) -> str:
    if "/rest/api/3" in request.url.path or "/ex/jira/" in request.url.path:
        return "jira"
    return "v1" if "/wiki/rest/" in request.url.path else "v2"


def fail(request: Request, status: int, message: str) -> NoReturn:
    headers = {"WWW-Authenticate": 'Bearer realm="Atlassian"'} if status == 401 else {}
    raise EmulatorError(status, error_body(status, message, family(request)), headers=headers)


def clean(record: Record) -> Record:
    return copy.deepcopy({k: v for k, v in record.items() if not k.startswith("_") or k == "_links"})


def local_path(request: Request) -> str:
    path = request.scope["path"]
    root = request.scope.get("root_path", "")
    return path.removeprefix(root) if root else path


def absolute(request: Request, path: str) -> str:
    return external_base(request) + path


def gateway_prefix(request: Request) -> str:
    match = re.match(r"(/ex/(?:confluence|jira)/[^/]+)", local_path(request))
    return match.group(1) if match else ""


def site_url(request: Request, ctx: ProviderContext, path: str = "") -> str:
    return url_for_host(request, ctx, SITE, path or "/").rstrip("/")


def api_url(request: Request, path: str) -> str:
    return absolute(request, gateway_prefix(request) + path)


def query_values(request: Request, name: str) -> list[str]:
    return [v for item in request.query_params.getlist(name) for v in item.split(",") if v]


def integer(
    request: Request, value: Any, name: str, default: int, maximum: int = 250, minimum: int = 1
) -> int:
    try:
        result = int(value) if value is not None else default
    except (ValueError, TypeError):
        fail(request, 400, f"Invalid {name}.")
    if not minimum <= result <= maximum:
        fail(request, 400, f"{name} must be between {minimum} and {maximum}.")
    return result


def cursor_token(offset: int, binding: str) -> str:
    return b64url(json.dumps([offset, stable_hash(binding, length=16)], separators=(",", ":")).encode())


def cursor_offset(request: Request, token: str | None, binding: str) -> int:
    if not token:
        return 0
    try:
        offset, digest = json.loads(b64url_decode(token))
        if type(offset) is not int or offset < 0 or digest != stable_hash(binding, length=16):
            raise ValueError
        return offset
    except (ValueError, TypeError, UnicodeError):
        fail(request, 400, "Invalid cursor.")


def page_response(request: Request, ctx: ProviderContext, records: list[Record]) -> JSONResponse:
    limit = integer(request, request.query_params.get("limit"), "limit", 25)
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k != "cursor"]
    binding = local_path(request) + "?" + urlencode(sorted(pairs))
    offset = cursor_offset(request, request.query_params.get("cursor"), binding)
    links = {"base": site_url(request, ctx, "/wiki")}
    headers: dict[str, str] = {}
    if offset + limit < len(records):
        links["next"] = (
            absolute(request, local_path(request))
            + "?"
            + urlencode([*pairs, ("cursor", cursor_token(offset + limit, binding))])
        )
        headers["Link"] = f'<{links["next"]}>; rel="next"'
    return JSONResponse({"results": records[offset : offset + limit], "_links": links}, headers=headers)


def person_record(ctx: ProviderContext, value: str | None) -> Record | None:
    return next(
        (
            u
            for u in ctx.store["users"]
            if value in (u["id"], u["_world_id"])
            or (isinstance(value, str) and value.casefold() == u["email"].casefold())
        ),
        None,
    )


def principal(ctx: ProviderContext, grant: Grant) -> str:
    if grant.app_only:
        value = ctx.setting("APP_USER", "p035") or "p035"
        user = person_record(ctx, value)
        return user["id"] if user else value
    return grant.user_id or ""


def space_role(ctx: ProviderContext, space: Record, grant: Grant) -> str | None:
    user = person_record(ctx, principal(ctx, grant))
    return space["_roles"].get(user["_world_id"]) if user else None


def can_view(ctx: ProviderContext, page: Record, grant: Grant) -> bool:
    space = ctx.store["spaces"].get(page["spaceId"])
    if not space or not space_role(ctx, space, grant):
        return False
    user = person_record(ctx, principal(ctx, grant))
    if not user:
        return False
    if page["status"] == "draft" and page["ownerId"] != user["id"]:
        return False
    for item in [page, *ancestors(ctx, page)]:
        allowed = item.get("_read")
        if allowed is not None and user["_world_id"] not in allowed:
            return False
    return True


def can_edit(ctx: ProviderContext, page: Record, grant: Grant) -> bool:
    if not can_view(ctx, page, grant):
        return False
    user = person_record(ctx, principal(ctx, grant))
    space = ctx.store["spaces"].get(page["spaceId"])
    allowed = page.get("_update")
    return bool(
        user
        and space
        and space_role(ctx, space, grant) in ("owner", "editor")
        and (allowed is None or user["_world_id"] in allowed)
    )


def ancestors(ctx: ProviderContext, page: Record) -> list[Record]:
    result: list[Record] = []
    seen = {page["id"]}
    parent = ctx.store["pages"].get(page.get("parentId"))
    while parent and parent["id"] not in seen:
        result.append(parent)
        seen.add(parent["id"])
        parent = ctx.store["pages"].get(parent.get("parentId"))
    return list(reversed(result))


def get_page(
    request: Request,
    ctx: ProviderContext,
    grant: Grant,
    page_id: str,
    *,
    edit: bool = False,
    content_type: str = "page",
) -> Record:
    page = ctx.store["pages"].get(page_id)
    if not page or page.get("_type", "page") != content_type or not can_view(ctx, page, grant):
        fail(request, 404, f"No content found with id: {page_id}")
    if edit and not can_edit(ctx, page, grant):
        fail(request, 403, "You do not have permission to update this content.")
    return page


def operations(ctx: ProviderContext, page: Record, grant: Grant) -> list[Record]:
    ops = ["read"]
    if can_edit(ctx, page, grant):
        ops += ["update", "delete"]
    return [{"operation": op, "targetType": page.get("_type", "page")} for op in ops]
