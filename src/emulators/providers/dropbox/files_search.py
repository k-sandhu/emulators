"""``files/search_v2`` and ``files/search/continue_v2``.

Matching follows Dropbox's term semantics closely enough for integration tests: every query term
must match (as a word prefix in the file name, or anywhere in indexed content); folders match on
name only; ``"quoted phrases"`` match as substrings. Content comes from the text index built at seed
time (document paragraphs) or from text-like uploads.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from emulators.providers.dropbox import catalog
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import (
    PATH_R_OR_ID,
    Args,
    BadInput,
    Pager,
    api_error,
    path_error,
    tag,
)
from emulators.providers.dropbox.dispatch import route
from emulators.providers.dropbox.files_core import base_from_state, collect
from emulators.providers.dropbox.fileutil import lookup


def terms_of(query: str) -> list[str]:
    try:
        parts = shlex.split(query.lower())
    except ValueError:
        parts = query.lower().split()
    return [p for p in parts if p]


def name_matches(name: str, terms: list[str]) -> bool:
    lowered = name.lower()
    for term in terms:
        if " " in term:
            if term not in lowered:
                return False
        elif re.search(r"(?<![a-z0-9])" + re.escape(term), lowered) is None:
            return False
    return True


def spans_for(name: str, terms: list[str]) -> list[dict[str, Any]]:
    lowered = name.lower()
    marks: list[tuple[int, int]] = []
    for term in terms:
        for hit in re.finditer(re.escape(term), lowered):
            marks.append((hit.start(), hit.end()))
    marks.sort()
    merged: list[list[int]] = []
    for start, end in marks:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out: list[dict[str, Any]] = []
    pos = 0
    for start, end in merged:
        if start > pos:
            out.append({"highlight_str": name[pos:start], "is_highlighted": False})
        out.append({"highlight_str": name[start:end], "is_highlighted": True})
        pos = end
    if pos < len(name):
        out.append({"highlight_str": name[pos:], "is_highlighted": False})
    return out


def run_search(c: m.Call, state: dict[str, Any]) -> list[tuple[dict[str, Any], m.Loc, str, list[str]]]:
    terms = terms_of(state["q"])
    if not terms:
        raise api_error(tag("invalid_argument", invalid_argument="query must contain at least one term"))
    base = base_from_state(c, state)
    exts = {e.lower().lstrip(".") for e in state.get("exts") or []}
    cats = set(state.get("cats") or [])
    deleted_only = state.get("status") == "deleted"
    text_index = c.ctx.store.collection(m.TEXT)
    hits: list[tuple[dict[str, Any], m.Loc, str, list[str]]] = []
    for _key, rec, loc in collect(
        c, base, recursive=True, mounted=True, deleted=deleted_only, downloadable=True
    ):
        if bool(rec.get("deleted")) != deleted_only:
            continue
        if exts and (rec["type"] == "folder" or rec["name"].rsplit(".", 1)[-1].lower() not in exts):
            continue
        if cats and catalog.category_of(rec["name"], rec["type"] == "folder") not in cats:
            continue
        by_name = name_matches(rec["name"], terms)
        by_content = False
        if rec["type"] == "file" and not state.get("fn_only"):
            doc = text_index.get(rec["id"])
            by_content = bool(doc) and all(t in doc["text"] for t in terms)
        if not (by_name or by_content):
            continue
        kind = "filename_and_content" if by_name and by_content else "filename" if by_name else "file_content"
        hits.append((rec, loc, kind, terms))
    if state.get("order") == "last_modified_time":
        hits.sort(
            key=lambda h: h[0]["revisions"][-1]["server_modified"] if h[0]["type"] == "file" else "",
            reverse=True,
        )
    else:
        rank = {"filename_and_content": 0, "filename": 1, "file_content": 2}
        hits.sort(key=lambda h: (rank[h[2]], len(h[0]["name"]), h[0]["path"]))
    return hits


def page(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    hits = run_search(c, state)
    offset, limit = state["offset"], state["max"]
    chunk = hits[offset : offset + limit]
    matches = []
    for rec, loc, kind, terms in chunk:
        item: dict[str, Any] = {
            "metadata": tag("metadata", metadata=m.metadata(c, rec, loc=loc)),
            "match_type": tag(kind),
        }
        if state.get("hl"):
            item["highlight_spans"] = spans_for(rec["name"], terms)
        matches.append(item)
    more = offset + limit < len(hits)
    out: dict[str, Any] = {"matches": matches, "has_more": more}
    if more:
        out["cursor"] = Pager.encode({**state, "offset": offset + limit})
    return out


@route("files/search_v2", scope="files.metadata.read")
def search_v2(c: m.Call) -> Any:
    a = c.args
    query = a.str("query")
    options = a.obj("options", None) or Args({}, "request body: options")
    highlights = a.bool("include_highlights")
    mf = a.obj("match_field_options", None)
    if mf is not None:
        highlights = highlights or mf.bool("include_highlights")
    status, _ = options.union("file_status", ("active", "deleted", "other"), default="active")
    order, _ = options.union("order_by", ("relevance", "last_modified_time", "other"), default="relevance")
    cats = []
    for raw in options.list("file_categories", []):
        name = raw.get(".tag") if isinstance(raw, dict) else raw
        cats.append(name)
    base = lookup(c, options.str("path", "", pattern=PATH_R_OR_ID))
    if base.rec is not None and base.rec["type"] != "folder":
        raise path_error("not_folder")
    state = {
        "v": 2,
        "a": c.actor.id,
        "q": query,
        "ns": base.ns,
        "rel": base.rel,
        "pl": base.pl,
        "pd": base.pd,
        "reach": base.reachable,
        "max": options.int("max_results", 100, lo=1, hi=1000),
        "status": status,
        "order": order,
        "fn_only": options.bool("filename_only"),
        "exts": options.list("file_extensions", []),
        "cats": cats,
        "hl": highlights,
        "offset": 0,
    }
    return page(c, state)


@route("files/search/continue_v2", scope="files.metadata.read")
def search_continue_v2(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"))
    if not state or state.get("v") != 2:
        raise BadInput("request body: cursor: invalid cursor")
    return page(c, state)
