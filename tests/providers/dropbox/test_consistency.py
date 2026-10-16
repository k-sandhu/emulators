"""Randomised file-tree operations checked against a trivial in-memory model, plus ``ns:`` paths."""

from __future__ import annotations

import base64
import hashlib
import random
from typing import Any


class Model:
    """Case-insensitive path -> kind/content, with Dropbox's create/move/copy/delete semantics."""

    def __init__(self) -> None:
        self.nodes: dict[str, tuple[str, bytes | None]] = {}  # lower path -> (kind, content)

    def parent_chain(self, path: str) -> list[str]:
        parts = path.strip("/").split("/")
        return ["/" + "/".join(parts[: i + 1]) for i in range(len(parts) - 1)]

    def mkdir(self, path: str) -> bool:
        for parent in [*self.parent_chain(path), path]:
            if parent in self.nodes and self.nodes[parent][0] == "file":
                return False
        for parent in [*self.parent_chain(path), path]:
            self.nodes.setdefault(parent, ("folder", None))
        return True

    def put(self, path: str, content: bytes) -> bool:
        if path in self.nodes:
            return False
        for parent in self.parent_chain(path):
            if parent in self.nodes and self.nodes[parent][0] == "file":
                return False
        for parent in self.parent_chain(path):
            self.nodes.setdefault(parent, ("folder", None))
        self.nodes[path] = ("file", content)
        return True

    def subtree(self, path: str) -> list[str]:
        return [p for p in self.nodes if p == path or p.startswith(path + "/")]

    def delete(self, path: str) -> bool:
        if path not in self.nodes:
            return False
        for p in self.subtree(path):
            del self.nodes[p]
        return True

    def relocate(self, src: str, dst: str, copy: bool) -> bool:
        if src not in self.nodes or dst in self.nodes or dst.startswith(src + "/"):
            return False
        for parent in self.parent_chain(dst):
            if parent in self.nodes and self.nodes[parent][0] == "file":
                return False
        moved = {p: self.nodes[p] for p in self.subtree(src)}
        if not copy:
            for p in moved:
                del self.nodes[p]
        for parent in self.parent_chain(dst):
            self.nodes.setdefault(parent, ("folder", None))
        for p, value in moved.items():
            self.nodes[dst + p[len(src) :]] = value
        return True


async def test_random_operations_match_the_model(dbx: Any) -> None:
    rng = random.Random(20261016)
    token = await dbx.token(dbx.MAYA)
    model = Model()
    names = ["alpha", "Beta", "gamma", "delta", "x", "Y"]

    def random_path(depth: int | None = None) -> str:
        depth = depth or rng.randint(1, 3)
        return "/fuzz/" + "/".join(rng.choice(names) for _ in range(depth))

    await dbx.ok(token, "files/create_folder_v2", {"path": "/fuzz"})
    model.mkdir("/fuzz")
    for step in range(220):
        op = rng.choice(["mkdir", "put", "put", "move", "copy", "delete"])
        path = random_path()
        if op == "mkdir":
            r = await dbx.rpc(
                token, "files/create_folder_v2", {"path": path.upper() if rng.random() < 0.3 else path}
            )
            expected = model.mkdir(path.lower())
            assert (r.status_code == 200) == expected or r.status_code == 409, (step, op, path, r.text)
            if r.status_code == 200:
                model.mkdir(path.lower())
            elif expected:
                model_before = path.lower()
                # the only legitimate 409 for a mkdir the model accepted is "already exists as folder"
                assert r.json()["error_summary"].startswith("path/conflict/folder"), (
                    step,
                    model_before,
                    r.text,
                )
        elif op == "put":
            content = f"step-{step}".encode()
            r = await dbx.upload(token, "files/upload", {"path": path}, content)
            ok = model.put(path.lower(), content)
            assert (r.status_code == 200) == ok, (step, op, path, r.text)
        elif op in ("move", "copy"):
            dst = random_path()
            r = await dbx.rpc(token, f"files/{op}_v2", {"from_path": path, "to_path": dst})
            ok = model.relocate(path.lower(), dst.lower(), copy=op == "copy")
            if path.lower() == dst.lower():
                continue
            assert (r.status_code == 200) == ok, (step, op, path, dst, r.text)
        else:
            r = await dbx.rpc(token, "files/delete_v2", {"path": path})
            assert (r.status_code == 200) == model.delete(path.lower()), (step, op, path, r.text)
        if step % 25 == 24:
            await check_state(dbx, token, model)
    await check_state(dbx, token, model)


async def check_state(dbx: Any, token: str, model: Model) -> None:
    entries = await dbx.list_all(token, "/fuzz", recursive=True)
    actual = {e["path_lower"]: e[".tag"] for e in entries}
    expected = {p: kind for p, (kind, _) in model.nodes.items() if p != "/fuzz"}
    assert actual == expected
    for path, (kind, content) in model.nodes.items():
        if kind == "file":
            r, _ = await dbx.download(token, path)
            assert r.content == content, path


async def test_namespace_paths_and_app_auth(dbx: Any) -> None:
    maya = await dbx.token(dbx.MAYA)
    home = (await dbx.ok(maya, "users/get_current_account"))["root_info"]["home_namespace_id"]
    listing = await dbx.ok(maya, "files/list_folder", {"path": f"ns:{home}"})
    assert {e["name"] for e in listing["entries"]} == {"Notes", "Expenses"}
    inside = await dbx.ok(maya, "files/list_folder", {"path": f"ns:{home}/Notes"})
    assert inside["entries"] and inside["entries"][0]["path_lower"].startswith("/notes/")
    root = await dbx.err(maya, "files/get_metadata", {"path": f"ns:{home}"})
    assert root["error"]["path"][".tag"] == "malformed_path"
    other = await dbx.err(maya, "files/list_folder", {"path": "ns:12345"})
    assert other["error"]["path"][".tag"] == "not_found"
    meta = await dbx.put(maya, f"ns:{home}/by-ns.txt", b"ns")
    assert meta["path_display"] == "/by-ns.txt"
    # app auth (HTTP Basic) can only use link-based reads
    basic = {
        "Authorization": "Basic " + base64.b64encode(b"app:secret").decode(),
        "Content-Type": "application/json",
    }
    link = await dbx.ok(maya, "sharing/create_shared_link_with_settings", {"path": "/by-ns.txt"})
    r = await dbx.client.post(
        f"{dbx.API}/2/sharing/get_shared_link_metadata", headers=basic, json={"url": link["url"]}
    )
    assert r.status_code == 200 and r.json()["name"] == "by-ns.txt"
    r = await dbx.client.post(f"{dbx.API}/2/files/list_folder", headers=basic, json={"path": ""})
    assert r.status_code == 400 and "shared_link" in r.text
    team_only = await dbx.ok(
        maya,
        "sharing/modify_shared_link_settings",
        {"url": link["url"], "settings": {"requested_visibility": {".tag": "team_only"}}},
    )
    assert team_only["link_permissions"]["resolved_visibility"] == {".tag": "team_only"}
    r = await dbx.client.post(
        f"{dbx.API}/2/sharing/get_shared_link_metadata", headers=basic, json={"url": link["url"]}
    )
    assert r.status_code == 409 and r.json()["error"][".tag"] == "shared_link_access_denied"


def test_content_hash_matches_dropbox_reference_implementation() -> None:
    from emulators.providers.dropbox.common import content_hash

    data = b"abc" * 3_000_000
    blocks = [hashlib.sha256(data[i : i + 4194304]).digest() for i in range(0, len(data), 4194304)]
    assert content_hash(data) == hashlib.sha256(b"".join(blocks)).hexdigest()
    assert content_hash(b"") == hashlib.sha256(b"").hexdigest()


async def test_prefix_routing_keeps_absolute_urls_on_the_gateway(hub: Any) -> None:
    import httpx

    transport = httpx.ASGITransport(app=hub.gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080") as client:
        token = (
            await client.post("/dropbox/_emulator/tokens", json={"user": "maya.chen@halcyonadvisory.example"})
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        up = await client.post(
            "/dropbox/2/files/upload",
            headers={
                **headers,
                "Dropbox-API-Arg": '{"path": "/prefix.txt"}',
                "Content-Type": "application/octet-stream",
            },
            content=b"prefix mode",
        )
        assert up.status_code == 200, up.text
        link = (
            await client.post(
                "/dropbox/2/files/get_temporary_link", headers=headers, json={"path": "/prefix.txt"}
            )
        ).json()["link"]
        assert link.startswith("http://localhost:44080/dropbox/apitl/1/")
        assert (await client.get(link)).content == b"prefix mode"
        shared = (
            await client.post(
                "/dropbox/2/sharing/create_shared_link_with_settings",
                headers=headers,
                json={"path": "/prefix.txt"},
            )
        ).json()["url"]
        assert shared.startswith("http://localhost:44080/dropbox/scl/fi/")
        redirect = await client.get(shared.replace("dl=0", "dl=1"))
        assert redirect.status_code == 302 and redirect.headers["location"].startswith(
            "http://localhost:44080/dropbox/cd/0/get/"
        )
        assert (await client.get(redirect.headers["location"])).content == b"prefix mode"
        authorize = await client.get(
            "/dropbox/oauth2/authorize",
            params={
                "client_id": "a",
                "redirect_uri": "http://app.test/cb",
                "login_hint": "maya.chen@halcyonadvisory.example",
            },
        )
        assert authorize.status_code == 302
