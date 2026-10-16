"""Dispatch JSON batch requests through the same Graph application."""

from __future__ import annotations

import base64
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


async def execute(request: Request, ctx: ProviderContext) -> dict[str, Any]:
    c.principal(request, ctx)
    data = await c.json_body(request)
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
    ids = [str(e.get("id", "")) for e in entries]
    if len(set(ids)) != len(ids) or not all(ids):
        raise c.graph_error(400, "BadRequest", "Batch request ids must be unique and nonempty.")
    for entry in entries:
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
            or any(str(d) not in ids or str(d) == str(entry["id"]) for d in entry.get("dependsOn", []))
        ):
            raise c.graph_error(400, "BadRequest", "Invalid batch URL or dependency.")
    pending = list(entries)
    results: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=request.app), base_url="https://graph.microsoft.com"
    ) as client:
        while pending:
            ready = [e for e in pending if all(str(d) in results for d in e.get("dependsOn", []))]
            if not ready:
                raise c.graph_error(400, "BadRequest", "Batch dependencies contain a cycle.")
            for entry in ready:
                identifier = str(entry["id"])
                if any(results[str(d)]["status"] >= 400 for d in entry.get("dependsOn", [])):
                    results[identifier] = {
                        "id": identifier,
                        "status": 424,
                        "headers": {},
                        "body": {"error": {"code": "FailedDependency", "message": "A dependency failed."}},
                    }
                else:
                    url = "/" + entry["url"].lstrip("/")
                    if not url.startswith(("/v1.0/", "/beta/")):
                        url = f"/{local_path(request).split('/')[1]}{url}"
                    if not url.startswith(("/v1.0/", "/beta/")):
                        raise c.graph_error(400, "BadRequest", "Batch URLs must address Graph resources.")
                    headers = {
                        "authorization": request.headers.get("authorization", ""),
                        **{k.lower(): v for k, v in entry.get("headers", {}).items()},
                    }
                    headers["authorization"] = request.headers.get("authorization", "")
                    response = await client.request(
                        entry.get("method", "GET"),
                        url,
                        headers=headers,
                        **({"json": entry["body"]} if "body" in entry else {}),
                    )
                    result: dict[str, Any] = {
                        "id": identifier,
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
