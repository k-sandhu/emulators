from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from emulators.providers.microsoft import meetings_model as mm
from emulators.providers.microsoft import sites
from emulators.providers.microsoft import teams_model as tm
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


@pytest.mark.parametrize(
    "body",
    [
        b"invalid-json",
        b"[]",
        b"null",
        b"",
        b'{"audit":"\\ud800"}',
        b'{"audit":"\xff"}',
        b'{"audit":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}",
    ],
)
async def test_every_json_write_rejects_malformed_unicode_and_deep_payloads(
    make_client: Callable[..., Any], body: bytes
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        file = (await client.put("/v1.0/me/drive/root:/fuzz.txt:/content", content=b"original")).json()
        path = "/v1.0/me/drive/items/" + file["id"]
        permission = (
            await client.post(
                path + "/invite",
                json={"roles": ["read"], "recipients": [{"email": ctx.world.person("p001").email}]},
            )
        ).json()["value"][0]
        channel = next(ch for ch in ctx.store[tm.CHANNELS] if "p018" in ch["_members"])
        messages = f"/v1.0/teams/{channel['_team']}/channels/{channel['id']}/messages"
        parent = (await client.post(messages, json={"body": {"content": "Fuzz"}})).json()
        replies = messages + f"/{parent['id']}/replies"
        reply = (await client.post(replies, json={"body": {"content": "Reply"}})).json()
        meeting = next(m for m in ctx.store[mm.MEETINGS] if m["_organizer"] == "p018")
        listing = ctx.store[sites.LISTS].first(name="EngagementTracker")
        items = f"/v1.0/sites/{listing['_site']}/lists/{listing['id']}/items"
        for method, url in (
            ("POST", "/v1.0/me/drive/root/children"),
            ("POST", path + "/invite"),
            ("POST", path + "/createLink"),
            ("POST", path + "/copy"),
            ("PATCH", path),
            ("PATCH", path + "/permissions/" + permission["id"]),
            ("POST", messages),
            ("PATCH", messages + "/" + parent["id"]),
            ("POST", replies),
            ("PATCH", replies + "/" + reply["id"]),
            ("POST", "/v1.0/me/onlineMeetings"),
            ("POST", "/v1.0/me/onlineMeetings/createOrGet"),
            ("PATCH", "/v1.0/me/onlineMeetings/" + meeting["id"]),
            ("POST", items),
            ("PATCH", items + "/1"),
            ("PATCH", items + "/1/fields"),
        ):
            response = await client.request(
                method, url, content=body, headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 400, (method, url, response.text)
            assert "error" in response.json()
        response = await client.post(
            "/v1.0/me/drive/root:/fuzz-session.txt:/createUploadSession", content=body
        )
        assert response.status_code == (400 if body else 200)


async def test_oversized_query_and_range_numbers_never_raise_500(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        huge = "9" * 5000
        for path in ("/v1.0/me/drive/root", "/v1.0/me/drive/root/children", "/v1.0/me/drive/root/delta"):
            response = await client.get(path, params={"$top": huge})
            assert response.status_code == 400, response.text
        file = (await client.put("/v1.0/me/drive/root:/range.txt:/content", content=b"range")).json()
        assert (
            await client.get(file["@microsoft.graph.downloadUrl"], headers={"Range": "bytes=0-" + huge})
        ).status_code == 416
        session = (
            await client.post("/v1.0/me/drive/root:/range-upload.txt:/createUploadSession", json={})
        ).json()
        assert (
            await client.put(
                session["uploadUrl"], content=b"x", headers={"Content-Range": f"bytes 0-0/{huge}"}
            )
        ).status_code == 400


async def test_sharepoint_nan_number_is_rejected(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        client.headers.update(bearer(await mint(client, BASE, app_only=True)))
        listing = ctx.store[sites.LISTS].first(name="EngagementTracker")
        path = f"/v1.0/sites/{listing['_site']}/lists/{listing['id']}/items"
        response = await client.post(
            path,
            content=b'{"fields":{"Title":"Finite budget","BudgetHours":1e999}}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
