"""Zoom event subscriptions: endpoint URL validation handshake, signed deliveries, actions.

Real Zoom configures subscriptions in the Marketplace app (not via the API), so here they are
configured with the ``configure_webhook`` action. Deliveries POST::

    {"event": "meeting.ended", "event_ts": 1760000000000, "payload": {"account_id": ..., "object": {...}}}

with ``x-zm-request-timestamp`` and ``x-zm-signature: v0=<hex hmac_sha256(secret, "v0:{ts}:{body}")>``.
The body is compact UTF-8 JSON (no ASCII escaping) so a consumer that re-serialises the parsed
JSON, as Zoom's own verification sample does, computes the same signature.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

import httpx

from emulators.core.errors import EmulatorError
from emulators.core.provider import Provider, ProviderContext
from emulators.core.webhooks import hmac_sha256_hex
from emulators.providers.zoom.common import now_ms, tracking_id, zoom_token, zoom_user_id
from emulators.world.model import Person

EVENT_NAMES = (
    "meeting.created",
    "meeting.updated",
    "meeting.deleted",
    "meeting.started",
    "meeting.ended",
    "meeting.participant_joined",
    "meeting.participant_left",
    "recording.completed",
    "recording.transcript_completed",
    "recording.trashed",
    "recording.deleted",
    "recording.recovered",
    "user.created",
    "user.updated",
    "user.deleted",
    "user.activated",
    "user.deactivated",
)
ENDPOINTS = "webhook_endpoints"
DOWNLOAD_TOKEN_TTL_S = 86400
_ZOOM_URL = re.compile(r"https://((?:[a-z0-9-]+\.)*zoom\.us)")


def issue_download_token(ctx: ProviderContext, uuid: str, ttl_s: int = DOWNLOAD_TOKEN_TTL_S) -> str:
    """A ``download_access_token`` / webhook ``download_token``: usable as Bearer or ``?access_token=``
    on that meeting's recording download URLs only."""
    token = zoom_token(
        ctx, kind="download", uid=ctx.world.org.zoom_account_id, client_id="zoom-download", ttl_s=ttl_s
    )
    ctx.tokens.issue(
        "download",
        client_id="zoom-download",
        user_id=None,
        token=token,
        ttl=timedelta(seconds=ttl_s),
        extra={"uuid": uuid},
    )
    return token


def endpoints(ctx: ProviderContext) -> list[dict[str, Any]]:
    return ctx.store.collection(ENDPOINTS).values()


def _public(ctx: ProviderContext, text: str) -> str:
    """Optionally point URLs in event payloads at ``EMULATORS_ZOOM_PUBLIC_BASE`` (prefix/port mode)."""
    base = ctx.setting("public_base")
    return _ZOOM_URL.sub(base.rstrip("/"), text) if base else text


def operator_fields(ctx: ProviderContext, operator: Person | None) -> dict[str, Any]:
    if operator is None:
        return {}
    return {"operator": operator.email, "operator_id": zoom_user_id(ctx.world, operator.id)}


async def emit(
    ctx: ProviderContext,
    event: str,
    obj: dict[str, Any],
    *,
    operator: Person | None = None,
    payload_extra: dict[str, Any] | None = None,
    top_extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Deliver ``event`` to every configured endpoint subscribed to it; returns the delivery records."""
    sent: list[dict[str, Any]] = []
    for endpoint in endpoints(ctx):
        if "*" not in endpoint["events"] and event not in endpoint["events"]:
            continue
        payload: dict[str, Any] = {"account_id": ctx.world.org.zoom_account_id}
        payload.update(operator_fields(ctx, operator))
        payload.update(payload_extra or {})
        payload["object"] = obj
        body = {"event": event, "payload": payload, "event_ts": now_ms(ctx), **(top_extra or {})}
        raw = _public(ctx, json.dumps(body, separators=(",", ":"), ensure_ascii=False)).encode("utf-8")
        secret = endpoint["secret_token"]

        def sign(data: bytes, _attempt: int, secret: str = secret) -> dict[str, str]:
            ts = str(ctx.clock.timestamp())
            return {
                "x-zm-request-timestamp": ts,
                "x-zm-signature": "v0=" + hmac_sha256_hex(secret, f"v0:{ts}:{data.decode('utf-8')}"),
            }

        record = await ctx.webhooks.deliver(
            endpoint["url"],
            raw,
            headers={
                "user-agent": "Zoom Marketplace/1.0a",
                "x-zm-trackingid": tracking_id(),
                **(
                    {"authorization": endpoint["verification_token"]}
                    if endpoint.get("verification_token")
                    else {}
                ),
            },
            event_type=event,
            content_type="application/json; charset=utf-8",
            metadata={"endpoint_id": endpoint["id"]},
            sign=sign,
        )
        sent.append(record)
    return sent


async def validate_endpoint(ctx: ProviderContext, url: str, secret: str) -> None:
    """The ``endpoint.url_validation`` challenge: the consumer must echo an HMAC of ``plainToken``."""
    plain = ctx.ids.urlsafe(22)
    body = json.dumps(
        {"payload": {"plainToken": plain}, "event_ts": now_ms(ctx), "event": "endpoint.url_validation"},
        separators=(",", ":"),
    )
    ts = str(ctx.clock.timestamp())
    headers = {
        "content-type": "application/json; charset=utf-8",
        "x-zm-request-timestamp": ts,
        "x-zm-signature": "v0=" + hmac_sha256_hex(secret, f"v0:{ts}:{body}"),
    }
    try:
        response = await ctx.webhooks.request("POST", url, headers=headers, content=body.encode())
    except httpx.HTTPError as exc:
        raise EmulatorError(
            400, {"error": "endpoint_validation_failed", "detail": f"{type(exc).__name__}: {exc}"}
        ) from exc
    try:
        answer = response.json()
    except ValueError:
        answer = {}
    expected = hmac_sha256_hex(secret, plain)
    if response.status_code != 200 or not isinstance(answer, dict):
        raise EmulatorError(
            400,
            {
                "error": "endpoint_validation_failed",
                "detail": f"endpoint answered HTTP {response.status_code}",
            },
        )
    if answer.get("plainToken") != plain or answer.get("encryptedToken") != expected:
        raise EmulatorError(
            400,
            {
                "error": "endpoint_validation_failed",
                "detail": "response must be {plainToken, encryptedToken=hex(hmac_sha256(secret_token, plainToken))}",
            },
        )


def actions(provider: Provider) -> None:
    @provider.action("configure_webhook")
    async def configure_webhook(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Subscribe an endpoint: ``{url, secret_token, events?, skip_validation?, verification_token?}``.

        Runs Zoom's ``endpoint.url_validation`` handshake first (the endpoint must answer
        ``{plainToken, encryptedToken}``) and refuses to save the subscription if it fails.
        ``events`` defaults to every event. Re-posting the same ``url`` replaces the subscription."""
        url = payload.get("url")
        if not url:
            raise EmulatorError(400, {"error": "url is required"})
        secret = payload.get("secret_token") or ctx.ids.urlsafe(22)
        events = payload.get("events") or ["*"]
        if isinstance(events, str):
            events = [events]
        unknown = [e for e in events if e != "*" and e not in EVENT_NAMES]
        if unknown:
            raise EmulatorError(400, {"error": f"unknown event(s) {unknown}", "events": list(EVENT_NAMES)})
        if not payload.get("skip_validation"):
            await validate_endpoint(ctx, url, secret)
        existing = next((e for e in endpoints(ctx) if e["url"] == url), None)
        record = {
            "id": existing["id"] if existing else ctx.ids.prefixed("whe", 12),
            "url": url,
            "secret_token": secret,
            "events": list(events),
            "validated": not payload.get("skip_validation"),
        }
        if payload.get("verification_token"):  # legacy: sent verbatim in the Authorization header
            record["verification_token"] = str(payload["verification_token"])
        ctx.store.collection(ENDPOINTS).put(record)
        return {**record, "events": list(EVENT_NAMES) if "*" in events else record["events"]}

    @provider.action("remove_webhook")
    def remove_webhook(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete a subscription by ``id`` or ``url``."""
        removed = 0
        for endpoint in endpoints(ctx):
            if payload.get("id") in (None, endpoint["id"]) and payload.get("url") in (None, endpoint["url"]):
                ctx.store.collection(ENDPOINTS).delete(endpoint["id"])
                removed += 1
        return {"removed": removed}
