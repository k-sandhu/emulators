"""Bulk API 2.0: ingest jobs (insert/update/upsert/delete/hardDelete) and query jobs.

Jobs run synchronously when the client closes the upload (``state=UploadComplete``) or creates
a query job; results are kept on the job record and served as CSV. ``EMULATORS_SALESFORCE_BULK_INPROGRESS_POLLS``
makes jobs report ``InProgress`` for N polls before ``JobComplete`` to exercise polling loops.
"""

from __future__ import annotations

import base64
import csv
import io
from typing import Any

from emulators.core.ids import salesforce_id
from emulators.providers.salesforce.common import SfError, norm_id, not_found
from emulators.providers.salesforce.dml import Dml
from emulators.providers.salesforce.rest_core import (
    Api,
    ApiRequest,
    ApiResponse,
    int_param,
    json_response,
    no_content,
)
from emulators.providers.salesforce.schema import SCHEMA, SObject
from emulators.providers.salesforce.soql_exec import QueryEngine
from emulators.providers.salesforce.soql_parser import Func, Ref, SubQuery, parse

JOBS = "_sf_jobs"
DELIMITERS = {"COMMA": ",", "TAB": "\t", "PIPE": "|", "SEMICOLON": ";", "CARET": "^", "BACKQUOTE": "`"}
LINE_ENDINGS = {"LF": "\n", "CRLF": "\r\n"}
OPERATIONS = {"insert", "update", "upsert", "delete", "hardDelete"}
DEFAULT_MAX_RECORDS = 100000
MAX_UPLOAD = 150 * 1024 * 1024


def invalid_job(message: str, code: str = "InvalidJob") -> SfError:
    return SfError(400, code, message)


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in job.items() if not k.startswith("_") and k != "id_" and v is not None}


def _csv_text(rows: list[list[Any]], delimiter: str, line_ending: str, *, quote_all: bool = True) -> str:
    buf = io.StringIO()
    writer = csv.writer(
        buf,
        delimiter=delimiter,
        lineterminator=line_ending,
        quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL,
    )
    writer.writerows(rows)
    return buf.getvalue()


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def register(api: Api) -> None:
    r = api.router

    def _new_job(req: ApiRequest, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        seq = req.org.meta["seq"]
        seq["750"] = seq.get("750", 0) + 1
        job_id = salesforce_id("750", seq["750"])
        now = req.org.now_str()
        delimiter = str(body.get("columnDelimiter") or "COMMA").upper()
        ending = str(body.get("lineEnding") or "LF").upper()
        if delimiter not in DELIMITERS:
            raise invalid_job(f"Invalid columnDelimiter: {delimiter}")
        if ending not in LINE_ENDINGS:
            raise invalid_job(f"Invalid lineEnding: {ending}")
        if str(body.get("contentType") or "CSV").upper() != "CSV":
            raise invalid_job("Invalid contentType: only CSV is supported")
        job: dict[str, Any] = {
            "id": job_id,
            "operation": body.get("operation"),
            "object": body.get("object"),
            "createdById": req.session.user["Id"],
            "createdDate": now,
            "systemModstamp": now,
            "state": "Open" if kind == "ingest" else "UploadComplete",
            "externalIdFieldName": body.get("externalIdFieldName"),
            "concurrencyMode": "Parallel",
            "contentType": "CSV",
            "apiVersion": float(req.version),
            "jobType": "V2Ingest" if kind == "ingest" else "V2Query",
            "lineEnding": ending,
            "columnDelimiter": (body.get("columnDelimiter") and delimiter) or "COMMA",
            "numberRecordsProcessed": 0,
            "retries": 0,
            "totalProcessingTime": 0,
            "apiActiveProcessingTime": 0,
            "apexProcessingTime": 0,
            "_kind": kind,
            "_polls": 0,
        }
        if kind == "ingest":
            job["contentUrl"] = f"services/data/v{req.version}/jobs/ingest/{job_id}/batches"
            job["numberRecordsFailed"] = 0
        return job

    def _get(req: ApiRequest, kind: str) -> dict[str, Any]:
        job = req.org.coll(JOBS).get(req.params["id"])
        if job is None or job["_kind"] != kind:
            raise not_found()
        if not req.access.is_admin and job["createdById"] != req.session.user["Id"]:
            raise not_found()
        return job

    def _sync_state(req: ApiRequest, job: dict[str, Any]) -> None:
        """UploadComplete -> (InProgress ...) -> JobComplete as the client polls."""
        if job["state"] not in ("UploadComplete", "InProgress"):
            return
        if job.get("_failure"):
            job["state"] = "Failed"
            job["errorMessage"] = f"InvalidBatch : {job['_failure']}"
            job["systemModstamp"] = req.org.now_str()
            return
        wanted = api.ctx.setting("BULK_INPROGRESS_POLLS", "0") or "0"
        if job["_polls"] < int(wanted):
            job["_polls"] += 1
            job["state"] = "InProgress"
            return
        job["state"] = "JobComplete"
        job["systemModstamp"] = req.org.now_str()
        job.pop("contentUrl", None)

    # -- ingest -------------------------------------------------------------------------
    @r.route("POST", "jobs/ingest")
    def create_ingest(req: ApiRequest) -> ApiResponse:
        body = req.obj_json()
        operation = body.get("operation")
        if not isinstance(operation, str) or operation not in OPERATIONS:
            raise invalid_job(f"Invalid operation: {operation}")
        if not isinstance(body.get("object"), str):
            raise invalid_job(f"Invalid object: {body.get('object')}")
        obj = SCHEMA.get(str(body.get("object") or ""))
        if obj is None:
            raise invalid_job(f"Invalid object: {body.get('object')}", "InvalidJob")
        if operation == "upsert":
            ext = body.get("externalIdFieldName")
            f = obj.field(ext) if ext else None
            if f is None or not (f.name == "Id" or f.external_id or f.id_lookup or f.unique):
                raise invalid_job("Missing or invalid externalIdFieldName for upsert")
        if operation == "hardDelete" and not req.access.is_admin:
            raise invalid_job("The 'Bulk API Hard Delete' permission is required for hardDelete")
        body = {**body, "object": obj.name}
        job = _new_job(req, "ingest", body)
        req.org.coll(JOBS).put_as(job["id"], job)
        return json_response(_job_view(job), 200)

    @r.route("GET", "jobs/ingest")
    def list_ingest(req: ApiRequest) -> ApiResponse:
        mine = [
            j
            for j in req.org.coll(JOBS)
            if j["_kind"] == "ingest" and (req.access.is_admin or j["createdById"] == req.session.user["Id"])
        ]
        for j in mine:
            _sync_state(req, j)
        return json_response({"done": True, "records": [_job_view(j) for j in mine], "nextRecordsUrl": None})

    @r.route("GET", "jobs/ingest/{id}")
    def get_ingest(req: ApiRequest) -> ApiResponse:
        job = _get(req, "ingest")
        _sync_state(req, job)
        return json_response(_job_view(job))

    @r.route("PUT", "jobs/ingest/{id}/batches")
    def upload(req: ApiRequest) -> ApiResponse:
        job = _get(req, "ingest")
        if job["state"] != "Open":
            raise invalid_job(
                f"Job state is {job['state']}; data can only be uploaded to an Open job", "InvalidBatch"
            )
        if len(req.body) > MAX_UPLOAD:
            raise SfError(413, "InvalidBatch", "Request entity too large")
        if job.get("_csv") is not None:
            raise invalid_job("Data has already been uploaded to this job", "InvalidBatch")
        text = req.body.decode("utf-8-sig", errors="replace")
        job["_csv"] = text
        return ApiResponse(201, None, media_type="application/json")

    @r.route("PATCH", "jobs/ingest/{id}")
    def patch_ingest(req: ApiRequest) -> ApiResponse:
        job = _get(req, "ingest")
        state = req.obj_json().get("state")
        if state == "Aborted":
            if job["state"] in ("JobComplete", "Failed"):
                raise invalid_job(f"Cannot abort a job in state {job['state']}")
            job["state"] = "Aborted"
            job["_unprocessed"] = job.get("_csv") or ""
            job["systemModstamp"] = req.org.now_str()
            return json_response(_job_view(job))
        if state != "UploadComplete":
            raise invalid_job(f"Invalid state: {state}")
        if job["state"] != "Open":
            raise invalid_job(f"Job state is {job['state']}; expected Open")
        if job.get("_csv") is None:
            raise invalid_job("No data has been uploaded to this job", "InvalidBatch")
        job["state"] = "UploadComplete"
        _process_ingest(req, job)
        return json_response(_job_view(job))

    @r.route("DELETE", "jobs/ingest/{id}")
    def delete_ingest(req: ApiRequest) -> ApiResponse:
        job = _get(req, "ingest")
        _sync_state(req, job)
        if job["state"] not in ("JobComplete", "Aborted", "Failed"):
            raise invalid_job(f"Cannot delete a job in state {job['state']}")
        req.org.coll(JOBS).delete(job["id"])
        return no_content()

    def _result(req: ApiRequest, key: str) -> ApiResponse:
        job = _get(req, "ingest")
        _sync_state(req, job)
        if job["state"] not in ("JobComplete", "Aborted", "Failed"):
            raise invalid_job(f"Job state is {job['state']}; results are not available yet", "InvalidJob")
        return ApiResponse(
            200,
            job.get(key, ""),
            {"Cache-Control": "no-cache"},
            media_type="text/csv; charset=UTF-8",
        )

    @r.route("GET", "jobs/ingest/{id}/successfulResults")
    def successful(req: ApiRequest) -> ApiResponse:
        return _result(req, "_success")

    @r.route("GET", "jobs/ingest/{id}/failedResults")
    def failed(req: ApiRequest) -> ApiResponse:
        return _result(req, "_failed")

    @r.route("GET", "jobs/ingest/{id}/unprocessedrecords", "jobs/ingest/{id}/unprocessedRecords")
    def unprocessed(req: ApiRequest) -> ApiResponse:
        return _result(req, "_unprocessed")

    # -- query ----------------------------------------------------------------------------
    @r.route("POST", "jobs/query")
    def create_query(req: ApiRequest) -> ApiResponse:
        body = req.obj_json()
        operation = body.get("operation")
        if not isinstance(operation, str) or operation not in ("query", "queryAll"):
            raise invalid_job(f"Invalid operation: {operation}")
        text = body.get("query")
        if not text or not isinstance(text, str):
            raise invalid_job("Missing required field: query")
        try:
            ast = parse(text)
        except SfError as exc:
            raise SfError(400, "InvalidJob", exc.message) from exc
        obj = SCHEMA.get(ast.from_name)
        job = _new_job(req, "query", {**body, "object": obj.name if obj else ast.from_name})
        job["query"] = text
        job.pop("externalIdFieldName", None)
        _run_query_job(req, job, ast.from_name, operation == "queryAll")
        req.org.coll(JOBS).put_as(job["id"], job)
        view = _job_view(job)
        view.pop("numberRecordsProcessed", None)
        view.pop("retries", None)
        view.pop("totalProcessingTime", None)
        view.pop("apiActiveProcessingTime", None)
        view.pop("apexProcessingTime", None)
        view.pop("query", None)
        return json_response(view)

    @r.route("GET", "jobs/query")
    def list_query(req: ApiRequest) -> ApiResponse:
        rows = []
        for j in req.org.coll(JOBS):
            if j["_kind"] == "query" and (req.access.is_admin or j["createdById"] == req.session.user["Id"]):
                _sync_state(req, j)
                rows.append(_job_view(j))
        return json_response({"done": True, "records": rows, "nextRecordsUrl": None})

    @r.route("GET", "jobs/query/{id}")
    def get_query(req: ApiRequest) -> ApiResponse:
        job = _get(req, "query")
        _sync_state(req, job)
        return json_response(_job_view(job))

    @r.route("PATCH", "jobs/query/{id}")
    def patch_query(req: ApiRequest) -> ApiResponse:
        job = _get(req, "query")
        if req.obj_json().get("state") != "Aborted":
            raise invalid_job("Only state Aborted is allowed")
        if job["state"] in ("JobComplete", "Failed"):
            raise invalid_job(f"Cannot abort a job in state {job['state']}")
        job["state"] = "Aborted"
        return json_response(_job_view(job))

    @r.route("DELETE", "jobs/query/{id}")
    def delete_query(req: ApiRequest) -> ApiResponse:
        job = _get(req, "query")
        _sync_state(req, job)
        if job["state"] not in ("JobComplete", "Aborted", "Failed"):
            raise invalid_job(f"Cannot delete a job in state {job['state']}")
        req.org.coll(JOBS).delete(job["id"])
        return no_content()

    @r.route("GET", "jobs/query/{id}/results")
    def query_results(req: ApiRequest) -> ApiResponse:
        job = _get(req, "query")
        _sync_state(req, job)
        if job["state"] == "Failed":
            raise invalid_job(job.get("errorMessage", "Query job failed"))
        if job["state"] != "JobComplete":
            raise invalid_job(f"Job state is {job['state']}; results are not available yet")
        rows: list[list[str]] = job["_rows"]
        offset = 0
        locator = req.q("locator")
        if locator and locator != "null":
            try:
                offset = int(base64.urlsafe_b64decode(locator.encode()).decode().split(":")[1])
            except (ValueError, IndexError) as exc:
                raise invalid_job("Invalid locator") from exc
        limit = int_param(req.q("maxRecords") or DEFAULT_MAX_RECORDS, "maxRecords", minimum=1)
        chunk = rows[offset : offset + limit]
        more = offset + limit < len(rows)
        delimiter = DELIMITERS[job["columnDelimiter"]]
        ending = LINE_ENDINGS[job["lineEnding"]]
        text = _csv_text([job["_header"], *chunk], delimiter, ending)
        next_locator = (
            base64.urlsafe_b64encode(f"offset:{offset + limit}".encode()).decode() if more else "null"
        )
        return ApiResponse(
            200,
            text,
            {"Sforce-NumberOfRecords": str(len(chunk)), "Sforce-Locator": next_locator},
            media_type="text/csv; charset=UTF-8",
        )


# -- query execution ---------------------------------------------------------------------------


def _run_query_job(req: ApiRequest, job: dict[str, Any], from_name: str, all_rows: bool) -> None:
    engine = QueryEngine(req.org, req.access, req.version)
    try:
        ast = parse(job["query"])
        for item in ast.select:
            if isinstance(item, SubQuery):
                raise invalid_job("Relationship subqueries are not supported by Bulk API 2.0 queries")
            if (isinstance(item, Func) and item.name != "FIELDS") or ast.group_by:
                raise invalid_job(
                    "Aggregate functions and GROUP BY are not supported by Bulk API 2.0 queries"
                )
        result = engine.run(ast, all_rows=all_rows)
    except SfError as exc:
        job["_failure"] = exc.message  # surfaces as state=Failed on the next poll
        job["_header"], job["_rows"] = [], []
        return
    obj = SCHEMA.get(ast.from_name)
    paths = _select_paths(ast, obj) if obj else []
    rows = [[_cell(_dig(rec, p)) for p in paths] for rec in result.records]
    job["_header"] = paths
    job["_rows"] = rows
    job["numberRecordsProcessed"] = len(rows)
    job["totalProcessingTime"] = 25 + len(rows)
    job["apiActiveProcessingTime"] = 0
    job["apexProcessingTime"] = 0


def _select_paths(ast: Any, obj: SObject) -> list[str]:
    paths: list[str] = []
    for item in ast.select:
        if isinstance(item, Ref):
            paths.append(
                item.dotted
                if len(item.parts) > 1
                else (obj.field(item.parts[0]).name if obj.field(item.parts[0]) else item.dotted)
            )
        elif isinstance(item, Func) and item.name == "FIELDS":
            kind = item.args[0].dotted.upper() if item.args else "ALL"
            chosen = {"ALL": obj.fields, "STANDARD": obj.standard_fields, "CUSTOM": obj.custom_fields}.get(
                kind, []
            )
            paths.extend(f.name for f in chosen if f.type != "address")
        elif isinstance(item, Func) and item.args and isinstance(item.args[0], Ref):
            paths.append(item.args[0].dotted)
    return paths


def _dig(rec: dict[str, Any], path: str) -> Any:
    cur: Any = rec
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        match = next((k for k in cur if k.lower() == part.lower()), None)
        if match is None:
            return None
        cur = cur[match]
    return cur


# -- ingest execution -----------------------------------------------------------------------------


def _fail_ingest(job: dict[str, Any], message: str) -> None:
    """Reject the whole upload; the job turns ``Failed`` on the next poll, like a real asynchronous failure."""
    job["_failure"] = message
    job["_polls"] = 0
    job["_success"] = job["_failed"] = job["_unprocessed"] = ""


def _header_problem(obj: SObject, operation: str, header: list[str]) -> str | None:
    """A job-level problem with the CSV header (unknown / duplicate / blank columns), else ``None``."""
    seen: set[str] = set()
    for column in header:
        if not column:
            return "Field name not found : (blank column header)"
        key = column.lower()
        if key in seen:
            return f"Duplicate field specified : {column}"
        seen.add(key)
        if "." in column:
            rel, _, target = column.partition(".")
            if obj.rel(rel) is None or not target:
                return f"Field name not found : {column}"
        elif obj.field(column) is None:
            return f"Field name not found : {column}"
    if operation in ("update", "delete", "hardDelete") and "id" not in seen:
        return "Id field is required for this operation"
    return None


def _process_ingest(req: ApiRequest, job: dict[str, Any]) -> None:
    obj = SCHEMA.objects[job["object"]]
    operation = job["operation"]
    delimiter = DELIMITERS[job["columnDelimiter"]]
    ending = LINE_ENDINGS[job["lineEnding"]]
    reader = csv.reader(io.StringIO(job["_csv"], newline=""), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        _fail_ingest(job, "The CSV file is empty")
        return
    header = [h.strip() for h in header]
    problem = _header_problem(obj, operation, header)
    if problem:
        _fail_ingest(job, problem)
        return
    rows = [row for row in reader if any(cell != "" for cell in row)]
    dml = Dml(req.org, req.access, req.txn)
    ok_rows: list[list[Any]] = []
    bad_rows: list[list[Any]] = []
    for row in rows:
        data = dict(zip(header, row + [""] * (len(header) - len(row)), strict=False))
        sp = req.txn.savepoint()
        try:
            rid, created = _apply_row(req, dml, obj, operation, job.get("externalIdFieldName"), data)
            ok_rows.append([rid, "true" if created else "false", *row])
        except SfError as exc:
            req.txn.rollback(sp)
            fields = ",".join(exc.fields or [])
            bad_rows.append(["", f"{exc.code}:{exc.message}:{fields} --", *row])
    job["numberRecordsProcessed"] = len(rows)
    job["numberRecordsFailed"] = len(bad_rows)
    job["totalProcessingTime"] = 50 + 5 * len(rows)
    job["_success"] = _csv_text([["sf__Id", "sf__Created", *header], *ok_rows], delimiter, ending)
    job["_failed"] = _csv_text([["sf__Id", "sf__Error", *header], *bad_rows], delimiter, ending)
    job["_unprocessed"] = _csv_text([header], delimiter, ending)
    job["_polls"] = 0


def _apply_row(
    req: ApiRequest, dml: Dml, obj: SObject, operation: str, ext_field: str | None, data: dict[str, str]
) -> tuple[str, bool]:
    fields: dict[str, Any] = {}
    record_id: str | None = None
    for column, raw in data.items():
        if raw == "":
            continue
        value: Any = None if raw == "#N/A" else raw
        if column.lower() == "id":
            record_id = raw
            continue
        if "." in column:
            if value is None:
                continue
            ref_field, rid = dml.resolve_lookup(obj, *column.partition(".")[::2], raw)
            fields[ref_field] = rid
            continue
        fields[column] = value
    if operation == "insert":
        return dml.insert(obj.name, fields)["Id"], True
    if operation in ("delete", "hardDelete"):
        if not record_id:
            raise SfError(400, "MISSING_ARGUMENT", "Id not specified in an update call", ["Id"])
        norm = norm_id(record_id)
        if norm is None or not norm.startswith(obj.prefix):
            raise SfError(400, "MALFORMED_ID", f"Id: id value of incorrect type: {record_id}", ["Id"])
        if operation == "hardDelete":
            dml.purge(obj.name, norm)
        else:
            dml.delete(obj.name, norm)
        return norm, False
    if operation == "update":
        if not record_id:
            raise SfError(400, "MISSING_ARGUMENT", "Id not specified in an update call", ["Id"])
        norm = norm_id(record_id)
        if norm is None:
            raise SfError(400, "MALFORMED_ID", f"Id: id value of incorrect type: {record_id}", ["Id"])
        dml.update(obj.name, norm, fields)
        return norm, False
    # upsert
    assert ext_field is not None
    key = (
        record_id
        if ext_field.lower() == "id"
        else fields.get(next((k for k in fields if k.lower() == ext_field.lower()), ""))
    )
    if key is None:
        raise SfError(
            400, "MISSING_ARGUMENT", f"Missing value for external id field {ext_field}", [ext_field]
        )
    matches = dml.find_by_external(obj, ext_field, key)
    if len(matches) > 1:
        raise SfError(
            400, "DUPLICATE_VALUE", f"More than one record found for external id {key}", [ext_field]
        )
    if matches:
        dml.update(
            obj.name, matches[0]["Id"], {k: v for k, v in fields.items() if k.lower() != ext_field.lower()}
        )
        return matches[0]["Id"], False
    return dml.insert(obj.name, fields)["Id"], True
