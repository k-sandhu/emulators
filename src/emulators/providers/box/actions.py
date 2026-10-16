"""``/_emulator/actions/*``: simulations and test configuration specific to Box."""

from __future__ import annotations

import base64
from typing import Any

from fastapi import FastAPI

from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.providers.box import access, db, events, oauth, ops
from emulators.providers.box.access import Principal
from emulators.providers.box.util import now_iso, validate_name

Record = dict[str, Any]


def fail(status: int, message: str) -> EmulatorError:
    return EmulatorError(status, {"error": message})


def resolve_user(ctx: ProviderContext, identifier: Any) -> Record:
    if identifier in (None, "", "enterprise", "service"):
        return oauth.service_account(ctx)
    user = access.lookup_user(ctx, str(identifier))
    if user is None:
        raise fail(404, f"unknown user '{identifier}'")
    return user


def whole_number(payload: dict[str, Any], name: str, default: int) -> int:
    value = payload.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise fail(400, f"{name} must be an integer")
    return value


def principal_for(ctx: ProviderContext, identifier: Any) -> Principal:
    user = resolve_user(ctx, identifier)
    return Principal(ctx, user, user)


def register(app: FastAPI, ctx: ProviderContext) -> None:
    provider = ctx.provider

    @provider.action("mint_token")
    def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Mint a Box access token: ``{"user": "<box id|p-id|email>|enterprise", "expires_in": 3600, "refresh": false}``."""
        user = resolve_user(ctx, payload.get("user"))
        client_id = payload.get("client_id") or "emulator-client"
        if not isinstance(client_id, str):
            raise fail(400, "client_id must be a string")
        body = oauth.issue_tokens(
            ctx,
            user,
            client_id,
            refresh=bool(payload.get("refresh", False)),
            ttl_seconds=whole_number(payload, "expires_in", oauth.ACCESS_TTL),
        )
        body["user_id"] = user["id"]
        return body

    @provider.action("configure_webhook_keys")
    def configure_webhook_keys(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Set the webhook signature keys the 'developer console' would hold: ``{"primary", "secondary"}``."""
        keys = events.webhook_keys(ctx)
        for name in ("primary", "secondary"):
            if name in payload:
                if payload[name] is not None and not isinstance(payload[name], str):
                    raise fail(400, f"{name} must be a string")
                keys[name] = payload[name] or ""
        return dict(keys)

    @provider.action("configure_uploads")
    def configure_uploads(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Change the chunked-upload ``part_size`` (bytes) so tests can exercise multi-part uploads cheaply."""
        settings = ctx.store.meta.setdefault("settings", {})
        if "part_size" in payload:
            size = whole_number(payload, "part_size", 0)
            if size < 1:
                raise fail(400, "part_size must be positive")
            settings["upload_part_size"] = size
        return dict(settings)

    @provider.action("simulate_upload")
    async def simulate_upload(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Someone else uploads via the web app: ``{"user", "parent_id", "name", "content"|"content_base64"}``.

        Creates the file (or a new version when the name exists) and fires events and webhooks.
        """
        p = principal_for(ctx, payload.get("user"))
        parent = p.get_item("folder", str(payload.get("parent_id", "0")), access.UPLOAD)
        name = validate_name(payload.get("name"))
        if "content_base64" in payload:
            try:
                data = base64.b64decode(str(payload["content_base64"]), validate=True)
            except ValueError:
                raise fail(400, "content_base64 is not valid base64") from None
        else:
            data = str(payload.get("content", "")).encode()
        existing = p.tree.find_by_name(parent["id"], name)
        now = now_iso(ctx)
        if existing is not None and existing["type"] == "file":
            ops.add_version(p, existing, data, now)
            record = existing
        else:
            record = ops.create_file(p, parent, name, data, now)
        await events.emit(p, "ITEM_UPLOAD", record)
        return {
            "id": record["id"],
            "name": record["name"],
            "version": record["_version"],
            "size": record["size"],
        }

    @provider.action("accept_collaboration")
    async def accept_collaboration(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """The invitee accepts a pending invitation: ``{"collaboration_id"}``; fires COLLABORATION.ACCEPTED."""
        collab = db.collabs(ctx).get(str(payload.get("collaboration_id", "")))
        if collab is None:
            raise fail(404, "unknown collaboration")
        collab["status"] = "accepted"
        collab["acknowledged_at"] = now_iso(ctx)
        collab["modified_at"] = collab["acknowledged_at"]
        item = db.item(ctx, collab["item_type"], collab["item_id"])
        if item is None:
            raise fail(404, "collaboration item is gone")
        actor = db.users(ctx).get(collab["accessible_id"]) if collab["accessible_type"] == "user" else None
        if actor is None:
            return {"status": collab["status"]}
        p = Principal(ctx, actor, actor)
        from emulators.providers.box.shapes import Shaper

        source = Shaper(p).collaboration(collab)
        await events.emit(
            p, "COLLABORATION_ACCEPT", None, source=source, kind="collaboration", webhook_item=item
        )
        return {"status": collab["status"], "id": collab["id"]}

    @provider.action("expire_token")
    def expire_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Revoke an access or refresh token: ``{"token"}`` (simulates expiry or revocation)."""
        token = str(payload.get("token", ""))
        return {"revoked": ctx.tokens.revoke(token)}
