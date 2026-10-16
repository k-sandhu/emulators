from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from emulators.providers.microsoft import common as c
from emulators.providers.microsoft import meetings_model as mm
from emulators.providers.microsoft import teams_model as tm
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


@pytest.mark.parametrize("content", [b"invalid-json", b"[]", b"null"])
async def test_files_teams_and_meeting_mutations_share_json_validation(
    make_client: Callable[..., Any],
    content: bytes,
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        team = next(t for t in ctx.store[tm.TEAMS] if "p018" in t["_members"])
        channel = next(
            ch for ch in ctx.store[tm.CHANNELS].where(_team=team["id"]) if "p018" in ch["_members"]
        )
        meeting = next(m for m in ctx.store[mm.MEETINGS] if m["_organizer"] == "p018")
        channel_path = f"/v1.0/teams/{team['id']}/channels/{channel['id']}/messages"
        draft = (
            await client.post(channel_path, json={"body": {"contentType": "text", "content": "Review"}})
        ).json()
        for method, path in (
            ("POST", channel_path),
            ("PATCH", channel_path + "/" + draft["id"]),
            ("PATCH", "/v1.0/me/onlineMeetings/" + meeting["id"]),
            ("POST", "/v1.0/me/onlineMeetings/createOrGet"),
            ("POST", "/v1.0/me/drive/root:/review.txt:/createUploadSession"),
            ("POST", "/v1.0/me/drive/root/createLink"),
        ):
            response = await client.request(
                method, path, content=content, headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 400, (path, response.text)
            assert "Unable to read JSON request payload" in response.text


async def test_teams_projection_types_count_preferences_and_delta(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        team = next(t for t in ctx.store[tm.TEAMS] if "p018" in t["_members"])
        channel = next(
            ch for ch in ctx.store[tm.CHANNELS].where(_team=team["id"]) if "p018" in ch["_members"]
        )
        root = f"/v1.0/teams/{team['id']}"
        members = await client.get(root + "/members?$select=displayName")
        assert all(set(m) == {"id", "displayName", "@odata.type"} for m in members.json()["value"])
        path = root + f"/channels/{channel['id']}/messages"
        response = await client.get(
            path,
            params={"$select": "body/content", "$count": "true"},
            headers={"Prefer": "odata.maxpagesize=2"},
        )
        assert response.status_code == 200 and len(response.json()["value"]) == 2
        assert response.json()["@odata.count"] >= 2
        assert response.headers["Preference-Applied"] == "odata.maxpagesize=2"
        assert set(response.json()["value"][0]) == {"id", "body"}
        assert set(response.json()["value"][0]["body"]) == {"content"}
        delta = await client.get(
            path + "/delta?$select=body/content", headers={"Prefer": "odata.maxpagesize=2"}
        )
        assert len(delta.json()["value"]) == 2
        assert delta.headers["Preference-Applied"] == "odata.maxpagesize=2"
        assert all(set(m) == {"id", "body"} for m in delta.json()["value"])
        assert "%24" not in delta.json()["@odata.nextLink"]
        for kind in ("HTML", "Text"):
            sent = await client.post(path, json={"body": {"contentType": kind, "content": "Review"}})
            assert sent.status_code == 201 and sent.json()["body"]["contentType"] == kind.lower()
        assert (await client.patch(path, json={})).status_code == 405


async def test_meeting_and_drive_nested_single_projections(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        ctx = client.hub.context("microsoft")
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        meeting = next(m for m in ctx.store[mm.MEETINGS] if m["_organizer"] == "p018")
        response = await client.get(
            "/v1.0/me/onlineMeetings/" + meeting["id"], params={"$select": "participants/organizer/upn"}
        )
        assert response.status_code == 200
        assert set(response.json()) == {"@odata.context", "id", "participants"}
        assert response.json()["participants"] == {"organizer": {"upn": "maya.chen@halcyonadvisory.example"}}
        star = await client.get("/v1.0/me/drive/root?$select=*")
        assert star.status_code == 200 and "folder" in star.json()
        root = await client.get("/v1.0/me/drive/root?$select=folder/childCount")
        assert set(root.json()) == {"@odata.context", "id", "folder"}
        assert set(root.json()["folder"]) == {"childCount"}
        outsider = next(
            person.id for person in ctx.world.active_employees if person.id not in meeting["_attendees"]
        )
        denied = bearer(await mint(client, BASE, user=outsider))
        uid = c.user_id(ctx.world, "p018")
        assert (
            await client.get(f"/v1.0/users/{uid}/onlineMeetings/{meeting['id']}", headers=denied)
        ).status_code in (403, 404)


async def test_event_delta_partial_projection_does_not_require_unselected_fields(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        response = await client.get(
            "/v1.0/me/events/delta?$select=start&$top=2",
            headers={"Prefer": 'outlook.body-content-type="text"'},
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["value"]) == 2
        assert all(set(event) == {"id", "start", "@odata.etag"} for event in response.json()["value"])
