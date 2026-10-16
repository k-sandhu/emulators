"""Calls *every* registered route with SDK-generated arguments and decodes whatever comes back with
the SDK's strict Stone validators: successes against the result type, 409s against the error type.

Arguments are synthesised from the route's own argument validator (so the SDK serialiser vouches for
them), in three flavours: minimal, "everything set" and path-heavy. Most calls fail with a plausible
409 -- which is exactly the point: error unions must be decodable too.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from stone.backends.python_rsrc import stone_validators as bv

from emulators.core.hub import EmulatorHub

HINTS = {
    "path": "/sweep/a.txt",
    "from_path": "/sweep/a.txt",
    "to_path": "/sweep/b.txt",
    "file": "id:sweep",
    "cursor": "AAHnope",
    "async_job_id": "dbjid:none",
    "shared_folder_id": "123",
    "team_folder_id": "123",
    "template_id": "ptid:none",
    "group_id": "g:none",
    "account_id": "dbid:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"[:40],
    "url": "https://www.dropbox.com/scl/fi/nope/a.txt?rlkey=x&dl=0",
    "rev": "0123456789abcdef",
    "email": "nobody@example.com",
    "member_email": "nobody@example.com",
    "query": "sweep",
    "name": "Sweep",
    "tag_text": "sweep",
    "session_id": "pid_upload_session:none",
    "copy_reference": "AAFnope",
    "team_member_id": "dbmid:none",
    "dropbox_id": "dbid:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"[:40],
    "group_name": "Sweep Group",
}


def make(v: Any, name: str, flavour: int, depth: int = 0) -> Any:
    """A JSON-compatible value for validator ``v``; ``None`` means "leave the field out"."""
    if isinstance(v, bv.Nullable):
        return make(v.validator, name, flavour, depth) if flavour >= 1 and depth < 3 else None
    if isinstance(v, bv.String):
        if name in HINTS:
            return HINTS[name]
        if name.endswith("_id"):
            return "id-1"
        return "sweep"
    if isinstance(v, bv.Boolean):
        return flavour == 1
    if isinstance(v, (bv.UInt64, bv.UInt32, bv.Int64, bv.Int32)):
        return max(1, getattr(v, "min_value", None) or 1)
    if isinstance(v, bv.Float64):
        return 3600.0
    if isinstance(v, bv.Timestamp):
        return "2030-01-01T00:00:00Z"
    if isinstance(v, bv.List):
        item = make(v.item_validator, name, flavour, depth + 1)
        return [] if item is None else [item]
    if isinstance(v, bv.Map):
        return {}
    if isinstance(v, bv.Void):
        return None
    if isinstance(v, bv.Struct):
        if depth > 4:
            return {}
        out: dict[str, Any] = {}
        for fname, fv in v.definition._all_fields_:
            value = make(fv, fname, flavour, depth + 1)
            if value is not None:
                out[fname] = value
        return out
    if isinstance(v, bv.Union):
        tags = [(t, tv) for t, tv in v.definition._tagmap.items() if t != "other"]
        if not tags:
            return {".tag": "other"}
        # Prefer a void tag, otherwise the first one that builds.
        order = sorted(tags, key=lambda item: (not isinstance(item[1], bv.Void), item[0]))
        pick = order[0] if flavour == 0 else order[min(flavour, len(order) - 1)]
        tname, tv = pick
        if isinstance(tv, bv.Void) or depth > 4:
            return {".tag": tname}
        inner = make(tv, tname, flavour, depth + 1)
        if isinstance(tv, bv.Struct) and isinstance(inner, dict):
            return {".tag": tname, **inner}
        return {".tag": tname, tname: inner} if inner is not None else {".tag": tname}
    return None


def sdk_routes() -> dict[str, Any]:
    import importlib

    from emulators.providers.dropbox.dispatch import ROUTES

    out = {}
    for name in ROUTES:
        ns, _, rest = name.partition("/")
        route = getattr(importlib.import_module(f"dropbox.{ns}"), rest.replace("/", "_"), None)
        if route is not None:
            out[name] = route
    return out


SKIP = {
    "files/list_folder/longpoll",  # waits; covered elsewhere
    "auth/token/revoke",  # revokes the token the sweep is using
    "team/members/remove",  # would remove members mid-sweep
    "team/members/suspend",
    "team/members/set_admin_permissions_v2",
}


@pytest.fixture(scope="module")
def routes() -> dict[str, Any]:
    EmulatorHub(providers=["dropbox"], anchor="2026-10-16")  # imports every route module
    return sdk_routes()


async def test_every_route_answers_with_sdk_decodable_shapes(dbx: Any, routes: dict[str, Any]) -> None:
    from stone.backends.python_rsrc import stone_serializers as ss

    maya = await dbx.token(dbx.MAYA)
    team = await dbx.team_token()
    maya_member = await dbx.member_id(dbx.MAYA)
    problems: list[str] = []
    seen_codes: dict[str, set[int]] = {}
    stats = {"ok": 0, "409": 0, "other": 0}
    for name, route in sorted(routes.items()):
        if name in SKIP:
            continue
        style = route.attrs["style"] or "rpc"
        is_team = (route.attrs["auth"] or "").strip() == "team"
        token = team if is_team else maya
        header_kw: dict[str, Any] = {}
        if not is_team and (route.attrs["auth"] or "").startswith("team"):
            header_kw["select_user"] = maya_member
        for flavour in (0, 1, 2):
            arg = make(route.arg_type, "arg", flavour)
            if isinstance(route.arg_type, bv.Void):
                arg = None
            # the SDK serialiser must accept what we are about to send
            if arg is not None:
                try:
                    ss.json_compat_obj_decode(route.arg_type, arg, strict=False)
                except Exception:
                    continue
            headers = dbx.headers(token, **header_kw)
            if style == "rpc":
                headers["Content-Type"] = "application/json"
                r = await dbx.client.post(
                    f"{dbx.API}/2/{name}",
                    headers=headers,
                    content=json.dumps(arg) if arg is not None else None,
                )
            else:
                headers["Dropbox-API-Arg"] = json.dumps(arg if arg is not None else None)
                if style == "upload":
                    headers["Content-Type"] = "application/octet-stream"
                r = await dbx.client.post(
                    f"{dbx.CONTENT}/2/{name}",
                    headers=headers,
                    content=b"sweep" if style == "upload" else None,
                )
            seen_codes.setdefault(name, set()).add(r.status_code)
            stats["ok" if r.status_code == 200 else "409" if r.status_code == 409 else "other"] += 1
            try:
                if r.status_code in (200, 409):
                    dbx.validate(name, r)  # strict Stone decode + .tag rules for results and errors
                else:
                    assert r.status_code in (400, 401, 403, 404, 413, 422), r.status_code
            except Exception as exc:
                problems.append(
                    f"{name} [flavour {flavour}] {r.status_code}: {str(exc)[:200]} :: {r.text[:200]!r}"
                )
    always_400 = {n for n, codes in seen_codes.items() if codes <= {400, 401, 403, 404}}
    print("sweep stats", stats, "routes never reaching their handler:", sorted(always_400))
    assert not problems, "\n".join(problems[:40])
    assert stats["ok"] > 40 and stats["409"] > 80


def test_arg_generator_is_sane() -> None:
    from dropbox import files

    arg = make(files.RelocationArg_validator, "arg", 0)
    assert arg["from_path"] == "/sweep/a.txt" and arg["to_path"] == "/sweep/b.txt"
    assert re.fullmatch(r"id:.*", make(files.GetMetadataArg_validator, "arg", 0)["path"]) is None
