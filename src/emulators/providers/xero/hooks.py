"""Xero's signed intent-to-receive handshake and invoice/contact notifications."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from fastapi import Request

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext, url_for_host
from emulators.core.webhooks import hmac_sha256_b64

from .common import BASE, ID_FIELDS, validation
from .validation import text


async def configure(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Configure and validate a subscriber with invalid and valid HMAC signatures."""
    url, key = payload.get("url"), payload.get("signing_key")
    if (
        not isinstance(url, str)
        or not url.startswith(("http://", "https://"))
        or not isinstance(key, str)
        or not key
    ):
        validation("url and signing_key are required.")
    text(url)
    text(key)
    ctx.store.collection("_config").delete("webhook")
    body = json.dumps(
        {"events": [], "firstEventSequence": 0, "lastEventSequence": 0, "entropy": ctx.ids.token(n=32)},
        separators=(",", ":"),
    ).encode()
    statuses = []
    try:
        for secret in [key + "-incorrect", key]:
            async with asyncio.timeout(5):
                response = await ctx.webhooks.request(
                    "POST",
                    url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "x-xero-signature": hmac_sha256_b64(secret, body),
                    },
                )
            statuses.append(response.status_code)
    except (httpx.HTTPError, TimeoutError):
        validation("Webhook intent-to-receive validation timed out or failed.")
    if statuses != [401, 200]:
        validation("Webhook must return 401 for an invalid signature and 200 for a valid signature.")
    ctx.store.collection("_config").put({"id": "webhook", "url": url, "signing_key": key, "sequence": 0})
    return {"status": "OK", "url": url, "validation_statuses": statuses}


async def notify(
    ctx: ProviderContext, request: Request, tenant: str, resource: str, record: dict[str, Any], created: bool
) -> None:
    config = ctx.store.collection("_config").get("webhook")
    if not config or resource not in {"Contacts", "Invoices"}:
        return
    config["sequence"] += 1
    sequence = config["sequence"]
    identifier = record[ID_FIELDS[resource]]
    event = {
        "resourceUrl": url_for_host(request, ctx, "api.xero.com", f"{BASE}/{resource}/{identifier}"),
        "resourceId": identifier,
        "eventDateUtc": isoformat_z(ctx.clock.now()),
        "eventType": "CREATE" if created else "UPDATE",
        "eventCategory": "INVOICE" if resource == "Invoices" else "CONTACT",
        "tenantId": tenant,
        "tenantType": "ORGANISATION",
    }
    body = json.dumps(
        {
            "events": [event],
            "firstEventSequence": sequence,
            "lastEventSequence": sequence,
            "entropy": ctx.ids.token(n=32),
        },
        separators=(",", ":"),
    ).encode()
    await ctx.webhooks.deliver(
        config["url"],
        body,
        headers={"x-xero-signature": hmac_sha256_b64(config["signing_key"], body)},
        event_type=event["eventCategory"] + "." + event["eventType"],
        metadata={"tenantId": tenant},
    )
