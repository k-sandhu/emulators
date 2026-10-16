"""Adversarial HTTP regressions for wire compatibility and failed-write isolation."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from .conftest import Slack

SLACK = "https://slack.com"
REDIRECT = "https://consumer.example/slack/callback"
WRITE_METHODS = (
    "chat.postMessage", "chat.update", "chat.delete", "chat.postEphemeral", "chat.meMessage",
    "chat.scheduleMessage", "chat.deleteScheduledMessage", "chat.unfurl", "conversations.create",
    "conversations.join", "conversations.leave", "conversations.invite", "conversations.kick",
    "conversations.archive", "conversations.unarchive", "conversations.rename", "conversations.setTopic",
    "conversations.setPurpose", "conversations.open", "conversations.close", "conversations.mark",
    "users.profile.set", "users.setPresence", "usergroups.create", "usergroups.update",
    "usergroups.users.update", "usergroups.disable", "usergroups.enable", "reactions.add",
    "reactions.remove", "pins.add", "pins.remove", "bookmarks.add", "bookmarks.edit", "bookmarks.remove",
    "files.upload", "files.getUploadURLExternal", "files.completeUploadExternal", "files.delete",
    "files.sharedPublicURL", "files.revokePublicURL", "reminders.add", "reminders.complete",
    "reminders.delete", "dnd.setSnooze", "dnd.endSnooze", "dnd.endDnd", "views.open", "views.push",
    "views.update", "views.publish", "oauth.v2.access", "openid.connect.token",
)  # fmt: skip


@pytest.mark.parametrize("method", WRITE_METHODS)
@pytest.mark.parametrize(
    ("content", "content_type", "error"),
    [
        (b"[null]", "application/json", "invalid_json"),
        (b"{", "application/json", "invalid_json"),
        (b'{"text":"\\ud800"}', "application/json", "invalid_json"),
        (b'{"metadata":{"nested":1e999}}', "application/json", "invalid_json"),
        (b"garbage", "multipart/form-data", "invalid_form_data"),
        (b"not a form", "text/plain", "invalid_post_type"),
        (b"payload", None, "missing_post_type"),
    ],
)
async def test_every_write_rejects_malformed_bodies(
    slack: Slack, method: str, content: bytes, content_type: str | None, error: str
) -> None:
    headers = {"Authorization": f"Bearer {await slack.user_token('p001')}"}
    if content_type:
        headers["Content-Type"] = content_type
    response = await slack.client.post(f"{SLACK}/api/{method}", headers=headers, content=content)
    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": error}
    assert response.headers["x-slack-req-id"]


@pytest.mark.parametrize(
    ("method", "field"),
    [
        ("files.getUploadURLExternal", "length"),
        ("files.list", "count"),
        ("files.list", "page"),
        ("files.list", "ts_from"),
        ("files.list", "ts_to"),
        ("chat.scheduleMessage", "post_at"),
        ("chat.scheduledMessages.list", "oldest"),
        ("chat.scheduledMessages.list", "latest"),
        ("reactions.list", "count"),
        ("reactions.list", "page"),
        ("dnd.setSnooze", "num_minutes"),
        ("users.list", "limit"),
        ("conversations.history", "limit"),
        ("search.messages", "count"),
    ],
)
@pytest.mark.parametrize("value", ["Infinity", "9" * 10_000, 1e300, 1.5, True, {}])
async def test_bad_numeric_arguments_are_slack_errors(
    slack: Slack, method: str, field: str, value: Any
) -> None:
    body = await slack.api(
        method,
        await slack.user_token("p001"),
        json={
            "channel": slack.channel_id("general"),
            "filename": "audit.txt",
            "text": "x",
            "query": "audit",
            field: value,
        },
    )
    assert body["ok"] is False
    assert body["error"] in ("invalid_arguments", "invalid_time")


@pytest.mark.parametrize("value", [[], {}, True, 42])
async def test_text_types_do_not_become_messages(slack: Slack, value: Any) -> None:
    channel = slack.channel_id("general")
    before = len(slack.ctx.store.collection("messages"))
    body = await slack.api("chat.postMessage", json={"channel": channel, "text": value})
    assert body["error"] == "invalid_arguments"
    assert len(slack.ctx.store.collection("messages")) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"status_text": "partial", "status_expiration": "bad"},
        {"status_text": "partial", "status_expiration": {}},
        {"status_text": "partial", "fields": []},
        {"status_text": "partial", "display_name": []},
        {"status_text": "partial", "email": {"bad": True}},
    ],
)
async def test_invalid_profiles_are_atomic(slack: Slack, changes: dict[str, Any]) -> None:
    token = await slack.user_token("p001")
    before = (await slack.ok("users.profile.get", token))["profile"]
    assert await slack.err("users.profile.set", token, profile=changes) == "invalid_profile"
    assert (await slack.ok("users.profile.get", token))["profile"] == before
    assert (await slack.ok("search.messages", token, query="from:eleanor.whitfield"))["ok"]


async def test_many_mrkdwn_spans_do_not_overflow_the_stack(slack: Slack) -> None:
    text = "*bold* " * 2000 + "你好 👋"
    posted = await slack.ok("chat.postMessage", channel=slack.channel_id("general"), text=text)
    assert posted["message"]["text"] == text
    elements = posted["message"]["blocks"][0]["elements"][0]["elements"]
    assert sum(e.get("style", {}).get("bold", False) for e in elements) == 2000


async def test_replies_paginate_the_parent_and_obey_time_filters(slack: Slack) -> None:
    channel = slack.channel_id("general")
    parent = await slack.ok("chat.postMessage", channel=channel, text="parent")
    replies = [
        await slack.ok("chat.postMessage", channel=channel, text=f"reply {i}", thread_ts=parent["ts"])
        for i in range(4)
    ]
    found: list[str] = []
    cursor = ""
    while True:
        page = await slack.ok(
            "conversations.replies", channel=channel, ts=parent["ts"], limit=1, cursor=cursor
        )
        assert len(page["messages"]) == 1
        found += [m["ts"] for m in page["messages"]]
        cursor = page["response_metadata"]["next_cursor"]
        assert page["has_more"] is bool(cursor)
        if not cursor:
            break
    assert found == [parent["ts"], *(r["ts"] for r in replies)]
    window = await slack.ok(
        "conversations.replies",
        channel=channel,
        ts=parent["ts"],
        oldest=replies[0]["ts"],
        latest=replies[3]["ts"],
    )
    assert [m["ts"] for m in window["messages"]] == [replies[1]["ts"], replies[2]["ts"]]
    inclusive = await slack.ok(
        "conversations.replies",
        channel=channel,
        ts=parent["ts"],
        oldest=replies[0]["ts"],
        latest=replies[3]["ts"],
        inclusive=True,
    )
    assert [m["ts"] for m in inclusive["messages"]] == [r["ts"] for r in replies]


async def test_membership_messages_cannot_be_threaded(slack: Slack) -> None:
    channel = (await slack.ok("conversations.create", name="audit-membership"))["channel"]["id"]
    await slack.ok("conversations.invite", channel=channel, users=slack.uid("p018"))
    joined = (await slack.history(channel))[0]
    assert joined["subtype"] == "channel_join"
    assert await slack.err("conversations.replies", channel=channel, ts=joined["ts"]) == "thread_not_found"


async def test_chat_update_clears_arrays_and_updates_metadata(slack: Slack) -> None:
    channel = slack.channel_id("general")
    posted = await slack.ok(
        "chat.postMessage",
        channel=channel,
        text="fallback",
        blocks=[{"type": "divider"}],
        attachments=[{"text": "attachment"}],
        metadata={"event_type": "audit", "event_payload": {}},
    )
    updated = await slack.ok("chat.update", channel=channel, ts=posted["ts"], blocks=[], attachments=[])
    assert updated["message"]["blocks"] == updated["message"]["attachments"] == []
    assert updated["text"] == "fallback"
    changed = await slack.ok(
        "chat.update",
        channel=channel,
        ts=posted["ts"],
        metadata={"event_type": "changed", "event_payload": {"count": 1}},
    )
    assert changed["message"]["metadata"]["event_type"] == "changed"
    cleared = await slack.ok("chat.update", channel=channel, ts=posted["ts"], metadata={})
    assert "metadata" not in cleared["message"]


async def test_private_writes_require_private_scopes(slack: Slack) -> None:
    narrow = await slack.mint(scopes=["channels:manage"])
    assert (
        await slack.err("conversations.create", narrow, name="audit-private", is_private=True)
        == "missing_scope"
    )
    channel = slack.channel_id("eng-bluefin-seafood-fees")
    assert (
        await slack.err("conversations.setTopic", narrow, channel=channel, topic="private") == "missing_scope"
    )
    allowed = await slack.mint(scopes=["groups:write"])
    assert (await slack.ok("conversations.setTopic", allowed, channel=channel, topic="private"))[
        "topic"
    ] == "private"


async def test_lookup_email_scope_and_departed_users(slack: Slack) -> None:
    token = await slack.mint(scopes=["users:read.email"])
    assert (await slack.ok("users.lookupByEmail", token, email=slack.person_email("p018")))["user"][
        "id"
    ] == slack.uid("p018")
    assert (
        await slack.err("users.lookupByEmail", token, email=slack.person_email("p046")) == "users_not_found"
    )


async def prepare_upload(slack: Slack, name: str = "audit.txt") -> dict[str, Any]:
    pending = await slack.ok("files.getUploadURLExternal", filename=name, length=3)
    assert (await slack.client.post(pending["upload_url"], content=b"abc")).status_code == 200
    return pending


async def test_failed_batch_completion_leaves_uploads_retryable(slack: Slack) -> None:
    pending = await prepare_upload(slack)
    before = len(slack.ctx.store.collection("files"))
    assert (
        await slack.err(
            "files.completeUploadExternal", files=[{"id": pending["file_id"]}, {"id": "FMISSING"}]
        )
        == "file_not_found"
    )
    assert len(slack.ctx.store.collection("files")) == before
    assert (
        await slack.err("files.completeUploadExternal", files=[{"id": pending["file_id"]}] * 2)
        == "invalid_arguments"
    )
    done = await slack.ok("files.completeUploadExternal", files=[{"id": pending["file_id"]}])
    assert done["files"][0]["size"] == 3
    assert (
        await slack.err("files.completeUploadExternal", files=[{"id": pending["file_id"]}])
        == "file_not_found"
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"thread_ts": "1.000001"},
        {"blocks": [{"type": "bogus"}]},
        {"initial_comment": "x" * 40_001},
    ],
)
async def test_invalid_file_shares_create_no_orphans(slack: Slack, extra: dict[str, Any]) -> None:
    pending = await prepare_upload(slack)
    before = len(slack.ctx.store.collection("files"))
    channel = slack.channel_id("general")
    body = await slack.api(
        "files.completeUploadExternal", files=[{"id": pending["file_id"]}], channel_id=channel, **extra
    )
    assert body["ok"] is False
    assert len(slack.ctx.store.collection("files")) == before
    assert (
        await slack.ok("files.completeUploadExternal", files=[{"id": pending["file_id"]}], channel_id=channel)
    )["files"]


async def test_guests_cannot_download_or_discover_unjoined_public_files(slack: Slack) -> None:
    general = slack.channel_id("general")
    guest = await slack.user_token("c002-1")
    file = (
        await slack.ok(
            "files.upload", content="private to this channel", filename="audit.txt", channels=general
        )
    )["file"]
    assert await slack.err("files.info", guest, file=file["id"]) == "file_not_found"
    assert file["id"] not in {f["id"] for f in (await slack.ok("files.list", guest))["files"]}
    assert (
        await slack.client.get(file["url_private"], headers={"Authorization": f"Bearer {guest}"})
    ).status_code == 404
    allowed = (
        await slack.ok(
            "files.upload",
            content="allowed",
            filename="allowed.txt",
            channels=slack.channel_id("eng-bluefin-seafood"),
        )
    )["file"]
    assert (await slack.ok("files.info", guest, file=allowed["id"]))["file"]["id"] == allowed["id"]


async def test_file_metadata_hides_private_shares(slack: Slack) -> None:
    owner = await slack.user_token("p001")
    public, private = slack.channel_id("general"), slack.channel_id("partners")
    shared = (
        await slack.ok(
            "files.upload", owner, content="audit", filename="audit.txt", channels=f"{public},{private}"
        )
    )["file"]
    assert private in shared["groups"]
    maya = await slack.mint(user="p020")
    shown = (await slack.ok("files.info", maya, file=shared["id"]))["file"]
    assert shown["groups"] == [] and "private" not in shown["shares"]
    messages = await slack.history(public, maya, limit=1)
    assert messages[0]["files"][0]["groups"] == []
    assert await slack.err("files.list", maya, channel=private) == "channel_not_found"
    assert (await slack.ok("search.files", maya, query="audit in:partners"))["files"]["total"] == 0
    assert shared["id"] in {
        f["id"]
        for f in (await slack.ok("search.files", maya, query="audit -in:partners"))["files"]["matches"]
    }


async def test_unicode_and_reserved_file_names_download(slack: Slack) -> None:
    pending = await prepare_upload(slack, '你好 #?"\r\n.txt')
    file = (await slack.ok("files.completeUploadExternal", files=[{"id": pending["file_id"]}]))["files"][0]
    response = await slack.client.get(
        file["url_private_download"], headers={"Authorization": f"Bearer {await slack.bot()}"}
    )
    assert response.status_code == 200 and response.content == b"abc"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert urlsplit(file["url_private_download"]).fragment == ""


@pytest.mark.parametrize(
    "query",
    ["during:2026-00", "during:2026-13", "during:0000", "during:9999", "on:9999-12-31", "after:9999-12-31"],
)
async def test_invalid_and_extreme_search_dates_never_crash(slack: Slack, query: str) -> None:
    assert (await slack.ok("search.messages", await slack.user_token("p001"), query=query))["ok"]


async def test_file_search_respects_negative_filters(slack: Slack) -> None:
    owner = await slack.user_token("p001")
    await slack.ok(
        "files.upload",
        owner,
        content="audit",
        filename="audit-marker.txt",
        channels=slack.channel_id("general"),
    )
    for suffix in ("-in:general", "-from:eleanor.whitfield"):
        assert (await slack.ok("search.files", owner, query=f"audit-marker {suffix}"))["files"]["total"] == 0


async def test_refresh_is_bound_to_issuing_client(slack: Slack) -> None:
    await slack.client.post(f"{SLACK}/_emulator/actions/configure_app", json={"token_rotation": True})
    app = slack.ctx.store.collection("app_config").get("app")
    response = await slack.client.get(
        f"{SLACK}/oauth/v2/authorize",
        params={
            "client_id": app["client_id"],
            "redirect_uri": REDIRECT,
            "login_hint": "p018",
            "scope": "chat:write",
        },
    )
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    issued = (
        await slack.client.post(
            f"{SLACK}/api/oauth.v2.access",
            auth=(app["client_id"], app["client_secret"]),
            data={"code": code, "redirect_uri": REDIRECT},
        )
    ).json()
    denied = (
        await slack.client.post(
            f"{SLACK}/api/oauth.v2.access",
            data={
                "client_id": "other-client",
                "client_secret": "test-value",
                "grant_type": "refresh_token",
                "refresh_token": issued["refresh_token"],
            },
        )
    ).json()
    assert denied["error"] == "invalid_client_id"
    allowed = (
        await slack.client.post(
            f"{SLACK}/api/oauth.v2.access",
            auth=(app["client_id"], app["client_secret"]),
            data={"grant_type": "refresh_token", "refresh_token": issued["refresh_token"]},
        )
    ).json()
    assert allowed["ok"] and allowed["refresh_token"] != issued["refresh_token"]


async def test_oauth_requires_bound_redirect_and_pkce(slack: Slack) -> None:
    app = slack.ctx.store.collection("app_config").get("app")
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    response = await slack.client.get(
        f"{SLACK}/oauth/v2/authorize",
        params={
            "client_id": app["client_id"],
            "redirect_uri": REDIRECT,
            "login_hint": "p018",
            "scope": "chat:write",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]

    async def exchange(**extra: str) -> dict[str, Any]:
        return (
            await slack.client.post(
                f"{SLACK}/api/oauth.v2.access",
                auth=(app["client_id"], app["client_secret"]),
                data={"code": code, **extra},
            )
        ).json()

    assert (await exchange())["error"] == "bad_redirect_uri"
    assert (await exchange(redirect_uri=REDIRECT))["error"] == "invalid_code_verifier"
    assert (await exchange(redirect_uri=REDIRECT, code_verifier=verifier))["ok"]


async def test_events_retry_three_times_and_honor_no_retry(slack: Slack) -> None:
    received: list[dict[str, Any]] = []
    stop = False
    receiver = FastAPI()

    @receiver.post("/events")
    async def receive(request: Request) -> JSONResponse:
        received.append({"headers": dict(request.headers), "body": await request.body()})
        return JSONResponse({}, status_code=503, headers={"x-slack-no-retry": "1"} if stop else {})

    slack.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    await slack.client.post(
        f"{SLACK}/_emulator/actions/configure_events",
        json={
            "request_url": "https://consumer.example/events",
            "skip_verification": True,
            "events": ["message.channels"],
        },
    )
    await slack.client.post(
        f"{SLACK}/_emulator/actions/post_as_user",
        json={"user": "p018", "channel": "general", "text": "retries"},
    )
    assert len(received) == 4
    assert [r["headers"].get("x-slack-retry-num") for r in received] == [None, "1", "2", "3"]
    assert len({json.loads(r["body"])["event_id"] for r in received}) == 1
    assert all(r["headers"]["x-slack-retry-reason"] == "http_error" for r in received[1:])
    stop = True
    received.clear()
    await slack.client.post(
        f"{SLACK}/_emulator/actions/post_as_user",
        json={"user": "p018", "channel": "general", "text": "no retries"},
    )
    assert len(received) == 1


async def test_invalid_schedules_never_poison_later_calls(slack: Slack) -> None:
    channel = slack.channel_id("general")
    assert (
        await slack.err(
            "chat.scheduleMessage",
            channel=channel,
            text="x" * 40_001,
            post_at=slack.ctx.clock.timestamp() + 60,
        )
        == "msg_too_long"
    )
    before = copy.deepcopy(slack.ctx.store.collection("scheduled_messages").values())
    slack.hub.clock.advance(timedelta(minutes=2))
    try:
        assert (await slack.ok("auth.test"))["ok"]
        assert slack.ctx.store.collection("scheduled_messages").values() == before
    finally:
        slack.hub.clock.advance(timedelta(minutes=-2))


async def test_conversation_listing_filters_scopes_and_pages_before_archived_filter(slack: Slack) -> None:
    narrow = await slack.mint(scopes=["channels:read"])
    shown = await slack.ok("conversations.list", narrow, types="public_channel,private_channel,im,mpim")
    assert shown["channels"] and all(c["is_channel"] and not c["is_private"] for c in shown["channels"])
    all_channels = (await slack.ok("conversations.list", limit=1000))["channels"]
    archived = all_channels[1]["id"]
    await slack.ok("conversations.archive", channel=archived)
    page = await slack.ok("conversations.list", limit=2, exclude_archived=True)
    assert [c["id"] for c in page["channels"]] == [all_channels[0]["id"]]
    following = await slack.ok(
        "conversations.list", limit=2, exclude_archived=True, cursor=page["response_metadata"]["next_cursor"]
    )
    assert following["channels"][0]["id"] == all_channels[2]["id"]


async def test_topic_specific_scope_is_accepted(slack: Slack) -> None:
    token = await slack.mint(scopes=["channels:write.topic"])
    assert (
        await slack.ok("conversations.setTopic", token, channel=slack.channel_id("general"), topic="audit")
    )["topic"] == "audit"


async def test_upload_rejects_bad_lengths_and_multipart_without_consuming(slack: Slack) -> None:
    pending = await slack.ok("files.getUploadURLExternal", filename="audit.txt", length=3)
    assert (await slack.client.post(pending["upload_url"], content=b"xx")).status_code == 400
    bad = await slack.client.post(
        pending["upload_url"], content=b"bad", headers={"Content-Type": "multipart/form-data"}
    )
    assert bad.status_code == 400
    assert (
        await slack.err("files.completeUploadExternal", files=[{"id": pending["file_id"]}])
        == "file_not_found"
    )
    assert (await slack.client.post(pending["upload_url"], content=b"abc")).status_code == 200
    assert (await slack.ok("files.completeUploadExternal", files=[{"id": pending["file_id"]}]))["files"]


async def test_invalid_response_url_payloads_do_not_mutate_messages(slack: Slack) -> None:
    from emulators.providers.slack.interactivity import new_response_url

    channel = slack.channel_id("general")
    posted = await slack.ok("chat.postMessage", channel=channel, text="original")
    path, _ = new_response_url(
        slack.ctx, "action", channel_id=channel, user_id=slack.uid("p018"), message_ts=posted["ts"]
    )
    url = "https://hooks.slack.com" + path
    for text in ([], {}, 42, True):
        invalid = await slack.client.post(url, json={"text": text, "replace_original": True})
        assert invalid.status_code == 400 and invalid.text == "invalid_payload"
    invalid_blocks = await slack.client.post(
        url, json={"text": "partial", "blocks": [{"type": "bogus"}], "replace_original": True}
    )
    assert invalid_blocks.status_code == 400
    assert (await slack.history(channel, limit=1))[0]["text"] == "original"


async def test_oauth_and_openid_codes_are_not_interchangeable(slack: Slack) -> None:
    app = slack.ctx.store.collection("app_config").get("app")
    for path, scope, wrong, correct in (
        ("/oauth/v2/authorize", "chat:write", "openid.connect.token", "oauth.v2.access"),
        ("/openid/connect/authorize", "openid", "oauth.v2.access", "openid.connect.token"),
    ):
        response = await slack.client.get(
            SLACK + path,
            params={
                "client_id": app["client_id"],
                "redirect_uri": REDIRECT,
                "login_hint": "p018",
                "scope": scope,
            },
        )
        code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
        auth = (app["client_id"], app["client_secret"])
        data = {"code": code, "redirect_uri": REDIRECT}
        assert (await slack.client.post(f"{SLACK}/api/{wrong}", auth=auth, data=data)).json()[
            "error"
        ] == "invalid_code"
        assert (await slack.client.post(f"{SLACK}/api/{correct}", auth=auth, data=data)).json()["ok"]


async def test_invalid_charset_and_fault_request_ids(slack: Slack) -> None:
    headers = {
        "Authorization": f"Bearer {await slack.bot()}",
        "Content-Type": "application/json; charset=ascii",
    }
    invalid = await slack.client.post(f"{SLACK}/api/auth.test", headers=headers, content=b"{}")
    assert invalid.status_code == 200 and invalid.json()["error"] == "invalid_charset"
    await slack.client.post(f"{SLACK}/_emulator/faults", json={"path": "/api/auth.test", "status": 429})
    limited = await slack.client.post(
        f"{SLACK}/api/auth.test", headers={"Authorization": headers["Authorization"]}
    )
    assert limited.status_code == 429 and limited.json()["error"] == "ratelimited"
    assert limited.headers["retry-after"] and limited.headers["x-slack-req-id"]


async def test_links_stay_in_prefix_routing_mode(slack: Slack) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=slack.hub.gateway), base_url="http://localhost:44080/slack/"
    ) as client:
        client.headers["Authorization"] = f"Bearer {await slack.bot()}"
        info = (await client.get("_emulator/info")).json()
        hook = info["credentials"]["incoming_webhooks"][0]
        assert hook.startswith("http://localhost:44080/slack/services/")
        assert (await client.post(hook, json={"text": "prefix webhook"})).text == "ok"
        posted = (
            await client.post(
                "api/chat.postMessage",
                json={"channel": slack.channel_id("general"), "text": "prefix permalink"},
            )
        ).json()
        link = (
            await client.get(
                "api/chat.getPermalink", params={"channel": posted["channel"], "message_ts": posted["ts"]}
            )
        ).json()["permalink"]
        assert link.startswith("http://localhost:44080/slack/archives/")
        assert (await client.get(link)).status_code == 200
        assert (await client.get("api/auth.test")).json()["url"] == "http://localhost:44080/slack/"


async def test_frozen_clock_preserves_history_and_thread_visibility(slack: Slack) -> None:
    slack.hub.clock.freeze()
    try:
        channel = slack.channel_id("general")
        parent = await slack.ok("chat.postMessage", channel=channel, text="frozen parent")
        reply = await slack.ok(
            "chat.postMessage", channel=channel, text="frozen reply", thread_ts=parent["ts"]
        )
        top = await slack.ok("chat.postMessage", channel=channel, text="frozen top")
        assert (await slack.history(channel, limit=1))[0]["ts"] == top["ts"]
        thread = await slack.ok("conversations.replies", channel=channel, ts=parent["ts"])
        assert [m["ts"] for m in thread["messages"]] == [parent["ts"], reply["ts"]]
    finally:
        slack.hub.clock.unfreeze()


async def test_legacy_snippet_shapes_channel_names_and_public_flags(slack: Slack) -> None:
    private = (await slack.ok("files.upload", content="note", filename="audit.txt"))["file"]
    assert private["title"] == "audit" and private["mode"] == "snippet" and private["editable"] is True
    assert private["is_public"] is False and private["public_url_shared"] is False
    plain = (await slack.ok("files.upload", content="note"))["file"]
    assert plain["filetype"] == "text" and plain["pretty_type"] == "Plain Text"
    shared = (await slack.ok("files.upload", content="note", filename="audit.txt", channels="#general"))[
        "file"
    ]
    assert shared["channels"] == [slack.channel_id("general")]
    assert shared["is_public"] is True and shared["public_url_shared"] is False
    member = await slack.mint(user="p020")
    published = (await slack.ok("files.sharedPublicURL", member, file=shared["id"]))["file"]
    assert published["public_url_shared"] is True
    assert await slack.err("files.sharedPublicURL", member, file=private["id"]) == "file_not_found"


@pytest.mark.parametrize(
    "payload",
    [
        {"scopes": 42},
        {"scopes": [{}]},
        {"expires_in": "Infinity"},
        {"expires_in": {}},
        {"expires_in": 10**18},
        {"client_id": []},
    ],
)
async def test_invalid_token_mint_inputs_do_not_create_broken_grants(
    slack: Slack, payload: dict[str, Any]
) -> None:
    before = len(slack.ctx.store.collection("_grants"))
    response = await slack.client.post(f"{SLACK}/_emulator/tokens", json=payload)
    assert response.status_code == 400 and response.json()["error"] == "invalid_arguments"
    assert len(slack.ctx.store.collection("_grants")) == before


@pytest.mark.parametrize("hops", [2, 3])
async def test_events_follow_at_most_two_redirects(slack: Slack, hops: int) -> None:
    received: list[tuple[int, dict[str, str]]] = []

    def receive(request: httpx.Request) -> httpx.Response:
        step = int(request.url.path.strip("/") or "0")
        received.append((step, dict(request.headers)))
        if step < hops:
            return httpx.Response(307, headers={"Location": f"https://consumer.example/{step + 1}"})
        return httpx.Response(200)

    slack.hub.webhook_transport = httpx.MockTransport(receive)
    await slack.client.post(
        f"{SLACK}/_emulator/actions/configure_events",
        json={
            "request_url": "https://consumer.example/0",
            "skip_verification": True,
            "events": ["message.channels"],
        },
    )
    await slack.client.post(
        f"{SLACK}/_emulator/actions/post_as_user",
        json={"user": "p018", "channel": "general", "text": "redirect test"},
    )
    log = (await slack.client.get(f"{SLACK}/_emulator/webhooks")).json()["deliveries"][-1]
    if hops == 2:
        assert log["status"] == "delivered" and [step for step, _ in received] == [0, 1, 2]
    else:
        assert log["status"] == "failed" and log["attempts"] == 4
        assert all(
            headers["x-slack-retry-reason"] == "too_many_redirects"
            for step, headers in received
            if step == 0 and "x-slack-retry-num" in headers
        )


async def test_invalid_receiver_redirects_are_journalled(slack: Slack) -> None:
    slack.hub.webhook_transport = httpx.MockTransport(
        lambda request: httpx.Response(307, headers={"Location": "http://["})
    )
    await slack.client.post(
        f"{SLACK}/_emulator/actions/configure_events",
        json={
            "request_url": "https://consumer.example/events",
            "skip_verification": True,
            "events": ["message.channels"],
        },
    )
    response = await slack.client.post(
        f"{SLACK}/_emulator/actions/post_as_user",
        json={"user": "p018", "channel": "general", "text": "invalid redirect"},
    )
    assert response.status_code == 200 and response.json()["ok"]
    log = (await slack.client.get(f"{SLACK}/_emulator/webhooks")).json()["deliveries"][-1]
    assert log["status"] == "failed" and log["attempts"] == 4
