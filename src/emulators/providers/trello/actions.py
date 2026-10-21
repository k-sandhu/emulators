"""Simulation entry points use the same permission checks and event path as REST."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

from emulators.core.provider import ProviderContext, ctx_url_for_host

from .auth import mint_token
from .common import Record, lookup, text
from .routes import Api


async def simulate(ctx: ProviderContext, payload: Record, *, comment: bool) -> Record:
    card = lookup(ctx, "cards", text(payload.get("card"), "card"))
    token = mint_token(ctx, {"user": payload.get("user", "p001")})["access_token"]
    grant = ctx.tokens.resolve(token)
    url = ctx_url_for_host(ctx, "api.trello.com", "/")
    parsed = urlsplit(url)
    request = Request(
        {
            "type": "http",
            "method": "POST" if comment else "PUT",
            "scheme": parsed.scheme,
            "server": (parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)),
            "path": "/1/cards/" + card["id"],
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", parsed.netloc.encode())],
            "state": {"external_base": url.rstrip("/")},
        }
    )
    params = {"text": payload.get("text")} if comment else {"idList": payload.get("idList")}
    path = "cards/" + card["id"] + ("/actions/comments" if comment else "")
    return await Api(ctx, request, params, grant).dispatch(path)


async def post_comment(ctx: ProviderContext, payload: Record) -> Record:
    return await simulate(ctx, payload, comment=True)


async def move_card(ctx: ProviderContext, payload: Record) -> Record:
    return await simulate(ctx, payload, comment=False)
