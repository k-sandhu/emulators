"""A sequenced change journal and Google header-only push channels."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.google.common import Principal, google_error, json_object
from emulators.providers.google.drive_access import file_shape, get_file, principal, role_for
from emulators.providers.google.drive_fields import select


def visibility(ctx: ProviderContext, row: dict[str, Any]) -> list[str]:
    return [p.id for p in ctx.world.people if role_for(ctx, row, Principal(p.id, False, ""))]


async def notify(ctx: ProviderContext, channel: dict[str, Any], state: str, changed: str = "") -> None:
    if int(channel["expiration"]) <= ctx.clock.timestamp() * 1000:
        return
    channel["_number"] = channel.get("_number", 0) + 1
    headers = {
        "X-Goog-Channel-ID": channel["id"],
        "X-Goog-Resource-ID": channel["resourceId"],
        "X-Goog-Resource-URI": channel["resourceUri"],
        "X-Goog-Resource-State": state,
        "X-Goog-Message-Number": str(channel["_number"]),
        "X-Goog-Channel-Expiration": format_datetime(
            datetime.fromtimestamp(int(channel["expiration"]) / 1000, UTC), usegmt=True
        ),
    }
    if channel.get("token"):
        headers["X-Goog-Channel-Token"] = channel["token"]
    if changed:
        headers["X-Goog-Changed"] = changed
    await ctx.webhooks.deliver(channel["_address"], b"", headers=headers, event_type="drive." + state)


async def record_change(
    ctx: ProviderContext,
    row: dict[str, Any],
    *,
    removed: bool = False,
    state: str = "update",
    changed: str = "properties",
    before: list[str] | None = None,
    drive: bool = False,
) -> None:
    number = ctx.store.meta.get("drive_change_sequence", 0) + 1
    ctx.store.meta["drive_change_sequence"] = number
    audience = list(set((before or []) + visibility(ctx, row))) if not drive else (before or [])
    change = {
        "id": str(number),
        "kind": "drive#change",
        "changeType": "drive" if drive else "file",
        "time": isoformat_z(ctx.clock.now(), timespec="milliseconds"),
        "removed": removed,
        "_visible": audience,
        "_record": copy.deepcopy(row),
    }
    if drive:
        change["driveId"] = row["id"]
    else:
        change["fileId"] = row["id"]
        if row.get("driveId"):
            change["driveId"] = row["driveId"]
    ctx.store.collection("drive_changes").put(change)
    for channel in ctx.store.collection("drive_channels"):
        actor = Principal(channel.get("_person"), channel.get("_person") is None, channel["_client"])
        if channel.get("_file") == row["id"] and (
            actor.person_id in audience or (not drive and role_for(ctx, row, actor))
        ):
            await notify(ctx, channel, state, changed)
        elif (
            channel.get("_changes")
            and (not channel.get("_drive") or channel["_drive"] == change.get("driveId"))
            and (actor.person_id in audience or (not drive and role_for(ctx, row, actor)))
        ):
            await notify(ctx, channel, "change")


def parse_change_token(ctx: ProviderContext, value: str | None) -> int:
    try:
        if value is None or not value.isdecimal():
            raise ValueError
        number = int(value)
        if not 1 <= number <= ctx.store.meta["drive_change_sequence"] + 1:
            raise ValueError
        return number
    except ValueError:
        raise google_error(400, "Invalid Value", reason="invalid", location="pageToken") from None


async def watch(
    ctx: ProviderContext, request: Request, actor: Principal, *, fid: str | None = None
) -> dict[str, Any]:
    data = await json_object(request)
    if (
        not data.get("id")
        or data.get("type") != "web_hook"
        or not str(data.get("address", "")).startswith(("http://", "https://"))
    ):
        raise google_error(400, "Invalid channel.", reason="invalid")
    if data["id"] in ctx.store.collection("drive_channels"):
        raise google_error(400, "Channel id already exists", reason="invalid")
    if fid:
        get_file(ctx, actor, fid)
    else:
        parse_change_token(ctx, request.query_params.get("pageToken"))
        if request.query_params.get("driveId"):
            get_file(ctx, actor, request.query_params["driveId"])
    maximum = ctx.clock.timestamp() * 1000 + (86400000 if fid else 604800000)
    try:
        expiration = min(int(data.get("expiration", maximum)), maximum)
        if expiration <= ctx.clock.timestamp() * 1000:
            raise ValueError
    except (ValueError, TypeError):
        raise google_error(400, "Invalid expiration", reason="invalid") from None
    resource = "/drive/v3/files/" + fid if fid else "/drive/v3/changes"
    row = {
        "kind": "api#channel",
        "id": data["id"],
        "resourceId": stable_hash(ctx.world.seed, "drive-channel", resource, length=28),
        "resourceUri": url_for_host(request, ctx, "www.googleapis.com", resource),
        "expiration": str(expiration),
        "_address": data["address"],
        "_person": actor.person_id,
        "_client": actor.client_id,
        "_file": fid,
        "_changes": fid is None,
        "_drive": request.query_params.get("driveId"),
    }
    if "token" in data:
        row["token"] = data["token"]
    ctx.store.collection("drive_channels").put(row)
    await notify(ctx, row, "sync")
    return {k: v for k, v in row.items() if not k.startswith("_")}


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/drive/v3/changes/startPageToken")
    async def start(request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        if request.query_params.get("driveId"):
            get_file(ctx, actor, request.query_params["driveId"])
        return {
            "kind": "drive#startPageToken",
            "startPageToken": str(ctx.store.meta["drive_change_sequence"] + 1),
        }

    @app.get("/drive/v3/changes")
    async def changes(request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        q = request.query_params
        start = parse_change_token(ctx, q.get("pageToken"))
        try:
            size = min(int(q.get("pageSize", "100")), 1000)
            if size < 1:
                raise ValueError
        except ValueError:
            raise google_error(400, "Invalid pageSize", reason="invalid") from None
        if q.get("driveId"):
            get_file(ctx, actor, q["driveId"])
        logs = [r for r in ctx.store.collection("drive_changes") if int(r["id"]) >= start]
        chosen = logs[:size]
        result: dict[str, Any] = {"kind": "drive#changeList", "changes": []}
        for change in chosen:
            if q.get("driveId") and change.get("driveId") != q["driveId"]:
                continue
            if q.get("restrictToMyDrive") == "true" and change.get("driveId"):
                continue
            if (
                change.get("driveId")
                and q.get("includeItemsFromAllDrives") != "true"
                and not q.get("driveId")
            ):
                continue
            row = change["_record"]
            current = ctx.store.collection("drive_files").get(row["id"])
            visible = role_for(ctx, current, actor) if current else None
            if not visible and actor.person_id not in change["_visible"]:
                continue
            removed = change["removed"] or not visible
            if removed and q.get("includeRemoved", "true") != "true":
                continue
            item = {k: v for k, v in change.items() if not k.startswith("_") and k != "id"}
            item["removed"] = removed
            if not removed:
                if change["changeType"] == "file":
                    item["file"] = file_shape(ctx, request, row, actor)
                else:
                    item["drive"] = {k: v for k, v in row.items() if not k.startswith("_")}
            result["changes"].append(item)
        if len(logs) > size:
            result["nextPageToken"] = str(int(chosen[-1]["id"]) + 1)
        else:
            result["newStartPageToken"] = str(ctx.store.meta["drive_change_sequence"] + 1)
        return select(result, q.get("fields"))

    @app.post("/drive/v3/changes/watch")
    async def watch_changes(request: Request) -> dict[str, Any]:
        return await watch(ctx, request, principal(request, ctx))

    @app.post("/drive/v3/channels/stop")
    async def stop(request: Request) -> Response:
        actor = principal(request, ctx)
        data = await json_object(request)
        row = ctx.store.collection("drive_channels").get(data.get("id"))
        if not row or row["resourceId"] != data.get("resourceId"):
            raise google_error(404, "Channel not found", reason="notFound")
        if row["_person"] != actor.person_id or row["_client"] != actor.client_id:
            raise google_error(403, "Forbidden", reason="forbidden")
        ctx.store.collection("drive_channels").delete(row["id"])
        return Response(status_code=204)
