"""Route registry, request parsing, authentication and response rendering.

Handlers are small ``async def handler(c: Call)`` functions registered with :func:`route`; this
module owns everything the three Dropbox call styles share:

``rpc``       JSON body in, JSON body out
``upload``    argument in ``Dropbox-API-Arg`` (or ``?arg=``), bytes in the body, JSON out
``download``  argument in ``Dropbox-API-Arg``, result JSON in ``Dropbox-API-Result``, bytes out
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.auth import OAuthError, basic_credentials, bearer_token
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import (
    Args,
    BadInput,
    api_error,
    auth_error,
    decode_header_json,
    header_json,
    missing_scope,
    text_error,
)
from emulators.providers.dropbox.validation import validate_argument

ANON_ACCOUNT: dict[str, Any] = {
    "id": "",
    "person": "",
    "email": "",
    "display_name": "",
    "status": "active",
    "is_team": False,
    "groups": [],
    "roles": [],
    "home_ns": "",
}


@dataclass
class RouteDef:
    name: str
    fn: Callable[[m.Call], Any]
    style: str = "rpc"
    auth: tuple[str, ...] = ("user",)
    scope: str | None = None
    void_arg: bool = False


@dataclass
class Download:
    """A content-download result: JSON in ``Dropbox-API-Result``, bytes in the body."""

    result: Any
    body: bytes
    content_type: str = "application/octet-stream"
    headers: dict[str, str] = field(default_factory=dict)
    status: int = 200


@dataclass
class Raw:
    """Escape hatch: a ready-made response."""

    response: Response


ROUTES: dict[str, RouteDef] = {}


def route(
    name: str,
    *,
    style: str = "rpc",
    auth: str = "user",
    scope: str | None = None,
    void_arg: bool = False,
) -> Callable[[Callable[[m.Call], Any]], Callable[[m.Call], Any]]:
    def decorator(fn: Callable[[m.Call], Any]) -> Callable[[m.Call], Any]:
        ROUTES[name] = RouteDef(name, fn, style, tuple(p.strip() for p in auth.split(",")), scope, void_arg)
        return fn

    return decorator


# ---------------------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------------------
def parse_path_root(raw: str | None) -> tuple[str, str | None]:
    if not raw:
        return "home", None
    try:
        value = json.loads(raw)
    except ValueError:
        raise BadInput(f'Bad HTTP "Dropbox-API-Path-Root" header: "{raw}"') from None
    tag_name = value.get(".tag") if isinstance(value, dict) else None
    if tag_name == "home":
        return "home", None
    if tag_name == "root" and isinstance(value.get("root"), str):
        return "root", value["root"]
    if tag_name == "namespace_id" and isinstance(value.get("namespace_id"), str):
        return "namespace_id", value["namespace_id"]
    raise BadInput(f'Bad HTTP "Dropbox-API-Path-Root" header: "{raw}"')


def _path_root_error(kind: str, **fields: Any) -> EmulatorError:
    body = {"error_summary": f"{kind}/...", "error": {".tag": kind, **fields}}
    return EmulatorError(422, body)


def authenticate(ctx: ProviderContext, request: Request, rd: RouteDef) -> m.Actor:
    token = bearer_token(request)
    header = request.headers.get("authorization", "")
    basic = header.lower().startswith("basic ")
    if token is None and not basic:
        qa = request.query_params.get("authorization")
        if qa:
            scheme, _, value = qa.partition(" ")
            token = value.strip() if scheme.lower() == "bearer" else None
    if "noauth" in rd.auth:
        return m.Actor(account=ANON_ACCOUNT)
    if basic:
        if "app" in rd.auth:
            credentials = basic_credentials(request)
            if credentials is None:
                raise BadInput('Bad HTTP "Authorization" header: invalid Basic credentials')
            try:
                ctx.tokens.authenticate_client(*credentials, require_secret=True)
            except OAuthError:
                raise auth_error("invalid_access_token") from None
            return m.Actor(account=ANON_ACCOUNT, app_auth=True, app_id=credentials[0])
        raise auth_error("invalid_access_token")
    if token is None:
        if header:
            raise BadInput(f'Bad HTTP "Authorization" header: "{header}"')
        raise BadInput('Must provide HTTP header "Authorization" or URL parameter "authorization".')
    grant = ctx.tokens.get(token, "access")
    if grant is None or grant.revoked:
        raise auth_error("invalid_access_token")
    if grant.expires_at is not None and ctx.tokens.is_expired(token):
        raise auth_error("expired_access_token")
    if rd.auth == ("app",):
        raise auth_error("route_access_denied")
    scopes = list(grant.scopes)
    if rd.scope and scopes and rd.scope not in scopes:
        raise missing_scope(rd.scope)

    team_token = bool(grant.extra.get("team"))
    owner = m.account_by_person(ctx, grant.user_id) if grant.user_id else None
    if owner is None:
        raise auth_error("invalid_access_token")
    select_user = request.headers.get("dropbox-api-select-user")
    select_admin = request.headers.get("dropbox-api-select-admin")

    actor = m.Actor(account=owner, grant=grant, team_token=team_token, scopes=scopes)
    wants_team = "team" in rd.auth
    wants_user = "user" in rd.auth or "app" in rd.auth
    if team_token:
        if wants_team:
            actor.team_scope = True  # acts as the team (authorising admin) -- no member selected
        else:
            chosen_id = select_user or select_admin
            if not chosen_id:
                raise text_error(
                    400,
                    "This API function operates on a single Dropbox account, but the access token is for a "
                    "team. Use the Dropbox-API-Select-User or Dropbox-API-Select-Admin header to act as a "
                    "team member.",
                    rd.name,
                )
            member = m.account_by_member_id(ctx, chosen_id)
            if member is None or not member.get("is_team"):
                raise auth_error("invalid_select_admin" if select_admin else "invalid_select_user")
            if member["status"] == "suspended":
                raise auth_error("user_suspended")
            if select_admin and not member.get("roles"):
                raise auth_error("invalid_select_admin")
            actor = m.Actor(
                account=member, grant=grant, team_token=True, admin=bool(select_admin), scopes=scopes
            )
    elif wants_team and not wants_user:
        raise text_error(
            400,
            "This API function operates on a Dropbox Business team, but the access token is for a single user.",
            rd.name,
        )

    if actor.account.get("status") == "suspended" and not actor.team_scope:
        raise auth_error("user_suspended")
    if actor.account.get("disabled"):
        raise auth_error("invalid_access_token")
    # Path root (files-style routes only; harmless elsewhere).
    mode, value = parse_path_root(request.headers.get("dropbox-api-path-root"))
    acct = actor.account
    if mode == "home":
        actor.root_ns = acct["home_ns"]
    elif mode == "root":
        if value != acct["root_ns"]:
            raise _path_root_error(
                "invalid_root",
                invalid_root={
                    ".tag": "team" if acct.get("is_team") else "user",
                    "root_namespace_id": acct["root_ns"],
                    "home_namespace_id": acct["home_ns"],
                    **({"home_path": "/" + acct["display_name"]} if acct.get("is_team") else {}),
                },
            )
        actor.root_ns = acct["root_ns"]
    else:
        ns = ctx.store.collection(m.NAMESPACES).get(value)
        if ns is None or m.ns_role(actor, ns) is None:
            raise _path_root_error("no_permission")
        actor.root_ns = value  # type: ignore[assignment]
    actor.root_mode = mode
    return actor


# ---------------------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------------------
JSON_TYPES = ("application/json", "application/json; charset=utf-8", "text/plain; charset=dropbox-cors-hack")
OCTET_TYPES = ("application/octet-stream", "text/plain; charset=dropbox-cors-hack")


def _content_type_ok(request: Request, allowed: tuple[str, ...], route_name: str) -> None:
    """Dropbox rejects other content types with a precise 400; a missing header is fine."""
    raw = request.headers.get("content-type")
    if raw is None:
        return
    media = raw.split(";")[0].strip().lower()
    if media == allowed[0] or (media == "text/plain" and "dropbox-cors-hack" in raw.lower()):
        return
    options = ", ".join(f'"{a}"' for a in allowed)
    raise text_error(400, f'Bad HTTP "Content-Type" header: "{raw}".  Expecting one of {options}', route_name)


def parse_arg(request: Request, rd: RouteDef, body: bytes) -> tuple[Args, bytes]:
    """Decode the route argument according to the call style; returns (args, payload bytes)."""
    qarg = request.query_params.get("arg")
    if rd.style == "rpc":
        if body.strip() in (b"", b"null"):
            if qarg:
                try:
                    return Args(json.loads(qarg)), b""
                except (ValueError, RecursionError):
                    raise BadInput("request body: could not decode input as JSON") from None
            return Args(None), b""
        if request.method == "POST":
            _content_type_ok(request, JSON_TYPES, rd.name)
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise BadInput("request body: could not decode input as JSON") from None
        return Args(data), b""
    raw = request.headers.get("dropbox-api-arg") or qarg
    if raw is None:
        if rd.void_arg:
            return Args(None), body
        raise BadInput('Bad HTTP header "Dropbox-API-Arg": missing')
    try:
        data = decode_header_json(raw)
    except (ValueError, RecursionError):
        raise BadInput('HTTP header "Dropbox-API-Arg": could not decode input as JSON') from None
    if request.method == "POST":
        _content_type_ok(request, OCTET_TYPES, rd.name)
    return Args(data, 'HTTP header "Dropbox-API-Arg"'), body


# ---------------------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------------------
def strip_none(value: Any) -> Any:
    """Dropbox omits absent optional fields instead of sending ``null``."""
    if isinstance(value, dict):
        return {k: strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_none(v) for v in value]
    return value


def json_response(result: Any, request_id: str, status: int = 200) -> Response:
    payload = json.dumps(strip_none(result), ensure_ascii=False, separators=(",", ":")).encode()
    return Response(
        payload,
        status_code=status,
        media_type="application/json",
        headers={"X-Dropbox-Request-Id": request_id, **CORS_HEADERS},
    )


def download_response(dl: Download, request_id: str) -> Response:
    headers = {
        "Dropbox-API-Result": header_json(strip_none(dl.result)),
        "X-Dropbox-Request-Id": request_id,
        "Accept-Ranges": "bytes",
        **CORS_HEADERS,
        **dl.headers,
    }
    return Response(dl.body, status_code=dl.status, media_type=dl.content_type, headers=headers)


def parse_range(header: str | None, total: int) -> tuple[int, int] | None:
    """``bytes=a-b`` -> inclusive (start, end); ``None`` = no/ignored range; raises on unsatisfiable."""
    if not header or not header.lower().startswith("bytes="):
        return None
    spec = header.split("=", 1)[1].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            length = int(end_s)
            start, end = max(total - length, 0), total - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
    except ValueError:
        return None
    end = min(end, total - 1)
    if start >= total or start > end:
        raise EmulatorError(
            416,
            "Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{total}"},
            media_type="text/plain",
        )
    return start, end


def ranged(request: Request, data: bytes, headers: dict[str, str]) -> tuple[int, bytes, dict[str, str]]:
    total = len(data)
    rng = parse_range(request.headers.get("range"), total)
    headers = dict(headers)
    if rng is None:
        headers["Content-Length"] = str(total)
        return 200, data, headers
    start, end = rng
    headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    headers["Content-Length"] = str(end - start + 1)
    return 206, data[start : end + 1], headers


# ---------------------------------------------------------------------------------------
# The FastAPI endpoint
# ---------------------------------------------------------------------------------------
async def handle(ctx: ProviderContext, request: Request, route_path: str) -> Response:
    request_id = m.new_hex(ctx, "reqid", 32)
    name = route_path.strip("/")
    rd = ROUTES.get(name)
    if rd is None:
        return Response(
            f'Error in call to API function "{name}": route not found',
            status_code=404,
            media_type="text/plain",
            headers={"X-Dropbox-Request-Id": request_id},
        )
    try:
        body = await request.body()
        args, payload = parse_arg(request, rd, body)
        actor = authenticate(ctx, request, rd)
        validate_argument(name, args)
        c = m.Call(
            ctx=ctx, request=request, route=name, args=args, body=payload, actor=actor, request_id=request_id
        )
        result = rd.fn(c)
        if inspect.isawaitable(result):
            result = await result
        if c.touched:
            from emulators.providers.dropbox import webhooks

            await webhooks.notify(c)
    except BadInput as exc:
        raise _stamp(text_error(400, str(exc), name), request_id) from None
    except EmulatorError as exc:
        raise _stamp(exc, request_id) from None
    if isinstance(result, Raw):
        result.response.headers["X-Dropbox-Request-Id"] = request_id
        return result.response
    if isinstance(result, Download):
        return download_response(result, request_id)
    return json_response(result, request_id)


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Expose-Headers": "Dropbox-API-Result, X-Dropbox-Request-Id, Content-Range, Accept-Ranges",
}


def _stamp(exc: EmulatorError, request_id: str) -> EmulatorError:
    exc.headers = {**exc.headers, "X-Dropbox-Request-Id": request_id, **CORS_HEADERS}
    return exc


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.api_route("/2/{route_path:path}", methods=["POST", "GET"], include_in_schema=False)
    async def api(route_path: str, request: Request) -> Response:
        return await handle(ctx, request, route_path)

    @app.options("/2/{route_path:path}", include_in_schema=False)
    @app.options("/oauth2/token", include_in_schema=False)
    async def preflight(request: Request) -> Response:
        """CORS preflight for the browser SDK (``Content-Type: text/plain; charset=dropbox-cors-hack``)."""
        return Response(
            status_code=200,
            headers={
                **CORS_HEADERS,
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": request.headers.get(
                    "access-control-request-headers",
                    "Authorization, Content-Type, Dropbox-API-Arg, Dropbox-API-Select-User, Dropbox-API-Select-Admin, Dropbox-API-Path-Root",
                ),
                "Access-Control-Max-Age": "86400",
            },
        )


__all__ = ["ROUTES", "Download", "Raw", "api_error", "register", "route"]
