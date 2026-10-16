"""Regression cases from the Webex reference and malformed-request audit."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import bearer, mint

BASE = "https://webexapis.com"
ADMIN = bearer("webex-admin-token")
HISTORY = {"from": "2026-09-01", "to": "2026-10-16", "meetingType": "meeting"}
SCHEDULE = {"title": "Café review 😀", "start": "2026-10-20T14:00:00Z", "end": "2026-10-20T15:00:00Z"}
HOOK = {
    "name": "audit",
    "targetUrl": "https://consumer.example/hook",
    "resource": "messages",
    "event": "created",
}


@pytest.fixture
async def resources(client: httpx.AsyncClient) -> dict[str, str]:
    room = (await client.post("/v1/rooms", json={"title": "Audit"})).json()
    meeting = (await client.post("/v1/meetings", json=SCHEDULE)).json()
    membership = (
        await client.post(
            "/v1/memberships", json={"roomId": room["id"], "personEmail": "maya.chen@halcyonadvisory.example"}
        )
    ).json()
    hook = (await client.post("/v1/webhooks", json=HOOK)).json()
    recording = (
        await client.get("/v1/recordings", params={"from": "2026-09-01", "to": "2026-09-30"})
    ).json()["items"][0]
    return {
        "room": room["id"],
        "meeting": meeting["id"],
        "membership": membership["id"],
        "hook": hook["id"],
        "recording": recording["id"],
    }


@pytest.mark.parametrize("operation", range(13))
async def test_write_endpoints_reject_malformed_input_without_crashing(
    client: httpx.AsyncClient, hub: EmulatorHub, resources: dict[str, str], operation: int
) -> None:
    room, meeting, member, hook, recording = (
        resources[k] for k in ("room", "meeting", "membership", "hook", "recording")
    )
    cases = [
        (
            "POST",
            "/v1/meetings",
            {
                **SCHEDULE,
                "timezone": "UTC",
                "invitees": [],
                "agenda": "x",
                "password": "fixture",
                "enabledAutoRecordMeeting": True,
                "hostEmail": "sofia.alvarez@halcyonadvisory.example",
            },
        ),
        ("PUT", f"/v1/meetings/{meeting}", SCHEDULE),
        ("PATCH", f"/v1/meetings/{meeting}", SCHEDULE),
        ("POST", "/v1/rooms", {"title": "Audit", "isLocked": False, "teamId": "unknown"}),
        ("PUT", f"/v1/rooms/{room}", {"title": "Audit", "isLocked": False}),
        (
            "POST",
            "/v1/memberships",
            {"roomId": room, "personEmail": "arjun.mehta@halcyonadvisory.example", "isModerator": False},
        ),
        ("PUT", f"/v1/memberships/{member}", {"isModerator": True}),
        (
            "POST",
            "/v1/messages",
            {"roomId": room, "text": "x", "markdown": "x", "files": [], "parentId": "unknown"},
        ),
        (
            "POST",
            "/v1/webhooks",
            {**HOOK, "secret": "fixture", "filter": "", "ownedBy": "creator", "status": "active"},
        ),
        (
            "PUT",
            f"/v1/webhooks/{hook}",
            {"name": "Audit", "targetUrl": HOOK["targetUrl"], "secret": "fixture", "status": "active"},
        ),
        (
            "POST",
            "/v1/recordings/softDelete",
            {"recordingIds": [recording], "siteUrl": "halcyonadvisory.webex.com"},
        ),
        ("POST", "/v1/recordings/restore", {"recordingIds": [recording], "restoreAll": False}),
        ("POST", "/v1/recordings/purge", {"recordingIds": [recording], "purgeAll": False}),
    ]
    method, path, good = cases[operation]
    snapshot = hub.context("webex").store.snapshot()
    values = [
        None,
        True,
        0,
        1.5,
        [],
        {},
        ["x"],
        {"x": 1},
        "",
        "x" * 10000,
        "é😀",
        "\ud800",
        "https://[",
        "9999-12-31T23:59:00-14:00",
    ]
    variants = [("root", v) for v in (None, True, 1, [], {}, "text")]
    variants += [(key, v) for key in good for v in values]
    for key, value in variants:
        hub.context("webex").store.restore(snapshot)
        payload = value if key == "root" else {**good, key: value}
        response = await client.request(
            method, path, content=json.dumps(payload), headers={"Content-Type": "application/json"}
        )
        assert response.status_code < 500, (method, path, key, type(value), response.text)
        assert response.headers["TrackingId"]
        if response.is_error:
            assert response.json()["trackingId"] == response.headers["TrackingId"]
    for raw in (b"", b"{", b"\xff", b"plain text", b'{"x":NaN}', b'{"x":Infinity}'):
        response = await client.request(
            method, path, content=raw, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400


@pytest.mark.parametrize(
    "bad",
    [
        "https://[",
        "https://consumer.example:wrong/hook",
        "https://consumer.example:99999/hook",
        "https://" + "x" * 64 + ".example/hook",
        "https://consumer.example/\nhook",
    ],
)
async def test_webhook_invalid_url_is_400_and_atomic(client: httpx.AsyncClient, bad: str) -> None:
    result = await client.post("/v1/webhooks", json={**HOOK, "targetUrl": bad})
    assert result.status_code == 400
    assert (await client.get("/v1/webhooks")).json()["items"] == []


async def test_presence_is_not_account_disablement_and_directory_presence_is_private(
    client: httpx.AsyncClient, hub: EmulatorHub
) -> None:
    me = (await client.get("/v1/people/me")).json()
    assert me["status"] == "inactive"
    assert (await client.get("/v1/people", params={"id": me["id"]})).status_code == 200
    by_id = (await client.get("/v1/people", params={"id": me["id"]})).json()["items"][0]
    assert "status" not in by_id and "lastActivity" not in by_id
    excluded = (
        await client.get("/v1/people", params={"email": me["emails"][0], "excludeStatus": "true"})
    ).json()["items"][0]
    assert "status" not in excluded and "lastActivity" not in excluded
    external = hub.context("webex").store.collection("people").filter(lambda p: p.get("_external"))[0]
    details = (await client.get(f"/v1/people/{external['id']}")).json()
    assert all(key not in details for key in ("status", "lastActivity", "roles", "licenses"))
    departed = next(p for p in hub.world.employees if not p.active)
    disabled = bearer(await mint(client, BASE, user=departed.id))
    assert (await client.get("/v1/people/me", headers=disabled)).status_code == 401
    assert sum(len(p["licenses"]) for p in hub.context("webex").store.collection("people")) == 45
    assert (
        await client.get("/v1/people", headers=ADMIN, params={"id": ",".join([me["id"]] * 86)})
    ).status_code == 400
    assert (
        await client.get("/v1/people", headers=ADMIN, params={"orgId": external["orgId"]})
    ).status_code == 403
    assert (await client.get("/v1/people", params={"roles": "unknown"})).status_code == 403
    assert (await client.get("/v1/people", headers=ADMIN, params={"roles": "unknown"})).json()["items"] == []


async def test_meeting_model_defaults_and_datetime_boundaries(
    client: httpx.AsyncClient, hub: EmulatorHub
) -> None:
    # Default lists cover the next seven days and return series, including non-recurring ones.
    created = (await client.post("/v1/meetings", json=SCHEDULE)).json()
    assert created["meetingType"] == "meetingSeries" and created["state"] == "active"
    default = (await client.get("/v1/meetings")).json()["items"]
    assert created["id"] in {m["id"] for m in default}
    assert all(m["meetingType"] == "meetingSeries" for m in default)
    seeded_series = next(m for m in default if m.get("recurrence"))
    assert seeded_series["start"] == "2026-10-21T18:00:00.000Z"
    children = (await client.get("/v1/meetings", params={"meetingSeriesId": created["id"]})).json()["items"]
    assert (
        len(children) == 1
        and children[0]["meetingType"] == "scheduledMeeting"
        and children[0]["state"] == "ready"
    )
    assert re.fullmatch(r"[a-f0-9]{32}_\d{8}T\d{6}Z", children[0]["id"])
    assert children[0]["meetingNumber"] == created["meetingNumber"]
    excluded = (
        await client.get("/v1/meetings", params={"from": "2026-10-20", "to": created["start"]})
    ).json()["items"]
    assert created["id"] not in {m["id"] for m in excluded}
    current = (await client.get("/v1/meetings", params={"meetingType": "meeting"}, headers=ADMIN)).json()[
        "items"
    ]
    assert len(current) == 1 and current[0]["start"] >= "2026-10-09"
    rows = (await client.get("/v1/meetings", params=HISTORY, headers=ADMIN)).json()["items"]
    first = rows[0]
    assert len(rows) == 4
    assert all(
        key not in first
        for key in ("password", "meetingNumber", "telephony", "sipAddress", "enabledAutoRecordMeeting")
    )
    schedule = (await client.get(f"/v1/meetings/{first['scheduledMeetingId']}", headers=ADMIN)).json()
    assert schedule["meetingSeriesId"] == first["meetingSeriesId"]
    assert (await client.delete(f"/v1/meetings/{first['id']}", headers=ADMIN)).status_code == 400
    updated = await client.patch(f"/v1/meetings/{created['id']}", json={"agenda": None})
    assert updated.status_code == 200 and "agenda" not in updated.json()
    normalized = await client.post(
        "/v1/meetings", json={**SCHEDULE, "start": "2026-10-16T12:00:30Z", "end": "2026-10-16T12:30:59Z"}
    )
    assert normalized.json()["start"] == "2026-10-16T12:01:00.000Z"
    assert normalized.json()["end"] == "2026-10-16T12:30:00.000Z"
    assert hub.clock.now().hour == 12


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "x" * 129},
        {"agenda": "x" * 1301},
        {"start": "2026-10-01T12:00:00Z", "end": "2026-10-01T13:00:00Z"},
        {"end": "2026-10-20T14:09:00Z"},
        {"end": "2026-10-21T14:00:00Z"},
        {"start": "9999-12-31T22:00:00Z", "end": "9999-12-31T23:00:00Z"},
        {"timezone": "9999-12-31T23:59:00-14:00"},
        {"invitees": [{"email": "maya.chen@halcyonadvisory.example", "displayName": {}}]},
    ],
)
async def test_schedule_validation_matches_webex(client: httpx.AsyncClient, fields: dict[str, Any]) -> None:
    assert (await client.post("/v1/meetings", json={**SCHEDULE, **fields})).status_code == 400


async def test_participant_attendance_is_host_only_and_parent_ids_work(client: httpx.AsyncClient) -> None:
    meeting = (
        await client.post(
            "/v1/meetings", json={**SCHEDULE, "invitees": [{"email": "maya.chen@halcyonadvisory.example"}]}
        )
    ).json()
    maya = bearer(await mint(client, BASE, user="p018"))
    assert (await client.get(f"/v1/meetings/{meeting['id']}", headers=maya)).status_code == 200
    assert (
        await client.get("/v1/meetingParticipants", params={"meetingId": meeting["id"]}, headers=maya)
    ).status_code == 404
    first = (await client.get("/v1/meetings", params=HISTORY)).json()["items"][0]
    actual = (await client.get("/v1/meetingParticipants", params={"meetingId": first["id"]})).json()["items"]
    for key in (first["scheduledMeetingId"], first["meetingSeriesId"]):
        rows = (await client.get("/v1/meetingParticipants", params={"meetingId": key})).json()["items"]
        assert rows and all(r["meetingId"].startswith(first["meetingSeriesId"]) for r in rows)
    assert actual


async def test_series_title_update_preserves_occurrence_dates_and_acl(
    client: httpx.AsyncClient, hub: EmulatorHub
) -> None:
    ctx = hub.context("webex")
    series = next(m for m in ctx.store.collection("meetings") if m.get("recurrence"))
    occurrences = [
        m
        for m in ctx.store.collection("meetings")
        if m.get("_seriesId") == series["id"] and m["meetingType"] == "scheduledMeeting"
    ]
    before = {m["id"]: (m["start"], m["end"], list(m["_people"])) for m in occurrences}
    updated = await client.patch(f"/v1/meetings/{series['id']}", json={"title": "Updated series"})
    assert updated.status_code == 200
    for occurrence in occurrences:
        actual = (await client.get(f"/v1/meetings/{occurrence['id']}")).json()
        assert (actual["start"], actual["end"], occurrence["_people"]) == before[occurrence["id"]]
    assert (
        await client.patch(
            f"/v1/meetings/{series['id']}",
            json={"start": "2026-10-21T19:00:00Z", "end": "2026-10-21T20:00:00Z"},
        )
    ).status_code == 400


async def test_recording_filters_defaults_links_and_recycle_lifecycle(
    client: httpx.AsyncClient, hub: EmulatorHub
) -> None:
    first = (await client.get("/v1/meetings", params=HISTORY)).json()["items"][0]
    recording = (await client.get("/v1/recordings", params={"meetingId": first["id"]})).json()["items"][0]
    assert (
        len(
            (
                await client.get(
                    "/v1/recordings", params={"from": "2026-01-01", "to": "2026-10-16"}, headers=ADMIN
                )
            ).json()["items"]
        )
        == 4
    )
    assert re.fullmatch(r"[a-f0-9]{32}", recording["id"])
    for key in (recording["scheduledMeetingId"], recording["meetingSeriesId"]):
        rows = (
            await client.get("/v1/recordings", params={"meetingId": key, "siteUrl": "other.webex.com"})
        ).json()["items"]
        assert recording["id"] in {r["id"] for r in rows}
    # Supplying only from sets to=from+7 days, rather than extending to the clock.
    rows = (await client.get("/v1/recordings", params={"from": "2026-09-09"})).json()["items"]
    assert [r["id"] for r in rows] == [recording["id"]]
    for query in (
        {"format": "ARF"},
        {"topic": "does not exist"},
        {"serviceType": "TrainingCenter"},
        {"integrationTag": "unknown"},
    ):
        result = await client.get("/v1/recordings", params={"meetingId": first["id"], **query})
        assert result.status_code == 200 and result.json()["items"] == []
    to = await client.get("/v1/recordings", params={"meetingId": first["id"], "to": recording["createTime"]})
    assert to.json()["items"] == []
    detail = (await client.get(f"/v1/recordings/{recording['id']}")).json()
    links = detail["temporaryDirectDownloadLinks"]
    assert links["expiration"] == "2026-10-16T15:00:00.000Z"
    hub.clock.advance(timedelta(hours=2))
    assert (await client.get(links["recordingDownloadLink"])).status_code == 200
    invalid = await client.post(
        "/v1/recordings/softDelete", json={"recordingIds": [recording["id"], "unknown"]}
    )
    assert invalid.status_code == 404
    assert (await client.get(f"/v1/recordings/{recording['id']}")).json()["status"] == "available"
    assert (
        await client.post("/v1/recordings/softDelete", json={"recordingIds": [recording["id"]]})
    ).status_code == 204
    assert (await client.get(links["recordingDownloadLink"])).status_code == 403
    assert (await client.get(f"/v1/recordings/{recording['id']}")).json()["status"] == "deleted"
    assert (await client.post("/v1/recordings/restore", json={"restoreAll": True})).status_code == 204
    assert (await client.get(f"/v1/recordings/{recording['id']}")).json()["status"] == "available"
    assert (await client.get(links["recordingDownloadLink"])).status_code == 403
    assert (await client.delete(f"/v1/recordings/{recording['id']}")).status_code == 204
    assert (await client.get(f"/v1/recordings/{recording['id']}")).status_code == 404
    assert (
        await client.post("/v1/recordings/restore", json={"recordingIds": [recording["id"]]})
    ).status_code == 404


async def test_transcript_filters_and_required_ended_instance(client: httpx.AsyncClient) -> None:
    first = (await client.get("/v1/meetings", params=HISTORY)).json()["items"][0]
    assert (
        await client.get("/v1/meetingTranscripts", params={"meetingId": first["meetingSeriesId"]})
    ).status_code == 400
    result = await client.get(
        "/v1/meetingTranscripts", params={"meetingId": first["id"], "siteUrl": "other.webex.com"}
    )
    assert result.json()["items"] == []
    transcript = (await client.get("/v1/meetingTranscripts", params={"meetingId": first["id"]})).json()[
        "items"
    ][0]
    assert transcript["meetingSeriesId"] == first["meetingSeriesId"]
    assert (
        await client.get("/v1/meetingTranscripts", params={"hostEmail": first["hostEmail"]})
    ).status_code == 400
    for path in (f"/v1/meetingTranscripts/{transcript['id']}/download",):
        assert (
            await client.get(path, params={"hostEmail": "other@halcyonadvisory.example"}, headers=ADMIN)
        ).status_code == 404
    assert len((await client.get("/v1/meetingTranscripts", headers=ADMIN)).json()["items"]) == 1


async def test_message_constraints_mentions_thread_filters_and_atomicity(
    client: httpx.AsyncClient, resources: dict[str, str]
) -> None:
    room = resources["room"]
    for fields in (
        {"text": "é" * 3720},
        {"files": ["https://files.example/a", "https://files.example/b"]},
        {"files": ["https://["]},
    ):
        result = await client.post("/v1/messages", json={"roomId": room, "text": "x", **fields})
        assert result.status_code == 400
    before = (await client.get("/v1/rooms", params={"type": "direct"})).json()["items"]
    rejected = await client.post(
        "/v1/messages",
        json={"toPersonEmail": "maya.chen@halcyonadvisory.example", "text": "x", "parentId": "unknown"},
    )
    assert rejected.status_code == 400
    assert (await client.get("/v1/rooms", params={"type": "direct"})).json()["items"] == before
    root = (await client.post("/v1/messages", json={"roomId": room, "text": "Root"})).json()
    reply = (
        await client.post(
            "/v1/messages",
            json={
                "roomId": room,
                "parentId": root["id"],
                "markdown": "Hi <@personEmail:maya.chen@halcyonadvisory.example>",
            },
        )
    ).json()
    maya = bearer(await mint(client, BASE, user="p018"))
    listed = await client.get(
        "/v1/messages", params={"roomId": room, "parentId": root["id"], "mentionedPeople": "me"}, headers=maya
    )
    assert [r["id"] for r in listed.json()["items"]] == [reply["id"]]
    assert (
        await client.get(
            "/v1/messages", params={"roomId": room, "mentionedPeople": reply["personId"]}, headers=maya
        )
    ).status_code == 400
    assert (
        await client.get("/v1/messages", params={"roomId": room, "mentionedPeople": "me", "max": 101})
    ).status_code == 400
    direct = (
        await client.post(
            "/v1/messages", json={"toPersonEmail": "maya.chen@halcyonadvisory.example", "text": "Hello"}
        )
    ).json()
    memberships = (await client.get("/v1/memberships", params={"roomId": direct["roomId"]})).json()["items"]
    assert (await client.delete(f"/v1/memberships/{memberships[0]['id']}")).status_code == 400
    assert (await client.delete(f"/v1/rooms/{room}", headers=maya)).status_code == 204
    assert (await client.get(f"/v1/rooms/{room}")).status_code == 200
    assert (await client.get(f"/v1/rooms/{room}", headers=maya)).status_code == 404


async def test_webhook_read_scope_ownership_update_and_metadata(
    client: httpx.AsyncClient, hub: EmulatorHub
) -> None:
    hub.context("webex").webhooks.mode = "manual"
    limited = bearer(
        await mint(
            client, BASE, user="p005", client_id="first-app", scopes=["spark:messages_read"], expires_in=1
        )
    )
    hook = await client.post("/v1/webhooks", headers=limited, json=HOOK)
    assert hook.status_code == 200
    key = hook.json()["id"]
    assert len((await client.get("/v1/webhooks", headers=limited)).json()["items"]) == 1
    other = bearer(
        await mint(client, BASE, user="p005", client_id="second-app", scopes=["spark:messages_read"])
    )
    assert (await client.get(f"/v1/webhooks/{key}", headers=other)).status_code == 404
    assert (await client.get("/v1/webhooks", headers=other)).json()["items"] == []
    for fields in ({"status": "inactive"}, {"targetUrl": "https://["}):
        assert (
            await client.put(
                f"/v1/webhooks/{key}",
                headers=limited,
                json={"name": "audit", "targetUrl": HOOK["targetUrl"], **fields},
            )
        ).status_code == 400
    assert (
        await client.put(f"/v1/webhooks/{key}", headers=limited, json={"name": "missing URL"})
    ).status_code == 400
    assert (
        await client.post(
            "/v1/webhooks",
            json={
                **HOOK,
                "resource": "meetings",
                "filter": "hostEmail=sofia.alvarez@halcyonadvisory.example",
            },
        )
    ).status_code == 400
    room = (await client.post("/v1/rooms", json={"title": "Secret room"})).json()
    await client.post(
        "/v1/memberships", json={"roomId": room["id"], "personEmail": "maya.chen@halcyonadvisory.example"}
    )
    hub.clock.advance(timedelta(minutes=1))
    message = (await client.post("/v1/messages", json={"roomId": room["id"], "text": "Sensitive"})).json()
    deliveries = (await client.get("/_emulator/webhooks")).json()["deliveries"]
    event = json.loads(deliveries[0]["body"])
    assert event["created"] == hook.json()["created"]
    assert event["data"]["id"] == message["id"] and "text" not in event["data"]
    assert event["createdBy"] == message["personId"]
    # Expiration of the registering token does not disable a webhook; deletion/disablement of its owner does.
    hub.context("webex").store.collection("people").get(message["personId"])["_loginEnabled"] = False
    maya = bearer(await mint(client, BASE, user="p018"))
    assert (
        await client.post("/v1/messages", headers=maya, json={"roomId": room["id"], "text": "Next"})
    ).status_code == 200
    assert hub.context("webex").store.collection("webhooks").get(key)["status"] == "inactive"
    assert len((await client.get("/_emulator/webhooks")).json()["deliveries"]) == 1


async def test_admin_mutation_webhooks_identify_actor_and_host(
    client: httpx.AsyncClient, hub: EmulatorHub
) -> None:
    hub.context("webex").webhooks.mode = "manual"
    meeting = (await client.post("/v1/meetings", json=SCHEDULE)).json()
    hook = await client.post("/v1/webhooks", json={**HOOK, "resource": "meetings", "event": "updated"})
    assert hook.status_code == 200
    assert (
        await client.patch(f"/v1/meetings/{meeting['id']}", headers=ADMIN, json={"title": "Admin edit"})
    ).status_code == 200
    event = json.loads((await client.get("/_emulator/webhooks")).json()["deliveries"][0]["body"])
    admin = (await client.get("/v1/people/me", headers=ADMIN)).json()
    assert event["actorId"] == admin["id"]
    assert event["data"]["hostPersonId"] == meeting["hostUserId"]
    assert event["data"]["meetingId"] == meeting["id"]


async def test_pkce_plain_default_utf8_form_and_mint_options(client: httpx.AsyncClient) -> None:
    approved = await client.get(
        "/v1/authorize",
        params={
            "client_id": "plain-pkce",
            "response_type": "code",
            "redirect_uri": "https://consumer.example/callback",
            "login_hint": "p005",
            "code_challenge": "fixture-verifier",
        },
    )
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "client_id": "plain-pkce",
        "code": code,
        "redirect_uri": "https://consumer.example/callback",
        "code_verifier": "fixture-verifier",
    }
    assert (await client.post("/v1/access_token", data=form)).status_code == 200
    assert (
        await client.post(
            "/v1/access_token", content=b"\xff", headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
    ).status_code == 400
    for fields in (
        {"expires_in": 10**100},
        {"expires_in": []},
        {"expires_in": True},
        {"client_id": []},
        {"app_only": "false"},
    ):
        assert (await client.post("/_emulator/tokens", json={"user": "p005", **fields})).status_code == 400
    assert (
        await client.post("/v1/rooms", content='{"title":"x"}', headers={"Content-Type": "text/plain"})
    ).status_code == 415


async def test_pagination_cursor_replay_final_page_and_strict_base64(client: httpx.AsyncClient) -> None:
    first = await client.get("/v1/meetings", params={**HISTORY, "max": 1}, headers=ADMIN)
    link = first.links["next"]["url"]
    assert (await client.get(link, headers=ADMIN)).json() == (await client.get(link, headers=ADMIN)).json()
    rows = first.json()["items"]
    while "next" in first.links:
        first = await client.get(first.links["next"]["url"], headers=ADMIN)
        rows.extend(first.json()["items"])
    assert len(rows) == len({r["id"] for r in rows}) == 4
    assert "Link" not in first.headers
    for cursor in ("!!!!MA", "LTE", "YWJj", "é"):
        assert (
            await client.get("/v1/meetings", params={**HISTORY, "cursor": cursor}, headers=ADMIN)
        ).status_code == 400


async def test_meeting_api_timezone_headers_convert_without_changing_store(client: httpx.AsyncClient) -> None:
    first = (await client.get("/v1/meetings", params=HISTORY)).json()["items"][0]
    headers = {"timezone": "America/Toronto"}
    localized = (await client.get(f"/v1/meetings/{first['id']}", headers=headers)).json()
    assert localized["start"] == "2026-09-09T14:00:00.000-04:00"
    assert (await client.get(f"/v1/meetings/{first['id']}")).json()["start"] == first["start"]
    row = (await client.get("/v1/recordings", params={"meetingId": first["id"]}, headers=headers)).json()[
        "items"
    ][0]
    assert row["timeRecorded"].endswith("-04:00")
    detail = (await client.get(f"/v1/recordings/{row['id']}", headers=headers)).json()
    assert detail["temporaryDirectDownloadLinks"]["expiration"].endswith("-04:00")
    participants = (
        await client.get("/v1/meetingParticipants", params={"meetingId": first["id"]}, headers=headers)
    ).json()["items"]
    assert participants[0]["joinedTime"].endswith("-04:00")
    assert participants[0]["devices"][0]["joinedTime"].endswith("-04:00")
    transcript = (
        await client.get("/v1/meetingTranscripts", params={"meetingId": first["id"]}, headers=headers)
    ).json()["items"][0]
    assert transcript["startTime"].endswith("-04:00")
    assert (
        await client.get(f"/v1/meetings/{first['id']}", headers={"timezone": "Bad/Timezone"})
    ).status_code == 400
