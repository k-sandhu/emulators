from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from .conftest import BASE, project, task, workspace


async def test_invalid_target_url_is_rejected_before_handshake(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    row = await task(client)
    handshakes, _ = receiver(client)
    response = await client.post(
        BASE + "/webhooks",
        json={"data": {"resource": row["gid"], "target": "https://receiver.example:invalid/hook"}},
    )
    assert response.status_code == 400 and handshakes == []
    assert len(client.hub.context("asana").store["webhooks"]) == 0


def receiver(
    client: httpx.AsyncClient, *, echo: bool = True, status: int = 204
) -> tuple[list[str], list[tuple[bytes, dict[str, str]]]]:
    app = FastAPI()
    handshakes: list[str] = []
    events: list[tuple[bytes, dict[str, str]]] = []

    @app.post("/hook")
    async def hook(request: Request) -> Response:
        secret = request.headers.get("x-hook-secret")
        if secret:
            handshakes.append(secret)
            return Response(status_code=status, headers={"X-Hook-Secret": secret if echo else "incorrect"})
        events.append((await request.body(), dict(request.headers)))
        return Response(status_code=status)

    client.hub.webhook_transport = httpx.ASGITransport(app=app)
    return handshakes, events


async def create_hook(client: httpx.AsyncClient, resource_id: str, **extra: Any) -> httpx.Response:
    return await client.post(
        BASE + "/webhooks",
        json={"data": {"resource": resource_id, "target": "https://receiver.example/hook", **extra}},
    )


async def test_project_webhook_handshake_signature_and_token_binding(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    p = await project(client)
    handshakes, received = receiver(client)
    created = await create_hook(client, p["gid"])
    assert created.status_code == 201, created.text
    secret = created.json()["X-Hook-Secret"]
    assert handshakes == [secret]
    hook_id = created.json()["data"]["gid"]
    assert "X-Hook-Secret" not in (await client.get(BASE + f"/webhooks/{hook_id}")).json()
    row = await task(client, projects=[p["gid"]])
    assert len(received) == 1
    raw, headers = received[0]
    assert headers["x-hook-signature"] == hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    assert json.loads(raw)["events"][0]["resource"]["gid"] == row["gid"]
    assert (
        await client.get(BASE + f"/webhooks/{hook_id}", headers={"Authorization": "Bearer asana-dev-p035"})
    ).status_code == 403
    assert (await client.delete(BASE + f"/webhooks/{hook_id}")).json() == {"data": {}}


async def test_webhook_subtype_and_field_filters_match_actual_task(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    p = await project(client)
    default = await task(client, projects=[p["gid"]])
    milestone = await task(client, projects=[p["gid"]], resource_subtype="milestone")
    _, received = receiver(client)
    created = await create_hook(
        client,
        p["gid"],
        filters=[
            {
                "resource_type": "task",
                "resource_subtype": "milestone",
                "action": "changed",
                "fields": ["name"],
            }
        ],
    )
    assert created.status_code == 201, created.text
    assert (
        await client.put(BASE + f"/tasks/{default['gid']}", json={"data": {"name": "No milestone event"}})
    ).status_code == 200
    assert received == []
    assert (
        await client.put(BASE + f"/tasks/{milestone['gid']}", json={"data": {"completed": True}})
    ).status_code == 200
    assert received == []
    assert (
        await client.put(BASE + f"/tasks/{milestone['gid']}", json={"data": {"name": "Milestone event"}})
    ).status_code == 200
    assert len(received) == 1
    assert json.loads(received[0][0])["events"][0]["resource"]["gid"] == milestone["gid"]
    assert json.loads(received[0][0])["events"][0]["change"] == {"field": "name", "action": "changed"}


@pytest.mark.parametrize(
    "resource_kind,rule",
    [
        ("project", {"resource_type": "task", "action": "added", "fields": ["name"]}),
        ("project", {"resource_type": "task", "fields": ["name"]}),
        ("workspace", {"resource_type": "task", "action": "changed", "fields": ["name"]}),
        ("workspace", {"resource_type": "task", "action": "changed", "fields": []}),
    ],
)
async def test_invalid_filters_fail_before_handshake_or_persistence(
    asana_client: httpx.AsyncClient, resource_kind: str, rule: dict[str, Any]
) -> None:
    client = asana_client
    resource_id = await workspace(client) if resource_kind == "workspace" else (await project(client))["gid"]
    handshakes, received = receiver(client)
    response = await create_hook(client, resource_id, filters=[rule])
    assert response.status_code == 400, response.text
    assert handshakes == [] and received == []
    assert len(client.hub.context("asana").store["webhooks"]) == 0


async def test_bad_handshake_is_atomic_and_inaccessible_resource_does_not_call_target(
    asana_client: httpx.AsyncClient,
) -> None:
    client = asana_client
    row = await task(client)
    handshakes, _ = receiver(client, echo=False)
    response = await create_hook(client, row["gid"])
    assert response.status_code == 400 and len(handshakes) == 1
    assert len(client.hub.context("asana").store["webhooks"]) == 0
    handshakes.clear()
    response = await client.post(
        BASE + "/webhooks",
        headers={"Authorization": "Bearer asana-dev-p035"},
        json={"data": {"resource": row["gid"], "target": "https://receiver.example/hook"}},
    )
    assert response.status_code == 403 and not handshakes


async def test_action_delivers_event_and_revoked_hook_token_stops_delivery(
    asana_client: httpx.AsyncClient,
) -> None:
    client = asana_client
    p = await project(client)
    row = await task(client, projects=[p["gid"]])
    _, received = receiver(client)
    token = (await client.post("https://app.asana.com/_emulator/tokens", json={"user": "p018"})).json()[
        "access_token"
    ]
    response = await client.post(
        BASE + "/webhooks",
        headers={"Authorization": "Bearer " + token},
        json={"data": {"resource": p["gid"], "target": "https://receiver.example/hook"}},
    )
    assert response.status_code == 201
    action = await client.post(
        "https://app.asana.com/_emulator/actions/simulate_task_change",
        json={"user": "p018", "task_gid": row["gid"], "changes": {"completed": True}},
    )
    assert action.status_code == 200, action.text
    assert len(received) == 1
    await client.post("https://app.asana.com/_emulator/actions/expire_token", json={"token": token})
    assert (
        await client.put(BASE + f"/tasks/{row['gid']}", json={"data": {"completed": False}})
    ).status_code == 200
    assert len(received) == 1
    assert len(client.hub.context("asana").store["webhooks"]) == 0


async def test_workspace_requires_filters_and_project_hook_receives_child_comments(
    asana_client: httpx.AsyncClient,
) -> None:
    client = asana_client
    p = await project(client)
    row = await task(client, projects=[p["gid"]])
    handshakes, received = receiver(client)
    assert (await create_hook(client, await workspace(client))).status_code == 400
    assert handshakes == []
    assert (
        await create_hook(client, p["gid"], filters=[{"resource_type": "story", "action": "added"}])
    ).status_code == 201
    created = await client.post(
        BASE + f"/tasks/{row['gid']}/stories", json={"data": {"text": "Project-level comment event"}}
    )
    assert created.status_code == 201 and len(received) == 1
    event = json.loads(received[0][0])["events"][0]
    assert event["resource"]["gid"] == created.json()["data"]["gid"]
    assert event["parent"]["gid"] == p["gid"]


async def test_resource_new_value_only_contains_webhook_resource_identity(
    asana_client: httpx.AsyncClient,
) -> None:
    client = asana_client
    row = await task(client)
    _, received = receiver(client)
    assert (await create_hook(client, row["gid"])).status_code == 201
    response = await client.put(BASE + f"/tasks/{row['gid']}", json={"data": {"assignee": "me"}})
    assert response.status_code == 200 and len(received) == 1
    event = json.loads(received[0][0])["events"][0]
    assert event["change"]["new_value"] == {
        "gid": response.json()["data"]["assignee"]["gid"],
        "resource_type": "user",
    }
