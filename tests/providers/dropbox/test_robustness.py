"""Garbage in, vendor-shaped errors out: no route may blow up on malformed arguments."""

from __future__ import annotations

import json
from typing import Any

import pytest

from emulators.core.hub import EmulatorHub

JUNK: list[Any] = [
    {},
    [],
    "text",
    123,
    True,
    {"path": 5},
    {"path": None},
    {"path": "\u0000"},
    {"path": "/" + "a" * 5000},
    {"path": "/ok", "mode": 7},
    {"cursor": 5},
    {"entries": "x"},
    {"members": [1, "x", None]},
    {"members": [{"member": 5}]},
    {"group": {".tag": "group_id"}},
    {"user": {".tag": "email"}},
    {"query": 5},
    {"limit": "ten"},
    {"shared_folder_id": 5},
    {"url": 5},
    {"settings": "oops"},
    {"commit": 3, "cursor": 4},
    {"new_members": [None]},
    {"template_id": {".tag": "x"}},
    {"deep": {"a": {"b": {"c": [1, [2, [3]]]}}}},
]


@pytest.fixture(scope="module")
def route_names() -> list[str]:
    EmulatorHub(providers=["dropbox"], anchor="2026-10-16")
    from emulators.providers.dropbox.dispatch import ROUTES

    skip = {"files/list_folder/longpoll", "auth/token/revoke"}
    return sorted(set(ROUTES) - skip)


async def test_malformed_arguments_never_crash(dbx: Any, route_names: list[str]) -> None:
    maya = await dbx.token(dbx.MAYA)
    arjun = await dbx.team_token()
    maya_member = await dbx.member_id(dbx.MAYA)
    bad: list[str] = []
    for name in route_names:
        token = (
            arjun
            if name.startswith(("team/", "team_log/"))
            or (name.startswith("file_properties/templates/") and name.endswith("_for_team"))
            else maya
        )
        for body in JUNK:
            kw: dict[str, Any] = {}
            if token == maya and name.startswith("team/"):
                kw["select_user"] = maya_member
            r = await dbx.client.post(
                f"{dbx.API}/2/{name}",
                headers={**dbx.headers(token, **kw), "Content-Type": "application/json"},
                content=json.dumps(body),
            )
            if r.status_code >= 500:
                bad.append(f"{name} <- {json.dumps(body)[:60]}: {r.status_code} {r.text[:80]}")
    assert not bad, "\n".join(bad[:30])


async def test_junk_in_content_routes_never_crashes(dbx: Any) -> None:
    from emulators.providers.dropbox.dispatch import ROUTES

    maya = await dbx.token(dbx.MAYA)
    for name in sorted(n for n, rd in ROUTES.items() if rd.style != "rpc"):
        for body in JUNK:
            headers = {**dbx.headers(maya), "Dropbox-API-Arg": json.dumps(body)}
            r = await dbx.client.post(f"{dbx.CONTENT}/2/{name}", headers=headers, content=b"data")
            assert r.status_code < 500, (name, body, r.text[:100])


async def test_malformed_content_headers_never_crash(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    for name in (
        "files/upload",
        "files/download",
        "files/upload_session/start",
        "files/upload_session/append_v2",
        "files/upload_session/finish",
    ):
        for arg in (
            "",
            "null",
            "[]",
            "{",
            '{"path": 5}',
            '{"cursor": 1}',
            '{"cursor": {"session_id": 1, "offset": "x"}}',
            "\xff\xfe",
        ):
            headers = dbx.headers(maya)
            headers["Dropbox-API-Arg"] = arg.encode("latin-1")  # type: ignore[assignment]
            r = await dbx.client.post(f"{dbx.CONTENT}/2/{name}", headers=headers, content=b"data")
            assert r.status_code < 500, (name, arg, r.text[:100])
