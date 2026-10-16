"""Shared Outlook ownership, projections and resource-bound delta cursors."""

from __future__ import annotations

import copy
import re
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import Request

from emulators.core.odata import ODataError
from emulators.core.provider import ProviderContext, external_base
from emulators.providers.microsoft import common as c


def clean(record: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy({k: v for k, v in record.items() if not k.startswith("_")})


def local_path(request: Request) -> str:
    path = request.url.path
    root = request.scope.get("root_path", "")
    return path.removeprefix(root) if root else path


def collection(
    request: Request,
    rows: list[dict[str, Any]],
    context: str,
    *,
    defaults: tuple[str, ...] | None = None,
    size: int = 100,
    maximum: int = 999,
    search: tuple[str, ...] = ("displayName",),
) -> dict[str, Any]:
    scope = dict(request.scope)
    scope["path"] = local_path(request)
    query_request = Request(scope)
    try:
        result = c.collection_response(
            query_request,
            [clean(r) for r in rows],
            context=context,
            default_page_size=size,
            max_page_size=maximum,
            search_fields=search,
        )
    except ODataError as exc:
        raise c.graph_error(400, exc.code, exc.message) from None
    if request.query_params.get("$top") == "0":
        result["value"] = []
        result.pop("@odata.nextLink", None)
    if defaults and not request.query_params.get("$select"):
        result["value"] = [
            {k: v for k, v in r.items() if k in defaults or k == "manager"} for r in result["value"]
        ]
    return result


def single(request: Request, record: dict[str, Any], context: str) -> dict[str, Any]:
    return c.single_response(request, clean(record), context=context)


def user(ctx: ProviderContext, identifier: str) -> dict[str, Any]:
    rows = ctx.store["users"]
    record = rows.get(identifier)
    if record is None:
        identifier = identifier.casefold()
        record = next(
            (
                r
                for r in rows
                if identifier
                in (
                    str(r.get("userPrincipalName", "")).casefold(),
                    str(r.get("mail", "")).casefold(),
                )
            ),
            None,
        )
    if record is None:
        raise c.not_found(identifier)
    return record


def mailbox(request: Request, ctx: ProviderContext, identifier: str) -> dict[str, Any]:
    caller = c.principal(request, ctx)
    record = user(ctx, c.me_user_id(caller) if identifier == "me" else identifier)
    if not caller.app_only and record["id"] != caller.user_id:
        raise c.graph_error(403, "ErrorAccessDenied", "Access is denied. Check credentials and try again.")
    if record.get("userType") == "Guest":
        raise c.graph_error(404, "MailboxNotEnabledForRESTAPI", "The mailbox is not enabled for REST API.")
    return record


def admin(request: Request, ctx: ProviderContext) -> c.Principal:
    caller = c.principal(request, ctx)
    allowed = caller.app_only
    if caller.person_id:
        person = next((p for p in ctx.world.people if p.id == caller.person_id), None)
        allowed = allowed or bool(person and person.is_admin)
    if not allowed:
        raise c.graph_error(
            403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation."
        )
    return caller


def now(ctx: ProviderContext) -> str:
    from emulators.core.clock import isoformat_z

    return isoformat_z(ctx.clock.now())


def outlook_id(ctx: ProviderContext) -> str:
    return "AAMkA" + ctx.ids.urlsafe(88)


def text_body(value: str) -> str:
    import html

    return html.unescape(re.sub("<[^>]+>", "", value))


@c.on_change
async def remember_change(
    ctx: ProviderContext, resource: str, change_type: str, data: dict[str, Any]
) -> None:
    sequence = ctx.store.meta.get("graph_change_sequence", 0) + 1
    ctx.store.meta["graph_change_sequence"] = sequence
    ctx.store["_graph_changes"].put(
        {
            "id": str(sequence),
            "sequence": sequence,
            "resource": resource.strip("/"),
            "changeType": change_type,
            "data": clean(data),
        }
    )


def delta(
    request: Request,
    ctx: ProviderContext,
    rows: list[dict[str, Any]],
    resource: str,
    context: str,
) -> dict[str, Any]:
    """Freeze a cursor boundary so writes during paging appear in the next round."""
    token = request.query_params.get("$deltatoken") or request.query_params.get("$skiptoken")
    binding = (resource, request.query_params.get("startDateTime"), request.query_params.get("endDateTime"))
    previous = ctx.store["_graph_delta_cursors"].get(token) if token else None
    if token and (previous is None or previous["binding"] != binding):
        raise c.graph_error(400, "BadRequest", "Invalid delta token.")
    upper = ctx.store.meta.get("graph_change_sequence", 0)
    offset = 0
    current = {record["id"]: record for record in rows}
    known_ids = list(current)
    if "$skiptoken" in request.query_params:
        rows, upper, offset = previous["rows"], previous["upper"], previous["offset"]
        known_ids = previous["known_ids"]
    elif previous:
        changes: dict[str, dict[str, Any]] = {}
        for change in ctx.store["_graph_changes"]:
            if previous["upper"] < change["sequence"] <= upper and change["resource"].rsplit("/", 1)[
                0
            ] == resource.rstrip("/"):
                item = change["data"]
                if item["id"] in current:
                    changes[item["id"]] = current[item["id"]]
                elif item["id"] in previous["known_ids"]:
                    changes[item["id"]] = {"id": item["id"], "@removed": {"reason": "deleted"}}
        rows = list(changes.values())
    options = c.query_options(request)
    size = min(options.top if options.top is not None else 100, 999)
    page = rows[offset : offset + size]
    result = {"@odata.context": c.context_url(request, context), "value": [clean(r) for r in page]}
    more = bool(size and offset + size < len(rows))
    encoded = secrets.token_urlsafe(32)
    ctx.store["_graph_delta_cursors"].put(
        {
            "id": encoded,
            "binding": binding,
            "upper": upper,
            "offset": offset + len(page) if more else 0,
            "rows": copy.deepcopy(rows) if more else [],
            "known_ids": known_ids,
        }
    )
    params = [(k, v) for k, v in request.query_params.multi_items() if k not in ("$skiptoken", "$deltatoken")]
    params.append(("$skiptoken" if more else "$deltatoken", encoded))
    result["@odata.nextLink" if more else "@odata.deltaLink"] = (
        external_base(request) + local_path(request) + "?" + urlencode(params)
    )
    if options.select:
        result["value"] = [
            {k: v for k, v in r.items() if k in options.select or k in ("id", "@removed")}
            for r in result["value"]
        ]
    return result
