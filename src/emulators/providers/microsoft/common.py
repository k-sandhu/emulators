"""Shared Microsoft Graph helpers: principals, error envelope, stable ids, OData paging.

Every Graph module (directory, mail, calendar, drive, teams, meetings...) uses these so
auth, errors and paging behave identically across the API surface.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import Response

from emulators.core.auth import bearer_token
from emulators.core.clock import isoformat_z
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_guid
from emulators.core.odata import ODataError, QueryOptions, apply_query, parse_query_options, select_record
from emulators.core.provider import ProviderContext, external_base
from emulators.world.model import DirectChat, Meeting, Person, World

GRAPH_VERSIONS = ("v1.0", "beta")
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 999
REQUEST_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar("microsoft_request_scope", default=None)
BEARER_CHALLENGE = (
    'Bearer realm="", authorization_uri="https://login.microsoftonline.com/common/oauth2/authorize", '
    'client_id="00000003-0000-0000-c000-000000000000"'
)


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


def request_ids(scope: dict[str, Any] | None = None) -> tuple[str, str]:
    scope = scope if scope is not None else REQUEST_SCOPE.get()
    state = scope.setdefault("state", {}) if scope is not None else {}
    rid = state.setdefault("graph_request_id", str(uuid.uuid4()))
    headers = dict(scope.get("headers", [])) if scope is not None else {}
    return rid, headers.get(b"client-request-id", rid.encode()).decode("latin-1")


def error_headers(status: int, scope: dict[str, Any] | None = None) -> dict[str, str]:
    rid, client = request_ids(scope)
    headers = {"request-id": rid, "client-request-id": client}
    if status == 401:
        headers["WWW-Authenticate"] = BEARER_CHALLENGE
    return headers


def error_body(
    code: str, message: str, *, request_id: str | None = None, scope: dict[str, Any] | None = None
) -> dict[str, Any]:
    rid, client = request_ids(scope)
    rid = request_id or rid
    from datetime import UTC, datetime

    scope = scope if scope is not None else REQUEST_SCOPE.get()
    stamp = (scope or {}).get("state", {}).get("graph_now") or isoformat_z(datetime.now(UTC))
    return {
        "error": {
            "code": code,
            "message": message,
            "innerError": {
                "date": stamp,
                "request-id": rid,
                "client-request-id": client,
            },
        }
    }


def graph_error(status: int, code: str, message: str) -> EmulatorError:
    return EmulatorError(status, error_body(code, message), headers=error_headers(status))


def invalid_json() -> EmulatorError:
    return graph_error(400, "BadRequest", "Unable to read JSON request payload. Please ensure JSON is valid.")


def json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid_json()
    return value


async def json_body(request: Request, *, optional: bool = False, code: str = "BadRequest") -> dict[str, Any]:
    """Graph JSON operations require an object, including when JSON decoding fails."""
    if optional and not await request.body():
        return {}
    try:
        value = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise graph_error(
            400, code, "Unable to read JSON request payload. Please ensure JSON is valid."
        ) from None
    if not isinstance(value, dict):
        raise graph_error(400, code, "Unable to read JSON request payload. Please ensure JSON is valid.")
    return value


def preference_applied(request: Request, value: str) -> None:
    values = request.scope.setdefault("state", {}).setdefault("graph_preferences", [])
    if value not in values:
        values.append(value)


def page_size(request: Request, top: int | None, default: int, maximum: int) -> int:
    size = min(top if top is not None else default, maximum)
    match = re.search(
        r"(?:^|,)\s*odata.maxpagesize\s*=\s*(\d+)(?:\s*,|\s*$)", request.headers.get("prefer", ""), re.I
    )
    if match and int(match[1]) > 0:
        preferred = min(int(match[1]), maximum)
        size = min(top, preferred) if top is not None else preferred
        preference_applied(request, f"odata.maxpagesize={preferred}")
    return size


def count_response(request: Request, records: list[dict[str, Any]], *, directory: bool = False) -> Response:
    if directory and request.headers.get("ConsistencyLevel", "").lower() != "eventual":
        raise graph_error(
            400, "Request_UnsupportedQuery", "Advanced queries require ConsistencyLevel: eventual."
        )
    options = query_options(request)
    from emulators.providers.microsoft.query_validation import validate

    kind = next(
        (
            part
            for part in reversed(request.url.path.split("/"))
            if part in ("users", "groups", "messages", "events")
        ),
        "",
    )
    validate(options, kind)
    options.top, options.skip = None, 0
    try:
        total = apply_query(records, options).total
    except ODataError as exc:
        raise graph_error(400, exc.code, exc.message) from None
    return Response(str(total), media_type="text/plain")


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
    size = page_size(request, options.top, default_page_size, max_page_size)
    options.top = None
    options.skip = start
    from emulators.providers.microsoft.query_validation import validate

    validate(options, context)
    try:
        result = apply_query(
            records,
            options,
            search_fields=search_fields,
            select_always=(*select_always, "@odata.type", "@odata.etag"),
        )
    except ODataError as exc:
        raise graph_error(400, exc.code, exc.message) from None
    page = result.items[:size]
    body: dict[str, Any] = {"@odata.context": context_url(request, context)}
    if options.count:
        body["@odata.count"] = result.total
    if extra:
        body.update(extra)
    next_start = start + len(page)
    if page and next_start < result.total:
        drop = {"$skiptoken", "skiptoken", "$skip", "skip", "$top", "top"}
        params = [(k, v) for k, v in request.query_params.multi_items() if k not in drop]
        params.append(("$top", str(size)))
        params.append(("$skiptoken", _encode_skiptoken(next_start)))
        path = request.url.path.removeprefix(request.scope.get("root_path", ""))
        body["@odata.nextLink"] = f"{external_base(request)}{path}?{urlencode(params, safe='$')}"
    body["value"] = page
    return body


def single_response(request: Request, record: dict[str, Any], *, context: str) -> dict[str, Any]:
    """One entity with ``@odata.context`` and ``$select`` applied."""
    options = query_options(request)
    from emulators.providers.microsoft.query_validation import validate

    validate(options, context)
    item = select_record(record, options.select)
    return {"@odata.context": context_url(request, context), **item}


# -- cross-module identifiers ------------------------------------------------------------
# Calendar events (mail/calendar module) and onlineMeetings (meetings module) must agree
# on join URLs and ids, because consumers look meetings up by JoinWebUrl.


def meeting_thread_id(world: World, meeting_id: str) -> str:
    """Teams meeting chat thread: ``19:meeting_<base64ish>@thread.v2``."""
    token = base64.b64encode(stable_guid(world.seed, "ms-meeting-thread", meeting_id).encode()).decode()
    return f"19:meeting_{token.rstrip('=')[:44]}@thread.v2"


def join_web_url(world: World, meeting: Meeting) -> str:
    from urllib.parse import quote

    thread = quote(meeting_thread_id(world, meeting.id), safe="")
    organizer = user_id(world, meeting.organizer_id)
    context = quote(json.dumps({"Tid": tenant_id(world), "Oid": organizer}, separators=(",", ":")), safe="")
    return f"https://teams.microsoft.com/l/meetup-join/{thread}/0?context={context}"


def online_meeting_id(world: World, meeting_id: str, organizer_person_id: str) -> str:
    """Graph onlineMeeting ids are base64 of ``1*<organizerOid>*0**<threadId>``."""
    raw = f"1*{user_id(world, organizer_person_id)}*0**{meeting_thread_id(world, meeting_id)}"
    return base64.b64encode(raw.encode()).decode().rstrip("=")


def event_id(world: World, meeting_id: str, person_id: str) -> str:
    """Outlook event ids differ per mailbox (``AAMkA...``)."""
    digest = stable_guid(world.seed, "ms-event", meeting_id, person_id).replace("-", "")
    return "AAMkA" + base64.urlsafe_b64encode(bytes.fromhex(digest * 3)).decode().rstrip("=")


def channel_id(world: World, world_channel_id: str) -> str:
    return f"19:{stable_guid(world.seed, 'ms-channel', world_channel_id).replace('-', '')}@thread.tacv2"


def chat_id(world: World, chat: DirectChat) -> str:
    if len(chat.member_ids) == 2 and not chat.topic:
        a, b = sorted(user_id(world, m) for m in chat.member_ids)
        return f"19:{a}_{b}@unq.gbl.spaces"
    return f"19:{stable_guid(world.seed, 'ms-chat', chat.id).replace('-', '')}@thread.v2"


# -- change notification bus -------------------------------------------------------------
# Modules publish resource changes; the subscriptions module (if present) listens and
# delivers Graph change notifications to matching subscriptions.

ChangeListener = Callable[[ProviderContext, str, str, dict[str, Any]], Awaitable[None]]
_change_listeners: list[ChangeListener] = []


def on_change(listener: ChangeListener) -> ChangeListener:
    if listener not in _change_listeners:
        _change_listeners.append(listener)
    return listener


async def publish_change(ctx: ProviderContext, resource: str, change_type: str, data: dict[str, Any]) -> None:
    """``resource`` is the Graph path without version, e.g. ``users/{id}/messages/{mid}``;
    ``change_type`` is ``created``/``updated``/``deleted``; ``data`` is the resource
    (used for ``resourceData`` and rich notifications)."""
    for listener in list(_change_listeners):
        await listener(ctx, resource, change_type, data)
