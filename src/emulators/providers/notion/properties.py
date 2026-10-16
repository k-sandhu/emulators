"""Database schema and page property normalization."""

from __future__ import annotations

import copy
import re
from typing import Any

from emulators.core.ids import stable_guid, stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.notion.common import (
    invalid,
    partial_user,
    property_by_key,
    rich_text,
    uid,
)

TYPES = {
    "title",
    "rich_text",
    "number",
    "select",
    "multi_select",
    "status",
    "date",
    "people",
    "files",
    "checkbox",
    "url",
    "email",
    "phone_number",
    "relation",
    "formula",
    "created_time",
    "last_edited_time",
    "created_by",
    "last_edited_by",
}
READ_ONLY = {"formula", "created_time", "last_edited_time", "created_by", "last_edited_by"}


def schema(raw: dict[str, Any], source_id: str, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        invalid("properties must be an object.")
    result = copy.deepcopy(existing or {})
    for name, value in raw.items():
        original = None
        if name in result or any(p["id"] == name for p in result.values()):
            old_name, original = property_by_key(result, name)
            result.pop(old_name)
        if value is None:
            continue
        if not isinstance(value, dict):
            invalid("Property schemas must be objects or null.")
        name = value.get("name", original["name"] if original else name)
        kind = (
            value.get("type") or next((k for k in value if k in TYPES), None) or (original or {}).get("type")
        )
        if kind not in TYPES:
            invalid(f"Invalid property type for {name}.")
        config = copy.deepcopy(value.get(kind, (original or {}).get(kind, {})))
        if not isinstance(config, dict):
            invalid(f"{kind} schema must be an object.")
        if kind in ("select", "multi_select", "status"):
            options = config.setdefault("options", [])
            if not isinstance(options, list) or any(
                not isinstance(option, dict) or not isinstance(option.get("name"), str) for option in options
            ):
                invalid("Schema options must be objects containing a name.")
            for option in options:
                option.setdefault("id", stable_guid(source_id, name, option["name"]))
                option.setdefault("color", "default")
            if kind == "status":
                config.setdefault(
                    "groups",
                    [
                        {
                            "id": stable_guid(source_id, name, "group"),
                            "name": "To-do",
                            "color": "gray",
                            "option_ids": [o["id"] for o in options],
                        }
                    ],
                )
        if kind == "number":
            config.setdefault("format", "number")
        if kind == "relation":
            config.setdefault("type", "single_property")
            config.setdefault(config["type"], {})
        if kind == "formula" and not isinstance(config.get("expression"), str):
            invalid("formula.expression must be a string.")
        result[name] = {
            "id": original["id"]
            if original
            else ("title" if kind == "title" else stable_hash(source_id, name, length=4)),
            "name": name,
            "type": kind,
            kind: config,
        }
    if sum(p["type"] == "title" for p in result.values()) != 1:
        invalid("A database must have exactly one title property.")
    return result


def attachment(ctx: ProviderContext, raw: Any) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        invalid("File or icon must be an object.")
    result = copy.deepcopy(raw)
    if any(key.startswith("_") for key in result):
        invalid("File objects contain an unsupported field.")
    kind = result.get("type") or next((k for k in ("external", "file_upload", "emoji") if k in result), None)
    if kind == "emoji" and isinstance(result.get("emoji"), str):
        return {"type": "emoji", "emoji": result["emoji"]}
    if kind == "external" and isinstance(result.get("external", {}).get("url"), str):
        result["type"] = "external"
        return result
    if kind == "file_upload":
        if not isinstance(result.get("file_upload"), dict):
            invalid("file_upload must be an object containing id.")
        upload = ctx.store["file_uploads"].get(uid(result["file_upload"].get("id")))
        if not upload or upload["status"] != "uploaded":
            invalid("The file upload must exist and have status uploaded.")
        # Ownership is verified by the route before normalizing attachments.
        upload["expiry_time"] = None
        return {
            "type": "file",
            "_file_id": upload["id"],
            **{k: v for k, v in result.items() if k not in ("type", "file_upload")},
        }
    invalid("Expected an external file, file_upload, or emoji.")


def check_upload_access(ctx: ProviderContext, raw: Any, client_id: str) -> None:
    if isinstance(raw, dict):
        if "file_upload" in raw:
            if not isinstance(raw["file_upload"], dict):
                invalid("file_upload must be an object containing id.")
            upload = ctx.store["file_uploads"].get(uid(raw["file_upload"].get("id")))
            if not upload or upload.get("_client_id") != client_id:
                invalid("File upload is not accessible to this integration.")
            if upload["expiry_time"] and upload["expiry_time"] <= ctx.clock.now().isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"):
                invalid("File upload has expired.")
        for value in raw.values():
            check_upload_access(ctx, value, client_id)
    elif isinstance(raw, list):
        for value in raw:
            check_upload_access(ctx, value, client_id)


def property_value(ctx: ProviderContext, spec: dict[str, Any], raw: dict[str, Any] | None) -> dict[str, Any]:
    kind = spec["type"]
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        invalid("Property values must be objects.")
    if raw.get("type", kind) != kind or any(k in TYPES and k != kind for k in raw):
        invalid(f"{spec['name']} is expected to be {kind}.")
    value = copy.deepcopy(raw.get(kind))
    if kind in ("title", "rich_text"):
        value = rich_text(value or [], ctx)
    elif kind in ("people", "relation", "multi_select", "files"):
        value = value or []
        if not isinstance(value, list) or len(value) > 100:
            invalid(f"{kind} must be an array with at most 100 items.")
        if any(not isinstance(item, dict) for item in value):
            invalid(f"{kind} items must be objects.")
        if kind == "people":
            value = [partial_user(uid(v.get("id"))) for v in value]
            if any(ctx.store["users"].get(v["id"]) is None for v in value):
                invalid("People property contains an unknown user.")
        elif kind == "relation":
            value = [{"id": uid(v.get("id"))} for v in value]
        elif kind == "files":
            value = [attachment(ctx, v) for v in value]
        else:
            value = [option(spec, v) for v in value]
    elif kind in ("select", "status"):
        value = option(spec, value) if value is not None else None
    elif kind == "checkbox":
        value = False if value is None else value
        if not isinstance(value, bool):
            invalid("checkbox must be a boolean.")
    elif kind == "number":
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            invalid("number must be a number or null.")
    elif kind == "date" and value is not None:
        from emulators.core.clock import parse_instant

        if not isinstance(value, dict) or not isinstance(value.get("start"), str):
            invalid("date must contain a start date.")
        try:
            parse_instant(value["start"])
            if value.get("end"):
                parse_instant(value["end"])
        except ValueError:
            invalid("Invalid ISO 8601 date.")
        value = {"start": value["start"], "end": value.get("end"), "time_zone": value.get("time_zone")}
    elif kind in ("url", "email", "phone_number") and value is not None and not isinstance(value, str):
        invalid(f"{kind} must be a string or null.")
    return {
        "id": spec["id"],
        "type": kind,
        kind: value,
        **({"has_more": False} if kind == "relation" else {}),
    }


def option(spec: dict[str, Any], raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not (raw.get("name") or raw.get("id")):
        invalid("Select options require name or id.")
    options = spec[spec["type"]].setdefault("options", [])
    found = next(
        (
            o
            for o in options
            if (raw.get("id") and o["id"] == raw["id"]) or (raw.get("name") and o["name"] == raw["name"])
        ),
        None,
    )
    if found is None:
        if spec["type"] == "status" or not raw.get("name"):
            invalid("Unknown select/status option.")
        found = {
            "id": stable_guid(spec["id"], raw["name"]),
            "name": raw["name"],
            "color": raw.get("color", "default"),
        }
        options.append(found)
    return copy.deepcopy(found)


def values(
    ctx: ProviderContext, specs: dict[str, Any], raw: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        invalid("properties must be an object.")
    result = copy.deepcopy(existing or {})
    for key, value in raw.items():
        name, spec = property_by_key(specs, key)
        if spec["type"] in READ_ONLY:
            invalid(f"{name} is read-only.")
        result[name] = property_value(ctx, spec, value)
    for name, spec in specs.items():
        if name not in result:
            result[name] = property_value(ctx, spec, None)
    return result


def computed(record: dict[str, Any], specs: dict[str, Any]) -> None:
    for name, spec in specs.items():
        kind = spec["type"]
        if kind in {"created_time", "last_edited_time", "created_by", "last_edited_by"}:
            record["properties"][name] = {"id": spec["id"], "type": kind, kind: record[kind]}
        elif kind == "formula":
            expression = spec[kind].get("expression", "")
            match = re.fullmatch(r'prop\("([^"\n]+)"\)(?:\s*\*\s*(-?\d+(?:\.\d+)?))?', expression)
            value: Any = None
            subtype = "string"
            if match:
                prop = record["properties"].get(match[1], {})
                subtype = {"rich_text": "string", "title": "string", "checkbox": "boolean"}.get(
                    prop.get("type"), prop.get("type", "string")
                )
                value = prop.get(prop.get("type"))
                if subtype == "string" and isinstance(value, list):
                    value = "".join(v.get("plain_text", "") for v in value)
                if match[2] and isinstance(value, (int, float)):
                    value *= float(match[2])
            elif expression in ("true", "false"):
                subtype, value = "boolean", expression == "true"
            elif re.fullmatch(r"-?\d+(?:\.\d+)?", expression):
                subtype, value = "number", float(expression)
            else:
                value = expression.strip('"')
            record["properties"][name] = {
                "id": spec["id"],
                "type": "formula",
                "formula": {"type": subtype, subtype: value},
            }
