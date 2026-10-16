"""Deprecated-but-still-called routes: ``files/search`` (v1), ``sharing/create_shared_link`` and
``get_shared_links``, ``sharing/remove_file_member`` (v1), ``files/alpha/*`` and ``openid/userinfo``."""

from __future__ import annotations

from typing import Any

from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import api_error, tag
from emulators.providers.dropbox.dispatch import ROUTES, route
from emulators.providers.dropbox.files_search import run_search
from emulators.providers.dropbox.fileutil import lookup
from emulators.providers.dropbox.sharing_files import find_explicit, locate, need_edit
from emulators.providers.dropbox.sharing_links import VISIBILITY_FOR_AUDIENCE, link_target, link_url, new_link

# -- aliases -----------------------------------------------------------------------------
ROUTES["files/alpha/get_metadata"] = ROUTES["files/get_metadata"]
ROUTES["files/alpha/upload"] = ROUTES["files/upload"]


# -- files/search v1 ---------------------------------------------------------------------
@route("files/search", scope="files.metadata.read")
def search_v1(c: m.Call) -> Any:
    a = c.args
    path = a.str("path", pattern=r"(/(.|[\r\n])*)?|id:.*|(ns:[0-9]+(/(.|[\r\n])*)?)")
    mode, _ = a.union(
        "mode", ("filename", "filename_and_content", "deleted_filename", "other"), default="filename"
    )
    base = lookup(c, path)
    state = {
        "q": a.str("query"),
        "ns": base.ns,
        "rel": base.rel,
        "pl": base.pl,
        "pd": base.pd,
        "reach": base.reachable,
        "status": "deleted" if mode == "deleted_filename" else "active",
        "fn_only": mode != "filename_and_content",
        "order": "relevance",
    }
    hits = run_search(c, state)
    start, limit = a.int("start", 0, lo=0), a.int("max_results", 100, lo=1, hi=1000)
    chunk = hits[start : start + limit]
    kinds = {"filename": "filename", "file_content": "content", "filename_and_content": "both"}
    matches = [
        {"match_type": tag(kinds[kind]), "metadata": m.metadata(c, rec, loc=loc)}
        for rec, loc, kind, _ in chunk
    ]
    more = start + limit < len(hits)
    return {"matches": matches, "more": more, "start": start + limit if more else start + len(chunk)}


# -- sharing/create_shared_link & get_shared_links ---------------------------------------
def path_link(c: m.Call, link: dict[str, Any], rec: dict[str, Any], *, tagged: bool) -> dict[str, Any]:
    d: dict[str, Any] = {".tag": "path"} if tagged else {}
    d["url"] = link_url(c, link, rec["name"])
    d["visibility"] = tag(VISIBILITY_FOR_AUDIENCE.get(link["audience"], "public"))
    if link.get("expires"):
        d["expires"] = link["expires"]
    d["path"] = m.loc_for_entry(c, rec).path_display
    return d


@route("sharing/create_shared_link", scope="sharing.write")
def create_shared_link(c: m.Call) -> Any:
    path = c.args.str("path")
    loc = lookup(c, path, allow_root=False)
    rec = loc.rec
    assert rec is not None
    for existing in c.ctx.store.collection(m.LINKS):
        if existing["entry"] == rec["id"] and existing["creator"] == c.actor.id:
            return path_link(c, existing, rec, tagged=False)
    link = new_link(c, rec, None, legacy=True)
    return path_link(c, link, rec, tagged=False)


@route("sharing/get_shared_links", scope="sharing.read")
def get_shared_links(c: m.Call) -> Any:
    path = c.args.str("path", None)
    target = None
    if path:
        target = lookup(c, path, allow_root=False).rec
    out = []
    for link in c.ctx.store.collection(m.LINKS):
        if link["creator"] != c.actor.id:
            continue
        rec = link_target(c, link)
        if rec is None or (target is not None and rec["id"] != target["id"]):
            continue
        out.append(path_link(c, link, rec, tagged=True))
    return {"links": out}


# -- sharing/remove_file_member v1 -------------------------------------------------------
@route("sharing/remove_file_member", scope="sharing.write")
def remove_file_member_v1(c: m.Call) -> Any:
    rec, _ = locate(c, c.args.str("file"))
    need_edit(c, rec)
    share, _ = find_explicit(c, rec, c.args.raw("member"))
    if share is None:
        raise api_error(tag("no_explicit_access"))
    rec["members"] = [s for s in rec["members"] if s is not share]
    return tag("success")


# -- openid -------------------------------------------------------------------------------
@route("openid/userinfo", scope="openid")
def openid_userinfo(c: m.Call) -> Any:
    scopes = c.actor.scopes
    if scopes and "openid" not in scopes:
        raise api_error({".tag": "openid_error", "openid_error": tag("incorrect_openid_scopes")})
    acct = c.actor.account
    out: dict[str, Any] = {"iss": "https://accounts.dropbox.com", "sub": acct["id"]}
    if not scopes or "profile" in scopes:
        out["given_name"] = acct["given_name"]
        out["family_name"] = acct["surname"]
    if not scopes or "email" in scopes:
        out["email"] = acct["email"]
        out["email_verified"] = acct["email_verified"]
    return out


# -- contacts ------------------------------------------------------------------------------
@route("contacts/delete_manual_contacts", scope="contacts.write", void_arg=True)
def delete_manual_contacts(c: m.Call) -> Any:
    """Manual contacts are not modelled; the call succeeds like Dropbox does for an empty list."""
    return None


@route("contacts/delete_manual_contacts_batch", scope="contacts.write")
def delete_manual_contacts_batch(c: m.Call) -> Any:
    for email in c.args.list("email_addresses"):
        if not isinstance(email, str) or "@" not in email:
            raise api_error({".tag": "contacts_not_found", "contacts_not_found": [str(email)]})
    return None
