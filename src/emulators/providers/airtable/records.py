"""Typed record writes, opaque list cursors, and blob-backed attachment cells."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import secrets
from datetime import date
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.auth import Grant
from emulators.core.clock import parse_instant
from emulators.core.provider import ProviderContext, url_for_host
from emulators.world.documents import render

from .common import (
    base_access,
    body,
    fail,
    field_for,
    finite,
    integer,
    invalid,
    new_id,
    record_for,
    records,
    table_for,
    text,
    timestamp,
)
from .formulas import Formula, field_map, values


def boolean(value: Any, name: str, *, query: bool = False) -> bool:
    if query and value in ("true", "false", "1", "0"):
        return value in ("true", "1")
    if not isinstance(value, bool):
        invalid(f"{name} must be a boolean.")
    return value


def ensure_blob(ctx: ProviderContext, attachment: dict[str, Any]) -> None:
    if "_blob" in attachment:
        return
    if attachment.get("_world"):
        doc = ctx.world.document(attachment["_world"])
        content, mime = render(
            doc.kind,
            doc.title,
            list(doc.paragraphs),
            created=doc.created,
            table=[list(row) for row in doc.table] if doc.table else None,
        )
    else:
        kind = attachment["filename"].rsplit(".", 1)[-1].lower()
        if kind not in {"docx", "xlsx", "pptx", "pdf", "csv", "md", "txt", "png"}:
            kind = "txt"
        content, mime = render(
            kind, attachment["filename"], ["Synthetic Airtable attachment"], created=ctx.world.anchor
        )
    attachment.update(_blob=ctx.store.blobs.put(content), type=mime, size=len(content))


def attachment_wire(ctx: ProviderContext, request: Request, key: str) -> dict[str, Any]:
    attachment = ctx.store.collection("attachments").get(key)
    assert attachment is not None
    ensure_blob(ctx, attachment)
    token = secrets.token_urlsafe(32)
    ctx.store.collection("_download_links").put(
        {
            "id": token,
            "attachment": key,
            "expires": ctx.clock.timestamp() + 7200,
        }
    )
    return {
        "id": key,
        "filename": attachment["filename"],
        "size": attachment["size"],
        "type": attachment["type"],
        "url": url_for_host(request, ctx, "v5.airtableusercontent.com", f"/attachments/{key}?token={token}"),
    }


def cell(
    ctx: ProviderContext, table: dict[str, Any], field: dict[str, Any], value: Any, *, typecast: bool
) -> Any:
    kind = field["type"]
    name = field["name"]
    if kind == "formula":
        invalid(
            f'Field "{name}" cannot accept a value because the field is computed.', "INVALID_VALUE_FOR_COLUMN"
        )
    if value is None or value == "":
        return None
    if kind in {"singleLineText", "multilineText", "email", "url", "phoneNumber"}:
        return text(value, name)
    if kind in {"number", "currency", "percent"}:
        if typecast and isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                invalid(f'Cannot parse value for field "{name}".', "INVALID_VALUE_FOR_COLUMN")
        value = finite(value, name)
        return round(value, field.get("options", {}).get("precision", 2))
    if kind == "checkbox":
        if typecast and value in ("true", "false", "1", "0", 1, 0):
            value = value in ("true", "1", 1)
        return boolean(value, name)
    if kind == "singleSelect":
        value = text(value, name)
        if value not in [choice["name"] for choice in field["options"]["choices"]]:
            invalid(
                f'Insufficient permissions to create new select option "{value}"',
                "INVALID_MULTIPLE_CHOICE_OPTIONS",
            )
        return value
    if kind in {"date", "dateTime"}:
        value = text(value, name)
        try:
            return (
                date.fromisoformat(value).isoformat()
                if kind == "date"
                else parse_instant(value).isoformat().replace("+00:00", "Z")
            )
        except (ValueError, OverflowError):
            invalid(f'Cannot parse date for field "{name}".', "INVALID_VALUE_FOR_COLUMN")
    if kind == "singleCollaborator":
        if not isinstance(value, dict) or set(value) - {"id", "email", "name"}:
            invalid(f'Invalid collaborator in "{name}".', "INVALID_VALUE_FOR_COLUMN")
        user = next(
            (
                u
                for u in ctx.store.collection("users")
                if u["id"] == value.get("id") or u["email"] == value.get("email")
            ),
            None,
        )
        base = ctx.store.collection("bases").get(table["_base"])
        if user is None or not user["_active"] or user["id"] not in base["_acl"]:
            invalid(f'Unknown collaborator for field "{name}".', "INVALID_VALUE_FOR_COLUMN")
        return {k: user[k] for k in ("id", "email", "name")}
    if kind == "multipleRecordLinks":
        if not isinstance(value, list) or len(value) > 1000 or any(not isinstance(key, str) for key in value):
            invalid(f'Field "{name}" requires an array of record IDs.', "INVALID_VALUE_FOR_COLUMN")
        target = ctx.store.collection("tables").get(field["options"]["linkedTableId"])
        for key in value:
            record_for(ctx, target, key)
        return list(dict.fromkeys(value))
    if kind == "multipleAttachments":
        if (
            not isinstance(value, list)
            or len(value) > 100
            or any(not isinstance(item, dict) for item in value)
        ):
            invalid(f'Field "{name}" requires an array of attachment objects.', "INVALID_VALUE_FOR_COLUMN")
        result = []
        for item in value:
            if item.get("id"):
                existing = ctx.store.collection("attachments").get(text(item["id"], "attachment ID"))
                if existing is None or existing["_base"] != table["_base"]:
                    invalid("Unknown attachment ID.", "INVALID_ATTACHMENT_OBJECT")
                result.append({"id": existing["id"]})
                continue
            url = text(item.get("url"), "attachment URL", empty=False, maximum=8192)
            try:
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
                    raise ValueError
                _ = parsed.port
            except ValueError:
                invalid("Invalid attachment URL.", "INVALID_ATTACHMENT_OBJECT")
            filename = text(
                item.get("filename", unquote(parsed.path.rsplit("/", 1)[-1]) or "attachment.txt"),
                "filename",
                maximum=255,
                empty=False,
            )
            attachment = {
                "id": new_id(ctx, "att"),
                "filename": filename,
                "_base": table["_base"],
                "_source": url,
            }
            ctx.store.collection("attachments").put(attachment)
            result.append({"id": attachment["id"]})
        return result
    invalid(f'Unsupported field type "{kind}".', "INVALID_VALUE_FOR_COLUMN")


def normalize(ctx: ProviderContext, table: dict[str, Any], fields: Any, *, typecast: bool) -> dict[str, Any]:
    if not isinstance(fields, dict):
        invalid("fields must be an object.", "INVALID_RECORDS")
    result = {}
    for key, value in fields.items():
        field = field_for(table, text(key, "field name"))
        if field["id"] in result:
            invalid("The same field was supplied by both name and ID.")
        result[field["id"]] = cell(ctx, table, field, value, typecast=typecast)
    return result


def options(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(payload or {})
    sort: dict[int, dict[str, str]] = {}
    for key, value in request.query_params.multi_items():
        text(value, key, maximum=16000)
        if key in {"fields[]", "recordMetadata[]"}:
            result.setdefault(key[:-2], []).append(value)
        elif key.startswith("sort["):
            match = re.fullmatch(r"sort\[(\d{1,2})\]\[(field|direction)\]", key)
            if not match or match[2] in sort.setdefault(int(match[1]), {}):
                invalid("Invalid sort parameter.")
            sort[int(match[1])][match[2]] = value
        elif key in result:
            invalid(f"Duplicate parameter {key}.")
        else:
            result[key] = value
    if sort:
        if "sort" in result or sorted(sort) != list(range(len(sort))):
            invalid("Invalid sort parameter.")
        result["sort"] = [sort[i] for i in sorted(sort)]
    supported = {
        "fields",
        "recordMetadata",
        "filterByFormula",
        "sort",
        "view",
        "offset",
        "pageSize",
        "maxRecords",
        "returnFieldsByFieldId",
        "cellFormat",
        "timeZone",
        "userLocale",
    }
    if set(result) - supported:
        invalid("Unknown query parameter.")
    for key in ("filterByFormula", "view", "offset", "timeZone", "userLocale"):
        if key in result:
            text(result[key], key, maximum=16000)
    result["returnFieldsByFieldId"] = boolean(
        result.get("returnFieldsByFieldId", False), "returnFieldsByFieldId", query=True
    )
    if result.get("cellFormat", "json") not in ("json", "string"):
        invalid("cellFormat must be json or string.")
    if result.get("cellFormat") == "string" and not (result.get("timeZone") and result.get("userLocale")):
        invalid("timeZone and userLocale are required when cellFormat is string.")
    if "timeZone" in result:
        try:
            ZoneInfo(result["timeZone"])
        except (ValueError, OSError, ZoneInfoNotFoundError):
            invalid("Invalid timeZone.")
    if "userLocale" in result and not re.fullmatch(
        r"[a-zA-Z]{2,3}(?:[-_][a-zA-Z0-9]{2,8}){0,2}", result["userLocale"]
    ):
        invalid("Invalid userLocale.")
    for key in ("fields", "recordMetadata"):
        if key in result and (
            not isinstance(result[key], list) or any(not isinstance(s, str) for s in result[key])
        ):
            invalid(f"{key} must be an array of strings.")
    if any(v != "commentCount" for v in result.get("recordMetadata", [])):
        invalid("Only commentCount is supported in recordMetadata.")
    return result


def wire(
    ctx: ProviderContext,
    request: Request,
    table: dict[str, Any],
    record: dict[str, Any],
    opts: dict[str, Any],
) -> dict[str, Any]:
    selected = {field_for(table, key)["id"] for key in opts["fields"]} if "fields" in opts else None
    output = {}
    for key, value in values(table, record).items():
        if selected is not None and key not in selected:
            continue
        if value is None or value is False or value == "" or value == []:
            continue
        field = field_for(table, key)
        if field["type"] == "multipleAttachments":
            value = [attachment_wire(ctx, request, item["id"]) for item in value]
        if opts.get("cellFormat") == "string":
            if field["type"] == "date":
                parsed = date.fromisoformat(value)
                value = (
                    f"{parsed.month}/{parsed.day}/{parsed.year}" if opts["userLocale"] == "en-US" else value
                )
            elif isinstance(value, list):
                value = ", ".join(
                    v.get("filename", v.get("name", v.get("id", ""))) if isinstance(v, dict) else str(v)
                    for v in value
                )
            elif isinstance(value, dict):
                value = value.get("name", value.get("email", ""))
            else:
                value = "checked" if value is True else str(value)
        output[key if opts.get("returnFieldsByFieldId") else field["name"]] = value
    result = {"id": record["id"], "createdTime": record["createdTime"], "fields": output}
    if opts.get("recordMetadata"):
        result["commentCount"] = 0
    return result


def list_records(
    ctx: ProviderContext, request: Request, table: dict[str, Any], grant: Grant, opts: dict[str, Any]
) -> dict[str, Any]:
    size = integer(opts.get("pageSize", 100), "pageSize", 1, 100)
    maximum = integer(opts.get("maxRecords", 1000000), "maxRecords", 1, 1000000)
    for field in opts.get("fields", []):
        field_for(table, field)
    selected = records(ctx, table)
    if "view" in opts:
        view = next((v for v in table["views"] if opts["view"] in (v["id"], v["name"])), None)
        if view is None:
            invalid("View not found", "VIEW_NOT_FOUND")
        if view.get("_formula"):
            formula = Formula(view["_formula"], field_map(table))
            selected = [r for r in selected if formula.evaluate(r["id"], values(table, r))]
    if "filterByFormula" in opts:
        formula = Formula(opts["filterByFormula"], field_map(table))
        selected = [r for r in selected if formula.evaluate(r["id"], values(table, r))]
    sorting = opts.get("sort", [])
    if not isinstance(sorting, list) or len(sorting) > 25:
        invalid("sort must be an array of sort objects.")
    for part in reversed(sorting):
        if (
            not isinstance(part, dict)
            or set(part) - {"field", "direction"}
            or not isinstance(part.get("field"), str)
            or part.get("direction", "asc") not in ("asc", "desc")
        ):
            invalid("Invalid sort object.")
        field = field_for(table, part["field"])

        def sort_value(record: dict[str, Any], field_id: str = field["id"]) -> Any:
            value = values(table, record).get(field_id)
            return (
                value is not None,
                str(value).casefold() if not isinstance(value, (int, float)) else value,
            )

        selected.sort(key=sort_value, reverse=part.get("direction") == "desc")
    selected = selected[:maximum]
    fingerprint = hashlib.sha256(
        json.dumps({k: v for k, v in opts.items() if k != "offset"}, sort_keys=True).encode()
    ).hexdigest()
    principal = [grant.client_id, grant.user_id, sorted(grant.scopes), grant.extra.get("bases")]
    start = 0
    if opts.get("offset"):
        cursor = ctx.store.collection("_cursors").get(opts["offset"])
        if (
            not cursor
            or cursor["table"] != table["id"]
            or cursor["query"] != fingerprint
            or cursor["principal"] != principal
            or cursor["expires"] <= ctx.clock.timestamp()
        ):
            invalid(
                "The list records iterator is no longer available.", "LIST_RECORDS_ITERATOR_NOT_AVAILABLE"
            )
        start = cursor["offset"]
    page = selected[start : start + size]
    result = {"records": [wire(ctx, request, table, r, opts) for r in page]}
    if start + size < len(selected):
        token = "itr" + secrets.token_urlsafe(32)
        ctx.store.collection("_cursors").put(
            {
                "id": token,
                "table": table["id"],
                "query": fingerprint,
                "principal": principal,
                "offset": start + size,
                "expires": ctx.clock.timestamp() + 3600,
            }
        )
        result["offset"] = token
    return result


def sync_links(
    ctx: ProviderContext, table: dict[str, Any], old: dict[str, Any] | None, new: dict[str, Any] | None
) -> None:
    record_id = (new or old)["id"]
    for field in table["fields"]:
        inverse = field.get("options", {}).get("inverseLinkFieldId")
        if not inverse:
            continue
        before = set((old or {}).get("fields", {}).get(field["id"]) or [])
        after = set((new or {}).get("fields", {}).get(field["id"]) or [])
        for key in before | after:
            target = ctx.store.collection("records").get(key)
            if target is None:
                continue
            linked = list(target["fields"].get(inverse) or [])
            target["fields"][inverse] = linked
            if key in after and record_id not in linked:
                linked.append(record_id)
            if key not in after and record_id in linked:
                linked.remove(record_id)


async def write(
    ctx: ProviderContext,
    request: Request,
    table: dict[str, Any],
    grant: Grant,
    data: dict[str, Any],
    single: str | None = None,
) -> dict[str, Any]:
    from .hooks import notify

    allowed = {"fields", "records", "typecast", "returnFieldsByFieldId", "performUpsert"}
    if set(data) - allowed or ("fields" in data and "records" in data):
        invalid("Invalid record request.", "INVALID_RECORDS")
    is_single = single is not None or "fields" in data
    items = [{"id": single, "fields": data.get("fields")}] if is_single else data.get("records")
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        invalid("You must provide between 1 and 10 records.", "INVALID_RECORDS")
    typecast = boolean(data.get("typecast", False), "typecast")
    opts = options(
        request,
        {"returnFieldsByFieldId": boolean(data.get("returnFieldsByFieldId", False), "returnFieldsByFieldId")},
    )
    upsert = data.get("performUpsert")
    merge = []
    if upsert is not None:
        if (
            request.method not in {"PATCH", "PUT"}
            or is_single
            or not isinstance(upsert, dict)
            or set(upsert) != {"fieldsToMergeOn"}
        ):
            invalid("Invalid performUpsert options.")
        merge = upsert["fieldsToMergeOn"]
        if (
            not isinstance(merge, list)
            or not 1 <= len(merge) <= 3
            or any(not isinstance(f, str) for f in merge)
        ):
            invalid("fieldsToMergeOn requires one to three fields.")
        merge = [field_for(table, f) for f in merge]
        if any(
            f["type"] not in {"singleLineText", "multilineText", "singleSelect", "number", "date", "dateTime"}
            for f in merge
        ):
            invalid("Unsupported field type in fieldsToMergeOn.")
    snapshot = ctx.store.snapshot()
    events, result, created_ids, updated_ids = [], [], [], []
    seen = set()
    try:
        for item in items:
            if not isinstance(item, dict) or set(item) - {"id", "fields"}:
                invalid("Each record requires a fields object.", "INVALID_RECORDS")
            normalized = normalize(ctx, table, item.get("fields"), typecast=typecast)
            old = None
            if item.get("id"):
                if request.method == "POST":
                    invalid("Record IDs must not be supplied when creating records.", "INVALID_RECORDS")
                old = record_for(ctx, table, text(item["id"], "record ID"))
            elif merge:
                if any(f["id"] not in normalized or normalized[f["id"]] is None for f in merge):
                    invalid("Every merge field must be provided.")
                matches = [
                    r
                    for r in records(ctx, table)
                    if all(r["fields"].get(f["id"]) == normalized[f["id"]] for f in merge)
                ]
                if len(matches) > 1:
                    invalid("The merge fields matched multiple records.", "INVALID_UPSERT_RECORDS")
                old = matches[0] if matches else None
            elif request.method != "POST":
                invalid("Record IDs are required for updates.", "INVALID_RECORDS")
            if old and old["id"] in seen:
                invalid("Duplicate record IDs in request.", "INVALID_RECORDS")
            record = (
                copy.deepcopy(old)
                if old
                else {
                    "id": new_id(ctx, "rec"),
                    "createdTime": timestamp(ctx),
                    "fields": {},
                    "_base": table["_base"],
                    "_table": table["id"],
                }
            )
            before = copy.deepcopy(old)
            if request.method == "PUT":
                record["fields"] = {}
            record["fields"].update(normalized)
            ctx.store.collection("records").put(record)
            sync_links(ctx, table, before, record)
            seen.add(record["id"])
            (updated_ids if old else created_ids).append(record["id"])
            events.append((before, copy.deepcopy(record)))
            result.append(wire(ctx, request, table, record, opts))
    except Exception:
        ctx.store.restore(snapshot)
        raise
    await notify(ctx, request, table, grant, events)
    if is_single:
        return result[0]
    output = {"records": result}
    if upsert is not None:
        output.update(createdRecords=created_ids, updatedRecords=updated_ids)
    return output


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.api_route("/v0/{base_id}/{table_key}/listRecords", methods=["POST"])
    async def post_list(request: Request, base_id: str, table_key: str) -> dict[str, Any]:
        grant, _ = base_access(ctx, request, base_id, "data.records:read")
        table = table_for(ctx, base_id, table_key)
        return list_records(ctx, request, table, grant, options(request, await body(request)))

    @app.api_route("/v0/{base_id}/{table_key}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    async def table_route(request: Request, base_id: str, table_key: str) -> dict[str, Any]:
        reading = request.method == "GET"
        grant, _ = base_access(
            ctx,
            request,
            base_id,
            "data.records:read" if reading else "data.records:write",
            level="read" if reading else "edit",
        )
        table = table_for(ctx, base_id, table_key)
        if reading:
            return list_records(ctx, request, table, grant, options(request))
        if request.method == "DELETE":
            keys = request.query_params.getlist("records[]")
            if (
                any(key != "records[]" for key in request.query_params)
                or not 1 <= len(keys) <= 10
                or len(set(keys)) != len(keys)
            ):
                invalid("Provide one to ten unique records[].", "INVALID_RECORDS")
            targets = [record_for(ctx, table, key) for key in keys]
            return await remove(ctx, request, table, grant, targets)
        return await write(ctx, request, table, grant, await body(request))

    @app.api_route("/v0/{base_id}/{table_key}/{record_id}", methods=["GET", "PATCH", "PUT", "DELETE"])
    async def record_route(request: Request, base_id: str, table_key: str, record_id: str) -> dict[str, Any]:
        reading = request.method == "GET"
        grant, _ = base_access(
            ctx,
            request,
            base_id,
            "data.records:read" if reading else "data.records:write",
            level="read" if reading else "edit",
        )
        table = table_for(ctx, base_id, table_key)
        record = record_for(ctx, table, record_id)
        if reading:
            return wire(ctx, request, table, record, options(request))
        if request.method == "DELETE":
            return (await remove(ctx, request, table, grant, [record]))["records"][0]
        return await write(ctx, request, table, grant, await body(request), record_id)

    @app.post("/v0/{base_id}/{record_id}/{field_key}/uploadAttachment")
    async def upload(request: Request, base_id: str, record_id: str, field_key: str) -> dict[str, Any]:
        from .hooks import notify

        grant, _ = base_access(ctx, request, base_id, "data.records:write", level="edit")
        old = ctx.store.collection("records").get(record_id)
        if old is None or old["_base"] != base_id:
            fail(404, "NOT_FOUND", "Record not found")
        table = ctx.store.collection("tables").get(old["_table"])
        field = field_for(table, field_key)
        if field["type"] != "multipleAttachments":
            invalid("The target field is not an attachment field.", "INVALID_ATTACHMENT_OBJECT")
        data = await body(request)
        if set(data) != {"file", "filename", "contentType"}:
            invalid("file, filename and contentType are required.")
        filename = text(data["filename"], "filename", maximum=255, empty=False)
        content_type = text(data["contentType"], "contentType", maximum=255, empty=False)
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", content_type):
            invalid("Invalid contentType.")
        try:
            content = base64.b64decode(text(data["file"], "file", maximum=7 * 1024 * 1024), validate=True)
        except ValueError:
            invalid("file must be valid base64.", "INVALID_ATTACHMENT_OBJECT")
        if len(content) > 5 * 1024 * 1024:
            fail(413, "REQUEST_TOO_LARGE", "Attachments must not exceed 5 MB.")
        record = copy.deepcopy(old)
        attachment = {
            "id": new_id(ctx, "att"),
            "filename": filename,
            "type": content_type,
            "size": len(content),
            "_base": base_id,
            "_blob": ctx.store.blobs.put(content),
        }
        ctx.store.collection("attachments").put(attachment)
        record["fields"][field["id"]] = [*(record["fields"].get(field["id"]) or []), {"id": attachment["id"]}]
        ctx.store.collection("records").put(record)
        await notify(ctx, request, table, grant, [(old, record)])
        return wire(ctx, request, table, record, {"returnFieldsByFieldId": True})

    @app.api_route("/attachments/{attachment_id}", methods=["GET", "HEAD"])
    async def download(request: Request, attachment_id: str) -> Response:
        link = ctx.store.collection("_download_links").get(request.query_params.get("token"))
        if not link or link["attachment"] != attachment_id or link["expires"] <= ctx.clock.timestamp():
            fail(403, "INVALID_PERMISSIONS", "This attachment URL is invalid or expired.")
        attachment = ctx.store.collection("attachments").get(attachment_id)
        if attachment is None:
            fail(404, "NOT_FOUND", "Attachment not found")
        ensure_blob(ctx, attachment)
        content = ctx.store.blobs.get(attachment["_blob"])
        return Response(
            content if request.method == "GET" else b"",
            media_type=attachment["type"],
            headers={"Content-Length": str(len(content))},
        )


async def remove(
    ctx: ProviderContext, request: Request, table: dict[str, Any], grant: Grant, targets: list[dict[str, Any]]
) -> dict[str, Any]:
    from .hooks import notify

    for old in targets:
        sync_links(ctx, table, old, None)
        ctx.store.collection("records").delete(old["id"])
        for record in ctx.store.collection("records"):
            other_table = ctx.store.collection("tables").get(record["_table"])
            for field in other_table["fields"]:
                if field["type"] == "multipleRecordLinks" and old["id"] in (
                    record["fields"].get(field["id"]) or []
                ):
                    record["fields"][field["id"]].remove(old["id"])
    await notify(ctx, request, table, grant, [(old, None) for old in targets])
    return {"records": [{"id": old["id"], "deleted": True} for old in targets]}
