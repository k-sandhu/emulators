"""Metadata templates (``/metadata_templates``) and metadata instances on files and folders."""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import FastAPI, Request

from emulators.core.provider import ProviderContext
from emulators.providers.box import access, db, errors, events, metadata
from emulators.providers.box.access import Principal
from emulators.providers.box.routing import Api, created, no_content
from emulators.providers.box.util import json_any, json_body, now_iso, object_or_empty, require_str

Record = dict[str, Any]


def camel_key(display_name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", display_name)
    if not words:
        raise errors.invalid_parameter("displayName", "A displayName is required")
    key = words[0].lower() + "".join(w.capitalize() for w in words[1:])
    return key if not key[0].isdigit() else f"t{key}"


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)
    templates = db.Live(ctx, db.TEMPLATES)

    def listing(entries: list[Record]) -> dict[str, Any]:
        return {"limit": 100, "entries": [metadata.render_template(t) for t in entries], "next_marker": None}

    # -- templates -----------------------------------------------------------------------
    @api.get("/metadata_templates/enterprise")
    async def enterprise_templates(request: Request) -> Any:
        api.principal(request)
        return listing([t for t in templates if t["scope"] == metadata.enterprise_scope(ctx)])

    @api.get("/metadata_templates/global")
    async def global_templates(request: Request) -> Any:
        api.principal(request)
        properties = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "box/global/properties")),
            "type": "metadata_template",
            "templateKey": "properties",
            "scope": "global",
            "displayName": "Properties",
            "hidden": False,
            "copyInstanceOnItemCopy": False,
            "fields": [],
        }
        return listing([properties])

    @api.get("/metadata_templates")
    async def templates_by_instance(request: Request) -> Any:
        api.principal(request)
        instance_id = request.query_params.get("metadata_instance_id")
        if not instance_id:
            raise errors.missing_parameter("metadata_instance_id")
        inst = ctx.store.collection(db.METADATA).get(instance_id)
        tpl = metadata.find_template(ctx, inst["scope"], inst["template"]) if inst else None
        if tpl is None:
            raise errors.not_found()
        return listing([tpl])

    @api.post("/metadata_templates/schema")
    async def create_template(request: Request) -> Any:
        p = api.principal(request)
        p.require_privileged()
        body = await json_body(request)
        scope = body.get("scope", "enterprise")
        if scope != "enterprise":
            raise errors.invalid_parameter("scope", "Only 'enterprise' templates can be created")
        display = require_str(body, "displayName")
        key = body.get("templateKey") or camel_key(display)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise errors.invalid_parameter("templateKey", "'templateKey' must be alphanumeric")
        fields = body.get("fields") or []
        if not isinstance(fields, list) or not all(isinstance(f, dict) for f in fields):
            raise errors.invalid_parameter("fields", "'fields' must be an array of field definitions")
        if metadata.find_template(ctx, "enterprise", key) is not None:
            raise errors.box_error(409, "tuple_already_exists", "A template with this key already exists")
        tpl = {
            "id": str(uuid.uuid4()),
            "type": "metadata_template",
            "templateKey": key,
            "scope": metadata.enterprise_scope(ctx),
            "displayName": display,
            "hidden": bool(body.get("hidden", False)),
            "copyInstanceOnItemCopy": bool(body.get("copyInstanceOnItemCopy", False)),
            "fields": [metadata.new_field(f) for f in fields],
        }
        templates.put(tpl)
        events.append_event(
            ctx, "METADATA_TEMPLATE_CREATE", created_by=p.uid, source=metadata.render_template(tpl)
        )
        return created(metadata.render_template(tpl))

    def template_or_404(scope: str, key: str) -> Record:
        tpl = metadata.find_template(ctx, scope, key)
        if tpl is None:
            raise errors.not_found("Specified Metadata Template not found")
        return tpl

    @api.get("/metadata_templates/{scope}/{key}/schema")
    async def get_template(request: Request, scope: str, key: str) -> Any:
        api.principal(request)
        return metadata.render_template(template_or_404(scope, key))

    @api.put("/metadata_templates/{scope}/{key}/schema")
    async def update_template(request: Request, scope: str, key: str) -> Any:
        p = api.principal(request)
        p.require_privileged()
        tpl = template_or_404(scope, key)
        raw = await json_any(request)
        if not isinstance(raw, list) or not raw:
            raise errors.bad_request(
                "Request body must be a non-empty array of operations", reason="invalid_request_body"
            )
        for op in raw:
            apply_template_op(tpl, op)
        events.append_event(
            ctx, "METADATA_TEMPLATE_UPDATE", created_by=p.uid, source=metadata.render_template(tpl)
        )
        return metadata.render_template(tpl)

    def apply_template_op(tpl: Record, op: Any) -> None:
        if not isinstance(op, dict):
            raise errors.bad_request(
                "Each template operation must be an object", reason="invalid_request_body", name="op"
            )
        kind = op.get("op")
        fields: list[Record] = tpl["fields"]

        def find(field_key: Any) -> Record:
            for field in fields:
                if field["key"] == field_key:
                    return field
            raise errors.not_found(f"Field '{field_key}' not found")

        if kind == "addField":
            spec = object_or_empty(op.get("data"), "data")
            if any(f["key"] == spec.get("key") for f in fields):
                raise errors.box_error(409, "tuple_already_exists", "A field with this key already exists")
            fields.append(metadata.new_field(spec))
        elif kind == "editField":
            field = find(op.get("fieldKey"))
            data = object_or_empty(op.get("data"), "data")
            if "key" in data and (
                not isinstance(data["key"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", data["key"])
            ):
                raise errors.invalid_parameter("data.key", "Field 'key' must be alphanumeric")
            for attr in ("displayName", "description"):
                if attr in data and not isinstance(data[attr], str):
                    raise errors.invalid_parameter(f"data.{attr}", f"'{attr}' must be a string")
            for attr in ("displayName", "description", "hidden", "key"):
                if attr in data:
                    old_key = field["key"]
                    field[attr] = bool(data[attr]) if attr == "hidden" else data[attr]
                    if attr == "key":
                        for inst in ctx.store.collection(db.METADATA):
                            if (
                                inst["scope"] == tpl["scope"]
                                and inst["template"] == tpl["templateKey"]
                                and old_key in inst["values"]
                            ):
                                inst["values"][data[attr]] = inst["values"].pop(old_key)
        elif kind == "removeField":
            field = find(op.get("fieldKey"))
            fields.remove(field)
            for inst in ctx.store.collection(db.METADATA):
                if inst["scope"] == tpl["scope"] and inst["template"] == tpl["templateKey"]:
                    inst["values"].pop(field["key"], None)
        elif kind == "reorderFields":
            order = op.get("fieldKeys") or []
            if not isinstance(order, list):
                raise errors.invalid_parameter("fieldKeys", "'fieldKeys' must be an array")
            fields.sort(key=lambda f: order.index(f["key"]) if f["key"] in order else len(order))
        elif kind == "editTemplate":
            data = object_or_empty(op.get("data"), "data")
            if "displayName" in data and not isinstance(data["displayName"], str):
                raise errors.invalid_parameter("data.displayName", "'displayName' must be a string")
            for attr in ("displayName", "hidden", "copyInstanceOnItemCopy"):
                if attr in data:
                    tpl[attr] = data[attr] if attr == "displayName" else bool(data[attr])
        elif kind in ("addEnumOption", "addMultiSelectOption"):
            field = find(op.get("fieldKey"))
            if field["type"] not in ("enum", "multiSelect"):
                raise errors.invalid_parameter("fieldKey", "Field has no options")
            option_key = object_or_empty(op.get("data"), "data").get("key")
            if not isinstance(option_key, str) or not option_key:
                raise errors.invalid_parameter("data.key", "An option 'key' is required")
            if any(o["key"] == option_key for o in field.get("options", [])):
                raise errors.box_error(409, "tuple_already_exists", "An option with this key already exists")
            field.setdefault("options", []).append({"id": str(uuid.uuid4()), "key": option_key})
        elif kind in ("removeEnumOption", "removeMultiSelectOption"):
            field = find(op.get("fieldKey"))
            field["options"] = [
                o
                for o in field.get("options", [])
                if o["key"] != op.get("enumOptionKey", op.get("multiSelectOptionKey"))
            ]
        else:
            raise errors.bad_request(
                f"Unsupported template operation '{kind}'", reason="invalid_parameter", name="op"
            )

    @api.delete("/metadata_templates/{scope}/{key}/schema")
    async def delete_template(request: Request, scope: str, key: str) -> Any:
        p = api.principal(request)
        p.require_privileged()
        tpl = template_or_404(scope, key)
        for inst in [
            i
            for i in ctx.store.collection(db.METADATA)
            if i["scope"] == tpl["scope"] and i["template"] == key
        ]:
            metadata.delete_instance(ctx, inst)
        templates.delete(tpl["id"])
        events.append_event(
            ctx, "METADATA_TEMPLATE_DELETE", created_by=p.uid, source=metadata.render_template(tpl)
        )
        return no_content()

    @api.get("/metadata_templates/{template_id}")
    async def get_template_by_id(request: Request, template_id: str) -> Any:
        api.principal(request)
        tpl = templates.get(template_id)
        if tpl is None:
            raise errors.not_found("Specified Metadata Template not found")
        return metadata.render_template(tpl)

    # -- instances -----------------------------------------------------------------------
    def instance_routes(kind: str) -> None:
        plural = f"{kind}s"

        def item_for(p: Principal, item_id: str, need: str | None) -> Record:
            return p.get_item(kind, item_id, need)

        async def emit(p: Principal, event_type: str, item: Record, inst: Record) -> None:
            await events.emit(
                p,
                event_type,
                None,
                source=metadata.render_instance(ctx, inst),
                kind="metadata",
                webhook_item=item,
            )

        def instance_or_404(item: Record, scope: str, template: str) -> Record:
            inst = metadata.find_instance(ctx, item, scope, template)
            if inst is None:
                raise errors.box_error(404, "instance_not_found", "Instance not found")
            return inst

        @api.get(f"/{plural}/{{item_id}}/metadata")
        async def list_instances(request: Request, item_id: str) -> Any:
            p = api.principal(request)
            item = item_for(p, item_id, None)
            return {
                "entries": [metadata.render_instance(ctx, i) for i in metadata.instances_for(ctx, item)],
                "limit": 100,
            }

        @api.get(f"/{plural}/{{item_id}}/metadata/{{scope}}/{{template}}")
        async def get_instance(request: Request, item_id: str, scope: str, template: str) -> Any:
            p = api.principal(request)
            item = item_for(p, item_id, None)
            return metadata.render_instance(ctx, instance_or_404(item, scope, template))

        @api.post(f"/{plural}/{{item_id}}/metadata/{{scope}}/{{template}}", status_code=201)
        async def create_instance(request: Request, item_id: str, scope: str, template: str) -> Any:
            p = api.principal(request)
            item = item_for(p, item_id, access.RENAME)
            body = await json_body(request, required=False)
            values = metadata.validate_values(ctx, scope, template, body)
            if metadata.find_instance(ctx, item, scope, template) is not None:
                raise errors.box_error(
                    409, "tuple_already_exists", "An instance already exists for this template on this item"
                )
            inst = metadata.set_instance(ctx, item, scope, template, values, now_iso(ctx))
            await emit(p, "METADATA_INSTANCE_CREATE", item, inst)
            return created(metadata.render_instance(ctx, inst))

        @api.put(f"/{plural}/{{item_id}}/metadata/{{scope}}/{{template}}")
        async def update_instance(request: Request, item_id: str, scope: str, template: str) -> Any:
            p = api.principal(request)
            item = item_for(p, item_id, access.RENAME)
            inst = instance_or_404(item, scope, template)
            ops_body = await json_any(request)
            inst["values"] = metadata.apply_patch(ctx, scope, template, inst["values"], ops_body)
            inst["version"] += 1
            await emit(p, "METADATA_INSTANCE_UPDATE", item, inst)
            return metadata.render_instance(ctx, inst)

        @api.delete(f"/{plural}/{{item_id}}/metadata/{{scope}}/{{template}}")
        async def delete_instance(request: Request, item_id: str, scope: str, template: str) -> Any:
            p = api.principal(request)
            item = item_for(p, item_id, access.RENAME)
            inst = instance_or_404(item, scope, template)
            metadata.delete_instance(ctx, inst)
            await emit(p, "METADATA_INSTANCE_DELETE", item, inst)
            return no_content()

    instance_routes("file")
    instance_routes("folder")
