"""``/sobjects``: describe, CRUD, external-id upsert, relationships, updated/deleted, list views."""

from __future__ import annotations

import re
from datetime import timedelta
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any
from urllib.parse import quote

from emulators.providers.salesforce.blobs import guess_mime, parse_multipart
from emulators.providers.salesforce.common import (
    MASTER_RECORD_TYPE_ID,
    SfError,
    bad_request,
    fmt_dt,
    insufficient_access,
    norm_id,
    not_found,
    parse_dt,
    to_18,
)
from emulators.providers.salesforce.rest_core import (
    Api,
    ApiRequest,
    ApiResponse,
    int_param,
    json_response,
    no_content,
    require_str,
)
from emulators.providers.salesforce.schema import SCHEMA, SObject
from emulators.providers.salesforce.soql_exec import QueryEngine

RECENT_LIMIT = 200


def _object(name: str) -> SObject:
    obj = SCHEMA.get(name)
    if obj is None:
        raise not_found()
    return obj


def body_data(req: ApiRequest, obj: SObject) -> dict[str, Any]:
    """The JSON body, or the fields of a ``multipart/form-data`` upload (entity_content + binary parts)."""
    content_type = req.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        return req.obj_json()
    data, filename = parse_multipart(content_type, req.body)
    if filename:
        for name in ("PathOnClient", "Name"):
            if obj.field(name) is not None and name not in data:
                data[name] = filename
    return data


def create_result(rid: str) -> dict[str, Any]:
    return {"id": rid, "success": True, "errors": []}


def parse_fields_param(obj: SObject, raw: str | None) -> list[str] | None:
    if not raw:
        return None
    names: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        f = obj.field(part)
        if f is None:
            raise SfError(400, "INVALID_FIELD", f"No such column '{part}' on sobject of type {obj.name}")
        names.append(f.name)
    return names


def _http_date(text: str) -> str:
    return format_datetime(parse_dt(text) or parse_dt("1970-01-01"), usegmt=True)  # type: ignore[arg-type]


def _not_modified_since(req: ApiRequest, stamp: str) -> bool:
    header = req.headers.get("if-modified-since")
    if not header:
        return False
    try:
        since = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return False
    modified = parse_dt(stamp)
    return bool(modified and since and since.tzinfo and modified.replace(microsecond=0) <= since)


def mark_referenced(req: ApiRequest, obj: SObject, rec: dict[str, Any]) -> None:
    """Maintain the user's most-recently-used list (shown by ``/recent`` and ``/sobjects/X``)."""
    api: Api = req.api
    recent: list[dict[str, str]] = api.org.meta.setdefault("recent", [])
    uid = req.session.user["Id"]
    recent[:] = [r for r in recent if not (r["user"] == uid and r["id"] == rec["Id"])]
    recent.append({"user": uid, "object": obj.name, "id": rec["Id"], "at": api.org.now_str()})
    if len(recent) > 2000:
        del recent[:500]
    for key in ("LastViewedDate", "LastReferencedDate"):
        if key in rec:
            rec[key] = api.org.now_str()


def recent_items(req: ApiRequest, only: str | None = None, limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
    org = req.org
    uid = req.session.user["Id"]
    items = []
    for entry in reversed(org.meta.get("recent", [])):
        if entry["user"] != uid or (only and entry["object"] != only):
            continue
        obj = SCHEMA.objects[entry["object"]]
        rec = org.coll(obj.name).get(entry["id"])
        if rec is None or rec.get("IsDeleted") or not req.access.can_read(obj, rec):
            continue
        name_value = rec.get(obj.name_field)
        items.append(
            {
                "attributes": org.attributes(obj.name, rec["Id"], req.version),
                "Id": rec["Id"],
                "Name": name_value,
            }
        )
        if len(items) >= limit:
            break
    return items


def register(api: Api) -> None:
    r = api.router

    # -- describe ----------------------------------------------------------------------
    @r.route("GET", "sobjects")
    def describe_global(req: ApiRequest) -> ApiResponse:
        access = req.access
        entries = [
            o.global_entry(
                req.version,
                can_create=access.can_create_object(o),
                can_update=access.can_update_object(o),
                can_delete=access.can_delete_object(o),
            )
            for o in SCHEMA.objects.values()
        ]
        for entry in entries:
            entry["urls"] = {
                "sobject": f"/services/data/v{req.version}/sobjects/{entry['name']}",
                "describe": f"/services/data/v{req.version}/sobjects/{entry['name']}/describe",
                "rowTemplate": f"/services/data/v{req.version}/sobjects/{entry['name']}/{{ID}}",
            }
        return ApiResponse(
            200,
            {"encoding": "UTF-8", "maxBatchSize": 200, "sobjects": entries},
            {"Last-Modified": _http_date(req.org.get("Organization", req.org.org_id18)["CreatedDate"])},
        )

    @r.route("GET", "sobjects/{type}")
    def object_info(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        access = req.access
        entry = SCHEMA.describe_object(
            obj,
            req.version,
            can_create=access.can_create_object(obj),
            can_update=access.can_update_object(obj),
            can_delete=access.can_delete_object(obj),
        )
        entry["urls"] = obj.urls(req.version)
        info = {
            k: v for k, v in entry.items() if k not in ("fields", "childRelationships", "recordTypeInfos")
        }
        return json_response(
            {"objectDescribe": info, "recentItems": recent_items(req, obj.name, 25)},
            headers={
                "Last-Modified": _http_date(req.org.get("Organization", req.org.org_id18)["CreatedDate"])
            },
        )

    @r.route("GET", "sobjects/{type}/describe")
    def describe_object(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        stamp = req.org.get("Organization", req.org.org_id18)["CreatedDate"]
        if _not_modified_since(req, stamp):
            return ApiResponse(304, None)
        access = req.access
        body = SCHEMA.describe_object(
            obj,
            req.version,
            can_create=access.can_create_object(obj),
            can_update=access.can_update_object(obj),
            can_delete=access.can_delete_object(obj),
        )
        return json_response(body, headers={"Last-Modified": _http_date(stamp)})

    @r.route("GET", "sobjects/{type}/describe/layouts", "sobjects/{type}/describe/layouts/{rtype}")
    def describe_layouts(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        sections = [
            {
                "columns": 2,
                "heading": f"{obj.label} Information",
                "layoutRows": [
                    {
                        "layoutItems": [
                            {
                                "editableForNew": f.createable,
                                "editableForUpdate": f.updateable,
                                "label": f.label,
                                "layoutComponents": [
                                    {
                                        "details": f.describe(),
                                        "displayLines": 1,
                                        "tabOrder": i + 1,
                                        "type": "Field",
                                        "value": f.name,
                                    }
                                ],
                                "placeholder": False,
                                "required": f.required,
                            }
                        ],
                        "numItems": 1,
                    }
                    for i, f in enumerate(obj.fields[:12])
                    if f.type != "address"
                ],
                "rows": 12,
                "tabOrder": "TopToBottom",
                "useCollapsibleSection": False,
                "useHeading": True,
            }
        ]
        layout = {
            "buttonLayoutSection": {"detailButtons": []},
            "detailLayoutSections": sections,
            "editLayoutSections": sections,
            "feedView": None,
            "highlightsPanelLayoutSection": None,
            "id": "00h000000000000AAA",
            "multirowEditLayoutSections": [],
            "offlineLinks": [],
            "quickActionList": {"quickActionListItems": []},
            "relatedContent": None,
            "relatedLists": [],
            "saveOptions": [],
        }
        if req.params.get("rtype") not in (None, MASTER_RECORD_TYPE_ID):
            raise not_found()
        if "rtype" in req.params:
            return json_response(layout)
        return json_response(
            {
                "layouts": [layout],
                "recordTypeMappings": [],
                "recordTypeSelectorRequired": [False],
            }
        )

    @r.route("GET", "sobjects/{type}/describe/compactLayouts")
    def compact_layouts(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        return json_response(
            {
                "compactLayouts": [
                    {
                        "actions": [],
                        "fieldItems": [],
                        "id": "0AH000000000000AAA",
                        "imageItems": [],
                        "label": "System Default",
                        "name": "SYSTEM",
                        "objectType": obj.name,
                    }
                ],
                "defaultCompactLayoutId": "0AH000000000000AAA",
                "recordTypeCompactLayoutMappings": [],
            }
        )

    @r.route("GET", "sobjects/{type}/describe/approvalLayouts")
    def approval_layouts(req: ApiRequest) -> ApiResponse:
        _object(req.params["type"])
        return json_response({"approvalLayouts": []})

    @r.route("GET", "sobjects/{type}/quickActions")
    def quick_actions(req: ApiRequest) -> ApiResponse:
        _object(req.params["type"])
        return json_response([])

    # -- create -------------------------------------------------------------------------
    @r.route("POST", "sobjects/{type}")
    def create(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        rec = req.dml().insert(obj.name, body_data(req, obj))
        mark_referenced(req, obj, rec)
        location = f"/services/data/v{req.version}/sobjects/{obj.name}/{rec['Id']}"
        return json_response(create_result(rec["Id"]), 201, {"Location": location})

    # -- updated / deleted ------------------------------------------------------------------
    def _range(req: ApiRequest) -> tuple[str, str, Any, Any]:
        raw_start, raw_end = req.q("start"), req.q("end")
        if not raw_start or not raw_end:
            raise bad_request("INVALID_FIELD", "Start and end dates are required")

        def parse(text: str) -> Any:
            fixed = re.sub(r" (\d{2}:\d{2})$", r"+\1", text.strip())
            dt = parse_dt(fixed)
            if dt is None:
                raise bad_request("INVALID_REPLICATION_DATE", f"Invalid date: {text}")
            return dt

        start, end = parse(raw_start), parse(raw_end)
        now = req.org.now()
        if end > now + timedelta(minutes=1):
            raise bad_request("INVALID_REPLICATION_DATE", "The requested end date is in the future")
        if start >= end:
            raise bad_request("INVALID_REPLICATION_DATE", "Start date must be before the end date")
        if start < now - timedelta(days=30):
            raise bad_request(
                "INVALID_REPLICATION_DATE", "Start date is outside the retention window (30 days)"
            )
        # Salesforce works at minute resolution; widen the window so edge-of-second clients still see their changes
        lo = start.replace(second=0, microsecond=0)
        hi = end.replace(microsecond=0) + timedelta(seconds=1)
        return fmt_dt(lo), fmt_dt(hi), start, end

    @r.route("GET", "sobjects/{type}/updated")
    def updated(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        lo, hi, _, end = _range(req)
        pred = req.access.readable(obj)
        ids = [
            rec["Id"]
            for rec in req.org.coll(obj.name)
            if not rec.get("IsDeleted") and lo <= rec.get("SystemModstamp", "") <= hi and pred(rec)
        ]
        return json_response({"ids": ids, "latestDateCovered": fmt_dt(end)})

    @r.route("GET", "sobjects/{type}/deleted")
    def deleted(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        lo, hi, _, end = _range(req)
        pred = req.access.readable(obj)
        rows = [
            {"deletedDate": rec["_DeletedDate"], "id": rec["Id"]}
            for rec in req.org.coll(obj.name)
            if rec.get("IsDeleted")
            and rec.get("_DeletedDate")
            and lo <= rec["_DeletedDate"] <= hi
            and pred(rec)
        ]
        return json_response(
            {
                "deletedRecords": rows,
                "earliestDateAvailable": fmt_dt(req.org.now() - timedelta(days=30)),
                "latestDateCovered": fmt_dt(end),
            }
        )

    # -- list views ----------------------------------------------------------------------
    register_listviews(api)

    @r.route("POST", "sobjects/User/{id}/password", "sobjects/SelfServiceUser/{id}/password")
    def set_password(req: ApiRequest) -> ApiResponse:
        user = _admin_target(req, req.params["id"])
        data = req.obj_json()
        new = data.get("NewPassword")
        if not new:
            raise SfError(
                400, "REQUIRED_FIELD_MISSING", "Required fields are missing: [NewPassword]", ["NewPassword"]
            )
        require_str(new, "NewPassword")
        if len(new) < 8:
            raise SfError(
                400, "INVALID_NEW_PASSWORD", "Your password must have a length of at least 8 characters."
            )
        req.org.meta.setdefault("passwords", {})[user["Id"]] = new
        user["LastPasswordChangeDate"] = req.org.now_str()
        return no_content()

    @r.route("GET", "sobjects/User/{id}/password")
    def password_status(req: ApiRequest) -> ApiResponse:
        _admin_target(req, req.params["id"])
        return json_response({"IsExpired": False})

    @r.route("DELETE", "sobjects/User/{id}/password")
    def reset_password(req: ApiRequest) -> ApiResponse:
        user = _admin_target(req, req.params["id"])
        password = "Tmp" + user["Id"][-8:] + "!"
        req.org.meta.setdefault("passwords", {})[user["Id"]] = password
        user["LastPasswordChangeDate"] = req.org.now_str()
        return json_response({"NewPassword": password})

    # -- record by id / external id -----------------------------------------------------------
    @r.route("GET", "sobjects/{type}/{a}")
    def get_record(req: ApiRequest) -> ApiResponse:
        return _get_by_id(req, req.params["a"])

    @r.route("PATCH", "sobjects/{type}/{a}")
    def patch_record(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        rid = _id_for(obj, req.params["a"])
        data = body_data(req, obj)
        if "if-unmodified-since" in req.headers:
            rec = req.dml().load(obj, rid)
            if not _not_modified_since(
                _with_header(req, "if-modified-since", req.headers["if-unmodified-since"]),
                rec["SystemModstamp"],
            ):
                raise SfError(412, "PRECONDITION_FAILED", "The record was modified since the supplied date")
        rec = req.dml().update(obj.name, rid, data)
        mark_referenced(req, obj, rec)
        return no_content()

    @r.route("DELETE", "sobjects/{type}/{a}")
    def delete_record(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        req.dml().delete(obj.name, _id_for(obj, req.params["a"]))
        return no_content()

    @r.route("GET", "sobjects/{type}/{a}/{b:path}")
    def get_nested(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        a, b = req.params["a"], req.params["b"]
        if norm_id(a) is not None:
            blob_field = obj.field(b)
            if blob_field is not None and blob_field.type == "base64":
                return _blob(req, obj, a, blob_field.name)
            return _relationship(req, obj, a, b)
        return _get_external(req, obj, a, b)

    @r.route("PATCH", "sobjects/{type}/{a}/{b:path}")
    def upsert(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        field_name, value = req.params["a"], req.params["b"]
        dml = req.dml()
        matches = dml.find_by_external(obj, field_name, value)
        data = req.obj_json()
        if len(matches) > 1:
            return _multiple(req, obj, matches)
        if matches:
            body = {k: v for k, v in data.items() if k != "attributes"}
            rec = dml.update(obj.name, matches[0]["Id"], body)
            mark_referenced(req, obj, rec)
            if float(req.version) >= 46:
                return json_response({**create_result(rec["Id"]), "created": False})
            return no_content()
        if float(req.version) >= 61 and (req.q("updateOnly") or "").lower() == "true":
            raise not_found()
        f = obj.field(field_name)
        if f is None or f.name == "Id":
            raise not_found()
        body = {**data, f.name: value}
        rec = dml.insert(obj.name, body)
        mark_referenced(req, obj, rec)
        location = f"/services/data/v{req.version}/sobjects/{obj.name}/{rec['Id']}"
        result = create_result(rec["Id"])
        if float(req.version) >= 46:
            result["created"] = True
        return json_response(result, 201, {"Location": location})

    @r.route("DELETE", "sobjects/{type}/{a}/{b:path}")
    def delete_external(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        dml = req.dml()
        matches = dml.find_by_external(obj, req.params["a"], req.params["b"])
        if not matches:
            raise not_found()
        if len(matches) > 1:
            return _multiple(req, obj, matches)
        dml.delete(obj.name, matches[0]["Id"])
        return no_content()


def _with_header(req: ApiRequest, name: str, value: str) -> ApiRequest:
    clone = ApiRequest(**{**req.__dict__})
    clone.headers = {**req.headers, name: value}
    return clone


def _admin_target(req: ApiRequest, user_id: str) -> dict[str, Any]:
    if not req.access.is_admin and req.session.user["Id"] != norm_id(user_id):
        raise insufficient_access()
    user = req.org.user(user_id)
    if user is None:
        raise not_found()
    return user


def _id_for(obj: SObject, segment: str) -> str:
    rid = norm_id(segment)
    if rid is None or not rid.startswith(obj.prefix):
        raise not_found()
    return rid


def _get_by_id(req: ApiRequest, segment: str) -> ApiResponse:
    obj = _object(req.params["type"])
    rid = _id_for(obj, segment)
    dml = req.dml()
    rec = dml.load(obj, rid, allow_deleted=True)
    if rec.get("IsDeleted"):
        raise not_found()
    if _not_modified_since(req, rec["SystemModstamp"]):
        return ApiResponse(304, None)
    fields = parse_fields_param(obj, req.q("fields"))
    mark_referenced(req, obj, rec)
    return json_response(
        req.org.render(obj, rec, req.version, fields),
        headers={"Last-Modified": _http_date(rec["SystemModstamp"])},
    )


def _blob(req: ApiRequest, obj: SObject, record_id: str, field_name: str) -> ApiResponse:
    rec = req.dml().load(obj, _id_for(obj, record_id), allow_deleted=True)
    handle = rec.get(field_name)
    if rec.get("IsDeleted") or not handle:
        raise not_found()
    name = rec.get("PathOnClient") or rec.get("Name") or rec.get("Title")
    data = req.org.store.blobs.get(str(handle).removeprefix("blob:"))
    headers = {}
    if name:
        fallback = re.sub(r'[\x00-\x1f\x7f"\\\\]', "_", name).encode("ascii", "replace").decode()
        headers["Content-Disposition"] = (
            f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
        )
    content_type = rec.get("ContentType") or guess_mime(name)
    if not content_type.isascii() or any(ord(ch) < 32 or ord(ch) == 127 for ch in content_type):
        content_type = "application/octet-stream"
    return ApiResponse(200, data, headers, media_type=content_type)


def _multiple(req: ApiRequest, obj: SObject, matches: list[dict[str, Any]]) -> ApiResponse:
    urls = [f"/services/data/v{req.version}/sobjects/{obj.name}/{m['Id']}" for m in matches]
    return json_response(urls, 300)


def _get_external(req: ApiRequest, obj: SObject, field_name: str, value: str) -> ApiResponse:
    matches = req.dml().find_by_external(obj, field_name, value)
    if not matches:
        raise not_found()
    if len(matches) > 1:
        return _multiple(req, obj, matches)
    fields = parse_fields_param(obj, req.q("fields"))
    mark_referenced(req, obj, matches[0])
    return json_response(req.org.render(obj, matches[0], req.version, fields))


def _relationship(req: ApiRequest, obj: SObject, record_id: str, rel_name: str) -> ApiResponse:
    rid = _id_for(obj, record_id)
    rec = req.dml().load(obj, rid, allow_deleted=True)
    if rec.get("IsDeleted"):
        raise not_found()
    rel = SCHEMA.child_rel(obj.name, rel_name)
    if rel is not None:
        child = SCHEMA.objects[rel.child]
        fields = parse_fields_param(child, req.q("fields"))
        engine = QueryEngine(req.org, req.access, req.version)
        pred = engine.readable(child)
        rows = [
            req.org.render(child, c, req.version, fields)
            for c in req.org.coll(child.name)
            if c.get(rel.field) == rid and not c.get("IsDeleted") and pred(c)
        ]
        return json_response({"totalSize": len(rows), "done": True, "records": rows})
    parent_field = obj.rel(rel_name)
    if parent_field is not None:
        parent_id = rec.get(parent_field.name)
        found = req.org.find(parent_id) if parent_id else None
        if found is None or found[1].get("IsDeleted") or not req.access.can_read(*found):
            raise not_found()
        fields = parse_fields_param(found[0], req.q("fields"))
        return json_response(req.org.render(found[0], found[1], req.version, fields))
    raise not_found()


# -- list views ---------------------------------------------------------------------------


def _view_id(obj: SObject, kind: str) -> str:
    from emulators.core.ids import stable_hash

    n = int(stable_hash("listview", obj.name, kind, length=8), 16) % 9_000_000 + 1000
    return to_18(f"00B5g{n:010d}")


def _views(obj: SObject) -> list[dict[str, str]]:
    views = [{"kind": "all", "label": f"All {obj.plural}", "dev": f"All{obj.plural.replace(' ', '')}"}]
    if obj.has_owner:
        views.append({"kind": "mine", "label": f"My {obj.plural}", "dev": f"My{obj.plural.replace(' ', '')}"})
    return views


def register_listviews(api: Api) -> None:
    r = api.router

    @r.route("GET", "sobjects/{type}/listviews")
    def list_views(req: ApiRequest) -> ApiResponse:
        obj = _object(req.params["type"])
        base = f"/services/data/v{req.version}/sobjects/{obj.name}/listviews"
        views = [
            {
                "describeUrl": f"{base}/{_view_id(obj, v['kind'])}/describe",
                "developerName": v["dev"],
                "id": _view_id(obj, v["kind"]),
                "label": v["label"],
                "resultsUrl": f"{base}/{_view_id(obj, v['kind'])}/results",
                "soqlCompatible": True,
                "url": f"{base}/{_view_id(obj, v['kind'])}",
            }
            for v in _views(obj)
        ]
        return json_response(
            {
                "done": True,
                "listviews": views,
                "nextRecordsUrl": None,
                "size": len(views),
                "sobjectType": obj.name,
            }
        )

    def _find_view(req: ApiRequest) -> tuple[SObject, dict[str, str]]:
        obj = _object(req.params["type"])
        view = next((v for v in _views(obj) if _view_id(obj, v["kind"]) == req.params["view"]), None)
        if view is None:
            raise not_found()
        return obj, view

    def _columns(obj: SObject) -> list[str]:
        names = [obj.name_field]
        for candidate in (
            "Type",
            "Phone",
            "StageName",
            "Amount",
            "CloseDate",
            "Status",
            "Email",
            "Company",
            "Subject",
            "AccountId",
        ):
            if obj.field(candidate) and candidate not in names:
                names.append(candidate)
        names.append("OwnerId" if obj.has_owner else "CreatedDate")
        return names[:6]

    def _soql(obj: SObject, view: dict[str, str]) -> str:
        cols = ", ".join(dict.fromkeys(["Id", *_columns(obj)]))
        where = " WHERE OwnerId = '{uid}'" if view["kind"] == "mine" else ""
        return f"SELECT {cols} FROM {obj.name}{where} ORDER BY {obj.name_field}"

    @r.route("GET", "sobjects/{type}/listviews/{view}")
    def view_info(req: ApiRequest) -> ApiResponse:
        obj, view = _find_view(req)
        return json_response(
            {
                "id": req.params["view"],
                "label": view["label"],
                "developerName": view["dev"],
                "sobjectType": obj.name,
                "query": _soql(obj, view).replace("{uid}", req.session.user["Id"]),
            }
        )

    @r.route("GET", "sobjects/{type}/listviews/{view}/describe")
    def view_describe(req: ApiRequest) -> ApiResponse:
        obj, view = _find_view(req)
        columns = [
            {
                "ascendingLabel": "A-Z",
                "descendingLabel": "Z-A",
                "fieldNameOrPath": c,
                "hidden": False,
                "label": obj.field(c).label,  # type: ignore[union-attr]
                "selectListItem": c,
                "sortDirection": None,
                "sortIndex": None,
                "sortable": True,
                "type": obj.field(c).type,  # type: ignore[union-attr]
            }
            for c in _columns(obj)
        ]
        return json_response(
            {
                "columns": columns,
                "id": req.params["view"],
                "orderBy": [],
                "query": _soql(obj, view).replace("{uid}", req.session.user["Id"]),
                "scope": "mine" if view["kind"] == "mine" else "everything",
                "sobjectType": obj.name,
                "whereCondition": None,
            }
        )

    @r.route("GET", "sobjects/{type}/listviews/{view}/results")
    def view_results(req: ApiRequest) -> ApiResponse:
        obj, view = _find_view(req)
        limit = int_param(req.q("pageSize") or req.q("limit") or 25, "pageSize", minimum=1, maximum=2000)
        engine = QueryEngine(req.org, req.access, req.version)
        result = engine.execute(_soql(obj, view).replace("{uid}", req.session.user["Id"]))
        cols = _columns(obj)
        records = [
            {
                "columns": [
                    {"fieldNameOrPath": c, "value": None if rec.get(c) is None else str(rec.get(c))}
                    for c in cols
                ]
            }
            for rec in result.records[:limit]
        ]
        return json_response(
            {
                "columns": [{"fieldNameOrPath": c, "label": obj.field(c).label} for c in cols],  # type: ignore[union-attr]
                "developerName": view["dev"],
                "done": True,
                "id": req.params["view"],
                "label": view["label"],
                "records": records,
                "size": result.total,
            }
        )
