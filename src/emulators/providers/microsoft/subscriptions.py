"""Graph basic change notifications and subscription lifecycle simulations."""

from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common as c
from emulators.providers.microsoft import directory_support as s
from emulators.providers.microsoft.mail_helpers import folder


def invalid(message: str) -> Any:
    return c.graph_error(400, "InvalidRequest", message)


def normalize(ctx: ProviderContext, resource: str, caller: c.Principal) -> tuple[str, int]:
    value = resource.strip("/")
    value = re.sub(r"mailFolders\('([^']+)'\)", r"mailFolders/\1", value, flags=re.I)
    if value.startswith("me/"):
        value = f"users/{c.me_user_id(caller)}/" + value[3:]
    if value in ("users", "groups"):
        if not caller.app_only:
            # Directory notifications require tenant-wide privilege in this emulator.
            person = next((p for p in ctx.world.people if p.id == caller.person_id), None)
            if not person or not person.is_admin:
                raise c.graph_error(
                    403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation."
                )
        return value, 41760
    match = re.fullmatch(
        r"users/([^/]+)/(messages|events|contacts|mailFolders/([^/]+)/messages|drive/root)", value, re.I
    )
    if match:
        owner = s.user(ctx, match[1])
        if not caller.app_only and caller.user_id != owner["id"]:
            raise c.graph_error(403, "ErrorAccessDenied", "Access is denied.")
        suffix = match[2].lower() if not match[3] else match[2]
        if match[3]:
            suffix = f"mailFolders/{folder(ctx, owner['id'], match[3])['id']}/messages"
        if suffix == "drive/root":
            pid = ctx.store.meta["ms_people"].get(owner["id"])
            drive = ctx.store["ms_drives"].first(_owner=pid, driveType="business")
            if not drive:
                raise c.not_found("drive")
            return f"drives/{drive['id']}/root", 42300
        return f"users/{owner['id']}/{suffix}", 10070
    if re.fullmatch(r"drives/[^/]+/root", value):
        drive = ctx.store["ms_drives"].get(value.split("/")[1])
        if not drive:
            raise c.not_found("drive")
        if caller.delegated:
            space = ctx.world.space(drive["_space"])
            visible = {space.owner_id}
            for pid, _role in space.members:
                visible.update(ctx.world.expand_principal(pid))
            if caller.person_id not in visible:
                raise c.access_denied()
        return value, 42300
    if re.fullmatch(r"teams/[^/]+/channels/[^/]+/messages|chats/[^/]+/messages", value):
        parts = value.split("/")
        if parts[0] == "teams":
            record = ctx.store["ms_teams_channels"].get(parts[3])
            if not record or record["_team"] != parts[1]:
                raise c.not_found("channel")
        else:
            record = ctx.store["ms_teams_chats"].get(parts[1])
        if not record:
            raise c.not_found("chat")
        if caller.delegated and caller.person_id not in record["_members"]:
            raise c.access_denied()
        return value, 4320
    if value == "communications/callRecords" or re.fullmatch(r"communications/onlineMeetings/.+", value):
        if not caller.app_only:
            raise c.access_denied()
        return value, 4230
    raise invalid("The specified resource is not supported for subscriptions.")


def expiration(ctx: ProviderContext, value: str, maximum: int) -> str:
    try:
        expires = parse_instant(value)
    except (ValueError, TypeError, AttributeError):
        raise invalid("Invalid expirationDateTime.") from None
    if expires <= ctx.clock.now() or expires > ctx.clock.now() + timedelta(minutes=maximum):
        raise invalid(f"expirationDateTime must be in the future and within {maximum} minutes.")
    return isoformat_z(max(expires, ctx.clock.now() + timedelta(minutes=45)))


async def validate(ctx: ProviderContext, target: str) -> None:
    parts = urlsplit(target)
    if parts.scheme != "https" or not parts.netloc:
        raise invalid("notificationUrl must use HTTPS.")
    token = "Validation: Testing client application reachability " + secrets.token_urlsafe(24)
    query = [*parse_qsl(parts.query, keep_blank_values=True), ("validationToken", token)]
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    try:
        response = await ctx.webhooks.request(
            "POST", url, content=b"", headers={"Content-Type": "text/plain; charset=utf-8"}
        )
    except httpx.HTTPError:
        raise invalid(
            "Subscription validation request failed. Notification endpoint must respond with 200 OK."
        ) from None
    if (
        response.status_code != 200
        or response.text != token
        or response.headers.get("content-type", "").split(";")[0].lower() != "text/plain"
    ):
        raise invalid(
            "Subscription validation request failed. Notification endpoint must echo the validation token as text/plain."
        )


@c.on_change
async def notify(ctx: ProviderContext, resource: str, change_type: str, data: dict[str, Any]) -> None:
    for subscription in ctx.store["subscriptions"]:
        if parse_instant(subscription["expirationDateTime"]) <= ctx.clock.now():
            continue
        watched = subscription["_resource"]
        observed = resource.strip("/")
        if watched.startswith("communications/onlineMeetings/") and "/onlineMeetings/" in observed:
            observed = "communications/onlineMeetings/" + observed.split("/onlineMeetings/", 1)[1]
        if watched in {"users", "groups"} and observed.rsplit("/", 1)[0] != watched:
            continue
        if observed != watched and not observed.startswith(watched + "/"):
            continue
        actual = "updated" if watched in {"users", "groups"} and change_type == "created" else change_type
        if watched == "communications/callRecords" and change_type == "created":
            actual = "created" if "created" in subscription["changeType"].split(",") else "updated"
        if actual not in subscription["changeType"].split(","):
            continue
        kind = (
            "user"
            if watched == "users"
            else "group"
            if watched == "groups"
            else "message"
            if "/mailFolders/" in watched or (watched.endswith("/messages") and watched.startswith("users/"))
            else "event"
            if watched.endswith("/events")
            else "contact"
            if watched.endswith("/contacts")
            else "driveItem"
            if watched.endswith("/root")
            else "callRecord"
            if watched.endswith("callRecords")
            else "callTranscript"
            if "transcripts" in watched
            else "callRecording"
            if "recordings" in watched
            else "chatMessage"
        )
        notification_resource = resource
        if kind in ("message", "event", "contact"):
            uid = resource.split("/")[1]
            notification_resource = f"Users/{uid}/{kind.capitalize()}s/{data['id']}"
        payload = {
            "value": [
                {
                    "subscriptionId": subscription["id"],
                    "subscriptionExpirationDateTime": subscription["expirationDateTime"],
                    "changeType": actual,
                    "resource": notification_resource,
                    "resourceData": {
                        "@odata.type": f"#microsoft.graph.{kind}",
                        "@odata.id": notification_resource,
                        "@odata.etag": data.get("@odata.etag", f'W/"{data.get("changeKey", "1")}"'),
                        "id": data["id"],
                    },
                    "clientState": subscription.get("clientState"),
                    "tenantId": c.tenant_id(ctx.world),
                }
            ]
        }
        if kind == "callRecord":
            payload["value"][0]["resourceData"]["@odata.type"] = "#Microsoft.Graph.callRecord"
            payload["value"][0]["resourceData"].pop("@odata.etag", None)
        await ctx.webhooks.deliver(
            subscription["notificationUrl"],
            json.dumps(payload).encode(),
            event_type=actual,
            metadata={"subscriptionId": subscription["id"]},
        )


def seed(ctx: ProviderContext) -> None:
    async def expire(context: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Simulate reauthorizationRequired or subscriptionRemoved lifecycle notifications."""
        sid = payload.get("subscription_id") or payload.get("id")
        record = context.store["subscriptions"].get(sid)
        if not record:
            raise c.not_found(str(sid))
        lifecycle = payload.get("lifecycleEvent", "reauthorizationRequired")
        if lifecycle not in {"reauthorizationRequired", "subscriptionRemoved"}:
            raise invalid("Unsupported lifecycleEvent.")
        target = record.get("lifecycleNotificationUrl")
        if target:
            body = {
                "value": [
                    {
                        "subscriptionId": sid,
                        "subscriptionExpirationDateTime": record["expirationDateTime"],
                        "lifecycleEvent": lifecycle,
                        "clientState": record.get("clientState"),
                        "tenantId": c.tenant_id(context.world),
                    }
                ]
            }
            await context.webhooks.deliver(target, json.dumps(body).encode(), event_type=lifecycle)
        if lifecycle == "subscriptionRemoved":
            context.store["subscriptions"].delete(sid)
        return {"id": sid, "lifecycleEvent": lifecycle}

    ctx.provider.actions["expire_subscription"] = expire


def register(app: FastAPI, ctx: ProviderContext) -> None:
    router = APIRouter()

    def get(request: Request, sid: str) -> dict[str, Any]:
        caller = c.principal(request, ctx)
        record = ctx.store["subscriptions"].get(sid)
        if (
            not record
            or record["applicationId"] != caller.client_id
            or record["creatorId"] != (caller.user_id or caller.client_id)
        ):
            raise c.not_found(sid)
        return record

    @router.post("/subscriptions")
    async def create(request: Request) -> Any:
        caller = c.principal(request, ctx)
        body = await c.json_body(request)
        if not all(body.get(k) for k in ("changeType", "notificationUrl", "resource", "expirationDateTime")):
            raise invalid("changeType, notificationUrl, resource and expirationDateTime are required.")
        if not all(
            isinstance(body[k], str)
            for k in ("changeType", "notificationUrl", "resource", "expirationDateTime")
        ):
            raise c.invalid_json()
        resource, maximum = normalize(ctx, body["resource"], caller)
        kinds = [x.strip() for x in body["changeType"].split(",")]
        allowed = (
            {"updated", "deleted"}
            if resource in {"users", "groups"}
            else {"updated"}
            if resource.endswith("/root")
            else {"created", "updated", "deleted"}
        )
        if not kinds or not set(kinds).issubset(allowed) or len(set(kinds)) != len(kinds):
            raise invalid("Unsupported changeType for this resource.")
        if "clientState" in body and not isinstance(body["clientState"], str):
            raise c.invalid_json()
        if len(body.get("clientState", "")) > 128:
            raise invalid("clientState cannot exceed 128 characters.")
        if body.get("includeResourceData"):
            raise invalid(
                "Encrypted resource data notifications are not implemented. Use basic notifications."
            )
        expires = expiration(ctx, body["expirationDateTime"], maximum)
        if (
            resource.startswith(("teams/", "chats/"))
            and parse_instant(expires) > ctx.clock.now() + timedelta(hours=1)
            and not body.get("lifecycleNotificationUrl")
        ):
            raise invalid(
                "lifecycleNotificationUrl is required for Teams subscriptions longer than one hour."
            )
        if any(
            r["_resource"] == resource
            and r["applicationId"] == caller.client_id
            and r["changeType"] == ",".join(kinds)
            and parse_instant(r["expirationDateTime"]) > ctx.clock.now()
            for r in ctx.store["subscriptions"]
        ):
            raise c.graph_error(
                409, "Conflict", "Subscription already exists for this resource and changeType."
            )
        await validate(ctx, body["notificationUrl"])
        if body.get("lifecycleNotificationUrl"):
            await validate(ctx, body["lifecycleNotificationUrl"])
        record = {
            "id": ctx.ids.guid(),
            "resource": body["resource"],
            "applicationId": caller.client_id,
            "changeType": ",".join(kinds),
            "clientState": body.get("clientState"),
            "notificationUrl": body["notificationUrl"],
            "expirationDateTime": expires,
            "creatorId": caller.user_id or caller.client_id,
            "latestSupportedTlsVersion": body.get("latestSupportedTlsVersion", "v1_2"),
            "lifecycleNotificationUrl": body.get("lifecycleNotificationUrl"),
            "includeResourceData": False,
            "_resource": resource,
            "_maximum": maximum,
        }
        ctx.store["subscriptions"].put(record)
        return JSONResponse(s.single(request, record, "subscriptions/$entity"), status_code=201)

    @router.get("/subscriptions")
    async def subscriptions(request: Request) -> Any:
        caller = c.principal(request, ctx)
        rows = [
            r
            for r in ctx.store["subscriptions"]
            if r["applicationId"] == caller.client_id
            and r["creatorId"] == (caller.user_id or caller.client_id)
            and parse_instant(r["expirationDateTime"]) > ctx.clock.now()
        ]
        return s.collection(request, rows, "subscriptions")

    @router.get("/subscriptions/{sid}")
    async def subscription(sid: str, request: Request) -> Any:
        return s.single(request, get(request, sid), "subscriptions/$entity")

    @router.patch("/subscriptions/{sid}")
    async def patch(sid: str, request: Request) -> Any:
        record = get(request, sid)
        body = await c.json_body(request)
        if "expirationDateTime" in body:
            record["expirationDateTime"] = expiration(ctx, body["expirationDateTime"], record["_maximum"])
        if "notificationUrl" in body:
            await validate(ctx, body["notificationUrl"])
            record["notificationUrl"] = body["notificationUrl"]
        return s.single(request, record, "subscriptions/$entity")

    @router.delete("/subscriptions/{sid}")
    async def delete(sid: str, request: Request) -> Response:
        get(request, sid)
        ctx.store["subscriptions"].delete(sid)
        return Response(status_code=204)

    @router.post("/subscriptions/{sid}/reauthorize")
    async def reauthorize(sid: str, request: Request) -> Response:
        get(request, sid)
        return Response(status_code=204)

    for version in c.GRAPH_VERSIONS:
        app.include_router(router, prefix=f"/{version}")
