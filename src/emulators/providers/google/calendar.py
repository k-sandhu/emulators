"""Calendar v3 resources, incremental sync and push channels."""

from __future__ import annotations

import copy
import re
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request, Response

from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.ids import b64url, stable_hash
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.google.calendar_model import (
    PREFIX,
    REMINDERS,
    calendar_access,
    conference_data,
    event_output,
    expand,
    identity,
    instant,
    list_entry,
    page,
    public,
    revision,
    seed,
    sync_token,
    validate_event,
)
from emulators.providers.google.common import google_error, json_object

__all__ = ["register", "seed"]


def _event_get(ctx: ProviderContext, cal: dict[str, Any], event_id: str) -> dict[str, Any]:
    key = cal["id"] + ":" + event_id
    event = ctx.store["calendar_events"].get(key) or ctx.store["calendar_instances"].get(key)
    if event is None and "_" in event_id:
        master_id = event_id.rsplit("_", 1)[0]
        master = ctx.store["calendar_events"].get(cal["id"] + ":" + master_id)
        if master:
            event = next((r for r in expand(ctx, master) if r["id"] == event_id), None)
    if event is None:
        raise google_error(404, "Not Found", reason="notFound")
    return event


def _precondition(request: Request, event: dict[str, Any]) -> None:
    match = request.headers.get("if-match")
    if match and match not in ("*", event["etag"]):
        raise google_error(412, "Precondition Failed", reason="conditionNotMet")


async def notify(
    ctx: ProviderContext, calendar_id: str, state: str = "exists", *, only: str | None = None
) -> None:
    for channel in ctx.store["calendar_channels"].where(_calendar=calendar_id):
        if channel["_expires"] <= ctx.clock.timestamp() * 1000 or (only and channel["id"] != only):
            continue
        channel["_number"] += 1
        headers = {
            "X-Goog-Channel-ID": channel["id"],
            "X-Goog-Resource-ID": channel["resourceId"],
            "X-Goog-Resource-URI": channel["resourceUri"],
            "X-Goog-Resource-State": state,
            "X-Goog-Message-Number": str(channel["_number"]),
        }
        if "token" in channel:
            headers["X-Goog-Channel-Token"] = channel["token"]
        await ctx.webhooks.deliver(
            channel["_address"],
            b"",
            headers=headers,
            event_type="calendar.events",
            content_type="application/octet-stream",
        )


async def _save_event(
    request: Request,
    ctx: ProviderContext,
    cal: dict[str, Any],
    actor: str,
    body: dict[str, Any],
    old: dict[str, Any] | None = None,
    *,
    replace: bool = False,
    importing: bool = False,
) -> dict[str, Any]:
    event_id = old["id"] if old else body.get("id", ctx.ids.hex(32))
    if not re.fullmatch(r"[0-9a-v]{5,1024}", event_id) and not (old and "recurringEventId" in old):
        raise google_error(400, "Invalid resource id value.", reason="invalid")
    if not old and ctx.store["calendar_events"].get(cal["id"] + ":" + event_id):
        raise google_error(409, "The requested identifier already exists.", reason="duplicate")
    if importing and not body.get("iCalUID"):
        raise google_error(400, "Missing iCalUID", reason="required")
    email = ctx.world.person(actor).email
    base: dict[str, Any] = {
        "kind": "calendar#event",
        "id": event_id,
        "status": "confirmed",
        "created": isoformat_z(ctx.clock.now()),
        "creator": {"email": email},
        "organizer": {"email": cal["id"]},
        "iCalUID": event_id + "@google.com",
        "sequence": 0,
        "reminders": {"useDefault": True},
        "eventType": "default",
        "_calendar": cal["id"],
        "_rev": 0,
    }
    if old:
        base = copy.deepcopy(old)
        if replace:
            base = {
                k: v
                for k, v in base.items()
                if k.startswith("_")
                or k in ("kind", "id", "created", "creator", "organizer", "iCalUID", "sequence")
            }
            base.update({"status": "confirmed", "reminders": {"useDefault": True}, "eventType": "default"})
    immutable = {
        "kind",
        "id",
        "etag",
        "created",
        "updated",
        "creator",
        "organizer",
        "htmlLink",
        "recurringEventId",
        "originalStartTime",
    }
    for key, value in body.items():
        if key.startswith("_") or key in immutable:
            continue
        if value is None:
            base.pop(key, None)
        elif isinstance(value, dict) and isinstance(base.get(key), dict) and not replace:
            base[key] = {**base[key], **copy.deepcopy(value)}
        else:
            base[key] = copy.deepcopy(value)
    validate_event(base)
    conference = base.get("conferenceData", {})
    if "createRequest" in conference and request.query_params.get("conferenceDataVersion") == "1":
        from emulators.providers.google.meet import create_space

        space = create_space(ctx, actor, "calendar-" + event_id, {})
        space["_members"] = list(
            dict.fromkeys(
                [
                    actor,
                    *[
                        p.id
                        for a in base.get("attendees", [])
                        if (p := ctx.world.person_by_email(a.get("email", "")))
                    ],
                ]
            )
        )
        base["conferenceData"] = conference_data(ctx, "calendar-" + event_id)
        base["conferenceData"]["createRequest"] = {
            **conference["createRequest"],
            "status": {"statusCode": "success"},
        }
        base["hangoutLink"] = space["meetingUri"]
    elif "conferenceData" in body and request.query_params.get("conferenceDataVersion", "0") != "1":
        if old and "conferenceData" in old:
            base["conferenceData"] = copy.deepcopy(old["conferenceData"])
        else:
            base.pop("conferenceData", None)
    base["htmlLink"] = "https://www.google.com/calendar/event?eid=" + b64url(
        (event_id + " " + cal["id"]).encode()
    )
    revision(ctx, base)
    if old and old.get("_seedSeries"):
        if "recurrence" in body or "start" in body or "end" in body:
            base.pop("_seedSeries", None)
            for instance in ctx.store["calendar_instances"].where(
                _calendar=cal["id"], recurringEventId=event_id
            ):
                if instance.get("_seedInstance"):
                    ctx.store["calendar_instances"].delete(cal["id"] + ":" + instance["id"])
        else:
            for instance in ctx.store["calendar_instances"].where(
                _calendar=cal["id"], recurringEventId=event_id
            ):
                for field in body:
                    if field in base and not field.startswith("_") and field not in immutable:
                        instance[field] = copy.deepcopy(base[field])
                revision(ctx, instance)
    collection = "calendar_instances" if "recurringEventId" in base else "calendar_events"
    ctx.store[collection].put_as(cal["id"] + ":" + event_id, base)
    # An organizer update is reflected on all internal invitation copies.
    if base.get("organizer", {}).get("email") == cal["id"]:
        recipients = {a.get("email") for a in base.get("attendees", [])} | {cal["id"]}
        for target in recipients:
            if target == cal["id"] or not ctx.store["calendar_calendars"].get(target):
                continue
            replica = copy.deepcopy(base)
            replica["_calendar"] = target
            ctx.store[collection].put_as(target + ":" + event_id, replica)
            await notify(ctx, target)
    await notify(ctx, cal["id"])
    return base


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get(PREFIX + "/users/me/calendarList")
    async def calendar_list(request: Request) -> dict[str, Any]:
        actor = identity(request, ctx)
        entries = [
            row
            for key in ctx.store["calendar_list"].ids()
            if key.startswith(actor + ":") and (row := ctx.store["calendar_list"].get(key))
        ]
        if request.query_params.get("minAccessRole"):
            levels = {"freeBusyReader": 0, "reader": 1, "writer": 2, "owner": 3}
            minimum = levels.get(request.query_params["minAccessRole"])
            if minimum is None:
                raise google_error(400, "Invalid minAccessRole", reason="invalid")
            entries = [r for r in entries if levels[r["accessRole"]] >= minimum]
        rows, token = page(request, entries, default=100, maximum=250, binding=actor)
        result = {
            "kind": "calendar#calendarList",
            "etag": '"calendar-list"',
            "items": [public(r) for r in rows],
        }
        if token:
            result["nextPageToken"] = token
        return result

    @app.post(PREFIX + "/users/me/calendarList")
    async def calendar_list_insert(request: Request) -> dict[str, Any]:
        body = await json_object(request)
        cal, role, actor = calendar_access(request, ctx, body.get("id", ""), busy=True)
        entry = list_entry(cal, role, cal["_owner"] == actor)
        for field in (
            "summaryOverride",
            "colorId",
            "backgroundColor",
            "foregroundColor",
            "hidden",
            "selected",
            "defaultReminders",
            "notificationSettings",
        ):
            if field in body:
                entry[field] = body[field]
        ctx.store["calendar_list"].put_as(actor + ":" + cal["id"], entry)
        return entry

    @app.api_route(PREFIX + "/users/me/calendarList/{calendar_id}", methods=["GET", "PATCH", "PUT", "DELETE"])
    async def calendar_list_item(request: Request, calendar_id: str) -> Any:
        cal, _, actor = calendar_access(request, ctx, calendar_id, busy=True)
        key = actor + ":" + cal["id"]
        entry = ctx.store["calendar_list"].get(key)
        if not entry:
            raise google_error(404, "Not Found", reason="notFound")
        if request.method == "DELETE":
            ctx.store["calendar_list"].delete(key)
            return Response(status_code=204)
        if request.method in ("PATCH", "PUT"):
            body = await json_object(request)
            for field in (
                "summaryOverride",
                "colorId",
                "backgroundColor",
                "foregroundColor",
                "hidden",
                "selected",
                "defaultReminders",
                "notificationSettings",
            ):
                if field in body:
                    entry[field] = body[field]
        return public(entry)

    @app.post(PREFIX + "/calendars")
    async def calendars_insert(request: Request) -> dict[str, Any]:
        actor = identity(request, ctx)
        body = await json_object(request)
        if not body.get("summary"):
            raise google_error(400, "Missing summary", reason="required")
        calendar_id = ctx.ids.hex(26) + "@group.calendar.google.com"
        cal = {
            "kind": "calendar#calendar",
            "id": calendar_id,
            "etag": '"' + stable_hash(calendar_id) + '"',
            "summary": body["summary"],
            "timeZone": body.get("timeZone", ctx.world.person(actor).timezone),
            "_owner": actor,
            "_acl": {actor: "owner"},
        }
        for field in ("description", "location"):
            if field in body:
                cal[field] = body[field]
        ctx.store["calendar_calendars"].put(cal)
        ctx.store["calendar_list"].put_as(actor + ":" + calendar_id, list_entry(cal, "owner"))
        return public(cal)

    @app.api_route(PREFIX + "/calendars/{calendar_id}", methods=["GET", "PATCH", "PUT", "DELETE"])
    async def calendars_item(request: Request, calendar_id: str) -> Any:
        cal, role, actor = calendar_access(
            request, ctx, calendar_id, write=request.method != "GET", busy=request.method == "GET"
        )
        if request.method == "DELETE":
            if role != "owner" or cal["id"] == ctx.world.person(actor).email:
                raise google_error(403, "Cannot delete this calendar", reason="forbidden")
            ctx.store["calendar_calendars"].delete(cal["id"])
            for key in ctx.store["calendar_list"].ids():
                if key.endswith(":" + cal["id"]):
                    ctx.store["calendar_list"].delete(key)
            for collection in ("calendar_events", "calendar_instances"):
                for row in ctx.store[collection].where(_calendar=cal["id"]):
                    ctx.store[collection].delete(cal["id"] + ":" + row["id"])
            await notify(ctx, cal["id"], "not_exists")
            for channel in ctx.store["calendar_channels"].where(_calendar=cal["id"]):
                ctx.store["calendar_channels"].delete(channel["id"])
            return Response(status_code=204)
        if request.method in ("PATCH", "PUT"):
            if role != "owner":
                raise google_error(
                    403, "Only calendar owners can update calendar properties", reason="forbidden"
                )
            body = await json_object(request)
            for field in ("summary", "description", "location", "timeZone"):
                if field in body:
                    cal[field] = body[field]
            cal["etag"] = '"' + ctx.ids.hex(16) + '"'
            for entry in ctx.store["calendar_list"].where(id=cal["id"]):
                for field in ("summary", "timeZone", "etag", "description", "location"):
                    if field in cal:
                        entry[field] = cal[field]
        return public(cal)

    @app.post(PREFIX + "/calendars/{calendar_id}/clear")
    async def calendars_clear(request: Request, calendar_id: str) -> Response:
        cal, role, _ = calendar_access(request, ctx, calendar_id, write=True)
        if role != "owner":
            raise google_error(403, "Only owners may clear a calendar", reason="forbidden")
        for collection in ("calendar_events", "calendar_instances"):
            for event in ctx.store[collection].where(_calendar=cal["id"]):
                event["status"] = "cancelled"
                revision(ctx, event)
        await notify(ctx, cal["id"])
        return Response(status_code=204)

    @app.get(PREFIX + "/calendars/{calendar_id}/acl")
    async def acl_list(request: Request, calendar_id: str) -> dict[str, Any]:
        cal, role, _ = calendar_access(request, ctx, calendar_id)
        if role != "owner":
            raise google_error(403, "Only owners may read calendar ACLs", reason="forbidden")
        rows = [
            {
                "kind": "calendar#aclRule",
                "id": "user:" + ctx.world.person(pid).email,
                "etag": '"' + stable_hash(cal["id"], pid, access) + '"',
                "scope": {"type": "user", "value": ctx.world.person(pid).email},
                "role": access,
            }
            for pid, access in cal["_acl"].items()
        ]
        if cal.get("_domainRole"):
            rows.append(
                {
                    "kind": "calendar#aclRule",
                    "id": "domain:" + ctx.world.org.domain,
                    "etag": '"domain"',
                    "scope": {"type": "domain", "value": ctx.world.org.domain},
                    "role": cal["_domainRole"],
                }
            )
        rows, token = page(request, rows, default=100, maximum=250)
        return {
            "kind": "calendar#acl",
            "etag": cal["etag"],
            "items": rows,
            **({"nextPageToken": token} if token else {}),
        }

    @app.get(PREFIX + "/calendars/{calendar_id}/events")
    async def events_list(request: Request, calendar_id: str) -> dict[str, Any]:
        cal, role, actor = calendar_access(request, ctx, calendar_id)
        query = request.query_params
        single = query.get("singleEvents", "false").lower() == "true"
        if query.get("orderBy") == "startTime" and not single:
            raise google_error(
                400, "The requested ordering is not available for the particular query.", reason="badRequest"
            )
        if query.get("orderBy") not in (None, "startTime", "updated"):
            raise google_error(400, "Invalid orderBy", reason="invalid")
        try:
            low = parse_instant(query["timeMin"]) if query.get("timeMin") else None
            high = parse_instant(query["timeMax"]) if query.get("timeMax") else None
            if low and high and low >= high:
                raise ValueError
        except ValueError as exc:
            raise google_error(400, "Bad Request", reason="badRequest") from exc
        since = None
        if query.get("syncToken"):
            cursor = ctx.store["calendar_sync"].get(query["syncToken"])
            if (
                not cursor
                or cursor["calendar"] != cal["id"]
                or cursor["actor"] != actor
                or cursor["expires"] <= ctx.clock.timestamp()
            ):
                raise google_error(
                    410, "Sync token is no longer valid, a full sync is required.", reason="fullSyncRequired"
                )
            if (
                any(
                    k in query
                    for k in (
                        "iCalUID",
                        "orderBy",
                        "privateExtendedProperty",
                        "sharedExtendedProperty",
                        "q",
                        "timeMin",
                        "timeMax",
                        "updatedMin",
                    )
                )
                or query.get("showDeleted") == "false"
            ):
                raise google_error(400, "Invalid query with syncToken", reason="badRequest")
            since = cursor["revision"]
        masters = ctx.store["calendar_events"].where(_calendar=cal["id"])
        rows = [
            event for master in masters for event in (expand(ctx, master, low, high) if single else [master])
        ]
        if since is not None:
            rows = [r for r in rows if r.get("_rev", 0) > since]
            if not single:
                rows += [
                    r
                    for r in ctx.store["calendar_instances"].where(_calendar=cal["id"])
                    if r.get("_rev", 0) > since
                ]
        else:
            rows = [
                r
                for r in rows
                if query.get("showDeleted", "false") == "true"
                or query.get("updatedMin")
                or r["status"] != "cancelled"
            ]
        if low:
            rows = [
                r
                for r in rows
                if ("recurrence" in r and expand(ctx, r, low, high)) or instant(r["end"]) > low
            ]
        if high:
            rows = [r for r in rows if instant(r["start"]) < high]
        if query.get("q"):
            term = query["q"].casefold()
            rows = [
                r
                for r in rows
                if term
                in " ".join(
                    str(r.get(f, "")) for f in ("summary", "description", "location", "attendees")
                ).casefold()
            ]
        if query.get("iCalUID"):
            rows = [r for r in rows if r.get("iCalUID") == query["iCalUID"]]
        if query.get("updatedMin"):
            try:
                updated_min = parse_instant(query["updatedMin"])
            except ValueError as exc:
                raise google_error(400, "Invalid updatedMin", reason="badRequest") from exc
            rows = [r for r in rows if parse_instant(r["updated"]) >= updated_min]
        if query.get("orderBy") == "startTime":
            rows.sort(key=lambda r: instant(r["start"]))
        elif query.get("orderBy") == "updated":
            rows.sort(key=lambda r: parse_instant(r["updated"]))
        rows, token = page(request, rows, binding=actor)
        result: dict[str, Any] = {
            "kind": "calendar#events",
            "etag": '"' + stable_hash(cal["id"], ctx.store.meta["calendar_revision"]) + '"',
            "summary": cal["summary"],
            "updated": isoformat_z(ctx.clock.now()),
            "timeZone": cal["timeZone"],
            "accessRole": role,
            "defaultReminders": copy.deepcopy(REMINDERS),
            "items": [event_output(request, ctx, r, cal, actor, role) for r in rows],
        }
        result["nextPageToken" if token else "nextSyncToken"] = token or sync_token(ctx, cal["id"], actor)
        return result

    @app.post(PREFIX + "/calendars/{calendar_id}/events")
    @app.post(PREFIX + "/calendars/{calendar_id}/events/import")
    async def events_insert(request: Request, calendar_id: str) -> dict[str, Any]:
        cal, role, actor = calendar_access(request, ctx, calendar_id, write=True)
        event = await _save_event(
            request,
            ctx,
            cal,
            actor,
            await json_object(request),
            importing=request.url.path.endswith("/import"),
        )
        return event_output(request, ctx, event, cal, actor, role)

    @app.post(PREFIX + "/calendars/{calendar_id}/events/quickAdd")
    async def quick_add(request: Request, calendar_id: str) -> dict[str, Any]:
        cal, role, actor = calendar_access(request, ctx, calendar_id, write=True)
        text = request.query_params.get("text", "")
        if not text:
            raise google_error(400, "Missing text", reason="required")
        # A small deterministic subset of Quick Add's natural-language input.
        match = re.search(r"\b(\d{4}-\d\d-\d\d)(?:\s+(?:at\s+)?(\d{1,2}):(\d\d))?", text)
        start = ctx.clock.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        summary = text
        if match:
            start = parse_instant(match[1] + f"T{int(match[2] or 9):02d}:{match[3] or '00'}:00Z")
            summary = text[: match.start()].strip() or text
        body = {
            "summary": summary,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
        }
        event = await _save_event(request, ctx, cal, actor, body)
        return event_output(request, ctx, event, cal, actor, role)

    @app.post(PREFIX + "/calendars/{calendar_id}/events/watch")
    async def events_watch(request: Request, calendar_id: str) -> dict[str, Any]:
        cal, _, actor = calendar_access(request, ctx, calendar_id)
        body = await json_object(request)
        if not body.get("id") or body.get("type") not in ("web_hook", "webhook") or not body.get("address"):
            raise google_error(400, "Invalid channel", reason="badRequest")
        if ctx.store["calendar_channels"].get(body["id"]):
            raise google_error(400, "Channel id is not unique", reason="invalid")
        expires = min(
            int(body.get("expiration", (ctx.clock.timestamp() + 604800) * 1000)),
            (ctx.clock.timestamp() + 604800) * 1000,
        )
        channel = {
            "kind": "api#channel",
            "id": body["id"],
            "resourceId": ctx.ids.urlsafe(24),
            "resourceUri": url_for_host(
                request, ctx, "www.googleapis.com", PREFIX + "/calendars/" + cal["id"] + "/events"
            ),
            "expiration": str(expires),
            "_expires": expires,
            "_address": body["address"],
            "_calendar": cal["id"],
            "_actor": actor,
            "_number": 0,
        }
        if "token" in body:
            channel["token"] = body["token"]
        ctx.store["calendar_channels"].put(channel)
        await notify(ctx, cal["id"], "sync", only=channel["id"])
        return public(channel)

    @app.api_route(
        PREFIX + "/calendars/{calendar_id}/events/{event_id}", methods=["GET", "PATCH", "PUT", "DELETE"]
    )
    async def events_item(request: Request, calendar_id: str, event_id: str) -> Any:
        cal, role, actor = calendar_access(request, ctx, calendar_id, write=request.method != "GET")
        event = _event_get(ctx, cal, event_id)
        if request.method == "GET":
            if event["status"] == "cancelled" and not event.get("recurringEventId"):
                raise google_error(410, "Resource has been deleted", reason="deleted")
            if request.headers.get("if-none-match") == event["etag"]:
                return Response(status_code=304, headers={"ETag": event["etag"]})
            return event_output(request, ctx, event, cal, actor, role)
        _precondition(request, event)
        if request.method == "DELETE":
            event["status"] = "cancelled"
            revision(ctx, event)
            collection = "calendar_instances" if "recurringEventId" in event else "calendar_events"
            ctx.store[collection].put_as(cal["id"] + ":" + event_id, event)
            if event.get("organizer", {}).get("email") == cal["id"]:
                for replica in ctx.store[collection].values():
                    if replica["id"] == event_id:
                        replica["status"] = "cancelled"
                        revision(ctx, replica)
                        await notify(ctx, replica["_calendar"])
            else:
                await notify(ctx, cal["id"])
            return Response(status_code=204)
        updated = await _save_event(
            request, ctx, cal, actor, await json_object(request), event, replace=request.method == "PUT"
        )
        return event_output(request, ctx, updated, cal, actor, role)

    @app.get(PREFIX + "/calendars/{calendar_id}/events/{event_id}/instances")
    async def events_instances(request: Request, calendar_id: str, event_id: str) -> dict[str, Any]:
        cal, role, actor = calendar_access(request, ctx, calendar_id)
        master = _event_get(ctx, cal, event_id)
        if "recurrence" not in master:
            raise google_error(400, "Not a recurring event", reason="badRequest")
        try:
            low = (
                parse_instant(request.query_params["timeMin"]) if "timeMin" in request.query_params else None
            )
            high = (
                parse_instant(request.query_params["timeMax"]) if "timeMax" in request.query_params else None
            )
        except ValueError as exc:
            raise google_error(400, "Invalid time range", reason="badRequest") from exc
        rows = [
            r
            for r in expand(ctx, master, low, high)
            if (not low or instant(r["end"]) > low)
            and (not high or instant(r["start"]) < high)
            and (request.query_params.get("showDeleted") == "true" or r["status"] != "cancelled")
        ]
        if request.query_params.get("originalStart"):
            original = parse_instant(request.query_params["originalStart"])
            rows = [r for r in rows if instant(r["originalStartTime"]) == original]
        rows, token = page(request, rows, binding=actor)
        return {
            "kind": "calendar#events",
            "etag": master["etag"],
            "items": [event_output(request, ctx, r, cal, actor, role) for r in rows],
            **({"nextPageToken": token} if token else {}),
        }

    @app.post(PREFIX + "/calendars/{calendar_id}/events/{event_id}/move")
    async def events_move(request: Request, calendar_id: str, event_id: str) -> dict[str, Any]:
        source, _, _ = calendar_access(request, ctx, calendar_id, write=True)
        target, role, actor = calendar_access(
            request, ctx, request.query_params.get("destination", ""), write=True
        )
        event = _event_get(ctx, source, event_id)
        if event.get("organizer", {}).get("email") != source["id"]:
            raise google_error(403, "Only the organizer can move an event", reason="forbidden")
        if "recurringEventId" in event or source["id"] == target["id"]:
            raise google_error(400, "Invalid event move", reason="badRequest")
        replica = copy.deepcopy(event)
        replica["_calendar"] = target["id"]
        replica["organizer"] = {"email": target["id"]}
        revision(ctx, replica)
        ctx.store["calendar_events"].put_as(target["id"] + ":" + event_id, replica)
        for instance in ctx.store["calendar_instances"].where(
            _calendar=source["id"], recurringEventId=event_id
        ):
            target_instance = copy.deepcopy(instance)
            target_instance["_calendar"] = target["id"]
            target_instance["organizer"] = {"email": target["id"]}
            revision(ctx, target_instance)
            ctx.store["calendar_instances"].put_as(target["id"] + ":" + instance["id"], target_instance)
            instance["status"] = "cancelled"
            revision(ctx, instance)
        for collection in ("calendar_events", "calendar_instances"):
            for invitation in ctx.store[collection].values():
                if invitation["_calendar"] not in (source["id"], target["id"]) and (
                    invitation["id"] == event_id or invitation.get("recurringEventId") == event_id
                ):
                    invitation["organizer"] = {"email": target["id"]}
                    revision(ctx, invitation)
                    await notify(ctx, invitation["_calendar"])
        event["status"] = "cancelled"
        revision(ctx, event)
        await notify(ctx, source["id"])
        await notify(ctx, target["id"])
        return event_output(request, ctx, replica, target, actor, role)

    @app.post(PREFIX + "/channels/stop")
    async def channels_stop(request: Request) -> Response:
        actor = identity(request, ctx)
        body = await json_object(request)
        channel = ctx.store["calendar_channels"].get(body.get("id"))
        if channel and channel["_actor"] == actor and channel["resourceId"] == body.get("resourceId"):
            ctx.store["calendar_channels"].delete(channel["id"])
        elif channel:
            raise google_error(403, "Forbidden", reason="forbidden")
        return Response(status_code=204)

    @app.post(PREFIX + "/freeBusy")
    async def free_busy(request: Request) -> dict[str, Any]:
        identity(request, ctx)
        body = await json_object(request)
        try:
            low, high = parse_instant(body["timeMin"]), parse_instant(body["timeMax"])
            if low >= high:
                raise ValueError
        except (KeyError, ValueError) as exc:
            raise google_error(400, "Invalid time range", reason="badRequest") from exc
        result = {
            "kind": "calendar#freeBusy",
            "timeMin": isoformat_z(low),
            "timeMax": isoformat_z(high),
            "calendars": {},
        }
        from emulators.core.errors import EmulatorError

        for item in body.get("items", []):
            calendar_id = item.get("id", "")
            try:
                cal, _, _ = calendar_access(request, ctx, calendar_id, busy=True)
            except EmulatorError:
                result["calendars"][calendar_id] = {
                    "errors": [{"domain": "global", "reason": "notFound"}],
                    "busy": [],
                }
                continue
            intervals = sorted(
                (max(instant(r["start"]), low), min(instant(r["end"]), high))
                for master in ctx.store["calendar_events"].where(_calendar=cal["id"])
                for r in expand(ctx, master, low, high)
                if r["status"] != "cancelled"
                and r.get("transparency") != "transparent"
                and instant(r["start"]) < high
                and instant(r["end"]) > low
            )
            merged: list[tuple[Any, Any]] = []
            for start, end in intervals:
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            result["calendars"][calendar_id] = {
                "busy": [{"start": isoformat_z(s), "end": isoformat_z(e)} for s, e in merged]
            }
        return result

    @app.get(PREFIX + "/colors")
    async def colors_get(request: Request) -> dict[str, Any]:
        identity(request, ctx)
        event_colors = [
            "#a4bdfc",
            "#7ae7bf",
            "#dbadff",
            "#ff887c",
            "#fbd75b",
            "#ffb878",
            "#46d6db",
            "#e1e1e1",
            "#5484ed",
            "#51b749",
            "#dc2127",
        ]
        calendar_colors = [
            "#ac725e",
            "#d06b64",
            "#f83a22",
            "#fa573c",
            "#ff7537",
            "#ffad46",
            "#42d692",
            "#16a765",
            "#7bd148",
            "#b3dc6c",
            "#fbe983",
            "#fad165",
            "#92e1c0",
            "#9fe1e7",
            "#9fc6e7",
            "#4986e7",
            "#9a9cff",
            "#b99aff",
            "#c2c2c2",
            "#cabdbf",
            "#cca6ac",
            "#f691b2",
            "#cd74e6",
            "#a47ae2",
        ]
        return {
            "kind": "calendar#colors",
            "updated": "2012-02-14T00:00:00.000Z",
            "calendar": {
                str(i + 1): {"background": color, "foreground": "#1d1d1d"}
                for i, color in enumerate(calendar_colors)
            },
            "event": {
                str(i + 1): {"background": color, "foreground": "#1d1d1d"}
                for i, color in enumerate(event_colors)
            },
        }

    def settings(actor: str) -> list[dict[str, Any]]:
        person = ctx.world.person(actor)
        values = {
            "timezone": person.timezone,
            "locale": person.locale.replace("-", "_"),
            "dateFieldOrder": "MDY",
            "format24HourTime": "false",
            "weekStart": "0",
            "defaultEventLength": "60",
            "hideInvitations": "false",
            "showDeclinedEvents": "true",
        }
        return [
            {
                "kind": "calendar#setting",
                "id": key,
                "value": value,
                "etag": '"' + stable_hash(key, value) + '"',
            }
            for key, value in values.items()
        ]

    @app.get(PREFIX + "/users/me/settings")
    async def settings_list(request: Request) -> dict[str, Any]:
        return {"kind": "calendar#settings", "etag": '"settings"', "items": settings(identity(request, ctx))}

    @app.get(PREFIX + "/users/me/settings/{setting_id}")
    async def settings_get(request: Request, setting_id: str) -> dict[str, Any]:
        row = next((r for r in settings(identity(request, ctx)) if r["id"] == setting_id), None)
        if row is None:
            raise google_error(404, "Not Found", reason="notFound")
        return row
