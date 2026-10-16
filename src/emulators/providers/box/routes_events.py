"""Events: user streams, enterprise ``admin_logs``, and the long-poll subscription."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from fastapi import FastAPI, Request

from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.box import db, errors, events
from emulators.providers.box.access import Principal
from emulators.providers.box.routing import Api
from emulators.providers.box.shapes import Shaper
from emulators.providers.box.util import input_ts, query_int, query_list

Record = dict[str, Any]
STREAMS = ("all", "changes", "sync", "admin_logs", "admin_logs_streaming")


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)

    def visible_events(
        p: Principal, stream: str, after: int, wanted_types: set[str], created: tuple[str | None, str | None]
    ) -> list[Record]:
        out = []
        for event in ctx.store.collection(db.EVENTS):
            if event["_pos"] <= after:
                continue
            if wanted_types and event["event_type"] not in wanted_types:
                continue
            if stream.startswith("admin_logs"):
                low, high = created
                if low and event["created_at"] < low:
                    continue
                if high and event["created_at"] > high:
                    continue
            else:
                if stream in ("changes", "sync") and event["event_type"] not in events.CHANGE_EVENTS:
                    continue
                if not events.event_visible(p, event):
                    continue
            out.append(event)
        out.sort(key=lambda e: e["_pos"])
        return out

    @api.get("/events")
    async def get_events(request: Request) -> Any:
        p = api.principal(request)
        q = request.query_params
        stream = q.get("stream_type", "all")
        if stream not in STREAMS:
            raise errors.invalid_parameter(
                "stream_type", f"'stream_type' must be one of {', '.join(STREAMS)}"
            )
        if stream.startswith("admin_logs"):
            p.require_privileged()
        position = q.get("stream_position")
        top = events.current_position(ctx)
        if position == "now":
            return {"chunk_size": 0, "next_stream_position": str(top), "entries": []}
        try:
            after = int(position) if position not in (None, "") else 0
        except ValueError:
            raise errors.invalid_parameter(
                "stream_position", "'stream_position' must be 'now' or a number"
            ) from None
        limit = query_int(request, "limit", 100, minimum=1, maximum=500)
        wanted = set(query_list(request, "event_type"))
        created = (
            input_ts(q["created_after"], "created_after") if q.get("created_after") else None,
            input_ts(q["created_before"], "created_before") if q.get("created_before") else None,
        )
        rows = visible_events(p, stream, after, wanted, created)[:limit]
        shaper = Shaper(p, request)
        # With nothing newer visible, jump to the head so the client does not rescan the log.
        next_position = rows[-1]["_pos"] if rows else max(after, top)
        return {
            "chunk_size": len(rows),
            "next_stream_position": str(next_position),
            "entries": [events.render_event(shaper, e) for e in rows],
        }

    @api.options("/events")
    async def long_poll_info(request: Request) -> Any:
        p = api.principal(request)
        stream = request.query_params.get("stream_type", "changes")
        channel = secrets.token_hex(16)
        ctx.store.collection(db.CHANNELS).put(
            {"id": channel, "user_id": p.uid, "stream": stream, "since": events.current_position(ctx)}
        )
        url = url_for_host(
            request, ctx, "realtime.box.com", f"/subscribe?channel={channel}&stream_type={stream}"
        )
        return {
            "chunk_size": 1,
            "entries": [
                {
                    "type": "realtime_server",
                    "url": url,
                    "ttl": "10",
                    "max_retries": "10",
                    "retry_timeout": 610,
                }
            ],
        }

    @app.get("/subscribe")
    async def subscribe(request: Request) -> Any:
        """The long-poll endpoint: ``new_change`` as soon as an event is visible, else ``reconnect``."""
        channel = ctx.store.collection(db.CHANNELS).get(request.query_params.get("channel", ""))
        if channel is None:
            return {"message": "reconnect"}
        user = db.users(ctx).get(channel["user_id"])
        if user is None:
            return {"message": "reconnect"}
        principal = Principal(ctx, user, user)
        raw = request.query_params.get("stream_position")
        since = int(raw) if raw and raw.isdigit() else channel["since"]
        timeout = float(ctx.setting("LONGPOLL_TIMEOUT", "3") or 3)
        deadline = time.monotonic() + timeout
        while True:
            if visible_events(principal, channel["stream"], since, set(), (None, None)):
                return {"message": "new_change"}
            if time.monotonic() >= deadline:
                return {"message": "reconnect"}
            await asyncio.sleep(0.05)
