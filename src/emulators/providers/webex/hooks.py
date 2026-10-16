"""Owner-scoped webhook registrations and Webex's raw-body SHA-1 signature."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any
from urllib.parse import parse_qsl, urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext

from .common import (
    admin_scope,
    authenticate,
    body,
    bot_message_visible,
    fail,
    is_admin,
    json_bytes,
    meeting_access,
    opaque,
    page,
    person,
    public,
    required,
    stamp,
    subject,
)
from .seed import add_artifacts, org_id

EVENTS = {
    "meetings": {"created", "updated", "deleted", "started", "ended", "all"},
    "recordings": {"created", "updated", "deleted", "all"},
    "meetingTranscripts": {"created", "updated", "deleted", "all"},
    "messages": {"created", "updated", "deleted", "all"},
    "memberships": {"created", "updated", "deleted", "all"},
    "rooms": {"created", "updated", "deleted", "all"},
}


def owner(ctx: ProviderContext, grant: Grant) -> str:
    return (
        f"app:{grant.client_id}" if grant.app_only else f"{subject(ctx, grant)['id']}/app:{grant.client_id}"
    )


def visible_event(ctx: ProviderContext, grant: Grant, resource: str, data: dict[str, Any]) -> bool:
    if resource in ("meetings", "recordings", "meetingTranscripts"):
        meeting = (
            data if resource == "meetings" else ctx.store.collection("meetings").get(data.get("meetingId"))
        )
        area = {"meetings": "schedules", "recordings": "recordings", "meetingTranscripts": "transcripts"}[
            resource
        ]
        return bool(meeting and meeting_access(ctx, grant, meeting, area))
    if grant.app_only or not grant.has_scope("spark:all", f"spark:{resource}_read"):
        return False
    user = subject(ctx, grant)
    if resource == "messages" and not bot_message_visible(user, data):
        return False
    room_id = data["id"] if resource == "rooms" else data.get("roomId")
    return bool(ctx.store.collection("memberships").first(roomId=room_id, personId=user["id"]))


async def emit(ctx: ProviderContext, resource: str, event: str, data: dict[str, Any], *, actor: str) -> None:
    for hook in ctx.store.collection("webhooks"):
        if hook["status"] != "active" or hook["resource"] != resource or hook["event"] not in (event, "all"):
            continue
        grant = Grant.from_record(hook["_grant"])
        user = person(ctx, grant.user_id)
        if not grant.app_only and (not user or not user.get("_loginEnabled", True)):
            hook["status"] = "inactive"
            continue
        if not visible_event(ctx, grant, resource, data):
            continue
        filter_data = dict(data)
        if resource in ("meetings", "recordings", "meetingTranscripts"):
            meeting = (
                data
                if resource == "meetings"
                else ctx.store.collection("meetings").get(data.get("meetingId"))
            )
            assert meeting is not None
            filter_data.update(hostEmail=meeting["hostEmail"], hostUserId=meeting["hostUserId"])
        filters = dict(parse_qsl(hook.get("filter", "")))
        filter_data.update(hasFiles=bool(data.get("files")), hasAttachments=bool(data.get("attachments")))
        if not all(
            ((grant.user_id if v == "me" else v) in data.get("mentionedPeople", []))
            if k == "mentionedPeople"
            else str(filter_data.get(k, "")).lower() == v.lower()
            for k, v in filters.items()
        ):
            continue
        payload = {
            k: hook[k]
            for k in (
                "id",
                "name",
                "targetUrl",
                "resource",
                "orgId",
                "createdBy",
                "appId",
                "ownedBy",
                "status",
            )
        }
        event_data = public(data)
        if resource == "messages":
            event_data = {
                k: v for k, v in event_data.items() if k not in ("text", "markdown", "html", "files")
            }
        if resource == "rooms":
            event_data.pop("title", None)
        if resource in ("meetings", "recordings", "meetingTranscripts"):
            event_data.update(hostEmail=meeting["hostEmail"])
            if resource == "meetings":
                event_data.update(meetingId=data["id"], hostPersonId=meeting["hostUserId"])
            else:
                event_data.update(hostUserId=meeting["hostUserId"])
        payload.update(event=event, actorId=actor, data=event_data, created=hook["created"])
        raw = json_bytes(payload)
        headers = {}
        if hook.get("secret"):
            headers["X-Spark-Signature"] = hmac.new(hook["secret"].encode(), raw, hashlib.sha1).hexdigest()
        await ctx.webhooks.deliver(
            hook["targetUrl"],
            raw,
            headers=headers,
            event_type=f"{resource}.{event}",
            metadata={"webhookId": hook["id"]},
        )


async def end_meeting(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """End a scheduled meeting, create its occurrence and publish importable artifacts."""
    required(payload, "meeting_id")
    row = ctx.store.collection("meetings").get(payload["meeting_id"])
    if not row:
        fail(404)
    if row["meetingType"] == "meetingSeries":
        if row.get("recurrence"):
            fail(400, "End an occurrence or scheduled meeting, rather than a recurring series.")
        row = ctx.store.collection("meetings").get(row["_scheduledId"])
        if not row:
            fail(404)
    if row.get("_endedInstance"):
        instance = ctx.store.collection("meetings").get(row["_endedInstance"])
        assert instance is not None
    elif row["meetingType"] == "meeting":
        instance = row
        if instance["state"] != "ended":
            instance.update(state="ended", end=stamp(ctx.clock.now()))
    else:
        from datetime import timedelta

        from .common import instant

        now = ctx.clock.now()
        start = min(instant(row["start"], "start"), now - timedelta(minutes=1))
        key = f"{row['_seriesId']}_I_{int(start.timestamp() * 1000)}"
        instance = {
            **row,
            "id": key,
            "meetingType": "meeting",
            "state": "ended",
            "start": stamp(start),
            "end": stamp(now),
            "scheduledMeetingId": row["id"],
        }
        ctx.store.collection("meetings").put(instance)
        row.update(state="ended", _endedInstance=key)
        series = ctx.store.collection("meetings").get(row["_seriesId"])
        if series and not series.get("recurrence"):
            series["state"] = "expired"
    existed = ctx.store.collection("recordings").first(meetingId=instance["id"])
    recording, transcript = add_artifacts(ctx, instance)
    if not existed:
        recording["createTime"] = stamp(ctx.clock.now())
        for resource, event, data in (
            ("meetings", "ended", instance),
            ("recordings", "created", recording),
            ("meetingTranscripts", "created", transcript),
        ):
            await emit(ctx, resource, event, data, actor=instance["hostUserId"])
    return {"meeting": public(instance), "recording": public(recording), "transcript": public(transcript)}


def validate(payload: dict[str, Any]) -> None:
    required(payload, "name", "targetUrl", "resource", "event")
    if payload["resource"] not in EVENTS or payload["event"] not in EVENTS[payload["resource"]]:
        fail(400, "Unsupported webhook resource or event.")
    try:
        parsed = urlparse(payload["targetUrl"])
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in payload["targetUrl"])
        ):
            raise ValueError
        _ = parsed.port
        parsed.hostname.encode("idna")
        httpx.URL(payload["targetUrl"])
    except (ValueError, UnicodeError, httpx.InvalidURL):
        fail(400, "targetUrl must be an absolute HTTP or HTTPS URL.")
    if "filter" in payload and not isinstance(payload["filter"], str):
        fail(400, "filter must be a query string.")
    if "secret" in payload and not isinstance(payload["secret"], str):
        fail(400, "secret must be a string.")
    if payload.get("status", "active") != "active":
        fail(400, "Invalid webhook status.")
    if payload.get("ownedBy", "creator") not in ("creator", "org"):
        fail(400, "ownedBy must be creator or org.")


def register(app: FastAPI, ctx: ProviderContext) -> None:
    def authorize(grant: Grant, *, write: bool = False) -> None:
        if not grant.has_scope(
            "spark:all",
            f"spark:webhooks_{'write' if write else 'read'}",
            "meeting:schedules_read",
            "meeting:recordings_read",
            "meeting:transcripts_read",
            "meeting:admin_schedule_read",
            "meeting:admin_recordings_read",
            "meeting:admin_transcripts_read",
            "spark-compliance:meetings_read",
            "spark:messages_read",
            "spark:rooms_read",
            "spark:memberships_read",
        ):
            fail(403, "The access token does not have the required webhook scope.")

    def owned(key: str, grant: Grant) -> dict[str, Any]:
        row = ctx.store.collection("webhooks").get(key)
        if not row or row["_owner"] != owner(ctx, grant):
            fail(404)
        return row

    @app.post("/v1/webhooks")
    async def create(request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        payload = await body(request)
        validate(payload)
        resource = payload["resource"]
        scopes = {
            "meetings": ["meeting:schedules_read", "meeting:admin_schedule_read"],
            "recordings": ["meeting:recordings_read", "meeting:admin_recordings_read"],
            "meetingTranscripts": ["meeting:transcripts_read", "meeting:admin_transcripts_read"],
        }.get(resource, ["spark:all", f"spark:{resource}_read"])
        if resource in ("meetings", "recordings", "meetingTranscripts"):
            scopes.append("spark-compliance:meetings_read")
        if not grant.has_scope(*scopes):
            fail(403, "The access token does not have the required webhook resource scope.")
        owned_by = payload.get("ownedBy", "org" if grant.app_only else "creator")
        filters = dict(parse_qsl(payload.get("filter", "")))
        if owned_by != "org" and any(
            k in filters for k in ("hostEmail", "hostUserId", "ownerEmail", "ownerId")
        ):
            fail(400, "Host and owner filters require ownedBy=org.")
        if owned_by == "org":
            area = {
                "meetings": "schedules",
                "recordings": "recordings",
                "meetingTranscripts": "transcripts",
            }.get(resource)
            if area is None or not is_admin(ctx, grant, admin_scope(area), "spark-compliance:meetings_read"):
                fail(403, "An administrator meeting scope is required for org-owned webhooks.")
        creator = owner(ctx, grant)
        row = {
            k: payload[k]
            for k in ("name", "targetUrl", "resource", "event", "filter", "secret")
            if k in payload
        }
        row.update(
            id=opaque("WEBHOOK", ctx.ids.guid()),
            orgId=org_id(ctx),
            createdBy=opaque("APPLICATION", grant.client_id) if grant.app_only else grant.user_id,
            appId=opaque("APPLICATION", grant.client_id),
            ownedBy=owned_by,
            status="active",
            created=stamp(ctx.clock.now()),
            _owner=creator,
            _grant=grant.to_record(),
        )
        ctx.store.collection("webhooks").put(row)
        return public(row)

    @app.get("/v1/webhooks")
    async def listing(request: Request) -> JSONResponse:
        grant = authenticate(request, ctx)
        authorize(grant)
        return page(request, ctx.store.collection("webhooks").where(_owner=owner(ctx, grant)))

    @app.get("/v1/webhooks/{webhook_id}")
    async def detail(webhook_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        authorize(grant)
        return public(owned(webhook_id, grant))

    @app.put("/v1/webhooks/{webhook_id}")
    async def update(webhook_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        authorize(grant, write=True)
        row = owned(webhook_id, grant)
        fields = {
            k: v for k, v in (await body(request)).items() if k in ("name", "targetUrl", "secret", "status")
        }
        required(fields, "name", "targetUrl")
        validate({**row, **fields})
        row.update(fields)
        return public(row)

    @app.delete("/v1/webhooks/{webhook_id}")
    async def delete(webhook_id: str, request: Request) -> Response:
        grant = authenticate(request, ctx)
        authorize(grant, write=True)
        owned(webhook_id, grant)
        ctx.store.collection("webhooks").delete(webhook_id)
        return Response(status_code=204)
