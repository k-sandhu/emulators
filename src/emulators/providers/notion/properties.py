"""Database schema and page property normalization."""

from __future__ import annotations

import copy
import math
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
from emulators.providers.notion.validation import color, date_value, string, url

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
NUMBER_FORMATS = {
    "number",
    "number_with_commas",
    "percent",
    "dollar",
    "australian_dollar",
    "canadian_dollar",
    "singapore_dollar",
    "euro",
    "pound",
    "yen",
    "ruble",
    "rupee",
    "won",
    "yuan",
    "real",
    "lira",
    "rupiah",
    "franc",
    "hong_kong_dollar",
    "new_zealand_dollar",
    "krona",
    "norwegian_krone",
    "mexican_peso",
    "rand",
    "new_taiwan_dollar",
    "danish_krone",
    "zloty",
    "baht",
    "forint",
    "koruna",
    "shekel",
    "chilean_peso",
    "philippine_peso",
    "dirham",
    "colombian_peso",
    "riyal",
    "ringgit",
    "leu",
    "argentine_peso",
    "uruguayan_peso",
    "peruvian_sol",
    "vietnamese_dong",
    "pakistani_rupee",
    "nigerian_naira",
    "bitcoin",
}


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
            if original and original["type"] == "title":
                invalid("The title property cannot be removed.")
            continue
        if not isinstance(value, dict):
            invalid("Property schemas must be objects or null.")
        if sum(key in TYPES for key in value) > 1:
            invalid("A property schema must specify only one type.")
        name = value.get("name", original["name"] if original else name)
        string(name, "Property name", empty=False)
        kind = value.get("type", next((k for k in value if k in TYPES), None) or (original or {}).get("type"))
        if not isinstance(kind, str) or kind not in TYPES:
            invalid(f"Invalid property type for {name}.")
        if original and original["type"] == "title" and kind != "title":
            invalid("The title property cannot be converted.")
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
                string(option["name"], "Option name", empty=False)
                if "," in option["name"]:
                    invalid("Option names cannot contain commas.")
                option.setdefault("id", stable_guid(source_id, name, option["name"]))
                option.setdefault("color", "default")
                string(option["id"], "Option id", empty=False)
                color(option["color"])
            if len({o["name"].casefold() for o in options}) != len(options):
                invalid("Option names must be unique, ignoring case.")
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
            if not isinstance(config["format"], str) or config["format"] not in NUMBER_FORMATS:
                invalid("number.format must be a supported Notion number format.")
        if kind == "relation":
            config.setdefault("type", "single_property")
            if config["type"] not in ("single_property", "dual_property"):
                invalid("relation.type must be single_property or dual_property.")
            config.setdefault(config["type"], {})
            if not isinstance(config[config["type"]], dict):
                invalid("Relation configuration must be an object.")
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
    if existing:
        original_title = next(p for p in existing.values() if p["type"] == "title")
        current_title = next(p for p in result.values() if p["type"] == "title")
        if original_title["id"] != current_title["id"]:
            invalid("The title property cannot be removed or converted.")
    return result


def attachment(ctx: ProviderContext, raw: Any, *, icon: bool = False) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        invalid("File or icon must be an object.")
    result = copy.deepcopy(raw)
    if any(key.startswith("_") for key in result):
        invalid("File objects contain an unsupported field.")
    kind = result.get("type", next((k for k in ("external", "file_upload", "emoji") if k in result), None))
    if kind == "emoji" and icon and isinstance(result.get("emoji"), str):
        string(result["emoji"], "emoji", empty=False)
        return {"type": "emoji", "emoji": result["emoji"]}
    extra = {k: result[k] for k in ("name", "caption") if k in result}
    if "name" in extra:
        string(extra["name"], "File name")
    if kind == "external":
        if not isinstance(result.get("external"), dict):
            invalid("external must be an object containing url.")
        return {"type": "external", "external": {"url": url(result["external"].get("url"))}, **extra}
    if kind == "file_upload":
        if not isinstance(result.get("file_upload"), dict):
            invalid("file_upload must be an object containing id.")
        upload = ctx.store["file_uploads"].get(uid(result["file_upload"].get("id")))
        if not upload or upload["status"] != "uploaded":
            invalid("The file upload must exist and have status uploaded.")
        # Ownership is verified by the route before normalizing attachments.
        return {
            "type": "file",
            "_file_id": upload["id"],
            **extra,
        }
    invalid("Expected an external file, file_upload, or emoji.")


def attach_uploads(ctx: ProviderContext, raw: Any) -> None:
    """Commit attachment expiry only after the entire write has validated."""
    if isinstance(raw, dict):
        if file_id := raw.get("_file_id"):
            upload = ctx.store["file_uploads"].get(file_id)
            if upload:
                upload["expiry_time"] = None
        for value in raw.values():
            attach_uploads(ctx, value)
    elif isinstance(raw, list):
        for value in raw:
            attach_uploads(ctx, value)


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
        value = rich_text([] if kind not in raw else value, ctx)
    elif kind in ("people", "relation", "multi_select", "files"):
        value = [] if kind not in raw else value
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
            if any(not isinstance(v.get("name"), str) for v in value):
                invalid("File properties require a name for every file.")
        else:
            value = [option(spec, v) for v in value]
    elif kind in ("select", "status"):
        value = option(spec, value) if value is not None else None
    elif kind == "checkbox":
        value = False if kind not in raw else value
        if not isinstance(value, bool):
            invalid("checkbox must be a boolean.")
    elif kind == "number":
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            invalid("number must be a number or null.")
    elif kind == "date" and value is not None:
        value = date_value(value)
    elif kind in ("url", "email", "phone_number") and value is not None:
        string(value, kind, maximum=2000 if kind == "url" else 200)
        if kind == "url" and value:
            url(value)
    return {
        "id": spec["id"],
        "type": kind,
        kind: value,
        **({"has_more": False} if kind == "relation" else {}),
    }


def option(spec: dict[str, Any], raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not (raw.get("name") or raw.get("id")):
        invalid("Select options require name or id.")
    for field in ("name", "id"):
        if field in raw:
            string(raw[field], f"Option {field}", empty=False)
    if "name" in raw and "," in raw["name"]:
        invalid("Option names cannot contain commas.")
    color(raw.get("color", "default"))
    options = spec[spec["type"]].setdefault("options", [])
    found = next(
        (
            o
            for o in options
            if (raw.get("id") and o["id"] == raw["id"])
            or (raw.get("name") and o["name"].casefold() == raw["name"].casefold())
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
        if not isinstance(value, dict):
            invalid("Property values must be objects.")
        name, spec = property_by_key(specs, key)
        if spec["type"] in READ_ONLY:
            invalid(f"{name} is read-only.")
        if spec["type"] not in value:
            invalid(f"{name} requires a {spec['type']} value.")
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
                prop_kind = prop.get("type", "")
                subtype = {
                    "number": "number",
                    "date": "date",
                    "checkbox": "boolean",
                    "created_time": "date",
                    "last_edited_time": "date",
                }.get(prop_kind, "string")
                value = prop.get(prop_kind)
                if subtype == "string" and isinstance(value, list):
                    value = "".join(v.get("plain_text", "") for v in value)
                elif prop_kind in ("select", "status"):
                    value = value.get("name") if value else None
                elif subtype == "string" and value is not None and not isinstance(value, str):
                    value = None
                elif prop_kind in ("created_time", "last_edited_time") and value:
                    value = {"start": value, "end": None, "time_zone": None}
                if match[2]:
                    if subtype == "number" and isinstance(value, (int, float)):
                        try:
                            value *= float(match[2])
                        except OverflowError:
                            value = None
                    else:
                        subtype, value = "number", None
            elif expression in ("true", "false"):
                subtype, value = "boolean", expression == "true"
            elif re.fullmatch(r"-?\d+(?:\.\d+)?", expression):
                subtype, value = "number", float(expression)
            else:
                value = expression.strip('"')
            if isinstance(value, float) and not math.isfinite(value):
                value = None
            record["properties"][name] = {
                "id": spec["id"],
                "type": "formula",
                "formula": {"type": subtype, subtype: value},
            }
