"""``file_requests/*``: request files from anyone into a folder.

The create/get/list/update/delete lifecycle is served like the real API. Uploads through the public
request page are *simulated* with the ``simulate_file_request_upload`` action (the page itself is a
stub), which stores the file in the destination and bumps ``file_count``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import Args, Pager, api_error, parse_ts, tag, ts
from emulators.providers.dropbox.dispatch import route
from emulators.providers.dropbox.ops import Ops

REQUESTS = "file_requests"
GRACE = {
    "one_day": timedelta(days=1),
    "two_days": timedelta(days=2),
    "seven_days": timedelta(days=7),
    "thirty_days": timedelta(days=30),
}


def is_open(ctx: ProviderContext, req: dict[str, Any]) -> bool:
    if not req["open"]:
        return False
    deadline = req.get("deadline")
    if not deadline:
        return True
    end = parse_ts(deadline["deadline"])
    late = deadline.get("allow_late_uploads")
    if late == "always":
        return True
    if late in GRACE:
        end += GRACE[late]
    return m.now(ctx) <= end


def render(c: m.Call, req: dict[str, Any]) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": req["id"],
        "url": url_for_host(c.request, c.ctx, "www.dropbox.com", f"/request/{req['id']}"),
        "title": req["title"],
        "destination": req.get("destination"),
        "created": req["created"],
        "is_open": is_open(c.ctx, req),
        "file_count": req["file_count"],
    }
    if req.get("deadline"):
        deadline: dict[str, Any] = {"deadline": req["deadline"]["deadline"]}
        if req["deadline"].get("allow_late_uploads"):
            deadline["allow_late_uploads"] = tag(req["deadline"]["allow_late_uploads"])
        d["deadline"] = deadline
    if req.get("description"):
        d["description"] = req["description"]
    return d


def parse_deadline(a: Args, key: str = "deadline") -> dict[str, Any] | None:
    if not a.has(key):
        return None
    dl = a.obj(key)
    assert dl is not None
    when = dl.ts("deadline")
    late = dl.union(
        "allow_late_uploads",
        ("one_day", "two_days", "seven_days", "thirty_days", "always", "other"),
        default=None,
    )[0]
    assert when is not None
    return {"deadline": ts(when), "allow_late_uploads": late}


def mine(c: m.Call) -> list[dict[str, Any]]:
    out = [r for r in c.ctx.store.collection(REQUESTS) if r["creator"] == c.actor.id]
    out.sort(key=lambda r: (r["created"], r["id"]), reverse=True)
    return out


def find(c: m.Call, request_id: str) -> dict[str, Any]:
    req = c.ctx.store.collection(REQUESTS).get(request_id)
    if req is None or req["creator"] != c.actor.id:
        raise api_error(tag("not_found"))
    return req


def destination_folder(c: m.Call, path: str, *, needs_write: bool = True) -> tuple[dict[str, Any], str]:
    loc = m.resolve(c, path, allow_root=False)
    if loc is None or loc.rec is None:
        raise api_error(tag("not_found"))
    if loc.rec["type"] != "folder":
        raise api_error(tag("not_a_folder"))
    if needs_write and not m.is_writer(
        m.entry_role(c, loc.rec) or m.ns_role(c.actor, c.tree.namespace(loc.ns))
    ):
        raise api_error(tag("no_write_permission"))
    return loc.rec, loc.path_display


@route("file_requests/create", scope="file_requests.write")
def create(c: m.Call) -> Any:
    a = c.args
    title = a.str("title")
    if not title.strip():
        raise api_error(tag("validation_error"))
    folder, display = destination_folder(c, a.str("destination", pattern=r"/(.|[\r\n])*"))
    deadline = parse_deadline(a)
    if deadline and parse_ts(deadline["deadline"]) <= c.now():
        raise api_error(tag("validation_error"))
    req = {
        "id": m.new_token(c.ctx, "filereq", 10).replace("-", "x").replace("_", "y"),
        "creator": c.actor.id,
        "title": title,
        "destination": display,
        "dest_id": folder["id"],
        "created": ts(c.now()),
        "deadline": deadline,
        "open": a.bool("open", True),
        "description": a.str("description", None),
        "file_count": 0,
    }
    c.ctx.store.collection(REQUESTS).put(req)
    return render(c, req)


@route("file_requests/get", scope="file_requests.read")
def get(c: m.Call) -> Any:
    return render(c, find(c, c.args.str("id")))


@route("file_requests/list", scope="file_requests.read", void_arg=True)
def list_v1(c: m.Call) -> Any:
    return {"file_requests": [render(c, r) for r in mine(c)]}


def page(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    items = mine(c)
    offset, limit = state["offset"], state["limit"]
    more = offset + limit < len(items)
    return {
        "file_requests": [render(c, r) for r in items[offset : offset + limit]],
        "cursor": Pager.encode({**state, "offset": offset + limit}, c),
        "has_more": more,
    }


@route("file_requests/list_v2", scope="file_requests.read")
def list_v2(c: m.Call) -> Any:
    return page(c, {"t": "freq", "offset": 0, "limit": c.args.int("limit", 1000, lo=1, hi=1000)})


@route("file_requests/list/continue", scope="file_requests.read")
def list_continue(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"), c)
    if not state or state.get("t") != "freq":
        raise api_error(tag("invalid_cursor"))
    return page(c, state)


@route("file_requests/count", scope="file_requests.read", void_arg=True)
def count(c: m.Call) -> Any:
    return {"file_request_count": len(mine(c))}


@route("file_requests/update", scope="file_requests.write")
def update(c: m.Call) -> Any:
    a = c.args
    req = find(c, a.str("id"))
    if a.has("title"):
        if not a.str("title").strip():
            raise api_error(tag("validation_error"))
        req["title"] = a.str("title")
    if a.has("destination"):
        folder, display = destination_folder(c, a.str("destination"))
        req["destination"], req["dest_id"] = display, folder["id"]
    if a.has("open"):
        req["open"] = a.bool("open")
    if a.has("description"):
        req["description"] = a.str("description")
    if a.has("deadline"):
        mode, payload = a.union("deadline", ("no_update", "update", "other"))
        if mode == "update":
            if payload:
                dl = parse_deadline(Args({"deadline": payload}))
                if dl and parse_ts(dl["deadline"]) <= c.now():
                    raise api_error(tag("validation_error"))
                req["deadline"] = dl
            else:
                req["deadline"] = None
    return render(c, req)


@route("file_requests/delete", scope="file_requests.write")
def delete(c: m.Call) -> Any:
    reqs = [find(c, rid) for rid in c.args.list("ids")]
    if any(is_open(c.ctx, r) for r in reqs):
        raise api_error(tag("file_request_open"))
    for req in reqs:
        c.ctx.store.collection(REQUESTS).delete(req["id"])
    return {"file_requests": [render(c, r) for r in reqs]}


@route("file_requests/delete_all_closed", scope="file_requests.write", void_arg=True)
def delete_all_closed(c: m.Call) -> Any:
    closed = [r for r in mine(c) if not is_open(c.ctx, r)]
    for req in closed:
        c.ctx.store.collection(REQUESTS).delete(req["id"])
    return {"file_requests": [render(c, r) for r in closed]}


async def simulate_upload(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Pretend an outsider used a file request page: ``{"id": "<request id>", "name": "scan.pdf",
    "content": "text", "uploader": "Someone"}``. Fails when the request is closed."""
    req = ctx.store.collection(REQUESTS).get(payload.get("id"))
    if req is None:
        raise EmulatorError(404, {"error": f"unknown file request '{payload.get('id')}'"})
    if not is_open(ctx, req):
        raise EmulatorError(409, {"error": "file request is closed"})
    folder = ctx.store.collection(m.ENTRIES).get(req["dest_id"])
    if folder is None or folder.get("deleted"):
        raise EmulatorError(409, {"error": "destination folder no longer exists"})
    tree = m.Tree(ctx)
    touched: set[str] = set()
    ops = Ops(ctx, tree, touched)
    parent_ns, parent_rel = (
        (folder["mount_ns"], "") if folder.get("mount_ns") else (folder["ns"], folder["path"])
    )
    taken = {r["name"].lower() for r in tree.children(parent_ns, parent_rel)}
    name = payload["name"]
    if name.lower() in taken:
        from emulators.providers.dropbox.common import autorename

        name = autorename(name, taken)
    content = payload.get("content", "")
    data = content.encode() if isinstance(content, str) else bytes(content)
    rec = ops.create_file(parent_ns, parent_rel, name, data, owner=req["creator"], modified_by=req["creator"])
    req["file_count"] += 1
    from emulators.providers.dropbox import webhooks

    await webhooks.notify_accounts(ctx, webhooks.accounts_with_access(ctx, touched))
    return {"id": rec["id"], "name": rec["name"], "file_count": req["file_count"]}
