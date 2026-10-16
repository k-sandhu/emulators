"""Shared Microsoft Graph helpers: principals, error envelope, stable ids, OData paging.

Every Graph module (directory, mail, calendar, drive, teams, meetings...) uses these so
auth, errors and paging behave identically across the API surface.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from fastapi import Request

from emulators.core.auth import bearer_token
from emulators.core.clock import isoformat_z
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_guid
from emulators.core.odata import ODataError, QueryOptions, apply_query, parse_query_options
from emulators.core.provider import ProviderContext, external_base
from emulators.world.model import Person, World

GRAPH_VERSIONS = ("v1.0", "beta")
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 999


# -- ids -------------------------------------------------------------------------------


def tenant_id(world: World) -> str:
    return world.org.microsoft_tenant_id


def user_id(world: World, person_id: str) -> str:
    """Directory object id (GUID) for a world person. Stable across runs and modules."""
    return stable_guid(world.seed, "ms-user", person_id)


def group_id(world: World, world_group_id: str) -> str:
    return stable_guid(world.seed, "ms-group", world_group_id)


def object_id(world: World, *parts: object) -> str:
    return stable_guid(world.seed, "ms", *parts)


def upn(world: World, person: Person) -> str:
    """Members use the verified domain; client contacts are B2B guests (``#EXT#``)."""
    if not person.is_external:
        return person.email
    return f"{person.email.replace('@', '_')}#EXT#@{world.org.slug}.onmicrosoft.com"


def identity(world: World, person: Person) -> dict[str, Any]:
    """``identitySet.user`` shape used by driveItems, messages, events, chat messages."""
    return {
        "user": {"id": user_id(world, person.id), "displayName": person.display_name, "email": person.email}
    }


def email_address(person: Person) -> dict[str, Any]:
    """``recipient`` shape used by mail and calendar."""
    return {"emailAddress": {"name": person.display_name, "address": person.email}}


# -- errors ----------------------------------------------------------------------------


def error_body(code: str, message: str, *, request_id: str | None = None) -> dict[str, Any]:
    rid = request_id or str(uuid.uuid4())
    from datetime import UTC, datetime

    return {
        "error": {
            "code": code,
            "message": message,
            "innerError": {
                "date": isoformat_z(datetime.now(UTC)),
                "request-id": rid,
                "client-request-id": rid,
            },
        }
    }


def graph_error(status: int, code: str, message: str) -> EmulatorError:
    rid = str(uuid.uuid4())
    return EmulatorError(status, error_body(code, message, request_id=rid), headers={"request-id": rid})


def not_found(what: str = "resource") -> EmulatorError:
    return graph_error(
        404,
        "Request_ResourceNotFound",
        f"Resource '{what}' does not exist or one of its queried reference-property objects are not present.",
    )


def item_not_found() -> EmulatorError:
    return graph_error(404, "itemNotFound", "The resource could not be found.")


def access_denied(message: str = "Access denied") -> EmulatorError:
    return graph_error(403, "accessDenied", message)


# -- principals ------------------------------------------------------------------------


@dataclass
class Principal:
    """Who is calling: a delegated user or an application (client credentials)."""

    user_id: str | None  # directory object id (GUID) for delegated calls
    person_id: str | None  # world person id for delegated calls
    app_only: bool
    client_id: str
    scopes: list[str] = field(default_factory=list)

    @property
    def delegated(self) -> bool:
        return not self.app_only


def principal(request: Request, ctx: ProviderContext) -> Principal:
    """Resolve the bearer token or raise Graph's 401."""
    token = bearer_token(request)
    if not token:
        raise graph_error(401, "InvalidAuthenticationToken", "Access token is empty.")
    grant = ctx.tokens.resolve(token)
    if grant is None:
        if ctx.tokens.is_expired(token):
            raise graph_error(
                401, "InvalidAuthenticationToken", "Lifetime validation failed, the token is expired."
            )
        raise graph_error(401, "InvalidAuthenticationToken", "IDX14100: JWT is not well formed.")
    person_id = grant.user_id
    if person_id and "@" in person_id:
        person = ctx.world.person_by_email(person_id)
        person_id = person.id if person else person_id
    uid = grant.extra.get("oid") or (user_id(ctx.world, person_id) if person_id else None)
    return Principal(uid, person_id, grant.app_only or person_id is None, grant.client_id, list(grant.scopes))


def me_user_id(p: Principal) -> str:
    if p.app_only or not p.user_id:
        raise graph_error(400, "BadRequest", "/me request is only valid with delegated authentication flow.")
    return p.user_id


# -- OData collections -----------------------------------------------------------------


def query_options(request: Request) -> QueryOptions:
    try:
        return parse_query_options(dict(request.query_params))
    except ODataError as exc:
        raise graph_error(400, exc.code, exc.message) from None


def graph_base(request: Request) -> str:
    """``https://graph.microsoft.com`` (or the prefix/port base) — no version segment."""
    return external_base(request)


def version_of(request: Request) -> str:
    path = request.url.path
    for version in GRAPH_VERSIONS:
        if f"/{version}/" in path or path.endswith(f"/{version}"):
            return version
    return "v1.0"


def context_url(request: Request, fragment: str) -> str:
    return f"{graph_base(request)}/{version_of(request)}/$metadata#{fragment}"


def _encode_skiptoken(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode().rstrip("=")


def _decode_skiptoken(token: str | None) -> int:
    if not token:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))["o"])
    except (ValueError, KeyError, TypeError):
        raise graph_error(400, "BadRequest", "Invalid $skiptoken value.") from None


def collection_response(
    request: Request,
    records: list[dict[str, Any]],
    *,
    context: str,
    search_fields: tuple[str, ...] = ("displayName",),
    default_page_size: int = DEFAULT_PAGE_SIZE,
    max_page_size: int = MAX_PAGE_SIZE,
    select_always: tuple[str, ...] = ("id",),
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply ``$filter/$search/$orderby/$select/$top/$skip/$count`` and page with
    ``$skiptoken`` like Graph: the next link repeats the original query plus a token."""
    options = query_options(request)
    token = request.query_params.get("$skiptoken") or request.query_params.get("skiptoken")
    start = _decode_skiptoken(token) if token else (options.skip or 0)
    page_size = min(options.top or default_page_size, max_page_size)
    options.top = None
    options.skip = start
    result = apply_query(records, options, search_fields=search_fields, select_always=select_always)
    page = result.items[:page_size]
    body: dict[str, Any] = {"@odata.context": context_url(request, context)}
    if options.count:
        body["@odata.count"] = result.total
    if extra:
        body.update(extra)
    next_start = start + len(page)
    if page and next_start < result.total:
        drop = {"$skiptoken", "skiptoken", "$skip", "skip", "$top", "top"}
        params = [(k, v) for k, v in request.query_params.multi_items() if k not in drop]
        params.append(("$top", str(page_size)))
        params.append(("$skiptoken", _encode_skiptoken(next_start)))
        body["@odata.nextLink"] = f"{external_base(request)}{request.url.path}?{urlencode(params)}"
    body["value"] = page
    return body


def single_response(request: Request, record: dict[str, Any], *, context: str) -> dict[str, Any]:
    """One entity with ``@odata.context`` and ``$select`` applied."""
    options = query_options(request)
    item = dict(record)
    if options.select:
        keep = set(options.select) | {"id", "@odata.type", "@odata.etag"}
        item = {k: v for k, v in item.items() if k in keep}
    return {"@odata.context": context_url(request, context), **item}
