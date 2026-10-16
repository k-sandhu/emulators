"""The live server + interceptors let unmodified clients reach emulators by real hostname."""

from __future__ import annotations

import json
import urllib.request

import httpx
import pytest
import requests
from fastapi import FastAPI, Request

from emulators.core.hub import EmulatorHub
from emulators.core.provider import Provider, ProviderContext, external_base
from emulators.testing.intercept import intercept
from emulators.testing.server import EmulatorServer


class PingProvider(Provider):
    name = "ping"
    title = "Ping"
    hosts = ("api.ping.example",)

    def build(self, app: FastAPI, ctx: ProviderContext) -> None:
        @app.api_route("/v1/echo", methods=["GET", "POST"])
        async def echo(request: Request) -> dict:
            return {
                "method": request.method,
                "base": external_base(request),
                "host": request.headers.get("host"),
                "body": (await request.body()).decode(),
            }


@pytest.fixture(scope="module")
def server():
    hub = EmulatorHub(providers=["ping"], extra_providers=[PingProvider()])
    with EmulatorServer(hub) as srv:
        yield srv


def test_prefix_url_on_live_server(server: EmulatorServer) -> None:
    r = httpx.get(f"{server.url_for('ping')}/v1/echo")
    assert r.status_code == 200
    assert r.json()["base"] == server.url_for("ping")


def test_httpx_sync_and_requests_are_rerouted(server: EmulatorServer) -> None:
    with intercept(server):
        r1 = httpx.get("https://api.ping.example/v1/echo")
        r2 = requests.post("https://api.ping.example/v1/echo", data="hello")
    assert r1.json()["base"] == "https://api.ping.example"
    assert r2.json() == {
        "method": "POST",
        "base": "https://api.ping.example",
        "host": "api.ping.example",
        "body": "hello",
    }


async def test_httpx_async_is_rerouted(server: EmulatorServer) -> None:
    with intercept(server):
        async with httpx.AsyncClient() as client:
            r = await client.get("https://api.ping.example/v1/echo")
    assert r.json()["host"] == "api.ping.example"


def test_urllib_and_httplib2_are_rerouted(server: EmulatorServer) -> None:
    httplib2 = pytest.importorskip("httplib2")
    with intercept(server):
        with urllib.request.urlopen("https://api.ping.example/v1/echo") as resp:
            body = json.loads(resp.read())
        _, content = httplib2.Http().request("https://api.ping.example/v1/echo")
    assert body["base"] == "https://api.ping.example"
    assert json.loads(content)["host"] == "api.ping.example"


def test_patches_removed_after_block(server: EmulatorServer) -> None:
    with intercept(server):
        pass
    with pytest.raises(httpx.ConnectError):
        httpx.get("https://api.ping.example/v1/echo", timeout=2)
