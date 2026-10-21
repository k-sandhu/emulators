"""Dispatch JSON batch requests through the same Graph application."""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Request

from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common as c
from emulators.providers.microsoft.directory_support import local_path

CANONICAL_HEADERS = {
    "request-id": "request-id",
    "client-request-id": "client-request-id",
    "etag": "ETag",
    "www-authenticate": "WWW-Authenticate",
    "odata-version": "OData-Version",
}
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


async def execute(request: Request, ctx: ProviderContext) -> dict[str, Any]:
    c.principal(request, ctx)
    data = await c.json_body(request)
    try:
        # IDs and other envelope values are echoed later, so validate more than request bodies.
        json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError):
        raise c.graph_error(
            400, "BadRequest", "Batch JSON must contain finite numbers and valid Unicode."
        ) from None
    entries = data.get("requests")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 20:
        raise c.graph_error(400, "BadRequest", "A batch must contain between 1 and 20 requests.")
    if any(not isinstance(e, dict) for e in entries):
        raise c.invalid_json()
    for entry in entries:
        if (
            not isinstance(entry.get("id"), str)
            or not isinstance(entry.get("url"), str)
            or not isinstance(entry.get("method"), str)
            or not isinstance(entry.get("headers", {}), dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in entry.get("headers", {}).items())
            or not isinstance(entry.get("dependsOn", []), list)
            or not all(isinstance(d, str) for d in entry.get("dependsOn", []))
        ):
            raise c.invalid_json()
    ids = [e["id"].lower() for e in entries]
    if len(set(ids)) != len(ids) or not all(ids):
        raise c.graph_error(400, "BadRequest", "Batch request ids must be unique and nonempty.")
    bodies: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for name, value in entry.get("headers", {}).items():
            if not HEADER_NAME.fullmatch(name) or any(
                ord(char) > 255 or ord(char) == 127 or (ord(char) < 32 and char != "\t") for char in value
            ):
                raise c.graph_error(400, "BadRequest", "Invalid batch HTTP header name or value.")
        if "body" in entry and not any(k.lower() == "content-type" for k in entry.get("headers", {})):
            raise c.graph_error(400, "BadRequest", "A batch request body requires a Content-Type header.")
        bodies[entry["id"].lower()] = {}
        if "body" in entry:
            body = entry["body"]
            if isinstance(body, dict):
                bodies[entry["id"].lower()] = {"json": body}
            elif isinstance(body, str):
                try:
                    decoded = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True)
                except (binascii.Error, ValueError):
                    raise c.graph_error(400, "BadRequest", "Invalid base64-encoded batch body.") from None
                bodies[entry["id"].lower()] = {"content": decoded}
            else:
                raise c.graph_error(400, "BadRequest", "A batch body must be an object or base64 string.")
        url = entry.get("url", "")
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise c.graph_error(400, "BadRequest", "Invalid batch URL.") from None
        if (
            not url
            or parsed.netloc
            or parsed.scheme
            or "$batch" in url
            or any(part == ".." for part in parsed.path.split("/"))
            or any(
                d.lower() not in ids or d.lower() == entry["id"].lower() for d in entry.get("dependsOn", [])
            )
        ):
            raise c.graph_error(400, "BadRequest", "Invalid batch URL or dependency.")
    # Validate the entire dependency graph before a ready write can change state.
    pending = list(entries)
    ordered: list[dict[str, Any]] = []
    completed: set[str] = set()
    while pending:
        ready = [e for e in pending if all(d.lower() in completed for d in e.get("dependsOn", []))]
        if not ready:
            raise c.graph_error(400, "BadRequest", "Batch dependencies contain a cycle.")
        for entry in ready:
            ordered.append(entry)
            completed.add(entry["id"].lower())
            pending.remove(entry)
    pending = ordered
    results: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=request.app), base_url="https://graph.microsoft.com"
    ) as client:
        prepared: dict[str, httpx.Request] = {}
        for entry in entries:
            url = "/" + entry["url"].lstrip("/")
            if not url.startswith(("/v1.0/", "/beta/")):
                url = f"/{local_path(request).split('/')[1]}{url}"
            if not url.startswith(("/v1.0/", "/beta/")):
                raise c.graph_error(400, "BadRequest", "Batch URLs must address Graph resources.")
            headers = {k.lower(): v for k, v in entry.get("headers", {}).items()}
            headers["authorization"] = request.headers.get("authorization", "")
            try:
                prepared[entry["id"].lower()] = client.build_request(
                    entry["method"],
                    url,
                    headers=httpx.Headers(headers, encoding="latin-1"),
                    **bodies[entry["id"].lower()],
                )
            except (ValueError, httpx.InvalidURL):
                raise c.graph_error(400, "BadRequest", "Invalid batch URL or body encoding.") from None
        while pending:
            ready = [e for e in pending if all(d.lower() in results for d in e.get("dependsOn", []))]
            if not ready:
                raise c.graph_error(400, "BadRequest", "Batch dependencies contain a cycle.")
            for entry in ready:
                identifier = entry["id"].lower()
                if any(results[d.lower()]["status"] >= 400 for d in entry.get("dependsOn", [])):
                    results[identifier] = {
                        "id": entry["id"],
                        "status": 424,
                        "headers": {},
                        "body": {"error": {"code": "FailedDependency", "message": "A dependency failed."}},
                    }
                else:
                    response = await client.send(prepared[identifier])
                    result: dict[str, Any] = {
                        "id": entry["id"],
                        "status": response.status_code,
                        "headers": {
                            CANONICAL_HEADERS.get(
                                key, "-".join(part.capitalize() for part in key.split("-"))
                            ): value
                            for key, value in response.headers.items()
                            if key.lower() not in ("content-length", "transfer-encoding")
                        },
                    }
                    if response.content:
                        try:
                            result["body"] = response.json()
                        except ValueError:
                            content_type = response.headers.get("content-type", "")
                            result["body"] = (
                                response.text
                                if content_type.startswith("text/")
                                else base64.b64encode(response.content).decode()
                            )
                    results[identifier] = result
                pending.remove(entry)
    return {"responses": [results[i] for i in ids]}
