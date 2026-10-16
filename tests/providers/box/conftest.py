"""Fixtures for the Box provider tests: a thin client over the in-process hub transport."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

API = "https://api.box.com/2.0"
UPLOAD = "https://upload.box.com/api/2.0"


class Box:
    """Calls Box like a consumer would: real hostnames, bearer tokens, JSON in and out."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.hub = client.hub  # type: ignore[attr-defined]

    @staticmethod
    def auth(who: str = "p035") -> dict[str, str]:
        """``who`` is a person id (``p018``), ``enterprise`` or a raw access token."""
        token = who if who.startswith(("box-", "tok-")) or len(who) > 12 else f"box-dev-{who}"
        return {"Authorization": f"Bearer {token}"}

    async def request(self, method: str, path: str, who: str = "p035", **kw: Any) -> httpx.Response:
        url = path if path.startswith("http") else f"{API}{path}"
        headers = {**self.auth(who), **kw.pop("headers", {})}
        return await self.client.request(method, url, headers=headers, **kw)

    async def get(self, path: str, who: str = "p035", **kw: Any) -> httpx.Response:
        return await self.request("GET", path, who, **kw)

    async def post(self, path: str, who: str = "p035", **kw: Any) -> httpx.Response:
        return await self.request("POST", path, who, **kw)

    async def put(self, path: str, who: str = "p035", **kw: Any) -> httpx.Response:
        return await self.request("PUT", path, who, **kw)

    async def delete(self, path: str, who: str = "p035", **kw: Any) -> httpx.Response:
        return await self.request("DELETE", path, who, **kw)

    async def items(self, folder_id: str = "0", who: str = "p035", **params: Any) -> list[dict[str, Any]]:
        params.setdefault("limit", 1000)
        r = await self.get(f"/folders/{folder_id}/items", who, params=params)
        assert r.status_code == 200, r.text
        return r.json()["entries"]

    async def find(self, path: str, who: str = "p035") -> str:
        """Id of the item at ``Client Engagements/Northwind Logistics/...`` in ``who``'s view."""
        current = "0"
        for part in [p for p in path.split("/") if p]:
            entries = await self.items(current, who)
            match = next((e for e in entries if e["name"] == part), None)
            assert match is not None, (
                f"{part!r} not found under {current} for {who}: {[e['name'] for e in entries]}"
            )
            current = match["id"]
        return current

    async def upload(
        self, parent_id: str, name: str, data: bytes, who: str = "p035", **attributes: Any
    ) -> httpx.Response:
        attrs = {"name": name, "parent": {"id": parent_id}, **attributes}
        return await self.client.post(
            f"{UPLOAD}/files/content",
            headers=self.auth(who),
            files={"attributes": (None, json.dumps(attrs)), "file": (name, data)},
        )

    async def mkdir(self, parent_id: str, name: str, who: str = "p035") -> str:
        r = await self.post("/folders", who, json={"name": name, "parent": {"id": parent_id}})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def externals(self) -> list[dict[str, Any]]:
        """Every external (client contact) account. ``GET /users`` never lists outsiders, as in Box:
        they are found by exact login or through collaborations, so tests read them from the store."""
        from emulators.providers.box import db

        ids = [u["id"] for u in db.users(self.hub.context("box")) if u.get("_kind") == "external"]
        users = []
        for user_id in ids:
            r = await self.get(f"/users/{user_id}")
            assert r.status_code == 200, r.text
            users.append(r.json())
        return users

    async def action(self, name: str, payload: dict[str, Any] | None = None) -> Any:
        r = await self.client.post(f"{API.replace('/2.0', '')}/_emulator/actions/{name}", json=payload or {})
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
async def box(make_client: Any) -> AsyncIterator[Box]:
    async with make_client("box", follow_redirects=False) as client:
        clock = client.hub.clock  # type: ignore[attr-defined]
        saved = (clock._offset, clock._frozen)
        try:
            yield Box(client)
        finally:  # the hub is cached across tests: undo any clock tampering
            clock._offset, clock._frozen = saved
