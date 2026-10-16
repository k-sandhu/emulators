"""Webhooks: endpoint validation handshake, signatures, API-write events and live-meeting simulation."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from emulators.core.webhooks import hmac_sha256_hex
from emulators.testing.helpers import bearer
from emulators.world import media
from tests.providers.zoom.conftest import (
    API,
    HOST,
    MAYA,
    ORCHID_ID,
    STAFF,
    V2,
    double_encode,
)

URL = "http://consumer.test/zoom/hook"
SECRET = "hunter2-secret-token"
ACTIONS = f"{API}/_emulator/actions"


class Receiver:
    """A consumer endpoint written the way Zoom's docs tell you to write one."""

    def __init__(self, secret: str = SECRET, *, answer_validation: bool = True, status: int = 200) -> None:
        self.secret = secret
        self.answer_validation = answer_validation
        self.status = status
        self.events: list[dict[str, Any]] = []
        self.raw: list[bytes] = []
        self.signatures_ok: list[bool] = []
        self.validations = 0
        self.app = FastAPI()

        @self.app.post("/zoom/hook")
        async def hook(request: Request) -> Any:
            raw = await request.body()
            body = json.loads(raw)
            if body.get("event") == "endpoint.url_validation":
                self.validations += 1
                if not self.answer_validation:
                    return {"plainToken": body["payload"]["plainToken"], "encryptedToken": "wrong"}
                plain = body["payload"]["plainToken"]
                return {"plainToken": plain, "encryptedToken": hmac_sha256_hex(self.secret, plain)}
            ts = request.headers["x-zm-request-timestamp"]
            # Zoom's sample re-serialises the parsed body (JSON.stringify): compact separators, raw UTF-8
            message = f"v0:{ts}:{json.dumps(body, separators=(',', ':'), ensure_ascii=False)}"
            expected = "v0=" + hmac_sha256_hex(self.secret, message)
            self.signatures_ok.append(
                request.headers["x-zm-signature"] == expected and message.split(":", 2)[2] == raw.decode()
            )
            self.events.append(body)
            self.raw.append(raw)
            from fastapi.responses import JSONResponse

            return JSONResponse({}, status_code=self.status)

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]

    def of(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


@pytest.fixture
async def hooked(zoom: httpx.AsyncClient) -> AsyncIterator[Receiver]:
    receiver = Receiver()
    zoom.hub.webhook_transport = httpx.ASGITransport(app=receiver.app)  # type: ignore[attr-defined]
    configured = await zoom.post(f"{ACTIONS}/configure_webhook", json={"url": URL, "secret_token": SECRET})
    assert configured.status_code == 200, configured.text
    yield receiver


async def action(client: httpx.AsyncClient, action_name: str, /, **payload: Any) -> dict[str, Any]:
    r = await client.post(f"{ACTIONS}/{action_name}", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


async def test_url_validation_handshake_and_endpoint_registry(zoom: httpx.AsyncClient) -> None:
    receiver = Receiver()
    zoom.hub.webhook_transport = httpx.ASGITransport(app=receiver.app)  # type: ignore[attr-defined]
    ok = await action(
        zoom, "configure_webhook", url=URL, secret_token=SECRET, events=["meeting.created", "meeting.ended"]
    )
    assert receiver.validations == 1
    assert (
        ok["url"] == URL and ok["events"] == ["meeting.created", "meeting.ended"] and ok["validated"] is True
    )
    log = (await zoom.get(f"{API}/_emulator/webhooks")).json()
    assert log["count"] == 0  # the handshake is not an event delivery
    again = await action(zoom, "configure_webhook", url=URL, secret_token=SECRET)
    assert again["id"] == ok["id"] and again["events"] != ok["events"]  # same url: replaced
    removed = await action(zoom, "remove_webhook", url=URL)
    assert removed == {"removed": 1}


async def test_failed_handshake_is_refused_and_skippable(zoom: httpx.AsyncClient) -> None:
    receiver = Receiver(answer_validation=False)
    zoom.hub.webhook_transport = httpx.ASGITransport(app=receiver.app)  # type: ignore[attr-defined]
    refused = await zoom.post(f"{ACTIONS}/configure_webhook", json={"url": URL, "secret_token": SECRET})
    assert refused.status_code == 400 and refused.json()["error"] == "endpoint_validation_failed"
    assert "hmac_sha256" in refused.json()["detail"]
    assert (await zoom.get(f"{API}/_emulator/state/webhook_endpoints")).json()["count"] == 0
    skipped = await zoom.post(
        f"{ACTIONS}/configure_webhook", json={"url": URL, "secret_token": SECRET, "skip_validation": True}
    )
    assert skipped.status_code == 200 and skipped.json()["validated"] is False
    bad_event = await zoom.post(
        f"{ACTIONS}/configure_webhook",
        json={"url": URL, "events": ["meeting.exploded"], "skip_validation": True},
    )
    assert bad_event.status_code == 400
    assert (await zoom.post(f"{ACTIONS}/configure_webhook", json={})).status_code == 400
    unreachable = await zoom.post(
        f"{ACTIONS}/configure_webhook", json={"url": "http://nowhere.test/x", "secret_token": "s"}
    )
    assert unreachable.status_code == 400


async def test_api_writes_emit_meeting_events(
    zoom: httpx.AsyncClient, staff: dict[str, str], hooked: Receiver
) -> None:
    created = (
        await zoom.post(
            f"{V2}/users/me/meetings", headers=staff,
            json={"topic": "Café — kickoff", "type": 2, "start_time": "2026-11-10T15:30:00Z", "duration": 30},
        )
    ).json()  # fmt: skip
    assert hooked.names() == ["meeting.created"]
    event = hooked.events[0]
    assert set(event) == {"event", "event_ts", "payload"} and isinstance(event["event_ts"], int)
    payload = event["payload"]
    assert payload["account_id"] == zoom.hub.world.org.zoom_account_id  # type: ignore[attr-defined]
    assert payload["operator"] == STAFF and len(payload["operator_id"]) == 22
    obj = payload["object"]
    assert obj["id"] == created["id"] and obj["uuid"] == created["uuid"] and obj["topic"] == "Café — kickoff"
    assert obj["type"] == 2 and obj["start_time"] == "2026-11-10T15:30:00Z" and obj["duration"] == 30
    assert (
        obj["join_url"] == created["join_url"]
        and obj["password"] == created["password"]
        and "settings" in obj
    )
    assert hooked.signatures_ok == [True]  # includes the non-ASCII em dash in the topic

    await zoom.patch(
        f"{V2}/meetings/{created['id']}", headers=staff, json={"topic": "Renamed", "duration": 45}
    )
    updated = hooked.of("meeting.updated")[0]["payload"]
    assert updated["object"]["topic"] == "Renamed" and updated["object"]["duration"] == 45
    assert updated["old_object"]["topic"] == "Café — kickoff" and updated["old_object"]["duration"] == 30
    assert updated["object"]["id"] == created["id"] and updated["time_stamp"] > 0

    await zoom.delete(f"{V2}/meetings/{created['id']}", headers=staff)
    deleted = hooked.of("meeting.deleted")[0]["payload"]["object"]
    assert deleted["id"] == created["id"] and deleted["topic"] == "Renamed"
    assert hooked.names() == ["meeting.created", "meeting.updated", "meeting.deleted"]
    assert all(hooked.signatures_ok)


async def test_event_subscriptions_filter_deliveries(zoom: httpx.AsyncClient, staff: dict[str, str]) -> None:
    receiver = Receiver()
    zoom.hub.webhook_transport = httpx.ASGITransport(app=receiver.app)  # type: ignore[attr-defined]
    await action(zoom, "configure_webhook", url=URL, secret_token=SECRET, events=["meeting.deleted"])
    made = (await zoom.post(f"{V2}/users/me/meetings", headers=staff, json={"topic": "x"})).json()
    await zoom.patch(f"{V2}/meetings/{made['id']}", headers=staff, json={"topic": "y"})
    assert receiver.events == []
    await zoom.delete(f"{V2}/meetings/{made['id']}", headers=staff)
    assert receiver.names() == ["meeting.deleted"]


async def test_failing_consumer_is_retried_and_journaled(
    zoom: httpx.AsyncClient, staff: dict[str, str]
) -> None:
    receiver = Receiver(status=500)
    zoom.hub.webhook_transport = httpx.ASGITransport(app=receiver.app)  # type: ignore[attr-defined]
    await action(zoom, "configure_webhook", url=URL, secret_token=SECRET)
    await zoom.post(f"{V2}/users/me/meetings", headers=staff, json={"topic": "x"})
    deliveries = (
        await zoom.get(f"{API}/_emulator/webhooks", params={"event_type": "meeting.created"})
    ).json()["deliveries"]
    assert len(deliveries) == 1 and deliveries[0]["status"] == "failed" and deliveries[0]["attempts"] == 3
    assert deliveries[0]["response_status"] == 500
    assert {"x-zm-signature", "x-zm-request-timestamp"} <= {h.lower() for h in deliveries[0]["headers"]}
    # every attempt carried a fresh, valid signature
    assert len(receiver.events) == 3 and all(receiver.signatures_ok)


async def test_live_meeting_lifecycle_creates_recordings_and_events(
    zoom: httpx.AsyncClient, staff: dict[str, str], maya: dict[str, str], hooked: Receiver
) -> None:
    made = (
        await zoom.post(
            f"{V2}/users/me/meetings", headers=staff,
            json={"topic": "Quarterly planning", "type": 2, "start_time": "2026-11-10T15:30:00Z", "duration": 40, "settings": {"auto_recording": "cloud"}},
        )
    ).json()  # fmt: skip
    mid = made["id"]
    started = await action(
        zoom, "start_meeting", meeting_id=mid,
        participants=[MAYA, {"name": "Gina Guest", "email": "gina@client.example"}],
    )  # fmt: skip
    assert started["status"] == "started" and started["uuid"] == made["uuid"]
    assert hooked.names() == ["meeting.created", "meeting.started"] + ["meeting.participant_joined"] * 3
    host_join, maya_join, guest_join = (
        e["payload"]["object"] for e in hooked.of("meeting.participant_joined")
    )
    assert (
        host_join["participant"]["user_name"] == "Ryan Murphy" and host_join["participant"]["email"] == STAFF
    )
    assert maya_join["participant"]["email"] == MAYA and len(maya_join["participant"]["user_id"]) == 16
    assert guest_join["participant"]["user_name"] == "Gina Guest" and guest_join["participant"]["email"] == ""
    assert (
        host_join["id"] == str(mid)
        and host_join["uuid"] == made["uuid"]
        and host_join["host_id"] == made["host_id"]
    )
    assert hooked.of("meeting.started")[0]["payload"]["object"]["id"] == str(mid)

    # while it runs: status, live listings, double-start refusal
    assert (await zoom.get(f"{V2}/meetings/{mid}", headers=staff)).json()["status"] == "started"
    assert [
        m["id"]
        for m in (await zoom.get(f"{V2}/users/me/meetings", headers=staff, params={"type": "live"})).json()[
            "meetings"
        ]
    ] == [mid]
    live = (
        await zoom.get(f"{V2}/metrics/meetings", headers=await _s2s(zoom), params={"type": "live"})
    ).json()
    assert [m["id"] for m in live["meetings"]] == [mid] and live["meetings"][0]["participants"] == 3
    assert (await zoom.post(f"{ACTIONS}/start_meeting", json={"meeting_id": mid})).status_code == 400
    assert (
        await zoom.delete(f"{V2}/meetings/{mid}", headers=staff)
    ).status_code == 400  # cannot delete a running meeting
    gone = await action(zoom, "participant_leave", meeting_id=mid, name="Gina Guest")
    assert gone["duration"] >= 0
    joiner = await action(
        zoom, "participant_join", meeting_id=mid, user="priya.raman@halcyonadvisory.example"
    )
    assert joiner["name"] == "Priya Raman"

    ended = await action(
        zoom, "end_meeting", meeting_id=mid, duration_minutes=35,
        chat=[{"from": "Gina Guest", "text": "Thanks for having me!", "offset_s": 90}],
    )  # fmt: skip
    assert ended["status"] == "ended" and ended["duration"] == 35 and ended["recording"] is True
    names = hooked.names()
    assert names[5:7] == ["meeting.participant_left", "meeting.participant_joined"]  # Gina left, Priya joined
    # ending: everyone still in leaves (host, Maya, Priya), then the meeting ends, then recordings are processed
    assert names[7:] == ["meeting.participant_left"] * 3 + [
        "meeting.ended",
        "recording.completed",
        "recording.transcript_completed",
    ]
    assert all(hooked.signatures_ok)
    uuid = ended["uuid"]
    assert (await zoom.get(f"{V2}/meetings/{mid}", headers=staff)).json()["status"] == "waiting"

    ended_obj = hooked.of("meeting.ended")[0]["payload"]["object"]
    assert ended_obj["id"] == str(mid) and ended_obj["uuid"] == uuid and ended_obj["end_time"].endswith("Z")
    done = hooked.of("recording.completed")[0]
    assert set(done) == {"event", "event_ts", "payload", "download_token"}
    obj = done["payload"]["object"]
    assert (
        obj["uuid"] == uuid
        and obj["id"] == mid
        and obj["host_email"] == STAFF
        and obj["topic"] == "Quarterly planning"
    )
    assert [f["file_type"] for f in obj["recording_files"]] == [
        "MP4",
        "M4A",
        "TRANSCRIPT",
        "CHAT",
        "TIMELINE",
    ]
    assert obj["recording_count"] == 5 and obj["total_size"] == sum(
        f["file_size"] for f in obj["recording_files"]
    )
    assert obj["share_url"].startswith("https://us06web.zoom.us/rec/share/")
    transcript_event = hooked.of("recording.transcript_completed")[0]
    assert [f["file_type"] for f in transcript_event["payload"]["object"]["recording_files"]] == [
        "TRANSCRIPT"
    ]
    assert transcript_event["download_token"]

    # the files in the event download with the event's download_token (Bearer or query) and are valid
    vtt_file = next(f for f in obj["recording_files"] if f["file_type"] == "TRANSCRIPT")
    for kwargs in (
        {"headers": bearer(done["download_token"])},
        {"params": {"access_token": done["download_token"]}},
    ):
        got = await zoom.get(vtt_file["download_url"], follow_redirects=True, **kwargs)
        assert got.status_code == 200 and int(got.headers["content-length"]) == vtt_file["file_size"]
    cues = media.parse_vtt(
        (
            await zoom.get(
                vtt_file["download_url"], headers=bearer(done["download_token"]), follow_redirects=True
            )
        ).content
    )
    assert len(cues) >= 5 and {c.speaker for c in cues} <= {
        "Ryan Murphy",
        "Maya Chen",
        "Gina Guest",
        "Priya Raman",
    }
    chat_file = next(f for f in obj["recording_files"] if f["file_type"] == "CHAT")
    chat = await zoom.get(
        chat_file["download_url"], headers=bearer(done["download_token"]), follow_redirects=True
    )
    assert (
        chat.text == "00:01:30\t From  Gina Guest to  Everyone:\tThanks for having me!\n"
    )  # offset_s is from recording start

    # and it is all visible through the regular API
    past = (await zoom.get(f"{V2}/past_meetings/{double_encode(uuid)}", headers=staff)).json()
    assert past["duration"] == 35 and past["participants_count"] == 4 and past["user_email"] == STAFF
    people = {
        p["name"]: p
        for p in (
            await zoom.get(f"{V2}/past_meetings/{double_encode(uuid)}/participants", headers=staff)
        ).json()["participants"]
    }
    assert set(people) == {"Ryan Murphy", "Maya Chen", "Gina Guest", "Priya Raman"}
    assert people["Gina Guest"]["internal_user"] is False and people["Gina Guest"]["user_id"] == ""
    assert people["Ryan Murphy"]["duration"] == 35 * 60
    recs = (await zoom.get(f"{V2}/meetings/{double_encode(uuid)}/recordings", headers=staff)).json()
    assert recs["recording_count"] == 5
    summary = await zoom.get(f"{V2}/meetings/{double_encode(uuid)}/meeting_summary", headers=staff)
    assert summary.status_code == 200 and summary.json()["meeting_topic"] == "Quarterly planning"
    instances = (await zoom.get(f"{V2}/past_meetings/{mid}/instances", headers=staff)).json()["meetings"]
    assert [i["uuid"] for i in instances] == [uuid]
    _ = maya


async def _s2s(client: httpx.AsyncClient) -> dict[str, str]:
    from tests.providers.zoom.conftest import token_for

    return await token_for(client)


async def test_end_via_api_follows_the_auto_recording_setting(
    zoom: httpx.AsyncClient, staff: dict[str, str], hooked: Receiver
) -> None:
    quiet = (await zoom.post(f"{V2}/users/me/meetings", headers=staff, json={"topic": "No recording"})).json()
    loud = (
        await zoom.post(
            f"{V2}/users/me/meetings",
            headers=staff,
            json={"topic": "Recorded", "settings": {"auto_recording": "cloud"}},
        )
    ).json()
    assert (
        await zoom.put(f"{V2}/meetings/{quiet['id']}/status", headers=staff, json={"action": "end"})
    ).status_code == 400
    for m in (quiet, loud):
        await action(zoom, "start_meeting", meeting_id=m["id"])
        assert (
            await zoom.put(f"{V2}/meetings/{m['id']}/status", headers=staff, json={"action": "end"})
        ).status_code == 204
    assert len(hooked.of("recording.completed")) == 1
    assert hooked.of("recording.completed")[0]["payload"]["object"]["topic"] == "Recorded"
    assert (
        await zoom.put(f"{V2}/meetings/{loud['id']}/status", headers=staff, json={"action": "pause"})
    ).status_code == 400


async def test_recurring_meeting_instances_get_their_own_uuid(
    zoom: httpx.AsyncClient, host: dict[str, str], hooked: Receiver, ctx: Any
) -> None:
    before = {i["id"] for i in ctx.store.collection("instances") if i["meeting_id"] == ORCHID_ID}
    started = await action(zoom, "start_meeting", meeting_id=ORCHID_ID, participants="all")
    assert started["uuid"] not in before  # the next scheduled occurrence has its own UUID
    assert len(hooked.of("meeting.participant_joined")) >= 5  # the host plus every invited attendee
    ended = await action(zoom, "end_meeting", meeting_id=ORCHID_ID, record=False)
    assert ended["recording"] is False and "recording.completed" not in hooked.names()
    instances = (await zoom.get(f"{V2}/past_meetings/{ORCHID_ID}/instances", headers=host)).json()["meetings"]
    assert len(instances) == len(before) + 1 and started["uuid"] in {i["uuid"] for i in instances}
    detail = (await zoom.get(f"{V2}/meetings/{ORCHID_ID}", headers=host)).json()
    assert detail["status"] == "waiting" and detail["uuid"] == started["uuid"]
    # that occurrence is now history: it drops out of the default occurrence list
    assert len(detail["occurrences"]) == 1


async def test_recording_api_events(
    zoom: httpx.AsyncClient, host: dict[str, str], hooked: Receiver, ctx: Any
) -> None:
    uuid = sorted(
        (r for r in ctx.store.collection("recordings") if r["meeting_id"] == ORCHID_ID),
        key=lambda r: r["start_time"],
    )[0]["id"]
    path = f"{V2}/meetings/{double_encode(uuid)}/recordings"
    await zoom.delete(path, headers=host)
    trashed = hooked.of("recording.trashed")[0]["payload"]
    assert trashed["object"]["uuid"] == uuid and trashed["operator"] == HOST
    assert trashed["object"]["recording_count"] == 4
    await zoom.put(f"{path}/status", headers=host, json={"action": "recover"})
    assert hooked.of("recording.recovered")[0]["payload"]["object"]["uuid"] == uuid
    await zoom.delete(path, headers=host, params={"action": "delete"})
    assert hooked.of("recording.deleted")[0]["payload"]["object"]["uuid"] == uuid


async def test_replaying_events_for_seeded_meetings(
    zoom: httpx.AsyncClient, hooked: Receiver, ctx: Any
) -> None:
    uuid = sorted(
        (r for r in ctx.store.collection("recordings") if r["meeting_id"] == ORCHID_ID),
        key=lambda r: r["start_time"],
    )[0]["id"]
    sent = await action(zoom, "replay_event", event="recording.completed", uuid=uuid)
    assert (
        sent["deliveries"][0]["event"] == "recording.completed"
        and sent["deliveries"][0]["status"] == "delivered"
    )
    event = hooked.events[-1]
    assert event["payload"]["object"]["uuid"] == uuid and event["download_token"]
    assert hooked.signatures_ok == [True]
    only_text = await action(
        zoom, "replay_event", event="recording.transcript_completed", meeting_id=ORCHID_ID
    )
    assert only_text["deliveries"][0]["event"] == "recording.transcript_completed"
    joins = await action(zoom, "replay_event", event="meeting.participant_joined", uuid=uuid)
    assert len(joins["deliveries"]) == 6
    assert (
        await zoom.post(f"{ACTIONS}/replay_event", json={"event": "meeting.exploded", "uuid": uuid})
    ).status_code == 400
    assert re.fullmatch(r"[\w.-]+", event["download_token"])


async def test_delivery_headers_and_legacy_verification_token(
    zoom: httpx.AsyncClient, staff: dict[str, str]
) -> None:
    receiver = Receiver()
    zoom.hub.webhook_transport = httpx.ASGITransport(app=receiver.app)  # type: ignore[attr-defined]
    await action(zoom, "configure_webhook", url=URL, secret_token=SECRET, verification_token="legacy-token")
    await zoom.post(f"{V2}/users/me/meetings", headers=staff, json={"topic": "x"})
    delivery = (await zoom.get(f"{API}/_emulator/webhooks")).json()["deliveries"][0]
    headers = {k.lower(): v for k, v in delivery["headers"].items()}
    assert headers["authorization"] == "legacy-token"
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert (
        headers["x-zm-trackingid"].startswith("v=2.0;clid=")
        and headers["user-agent"] == "Zoom Marketplace/1.0a"
    )
    assert headers["x-zm-signature"].startswith("v0=") and headers["x-zm-request-timestamp"].isdigit()
    assert delivery["event_type"] == "meeting.created" and delivery["status"] == "delivered"


async def test_public_base_setting_rewrites_urls_inside_event_payloads() -> None:
    from emulators.core.hub import EmulatorHub

    hub = EmulatorHub(
        providers=["zoom"],
        anchor="2026-10-16",
        webhook_mode="sync",
        settings={"zoom": {"public_base": "http://localhost:44106"}},
    )
    receiver = Receiver()
    hub.webhook_transport = httpx.ASGITransport(app=receiver.app)
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        await action(client, "configure_webhook", url=URL, secret_token=SECRET)
        uuid = sorted(
            (r for r in hub.context("zoom").store.collection("recordings") if r["meeting_id"] == ORCHID_ID),
            key=lambda r: r["start_time"],
        )[0]["id"]
        await action(client, "replay_event", event="recording.completed", uuid=uuid)
    obj = receiver.events[0]["payload"]["object"]
    assert obj["share_url"].startswith("http://localhost:44106/rec/share/")
    assert all(
        f["download_url"].startswith("http://localhost:44106/rec/download/") for f in obj["recording_files"]
    )
    assert receiver.signatures_ok == [True]
