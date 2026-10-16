"""``file_properties/*``: property templates (per user or per team) and the property groups
attached to files, including property search and ``include_property_groups`` on file metadata."""

from __future__ import annotations

from typing import Any

from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import (
    PATH_ONLY,
    Args,
    BadInput,
    Pager,
    api_error,
    b64url,
    digest,
    path_error,
    tag,
    union_tag,
)
from emulators.providers.dropbox.dispatch import route
from emulators.providers.dropbox.fileutil import lookup

TEMPLATES = "prop_templates"
MAX_VALUE = 1024
MAX_FIELDS = 32
MAX_TEMPLATES = 100
PATH_PATTERN = rf"{PATH_ONLY}|id:.*|(ns:[0-9]+(/(.|[\r\n])*)?)"


def template_scope(c: m.Call, team: bool) -> str:
    return "team" if team else c.actor.id


def visible_templates(c: m.Call) -> list[dict[str, Any]]:
    mine = {c.actor.id, "team" if c.actor.account.get("is_team") else None}
    return [t for t in c.ctx.store.collection(TEMPLATES) if t["owner"] in mine]


def find_template(c: m.Call, template_id: str, *, writable_team: bool | None = None) -> dict[str, Any]:
    template = c.ctx.store.collection(TEMPLATES).get(template_id)
    mine = {c.actor.id, "team" if c.actor.account.get("is_team") else None}
    if template is None or template["owner"] not in mine:
        raise api_error(tag("template_not_found", template_not_found=template_id))
    if writable_team is not None and (template["owner"] == "team") != writable_team:
        raise api_error(tag("template_not_found", template_not_found=template_id))
    return template


def parse_fields(raw: list[Any]) -> list[dict[str, Any]]:
    fields = []
    for item in raw:
        f = Args(item, "request body: fields")
        ftype, _ = f.union("type", ("string", "other"), default="string")
        fields.append({"name": f.str("name"), "description": f.str("description", ""), "type": tag(ftype)})
    return fields


def validate_fields(fields: list[dict[str, Any]]) -> None:
    names = [f["name"] for f in fields]
    if len(set(names)) != len(names):
        raise api_error(tag("conflicting_property_names"))
    if len(fields) > MAX_FIELDS:
        raise api_error(tag("too_many_properties"))
    if any(len(f["name"]) > 256 or len(f["description"]) > 1024 for f in fields):
        raise api_error(tag("template_attribute_too_large"))


def add_template(c: m.Call, team: bool) -> Any:
    a = c.args
    fields = parse_fields(a.list("fields"))
    validate_fields(fields)
    store = c.ctx.store.collection(TEMPLATES)
    if sum(1 for t in store if t["owner"] == template_scope(c, team)) >= MAX_TEMPLATES:
        raise api_error(tag("too_many_templates"))
    tid = "ptid:" + b64url(digest("dbx-ptid", c.ctx.world.seed, m.counter(c.ctx, "ptid")))[:22]
    store.put(
        {
            "id": tid,
            "owner": template_scope(c, team),
            "name": a.str("name"),
            "description": a.str("description", ""),
            "fields": fields,
        }
    )
    return {"template_id": tid}


def get_template(c: m.Call, team: bool) -> Any:
    t = find_template(c, c.args.str("template_id"), writable_team=True if team else None)
    return {"name": t["name"], "description": t["description"], "fields": t["fields"]}


def list_templates(c: m.Call, team: bool) -> Any:
    """``*_for_user`` lists the caller's own templates plus the team's; ``*_for_team`` only the team's."""
    if team:
        return {"template_ids": [t["id"] for t in c.ctx.store.collection(TEMPLATES) if t["owner"] == "team"]}
    return {"template_ids": [t["id"] for t in visible_templates(c)]}


def remove_template(c: m.Call, team: bool) -> Any:
    t = find_template(c, c.args.str("template_id"), writable_team=team)
    c.ctx.store.collection(TEMPLATES).delete(t["id"])
    for rec in c.ctx.store.collection(m.ENTRIES):
        rec.get("props", {}).pop(t["id"], None)
    return None


def update_template(c: m.Call, team: bool) -> Any:
    a = c.args
    t = find_template(c, a.str("template_id"), writable_team=team)
    if a.has("name"):
        t["name"] = a.str("name")
    if a.has("description"):
        t["description"] = a.str("description")
    if a.has("add_fields"):
        fields = t["fields"] + parse_fields(a.list("add_fields"))
        validate_fields(fields)
        t["fields"] = fields
    return {"template_id": t["id"]}


for _scope, _team, _auth in (("user", False, "user"), ("team", True, "team")):
    for _suffix, _fn in (
        ("add", add_template),
        ("get", get_template),
        ("list", list_templates),
        ("remove", remove_template),
        ("update", update_template),
    ):
        _name = f"file_properties/templates/{_suffix}_for_{_scope}"
        _void = _suffix == "list"

        def _make(fn: Any, team: bool) -> Any:
            def handler(c: m.Call) -> Any:
                return fn(c, team)

            return handler

        route(
            _name,
            auth=_auth,
            scope="files.metadata.write" if _suffix in ("add", "remove", "update") else "files.metadata.read",
            void_arg=_void,
        )(_make(_fn, _team))


# ---------------------------------------------------------------------------------------
# Property groups on files
# ---------------------------------------------------------------------------------------
def group_obj(template_id: str, values: dict[str, str]) -> dict[str, Any]:
    return {"template_id": template_id, "fields": [{"name": k, "value": v} for k, v in values.items()]}


def groups_of(rec: dict[str, Any], template_ids: list[str] | None = None) -> list[dict[str, Any]]:
    props = rec.get("props", {})
    return [
        group_obj(tid, vals) for tid, vals in props.items() if template_ids is None or tid in template_ids
    ]


def parse_groups(c: m.Call, raw: list[Any]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for item in raw:
        g = Args(item, "request body: property_groups")
        tid = g.str("template_id")
        template = find_template(c, tid)
        if tid in out:
            raise api_error(tag("duplicate_property_groups"))
        values: dict[str, str] = {}
        names = {f["name"] for f in template["fields"]}
        for field in g.list("fields"):
            f = Args(field, "request body: fields")
            if f.str("name") not in names:
                raise api_error(tag("does_not_fit_template"))
            value = f.str("value")
            if len(value.encode()) > MAX_VALUE:
                raise api_error(tag("property_field_too_large"))
            values[f.str("name")] = value
        out[tid] = values
    return out


def target_file(c: m.Call, path: str) -> dict[str, Any]:
    loc = lookup(c, path, allow_root=False)
    rec = loc.rec
    assert rec is not None
    if rec["type"] != "file":
        raise api_error(tag("unsupported_folder"))
    if not m.is_writer(m.entry_role(c, rec)):
        raise path_error("not_found")
    return rec


def prop_filter_of(a: Args) -> list[str] | None:
    """``include_property_groups`` (TemplateFilterBase) -> list of template ids to include."""
    if not a.has("include_property_groups"):
        return None
    name, payload = union_tag(a.raw("include_property_groups"))
    if name == "filter_some" and isinstance(payload, list):
        return list(payload)
    return None


@route("file_properties/properties/add", scope="files.metadata.write")
def properties_add(c: m.Call) -> Any:
    a = c.args
    rec = target_file(c, a.str("path", pattern=PATH_PATTERN))
    groups = parse_groups(c, a.list("property_groups"))
    props = rec.setdefault("props", {})
    if any(tid in props for tid in groups):
        raise api_error(tag("property_group_already_exists"))
    props.update(groups)
    return None


@route("file_properties/properties/overwrite", scope="files.metadata.write")
def properties_overwrite(c: m.Call) -> Any:
    a = c.args
    rec = target_file(c, a.str("path", pattern=PATH_PATTERN))
    rec.setdefault("props", {}).update(parse_groups(c, a.list("property_groups")))
    return None


@route("file_properties/properties/update", scope="files.metadata.write")
def properties_update(c: m.Call) -> Any:
    a = c.args
    rec = target_file(c, a.str("path", pattern=PATH_PATTERN))
    props = rec.setdefault("props", {})
    updates = a.list("update_property_groups")
    for item in updates:
        u = Args(item, "request body: update_property_groups")
        tid = u.str("template_id")
        template = find_template(c, tid)
        if tid not in props:
            raise api_error(
                {".tag": "property_group_lookup", "property_group_lookup": tag("property_group_not_found")}
            )
        names = {f["name"] for f in template["fields"]}
        for field in u.list("add_or_update_fields", []):
            f = Args(field, "request body: add_or_update_fields")
            if f.str("name") not in names:
                raise api_error(tag("does_not_fit_template"))
            if len(f.str("value").encode()) > MAX_VALUE:
                raise api_error(tag("property_field_too_large"))
            props[tid][f.str("name")] = f.str("value")
        for name in u.list("remove_fields", []):
            props[tid].pop(name, None)
    return None


@route("file_properties/properties/remove", scope="files.metadata.write")
def properties_remove(c: m.Call) -> Any:
    a = c.args
    rec = target_file(c, a.str("path", pattern=PATH_PATTERN))
    props = rec.setdefault("props", {})
    for tid in a.list("property_template_ids"):
        find_template(c, tid)
        if tid not in props:
            raise api_error(
                {".tag": "property_group_lookup", "property_group_lookup": tag("property_group_not_found")}
            )
        del props[tid]
    return None


def search_page(c: m.Call, state: dict[str, Any]) -> dict[str, Any]:
    visible = {t["id"] for t in visible_templates(c)}
    wanted = state.get("templates")
    queries = state["queries"]
    matches: list[dict[str, Any]] = []
    for rec in sorted(c.ctx.store.collection(m.ENTRIES), key=lambda r: r["id"]):
        if rec["type"] != "file" or not rec.get("props") or m.entry_role(c, rec) is None:
            continue
        hits = []
        for q in queries:
            found = False
            for tid, values in rec["props"].items():
                if tid not in visible or (wanted is not None and tid not in wanted):
                    continue
                value = values.get(q["field"])
                if value is not None and q["query"].lower() in value.lower():
                    found = True
            hits.append(found)
        if not hits or not (all(hits) if state["op"] == "and" else any(hits)):
            continue
        loc = m.loc_for_entry(c, rec)
        groups = [
            g
            for g in groups_of(rec)
            if g["template_id"] in visible and (wanted is None or g["template_id"] in wanted)
        ]
        matches.append(
            {
                "id": rec["id"],
                "path": loc.path_display if loc.reachable else "/" + rec["name"],
                "is_deleted": bool(rec.get("deleted")),
                "property_groups": groups,
            }
        )
    offset, limit = state["offset"], 100
    out: dict[str, Any] = {"matches": matches[offset : offset + limit]}
    if offset + limit < len(matches):
        out["cursor"] = Pager.encode({**state, "offset": offset + limit})
    return out


@route("file_properties/properties/search", scope="files.metadata.read")
def properties_search(c: m.Call) -> Any:
    a = c.args
    queries = []
    for raw in a.list("queries"):
        q = Args(raw, "request body: queries")
        mode, field = union_tag(q.raw("mode"))
        if mode != "field_name":
            raise BadInput("request body: queries: mode: expected field_name")
        queries.append({"query": q.str("query"), "field": field})
    ops = {
        Args(r).union("logical_operator", ("or_operator", "other"), default="or_operator")[0]
        for r in a.list("queries")
    }
    template_filter, payload = a.union(
        "template_filter", ("filter_none", "filter_some", "other"), default="filter_none"
    )
    wanted = list(payload) if template_filter == "filter_some" and isinstance(payload, list) else None
    if wanted:
        for tid in wanted:
            find_template(c, tid)
    return search_page(
        c,
        {
            "t": "props",
            "queries": queries,
            "op": "and" if ops == {"and_operator"} else "or",
            "templates": wanted,
            "offset": 0,
        },
    )


@route("file_properties/properties/search/continue", scope="files.metadata.read")
def properties_search_continue(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"))
    if not state or state.get("t") != "props":
        raise api_error(tag("reset"))
    return search_page(c, state)
