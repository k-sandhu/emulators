"""Event streams, long polling and webhooks v2 (signed deliveries)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, Request

PRIMARY = "box-primary-signature-key"
SECONDARY = "box-secondary-signature-key"


async def position(box, who="p035", stream="all") -> str:
    r = await box.get("/events", who, params={"stream_type": stream, "stream_position": "now"})
    assert r.status_code == 200
    return r.json()["next_stream_position"]


async def events_since(box, pos, who="p035", **params):
    r = await box.get("/events", who, params={"stream_position": pos, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_stream_position_now_and_incremental_reads(box):
    start = await position(box)
    assert start.isdigit()
    empty = await events_since(box, start)
    assert empty["chunk_size"] == 0 and empty["entries"] == [] and empty["next_stream_position"] == start
    parent = await box.find("Technology")
    uploaded = (await box.upload(parent, "event-demo.txt", b"x")).json()["entries"][0]
    body = await events_since(box, start)
    assert body["chunk_size"] == len(body["entries"]) >= 1
    assert int(body["next_stream_position"]) > int(start)
    event = body["entries"][-1]
    assert event["type"] == "event" and event["event_type"] == "ITEM_UPLOAD"
    assert event["created_by"]["name"] == "Arjun Mehta"
    assert event["source"]["id"] == uploaded["id"] and event["source"]["name"] == "event-demo.txt"
    assert event["source"]["parent"]["id"] == parent
    assert len(event["event_id"]) == 36 and len(event["session_id"]) >= 8
    assert event["created_at"] and event["recorded_at"]
    again = await events_since(box, body["next_stream_position"])
    assert again["entries"] == []


async def test_event_types_for_common_operations(box):
    start = await position(box)
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Events")
    fid = (await box.upload(folder, "a.txt", b"a")).json()["entries"][0]["id"]
    await box.put(f"/files/{fid}", json={"name": "b.txt"})
    other = await box.mkdir(parent, "Other")
    await box.put(f"/files/{fid}", json={"parent": {"id": other}})
    await box.post(f"/files/{fid}/copy", json={"parent": {"id": folder}, "name": "c.txt"})
    await box.get(f"/files/{fid}/content")
    await box.delete(f"/files/{fid}")
    await box.post(f"/files/{fid}", json={})
    got = [e["event_type"] for e in (await events_since(box, start, limit=500))["entries"]]
    assert got == [
        "ITEM_CREATE",
        "ITEM_UPLOAD",
        "ITEM_RENAME",
        "ITEM_CREATE",
        "ITEM_MOVE",
        "ITEM_COPY",
        "ITEM_DOWNLOAD",
        "ITEM_TRASH",
        "ITEM_UNDELETE_VIA_TRASH",
    ]
    changes = [
        e["event_type"] for e in (await events_since(box, start, stream_type="changes", limit=500))["entries"]
    ]
    assert "ITEM_DOWNLOAD" not in changes and "ITEM_MOVE" in changes
    ignored = await events_since(box, start, event_type="ITEM_RENAME,ITEM_MOVE")
    assert [e["event_type"] for e in ignored["entries"]] == got  # the user streams ignore ``event_type``
    filtered = await events_since(box, start, stream_type="admin_logs", event_type="RENAME,MOVE")
    assert [e["event_type"] for e in filtered["entries"]] == ["RENAME", "MOVE"]
    limited = await events_since(box, start, limit=2)
    assert limited["chunk_size"] == 2
    rest = await events_since(box, limited["next_stream_position"], limit=500)
    assert len(rest["entries"]) == len(got) - 2


async def test_user_streams_only_carry_visible_events(box):
    start = await position(box, "p044")
    parent = await box.find("Technology")  # growth staff cannot see IT
    await box.upload(parent, "it-only.txt", b"x")
    assert (await events_since(box, start, "p044"))["entries"] == []
    mine = await box.mkdir("0", "Rebecca scratch", "p044")
    await box.upload(mine, "mine.txt", b"x", "p044")
    seen = await events_since(box, start, "p044")
    assert [e["source"]["name"] for e in seen["entries"]] == ["Rebecca scratch", "mine.txt"]
    admin = await events_since(box, start, "p035")
    assert [e["source"]["name"] for e in admin["entries"]] == ["it-only.txt"]
    shared = await box.find("Marketing & BD", "p044")  # team folder owned by the admin, edited by p044
    await box.upload(shared, "shared.txt", b"x", "p044")
    assert "shared.txt" in [e["source"]["name"] for e in (await events_since(box, start, "p035"))["entries"]]


async def test_seeded_history_is_available_from_position_zero(box):
    body = await events_since(box, "0", who="p018", limit=500)
    types = {e["event_type"] for e in body["entries"]}
    assert {"ITEM_UPLOAD", "ITEM_CREATE"} <= types
    times = [datetime.fromisoformat(e["created_at"]) for e in body["entries"]]
    assert times == sorted(times)
    positions = [int(body["next_stream_position"])]
    assert positions[0] > 0


async def test_admin_logs_are_enterprise_wide_and_privileged(box):
    assert (await box.get("/events", "p018", params={"stream_type": "admin_logs"})).status_code == 403
    assert (await box.get("/events", "p035", params={"stream_type": "nonsense"})).status_code == 400
    assert (await box.get("/events", "p035", params={"stream_position": "soon"})).status_code == 400
    box.hub.clock.advance(timedelta(days=2))  # seeded history is dated up to the world anchor
    start = await position(box, stream="admin_logs")
    marker = box.hub.clock.now().isoformat()
    folder = await box.find("Notes", "p018")
    await box.upload(folder, "private.txt", b"x", "p018")
    logs = await events_since(box, start, stream_type="admin_logs")
    # personal folders are visible in the audit log, which names file activity the enterprise way
    assert [e["event_type"] for e in logs["entries"]] == ["UPLOAD"]
    assert (
        await box.get("/events", "p036", params={"stream_type": "admin_logs", "stream_position": start})
    ).status_code == 200
    window = await events_since(
        box,
        "0",
        stream_type="admin_logs",
        event_type="UPLOAD",
        created_after=marker,
        created_before="2999-01-01T00:00:00Z",
    )
    assert [e["source"]["name"] for e in window["entries"]] == ["private.txt"]
    legacy = await events_since(
        box, "0", stream_type="admin_logs", event_type="ITEM_UPLOAD", created_after=marker
    )
    assert [e["event_type"] for e in legacy["entries"]] == ["UPLOAD"]  # the per-user name filters too
    # Box ignores ``event_type`` on the per-user streams: only admin_logs can be filtered by it
    user_stream = await events_since(box, "0", "p018", event_type="UPLOAD", limit=500)
    assert {"ITEM_UPLOAD", "ITEM_CREATE"} <= {e["event_type"] for e in user_stream["entries"]}
    assert (await events_since(box, "0", stream_type="admin_logs", created_before="2000-01-01T00:00:00Z"))[
        "entries"
    ] == []
    enterprise_token = (await box.action("mint_token", {"user": "enterprise"}))["access_token"]
    assert (
        await box.get(
            "/events", enterprise_token, params={"stream_type": "admin_logs", "stream_position": "0"}
        )
    ).status_code == 200


async def test_admin_audit_events_for_users_and_groups(box):
    start = await position(box, stream="admin_logs")
    user = (
        await box.post("/users", json={"name": "Audit Subject", "login": "audit.subject@example.test"})
    ).json()
    group = (await box.post("/groups", json={"name": "Audit Group"})).json()
    membership = (
        await box.post("/group_memberships", json={"user": {"id": user["id"]}, "group": {"id": group["id"]}})
    ).json()
    await box.delete(f"/group_memberships/{membership['id']}")
    await box.delete(f"/groups/{group['id']}")
    await box.delete(f"/users/{user['id']}")
    kinds = [e["event_type"] for e in (await events_since(box, start, stream_type="admin_logs"))["entries"]]
    assert kinds == [
        "NEW_USER",
        "GROUP_CREATION",
        "GROUP_ADD_USER",
        "GROUP_REMOVE_USER",
        "GROUP_DELETION",
        "DELETE_USER",
    ]


async def test_long_poll_discovery_and_notification(box):
    ctx = box.hub.context("box")
    ctx.settings["LONGPOLL_TIMEOUT"] = "0.4"
    try:
        r = await box.client.request("OPTIONS", "https://api.box.com/2.0/events", headers=box.auth())
        assert r.status_code == 200
        entry = r.json()["entries"][0]
        assert r.json()["chunk_size"] == 1
        assert entry["type"] == "realtime_server" and entry["url"].startswith(
            "https://realtime.box.com/subscribe?channel="
        )
        assert entry["ttl"] and entry["max_retries"] and entry["retry_timeout"]
        quiet = await box.client.get(entry["url"], headers=box.auth())
        assert quiet.json() == {"message": "reconnect"}
        parent = await box.find("Technology")

        async def poll():
            return (await box.client.get(entry["url"])).json()

        task = asyncio.create_task(poll())
        await asyncio.sleep(0.1)
        await box.upload(parent, "wake.txt", b"x")
        assert await task == {"message": "new_change"}
        # A stream_position older than the latest event fires immediately.
        immediate = await box.client.get(entry["url"] + "&stream_position=0")
        assert immediate.json() == {"message": "new_change"}
        unknown = await box.client.get("https://realtime.box.com/subscribe?channel=bogus")
        assert unknown.json() == {"message": "reconnect"}
    finally:
        ctx.settings.pop("LONGPOLL_TIMEOUT", None)


# -- webhooks ----------------------------------------------------------------------------------------


class Receiver:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.deliveries: list[tuple[dict[str, str], bytes]] = []
        self.app = FastAPI()

        @self.app.post("/hook")
        async def hook(request: Request):
            self.deliveries.append((dict(request.headers), await request.body()))
            from fastapi.responses import JSONResponse

            return JSONResponse({}, status_code=self.status)


def attach(box, receiver: Receiver) -> None:
    box.hub.webhook_transport = httpx.ASGITransport(app=receiver.app)


def signature(key: str, body: bytes, timestamp: str) -> str:
    return base64.b64encode(
        hmac.new(key.encode(), body + timestamp.encode(), hashlib.sha256).digest()
    ).decode()


async def make_webhook(box, target_id, triggers, target_type="folder", who="p035"):
    return await box.post(
        "/webhooks",
        who,
        json={
            "target": {"id": target_id, "type": target_type},
            "address": "http://consumer.test/hook",
            "triggers": triggers,
        },
    )


async def test_webhook_crud_and_validation(box):
    folder = await box.find("Technology")
    created = await make_webhook(box, folder, ["FILE.UPLOADED", "FOLDER.CREATED"])
    assert created.status_code == 201, created.text
    hook = created.json()
    assert hook["type"] == "webhook" and hook["target"] == {"id": folder, "type": "folder"}
    assert hook["address"] == "http://consumer.test/hook"
    assert hook["triggers"] == ["FILE.UPLOADED", "FOLDER.CREATED"]
    assert hook["created_by"]["name"] == "Arjun Mehta" and hook["created_at"]
    assert (await make_webhook(box, folder, ["FILE.UPLOADED"])).status_code == 409
    listed = (await box.get("/webhooks")).json()
    assert listed["entries"] == [
        {"id": hook["id"], "type": "webhook", "target": {"id": folder, "type": "folder"}}
    ]
    assert listed["limit"] == 100 and listed["next_marker"] is None
    assert (await box.get(f"/webhooks/{hook['id']}")).json() == hook
    assert (await box.get(f"/webhooks/{hook['id']}", "p019")).status_code == 404
    updated = await box.put(
        f"/webhooks/{hook['id']}",
        json={"triggers": ["FILE.DOWNLOADED"], "address": "https://consumer.test/other"},
    )
    assert (
        updated.json()["triggers"] == ["FILE.DOWNLOADED"]
        and updated.json()["address"] == "https://consumer.test/other"
    )
    for bad in (
        {"triggers": ["FILE.WIGGLED"]},
        {"triggers": []},
        {"address": "not-a-url"},
    ):
        assert (await box.put(f"/webhooks/{hook['id']}", json=bad)).status_code == 400, bad
    assert (await make_webhook(box, "999999", ["FILE.UPLOADED"])).status_code == 404
    assert (await make_webhook(box, folder, ["FILE.UPLOADED"], "folder", "p044")).status_code == 404
    kb = await box.find("Knowledge Base", "p044")
    assert (
        await make_webhook(box, kb, ["FILE.UPLOADED"], "folder", "p044")
    ).status_code == 403  # viewers cannot
    assert (await box.post("/webhooks", json={"target": {"id": folder, "type": "folder"}})).status_code == 400
    assert (await box.delete(f"/webhooks/{hook['id']}")).status_code == 204
    assert (await box.get(f"/webhooks/{hook['id']}")).status_code == 404


async def test_webhook_deliveries_are_signed_and_describe_the_event(box):
    receiver = Receiver()
    attach(box, receiver)
    folder = await box.find("Technology")
    hook = (await make_webhook(box, folder, ["FILE.UPLOADED"])).json()
    created_file = (await box.upload(folder, "hooked.txt", b"payload")).json()["entries"][0]
    assert len(receiver.deliveries) == 1
    headers, raw = receiver.deliveries[0]
    assert headers["box-signature-version"] == "1" and headers["box-signature-algorithm"] == "HmacSHA256"
    assert len(headers["box-delivery-id"]) == 36
    timestamp = headers["box-delivery-timestamp"]
    assert datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert headers["box-signature-primary"] == signature(PRIMARY, raw, timestamp)
    assert headers["box-signature-secondary"] == signature(SECONDARY, raw, timestamp)
    assert headers["content-type"].startswith("application/json")
    body = json.loads(raw)
    assert body["type"] == "webhook_event" and body["trigger"] == "FILE.UPLOADED"
    assert body["webhook"] == {"id": hook["id"], "type": "webhook"}
    assert body["id"] == headers["box-delivery-id"]
    assert body["created_by"]["login"] == "arjun.mehta@halcyonadvisory.example"
    assert body["source"]["id"] == created_file["id"] and body["source"]["type"] == "file"
    assert body["source"]["name"] == "hooked.txt"
    log = (await box.client.get("https://api.box.com/_emulator/webhooks")).json()
    assert (
        log["deliveries"][0]["status"] == "delivered"
        and log["deliveries"][0]["event_type"] == "FILE.UPLOADED"
    )


async def test_sdk_style_signature_verification_with_the_configured_keys(box):
    receiver = Receiver()
    attach(box, receiver)
    await box.action("configure_webhook_keys", {"primary": "new-primary-key", "secondary": ""})
    folder = await box.find("Technology")
    await make_webhook(box, folder, ["FILE.UPLOADED"])
    await box.upload(folder, "k.txt", b"x")
    headers, raw = receiver.deliveries[0]
    assert headers["box-signature-primary"] == signature(
        "new-primary-key", raw, headers["box-delivery-timestamp"]
    )
    assert "box-signature-secondary" not in headers
    assert headers["box-signature-primary"] != signature(PRIMARY, raw, headers["box-delivery-timestamp"])


async def test_webhooks_fire_for_the_watched_subtree_and_chosen_triggers(box):
    receiver = Receiver()
    attach(box, receiver)
    parent = await box.find("Technology")
    watched = await box.mkdir(parent, "Watched")
    inner = await box.mkdir(watched, "Inner")
    unwatched = await box.mkdir(parent, "Unwatched")
    await make_webhook(
        box,
        watched,
        [
            "FILE.UPLOADED",
            "FILE.TRASHED",
            "FILE.RENAMED",
            "FILE.MOVED",
            "FILE.DOWNLOADED",
            "FOLDER.CREATED",
            "FILE.COPIED",
        ],
    )
    fid = (await box.upload(inner, "deep.txt", b"x")).json()["entries"][0]["id"]  # nested uploads count
    await box.upload(unwatched, "ignored.txt", b"x")
    await box.mkdir(inner, "Child")
    await box.put(f"/files/{fid}", json={"name": "deep2.txt"})
    await box.put(f"/files/{fid}", json={"parent": {"id": watched}})
    await box.get(f"/files/{fid}/content")
    await box.post(f"/files/{fid}/copy", json={"parent": {"id": watched}, "name": "copy.txt"})
    await box.delete(f"/files/{fid}")
    await box.post(f"/files/{fid}", json={})  # FILE.RESTORED is not subscribed
    triggers = [json.loads(raw)["trigger"] for _, raw in receiver.deliveries]
    assert triggers == [
        "FILE.UPLOADED", "FOLDER.CREATED", "FILE.RENAMED", "FILE.MOVED", "FILE.DOWNLOADED", "FILE.COPIED", "FILE.TRASHED",
    ]  # fmt: skip


async def test_file_target_webhooks_and_collaboration_triggers(box):
    receiver = Receiver()
    attach(box, receiver)
    parent = await box.find("Technology")
    fid = (await box.upload(parent, "single.txt", b"x")).json()["entries"][0]["id"]
    other = (await box.upload(parent, "other.txt", b"x")).json()["entries"][0]["id"]
    await make_webhook(box, fid, ["FILE.RENAMED", "COLLABORATION.CREATED"], "file")
    await box.put(f"/files/{other}", json={"name": "other2.txt"})
    assert receiver.deliveries == []
    await box.put(f"/files/{fid}", json={"name": "single2.txt"})
    user = (await box.get("/users/me", "p044")).json()
    await box.post(
        "/collaborations",
        json={
            "item": {"type": "file", "id": fid},
            "accessible_by": {"type": "user", "id": user["id"]},
            "role": "viewer",
        },
    )
    bodies = [json.loads(raw) for _, raw in receiver.deliveries]
    assert [b["trigger"] for b in bodies] == ["FILE.RENAMED", "COLLABORATION.CREATED"]
    assert (
        bodies[1]["source"]["type"] == "collaboration"
        and bodies[1]["source"]["accessible_by"]["id"] == user["id"]
    )
    assert bodies[1]["source"]["item"]["id"] == fid


async def test_shared_link_comment_and_metadata_triggers(box):
    receiver = Receiver()
    attach(box, receiver)
    parent = await box.find("Technology")
    fid = (await box.upload(parent, "t.txt", b"x")).json()["entries"][0]["id"]
    await make_webhook(
        box,
        parent,
        [
            "SHARED_LINK.CREATED",
            "SHARED_LINK.UPDATED",
            "SHARED_LINK.DELETED",
            "COMMENT.CREATED",
            "METADATA_INSTANCE.CREATED",
            "FILE.LOCKED",
            "FILE.UNLOCKED",
        ],
    )
    await box.put(f"/files/{fid}", json={"shared_link": {"access": "open"}})
    await box.put(f"/files/{fid}", json={"shared_link": {"access": "company"}})
    await box.put(f"/files/{fid}", json={"shared_link": None})
    await box.post("/comments", json={"message": "hi", "item": {"id": fid, "type": "file"}})
    await box.post(f"/files/{fid}/metadata/global/properties", json={"owner": "ops"})
    await box.put(f"/files/{fid}", json={"lock": {"access": "lock"}})
    await box.put(f"/files/{fid}", json={"lock": None})
    assert [json.loads(raw)["trigger"] for _, raw in receiver.deliveries] == [
        "SHARED_LINK.CREATED", "SHARED_LINK.UPDATED", "SHARED_LINK.DELETED", "COMMENT.CREATED",
        "METADATA_INSTANCE.CREATED", "FILE.LOCKED", "FILE.UNLOCKED",
    ]  # fmt: skip


async def test_failed_deliveries_are_retried_and_journalled(box):
    receiver = Receiver(status=500)
    attach(box, receiver)
    folder = await box.find("Technology")
    await make_webhook(box, folder, ["FILE.UPLOADED"])
    await box.upload(folder, "retry.txt", b"x")
    assert len(receiver.deliveries) == 3  # three attempts
    delivery = (await box.client.get("https://api.box.com/_emulator/webhooks")).json()["deliveries"][0]
    assert delivery["status"] == "failed" and delivery["attempts"] == 3 and delivery["response_status"] == 500
    ids = {headers["box-delivery-id"] for headers, _ in receiver.deliveries}
    assert len(ids) == 1  # the same delivery id is reused for retries


async def test_deleting_or_trashing_the_target_removes_its_webhooks(box):
    folder = await box.find("Technology")
    child = await box.mkdir(folder, "Temporary")
    hook = (await make_webhook(box, child, ["FILE.UPLOADED"])).json()
    await box.delete(f"/folders/{child}")
    await box.delete(f"/folders/{child}/trash")
    assert (await box.get(f"/webhooks/{hook['id']}")).status_code == 404


@pytest.mark.parametrize("trigger", ["FILE.UPLOADED", "FOLDER.CREATED"])
async def test_trigger_names_accepted(box, trigger):
    folder = await box.find("Technology")
    assert (await make_webhook(box, folder, [trigger])).status_code == 201
