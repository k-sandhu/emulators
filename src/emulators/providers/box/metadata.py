"""Metadata templates and instances: validation, RFC 6902 patching and wire rendering."""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.box import db, errors
from emulators.providers.box.util import input_ts

Record = dict[str, Any]

FIELD_TYPES = ("string", "float", "date", "enum", "multiSelect")
GLOBAL_PROPERTIES = "properties"
CLASSIFICATION_KEY = "securityClassification-6VMVochwUWo"
CLASSIFICATION_FIELD = "Box__Security__Classification__Key"
CLASSIFICATION_COLORS = {
    0: "#e0e0e0",
    1: "#c1d8ee",
    2: "#a6e3b5",
    3: "#fff3a8",
    4: "#ffc9a6",
    5: "#f7a1a1",
    6: "#cfb6f0",
    7: "#9eb4c8",
}


def enterprise_scope(ctx: ProviderContext) -> str:
    return f"enterprise_{ctx.world.org.box_enterprise_id}"


def normalize_scope(ctx: ProviderContext, scope: str) -> str:
    return enterprise_scope(ctx) if scope == "enterprise" else scope


def templates(ctx: ProviderContext):
    return ctx.store.collection(db.TEMPLATES)


def find_template(ctx: ProviderContext, scope: str, key: str) -> Record | None:
    scope = normalize_scope(ctx, scope)
    return next((t for t in templates(ctx) if t["scope"] == scope and t["templateKey"] == key), None)


def render_template(tpl: Record) -> Record:
    return {
        "id": tpl["id"],
        "type": "metadata_template",
        "templateKey": tpl["templateKey"],
        "scope": tpl["scope"],
        "displayName": tpl["displayName"],
        "hidden": bool(tpl.get("hidden", False)),
        "copyInstanceOnItemCopy": bool(tpl.get("copyInstanceOnItemCopy", False)),
        "fields": copy.deepcopy(tpl["fields"]),
    }


def new_field(spec: dict[str, Any]) -> Record:
    ftype = spec.get("type")
    if ftype not in FIELD_TYPES:
        raise errors.invalid_parameter("fields", f"Unsupported field type '{ftype}'")
    key = spec.get("key")
    if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
        raise errors.invalid_parameter("fields", "Field 'key' is required and must be alphanumeric")
    field: Record = {
        "id": str(uuid.uuid4()),
        "type": ftype,
        "key": key,
        "displayName": spec.get("displayName") or key,
        "hidden": bool(spec.get("hidden", False)),
    }
    if spec.get("description"):
        field["description"] = spec["description"]
    if ftype in ("enum", "multiSelect"):
        field["options"] = [{"id": str(uuid.uuid4()), "key": o["key"]} for o in spec.get("options", [])]
    return field


# -- instances -----------------------------------------------------------------------
def instances_for(ctx: ProviderContext, item: Record) -> list[Record]:
    return [
        i
        for i in ctx.store.collection(db.METADATA)
        if i["item_type"] == item["type"] and i["item_id"] == item["id"]
    ]


def find_instance(ctx: ProviderContext, item: Record, scope: str, key: str) -> Record | None:
    scope = normalize_scope(ctx, scope)
    return next((i for i in instances_for(ctx, item) if i["scope"] == scope and i["template"] == key), None)


def render_instance(ctx: ProviderContext, inst: Record) -> Record:
    tpl = find_template(ctx, inst["scope"], inst["template"])
    out: Record = {
        "$parent": f"{inst['item_type']}_{inst['item_id']}",
        "$id": inst["id"],
        "$version": inst["version"],
        "$type": f"{inst['template']}-{tpl['id']}" if tpl else inst["template"],
        "$typeVersion": inst.get("typeVersion", 0),
        "$template": inst["template"],
        "$scope": inst["scope"],
        "$canEdit": bool(inst.get("can_edit", True)),
    }
    out.update(copy.deepcopy(inst["values"]))
    return out


def coerce_value(field: Record, value: Any) -> Any:
    ftype = field["type"]
    key = field["key"]
    if ftype == "string":
        if not isinstance(value, str):
            raise errors.bad_request(f"Field '{key}' must be a string", reason="invalid_parameter", name=key)
        return value
    if ftype == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise errors.bad_request(f"Field '{key}' must be a number", reason="invalid_parameter", name=key)
        return value
    if ftype == "date":
        if not isinstance(value, str):
            raise errors.bad_request(f"Field '{key}' must be a date", reason="invalid_parameter", name=key)
        input_ts(value, key)
        return value
    options = {o["key"] for o in field.get("options", [])}
    if ftype == "enum":
        if value not in options:
            raise errors.bad_request(
                f"Value '{value}' is not a valid option for '{key}'", reason="invalid_parameter", name=key
            )
        return value
    if not isinstance(value, list) or any(v not in options for v in value):
        raise errors.bad_request(
            f"Invalid value for multiSelect '{key}'", reason="invalid_parameter", name=key
        )
    return value


def validate_values(ctx: ProviderContext, scope: str, key: str, values: dict[str, Any]) -> dict[str, Any]:
    """Check ``values`` against the template (global ``properties`` is free-form strings)."""
    scope = normalize_scope(ctx, scope)
    if scope == "global" and key == GLOBAL_PROPERTIES:
        return {k: str(v) if not isinstance(v, str) else v for k, v in values.items()}
    tpl = find_template(ctx, scope, key)
    if tpl is None:
        raise errors.not_found("Specified Metadata Template not found")
    by_key = {f["key"]: f for f in tpl["fields"]}
    clean: dict[str, Any] = {}
    for name, value in values.items():
        field = by_key.get(name)
        if field is None:
            raise errors.bad_request(
                f"Unknown field '{name}' for template '{key}'", reason="invalid_parameter", name=name
            )
        clean[name] = coerce_value(field, value)
    return clean


def apply_patch(
    ctx: ProviderContext, scope: str, key: str, values: dict[str, Any], ops: Any
) -> dict[str, Any]:
    """Apply an RFC 6902 JSON patch (``add``, ``replace``, ``remove``, ``test``, ``move``, ``copy``)."""
    if not isinstance(ops, list) or not ops:
        raise errors.bad_request(
            "Request body must be a non-empty JSON patch array", reason="invalid_request_body"
        )
    result = copy.deepcopy(values)

    def path_key(path: Any) -> str:
        if not isinstance(path, str) or not path.startswith("/") or path.count("/") != 1:
            raise errors.bad_request(
                f"Unsupported patch path '{path}'", reason="invalid_parameter", name="path"
            )
        return path[1:].replace("~1", "/").replace("~0", "~")

    for op in ops:
        kind = op.get("op")
        if kind in ("add", "replace"):
            name = path_key(op.get("path"))
            if kind == "replace" and name not in result:
                raise errors.bad_request(f"Field '{name}' not set", reason="invalid_parameter", name=name)
            result.update(validate_values(ctx, scope, key, {name: op.get("value")}))
        elif kind == "remove":
            name = path_key(op.get("path"))
            if name not in result:
                raise errors.bad_request(f"Field '{name}' not set", reason="invalid_parameter", name=name)
            del result[name]
        elif kind == "test":
            name = path_key(op.get("path"))
            if result.get(name) != op.get("value"):
                raise errors.bad_request(f"Test failed for '{name}'", reason="invalid_parameter", name=name)
        elif kind in ("move", "copy"):
            source, target = path_key(op.get("from")), path_key(op.get("path"))
            if source not in result:
                raise errors.bad_request(f"Field '{source}' not set", reason="invalid_parameter", name=source)
            value = result[source]
            if kind == "move":
                del result[source]
            result.update(validate_values(ctx, scope, key, {target: value}))
        else:
            raise errors.bad_request(
                f"Unsupported patch operation '{kind}'", reason="invalid_parameter", name="op"
            )
    return result


def set_instance(
    ctx: ProviderContext, item: Record, scope: str, key: str, values: dict[str, Any], now: str
) -> Record:
    scope = normalize_scope(ctx, scope)
    inst = find_instance(ctx, item, scope, key)
    if inst is None:
        inst = {
            "id": str(uuid.uuid4()),
            "item_type": item["type"],
            "item_id": item["id"],
            "scope": scope,
            "template": key,
            "values": values,
            "version": 0,
            "typeVersion": 0,
            "created_at": now,
            "can_edit": True,
        }
        ctx.store.collection(db.METADATA).put(inst)
    else:
        inst["values"] = values
        inst["version"] += 1
    return inst


def delete_instance(ctx: ProviderContext, inst: Record) -> None:
    ctx.store.collection(db.METADATA).delete(inst["id"])


def copy_instances(ctx: ProviderContext, source: Record, target: Record, now: str) -> None:
    """Item copies keep metadata only for templates flagged ``copyInstanceOnItemCopy`` and global properties."""
    for inst in instances_for(ctx, source):
        tpl = find_template(ctx, inst["scope"], inst["template"])
        if (tpl and tpl.get("copyInstanceOnItemCopy")) or inst["scope"] == "global":
            set_instance(ctx, target, inst["scope"], inst["template"], copy.deepcopy(inst["values"]), now)
