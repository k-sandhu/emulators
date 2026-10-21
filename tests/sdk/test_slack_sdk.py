"""The official ``slack_sdk`` against the emulator (Web API, OAuth, OIDC, webhooks, Events API)."""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import requests
from fastapi import FastAPI, Request
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
from slack_sdk.oauth import AuthorizeUrlGenerator
from slack_sdk.signature import SignatureVerifier
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.webhook import WebhookClient

from emulators.testing.server import EmulatorServer

pytestmark = pytest.mark.sdk

INFO = "https://slack.com/_emulator/info"
REDIRECT = "https://consumer.example/slack/callback"


@pytest.fixture
def credentials(emulator_intercept: EmulatorServer) -> dict[str, Any]:
    info = requests.get(INFO, timeout=20).json()["credentials"]
    return info


@pytest.fixture
def bot(credentials: dict[str, Any]) -> WebClient:
    return WebClient(token=credentials["tokens"]["bot"])


@pytest.fixture
def owner(credentials: dict[str, Any]) -> WebClient:
    return WebClient(token=credentials["tokens"]["user:p001"])


def channel_id(client: WebClient, name: str) -> str:
    for page in client.conversations_list(limit=200, types="public_channel,private_channel"):
        for channel in page["channels"]:
            if channel["name"] == name:
                return channel["id"]
    raise AssertionError(name)


def test_auth_test_and_default_hostnames(bot: WebClient, credentials: dict[str, Any]) -> None:
    assert bot.base_url == "https://slack.com/api/"
    response = bot.auth_test()
    assert response["ok"] is True and response["url"] == "https://halcyonadvisory.slack.com/"
    assert response["team_id"] == credentials["team_id"] and response["user"] == "halcyon_bot"
    assert response.status_code == 200
    assert "chat:write" in response.headers["x-oauth-scopes"]


def test_archived_messages_cannot_be_updated(owner: WebClient) -> None:
    channel = owner.conversations_create(name="sdk-archived-update")["channel"]["id"]
    posted = owner.chat_postMessage(channel=channel, text="Original")
    owner.conversations_archive(channel=channel)
    with pytest.raises(SlackApiError) as error:
        owner.chat_update(channel=channel, ts=posted["ts"], text="Changed")
    assert error.value.response["error"] == "is_inactive"
    owner.conversations_unarchive(channel=channel)
    messages = owner.conversations_history(channel=channel)["messages"]
    assert next(message for message in messages if message["ts"] == posted["ts"])["text"] == "Original"


def test_post_message_history_replies_and_thread(bot: WebClient) -> None:
    general = channel_id(bot, "general")
    posted = bot.chat_postMessage(channel=general, text="Hello from the SDK :wave:")
    assert posted["channel"] == general and posted["message"]["text"] == "Hello from the SDK :wave:"
    thread = bot.chat_postMessage(channel=general, text="in thread", thread_ts=posted["ts"])
    assert thread["message"]["thread_ts"] == posted["ts"]
    history = bot.conversations_history(channel=general, limit=5)
    assert history["messages"][0]["ts"] == thread["ts"] or history["messages"][0]["ts"] == posted["ts"]
    replies = bot.conversations_replies(channel=general, ts=posted["ts"])
    assert [m["text"] for m in replies["messages"]] == ["Hello from the SDK :wave:", "in thread"]
    bot.chat_update(channel=general, ts=posted["ts"], text="edited")
    assert (
        bot.conversations_history(channel=general, limit=1, oldest=posted["ts"], inclusive=True)["messages"][
            -1
        ]["text"]
        == "edited"
    )
    link = bot.chat_getPermalink(channel=general, message_ts=posted["ts"])
    assert link["permalink"].startswith("https://halcyonadvisory.slack.com/archives/")
    bot.chat_delete(channel=general, ts=thread["ts"])


def test_blocks_attachments_and_error_handling(bot: WebClient) -> None:
    general = channel_id(bot, "general")
    bot.chat_postMessage(
        channel=general,
        text="fallback",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "*bold*"}}],
        attachments=[{"color": "#36a64f", "text": "attached"}],
    )
    with pytest.raises(SlackApiError) as error:
        bot.chat_postMessage(channel="C0NOTREALXX", text="x")
    assert error.value.response["error"] == "channel_not_found" and error.value.response.status_code == 200
    with pytest.raises(SlackApiError) as no_text:
        bot.chat_postMessage(channel=general)
    assert no_text.value.response["error"] == "no_text"
    narrow = WebClient(
        token=requests.post("https://slack.com/_emulator/tokens", json={"scopes": ["users:read"]}).json()[
            "access_token"
        ]
    )
    with pytest.raises(SlackApiError) as scope:
        narrow.chat_postMessage(channel=general, text="x")
    assert scope.value.response["error"] == "missing_scope" and scope.value.response["needed"] == "chat:write"
    with pytest.raises(SlackApiError) as auth:
        WebClient(token="xoxb-bogus").auth_test()
    assert auth.value.response["error"] == "invalid_auth"


def test_cursor_pagination_iterates_all_pages(bot: WebClient) -> None:
    names = []
    pages = 0
    for page in bot.conversations_list(limit=4, types="public_channel"):
        pages += 1
        names += [c["name"] for c in page["channels"]]
    assert pages > 3 and len(names) == len(set(names)) and "general" in names
    users = [m["id"] for page in bot.users_list(limit=20) for m in page["members"]]
    assert len(users) == len(set(users)) > 40
    members = [
        u
        for page in bot.conversations_members(channel=channel_id(bot, "general"), limit=10)
        for u in page["members"]
    ]
    assert len(members) > 40


def test_users_conversations_and_team(bot: WebClient, owner: WebClient) -> None:
    eleanor = owner.auth_test()["user_id"]
    info = bot.users_info(user=eleanor)["user"]
    assert info["is_owner"] and info["profile"]["email"] == "eleanor.whitfield@halcyonadvisory.example"
    assert bot.users_lookupByEmail(email=info["profile"]["email"])["user"]["id"] == eleanor
    with pytest.raises(SlackApiError) as missing:
        bot.users_lookupByEmail(email="nobody@halcyonadvisory.example")
    assert missing.value.response["error"] == "users_not_found"
    assert "general" in {c["name"] for c in bot.users_conversations(user=eleanor, limit=200)["channels"]}
    assert bot.team_info()["team"]["domain"] == "halcyonadvisory"
    assert {g["handle"] for g in bot.usergroups_list()["usergroups"]} >= {"audit", "tax"}
    assert bot.emoji_list()["emoji"]["halcyon"].startswith("https://")


def test_conversation_management(bot: WebClient, owner: WebClient) -> None:
    created = bot.conversations_create(name="sdk-project")["channel"]
    maya = bot.users_lookupByEmail(email="maya.chen@halcyonadvisory.example")["user"]["id"]
    bot.conversations_invite(channel=created["id"], users=maya)
    assert maya in bot.conversations_members(channel=created["id"])["members"]
    bot.conversations_setTopic(channel=created["id"], topic="SDK sprint")
    assert bot.conversations_info(channel=created["id"])["channel"]["topic"]["value"] == "SDK sprint"
    bot.conversations_rename(channel=created["id"], name="sdk-project-2")
    bot.conversations_archive(channel=created["id"])
    with pytest.raises(SlackApiError) as archived:
        bot.chat_postMessage(channel=created["id"], text="too late")
    assert archived.value.response["error"] == "is_archived"
    dm = bot.conversations_open(users=maya, return_im=True)["channel"]
    assert dm["is_im"] and bot.chat_postMessage(channel=dm["id"], text="hi Maya")["ok"]


def test_reactions_pins_and_bookmarks(bot: WebClient) -> None:
    general = channel_id(bot, "general")
    ts = bot.chat_postMessage(channel=general, text="react and pin me")["ts"]
    bot.reactions_add(channel=general, timestamp=ts, name="eyes")
    with pytest.raises(SlackApiError) as again:
        bot.reactions_add(channel=general, timestamp=ts, name="eyes")
    assert again.value.response["error"] == "already_reacted"
    got = bot.reactions_get(channel=general, timestamp=ts)
    assert got["message"]["reactions"][0]["name"] == "eyes"
    bot.pins_add(channel=general, timestamp=ts)
    assert ts in [item["message"]["ts"] for item in bot.pins_list(channel=general)["items"]]
    bot.bookmarks_add(
        channel_id=general, title="Docs", type="link", link="https://docs.halcyonadvisory.example"
    )
    assert "Docs" in [b["title"] for b in bot.bookmarks_list(channel_id=general)["bookmarks"]]


def test_scheduled_messages(bot: WebClient) -> None:
    import time

    general = channel_id(bot, "general")
    scheduled = bot.chat_scheduleMessage(channel=general, text="later", post_at=int(time.time()) + 3600)
    listing = bot.chat_scheduledMessages_list(channel=general)["scheduled_messages"]
    assert listing[0]["id"] == scheduled["scheduled_message_id"]
    bot.chat_deleteScheduledMessage(channel=general, scheduled_message_id=scheduled["scheduled_message_id"])
    assert bot.chat_scheduledMessages_list(channel=general)["scheduled_messages"] == []


def test_files_upload_v2_and_download(bot: WebClient) -> None:
    general = channel_id(bot, "general")
    response = bot.files_upload_v2(
        channel=general, content="alpha,beta\n1,2\n", filename="data.csv", title="Data", initial_comment="csv"
    )
    file = response["file"]
    assert file["name"] == "data.csv" and file["size"] == len("alpha,beta\n1,2\n") and file["title"] == "Data"
    assert bot.files_info(file=file["id"])["file"]["id"] == file["id"]
    download = requests.get(
        file["url_private_download"], headers={"Authorization": f"Bearer {bot.token}"}, timeout=20
    )
    assert download.status_code == 200 and download.text == "alpha,beta\n1,2\n"
    assert requests.get(file["url_private"], allow_redirects=False, timeout=20).status_code == 302
    listed = bot.files_list(channel=general, count=5)["files"]
    assert file["id"] in [f["id"] for f in listed]
    multiple = bot.files_upload_v2(
        channel=general,
        file_uploads=[
            {"content": "one", "filename": "one.txt", "title": "One"},
            {"file": io.BytesIO(b"two"), "filename": "two.txt", "title": "Two"},
        ],
    )
    assert [f["name"] for f in multiple["files"]] == ["one.txt", "two.txt"]
    bot.files_delete(file=file["id"])
    with pytest.raises(SlackApiError) as gone:
        bot.files_info(file=file["id"])
    assert gone.value.response["error"] == "file_deleted"


def test_legacy_files_upload(bot: WebClient) -> None:
    general = channel_id(bot, "general")
    with pytest.warns(UserWarning):
        response = bot.files_upload(channels=general, content="legacy", filename="legacy.txt", title="Legacy")
    assert response["file"]["name"] == "legacy.txt" and response["file"]["channels"] == [general]


def test_search_with_a_user_token(owner: WebClient, bot: WebClient) -> None:
    results = owner.search_messages(query="coffee machine in:#general", count=3)
    assert results["messages"]["total"] > 0 and len(results["messages"]["matches"]) <= 3
    assert results["messages"]["matches"][0]["channel"]["name"] == "general"
    with pytest.raises(SlackApiError) as bot_error:
        bot.search_messages(query="coffee")
    assert bot_error.value.response["error"] == "not_allowed_token_type"
    assert owner.search_files(query="pdf")["ok"]


def test_rate_limit_retry_handler(credentials: dict[str, Any]) -> None:
    requests.post(
        "https://slack.com/_emulator/faults",
        json={"path": "/api/auth.test", "status": 429, "times": 1, "headers": {"Retry-After": "1"}},
    )
    client = WebClient(token=credentials["tokens"]["bot"])
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=2))
    assert client.auth_test()["ok"] is True
    journal = requests.get("https://slack.com/_emulator/requests", params={"path": "auth.test"}).json()[
        "requests"
    ]
    assert [e["status"] for e in journal][-2:] == [429, 200]


def test_prefix_base_url(emulator_server: EmulatorServer, credentials: dict[str, Any]) -> None:
    client = WebClient(
        token=credentials["tokens"]["bot"], base_url=emulator_server.url_for("slack") + "/api/"
    )
    assert client.auth_test()["team"] == "Halcyon Advisory Group"
    general = channel_id(client, "general")
    uploaded = client.files_upload_v2(channel=general, content="prefix", filename="prefix.txt")
    assert uploaded["file"]["url_private"].startswith(emulator_server.url_for("slack") + "/files-pri/")
    assert (
        requests.get(
            uploaded["file"]["url_private"], headers={"Authorization": f"Bearer {client.token}"}, timeout=20
        ).text
        == "prefix"
    )


async def test_async_web_client_with_a_prefix_base_url(
    emulator_server: EmulatorServer, credentials: dict[str, Any]
) -> None:
    client = AsyncWebClient(
        token=credentials["tokens"]["user:p001"], base_url=emulator_server.url_for("slack") + "/api/"
    )
    me = await client.auth_test()
    assert me["user"] == "eleanor.whitfield" and me["url"] == emulator_server.url_for("slack") + "/"
    found = await client.search_messages(query="coffee machine", count=2)
    assert found["messages"]["total"] > 0
    channels = [c["name"] for page in [await client.conversations_list(limit=500)] for c in page["channels"]]
    assert "general" in channels
    uploaded = await client.files_upload_v2(
        channel=next(
            c["id"]
            for c in (await client.conversations_list(limit=500))["channels"]
            if c["name"] == "general"
        ),
        content="prefix async",
        filename="prefix-async.txt",
    )
    assert uploaded["file"]["url_private"].startswith(emulator_server.url_for("slack") + "/files-pri/")


async def test_async_web_client(credentials: dict[str, Any]) -> None:
    client = AsyncWebClient(token=credentials["tokens"]["bot"])
    assert (await client.auth_test())["ok"]
    general = None
    async for page in await client.conversations_list(limit=50):
        for channel in page["channels"]:
            if channel["name"] == "general":
                general = channel["id"]
    assert general
    posted = await client.chat_postMessage(channel=general, text="async hello")
    assert posted["message"]["text"] == "async hello"
    with pytest.raises(SlackApiError) as error:
        await client.conversations_history(channel="C0NOTREALXX")
    assert error.value.response["error"] == "channel_not_found"
    uploaded = await client.files_upload_v2(channel=general, content="async file", filename="async.txt")
    assert uploaded["file"]["name"] == "async.txt"


def test_incoming_webhook_client(credentials: dict[str, Any], bot: WebClient) -> None:
    hook = credentials["incoming_webhooks"][0]
    client = WebhookClient(hook)
    response = client.send(
        text="Webhook hello", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
    )
    assert response.status_code == 200 and response.body == "ok"
    bad = WebhookClient(hook).send_dict({"text": ""})
    assert bad.status_code == 400 and bad.body == "no_text"
    gone = WebhookClient("https://hooks.slack.com/services/T0/B0/nope").send(text="x")
    assert gone.status_code == 404 and gone.body == "no_service"


def test_oauth_v2_install_and_openid_connect(credentials: dict[str, Any]) -> None:
    generator = AuthorizeUrlGenerator(
        client_id=credentials["client_id"],
        scopes=["chat:write", "channels:read", "incoming-webhook"],
        user_scopes=["users:read"],
        redirect_uri=REDIRECT,
        authorization_url="https://slack.com/oauth/v2/authorize",
    )
    url = generator.generate(state="abc") + "&login_hint=maya.chen@halcyonadvisory.example"
    redirect = requests.get(url, allow_redirects=False, timeout=20)
    assert redirect.status_code == 302
    query = parse_qs(urlsplit(redirect.headers["location"]).query)
    assert query["state"] == ["abc"]
    installed = WebClient().oauth_v2_access(
        client_id=credentials["client_id"],
        client_secret=credentials["client_secret"],
        code=query["code"][0],
        redirect_uri=REDIRECT,
    )
    assert installed["access_token"].startswith("xoxb-") and installed["authed_user"][
        "access_token"
    ].startswith("xoxp-")
    assert installed["incoming_webhook"]["url"].startswith("https://hooks.slack.com/services/")
    assert WebClient(token=installed["access_token"]).auth_test()["bot_id"]
    assert WebhookClient(installed["incoming_webhook"]["url"]).send(text="installed").status_code == 200
    me = WebClient(token=installed["authed_user"]["access_token"]).auth_test()
    assert me["user"] == "maya.chen"
    with pytest.raises(SlackApiError) as reused:
        WebClient().oauth_v2_access(
            client_id=credentials["client_id"],
            client_secret=credentials["client_secret"],
            code=query["code"][0],
            redirect_uri=REDIRECT,
        )
    assert reused.value.response["error"] == "invalid_code"
    sign_in = requests.get(
        "https://slack.com/openid/connect/authorize",
        params={
            "response_type": "code", "scope": "openid email profile", "client_id": credentials["client_id"],
            "redirect_uri": REDIRECT, "state": "s", "nonce": "n", "login_hint": "p001",
        },
        allow_redirects=False,
        timeout=20,
    )  # fmt: skip
    code = parse_qs(urlsplit(sign_in.headers["location"]).query)["code"][0]
    token = WebClient().openid_connect_token(
        client_id=credentials["client_id"],
        client_secret=credentials["client_secret"],
        code=code,
        redirect_uri=REDIRECT,
    )
    userinfo = WebClient().openid_connect_userInfo(token=token["access_token"])
    assert (
        userinfo["email"] == "eleanor.whitfield@halcyonadvisory.example"
        and userinfo["name"] == "Eleanor Whitfield"
    )


def test_events_api_delivery_is_verifiable_with_slack_sdk(
    emulator_hub: Any, credentials: dict[str, Any], bot: WebClient
) -> None:
    seen: list[tuple[bytes, dict[str, str], dict[str, Any]]] = []
    consumer = FastAPI()

    @consumer.post("/slack/events")
    async def events(request: Request) -> dict[str, Any]:
        body = await request.body()
        import json

        payload = json.loads(body)
        seen.append((body, dict(request.headers), payload))
        if payload["type"] == "url_verification":
            return {"challenge": payload["challenge"]}
        return {}

    emulator_hub.webhook_transport = httpx.ASGITransport(app=consumer)
    try:
        configured = requests.post(
            "https://slack.com/_emulator/actions/configure_events",
            json={
                "request_url": "http://consumer.test/slack/events",
                "events": ["message.channels", "reaction_added"],
            },
            timeout=20,
        )
        assert configured.status_code == 200, configured.text
        verifier = SignatureVerifier(configured.json()["signing_secret"])
        assert verifier.is_valid_request(seen[0][0], seen[0][1]) and seen[0][2]["type"] == "url_verification"
        requests.post(
            "https://slack.com/_emulator/actions/post_as_user",
            json={
                "user": "maya.chen@halcyonadvisory.example",
                "channel": "general",
                "text": "hello from Maya",
            },
            timeout=20,
        )
        callbacks = [s for s in seen if s[2]["type"] == "event_callback"]
        assert len(callbacks) == 1
        body, headers, payload = callbacks[0]
        assert verifier.is_valid_request(body, headers)
        assert payload["event"]["text"] == "hello from Maya" and payload["event"]["channel_type"] == "channel"
        bot.chat_postMessage(channel=payload["event"]["channel"], text="the bot's own message is not echoed")
        assert len([s for s in seen if s[2]["type"] == "event_callback"]) == 1
    finally:
        emulator_hub.webhook_transport = None


@pytest.fixture(autouse=True)
def _drain() -> Iterator[None]:
    yield


def test_thread_pagination_counts_parent_and_finishes(bot: WebClient) -> None:
    general = channel_id(bot, "general")
    parent = bot.chat_postMessage(channel=general, text="SDK paginated parent")
    reply = bot.chat_postMessage(channel=general, text="SDK paginated reply", thread_ts=parent["ts"])
    pages = [dict(page.data) for page in bot.conversations_replies(channel=general, ts=parent["ts"], limit=1)]
    assert len(pages) == 2 and all(len(page["messages"]) == 1 for page in pages)
    assert [page["messages"][0]["ts"] for page in pages] == [parent["ts"], reply["ts"]]
    assert pages[-1]["response_metadata"]["next_cursor"] == ""
    bot.chat_delete(channel=general, ts=reply["ts"])
    retained = bot.conversations_replies(channel=general, ts=parent["ts"])["messages"][0]
    assert retained["thread_ts"] == parent["ts"] and retained["reply_count"] == 0


def test_chat_update_clears_structured_fields(bot: WebClient) -> None:
    general = channel_id(bot, "general")
    posted = bot.chat_postMessage(
        channel=general,
        text="fallback",
        blocks=[{"type": "divider"}],
        attachments=[{"text": "attachment"}],
        metadata={"event_type": "audit", "event_payload": {}},
    )
    updated = bot.chat_update(channel=general, ts=posted["ts"], blocks=[], attachments=[], metadata={})
    assert updated["message"]["blocks"] == updated["message"]["attachments"] == []
    assert "metadata" not in updated["message"] and updated["text"] == "fallback"


def test_email_lookup_rejects_departed_users(bot: WebClient) -> None:
    departed = next(u for page in bot.users_list() for u in page["members"] if u["deleted"])
    with pytest.raises(SlackApiError) as error:
        bot.users_lookupByEmail(email=departed["profile"]["email"])
    assert error.value.response["error"] == "users_not_found"


def test_unicode_upload_and_download(bot: WebClient) -> None:
    uploaded = bot.files_upload_v2(content="SDK unicode content", filename="你好 #?.txt")
    file = uploaded["files"][0]
    response = requests.get(
        file["url_private_download"], headers={"Authorization": f"Bearer {bot.token}"}, timeout=20
    )
    assert response.status_code == 200 and response.text == "SDK unicode content"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
