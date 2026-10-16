"""Keep the documented consumer entry points executable."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import time
from pathlib import Path
from types import ModuleType

import httpx
import pytest

pytestmark = pytest.mark.e2e
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def load_example(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"emulator_example_{name}", EXAMPLES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_python_inprocess() -> None:
    result = await load_example("python_inprocess").main()
    assert result["employee"] == "maya.chen@halcyonadvisory.example"
    assert result["stripe_customers"] > 0


async def test_official_sdks() -> None:
    result = await asyncio.to_thread(load_example("official_sdks").main)
    assert result["google_users"] == 47
    assert result["graph_users"] >= result["google_users"]
    assert result["slack_users"] >= result["google_users"]
    assert result["stripe_customers"] > 0


async def test_webhook_receiver() -> None:
    assert await load_example("webhook_receiver").main() == ["customer.created", "meeting.created"]


async def test_webhook_receiver_rejects_tampered_and_stale_events() -> None:
    example = load_example("webhook_receiver")
    secrets = {"stripe": "whsec_synthetic_example", "zoom": "synthetic-zoom-secret"}
    received: list[str] = []
    app = example.create_app(secrets, received)
    body = json.dumps({"event": "meeting.created", "payload": {"object": {"id": 123}}}).encode()
    now = int(time.time())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://consumer.example"
    ) as client:
        # Sign one body and send another; a receiver must verify the raw bytes.
        for path, timestamp in (("/stripe", now), ("/zoom", now), ("/zoom", now - 600)):
            if path == "/stripe":
                digest = hmac.new(
                    secrets["stripe"].encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
                ).hexdigest()
                headers = {"stripe-signature": f"t={timestamp},v1={digest}"}
            else:
                digest = hmac.new(
                    secrets["zoom"].encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256
                ).hexdigest()
                headers = {"x-zm-request-timestamp": str(timestamp), "x-zm-signature": "v0=" + digest}
            sent = body if timestamp < now else body + b" "
            response = await client.post(path, content=sent, headers=headers)
            assert response.status_code == 400
    assert not received
