"""Browser-facing endpoints: shared-link pages on ``www.dropbox.com``, temporary download links on
``dl.dropboxusercontent.com`` and the temporary upload link on ``content.dropboxapi.com``.

``?dl=1`` / ``?raw=1`` on a file link redirect (302) to ``uc<hash>.dl.dropboxusercontent.com``
exactly like production; password links accept ``?password=`` (or a form POST)."""

from __future__ import annotations

import html
import io
import json
import zipfile
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.dropbox import catalog
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import Args, parse_ts, ts
from emulators.providers.dropbox.dispatch import ranged, strip_none


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head>"
        f"<body style='font-family:system-ui;max-width:640px;margin:40px auto'>{body}</body></html>",
        status_code=status,
    )


def content_response(
    request: Request, data: bytes, name: str, *, attachment: bool, mime: str | None = None
) -> Response:
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"{'attachment' if attachment else 'inline'}; filename=\"{quote(name)}\"; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff",
    }
    try:
        status, body, extra = ranged(request, data, {})
    except EmulatorError as exc:
        return Response(str(exc.body), status_code=exc.status, headers=exc.headers)
    headers.update(extra)
    return Response(body, status_code=status, media_type=mime or catalog.mime_of(name), headers=headers)


def anonymous_call(ctx: ProviderContext, request: Request, *, admin_view: bool = False) -> m.Call:
    from emulators.providers.dropbox.dispatch import ANON_ACCOUNT

    return m.Call(
        ctx=ctx,
        request=request,
        route="web",
        args=Args({}),
        body=b"",
        actor=m.Actor(account=ANON_ACCOUNT, admin=admin_view),
        request_id="",
    )


def register(app: FastAPI, ctx: ProviderContext) -> None:
    store = ctx.store

    def lookup_link(slug: str) -> tuple[dict[str, Any] | None, m.Call]:
        c = anonymous_call(ctx, _NO_REQUEST)
        link = store.collection(m.LINKS).get(slug)
        if link is not None and link.get("expires") and link["expires"] <= ts(m.now(ctx)):
            link = None
        return link, c

    def make_download_url(
        request: Request, rec: dict[str, Any], rev: dict[str, Any], account: str = ""
    ) -> str:
        token = m.new_token(ctx, "templink", 40)
        store.collection(m.TEMP_LINKS).put(
            {
                "id": token,
                "kind": "download",
                "entry": rec["id"],
                "rev": rev["rev"],
                "account": account,
                "expires": ts(m.now(ctx) + timedelta(hours=4)),
            }
        )
        host = f"uc{m.new_hex(ctx, 'uchost', 8)}.dl.dropboxusercontent.com"
        return url_for_host(request, ctx, host, f"/cd/0/get/{token}/{quote(rec['name'])}?dl=1")

    def open_link(request: Request, slug: str, kind: str) -> Response:
        link, c = lookup_link(slug)
        if link is None or link["kind"] != kind:
            return page("Not found", "<h1>That link doesn't exist</h1>", 404)
        rec = c.tree.by_id(link["entry"])
        if rec is None or rec.get("deleted"):
            return page("Not found", "<h1>That file was deleted</h1>", 404)
        password = request.query_params.get("password")
        if link["audience"] in ("team", "no_one", "members"):
            return page("Sign in", "<h1>Sign in to view this link</h1>", 403)
        if link.get("password") and password != link["password"]:
            return page(
                "Password required",
                f"<h1>Password required</h1><p>{html.escape(rec['name'])} is protected by a password.</p>"
                "<form method='get'><input type='password' name='password'><button>Continue</button></form>",
                401,
            )
        wants_file = request.query_params.get("dl") == "1" or request.query_params.get("raw") == "1"
        if wants_file and not link.get("allow_download", True):
            return page("Downloads disabled", "<h1>Downloads are disabled for this link</h1>", 403)
        if rec["type"] == "file":
            latest = rec["revisions"][-1]
            if wants_file:
                return RedirectResponse(make_download_url(request, rec, latest), status_code=302)
            return page(
                rec["name"],
                f"<h1>{html.escape(rec['name'])}</h1><p>{latest['size']} bytes</p>"
                f"<p><a href='?dl=1'>Download</a></p>",
            )
        ns, rel = (rec["mount_ns"], "") if rec.get("mount_ns") else (rec["ns"], rec["path"])
        if wants_file:
            return zip_folder(c, rec, ns, rel)
        rows = "".join(f"<li>{html.escape(ch['name'])}</li>" for ch in c.tree.children(ns, rel))
        return page(
            rec["name"],
            f"<h1>{html.escape(rec['name'])}</h1><ul>{rows}</ul><p><a href='?dl=1'>Download</a></p>",
        )

    def zip_folder(c: m.Call, rec: dict[str, Any], ns: str, rel: str) -> Response:
        buf = io.BytesIO()
        base = len(rel)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            stack = list(reversed(c.tree.children(ns, rel)))
            while stack:
                item = stack.pop()
                name = (
                    f"{rec['name']}{item['dpath'][base:]}"
                    if not rec.get("mount_ns")
                    else f"{rec['name']}{item['dpath']}"
                )
                if item["type"] == "folder" and not item.get("mount_ns"):
                    zf.writestr(zipfile.ZipInfo(name + "/", (1980, 1, 1, 0, 0, 0)), b"")
                    stack.extend(reversed(c.tree.children(item["ns"], item["path"])))
                elif item["type"] == "file":
                    latest = item["revisions"][-1]
                    when = parse_ts(latest["server_modified"])
                    info = zipfile.ZipInfo(
                        name,
                        (max(when.year, 1980), when.month, when.day, when.hour, when.minute, when.second),
                    )
                    info.compress_type = zipfile.ZIP_DEFLATED
                    zf.writestr(info, ctx.store.blobs.get(latest["blob"]))
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{quote(rec["name"])}.zip"'},
        )

    @app.get("/request/{request_id}")
    async def file_request_page(request_id: str) -> Response:
        req = store.collection("file_requests").get(request_id)
        if req is None:
            return page("Not found", "<h1>This file request doesn't exist</h1>", 404)
        from emulators.providers.dropbox.file_requests import is_open

        if not is_open(ctx, req):
            return page(
                req["title"], f"<h1>{html.escape(req['title'])}</h1><p>This file request is closed.</p>", 410
            )
        return page(
            req["title"],
            f"<h1>{html.escape(req['title'])}</h1><p>{html.escape(req.get('description') or '')}</p>",
        )

    @app.get("/scl/fi/{slug}/{name:path}")
    async def scl_file(slug: str, name: str, request: Request) -> Response:
        return open_link(request, slug, "file")

    @app.get("/s/{slug}/{name:path}")
    async def legacy_file(slug: str, name: str, request: Request) -> Response:
        return open_link(request, slug, "file")

    @app.get("/scl/fo/{slug}/{token:path}")
    async def scl_folder(slug: str, token: str, request: Request) -> Response:
        return open_link(request, slug, "folder")

    @app.get("/sh/{slug}/{token:path}")
    async def legacy_folder(slug: str, token: str, request: Request) -> Response:
        return open_link(request, slug, "folder")

    # -- temporary links ---------------------------------------------------------------
    def serve_temp(request: Request, token: str) -> Response:
        link = store.collection(m.TEMP_LINKS).get(token)
        if link is None or link["kind"] != "download" or link["expires"] <= ts(m.now(ctx)):
            return page("Link expired", "<h1>This link is invalid or has expired</h1>", 404)
        rec = store.collection(m.ENTRIES).get(link["entry"])
        if rec is None or rec.get("deleted"):
            return page("Not found", "<h1>File not found</h1>", 404)
        revision = next((r for r in rec["revisions"] if r["rev"] == link["rev"]), rec["revisions"][-1])
        return content_response(request, ctx.store.blobs.get(revision["blob"]), rec["name"], attachment=True)

    @app.get("/apitl/1/{token}")
    async def temp_link(token: str, request: Request) -> Response:
        return serve_temp(request, token)

    @app.get("/cd/0/get/{token}/{name:path}")
    async def cd_get(token: str, name: str, request: Request) -> Response:
        return serve_temp(request, token)

    @app.post("/apitul/1/{token}")
    async def temp_upload(token: str, request: Request) -> Response:
        link = store.collection(m.TEMP_LINKS).get(token)
        if link is None or link["kind"] != "upload" or link["expires"] <= ts(m.now(ctx)):
            return page("Link expired", "<h1>This upload link is invalid or has expired</h1>", 404)
        acct = m.account(ctx, link["account"])
        if acct is None:
            return page("Not found", "<h1>Account not found</h1>", 404)
        from emulators.providers.dropbox.dispatch import json_response
        from emulators.providers.dropbox.files_content import commit_file, finish_fail, parse_mode

        body = await request.body()
        commit = Args(link["commit"], "commit_info")
        actor = m.Actor(account=acct, root_ns=link["root"])
        c = m.Call(
            ctx=ctx, request=request, route="apitul", args=Args({}), body=body, actor=actor, request_id=""
        )
        mode, mode_rev = parse_mode(commit)
        try:
            rec = commit_file(
                c,
                commit.str("path"),
                body,
                mode=mode,
                mode_rev=mode_rev,
                autorename_=commit.bool("autorename"),
                client_modified=commit.ts("client_modified"),
                strict_conflict=commit.bool("strict_conflict"),
                fail=finish_fail,
            )
        except EmulatorError as exc:
            return Response(
                json.dumps(strip_none(exc.body)), status_code=exc.status, media_type="application/json"
            )
        store.collection(m.TEMP_LINKS).delete(token)
        if c.touched:
            from emulators.providers.dropbox import webhooks

            await webhooks.notify(c)
        return json_response(m.file_metadata(c, rec), "")


class _NoRequest:
    """Placeholder request for anonymous web calls that never read headers."""


_NO_REQUEST: Any = _NoRequest()
