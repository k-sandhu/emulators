from __future__ import annotations

from collections.abc import Callable
from typing import Any

from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


async def test_batch_dependency_order_auth_and_errors(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        result = await client.post(
            "/v1.0/$batch",
            headers=auth,
            json={
                "requests": [
                    {"id": "2", "method": "GET", "url": "/me/messages?$top=1", "dependsOn": ["1"]},
                    {"id": "1", "method": "GET", "url": "/me?$select=id,mail"},
                    {"id": "3", "method": "GET", "url": "/users/absent"},
                    {"id": "4", "method": "GET", "url": "/me", "dependsOn": ["3"]},
                ]
            },
        )
        assert result.status_code == 200, result.text
        rows = result.json()["responses"]
        assert [r["id"] for r in rows] == ["2", "1", "3", "4"]
        assert [r["status"] for r in rows] == [200, 200, 404, 424]
        assert "request-id" in rows[0]["headers"]
        for requests in (
            [{"id": str(i), "url": "/me"} for i in range(21)],
            [{"id": "1", "url": "/me", "dependsOn": ["2"]}, {"id": "2", "url": "/me", "dependsOn": ["1"]}],
            [{"id": "1", "url": "https://consumer.example/private"}],
            [{"id": "1", "url": "/../_emulator/reset"}],
        ):
            assert (
                await client.post("/beta/$batch", headers=auth, json={"requests": requests})
            ).status_code == 400
