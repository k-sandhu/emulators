"""The seeded Dropbox team: deterministic, fast, and faithful to the shared world."""

from __future__ import annotations

import time
from typing import Any

from emulators.core.hub import EmulatorHub


def collection(hub: Any, name: str) -> list[dict[str, Any]]:
    return hub.contexts["dropbox"].store.collection(name).values()


def test_seed_is_fast_and_deterministic() -> None:
    started = time.perf_counter()
    first = EmulatorHub(providers=["dropbox"], anchor="2026-10-16")
    elapsed = time.perf_counter() - started
    assert elapsed < 4.0  # includes world generation and app construction
    ctx = first.contexts["dropbox"]
    started = time.perf_counter()
    first._seed("dropbox")
    assert time.perf_counter() - started < 2.0
    second = EmulatorHub(providers=["dropbox"], anchor="2026-10-16")

    def fingerprint(hub: EmulatorHub) -> list[tuple[Any, ...]]:
        entries = hub.contexts["dropbox"].store.collection("entries").values()
        return sorted((e["id"], e["ns"], e["path"], e.get("revisions", [{}])[-1].get("rev")) for e in entries)

    assert fingerprint(first) == fingerprint(second)
    accounts = lambda hub: sorted(a["id"] for a in hub.contexts["dropbox"].store.collection("accounts"))  # noqa: E731
    assert accounts(first) == accounts(second)
    assert ctx is first.contexts["dropbox"]


def test_world_is_projected_into_the_team() -> None:
    hub = EmulatorHub(providers=["dropbox"], anchor="2026-10-16")
    world = hub.world
    accounts = collection(hub, "accounts")
    team = [a for a in accounts if a["is_team"]]
    assert len(team) == len(world.employees)
    assert {a["email"] for a in team if a["status"] == "suspended"} == {
        p.email for p in world.employees if not p.active
    }
    admins = {a["email"]: [r["name"] for r in a["roles"]] for a in team if a["roles"]}
    assert admins == {
        "arjun.mehta@halcyonadvisory.example": ["Team admin"],
        "sarah.johansson@halcyonadvisory.example": ["User management admin"],
    }
    assert len(accounts) == len(world.people)  # client contacts are non-team accounts
    namespaces = {ns["id"]: ns for ns in collection(hub, "namespaces")}
    team_folders = [ns for ns in namespaces.values() if ns["type"] == "team_folder"]
    assert {ns["name"] for ns in team_folders} == {s.name for s in world.spaces if s.kind == "team"}
    clients = next(ns for ns in team_folders if ns["name"] == "Client Engagements")
    roles = {}
    for member in clients["members"]:
        gid = next(g for g in collection(hub, "groups") if g["id"] == member["group_id"])["external_id"]
        roles[gid] = member["access_type"]
    assert roles == {
        "g-partners": "editor",
        "g-audit": "editor",
        "g-tax": "editor",
        "g-advisory": "editor",
        "g-finance": "viewer",
    }
    entries = collection(hub, "entries")
    files = [e for e in entries if e["type"] == "file" and e.get("doc") is not None]
    assert len(files) == len(world.documents)
    assert sum(1 for e in files if e.get("no_inherit")) == sum(1 for d in world.documents if d.restricted)
    shared = [e for e in files if e["members"]]
    assert len(shared) == len({d.id for d in world.documents if any(s.principal_id for s in d.shares)})
    links = collection(hub, "links")
    assert len(links) == sum(1 for d in world.documents for s in d.shares if s.link != "none")
    # duplicate world paths are disambiguated Dropbox-style
    names = [
        e["name"]
        for e in entries
        if e["ns"] == clients["id"] and e["path"].endswith("/client onboarding form (1).pdf")
    ]
    assert names


def test_every_file_has_real_content_and_revisions() -> None:
    hub = EmulatorHub(providers=["dropbox"], anchor="2026-10-16")
    ctx = hub.contexts["dropbox"]
    docs = {d.id: d for d in hub.world.documents}
    for rec in ctx.store.collection("entries"):
        if rec["type"] != "file" or not rec.get("doc"):
            continue
        latest = rec["revisions"][-1]
        data = ctx.store.blobs.get(latest["blob"])
        assert len(data) == latest["size"] > 0
        assert len(rec["revisions"]) == docs[rec["doc"]].versions
        assert [r["rev"] for r in rec["revisions"]] == sorted(
            {r["rev"] for r in rec["revisions"]}, key=[r["rev"] for r in rec["revisions"]].index
        )
        stamps = [r["server_modified"] for r in rec["revisions"]]
        assert stamps == sorted(stamps)


async def test_reset_restores_the_seed_and_snapshots_work(dbx: Any, hub: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    before = len(collection(hub, "entries"))
    await dbx.put(token, "/reset-me.txt", b"x")
    assert len(collection(hub, "entries")) == before + 1
    snap = await dbx.client.post(f"{dbx.API}/_emulator/snapshots", json={"name": "after-upload"})
    assert snap.json() == {"name": "after-upload"}
    await dbx.ok(token, "files/delete_v2", {"path": "/reset-me.txt"})
    restored = await dbx.client.post(f"{dbx.API}/_emulator/snapshots/after-upload/restore")
    assert restored.status_code == 200
    token = await dbx.token(dbx.MAYA)
    assert (await dbx.ok(token, "files/get_metadata", {"path": "/reset-me.txt"}))["name"] == "reset-me.txt"
    await dbx.client.post(f"{dbx.API}/_emulator/reset")
    token = await dbx.token(dbx.MAYA, expires_in=7200)
    assert len(collection(hub, "entries")) == before
    err = await dbx.err(token, "files/get_metadata", {"path": "/reset-me.txt"})
    assert err["error"]["path"][".tag"] == "not_found"
    info = (await dbx.client.get(f"{dbx.API}/_emulator/info")).json()
    assert (
        info["name"] == "dropbox"
        and "mint_token" in info["actions"]
        and info["collections"]["entries"] == before
    )
    state = (await dbx.client.get(f"{dbx.API}/_emulator/state/accounts", params={"limit": 3})).json()
    assert state["count"] > 100
