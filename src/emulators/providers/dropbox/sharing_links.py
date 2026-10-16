"""Shared links: ``sharing/create_shared_link_with_settings``, listing, metadata, file download,
settings changes and revocation, plus the helpers the public ``www.dropbox.com`` pages reuse."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from emulators.core.errors import EmulatorError
from emulators.core.provider import external_base, url_for_host
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import (
    READ_PATH,
    Args,
    Pager,
    api_error,
    parse_ts,
    path_error,
    tag,
    ts,
)
from emulators.providers.dropbox.dispatch import Download, ranged, route
from emulators.providers.dropbox.fileutil import lookup
from emulators.providers.dropbox.team_log import audit

LINK_HOSTS = ("www.dropbox.com", "dropbox.com", "dl.dropbox.com")
AUDIENCES = ("public", "team", "no_one", "password", "members", "public_logged_in_only", "other")
VISIBILITY_FOR_AUDIENCE = {
    "public": "public",
    "team": "team_only",
    "password": "password",
    "no_one": "no_one",
    "members": "shared_folder_only",
}
AUDIENCE_FOR_VISIBILITY = {"public": "public", "team_only": "team", "password": "password"}


# ---------------------------------------------------------------------------------------
# URL <-> link
# ---------------------------------------------------------------------------------------
def link_url(c: m.Call, link: dict[str, Any], name: str) -> str:
    key = quote(link["rlkey"])
    if link["kind"] == "folder":
        return url_for_host(
            c.request, c.ctx, "www.dropbox.com", f"/scl/fo/{link['id']}/{link['hash']}?rlkey={key}&dl=0"
        )
    if link.get("legacy"):
        return url_for_host(c.request, c.ctx, "www.dropbox.com", f"/s/{link['id']}/{quote(name)}?dl=0")
    return url_for_host(
        c.request, c.ctx, "www.dropbox.com", f"/scl/fi/{link['id']}/{quote(name)}?rlkey={key}&dl=0"
    )


def parse_link(url: str, base_path: str = "") -> tuple[str, str] | None:
    """``(kind, slug)`` for ``/scl/fi|fo/<slug>/..``, ``/s/<slug>/..`` and ``/sh/<slug>/..``."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    path = parsed.path
    if base_path and path.startswith(base_path.rstrip("/") + "/"):
        path = path[len(base_path.rstrip("/")) :]
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "scl" and parts[1] in ("fi", "fo"):
        return ("file" if parts[1] == "fi" else "folder"), parts[2]
    if len(parts) >= 2 and parts[0] in ("s", "sh"):
        return ("file" if parts[0] == "s" else "folder"), parts[1]
    return None


def find_link(c: m.Call, url: str) -> dict[str, Any] | None:
    origin = urlparse(external_base(c.request))
    if urlparse(url).hostname not in (*LINK_HOSTS, origin.hostname):
        return None
    parsed = parse_link(url, origin.path)
    if parsed is None:
        return None
    link = c.ctx.store.collection(m.LINKS).get(parsed[1])
    if link is None or link["kind"] != parsed[0]:
        return None
    if not link.get("legacy") and parse_qs(urlparse(url).query).get("rlkey") != [link["rlkey"]]:
        return None
    if link.get("expires") and link["expires"] <= ts(c.now()):
        return None
    return link


# ---------------------------------------------------------------------------------------
# Wire metadata
# ---------------------------------------------------------------------------------------
def team_member_info(c: m.Call, link: dict[str, Any]) -> dict[str, Any] | None:
    creator = m.account(c.ctx, link["creator"])
    if creator is None or not creator.get("is_team"):
        return None
    team = m.team_info(c.ctx)
    return {
        "team_info": {"id": team["id"], "name": team["name"]},
        "display_name": creator["display_name"],
        "member_id": creator["team_member_id"],
    }


def link_permissions(c: m.Call, link: dict[str, Any]) -> dict[str, Any]:
    vis = VISIBILITY_FOR_AUDIENCE.get(link["audience"], "public")
    requested = {"public": "public", "team": "team_only", "password": "password"}.get(
        link["audience"], "public"
    )
    creator_is_actor = link["creator"] == c.actor.id or c.actor.admin
    team_only = (
        c.actor.account.get("is_team")
        and m.team_info(c.ctx)["policies"]["shared_link_create_policy"] == "team_only"
    )
    return {
        "resolved_visibility": tag(vis),
        "requested_visibility": tag(requested),
        "can_revoke": creator_is_actor,
        "visibility_policies": [
            {"policy": tag("public"), "resolved_policy": tag("public"), "allowed": not team_only},
            {"policy": tag("team_only"), "resolved_policy": tag("team_only"), "allowed": True},
            {"policy": tag("password"), "resolved_policy": tag("password"), "allowed": not team_only},
        ],
        "can_set_expiry": creator_is_actor,
        "can_remove_expiry": creator_is_actor and bool(link.get("expires")),
        "allow_download": bool(link.get("allow_download", True)),
        "can_allow_download": creator_is_actor,
        "can_disallow_download": creator_is_actor,
        "allow_comments": True,
        "team_restricts_comments": False,
        "audience_options": [
            {"audience": tag("public"), "allowed": not team_only},
            {"audience": tag("team"), "allowed": True},
            {"audience": tag("no_one"), "allowed": True},
            {"audience": tag("password"), "allowed": not team_only},
        ],
        "can_set_password": creator_is_actor,
        "can_remove_password": creator_is_actor and bool(link.get("password")),
        "require_password": bool(link.get("password")),
        "can_use_extended_sharing_controls": False,
        "effective_audience": tag(link["audience"]),
        "link_access_level": tag(link["access"]),
    }


def link_target(c: m.Call, link: dict[str, Any]) -> dict[str, Any] | None:
    rec = c.tree.by_id(link["entry"])
    return None if rec is None or rec.get("deleted") else rec


def link_metadata(
    c: m.Call, link: dict[str, Any], rec: dict[str, Any], *, subpath: dict[str, Any] | None = None
) -> dict[str, Any]:
    target = subpath or rec
    d: dict[str, Any] = {".tag": "file" if target["type"] == "file" else "folder"}
    d["url"] = link_url(c, link, rec["name"])
    d["id"] = target["id"]
    d["name"] = target["name"]
    if link.get("expires"):
        d["expires"] = link["expires"]
    if link["creator"] == c.actor.id or c.actor.admin:
        loc = m.loc_for_entry(c, target)
        if loc.reachable:
            d["path_lower"] = loc.path_lower
    d["link_permissions"] = link_permissions(c, link)
    info = team_member_info(c, link)
    if info:
        d["team_member_info"] = info
        team = m.team_info(c.ctx)
        d["content_owner_team_info"] = {"id": team["id"], "name": team["name"]}
    if target["type"] == "file":
        latest = target["revisions"][-1]
        d.update(
            client_modified=latest["client_modified"],
            server_modified=latest["server_modified"],
            rev=latest["rev"],
            size=latest["size"],
        )
    return d


def can_open(c: m.Call, link: dict[str, Any], password: str | None) -> bool:
    """Whether the caller may use the link under its audience / password rules."""
    actor = c.actor
    if link["creator"] == actor.id or actor.admin:
        return True
    audience = link["audience"]
    if audience == "no_one":
        return False
    if audience in ("team", "members"):
        if audience == "team" and not actor.account.get("is_team"):
            return False
        if audience == "members":
            rec = link_target(c, link)
            if rec is None or m.entry_role(c, rec) is None:
                return False
    return not (link.get("password") and password != link["password"])


def require_link(
    c: m.Call, url: str, password: str | None, *, error: Any = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    link = find_link(c, url)
    if link is None:
        raise api_error(tag("shared_link_not_found"))
    rec = link_target(c, link)
    if rec is None:
        raise api_error(tag("shared_link_not_found"))
    if not can_open(c, link, password):
        raise api_error(tag("shared_link_access_denied"))
    return link, rec


def subpath_entry(c: m.Call, rec: dict[str, Any], path: str) -> dict[str, Any]:
    """The item at ``path`` below a folder link's root."""
    if rec["type"] != "folder":
        raise api_error(tag("shared_link_not_found"))
    ns, rel = (rec["mount_ns"], "") if rec.get("mount_ns") else (rec["ns"], rec["path"])
    for seg in m.split_path(path):
        found = c.tree.live.get((ns, rel + "/" + seg.lower()))
        if found is None:
            raise path_error("not_found")
        if found.get("mount_ns"):
            ns, rel = found["mount_ns"], ""
        else:
            rel = found["path"]
        rec = found
    return rec


# ---------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------
def settings_error(kind: str = "invalid_settings") -> EmulatorError:
    return api_error({".tag": "settings_error", "settings_error": {".tag": kind}})


def apply_settings(
    c: m.Call, link: dict[str, Any], rec: dict[str, Any], s: Args | None, *, creating: bool
) -> None:
    team_policy = (
        m.team_info(c.ctx)["policies"]["shared_link_create_policy"]
        if c.actor.account.get("is_team")
        else "default_public"
    )
    audience = link.get("audience")
    if s is not None:
        vis = s.union("requested_visibility", ("public", "team_only", "password"), default=None)[0]
        aud = s.union("audience", AUDIENCES, default=None)[0]
        if aud is not None:
            audience = aud
        elif vis is not None:
            audience = AUDIENCE_FOR_VISIBILITY[vis]
        password = s.str("link_password", None)
        if s.bool("require_password") and not password and not link.get("password"):
            raise settings_error()
        if vis == "password" and not password and not link.get("password"):
            raise settings_error()
        if password:
            link["password"] = password
            audience = "password" if aud is None and vis in (None, "password") else audience
        elif s.has("require_password") and not s.bool("require_password"):
            link["password"] = None
            if audience == "password":
                audience = "public"
        expires = s.ts("expires")
        if expires is not None:
            if expires <= c.now():
                raise settings_error()
            link["expires"] = ts(expires)
        access, _ = s.union("access", ("viewer", "editor", "max", "default", "other"), default=None)
        if access is not None:
            role = m.entry_role(c, rec)
            can_edit = m.is_writer(role)
            if access == "editor" and not can_edit:
                raise settings_error("not_authorized")
            link["access"] = "editor" if access in ("editor", "max") and can_edit else "viewer"
        if s.has("allow_download"):
            link["allow_download"] = s.bool("allow_download", True)
    if audience is None:
        audience = "team" if team_policy in ("default_team_only", "team_only") else "public"
    if team_policy == "team_only" and audience in ("public", "password") and c.actor.account.get("is_team"):
        raise settings_error("not_authorized")
    ns = c.tree.namespace(rec["ns"])
    if ns and ns.get("policy", {}).get("shared_link_policy") == "team" and audience == "public":
        raise settings_error("not_authorized")
    link["audience"] = audience


def new_link(
    c: m.Call, rec: dict[str, Any], settings: Args | None, *, legacy: bool = False
) -> dict[str, Any]:
    """Create, store and audit a shared link for ``rec`` (the caller checked for duplicates)."""
    store = c.ctx.store.collection(m.LINKS)
    kind = "file" if rec["type"] == "file" else "folder"
    slug = m.new_token(c.ctx, "linkslug", 25).lower().replace("-", "x").replace("_", "y")
    link = {
        "id": slug,
        "kind": kind,
        "entry": rec["id"],
        "creator": c.actor.id,
        "audience": None,
        "access": "viewer",
        "allow_download": True,
        "password": None,
        "expires": None,
        "created": ts(c.now()),
        "rlkey": m.new_token(c.ctx, "rlkey", 25).lower().replace("-", "x").replace("_", "y"),
        "hash": m.new_token(c.ctx, "linkhash", 20).replace("-", "x").replace("_", "y"),
        "legacy": legacy,
    }
    apply_settings(c, link, rec, settings, creating=True)
    store.put(link)
    audit(c, "shared_link_create", rec, access=link["access"])
    return link


@route("sharing/create_shared_link_with_settings", scope="sharing.write")
def create_shared_link_with_settings(c: m.Call) -> Any:
    a = c.args
    path = a.str("path", pattern=READ_PATH)
    loc = lookup(c, path, allow_root=False)
    rec = loc.rec
    assert rec is not None
    if m.entry_role(c, rec) is None and not rec.get("mount_ns"):
        raise api_error(tag("access_denied"))
    store = c.ctx.store.collection(m.LINKS)
    for existing in store:
        if existing["entry"] == rec["id"] and existing["creator"] == c.actor.id:
            raise api_error(
                {
                    ".tag": "shared_link_already_exists",
                    "shared_link_already_exists": tag("metadata", metadata=link_metadata(c, existing, rec)),
                }
            )
    link = new_link(c, rec, a.obj("settings", None))
    return link_metadata(c, link, rec)


@route("sharing/list_shared_links", scope="sharing.read")
def list_shared_links(c: m.Call) -> Any:
    a = c.args
    cursor = a.str("cursor", None)
    state = {"offset": 0}
    if cursor is not None:
        state = Pager.decode(cursor, c) or {}
        if state.get("t") != "links":
            raise api_error(tag("reset"))
    path = state.get("path") if cursor else a.str("path", None)
    direct = state.get("direct", False) if cursor else a.bool("direct_only", False)
    target = None
    if path:
        loc = lookup(c, path, allow_root=False)
        target = loc.rec
    page_size = int(c.ctx.setting("SHARED_LINKS_PAGE", "100") or 100)
    mine = [
        lk
        for lk in c.ctx.store.collection(m.LINKS)
        if lk["creator"] == c.actor.id and (not lk.get("expires") or lk["expires"] > ts(c.now()))
    ]
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for lk in mine:
        rec = link_target(c, lk)
        if rec is None:
            continue
        if target is not None:
            if rec["id"] == target["id"]:
                pass
            elif direct:
                continue
            else:
                covers = (
                    rec["type"] == "folder"
                    and rec["ns"] == target["ns"]
                    and target["path"].startswith(rec["path"] + "/")
                )
                if not covers:
                    continue
        out.append((lk, rec))
    out.sort(key=lambda item: (item[0]["created"], item[0]["id"]))
    offset = state.get("offset", 0)
    chunk = out[offset : offset + page_size]
    more = offset + page_size < len(out)
    result: dict[str, Any] = {"links": [link_metadata(c, lk, rec) for lk, rec in chunk], "has_more": more}
    if more:
        result["cursor"] = Pager.encode(
            {"t": "links", "offset": offset + page_size, "path": path, "direct": direct}, c
        )
    return result


@route("sharing/get_shared_link_metadata", auth="app,user", scope="sharing.read")
def get_shared_link_metadata(c: m.Call) -> Any:
    a = c.args
    link, rec = require_link(c, a.str("url"), a.str("link_password", None))
    sub = None
    if a.has("path") and a.str("path") not in ("", "/"):
        sub = subpath_entry(c, rec, a.str("path"))
    return link_metadata(c, link, rec, subpath=sub)


@route("sharing/get_shared_link_file", style="download", auth="app,user", scope="sharing.read")
def get_shared_link_file(c: m.Call) -> Any:
    a = c.args
    link, rec = require_link(c, a.str("url"), a.str("link_password", None))
    if not link.get("allow_download", True):
        raise api_error(tag("shared_link_access_denied"))
    target = rec
    if a.has("path") and a.str("path") not in ("", "/"):
        target = subpath_entry(c, rec, a.str("path"))
    if target["type"] == "folder":
        raise api_error(tag("shared_link_is_directory"))
    latest = target["revisions"][-1]
    data = c.ctx.store.blobs.get(latest["blob"])
    status, body, headers = ranged(c.request, data, {})
    return Download(
        link_metadata(c, link, rec, subpath=target if target is not rec else None),
        body,
        "application/octet-stream",
        headers,
        status,
    )


@route("sharing/modify_shared_link_settings", scope="sharing.write")
def modify_shared_link_settings(c: m.Call) -> Any:
    a = c.args
    link = find_link(c, a.str("url"))
    if link is None:
        raise api_error(tag("shared_link_not_found"))
    if link["creator"] != c.actor.id and not c.actor.admin:
        raise api_error(tag("shared_link_access_denied"))
    rec = link_target(c, link)
    if rec is None:
        raise api_error(tag("shared_link_not_found"))
    settings = a.obj("settings")
    candidate = dict(link)
    if a.bool("remove_expiration"):
        candidate["expires"] = None
    apply_settings(c, candidate, rec, settings, creating=False)
    link.update(candidate)
    return link_metadata(c, link, rec)


@route("sharing/revoke_shared_link", scope="sharing.write")
def revoke_shared_link(c: m.Call) -> Any:
    url = c.args.str("url")
    if parse_link(url, urlparse(external_base(c.request)).path) is None:
        raise api_error(tag("shared_link_malformed"))
    link = find_link(c, url)
    if link is None:
        raise api_error(tag("shared_link_not_found"))
    if link["creator"] != c.actor.id and not c.actor.admin:
        raise api_error(tag("shared_link_access_denied"))
    c.ctx.store.collection(m.LINKS).delete(link["id"])
    return None


# ---------------------------------------------------------------------------------------
# files/list_folder with shared_link, thumbnails for links
# ---------------------------------------------------------------------------------------
def list_link_folder(c: m.Call, a: Args, flags: dict[str, Any]) -> Any:
    sl = a.obj("shared_link")
    assert sl is not None
    if flags["rec"]:
        raise EmulatorError(
            400,
            'Error in call to API function "files/list_folder": recursive listing is not supported for shared links',
            media_type="text/plain",
        )
    try:
        link, rec = require_link(c, sl.str("url"), sl.str("password", None))
    except EmulatorError as exc:  # ListFolderError only knows path/<lookup error>
        denied = "access_denied" in str(exc.body)
        raise path_error("restricted_content" if denied else "not_found") from None
    base = rec
    path = a.str("path", "")
    if path not in ("", "/"):
        base = subpath_entry(c, rec, path)
    if base["type"] != "folder":
        raise path_error("not_folder")
    ns, rel = (base["mount_ns"], "") if base.get("mount_ns") else (base["ns"], base["path"])
    state = {
        "v": 3,
        "link": link["id"],
        "url": sl.str("url"),
        "password": sl.str("password", None),
        "ns": ns,
        "rel": rel,
        "limit": flags["lim"],
        "after": None,
        "dl": flags["dl"],
    }
    return list_link_page(c, state)


def list_link_page(c: m.Call, state: dict[str, Any]) -> Any:
    """Recheck link access on each page; continuations retain the original listing options."""
    try:
        require_link(c, state["url"], state["password"])
    except EmulatorError:
        raise path_error("not_found") from None
    entries = []
    for child in c.tree.children(state["ns"], state["rel"]):
        if child.get("no_inherit") or (state["after"] is not None and child["path"] <= state["after"]):
            continue
        if not state["dl"] and child["type"] == "file" and not child.get("downloadable", True):
            continue
        if child["type"] == "folder":
            entries.append({".tag": "folder", "name": child["name"], "id": child["id"]})
        else:
            latest = child["revisions"][-1]
            entries.append(
                {
                    ".tag": "file",
                    "name": child["name"],
                    "id": child["id"],
                    "client_modified": latest["client_modified"],
                    "server_modified": latest["server_modified"],
                    "rev": latest["rev"],
                    "size": latest["size"],
                    "is_downloadable": child.get("downloadable", True),
                    "content_hash": latest["content_hash"],
                }
            )
    limit = state["limit"]
    page = entries[:limit]
    more = len(entries) > limit
    if page:
        last = c.tree.by_id(page[-1]["id"])
        state = {**state, "after": last["path"]}
    return {"entries": page, "cursor": Pager.encode(state, c), "has_more": more}


def thumbnail_for_link(c: m.Call, payload: Any) -> Download:
    from emulators.providers.dropbox.files_content import THUMB_SIZES, make_thumbnail

    info = Args(payload if isinstance(payload, dict) else {}, "request body: resource")
    try:
        link, rec = require_link(c, info.str("url"), info.str("password", None))
    except EmulatorError as exc:  # ThumbnailV2Error speaks not_found / access_denied for links
        kind = "access_denied" if "access_denied" in str(exc.body) else "not_found"
        raise api_error(tag(kind)) from None
    target = rec
    if info.has("path"):
        target = subpath_entry(c, rec, info.str("path"))
    if target["type"] != "file":
        raise api_error(tag("unsupported_image"))
    size_tag, _ = c.args.union("size", tuple(THUMB_SIZES), default="w64h64")
    fmt, _ = c.args.union("format", ("jpeg", "png", "webp"), default="jpeg")
    data, ctype = make_thumbnail(target, THUMB_SIZES[size_tag], fmt)
    status, body, headers = ranged(c.request, data, {})
    result = {
        "link_metadata": {
            "url": link_url(c, link, rec["name"]),
            "id": target["id"],
            "rev": target["revisions"][-1]["rev"],
        }
    }
    return Download(result, body, ctype, headers, status)


__all__ = ["parse_ts"]
