"""Google transport conventions shared by the discovery APIs."""

from __future__ import annotations

import json
import re
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import httpx
from fastapi import FastAPI, Request, Response
from starlette.responses import JSONResponse

from emulators.core.clock import parse_instant
from emulators.core.errors import EmulatorError
from emulators.providers.google.api_schema import ROUTES, SCHEMAS
from emulators.providers.google.common import google_error
from emulators.providers.google.drive_fields import parse_fields, project

PATTERNS = [
    (method, re.compile(re.sub(r"\{\w+\}", r"[^/]+", path) + "$"), request, response)
    for method, path, request, response in sorted(
        ROUTES, key=lambda r: len(re.sub(r"\{[^}]+\}", "", r[1])), reverse=True
    )
]


def route_schema(method: str, path: str) -> tuple[Any, ...] | None:
    path = path.removeprefix("/upload")
    if path.endswith("/messages/insert"):
        path = path.removesuffix("/insert")
    return next((r for r in PATTERNS if r[0] == method and r[1].fullmatch(path)), None)


def validate_value(value: Any, schema: dict[str, Any], path: str = "") -> None:
    """Validate wire types before a resource handler can mutate state."""
    if value is None:
        return
    if "$ref" in schema:
        schema = SCHEMAS[schema["$ref"]]
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
    }.get(kind, True)
    if schema.get("format") in ("int64", "uint64"):
        valid = isinstance(value, (str, int)) and not isinstance(value, bool)
    if not valid:
        raise google_error(400, f"Invalid value at '{path}' ({kind})", reason="invalidArgument")
    if schema.get("format") in ("date-time", "google-datetime"):
        try:
            parse_instant(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise google_error(400, f"Invalid timestamp at '{path}'", reason="invalidArgument") from exc
    if kind == "object":
        for key, item in value.items():
            child = schema.get("properties", {}).get(key, schema.get("additionalProperties"))
            if isinstance(child, dict):
                validate_value(item, child, f"{path}.{key}".strip("."))
    elif kind == "array":
        for item in value:
            validate_value(item, schema.get("items", {}), path + "[]")


def field_tree(schema: dict[str, Any], seen: frozenset[str] = frozenset()) -> dict[str, Any]:
    ref = schema.get("$ref")
    if ref:
        if ref in seen:
            return {"*": None}
        seen = seen | {ref}
        schema = SCHEMAS[ref]
    if schema.get("type") == "array":
        return field_tree(schema.get("items", {}), seen)
    if schema.get("type") != "object":
        return {}
    result = {
        k: field_tree(v, seen) if v.get("type") in ("array", "object") or "$ref" in v else None
        for k, v in schema.get("properties", {}).items()
    }
    if "additionalProperties" in schema:
        result["*"] = None
    return result


class GoogleWireMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        path = scope["path"].removeprefix(scope.get("root_path", ""))
        override = request.headers.get("x-http-method-override", "").upper()
        body: bytes | None = None
        if scope["method"] == "POST" and override in ("GET", "PATCH", "PUT", "DELETE"):
            scope = dict(scope, method=override)
            if override == "GET":
                body = await request.body()
                scope["query_string"] = b"&".join(filter(None, (scope.get("query_string", b""), body)))
                body = b""
                scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"content-length"]
        route = route_schema(scope["method"], path)
        query_params = parse_qs(scope.get("query_string", b"").decode())
        fields = query_params.get("fields", [None])[-1]
        tree = None
        try:
            content_type = request.headers.get("content-type", "").split(";", 1)[0]
            if (
                route
                and route[2]
                and not (
                    path.startswith("/upload/")
                    and (query_params.get("uploadType") == ["media"] or "upload_id" in query_params)
                )
                and (not content_type or content_type == "application/json" or content_type.endswith("+json"))
            ):
                body = await request.body() if body is None else body
                value = {} if not body and path.startswith("/upload/") else json.loads(body)
                if not isinstance(value, dict):
                    raise google_error(400, "Invalid JSON payload received.", reason="invalidArgument")
                validate_value(value, SCHEMAS[route[2]])
            if route and fields:
                from emulators.providers.google.drive_fields import validate

                tree = parse_fields(fields)
                if route[3]:
                    validate(tree, field_tree(SCHEMAS[route[3]]))
        except (ValueError, EmulatorError) as exc:
            error = (
                exc if isinstance(exc, EmulatorError) else google_error(400, "Invalid JSON payload received.")
            )
            if path.startswith("/v1/documents"):
                error.body.get("error", {}).pop("errors", None)
            await JSONResponse(error.body, status_code=error.status)(scope, receive, send)
            return
        if body is not None:
            consumed = False

            async def replay() -> dict[str, Any]:
                nonlocal consumed
                if consumed:
                    return await receive()
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}

            receiver = replay
        else:
            receiver = receive
        # Only selected JSON and Docs errors need buffering; media remains streaming.
        if tree is None and not path.startswith("/v1/documents"):
            await self.app(scope, receiver, send)
            return
        messages: list[dict[str, Any]] = []

        async def capture(message: dict[str, Any]) -> None:
            messages.append(message)

        await self.app(scope, receiver, capture)
        start = messages[0]
        content = b"".join(m.get("body", b"") for m in messages[1:])
        if content and any(k == b"content-type" and b"json" in v for k, v in start["headers"]):
            value = json.loads(content)
            if start["status"] < 300 and tree is not None:
                value = project(value, tree)
            if path.startswith("/v1/documents") and start["status"] >= 400:
                value.get("error", {}).pop("errors", None)
                if start["status"] == 404:
                    value["error"]["message"] = "Requested entity was not found."
                elif start["status"] == 403:
                    value["error"]["message"] = "The caller does not have permission"
            content = json.dumps(value).encode()
            start["headers"] = [(k, v) for k, v in start["headers"] if k != b"content-length"]
            start["headers"].append((b"content-length", str(len(content)).encode()))
        await send(start)
        await send({"type": "http.response.body", "body": content})


def register_batch(app: FastAPI) -> None:
    @app.post("/batch")
    @app.post("/batch/drive/v3")
    @app.post("/batch/calendar/v3")
    @app.post("/batch/admin/directory_v1")
    @app.post("/batch/gmail/v1")
    async def batch(request: Request) -> Response:
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + request.headers.get("content-type", "").encode()
            + b"\r\n\r\n"
            + await request.body()
        )
        if message.get_content_type() != "multipart/mixed" or not message.is_multipart():
            raise google_error(400, "Invalid multipart request", reason="badRequest")
        parts = list(message.iter_parts())
        if not 1 <= len(parts) <= 100:
            raise google_error(400, "Invalid number of batch requests", reason="badRequest")
        boundary = "batch_" + request.app.state.ctx.ids.hex(24)
        output: list[bytes] = []
        root = request.scope.get("root_path", "")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, root_path=root)) as client:
            for part in parts:
                if part.get_content_type() != "application/http":
                    raise google_error(400, "Expected application/http", reason="badRequest")
                raw = part.get_payload(decode=True) or b""
                try:
                    line, rest = raw.split(b"\n", 1)
                    method, target, _ = line.decode().strip().split(" ", 2)
                    inner = BytesParser(policy=policy.default).parsebytes(rest)
                    parsed = urlsplit(target)
                    if parsed.scheme or not target.startswith("/") or parsed.path.startswith("/batch"):
                        raise ValueError
                except ValueError as exc:
                    raise google_error(400, "Invalid batch request", reason="badRequest") from exc
                headers = {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower()
                    not in (
                        "content-type",
                        "content-length",
                        "host",
                        "x-http-method-override",
                        "accept-encoding",
                    )
                }
                headers.update({k.lower(): str(v) for k, v in inner.items()})
                inner_query = parse_qsl(parsed.query, keep_blank_values=True)
                overridden = {key for key, _ in inner_query}
                query = [
                    (k, v) for k, v in request.query_params.multi_items() if k not in overridden
                ] + inner_query
                target = parsed.path + ("?" + urlencode(query) if query else "")
                response = await client.request(
                    method,
                    f"{request.url.scheme}://{request.url.netloc}{root}{target}",
                    headers=headers,
                    content=inner.get_payload(decode=True) or b"",
                )
                content_id = str(part.get("Content-ID", "")).strip("<>")
                head = f"--{boundary}\r\nContent-Type: application/http\r\n"
                if content_id:
                    head += f"Content-ID: <response-{content_id}>\r\n"
                head += f"\r\nHTTP/1.1 {response.status_code} {HTTPStatus(response.status_code).phrase}\r\n"
                head += "".join(f"{k}: {v}\r\n" for k, v in response.headers.items()) + "\r\n"
                output.append(head.encode() + response.content + b"\r\n")
        return Response(
            b"".join(output) + f"--{boundary}--\r\n".encode(),
            media_type=f"multipart/mixed; boundary={boundary}",
        )
