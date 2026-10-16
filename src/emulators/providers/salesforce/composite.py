"""Composite resources: ``/composite``, ``/composite/batch``, ``/composite/tree`` and sObject collections."""

from __future__ import annotations

import re
from typing import Any

from emulators.providers.salesforce.common import SfError, bad_request, norm_id
from emulators.providers.salesforce.rest_core import (
    Api,
    ApiRequest,
    ApiResponse,
    json_error,
    json_response,
    require_dict,
    require_list,
)
from emulators.providers.salesforce.rest_sobjects import parse_fields_param
from emulators.providers.salesforce.schema import SCHEMA

MAX_SUBREQUESTS = 25
MAX_RECORDS = 200
HALTED = {
    "errorCode": "PROCESSING_HALTED",
    "message": "The transaction was rolled back since another operation in the same transaction failed.",
}
ROLLED_BACK = {
    "statusCode": "ALL_OR_NONE_OPERATION_ROLLED_BACK",
    "message": "Record rolled back because not all records were valid and the request was made with allOrNone header.",
    "fields": [],
}
_REF = re.compile(r"@\{([^}]+)\}")


# -- references --------------------------------------------------------------------------


class RefError(Exception):
    """An ``@{ref.path}`` placeholder that points at nothing."""


def _lookup(path: str, results: dict[str, Any]) -> Any:
    ref_id, _, rest = path.partition(".")
    if ref_id not in results:
        raise RefError(path)
    value: Any = results[ref_id]
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", rest):
        try:
            if part.startswith("["):
                value = value[int(part[1:-1])]
            else:
                if not isinstance(value, dict) or part not in value:
                    raise RefError(path)
                value = value[part]
        except (IndexError, TypeError, KeyError):
            raise RefError(path) from None
    return value


def substitute(value: Any, results: dict[str, Any]) -> Any:
    """Replace ``@{ref.path}`` placeholders in strings (recursively through JSON bodies)."""
    if isinstance(value, str):
        whole = _REF.fullmatch(value)
        if whole:
            return _lookup(whole.group(1), results)
        return _REF.sub(lambda m: str(_lookup(m.group(1), results)), value)
    if isinstance(value, list):
        return [substitute(v, results) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, results) for k, v in value.items()}
    return value


def _error_body(exc: SfError) -> list[dict[str, Any]]:
    return exc.body


# -- helpers -----------------------------------------------------------------------------


def _record_type(record: dict[str, Any], default: str | None = None) -> str:
    require_dict(record, "records entry")
    attrs = require_dict(record.get("attributes") or {}, "attributes")
    name = attrs.get("type") or default
    if name is not None and not isinstance(name, str):
        raise json_error("Cannot deserialize instance of string for attributes.type")
    if not name:
        raise bad_request(
            "INVALID_INPUT", "The required attribute 'type' is missing from the record attributes"
        )
    if SCHEMA.get(name) is None:
        raise bad_request("INVALID_TYPE", f"sObject type '{name}' is not supported.")
    return SCHEMA.get(name).name  # type: ignore[union-attr]


def _check_size(records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list) or not records:
        raise bad_request("INVALID_INPUT", "Records list must contain at least one record")
    if len(records) > MAX_RECORDS:
        raise SfError(400, "EXCEEDED_ID_LIMIT", f"Maximum size of records list is {MAX_RECORDS}")
    return records


def _save_result(rid: str | None, exc: SfError | None = None, **extra: Any) -> dict[str, Any]:
    if exc is not None:
        out: dict[str, Any] = {"success": False, "errors": [exc.dml_entry()]}
        if rid:
            out = {"id": rid, **out}
        return out
    return {"id": rid, "success": True, "errors": [], **extra}


def register(api: Api) -> None:
    r = api.router

    # -- /composite ---------------------------------------------------------------------
    @r.route("POST", "composite")
    async def composite(req: ApiRequest) -> ApiResponse:
        body = req.obj_json()
        subs = body.get("compositeRequest")
        if not isinstance(subs, list) or not subs:
            raise bad_request("INVALID_INPUT", "compositeRequest must be a non-empty list")
        for sub in subs:
            require_dict(sub, "compositeRequest entry")
            for key in ("method", "url", "referenceId"):
                if key in sub and not isinstance(sub[key], str):
                    raise json_error(f"Cannot deserialize instance of string for compositeRequest.{key}")
            if sub.get("body") is not None and not isinstance(sub["body"], (dict, list)):
                raise json_error("Cannot deserialize instance of object for compositeRequest.body")
        if len(subs) > MAX_SUBREQUESTS:
            raise bad_request(
                "INVALID_INPUT", f"Too many subrequests: {len(subs)}. The maximum is {MAX_SUBREQUESTS}."
            )
        seen: set[str] = set()
        for sub in subs:
            ref = sub.get("referenceId")
            if not ref or ref in seen:
                raise bad_request("INVALID_INPUT", f"referenceId is required and must be unique: {ref}")
            seen.add(ref)
        all_or_none = bool(body.get("allOrNone"))
        results: dict[str, Any] = {}
        out: list[dict[str, Any]] = []
        halted = False
        start = req.txn.savepoint()
        for sub in subs:
            ref = sub["referenceId"]
            if halted:
                out.append(_response(ref, 400, [HALTED], {}))
                continue
            try:
                url = substitute(sub.get("url", ""), results)
                payload = substitute(sub.get("body"), results)
            except RefError as exc:
                failure = _response(
                    ref,
                    400,
                    [
                        {
                            "errorCode": "INVALID_REFERENCE",
                            "message": f"Invalid reference specified. No value for @{{{exc}}}",
                        }
                    ],
                    {},
                )
                out.append(failure)
                results[ref] = failure["body"]
                if all_or_none:
                    halted = True
                continue
            method = str(sub.get("method", "GET")).upper()
            if method == "PUT":
                method = "PATCH"
            child = api.sub_request(req, method, url, payload)
            if child.path.startswith("composite"):
                nested = _response(
                    ref,
                    400,
                    [{"errorCode": "INVALID_INPUT", "message": "Composite requests cannot be nested"}],
                    {},
                )
                out.append(nested)
                results[ref] = nested["body"]
                if all_or_none:
                    halted = True
                continue
            child.headers = {
                **req.headers,
                **{k.lower(): v for k, v in (sub.get("httpHeaders") or {}).items()},
            }
            resp = await api.dispatch(child)
            entry = _response(ref, resp.status, resp.body, resp.headers)
            out.append(entry)
            results[ref] = resp.body
            if resp.status >= 400 and all_or_none:
                halted = True
        if halted:
            req.txn.rollback(start)
            failing = next(
                (i for i, e in enumerate(out) if e["httpStatusCode"] >= 400 and e["body"] != [HALTED]), None
            )
            out = [
                e if i == failing or e["body"] == [HALTED] else _response(e["referenceId"], 400, [HALTED], {})
                for i, e in enumerate(out)
            ]
        return json_response({"compositeResponse": out})

    # -- /composite/batch ---------------------------------------------------------------
    @r.route("POST", "composite/batch")
    async def batch(req: ApiRequest) -> ApiResponse:
        body = req.obj_json()
        items = body.get("batchRequests")
        if not isinstance(items, list) or not items:
            raise bad_request("INVALID_INPUT", "batchRequests must be a non-empty list")
        for item in items:
            require_dict(item, "batchRequests entry")
            for key in ("method", "url"):
                if key in item and not isinstance(item[key], str):
                    raise json_error(f"Cannot deserialize instance of string for batchRequests.{key}")
        if len(items) > MAX_SUBREQUESTS:
            raise bad_request(
                "INVALID_INPUT", f"Too many subrequests: {len(items)}. The maximum is {MAX_SUBREQUESTS}."
            )
        halt = bool(body.get("haltOnError"))
        results: list[dict[str, Any]] = []
        has_errors = False
        for item in items:
            if has_errors and halt:
                results.append(
                    {
                        "statusCode": 412,
                        "result": [
                            {
                                "errorCode": "BATCH_PROCESSING_HALTED",
                                "message": "Halted because of earlier error in batch",
                            }
                        ],
                    }
                )
                continue
            method = str(item.get("method", "GET")).upper()
            if method == "PUT":
                method = "PATCH"
            child = api.sub_request(req, method, item.get("url", ""), item.get("richInput"))
            if child.path.startswith("composite"):
                has_errors = True
                results.append(
                    {
                        "statusCode": 400,
                        "result": [
                            {"errorCode": "INVALID_INPUT", "message": "Composite requests cannot be nested"}
                        ],
                    }
                )
                continue
            resp = await api.dispatch(child)
            if resp.status >= 400:
                has_errors = True
            results.append({"statusCode": resp.status, "result": None if resp.status == 204 else resp.body})
        return json_response({"hasErrors": has_errors, "results": results})

    # -- /composite/tree/{type} ---------------------------------------------------------
    @r.route("POST", "composite/tree/{type}")
    def tree(req: ApiRequest) -> ApiResponse:
        root_type = SCHEMA.get(req.params["type"])
        if root_type is None:
            raise bad_request("INVALID_TYPE", f"sObject type '{req.params['type']}' is not supported.")
        records = _check_size(req.obj_json().get("records"))
        # (referenceId, sObject name, record, parent referenceId, parent field)
        flat: list[tuple[str, str, dict[str, Any], str | None, str | None]] = []
        seen: set[str] = set()

        def walk(
            rec: dict[str, Any], type_name: str, parent: str | None, parent_field: str | None, depth: int
        ) -> None:
            require_dict(rec, "records entry")
            attrs = require_dict(rec.get("attributes") or {}, "attributes")
            ref = attrs.get("referenceId")
            if not ref or ref in seen:
                raise bad_request("INVALID_INPUT", f"referenceId is required and must be unique: {ref}")
            if depth > 5:
                raise bad_request("INVALID_INPUT", "Maximum tree depth is 5")
            seen.add(ref)
            obj = SCHEMA.get(attrs.get("type") or type_name)
            if obj is None:
                raise bad_request("INVALID_TYPE", f"sObject type '{attrs.get('type')}' is not supported.")
            flat.append((ref, obj.name, rec, parent, parent_field))
            if len(flat) > MAX_RECORDS:
                raise SfError(400, "EXCEEDED_ID_LIMIT", f"Maximum size of records list is {MAX_RECORDS}")
            for key, value in rec.items():
                if key == "attributes" or not isinstance(value, dict) or "records" not in value:
                    continue
                rel = SCHEMA.child_rel(obj.name, key)
                if rel is None:
                    raise bad_request(
                        "INVALID_FIELD", f"No such relationship '{key}' on sobject of type {obj.name}"
                    )
                for child in require_list(value["records"], "records"):
                    walk(child, rel.child, ref, rel.field, depth + 1)

        for rec in records:
            walk(rec, root_type.name, None, None, 1)
        dml = req.dml()
        ids: dict[str, str] = {}
        created: list[dict[str, str]] = []
        sp = req.txn.savepoint()
        current = ""
        try:
            for ref, obj_name, rec, parent, parent_field in flat:
                current = ref
                data = {
                    k: v
                    for k, v in rec.items()
                    if k != "attributes" and not (isinstance(v, dict) and "records" in v)
                }
                if parent and parent_field:
                    data[parent_field] = ids[parent]
                saved = dml.insert(obj_name, data)
                ids[ref] = saved["Id"]
                created.append({"referenceId": ref, "id": saved["Id"]})
        except SfError as exc:
            req.txn.rollback(sp)
            halted = {"statusCode": "PROCESSING_HALTED", "message": HALTED["message"], "fields": []}
            results = [
                {"referenceId": ref, "errors": [exc.dml_entry() if ref == current else halted]}
                for ref, *_ in flat
            ]
            return json_response({"hasErrors": True, "results": results}, 400)
        return json_response({"hasErrors": False, "results": created}, 201)

    # -- /composite/sobjects --------------------------------------------------------------
    @r.route("POST", "composite/sobjects")
    def create_many(req: ApiRequest) -> ApiResponse:
        body = req.obj_json()
        records = _check_size(body.get("records"))
        return json_response(_save_many(req, records, bool(body.get("allOrNone")), "create"))

    @r.route("PATCH", "composite/sobjects")
    def update_many(req: ApiRequest) -> ApiResponse:
        body = req.obj_json()
        records = _check_size(body.get("records"))
        return json_response(_save_many(req, records, bool(body.get("allOrNone")), "update"))

    @r.route("PATCH", "composite/sobjects/{type}/{field}")
    def upsert_many(req: ApiRequest) -> ApiResponse:
        body = req.obj_json()
        records = _check_size(body.get("records"))
        return json_response(
            _save_many(
                req, records, bool(body.get("allOrNone")), "upsert", req.params["type"], req.params["field"]
            )
        )

    @r.route("DELETE", "composite/sobjects")
    def delete_many(req: ApiRequest) -> ApiResponse:
        ids = [i for i in (req.q("ids") or "").split(",") if i]
        if not ids:
            raise bad_request("INVALID_INPUT", "The ids query parameter is required")
        if len(ids) > MAX_RECORDS:
            raise SfError(400, "EXCEEDED_ID_LIMIT", f"Maximum size of ids list is {MAX_RECORDS}")
        all_or_none = (req.q("allOrNone") or "false").lower() == "true"
        dml = req.dml()
        results: list[dict[str, Any]] = []
        start = req.txn.savepoint()
        failed = False
        for rid in ids:
            sp = req.txn.savepoint()
            try:
                norm = norm_id(rid)
                found = req.org.find(norm) if norm else None
                if found is None:
                    raise SfError(404, "ENTITY_IS_DELETED", "entity is deleted")
                dml.delete(found[0].name, norm)
                results.append(_save_result(rid))
            except SfError as exc:
                req.txn.rollback(sp)
                failed = True
                results.append(_save_result(rid, exc))
        if failed and all_or_none:
            req.txn.rollback(start)
            results = [
                r_ if not r_["success"] else {"id": r_["id"], "success": False, "errors": [ROLLED_BACK]}
                for r_ in results
            ]
        return json_response(results)

    def _retrieve(req: ApiRequest, type_name: str, ids: list[str], fields_raw: str | None) -> ApiResponse:
        obj = SCHEMA.get(type_name)
        if obj is None:
            raise bad_request("INVALID_TYPE", f"sObject type '{type_name}' is not supported.")
        if len(ids) > MAX_RECORDS:
            raise SfError(400, "EXCEEDED_ID_LIMIT", f"Maximum size of ids list is {MAX_RECORDS}")
        fields = parse_fields_param(obj, fields_raw)
        out: list[dict[str, Any] | None] = []
        for rid in ids:
            norm = norm_id(rid)
            rec = req.org.coll(obj.name).get(norm) if norm else None
            if rec is None or rec.get("IsDeleted") or not req.access.can_read(obj, rec):
                out.append(None)
            else:
                out.append(req.org.render(obj, rec, req.version, fields))
        return json_response(out)

    @r.route("GET", "composite/sobjects/{type}")
    def retrieve_get(req: ApiRequest) -> ApiResponse:
        ids = [i for i in (req.q("ids") or "").split(",") if i]
        if not ids:
            raise bad_request("INVALID_INPUT", "The ids query parameter is required")
        return _retrieve(req, req.params["type"], ids, req.q("fields"))

    @r.route("POST", "composite/sobjects/{type}")
    def retrieve_post(req: ApiRequest) -> ApiResponse:
        body = req.obj_json()
        fields = require_list(body.get("fields") or [], "fields")
        ids = require_list(body.get("ids") or [], "ids")
        if not all(isinstance(x, str) for x in [*fields, *ids]):
            raise json_error("ids and fields must be arrays of strings")
        return _retrieve(req, req.params["type"], ids, ",".join(fields) if fields else None)


def _response(ref: str, status: int, body: Any, headers: dict[str, str]) -> dict[str, Any]:
    return {
        "body": None if status == 204 else body,
        "httpHeaders": {
            k: v for k, v in headers.items() if k.lower() in ("location", "etag", "last-modified")
        },
        "httpStatusCode": status,
        "referenceId": ref,
    }


def _save_many(
    req: ApiRequest,
    records: list[dict[str, Any]],
    all_or_none: bool,
    op: str,
    upsert_type: str | None = None,
    ext_field: str | None = None,
) -> list[dict[str, Any]]:
    dml = req.dml()
    results: list[dict[str, Any]] = []
    for rec in records:  # a record without a resolvable type invalidates the whole request
        _record_type(rec, upsert_type)
    start = req.txn.savepoint()
    failed = False
    for rec in records:
        sp = req.txn.savepoint()
        rid = norm_id(rec.get("id") or rec.get("Id")) if (rec.get("id") or rec.get("Id")) else None
        try:
            type_name = _record_type(rec, upsert_type)
            data = {k: v for k, v in rec.items() if k not in ("attributes", "id", "Id")}
            if op == "create":
                saved = dml.insert(type_name, data)
                results.append(_save_result(saved["Id"]))
            elif op == "update":
                if not rid:
                    raise SfError(400, "MISSING_ARGUMENT", "Id not specified in an update call", [])
                dml.update(type_name, rid, data)
                results.append(_save_result(rid))
            else:
                obj = SCHEMA.objects[type_name]
                assert ext_field is not None
                f = obj.field(ext_field)
                if f is None:
                    raise SfError(
                        400, "INVALID_FIELD", f"No such column '{ext_field}' on sobject of type {type_name}"
                    )
                value = data.get(f.name, rec.get(ext_field))
                matches = dml.find_by_external(obj, f.name, value) if value is not None else []
                if len(matches) > 1:
                    raise SfError(300, "MULTIPLE_CHOICES", "More than one record found for external id field")
                if matches:
                    dml.update(type_name, matches[0]["Id"], {k: v for k, v in data.items() if k != f.name})
                    results.append(_save_result(matches[0]["Id"], created=False))
                else:
                    saved = dml.insert(type_name, data)
                    results.append(_save_result(saved["Id"], created=True))
        except SfError as exc:
            req.txn.rollback(sp)
            failed = True
            results.append(_save_result(rid if op != "create" else None, exc))
    if failed and all_or_none:
        req.txn.rollback(start)
        results = [r_ if not r_["success"] else {"success": False, "errors": [ROLLED_BACK]} for r_ in results]
    return results
