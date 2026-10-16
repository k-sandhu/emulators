"""Verify real vendor signatures while delivering entirely in-process."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import time
from typing import Any

import httpx
import stripe
from fastapi import FastAPI, HTTPException, Request

from emulators.core.hub import EmulatorHub


def create_app(secrets: dict[str, str], received: list[str]) -> FastAPI:
    app = FastAPI()

    @app.post("/stripe")
    async def stripe_hook(request: Request) -> dict[str, bool]:
        try:
            event = stripe.Webhook.construct_event(
                await request.body(), request.headers.get("stripe-signature", ""), secrets["stripe"]
            )
        except (ValueError, stripe.SignatureVerificationError) as exc:
            raise HTTPException(400, "Invalid Stripe signature") from exc
        received.append(event["type"])
        return {"received": True}

    @app.post("/zoom")
    async def zoom_hook(request: Request) -> dict[str, Any]:
        body = await request.body()
        timestamp = request.headers.get("x-zm-request-timestamp", "")
        try:
            if abs(time.time() - int(timestamp)) > 300:
                raise ValueError("stale timestamp")
        except ValueError as exc:
            raise HTTPException(400, "Invalid Zoom timestamp") from exc
        digest = hmac.new(
            secrets["zoom"].encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest("v0=" + digest, request.headers.get("x-zm-signature", "")):
            raise HTTPException(400, "Invalid Zoom signature")
        event = json.loads(body)
        if event["event"] == "endpoint.url_validation":
            plain = event["payload"]["plainToken"]
            encrypted = hmac.new(secrets["zoom"].encode(), plain.encode(), hashlib.sha256).hexdigest()
            return {"plainToken": plain, "encryptedToken": encrypted}
        received.append(event["event"])
        return {"received": True}

    return app


async def main() -> list[str]:
    received: list[str] = []
    secrets = {"zoom": "synthetic-zoom-webhook-secret"}
    receiver = create_app(secrets, received)
    hub = EmulatorHub(
        providers=["stripe", "zoom"],
        seed="emulators",
        anchor="2026-10-16",
        webhook_mode="sync",
        webhook_transport=httpx.ASGITransport(app=receiver),
    )
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        stripe_auth = {"Authorization": "Bearer sk_test_emulator"}
        response = await client.post(
            "https://api.stripe.com/v1/webhook_endpoints",
            headers=stripe_auth,
            data={"url": "https://consumer.example/stripe", "enabled_events[]": "customer.created"},
        )
        response.raise_for_status()
        secrets["stripe"] = response.json()["secret"]
        response = await client.post(
            "https://api.stripe.com/v1/customers",
            headers=stripe_auth,
            data={"name": "Synthetic webhook customer", "email": "billing@consumer.example"},
        )
        response.raise_for_status()
        response = await client.post(
            "https://api.zoom.us/_emulator/actions/configure_webhook",
            json={
                "url": "https://consumer.example/zoom",
                "secret_token": secrets["zoom"],
                "events": ["meeting.created"],
            },
        )
        response.raise_for_status()
        grant = await client.post("https://api.zoom.us/_emulator/tokens", json={"user": "p005"})
        grant.raise_for_status()
        response = await client.post(
            "https://api.zoom.us/v2/users/me/meetings",
            headers={"Authorization": f"Bearer {grant.json()['access_token']}"},
            json={"topic": "Webhook example", "type": 2, "start_time": "2026-10-20T15:00:00Z"},
        )
        response.raise_for_status()
        await hub.drain_webhooks()
        for base in ("https://api.stripe.com", "https://api.zoom.us"):
            journal = await client.get(base + "/_emulator/webhooks")
            journal.raise_for_status()
            deliveries = journal.json()["deliveries"]
            assert deliveries and all(item["status"] == "delivered" for item in deliveries)
    assert received == ["customer.created", "meeting.created"]
    return received


if __name__ == "__main__":
    sys.stdout.write(json.dumps(asyncio.run(main()), indent=2) + "\n")
