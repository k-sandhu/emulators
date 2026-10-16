"""Recording downloads and the small web pages Zoom serves next to the API.

* ``GET {web}/rec/download/<token>`` -- needs ``Authorization: Bearer <oauth or download token>`` or
  ``?access_token=``; answers ``302`` to a pre-signed ``https://ssrweb.zoom.us/file/...`` URL.
* ``GET ssrweb/file/<file id>/<name>`` -- streams the bytes (``Content-Length``, ``Range``).
* ``/rec/play``, ``/rec/share``, ``/j/<id>``, ``/s/<id>``, ``/w/<id>``, ``/p/v2/<id>`` -- minimal pages.
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from emulators.core.auth import bearer_token
from emulators.core.errors import EmulatorError
from emulators.core.ids import b64url
from emulators.core.provider import ProviderContext
from emulators.core.webhooks import hmac_sha256_hex
from emulators.providers.zoom import lookup
from emulators.providers.zoom.common import (
    E_INVALID_TOKEN,
    SSR_HOST,
    Need,
    externalize,
    principal_from_grant,
    recording_missing,
    zoom_error,
)
from emulators.providers.zoom.shapes import share_token
from emulators.world.documents import make_png

READ_FILES = Need("recording:read", "cloud_recording:read:list_recording_files")
SIGNED_URL_TTL_S = 3600
KEY_PAIR_ID = "APKAJZOOMEMULATOR"
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def unauthorized(request: Request) -> EmulatorError:
    """Zoom answers unauthenticated downloads with 401: HTML for browsers, JSON for API clients."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "json" not in accept:
        body = (
            "<html><head><title>401 Unauthorized</title></head><body><h1>401 Unauthorized</h1></body></html>"
        )
        return EmulatorError(401, body, media_type="text/html")
    return zoom_error(401, E_INVALID_TOKEN, "Invalid access token.")


def register(app: FastAPI, ctx: ProviderContext) -> None:
    secret = f"zoom-ssr/{ctx.world.seed}"

    def find_file(predicate: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for rec in ctx.store.collection("recordings"):
            for entry in rec["recording_files"]:
                if predicate(entry):
                    return rec, entry
        return None

    def signature(file_id: str, expires: int) -> str:
        return b64url(bytes.fromhex(hmac_sha256_hex(secret, f"{file_id}:{expires}")))

    def signed_location(request: Request, entry: dict[str, Any]) -> str:
        expires = ctx.clock.timestamp() + SIGNED_URL_TTL_S
        name = quote(entry["_filename"])
        query = (
            f"response-content-type={quote(entry['_content_type'], safe='')}"
            f"&Expires={expires}&Signature={signature(entry['id'], expires)}&Key-Pair-Id={KEY_PAIR_ID}"
        )
        return str(externalize(request, ctx, f"https://{SSR_HOST}/file/{entry['id']}/{name}?{query}"))

    def authorize_download(request: Request, rec: dict[str, Any]) -> None:
        token = bearer_token(request) or request.query_params.get("access_token")
        if not token:
            raise unauthorized(request)
        download = ctx.tokens.resolve(token, "download")
        if download is not None:
            if download.extra.get("uuid") != rec["id"]:
                raise unauthorized(request)
            return
        grant = ctx.tokens.resolve(token)
        if grant is None:
            raise unauthorized(request)
        principal = principal_from_grant(ctx, grant)
        admin = principal.authorize(READ_FILES)
        lookup.ensure_recording_access(ctx, principal, admin, rec)

    @app.get("/rec/download/{token:path}")
    @app.head("/rec/download/{token:path}")
    async def download(request: Request, token: str) -> Response:
        found = find_file(lambda f: f["_token"] == token)
        if found is None:
            raise recording_missing()
        rec, entry = found
        authorize_download(request, rec)
        if rec["trashed"] or entry.get("_trashed"):
            raise recording_missing()
        return Response(status_code=302, headers={"location": signed_location(request, entry)})

    @app.get("/file/{file_id}/{name:path}")
    @app.head("/file/{file_id}/{name:path}")
    async def stream(request: Request, file_id: str, name: str) -> Response:
        q = request.query_params
        try:
            expires = int(q.get("Expires", "0"))
        except ValueError:
            expires = 0
        if not q.get("Key-Pair-Id") or q.get("Signature") != signature(file_id, expires):
            raise EmulatorError(
                403,
                {"message": "Missing Key-Pair-Id query parameter or cookie value"},
                media_type="application/json",
            )
        if expires < ctx.clock.timestamp():
            raise EmulatorError(403, {"message": "Request has expired"}, media_type="application/json")
        found = find_file(lambda f: f["id"] == file_id)
        if found is None:
            raise recording_missing()
        _, entry = found
        data = ctx.store.blobs.get(entry["_blob"])
        total = len(data)
        headers = {
            "accept-ranges": "bytes",
            "content-disposition": f'attachment; filename="{entry["_filename"]}"',
            "etag": f'"{entry["_blob"][:32]}"',
            "cache-control": "max-age=0, s-maxage=86400",
        }
        status, body, lo, hi = 200, data, 0, total - 1
        spec = request.headers.get("range")
        if spec:
            match = RANGE_RE.match(spec.strip())
            if match is None or (not match.group(1) and not match.group(2)):
                status = 200
            else:
                start, end = match.groups()
                if not start:  # suffix range: last N bytes
                    lo, hi = max(0, total - int(end)), total - 1
                else:
                    lo, hi = int(start), min(int(end), total - 1) if end else total - 1
                if lo >= total or lo > hi:
                    return Response(
                        status_code=416,
                        headers={
                            **headers,
                            "content-range": f"bytes */{total}",
                            "content-type": "text/plain",
                        },
                    )
                status, body = 206, data[lo : hi + 1]
                headers["content-range"] = f"bytes {lo}-{hi}/{total}"
        headers["content-length"] = str(len(body))
        if request.method == "HEAD":
            return Response(status_code=status, headers={**headers, "content-type": entry["_content_type"]})
        return Response(body, status_code=status, headers=headers, media_type=entry["_content_type"])

    # -- pages --------------------------------------------------------------------------------

    def page(title: str, body: str, status: int = 200) -> HTMLResponse:
        text = (
            f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head>"
            f"<body><h1>{html.escape(title)}</h1>{body}<p><em>Zoom emulator</em></p></body></html>"
        )
        return HTMLResponse(text, status_code=status)

    @app.get("/rec/play/{token:path}")
    async def play(token: str) -> Response:
        found = find_file(lambda f: f.get("_token") == token)
        if found is None:
            return page("This recording does not exist", "", 404)
        rec, entry = found
        return page(rec["topic"], f"<p>{html.escape(entry['_filename'])} ({entry['file_type']})</p>")

    @app.get("/rec/share/{token:path}")
    async def share(token: str) -> Response:
        for rec in ctx.store.collection("recordings"):
            if rec["share_url"].rsplit("/", 1)[-1] == token:
                return page(rec["topic"], f"<p>{rec['recording_count']} recording file(s).</p>")
        return page("This recording does not exist", "", 404)

    @app.get("/j/{meeting_id}")
    @app.get("/s/{meeting_id}")
    @app.get("/w/{meeting_id}")
    async def join(meeting_id: str) -> Response:
        meeting = ctx.store.collection("meetings").get(meeting_id)
        if meeting is None:
            return page("Invalid meeting ID", "<p>This meeting ID is not valid.</p>", 404)
        return page(meeting["topic"], f"<p>Meeting ID {meeting['id']}</p><p>Launch Meeting</p>")

    @app.get("/p/v2/{avatar_id}")
    async def avatar(avatar_id: str) -> Response:
        seed = int(share_token(ctx.world, "avatar", avatar_id, length=6).encode().hex()[:6], 16)
        return Response(make_png(64, 64, seed=seed), media_type="image/png")
