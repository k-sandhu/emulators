from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi import FastAPI, Request

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint

from .test_confluence import DOCUMENT, create_page
from .test_jira import create_issue

SITE = "https://halcyonadvisory.atlassian.net"


async def test_signed_page_and_jira_webhooks(
    client: httpx.AsyncClient, auth: dict[str, str], hub: EmulatorHub
) -> None:
    received: list[tuple[bytes, str]] = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> dict[str, bool]:
        received.append((await request.body(), request.headers["x-hub-signature"]))
        return {"ok": True}

    hub.webhook_transport = httpx.ASGITransport(app=receiver)
    subscribed = await client.post(
        "/_emulator/actions/configure_webhook",
        json={
            "url": "https://consumer.example/hook",
            "events": ["page_created", "page_updated", "jira:issue_created", "jira:issue_updated"],
            "secret": "test-signature-key",
        },
    )
    assert subscribed.status_code == 200 and "secret" not in subscribed.json()
    page = await create_page(client, auth)
    updated = await client.put(
        "/wiki/api/v2/pages/" + page["id"],
        headers=auth,
        json={
            "id": page["id"],
            "title": page["title"],
            "status": "current",
            "version": {"number": 2},
            "body": {"representation": "storage", "value": "<p>Updated webhook test</p>"},
        },
    )
    assert updated.status_code == 200
    issue = await create_issue(client, auth)
    assert (
        await client.put(
            "/rest/api/3/issue/" + issue["key"],
            headers=auth,
            json={"fields": {"summary": "Updated webhook test"}},
        )
    ).status_code == 204
    assert len(received) == 4
    for body, signature in received:
        assert signature == "sha256=" + hmac.new(b"test-signature-key", body, hashlib.sha256).hexdigest()
    page_event = json.loads(received[0][0])
    assert isinstance(page_event["page"]["creationDate"], int) and isinstance(page_event["page"]["id"], int)
    assert page_event["page"]["spaceKey"] == "KB" and page_event["page"]["self"].startswith(SITE + "/wiki/")
    assert json.loads(received[1][0])["page"]["version"] == 2
    jira_event = json.loads(received[2][0])
    assert jira_event["webhookEvent"] == "jira:issue_created" and jira_event["issue"]["key"] == issue["key"]
    assert json.loads(received[3][0])["changelog"]["items"][0]["field"] == "summary"
    journal = (await client.get("/_emulator/webhooks")).json()["deliveries"]
    assert [r["event_type"] for r in journal] == [
        "page_created",
        "page_updated",
        "jira:issue_created",
        "jira:issue_updated",
    ]
    assert all(r["status"] == "delivered" for r in journal)


async def test_fault_journal_snapshot_and_reset(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    await client.post("/_emulator/faults", json={"path": "/wiki/api/v2/pages", "status": 429, "times": 1})
    throttled = await client.get("/wiki/api/v2/pages", headers=auth)
    assert throttled.status_code == 429 and throttled.json()["errors"][0]["status"] == 429
    assert throttled.headers["retry-after"] == "2" and throttled.headers["x-ratelimit-remaining"] == "0"
    assert (await client.get("/wiki/api/v2/pages", headers=auth)).status_code == 200
    page = await create_page(client, auth)
    await client.post("/_emulator/snapshots", json={"name": "with-page"})
    await client.delete("/wiki/api/v2/pages/" + page["id"], headers=auth)
    await client.post("/_emulator/snapshots/with-page/restore")
    assert (await client.get("/wiki/api/v2/pages/" + page["id"], headers=auth)).status_code == 200
    requests = (await client.get("/_emulator/requests")).json()["requests"]
    assert requests[0]["status"] == 429 and requests[0]["fault"]
    assert all("Bearer ey" not in r["headers"].get("authorization", "") for r in requests)
    await client.post("/_emulator/reset")
    fresh = bearer(await mint(client, SITE, user="p001"))
    assert (await client.get("/wiki/api/v2/pages/" + page["id"], headers=fresh)).status_code == 404
    assert (await client.get("/wiki/api/v2/pages/" + DOCUMENT, headers=fresh)).status_code == 200
    assert (await client.get("/wiki/api/v2/pages", headers=auth)).status_code == 401


@pytest.mark.parametrize(
    "path,field",
    [
        ("/wiki/api/v2/no-such-resource", "errors"),
        ("/wiki/rest/api/no-such-resource", "statusCode"),
        ("/rest/api/3/no-such-resource", "errorMessages"),
    ],
)
async def test_unknown_routes_have_family_envelopes(client: httpx.AsyncClient, path: str, field: str) -> None:
    response = await client.get(path)
    assert response.status_code == 404 and field in response.json()


@pytest.mark.parametrize(
    "path,field", [("/wiki/api/v2/pages", "errors"), ("/rest/api/3/issue", "errorMessages")]
)
async def test_malformed_json_is_vendor_shaped(
    client: httpx.AsyncClient, auth: dict[str, str], path: str, field: str
) -> None:
    response = await client.post(
        path, headers={**auth, "Content-Type": "application/json"}, content=b'{"invalid":'
    )
    assert response.status_code == 400 and field in response.json()


def test_seed_determinism_and_speed() -> None:
    first = EmulatorHub(providers=["atlassian"], anchor="2026-10-16")
    ctx = first.context("atlassian")
    ctx.store.clear()
    started = time.perf_counter()
    ctx.provider.seed(first.context("atlassian"))
    assert time.perf_counter() - started < 2
    second = EmulatorHub(providers=["atlassian"], anchor="2026-10-16")
    assert ctx.store.snapshot() == second.context("atlassian").store.snapshot()
    assert all(a["_blob"] is None for a in ctx.store["attachments"])
    assert len(ctx.store["issues"]) == 60 and len(ctx.store["pages"]) > 700


@pytest.mark.parametrize(
    "path,field", [("/rest/api/3/myself", "errorMessages"), ("/wiki/rest/api/user/current", "statusCode")]
)
async def test_family_specific_throttling(
    client: httpx.AsyncClient, auth: dict[str, str], path: str, field: str
) -> None:
    await client.post("/_emulator/faults", json={"path": path, "status": 429, "times": 1})
    response = await client.get(path, headers=auth)
    assert response.status_code == 429 and field in response.json()
    assert "retry-after" in response.headers and response.headers["x-ratelimit-limit"] == "1000"
