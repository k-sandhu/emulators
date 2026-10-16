"""App subscription matching and HubSpot v1/v3 webhook signatures."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import Any
from urllib.parse import urlparse

from emulators.core.provider import ProviderContext

from .common import APP_ID, PORTAL_ID, fail

SINGULAR = {"companies": "company", "contacts": "contact", "deals": "deal", "tickets": "ticket"}
DECODE_URI = {"3A": ":", "2F": "/", "3F": "?", "40": "@", "21": "!", "24": "$", "27": "'", "28": "(", "29": ")", "2A": "*", "2C": ",", "3B": ";"}


def configure(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Configure app-level subscriptions with a target_url, client_secret and subscriptions."""
    url = payload.get("target_url", "")
    if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        fail(400, "A valid target_url is required.")
    if not isinstance(payload.get("client_secret"), str) or not payload["client_secret"]:
        fail(400, "client_secret is required.")
    subscriptions = payload.get("subscriptions", [])
    if not isinstance(subscriptions, list):
        fail(400, "subscriptions must be an array.")
    cleaned = []
    for i, sub in enumerate(subscriptions):
        if isinstance(sub, str):
            sub = {"subscriptionType": sub}
        if not isinstance(sub, dict):
            fail(400, "Invalid subscription.")
        subscription_type = sub.get("subscriptionType", sub.get("eventType"))
        if subscription_type not in {f"{kind}.{event}" for kind in SINGULAR.values() for event in ("creation", "propertyChange", "deletion")}:
            fail(400, "Unsupported subscriptionType.")
        cleaned.append({**sub, "subscriptionType": subscription_type, "id": int(sub.get("id", i + 1))})
    ctx.store.meta["webhook"] = {"target_url": url, "client_secret": payload["client_secret"], "subscriptions": cleaned}
    return {"target_url": url, "subscriptions": cleaned}


async def emit(ctx: ProviderContext, changes: list[tuple[str, str, str, dict[str, str]]]) -> None:
    config = ctx.store.meta.get("webhook")
    if not config:
        return
    events = []
    now = int(ctx.clock.now().timestamp() * 1000)
    for kind, rid, operation, props in changes:
        if kind not in SINGULAR:
            continue
        event_type = SINGULAR[kind] + "." + operation
        for sub in config["subscriptions"]:
            if sub["subscriptionType"] != event_type or not sub.get("active", True):
                continue
            values = props.items() if operation == "propertyChange" else [(None, None)]
            for prop, value in values:
                if prop and sub.get("propertyName") and sub["propertyName"] != prop:
                    continue
                ctx.store.meta["event_counter"] = ctx.store.meta.get("event_counter", 0) + 1
                event = {"eventId": ctx.store.meta["event_counter"], "subscriptionId": sub["id"],
                    "portalId": PORTAL_ID, "appId": APP_ID, "occurredAt": now,
                    "subscriptionType": event_type, "attemptNumber": 0, "objectId": int(rid),
                    "changeSource": "CRM", "sourceId": str(APP_ID)}
                if prop is not None:
                    event.update(propertyName=prop, propertyValue=value)
                events.append(event)
    target = config["target_url"]
    uri = re.sub(r"%([0-9a-fA-F]{2})", lambda m: DECODE_URI.get(m[1].upper(), m[0]), target)
    secret = config["client_secret"]
    for offset in range(0, len(events), 100):
        content = json.dumps(events[offset:offset + 100], separators=(",", ":")).encode()

        def sign(raw: bytes, attempt: int) -> dict[str, str]:
            timestamp = str(int(ctx.clock.now().timestamp() * 1000))
            signed = b"POST" + uri.encode() + raw + timestamp.encode()
            return {"X-HubSpot-Signature-v3": base64.b64encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest()).decode(),
                    "X-HubSpot-Request-Timestamp": timestamp,
                    "X-HubSpot-Signature": hashlib.sha256(secret.encode() + raw).hexdigest(),
                    "X-HubSpot-Signature-Version": "v1"}

        await ctx.webhooks.deliver(target, content, event_type="hubspot.batch", sign=sign)
