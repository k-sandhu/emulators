"""Call two real vendor URLs without opening a socket."""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

from emulators.core.hub import EmulatorHub


async def main() -> dict[str, str | int]:
    hub = EmulatorHub(providers=["microsoft", "stripe"], seed="emulators", anchor="2026-10-16")
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        grant = await client.post("https://graph.microsoft.com/_emulator/tokens", json={"user": "p018"})
        grant.raise_for_status()
        me = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {grant.json()['access_token']}"},
        )
        me.raise_for_status()
        customers = await client.get(
            "https://api.stripe.com/v1/customers",
            params={"limit": 100},
            headers={"Authorization": "Bearer sk_test_emulator"},
        )
        customers.raise_for_status()
    result = {"employee": me.json()["mail"], "stripe_customers": len(customers.json()["data"])}
    assert result["employee"] == "maya.chen@halcyonadvisory.example"
    assert result["stripe_customers"] > 0
    return result


if __name__ == "__main__":
    sys.stdout.write(json.dumps(asyncio.run(main()), indent=2) + "\n")
