"""``/query``, ``/queryAll`` (with cursors), ``/search`` (SOSL) and ``/parameterizedSearch``."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from emulators.core.ids import salesforce_id
from emulators.providers.salesforce.common import SfError, bad_request
from emulators.providers.salesforce.rest_core import (
    Api,
    ApiRequest,
    ApiResponse,
    int_param,
    json_error,
    json_response,
    require_dict,
    require_list,
    require_str,
)
from emulators.providers.salesforce.rest_sobjects import mark_referenced
from emulators.providers.salesforce.schema import SCHEMA
from emulators.providers.salesforce.soql_exec import QueryEngine, Result
from emulators.providers.salesforce.sosl import build_parameterized, parse_sosl, run_sosl

CURSORS = "_sf_cursors"
DEFAULT_BATCH = 2000
MIN_BATCH = 200


def batch_size(req: ApiRequest) -> int:
    header = req.headers.get("sforce-query-options", "")
    match = re.search(r"batchSize\s*=\s*(\d+)", header, re.IGNORECASE)
    if not match:
        return DEFAULT_BATCH
    digits = match.group(1).lstrip("0")
    requested = DEFAULT_BATCH if len(digits) > 4 else int(digits or "0")
    return min(max(requested, MIN_BATCH), DEFAULT_BATCH)


def new_cursor_id(api: Api) -> str:
    seq = api.org.meta["seq"]
    seq["01g"] = seq.get("01g", 0) + 1
    return salesforce_id("01g", seq["01g"])


def page(
    req: ApiRequest,
    records: list[dict[str, Any]],
    total: int,
    batch: int,
    *,
    offset: int = 0,
    cursor_id: str | None = None,
) -> dict[str, Any]:
    chunk = records[offset : offset + batch]
    if offset + batch >= len(records):
        return {"totalSize": total, "done": True, "records": chunk}
    api = req.api
    if cursor_id is None:
        cursor_id = new_cursor_id(api)
        api.org.coll(CURSORS).put_as(
            cursor_id,
            {
                "id": cursor_id,
                "records": records,
                "total": total,
                "batch": batch,
                "owner": req.session.user["Id"],
                "expires_at": req.org.now() + timedelta(days=2),
            },
        )
    return {
        "totalSize": total,
        "done": False,
        "nextRecordsUrl": f"/services/data/v{req.version}/query/{cursor_id}-{offset + batch}",
        "records": chunk,
    }


def query_body(req: ApiRequest, result: Result, batch: int) -> dict[str, Any]:
    if result.count_only:
        return {"totalSize": result.total, "done": True, "records": []}
    return page(req, result.records, result.total, batch)


def register(api: Api) -> None:
    r = api.router

    def run(req: ApiRequest, all_rows: bool) -> ApiResponse:
        explain = req.q("explain")
        text = req.q("q") or explain
        if not text:
            raise bad_request("MALFORMED_QUERY", "Missing required query parameter: q")
        engine = QueryEngine(req.org, req.access, req.version)
        result = engine.execute(text, all_rows=all_rows)
        if explain:
            return json_response(
                {
                    "plans": [
                        {
                            "cardinality": result.total,
                            "fields": [],
                            "leadingOperationType": "TableScan",
                            "notes": [
                                {
                                    "description": "Not considering filter for optimization because unindexed",
                                    "fields": ["Name"],
                                    "tableEnumOrId": result.entity,
                                }
                            ],
                            "relativeCost": 1.0,
                            "sobjectCardinality": len(req.org.coll(result.entity)),
                            "sobjectType": result.entity,
                        }
                    ]
                }
            )
        for obj, rec in result.viewed:
            mark_referenced(req, obj, rec)
        return json_response(query_body(req, result, batch_size(req)))

    @r.route("GET", "query")
    def query(req: ApiRequest) -> ApiResponse:
        return run(req, False)

    @r.route("GET", "queryAll")
    def query_all(req: ApiRequest) -> ApiResponse:
        return run(req, True)

    @r.route("GET", "query/{locator}", "queryAll/{locator}")
    def more(req: ApiRequest) -> ApiResponse:
        cursor_id, _, offset_text = req.params["locator"].rpartition("-")
        cursor = req.org.coll(CURSORS).get(cursor_id)
        try:
            offset = int(offset_text) if offset_text.isascii() and offset_text.isdigit() else -1
        except ValueError:
            offset = -1
        if (
            cursor is None
            or offset < 0
            or cursor.get("owner") != req.session.user["Id"]
            or req.org.now() >= cursor["expires_at"]
            or offset > len(cursor["records"])
        ):
            raise SfError(400, "INVALID_QUERY_LOCATOR", "Invalid query locator")
        return json_response(
            page(req, cursor["records"], cursor["total"], cursor["batch"], offset=offset, cursor_id=cursor_id)
        )

    # -- SOSL ---------------------------------------------------------------------------
    @r.route("GET", "search")
    def search(req: ApiRequest) -> ApiResponse:
        text = req.q("q")
        if not text:
            raise bad_request("INVALID_SEARCH", "Missing required query parameter: q")
        engine = QueryEngine(req.org, req.access, req.version)
        return json_response({"searchRecords": run_sosl(engine, parse_sosl(text))})

    def _parameterized(req: ApiRequest, spec: dict[str, Any]) -> ApiResponse:
        q = spec.get("q")
        if not q:
            raise bad_request("INVALID_SEARCH", "Missing required parameter: q")
        require_str(q, "q")
        scope = spec.get("in") or "ALL"
        if not isinstance(scope, str) or scope.upper() not in ("ALL", "NAME", "EMAIL", "PHONE", "SIDEBAR"):
            raise bad_request("INVALID_SEARCH", f"Invalid search scope: {scope}")
        fields = spec.get("fields")
        if fields is not None and (
            not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)
        ):
            raise json_error("fields must be an array of field names")
        sobjects = spec.get("sobjects")
        if sobjects is not None:
            for entry in require_list(sobjects, "sobjects"):
                require_dict(entry, "sobjects entry")
                if not isinstance(entry.get("name"), str):
                    raise json_error("Every sobjects entry needs a string 'name'")
                if SCHEMA.get(entry["name"]) is None:
                    raise bad_request("INVALID_TYPE", f"sObject type '{entry['name']}' is not supported.")
                if entry.get("fields") is not None and not (
                    isinstance(entry["fields"], list) and all(isinstance(f, str) for f in entry["fields"])
                ):
                    raise json_error("sobjects.fields must be an array of field names")
                if entry.get("limit") is not None:
                    entry["limit"] = int_param(entry["limit"], f"{entry['name']}.limit", minimum=1)
        sosl = build_parameterized(
            q,
            scope=scope,
            sobjects=sobjects,
            fields=fields,
            overall_limit=int_param(spec["overallLimit"], "overallLimit", minimum=1)
            if spec.get("overallLimit") is not None
            else None,
            default_limit=int_param(spec["defaultLimit"], "defaultLimit", minimum=1)
            if spec.get("defaultLimit") is not None
            else None,
        )
        engine = QueryEngine(req.org, req.access, req.version)
        return json_response({"searchRecords": run_sosl(engine, sosl)})

    @r.route("GET", "parameterizedSearch")
    def parameterized_get(req: ApiRequest) -> ApiResponse:
        names = req.query.get("sobject", [])
        sobjects = []
        for name in names:
            entry: dict[str, Any] = {"name": name}
            if fields := req.q(f"{name}.fields"):
                entry["fields"] = [f.strip() for f in fields.split(",")]
            if where := req.q(f"{name}.where"):
                entry["where"] = where
            if limit := req.q(f"{name}.limit"):
                entry["limit"] = int_param(limit, f"{name}.limit", minimum=1)
            sobjects.append(entry)
        spec: dict[str, Any] = {
            "q": req.q("q"),
            "in": req.q("in"),
            "fields": [f.strip() for f in (req.q("fields") or "").split(",") if f.strip()] or None,
            "sobjects": sobjects or None,
            "overallLimit": req.q("overallLimit") or None,
            "defaultLimit": req.q("defaultLimit") or None,
        }
        return _parameterized(req, spec)

    @r.route("POST", "parameterizedSearch")
    def parameterized_post(req: ApiRequest) -> ApiResponse:
        return _parameterized(req, req.obj_json())
