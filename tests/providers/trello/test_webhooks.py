"""HEAD validation, exact Trello signatures, token ownership and delivery lifecycle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from .conftest import mint

CALLBACK = "https://consumer.example/trello?subscription=halcyon"


class Receiver:
    def __init__(self) -> None:
        self.app = FastAPI()
        self.head_status = 200
        self.post_status = 200
        self.heads = 0
        self.deliveries: list[tuple[bytes, dict[str, str]]] = []

        @self.app.api_route("/trello", methods=["HEAD", "POST"])
        async def handle(request: Request) -> Response:
            if request.method == "HEAD":
                self.heads += 1
                return Response(status_code=self.head_status)
            self.deliveries.append((await request.body(), dict(request.headers)))
            return Response(status_code=self.post_status)


@pytest.fixture
def receiver(client: httpx.AsyncClient) -> Receiver:
    result = Receiver()
    client.hub.webhook_transport = httpx.ASGITransport(app=result.app)  # type: ignore[attr-defined]
    return result


async def subscribe(client: httpx.AsyncClient, model: str, **updates: Any) -> httpx.Response:
    return await client.post(
        "/1/webhooks",
        json={"callbackURL": CALLBACK, "idModel": model, "description": "Board webhook", **updates},
    )


async def test_head_validation_and_exact_signature(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any], receiver: Receiver
) -> None:
    response = await subscribe(client, board["id"])
    assert response.status_code == 200, response.text
    hook = response.json()
    assert receiver.heads == 1 and receiver.deliveries == []
    assert hook["idModel"] == board["id"] and hook["active"] is True
    await client.post(f"/1/cards/{card['id']}/actions/comments", data={"text": "Client approved 👋"})
    assert len(receiver.deliveries) == 1
    raw, headers = receiver.deliveries[0]
    secret = (await client.get("/_emulator/info")).json()["credentials"]["application_secret"]
    expected = base64.b64encode(
        hmac.new(secret.encode(), raw + CALLBACK.encode(), hashlib.sha1).digest()
    ).decode()
    assert headers["x-trello-webhook"] == expected
    payload = json.loads(raw)
    assert payload["action"]["type"] == "commentCard"
    assert payload["action"]["data"]["text"] == "Client approved 👋"
    assert payload["model"]["id"] == board["id"] and payload["webhook"]["id"] == hook["id"]
    assert "_token" not in raw.decode() and "_members" not in raw.decode()
    journal = (await client.get("/_emulator/webhooks")).json()
    assert journal["deliveries"][0]["status"] == "delivered"


@pytest.mark.parametrize("status", [204, 301, 400, 404, 500])
async def test_rejected_head_does_not_register(
    client: httpx.AsyncClient, board: dict[str, Any], receiver: Receiver, status: int
) -> None:
    receiver.head_status = status
    response = await subscribe(client, board["id"])
    assert response.status_code == 400 and "200" in response.text
    assert (await client.get("/1/webhooks")).json() == []


async def test_owner_token_privacy_and_update_atomicity(
    client: httpx.AsyncClient, board: dict[str, Any], receiver: Receiver
) -> None:
    hook = (await subscribe(client, board["id"])).json()
    other = await mint(client)
    response = await client.get(f"/1/webhooks/{hook['id']}", params={"token": other["token"]})
    assert response.status_code == 401 and response.text == "webhook does not belong to token"
    assert (await client.get("/1/webhooks", params={"token": other["token"]})).json() == []
    receiver.head_status = 404
    response = await client.put(
        f"/1/webhooks/{hook['id']}",
        json={"description": "Must not persist", "callbackURL": "https://consumer.example/trello?new=1"},
    )
    assert response.status_code == 400
    assert (await client.get(f"/1/webhooks/{hook['id']}")).json() == hook
    await client.put(f"/1/webhooks/{hook['id']}/active", json={"value": False})
    assert (await client.get(f"/1/webhooks/{hook['id']}/active")).json() == {"_value": False}


async def test_model_acl_duplicate_and_revocation(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any], receiver: Receiver
) -> None:
    other = await mint(client, "p026")
    denied = await client.post(
        "/1/webhooks",
        params={"token": other["token"]},
        json={"callbackURL": CALLBACK, "idModel": board["id"]},
    )
    assert denied.status_code == 401 and receiver.heads == 0
    token = await mint(client)
    token_params = {"token": token["token"]}
    hook = await client.post(
        f"/1/tokens/{token['token']}/webhooks",
        params=token_params,
        json={"callbackURL": CALLBACK, "idModel": card["id"]},
    )
    assert hook.status_code == 200
    duplicate = await client.post(
        "/1/webhooks", params=token_params, json={"callbackURL": CALLBACK, "idModel": card["id"]}
    )
    assert duplicate.status_code == 400
    await client.delete(f"/1/tokens/{token['token']}", params=token_params)
    await client.put(f"/1/cards/{card['id']}", json={"name": "No revoked delivery"})
    assert receiver.deliveries == []
    assert not client.hub.context("trello").store["webhooks"].get(hook.json()["id"])  # type: ignore[attr-defined]


async def test_gone_deletes_webhook_and_server_errors_retry(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any], receiver: Receiver
) -> None:
    hook = (await subscribe(client, board["id"])).json()
    receiver.post_status = 500
    await client.put(f"/1/cards/{card['id']}", json={"name": "First"})
    assert len(receiver.deliveries) == 3
    journal = (await client.get("/_emulator/webhooks")).json()["deliveries"][0]
    assert journal["attempts"] == 3 and journal["status"] == "failed"
    assert (await client.get(f"/1/webhooks/{hook['id']}")).json()["consecutiveFailures"] == 1
    receiver.post_status = 410
    await client.put(f"/1/cards/{card['id']}", json={"name": "Second"})
    assert (await client.get(f"/1/webhooks/{hook['id']}")).status_code == 404


async def test_manual_mode_and_invalid_write_deliver_nothing(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any], receiver: Receiver
) -> None:
    ctx = client.hub.context("trello")  # type: ignore[attr-defined]
    ctx.webhooks.mode = "manual"
    await subscribe(client, board["id"])
    assert (await client.put(f"/1/cards/{card['id']}", json={"name": "No", "closed": []})).status_code == 400
    assert receiver.deliveries == []
    assert (await client.get("/_emulator/webhooks")).json()["deliveries"] == []
    await client.put(f"/1/cards/{card['id']}", json={"name": "Queued"})
    assert receiver.deliveries == []
    await client.post("/_emulator/webhooks/flush")
    assert len(receiver.deliveries) == 1
    ctx.webhooks.mode = "sync"
