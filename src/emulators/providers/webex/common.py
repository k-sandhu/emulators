"""Webex wire conventions and resource-specific authorization."""

from __future__ import annotations

import base64
import json
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.responses import JSONResponse

from emulators.core.auth import Grant, bearer_token
from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.errors import EmulatorError
from emulators.core.ids import b64url, stable_guid
from emulators.core.provider import ProviderContext, external_base

SITE = "halcyonadvisory.webex.com"
TRACKING: ContextVar[str] = ContextVar("webex_tracking", default="")
PERSONAL_SCOPES = [
    "spark:all",
    "meeting:schedules_read",
    "meeting:schedules_write",
    "meeting:recordings_read",
    "meeting:recordings_write",
    "meeting:transcripts_read",
    "meeting:participants_read",
]
SERVICE_SCOPES = [
    "spark-admin:people_read",
    "spark-admin:organizations_read",
    "spark-admin:roles_read",
    "spark-admin:licenses_read",
    "meeting:admin_schedule_read",
    "meeting:admin_schedule_write",
    "meeting:admin_recordings_read",
    "meeting:admin_recordings_write",
    "meeting:admin_transcripts_read",
    "meeting:admin_participants_read",
]


def opaque(kind: str, key: str) -> str:
    return b64url(f"ciscospark://us/{kind}/{stable_guid('webex', kind, key)}".encode())


def stamp(value: datetime) -> str:
    return isoformat_z(value, timespec="milliseconds")


def error_body(status: int, message: str) -> dict[str, Any]:
    if status == 404:
        message = "The requested resource could not be found."
    return {
        "message": message,
        "errors": [{"description": message}],
        "trackingId": TRACKING.get() or f"ROUTER_EMULATOR_{uuid.uuid4().hex}",
    }


def fail(status: int, message: str = "The requested resource could not be found.") -> Any:
    raise EmulatorError(status, error_body(status, message))


class TrackingMiddleware:
    """Use one tracking identifier in the header and error body, including core errors."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        tracking = f"ROUTER_EMULATOR_{uuid.uuid4().hex}"
        context = TRACKING.set(tracking)

        async def tracked(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"trackingid"
                ] + [(b"trackingid", tracking.encode())]
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        finally:
            TRACKING.reset(context)


def person(ctx: ProviderContext, identifier: str | None) -> dict[str, Any] | None:
    if not isinstance(identifier, str) or not identifier:
        return None
    return next(
        (
            p
            for p in ctx.store.collection("people")
            if identifier == p["id"]
            or identifier == p.get("_worldId")
            or identifier.lower() in [email.lower() for email in p["emails"]]
        ),
        None,
    )


def authenticate(request: Request, ctx: ProviderContext) -> Grant:
    grant = ctx.tokens.resolve(bearer_token(request))
    if grant is None:
        fail(401, "The request requires a valid access token set in the Authorization request header.")
    if not grant.app_only:
        user = person(ctx, grant.user_id)
        if not user or not user.get("_loginEnabled", True):
            fail(401, "The access token does not identify an active user.")
    return grant


def subject(ctx: ProviderContext, grant: Grant) -> dict[str, Any]:
    if grant.app_only:
        fail(403, "This operation requires a user access token.")
    return person(ctx, grant.user_id) or fail(401, "Unknown user.")


def is_admin(ctx: ProviderContext, grant: Grant, *scopes: str) -> bool:
    user = person(ctx, grant.user_id)
    return bool((grant.app_only or (user and user.get("_admin"))) and grant.has_scope(*scopes))


def require_scope(grant: Grant, *scopes: str) -> None:
    if not grant.has_scope(*scopes):
        fail(403, "The access token does not have the required scopes.")


def admin_scope(area: str, write: bool = False) -> str:
    return f"meeting:admin_{'schedule' if area == 'schedules' else area}_{'write' if write else 'read'}"


def meeting_access(
    ctx: ProviderContext, grant: Grant, meeting: dict[str, Any], area: str, *, write: bool = False
) -> bool:
    elevated = admin_scope(area, write)
    if is_admin(ctx, grant, elevated, *([] if write else ["spark-compliance:meetings_read"])):
        return True
    if grant.app_only:
        return False
    user = subject(ctx, grant)
    if user["type"] == "bot" or not grant.has_scope(f"meeting:{area}_{'write' if write else 'read'}"):
        return False
    if area in ("recordings", "transcripts", "participants") or write:
        return user["id"] == meeting["hostUserId"]
    return user["id"] in meeting.get("_people", [])


def get_meeting(
    ctx: ProviderContext, meeting_id: str, grant: Grant, area: str = "schedules", *, write: bool = False
) -> dict[str, Any]:
    meeting = ctx.store.collection("meetings").get(meeting_id)
    if meeting is None or not meeting_access(ctx, grant, meeting, area, write=write):
        fail(404)
    return meeting


def public(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if not k.startswith("_")}


def time_wire(request: Request, record: dict[str, Any]) -> dict[str, Any]:
    """Meeting APIs use the timezone header for timestamps, independently of scheduling timezone."""
    try:
        zone = ZoneInfo(request.headers.get("timezone", "UTC"))
    except (ValueError, OSError, ZoneInfoNotFoundError):
        return fail(400, "Invalid timezone header.")
    fields = {
        "start",
        "end",
        "createTime",
        "timeRecorded",
        "startTime",
        "joinedTime",
        "leftTime",
        "expiration",
    }

    def convert(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: convert(v, k) for k, v in public(value).items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        if key in fields and isinstance(value, str):
            return (
                parse_instant(value)
                .astimezone(zone)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        return value

    return convert(record)


def bot_message_visible(user: dict[str, Any], message: dict[str, Any]) -> bool:
    return (
        user["type"] != "bot"
        or message.get("roomType") == "direct"
        or message.get("personId") == user["id"]
        or user["id"] in message.get("mentionedPeople", [])
    )


def integer(raw: str, name: str, *, minimum: int = 1, maximum: int = 1000) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        fail(400, f"{name} must be an integer.")
    if not minimum <= value <= maximum:
        fail(400, f"{name} must be between {minimum} and {maximum}.")
    return value


def instant(raw: str, name: str) -> datetime:
    try:
        if not isinstance(raw, str):
            raise ValueError
        return parse_instant(raw)
    except (TypeError, ValueError, OverflowError):
        return fail(400, f"{name} must be an ISO 8601 timestamp.")


def date_range(
    request: Request,
    ctx: ProviderContext,
    *,
    days: int | None = None,
    defaults: bool = False,
    future: bool = False,
) -> tuple[datetime | None, datetime | None]:
    query = request.query_params
    end = instant(query["to"], "to") if "to" in query else None
    start = instant(query["from"], "from") if "from" in query else None
    if defaults:
        try:
            if start is None:
                start = (
                    end - timedelta(days=7) if end else ctx.clock.now() - timedelta(days=0 if future else 7)
                )
            if end is None:
                end = start + timedelta(days=7)
        except OverflowError:
            fail(400, "Invalid time range.")
    if start and end and (start > end or (days and end - start > timedelta(days=days))):
        fail(400, f"Invalid time range; from must precede to within {days or 'the allowed'} days.")
    return start, end


def in_range(value: str, bounds: tuple[datetime | None, datetime | None]) -> bool:
    start, end = bounds
    when = parse_instant(value)
    return (start is None or when >= start) and (end is None or when < end)


def parent_matches(row: dict[str, Any], key: str) -> bool:
    return key in (row.get("meetingId"), row.get("scheduledMeetingId"), row.get("meetingSeriesId"))


def page(
    request: Request, rows: list[dict[str, Any]], *, default: int = 100, maximum: int = 1000
) -> JSONResponse:
    limit = integer(request.query_params.get("max", str(default)), "max", maximum=maximum)
    cursor = request.query_params.get("cursor")
    offset = 0
    if cursor:
        try:
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            if not raw.isdigit():
                raise ValueError
            offset = int(raw)
        except (ValueError, UnicodeError):
            fail(400, "Invalid pagination cursor.")
        if offset < 0:
            fail(400, "Invalid pagination cursor.")
    headers = {}
    if offset + limit < len(rows):
        params = dict(request.query_params)
        params.update(max=str(limit), cursor=b64url(str(offset + limit).encode()))
        path = request.url.path.removeprefix(request.scope.get("root_path", ""))
        url = external_base(request) + path + "?" + urlencode(params)
        headers["Link"] = f'<{url}>; rel="next"'
    return JSONResponse({"items": [public(r) for r in rows[offset : offset + limit]]}, headers=headers)


async def body(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json" and not content_type.endswith("+json"):
        fail(415, "The request requires a JSON content type.")
    try:
        value = await request.json()
        # JSON permits escaped surrogate code points, but Webex strings and our wire are UTF-8.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        return fail(400, "The request body must contain valid JSON.")
    if not isinstance(value, dict):
        fail(400, "The request body must be a JSON object.")
    return value


def strings(payload: dict[str, Any], *keys: str, maximum: int | None = None) -> None:
    for key in keys:
        if key in payload and (
            not isinstance(payload[key], str) or (maximum is not None and len(payload[key]) > maximum)
        ):
            fail(400, f"{key} must be a string" + (f" of at most {maximum} characters." if maximum else "."))


def required(payload: dict[str, Any], *keys: str) -> None:
    for key in keys:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            fail(400, f"{key} is required and must be a nonempty string.")


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
