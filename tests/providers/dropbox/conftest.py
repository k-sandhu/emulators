"""Fixtures for the Dropbox emulator tests.

``dbx`` wraps an in-process hub with only the dropbox provider and exposes small helpers that speak
the real wire protocol (RPC / upload / download) on the real hostnames. Every ``ok``/``err``
response is checked against the official SDK's Stone validators -- the strictest test that our JSON
matches what the real SDK decodes.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

API = "https://api.dropboxapi.com"
CONTENT = "https://content.dropboxapi.com"
NOTIFY = "https://notify.dropboxapi.com"
WWW = "https://www.dropbox.com"

MAYA = "maya.chen@halcyonadvisory.example"
ARJUN = "arjun.mehta@halcyonadvisory.example"
SARAH = "sarah.johansson@halcyonadvisory.example"
ELEANOR = "eleanor.whitfield@halcyonadvisory.example"
CHRISTOPHER = "christopher.ingram@northwindlogistics.example"  # Northwind Logistics client contact


def sdk_route(name: str) -> Any:
    namespace, _, rest = name.partition("/")
    module = importlib.import_module(f"dropbox.{namespace}")
    return getattr(module, rest.replace("/", "_"))


def tag_problems(v: Any, obj: Any, path: str = "$", flat: bool = False) -> list[str]:
    """Where our ``.tag`` usage differs from Stone's rules.

    The SDK decoder *ignores* stray ``.tag`` keys, so strict decoding alone cannot tell
    ``FileMetadata`` (leaf type: never tagged) from ``Metadata`` (polymorphic: always tagged).
    """
    from stone.backends.python_rsrc import stone_validators as bv

    out: list[str] = []
    if obj is None:
        return out
    if isinstance(v, bv.Nullable):
        return tag_problems(v.validator, obj, path, flat)
    if isinstance(v, bv.List) and isinstance(obj, list):
        for i, item in enumerate(obj):
            out += tag_problems(v.item_validator, item, f"{path}[{i}]")
        return out
    if isinstance(v, bv.Struct) and isinstance(obj, dict):
        cls = v.definition
        subtypes = vars(cls).get("_tag_to_subtype_")
        if subtypes:  # polymorphic base: the tag picks the concrete struct
            found = obj.get(".tag")
            target = subtypes.get((found,)) if found is not None else None
            if found is None:
                out.append(f"{path}: {cls.__name__} needs a .tag")
            if target is not None:
                v = target
        elif ".tag" in obj and not flat:
            out.append(f"{path}: leaf struct {cls.__name__} must not carry .tag")
        for fname, fv in v.definition._all_fields_:
            if fname in obj:
                out += tag_problems(fv, obj[fname], f"{path}.{fname}")
        return out
    if isinstance(v, bv.Union) and isinstance(obj, dict):
        variant = v.definition._tagmap.get(obj.get(".tag"))
        if variant is None or isinstance(variant, bv.Void):
            return out
        inner = variant.validator if isinstance(variant, bv.Nullable) else variant
        if isinstance(inner, bv.Struct) and not vars(inner.definition).get("_tag_to_subtype_"):
            return out + tag_problems(inner, obj, path, flat=True)  # struct payloads are inlined
        if obj.get(obj[".tag"]) is not None:
            out += tag_problems(variant, obj[obj[".tag"]], f"{path}.{obj['.tag']}")
        return out
    return out


def sdk_check(name: str, kind: str, obj: Any) -> Any:
    """Decode ``obj`` with the SDK validator of route ``name``; ``kind`` is arg|result|error."""
    from stone.backends.python_rsrc import stone_serializers as ss

    route = sdk_route(name)
    validator = {"arg": route.arg_type, "result": route.result_type, "error": route.error_type}[kind]
    decoded = ss.json_compat_obj_decode(validator, obj, strict=True)
    problems = tag_problems(validator, obj)
    assert not problems, f"{name} {kind}: " + "; ".join(problems[:5])
    return decoded


class Dbx:
    """A tiny Dropbox wire client bound to a hub."""

    MAYA = MAYA
    ARJUN = ARJUN
    SARAH = SARAH
    ELEANOR = ELEANOR
    CHRISTOPHER = CHRISTOPHER
    API = API
    CONTENT = CONTENT
    NOTIFY = NOTIFY
    WWW = WWW
    check = staticmethod(sdk_check)

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.tokens: dict[str, str] = {}

    # -- auth -------------------------------------------------------------------------
    async def token(self, user: str = MAYA, **payload: Any) -> str:
        key = json.dumps([user, payload], sort_keys=True)
        if key not in self.tokens:
            r = await self.client.post(f"{API}/_emulator/tokens", json={"user": user, **payload})
            r.raise_for_status()
            self.tokens[key] = r.json()["access_token"]
        return self.tokens[key]

    async def team_token(self, user: str = ARJUN, **payload: Any) -> str:
        return await self.token(user, team=True, **payload)

    def headers(
        self,
        token: str,
        *,
        select_user: str | None = None,
        select_admin: str | None = None,
        path_root: dict[str, str] | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        h = {"Authorization": f"Bearer {token}"}
        if select_user:
            h["Dropbox-API-Select-User"] = select_user
        if select_admin:
            h["Dropbox-API-Select-Admin"] = select_admin
        if path_root:
            h["Dropbox-API-Path-Root"] = json.dumps(path_root)
        return {**h, **(extra or {})}

    # -- calls -------------------------------------------------------------------------
    def validate(self, route: str, r: httpx.Response) -> httpx.Response:
        """Every 200/409 that passes through the helpers must decode with the SDK's validators."""
        try:
            sdk_route(route)
        except (AttributeError, ImportError):
            return r
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
            sdk_check(route, "result", r.json())
        elif r.status_code == 200 and "dropbox-api-result" in r.headers:
            sdk_check(route, "result", json.loads(r.headers["dropbox-api-result"]))
        elif r.status_code == 409 and r.headers.get("content-type", "").startswith("application/json"):
            body = r.json()
            if "error" in body:
                sdk_check(route, "error", body["error"])
        return r

    async def rpc(
        self, token: str, route: str, body: Any = None, *, host: str = API, **header_kw: Any
    ) -> httpx.Response:
        headers = self.headers(token, **header_kw)
        if body is None:
            return self.validate(route, await self.client.post(f"{host}/2/{route}", headers=headers))
        headers["Content-Type"] = "application/json"
        r = await self.client.post(f"{host}/2/{route}", headers=headers, content=json.dumps(body))
        return self.validate(route, r)

    async def ok(self, token: str, route: str, body: Any = None, *, check: bool = True, **kw: Any) -> Any:
        r = await self.rpc(token, route, body, **kw)
        assert r.status_code == 200, f"{route} -> {r.status_code}: {r.text[:500]}"
        data = r.json()
        if check:
            sdk_check(route, "result", data)
        return data

    async def err(self, token: str, route: str, body: Any = None, status: int = 409, **kw: Any) -> Any:
        r = await self.rpc(token, route, body, **kw)
        assert r.status_code == status, f"{route} -> {r.status_code}: {r.text[:500]}"
        if status == 409:
            data = r.json()
            sdk_check(route, "error", data["error"])
            return data
        if status in (401, 429, 422):
            return r.json()
        return r.text

    async def upload(
        self, token: str, route: str, arg: dict[str, Any], content: bytes = b"", **header_kw: Any
    ) -> httpx.Response:
        headers = self.headers(token, **header_kw)
        headers["Dropbox-API-Arg"] = json.dumps(arg)
        headers.setdefault("Content-Type", "application/octet-stream")
        r = await self.client.post(f"{CONTENT}/2/{route}", headers=headers, content=content)
        return self.validate(route, r)

    async def put(self, token: str, path: str, content: bytes, **arg: Any) -> Any:
        header_kw = {k: arg.pop(k) for k in ("select_user", "select_admin", "path_root") if k in arg}
        r = await self.upload(token, "files/upload", {"path": path, **arg}, content, **header_kw)
        assert r.status_code == 200, r.text
        data = r.json()
        sdk_check("files/upload", "result", data)
        return data

    async def download(
        self,
        token: str,
        path: str,
        *,
        route: str = "files/download",
        range_: str | None = None,
        **header_kw: Any,
    ) -> tuple[httpx.Response, Any]:
        headers = self.headers(token, **header_kw)
        headers["Dropbox-API-Arg"] = json.dumps({"path": path})
        if range_:
            headers["Range"] = range_
        r = await self.client.post(f"{CONTENT}/2/{route}", headers=headers)
        self.validate(route, r)
        result = json.loads(r.headers["dropbox-api-result"]) if "dropbox-api-result" in r.headers else None
        return r, result

    async def list_all(self, token: str, path: str = "", **kw: Any) -> list[dict[str, Any]]:
        header_kw = {k: kw.pop(k) for k in ("select_user", "select_admin", "path_root") if k in kw}
        page = await self.ok(token, "files/list_folder", {"path": path, **kw}, **header_kw)
        entries = list(page["entries"])
        while page["has_more"]:
            page = await self.ok(token, "files/list_folder/continue", {"cursor": page["cursor"]}, **header_kw)
            entries.extend(page["entries"])
        return entries

    async def root_ns(self, token: str, **kw: Any) -> str:
        info = await self.ok(token, "users/get_current_account", **kw)
        return info["root_info"]["root_namespace_id"]

    async def root(self, token: str, **kw: Any) -> dict[str, str]:
        """``Dropbox-API-Path-Root`` value selecting the team-space root."""
        return {".tag": "root", "root": await self.root_ns(token, **kw)}

    async def member_id(self, email: str) -> str:
        arjun = await self.team_token()
        data = await self.ok(
            arjun, "team/members/get_info_v2", {"members": [{".tag": "email", "email": email}]}
        )
        return data["members_info"][0]["profile"]["team_member_id"]

    async def account_id(self, email: str) -> str:
        token = await self.token(email)
        return (await self.ok(token, "users/get_current_account"))["account_id"]


@pytest.fixture
async def dbx(make_client: Any) -> AsyncIterator[Dbx]:
    async with make_client("dropbox") as client:
        yield Dbx(client)


@pytest.fixture
def hub(dbx: Dbx) -> Any:
    return dbx.client.hub  # type: ignore[attr-defined]
