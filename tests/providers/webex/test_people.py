from __future__ import annotations

import base64

import httpx
import pytest

from emulators.testing.helpers import bearer, mint

ADMIN = bearer("webex-admin-token")


async def test_directory_hierarchy_filters_and_pagination(client: httpx.AsyncClient) -> None:
    me = (await client.get("/v1/people/me")).json()
    assert "ciscospark://us/PEOPLE/" in base64.urlsafe_b64decode(me["id"] + "==").decode()
    assert me["manager"] == "Daniel Kowalski" and me["siteUrls"] == ["halcyonadvisory.webex.com#attendee"]
    manager = await client.get(f"/v1/people/{me['managerId']}")
    assert manager.json()["displayName"] == me["manager"]
    assert (await client.get(me["avatar"])).content.startswith(b"\x89PNG")
    for query in ({"email": me["emails"][0].upper()}, {"displayName": "Sofia"}, {"id": me["id"]}):
        response = await client.get("/v1/people", params=query)
        assert response.status_code == 200 and response.json()["items"][0]["id"] == me["id"]
    assert (await client.get("/v1/people")).status_code == 403
    response = await client.get("/v1/people", params={"max": 2}, headers=ADMIN)
    seen = []
    while True:
        assert set(response.json()) == {"items"}
        assert len(response.json()["items"]) <= 2
        seen.extend(p["id"] for p in response.json()["items"])
        if "next" not in response.links:
            break
        response = await client.get(response.links["next"]["url"], headers=ADMIN)
    assert len(seen) == len(set(seen)) == 48


@pytest.mark.parametrize("resource", ["organizations", "roles", "licenses"])
async def test_admin_metadata(client: httpx.AsyncClient, resource: str) -> None:
    assert (await client.get(f"/v1/{resource}")).status_code == 403
    r = await client.get(f"/v1/{resource}", headers=ADMIN)
    assert r.status_code == 200 and len(r.json()["items"]) == 1


async def test_bot_and_app_visibility(client: httpx.AsyncClient) -> None:
    bot = (await client.get("/v1/people/me", headers=bearer("webex-bot-token"))).json()
    assert bot["type"] == "bot" and bot["displayName"] == "Halcyon Notes Bot"
    service = await mint(client, "https://webexapis.com", app_only=True)
    assert (await client.get("/v1/people", headers=bearer(service))).status_code == 200
    assert (await client.get("/v1/people/me", headers=bearer(service))).status_code == 403
    assert (await client.get("/v1/rooms", headers=bearer(service))).status_code == 403
    fake_admin = await mint(client, "https://webexapis.com", user="p005", scopes=["spark-admin:people_read"])
    assert (await client.get("/v1/people", headers=bearer(fake_admin))).status_code == 403
