"""The ``/v3/company/{realmId}`` API surface: CRUD, query, batch, CDC, reports."""

from __future__ import annotations

import json
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from typing import Any

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from emulators.core.clock import parse_instant, utc
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.core.webhooks import hmac_sha256_b64
from emulators.providers.quickbooks import crud
from emulators.providers.quickbooks.common import (
    BY_LOWER,
    BY_PATH,
    auth_failure,
    fail,
    fault_body,
    invoice_link,
    money,
    not_found,
    now_text,
    parse_time,
    public,
    realm,
    require_actor,
    respond,
    touch,
    user_for_grant,
    wrap,
)
from emulators.providers.quickbooks.ledger import refresh_computed
from emulators.providers.quickbooks.query import run_query
from emulators.providers.quickbooks.reports import build_report

WRITE_BLOCKED_MESSAGE = "User does not have permission to create or edit transactions in this company."


def check_read(ctx: ProviderContext, request: Request, realm_id: str) -> Any:
    minor = request.query_params.get("minorversion")
    if minor is not None:
        try:
            value = int(minor)
        except ValueError:
            value = 0
        if value < 1 or value > 75:
            fail(
                ctx,
                400,
                "Invalid minorversion",
                f"minorversion {minor} is not supported; supported versions are 1 through 75.",
                "6180",
            )
    return require_actor(ctx, request, realm_id)


def check_write(ctx: ProviderContext, request: Request, realm_id: str) -> Any:
    grant = check_read(ctx, request, realm_id)
    if not grant.app_only:
        user = user_for_grant(ctx, grant)
        if user is not None and user.get("role") != "full":
            fail(
                ctx,
                403,
                "Authorization Failure",
                f"Authorization Failure : {WRITE_BLOCKED_MESSAGE}",
                "110",
                fault_type="AuthenticationFault",
            )
    return grant


async def emit_webhooks(ctx: ProviderContext, events: list[crud.Event]) -> None:
    if not events:
        return
    configs = ctx.store.collection("_webhook_config").values()
    if not configs:
        return
    entities = [
        {
            "name": entity,
            "id": str(record_id),
            "operation": operation,
            "lastUpdated": _webhook_time(ctx),
        }
        for entity, record_id, operation in events
    ]
    for config in configs:
        wanted = config.get("entities")
        selected = [e for e in entities if not wanted or e["name"] in wanted]
        if not selected:
            continue
        body = json.dumps(
            {"eventNotifications": [{"realmId": realm(ctx), "dataChangeEvent": {"entities": selected}}]},
            separators=(",", ":"),
        ).encode()
        token = str(config.get("verifier_token") or "")

        def sign(payload: bytes, _attempt: int, tok: str = token) -> dict[str, str]:
            return {"intuit-signature": hmac_sha256_b64(tok, payload)} if tok else {}

        await ctx.webhooks.deliver(
            str(config["url"]),
            body,
            event_type="quickbooks.dataChangeEvent",
            headers={"intuit-signature": hmac_sha256_b64(token, body)} if token else {},
            sign=sign if token else None,
            metadata={"realmId": realm(ctx)},
        )


def _webhook_time(ctx: ProviderContext) -> str:
    instant = utc(ctx.clock.now())
    return instant.strftime("%Y-%m-%dT%H:%M:%S.") + f"{instant.microsecond // 1000:03d}Z"


async def _json_body(ctx: ProviderContext, request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        fail(ctx, 400, "Invalid JSON", "Request body must be a JSON object.", "2010")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        fail(ctx, 400, "Invalid JSON", "Cannot parse the JSON request body.", "2010")
    if not isinstance(data, dict):
        fail(ctx, 400, "Invalid JSON", "Request body must be a JSON object.", "2010")
    return data


def _entity_from_path(segment: str) -> str | None:
    return BY_PATH.get(segment.lower()) or BY_LOWER.get(segment.lower())


def register_api(app: FastAPI, ctx: ProviderContext) -> None:
    base = "/v3/company/{realm_id}"

    @app.middleware("http")
    async def response_headers(request: Request, call_next: Any) -> Response:
        request.scope.setdefault("state", {})["quickbooks_time"] = now_text(ctx)
        response = await call_next(request)
        response.headers.setdefault("intuit_tid", ctx.ids.hex(32))
        return response

    @app.get(f"{base}/companyinfo/{{company_id}}")
    async def get_company_info(request: Request, realm_id: str, company_id: str) -> Response:
        check_read(ctx, request, realm_id)
        if str(company_id) != realm(ctx):
            not_found(ctx, "CompanyInfo")
        record = ctx.store.collection("CompanyInfo").get(realm(ctx))
        assert record is not None
        return respond(ctx, request, wrap(ctx, "CompanyInfo", record))

    @app.get(f"{base}/preferences")
    async def get_preferences(request: Request, realm_id: str) -> Response:
        check_read(ctx, request, realm_id)
        record = next(iter(ctx.store.collection("Preferences")), None)
        if record is None:
            not_found(ctx, "Preferences")
        assert record is not None
        return respond(ctx, request, wrap(ctx, "Preferences", record))

    @app.post(f"{base}/preferences")
    async def update_preferences(request: Request, realm_id: str) -> Response:
        check_write(ctx, request, realm_id)
        payload = await _json_body(ctx, request)
        record, _ = crud.update_entity(ctx, "Preferences", payload)
        return respond(ctx, request, wrap(ctx, "Preferences", record))

    @app.get(f"{base}/reports/{{report_name}}")
    async def report(request: Request, realm_id: str, report_name: str) -> Response:
        check_read(ctx, request, realm_id)
        refresh_computed(ctx)
        params = {k: v for k, v in request.query_params.items()}
        body = build_report(ctx, report_name, params)
        # Reports are not wrapped in an entity envelope; no top-level time in real QBO
        headers = {"intuit_tid": ctx.ids.hex(32)}
        return JSONResponse(body, headers=headers)

    @app.api_route(f"{base}/query", methods=["GET", "POST"])
    async def query(request: Request, realm_id: str) -> Response:
        grant = check_read(ctx, request, realm_id)
        _ = grant
        refresh_computed(ctx)
        text = request.query_params.get("query")
        if text is None and request.method == "POST":
            raw = await request.body()
            text = raw.decode("utf-8", errors="replace").strip()
            # python-quickbooks POSTs the raw SQL as application/text
        if not text:
            fail(ctx, 400, "Error parsing query", "QueryParserError: query is empty", "4000")
        assert text is not None
        result = run_query(ctx, text)
        return respond(ctx, request, {"QueryResponse": result, "time": now_text(ctx)})

    @app.post(f"{base}/batch")
    async def batch(request: Request, realm_id: str) -> Response:
        check_read(ctx, request, realm_id)
        payload = await _json_body(ctx, request)
        items = payload.get("BatchItemRequest")
        if not isinstance(items, list) or not items:
            fail(
                ctx, 400, "Batch request is empty", "BatchItemRequest must contain at least one item.", "6000"
            )
        assert isinstance(items, list)
        if len(items) > 30:
            fail(ctx, 400, "Batch Items Limit Exceeded", "A batch may contain at most 30 items.", "6140")
        responses: list[dict[str, Any]] = []
        all_events: list[crud.Event] = []
        for item in items:
            bid = str(item.get("bId", "")) if isinstance(item, dict) else ""
            entry: dict[str, Any] = {"bId": bid}
            try:
                if not isinstance(item, dict):
                    fail(ctx, 400, "Invalid batch item", "Each batch item must be an object.", "2010")
                if "Query" in item:
                    refresh_computed(ctx)
                    entry["QueryResponse"] = run_query(ctx, item["Query"])
                else:
                    check_write(ctx, request, realm_id)
                    # entity key is the canonical name, e.g. "Invoice"
                    canonical = next((k for k in item if _entity_from_path(k) is not None), None)
                    if canonical is None:
                        fail(ctx, 400, "Invalid batch item", "An entity payload is required.", "2020")
                    entity_name = _entity_from_path(canonical)
                    assert entity_name is not None
                    operation = str(item.get("operation") or "create").lower()
                    body_payload = item[canonical]
                    if not isinstance(body_payload, dict):
                        fail(ctx, 400, "Invalid JSON", "Entity payload must be an object.", "2010")
                    if operation == "create":
                        record, events = crud.create_entity(ctx, entity_name, body_payload)
                    elif operation == "delete":
                        record, events = crud.delete_entity(ctx, entity_name, body_payload)
                    elif operation == "void":
                        record, events = crud.void_entity(ctx, entity_name, body_payload)
                    elif operation == "update":
                        if not body_payload.get("Id"):
                            fail(ctx, 400, "Required param missing", "Id is required for update.", "2020")
                        record, events = crud.update_entity(ctx, entity_name, body_payload)
                    else:
                        fail(ctx, 400, "Invalid operation", "Unsupported batch operation.", "6000")
                    all_events.extend(events)
                    if record.get("_deleted"):
                        entry[entity_name] = {"Id": record.get("Id"), "status": "Deleted", "domain": "QBO"}
                    else:
                        entry[entity_name] = public(record)
            except EmulatorError as exc:
                body = (
                    exc.body
                    if isinstance(exc.body, dict)
                    else fault_body(ctx, "Error", str(exc.body), "6000")
                )
                entry["Fault"] = body.get("Fault") or body.get("fault") or body
            responses.append(entry)
        await emit_webhooks(ctx, all_events)
        return respond(ctx, request, {"BatchItemResponse": responses, "time": now_text(ctx)})

    @app.get(f"{base}/cdc")
    async def cdc(request: Request, realm_id: str) -> Response:
        check_read(ctx, request, realm_id)
        refresh_computed(ctx)
        entity_names = [
            e.strip() for e in (request.query_params.get("entities") or "").split(",") if e.strip()
        ]
        if not entity_names:
            fail(ctx, 400, "Required param missing", "entities is required.", "2020")
        since_text = request.query_params.get("changedSince")
        since = parse_time(since_text) if since_text else None
        if since is None:
            fail(ctx, 400, "Invalid changedSince", "changedSince must be an ISO-8601 timestamp.", "6000")
        assert since is not None
        if utc(since) > utc(ctx.clock.now()):
            fail(ctx, 400, "Invalid changedSince", "changedSince must not be in the future.", "6000")
        if utc(ctx.clock.now()) - utc(since) > timedelta(days=30):
            fail(
                ctx,
                400,
                "CDC query too old",
                "Change data capture only returns entities changed in the last 30 days.",
                "6200",
            )
        query_responses: list[dict[str, Any]] = []
        for raw_name in entity_names:
            entity = _entity_from_path(raw_name)
            if entity is None or entity in (
                "TimeActivity",
                "TaxCode",
                "TaxRate",
                "Attachable",
                "CompanyInfo",
                "Preferences",
            ):
                fail(ctx, 400, "Invalid entity", f"CDC is not supported for {raw_name}.", "6000")
            changed: list[Any] = []
            deleted: list[dict[str, Any]] = []
            for record in ctx.store.collection(entity):
                updated = parse_time((record.get("MetaData") or {}).get("LastUpdatedTime"))
                if updated is None or utc(updated) < utc(since):
                    continue
                if record.get("_deleted"):
                    deleted.append(
                        {"Id": record.get("Id"), "status": "Deleted", "MetaData": record.get("MetaData")}
                    )
                else:
                    changed.append(public(record))
            items = [*changed, *deleted]
            if not items:
                continue
            query_responses.append(
                {
                    entity: items,
                    "startPosition": 1,
                    "maxResults": len(items),
                    "totalCount": len(items),
                }
            )
        body = {"CDCResponse": [{"QueryResponse": query_responses}], "time": now_text(ctx)}
        return respond(ctx, request, body)

    @app.post(f"{base}/upload")
    async def upload(request: Request, realm_id: str) -> Response:
        check_write(ctx, request, realm_id)
        raw = await request.body()
        meta = _multipart_metadata(ctx, raw, request.headers.get("content-type", ""))
        record, events = crud.create_entity(ctx, "Attachable", meta)
        await emit_webhooks(ctx, events)
        return respond(
            ctx,
            request,
            {"AttachableResponse": [{"Attachable": public(record)}], "time": now_text(ctx)},
        )

    # -- generic entity routes --------------------------------------------------

    @app.get(f"{base}/{{segment}}/{{record_id}}/pdf")
    async def pdf(request: Request, realm_id: str, segment: str, record_id: str) -> Response:
        check_read(ctx, request, realm_id)
        entity = _entity_from_path(segment)
        if entity not in ("Invoice", "Estimate", "SalesReceipt", "CreditMemo"):
            not_found(ctx, segment)
        assert entity is not None
        refresh_computed(ctx)
        record = crud.get_record(ctx, entity, record_id)
        blob_key = record.get("_pdf_blob")
        if not blob_key or not ctx.store.blobs.has(str(blob_key)):
            data = _render_pdf(ctx, entity, record)
            blob_key = ctx.store.blobs.put(data)
            record["_pdf_blob"] = blob_key
        return Response(
            ctx.store.blobs.get(str(blob_key)),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{entity}_{record_id}.pdf"',
                "intuit_tid": ctx.ids.hex(32),
            },
        )

    @app.post(f"{base}/{{segment}}/{{record_id}}/send")
    async def send_entity(request: Request, realm_id: str, segment: str, record_id: str) -> Response:
        check_write(ctx, request, realm_id)
        entity = _entity_from_path(segment)
        if entity not in ("Invoice", "Estimate", "SalesReceipt", "CreditMemo"):
            not_found(ctx, segment)
        assert entity is not None
        record = crud.get_record(ctx, entity, record_id)
        send_to = request.query_params.get("sendTo")
        recipient = send_to or (record.get("BillEmail") or {}).get("Address")
        if not recipient or "@" not in recipient or any(c in recipient for c in "\r\n"):
            fail(ctx, 400, "Business Validation Error", "A valid email recipient is required.", "6000")
        if send_to:
            record["BillEmail"] = {"Address": send_to}
        record["EmailStatus"] = "EmailSent"
        record["DeliveryInfo"] = {
            "DeliveryType": "Email",
            "DeliveryTime": now_text(ctx),
            "DeliveryAddress": (record.get("BillEmail") or {}).get("Address", ""),
        }
        touch(ctx, record)
        await emit_webhooks(ctx, [(entity, record_id, "Update")])
        return respond(ctx, request, wrap(ctx, entity, record))

    @app.get(f"{base}/{{segment}}/{{record_id}}")
    async def get_entity(request: Request, realm_id: str, segment: str, record_id: str) -> Response:
        check_read(ctx, request, realm_id)
        entity = _entity_from_path(segment)
        if entity is None:
            not_found(ctx, segment)
        assert entity is not None
        refresh_computed(ctx)
        record = crud.get_record(ctx, entity, record_id)
        body = wrap(ctx, entity, record)
        include = (request.query_params.get("include") or "").lower()
        if entity == "Invoice" and "invoicelink" in include:
            body[entity]["InvoiceLink"] = invoice_link(ctx, request, record_id)
        return respond(ctx, request, body)

    @app.post(f"{base}/{{segment}}")
    async def upsert_entity(request: Request, realm_id: str, segment: str) -> Response:
        # Attachable upload is a POST to /upload; plain attachable create is JSON
        check_write(ctx, request, realm_id)
        entity = _entity_from_path(segment)
        if entity is None:
            not_found(ctx, segment)
        assert entity is not None
        payload = await _json_body(ctx, request)
        operation = (request.query_params.get("operation") or "").lower() or None
        if operation not in (None, "create", "update", "delete", "void"):
            fail(ctx, 400, "Invalid operation", "Unsupported operation.", "6000")
        if operation == "update" and not payload.get("Id"):
            fail(ctx, 400, "Required param missing", "Id is required for update.", "2020")
        if entity in ("CompanyInfo", "Preferences") and not payload.get("Id"):
            fail(ctx, 400, "Required param missing", "Id is required for singleton updates.", "2020")
        include = (request.query_params.get("include") or "").lower() or None
        if payload.get("Id") or operation in ("delete", "void"):
            record, events = crud.update_entity(ctx, entity, payload, operation=operation, include=include)
        else:
            record, events = crud.create_entity(ctx, entity, payload)
        await emit_webhooks(ctx, events)
        if record.get("_deleted"):
            body = {
                entity: {"Id": record.get("Id"), "status": "Deleted", "domain": "QBO"},
                "time": now_text(ctx),
            }
            return respond(ctx, request, body)
        body = wrap(ctx, entity, record)
        if entity == "Invoice" and include and "invoicelink" in include:
            body[entity]["InvoiceLink"] = invoice_link(ctx, request, str(record.get("Id")))
        return respond(ctx, request, body)


def _entity_keys(item: dict[str, Any]) -> list[str]:
    return [k for k in item if k not in ("bId", "operation", "Query")]


def _multipart_metadata(ctx: ProviderContext, raw: bytes, content_type: str) -> dict[str, Any]:
    if not content_type.lower().startswith("multipart/form-data"):
        fail(ctx, 400, "Invalid upload", "Upload requires multipart/form-data.", "2010")
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw
    )
    if message.is_multipart():
        for part in message.iter_parts():
            if part.get_content_type() != "application/json":
                continue
            try:
                data = json.loads(part.get_payload(decode=True) or b"")
            except (ValueError, UnicodeDecodeError, RecursionError):
                fail(ctx, 400, "Invalid JSON", "Cannot parse attachment metadata.", "2010")
            if not isinstance(data, dict):
                fail(ctx, 400, "Invalid JSON", "Attachment metadata must be an object.", "2010")
            return data
    fail(ctx, 400, "Invalid upload", "A JSON metadata part is required.", "2020")


def _render_pdf(ctx: ProviderContext, entity: str, record: dict[str, Any]) -> bytes:
    from emulators.world.documents import render

    org = ctx.world.org
    doc = record.get("DocNumber") or record.get("Id")
    cust = record.get("CustomerRef") or {}
    title = f"{entity} {doc} - {org.name}"
    paragraphs = [
        f"{org.name} ({org.legal_name})",
        f"{entity} #: {doc}",
        f"Date: {record.get('TxnDate', '')}  Due: {record.get('DueDate', '')}",
        f"Bill To: {cust.get('name') or ''}",
        "",
    ]
    table: list[list[object]] = [["Description", "Qty", "Rate", "Amount"]]
    for line in record.get("Line") or []:
        if line.get("DetailType") == "SalesItemLineDetail":
            detail = line.get("SalesItemLineDetail") or {}
            table.append(
                [
                    line.get("Description") or "",
                    detail.get("Qty", ""),
                    detail.get("UnitPrice", ""),
                    money(line.get("Amount")),
                ]
            )
    tax = money((record.get("TxnTaxDetail") or {}).get("TotalTax"))
    paragraphs.append(f"Tax: {tax:.2f}")
    paragraphs.append(f"Total: {money(record.get('TotalAmt')):.2f}")
    paragraphs.append(f"Balance due: {money(record.get('Balance')):.2f}")
    memo = record.get("CustomerMemo") or {}
    if isinstance(memo, dict) and memo.get("value"):
        paragraphs.append(str(memo["value"]))
    data, _mime = render("pdf", title, paragraphs, author=org.name, table=table)
    return data


_ = auth_failure
_ = parse_instant
