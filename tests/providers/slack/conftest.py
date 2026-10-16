"""Fixtures for the Slack provider tests: a thin harness around the in-process client."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from emulators.core.hub import EmulatorHub
from emulators.core.provider import ProviderContext

SLACK = "https://slack.com"


class Slack:
    """Calls Slack methods against the emulator with the real hostnames."""

    def __init__(self, client: httpx.AsyncClient, hub: EmulatorHub) -> None:
        self.client = client
        self.hub = hub
        self.ctx: ProviderContext = hub.context("slack")
        self._tokens: dict[str, str] | None = None

    # -- tokens ----------------------------------------------------------------------------
    async def tokens(self) -> dict[str, str]:
        if self._tokens is None:
            info = (await self.client.get(f"{SLACK}/_emulator/info")).json()
            self._tokens = info["credentials"]["tokens"]
        return self._tokens

    async def bot(self) -> str:
        return (await self.tokens())["bot"]

    async def user_token(self, person: str) -> str:
        return (await self.tokens())[f"user:{person}"]

    async def mint(self, **payload: Any) -> str:
        r = await self.client.post(f"{SLACK}/_emulator/tokens", json=payload)
        r.raise_for_status()
        return r.json()["access_token"]

    # -- calls -----------------------------------------------------------------------------
    async def api(
        self, method: str, token: str | None = None, *, json: Any = None, **params: Any
    ) -> dict[str, Any]:
        """POST form-encoded (or JSON when ``json`` is given) with ``token`` (default: the bot token)."""
        token = token or await self.bot()
        headers = {"Authorization": f"Bearer {token}"}
        if json is not None:
            r = await self.client.post(f"{SLACK}/api/{method}", headers=headers, json=json)
        else:
            r = await self.client.post(f"{SLACK}/api/{method}", headers=headers, data=_encode(params))
        assert r.status_code == 200, r.text
        return r.json()

    async def ok(self, method: str, token: str | None = None, **params: Any) -> dict[str, Any]:
        body = await self.api(method, token, **params)
        assert body["ok"] is True, body
        return body

    async def err(self, method: str, token: str | None = None, **params: Any) -> str:
        body = await self.api(method, token, **params)
        assert body["ok"] is False, body
        return body["error"]

    # -- world lookups ---------------------------------------------------------------------
    def channel_id(self, name: str) -> str:
        record = self.ctx.store.collection("channels").first(name=name)
        assert record is not None, name
        return record["id"]

    def uid(self, person: str) -> str:
        record = self.ctx.store.collection("users").first(_person=person)
        assert record is not None, person
        return record["id"]

    def channel_without(self, person: str) -> str:
        """A public channel the person has not joined."""
        uid = self.uid(person)
        for record in self.ctx.store.collection("channels"):
            if record["is_channel"] and not record["is_archived"] and uid not in record["_members"]:
                return record["id"]
        raise AssertionError(f"{person} is a member of every public channel")

    def person_email(self, person: str) -> str:
        return self.ctx.world.person(person).email

    async def history(self, channel: str, token: str | None = None, **params: Any) -> list[dict[str, Any]]:
        body = await self.ok("conversations.history", token, channel=channel, **params)
        return body["messages"]


def _encode(params: dict[str, Any]) -> dict[str, Any]:
    import json as _json

    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            out[key] = _json.dumps(value)
        else:
            out[key] = value
    return out


@pytest.fixture
def slack_session(make_client: Callable[..., Any]) -> Callable[[], Any]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[Slack]:
        async with make_client("slack") as client:
            yield Slack(client, client.hub)

    return factory


@pytest.fixture
async def slack(make_client: Callable[..., Any]) -> AsyncIterator[Slack]:
    async with make_client("slack") as client:
        yield Slack(client, client.hub)


class Receiver:
    """An in-process consumer app that records what the emulator delivers to it."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.fail_times = 0
        self.challenge_mode = "json"
        self.app = FastAPI()

        @self.app.post("/slack/events")
        async def events(request: Request) -> Any:
            body = await request.body()
            import json

            payload = json.loads(body)
            if payload.get("type") == "url_verification":
                self.requests.append({"headers": dict(request.headers), "body": body, "payload": payload})
                if self.challenge_mode == "text":
                    from fastapi.responses import PlainTextResponse

                    return PlainTextResponse(payload["challenge"])
                if self.challenge_mode == "wrong":
                    return {"challenge": "nope"}
                return {"challenge": payload["challenge"]}
            self.requests.append({"headers": dict(request.headers), "body": body, "payload": payload})
            if self.fail_times > 0:
                self.fail_times -= 1
                from fastapi.responses import JSONResponse

                return JSONResponse({"error": "boom"}, status_code=500)
            return {}

    @property
    def events(self) -> list[dict[str, Any]]:
        return [r["payload"] for r in self.requests if r["payload"].get("type") == "event_callback"]


@pytest.fixture
def receiver(slack: Slack) -> Receiver:
    import httpx as _httpx

    consumer = Receiver()
    slack.hub.webhook_transport = _httpx.ASGITransport(app=consumer.app)
    return consumer
