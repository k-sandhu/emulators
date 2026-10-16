"""Mailbox settings persist independently for each authenticated user."""

from __future__ import annotations

import copy
from typing import Any

from fastapi import FastAPI, Request, Response

from emulators.core.provider import ProviderContext
from emulators.providers.google.common import google_error, json_object
from emulators.providers.google.gmail_state import mailbox

PREFIX = "/gmail/v1/users/{user_id}/settings"
DEFAULTS: dict[str, dict[str, Any]] = {
    "vacation": {"enableAutoReply": False, "restrictToContacts": False, "restrictToDomain": False},
    "autoForwarding": {"enabled": False, "disposition": "leaveInInbox"},
    "imap": {"enabled": True, "autoExpunge": True, "expungeBehavior": "archive", "maxFolderSize": 0},
    "pop": {"accessWindow": "disabled", "disposition": "leaveInInbox"},
}


def send_as_rows(ctx: ProviderContext, owner: str) -> list[dict[str, Any]]:
    person = ctx.world.person(owner)
    primary = {
        "sendAsEmail": person.email,
        "displayName": person.display_name,
        "replyToAddress": person.email,
        "signature": "",
        "isPrimary": True,
        "isDefault": True,
        "treatAsAlias": False,
        "verificationStatus": "accepted",
    }
    records = ctx.store["gmail_send_as:" + owner]
    if not records.get(person.email):
        records.put_as(person.email, primary)
    return copy.deepcopy(records.values())


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.api_route(PREFIX + "/{setting}", methods=["PUT"])
    async def update(request: Request, user_id: str, setting: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        if setting not in DEFAULTS:
            raise google_error(404, "Not Found", reason="notFound")
        value = {**DEFAULTS[setting], **await json_object(request)}
        if setting == "autoForwarding" and value.get("enabled"):
            address = ctx.store["gmail_forwarding:" + owner].get(value.get("emailAddress"))
            if not address or address["verificationStatus"] != "accepted":
                raise google_error(400, "Forwarding address not verified", reason="invalidArgument")
        ctx.store["gmail_settings:" + owner].put_as(setting, value)
        return copy.deepcopy(value)

    async def get(request: Request, user_id: str, setting: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        if setting not in DEFAULTS:
            raise google_error(404, "Not Found", reason="notFound")
        return copy.deepcopy(ctx.store["gmail_settings:" + owner].get(setting) or DEFAULTS[setting])

    @app.api_route(PREFIX + "/filters/{filter_id}", methods=["GET", "DELETE"])
    async def filter_item(request: Request, user_id: str, filter_id: str) -> Any:
        owner = mailbox(request, ctx, user_id)
        record = ctx.store["gmail_filters:" + owner].get(filter_id)
        if not record:
            raise google_error(404, "Not Found", reason="notFound")
        if request.method == "DELETE":
            ctx.store["gmail_filters:" + owner].delete(filter_id)
            return Response(status_code=204)
        return copy.deepcopy(record)

    @app.api_route(PREFIX + "/forwardingAddresses", methods=["GET", "POST"])
    async def forwarding(request: Request, user_id: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        records = ctx.store["gmail_forwarding:" + owner]
        if request.method == "GET":
            return {"forwardingAddresses": copy.deepcopy(records.values())}
        body = await json_object(request)
        address = body.get("forwardingEmail")
        if not address or "@" not in address:
            raise google_error(400, "Invalid forwarding address", reason="invalidArgument")
        if records.get(address):
            raise google_error(409, "Forwarding address already exists", reason="duplicate")
        record = {"forwardingEmail": address, "verificationStatus": "pending"}
        records.put_as(address, record)
        return copy.deepcopy(record)

    @app.api_route(PREFIX + "/forwardingAddresses/{email}", methods=["GET", "DELETE"])
    async def forwarding_item(request: Request, user_id: str, email: str) -> Any:
        owner = mailbox(request, ctx, user_id)
        records = ctx.store["gmail_forwarding:" + owner]
        record = records.get(email)
        if not record:
            raise google_error(404, "Not Found", reason="notFound")
        if request.method == "DELETE":
            records.delete(email)
            return Response(status_code=204)
        return copy.deepcopy(record)

    @app.post(PREFIX + "/sendAs")
    async def send_as_create(request: Request, user_id: str) -> dict[str, Any]:
        owner = mailbox(request, ctx, user_id)
        send_as_rows(ctx, owner)
        body = await json_object(request)
        address = body.get("sendAsEmail")
        if not address or "@" not in address:
            raise google_error(400, "Invalid send-as address", reason="invalidArgument")
        records = ctx.store["gmail_send_as:" + owner]
        if records.get(address):
            raise google_error(409, "Send-as address already exists", reason="duplicate")
        user = ctx.store["directory_users"].first(_person=owner) or {}
        record = {
            **body,
            "isPrimary": False,
            "isDefault": False,
            "verificationStatus": "accepted" if address in user.get("aliases", []) else "pending",
        }
        records.put_as(address, record)
        return copy.deepcopy(record)

    @app.api_route(PREFIX + "/sendAs/{email}", methods=["GET", "PUT", "PATCH", "DELETE"])
    async def send_as_item(request: Request, user_id: str, email: str) -> Any:
        owner = mailbox(request, ctx, user_id)
        send_as_rows(ctx, owner)
        records = ctx.store["gmail_send_as:" + owner]
        record = records.get(email)
        if not record:
            raise google_error(404, "Not Found", reason="notFound")
        if request.method == "DELETE":
            if record["isPrimary"]:
                raise google_error(400, "Cannot delete primary send-as address", reason="invalidArgument")
            records.delete(email)
            return Response(status_code=204)
        if request.method in ("PATCH", "PUT"):
            body = await json_object(request)
            if body.get("isDefault"):
                for other in records:
                    other["isDefault"] = False
            for key in ("displayName", "replyToAddress", "signature", "isDefault", "treatAsAlias", "smtpMsa"):
                if key in body:
                    record[key] = body[key]
        return copy.deepcopy(record)

    app.add_api_route(PREFIX + "/{setting}", get, methods=["GET"])
